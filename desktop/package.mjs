import { packager } from '@electron/packager';
import { unpackPython, unpackGitTools } from './runtime.mjs';
import { createHash } from 'node:crypto';
import fs from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.dirname(here);
const manifest = JSON.parse(await fs.readFile(path.join(here, 'package.json')));
if (process.platform !== 'darwin') throw Error('Build this macOS release on a Mac.');
const temp = await fs.mkdtemp(path.join(os.tmpdir(), 'umem-package-'));
const out = path.join(root, 'dist');
try {
  const appDir = path.join(temp, 'app');
  const framework = path.join(temp, 'framework');
  await fs.mkdir(appDir);
  for (const file of ['main.cjs', 'preload.cjs', 'updater.cjs']) {
    await fs.copyFile(path.join(here, file), path.join(appDir, file));
  }
  // Menu-bar glyph: black-on-transparent template images (macOS tints them for light/dark).
  for (const [file, pixels] of [['trayTemplate.png', '18'], ['trayTemplate@2x.png', '36']]) {
    execFileSync('sips', ['-s','format','png','-z',pixels,pixels,path.join(here,'tray.svg'),'--out',path.join(appDir,file)]);
  }
  const { name, productName, version, description, main } = manifest;
  await fs.writeFile(path.join(appDir, 'package.json'), JSON.stringify({ name, productName, version, description, main }));
  const python = process.env.PYTHON || 'python3';
  const files = JSON.parse(execFileSync(python, ['-B','-c',
    'import json; from package_release import PUBLIC_FILES; print(json.dumps(PUBLIC_FILES))'],
    { cwd: path.join(root, 'scripts'), encoding: 'utf8' }));
  for (const file of files.filter(f => !f.startsWith('desktop/') && !f.startsWith('tests/'))) {
    const target = path.join(framework, file);
    await fs.mkdir(path.dirname(target), { recursive: true });
    await fs.copyFile(path.join(root,file),target);
  }
  const icons = path.join(temp, 'Memory.iconset');
  await fs.mkdir(icons);
  const original = path.join(temp, 'symbol.png');
  execFileSync('sips', ['-s','format','png','-z','1024','1024',path.join(here,'icon.svg'),'--out',original]);
  for (const size of [16,32,128,256,512]) for (const scale of [1,2]) {
    const pixels = String(size * scale);
    execFileSync('sips',['-z',pixels,pixels,original,'--out',
      path.join(icons,'icon_' + size + 'x' + size + (scale === 2 ? '@2x' : '') + '.png')]);
  }
  const icon = path.join(temp,'Memory.icns');
  execFileSync('iconutil',['-c','icns',icons,'-o',icon]);
  const extraResource = [framework, path.join(here,'umem-python')];
  const runtime = process.env.UMEM_PYTHON_DIST || unpackPython(temp);
  const [build] = await packager({dir:appDir, out, name:productName, appVersion:version,
    platform:'darwin', arch:process.arch, icon, appBundleId:'local.unifiedmemory.desktop',
    electronVersion:manifest.devDependencies.electron, electronZipDir:process.env.ELECTRON_ZIP_DIR,
    asar:true, extraResource, overwrite:true, prune:false, osxSign:false
  });
  const appPath = path.join(build,productName + '.app');
  // Packager resolves extraResource symlinks into build-time absolute paths.
  // Preserve the runtime's relative links so Python still starts after moving the app.
  await fs.cp(runtime,path.join(appPath,'Contents','Resources','python'),{recursive:true,verbatimSymlinks:true});
  unpackGitTools(path.join(appPath, 'Contents', 'Resources'));
  const notices = path.join(appPath,'Contents','Resources','licenses');
  await fs.cp(path.join(here, 'licenses'), notices, {recursive:true});
  for (const name of ['LICENSE','LICENSES.chromium.html']) await fs.copyFile(path.join(build,name),path.join(notices,name));
  execFileSync('codesign',['--force','--deep','--sign','-',appPath]);
  execFileSync('codesign',['--verify','--deep','--strict',appPath]);
  const zip = path.join(out,'Unified-Memory-macOS-' + process.arch + '.zip');
  execFileSync('ditto',['-c','-k','--sequesterRsrc','--keepParent',appPath,zip]);
  const sha = createHash('sha256').update(await fs.readFile(zip)).digest('hex');
  await fs.writeFile(zip + '.sha256', `${sha}  ${path.basename(zip)}\n`, 'utf8');
  console.log(zip);
} finally { await fs.rm(temp,{recursive:true,force:true}); }
