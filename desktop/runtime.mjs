import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// Verify cached downloads against pinned upstream release digests before extracting.
function unpack(directory, filename, digest, releaseURL) {
  const cache = path.join(os.homedir(), 'Library', 'Caches', 'unified-memory-build');
  fs.mkdirSync(cache, {recursive: true});
  const file = path.join(cache, filename);
  const valid = name => fs.existsSync(name) && createHash('sha256').update(fs.readFileSync(name)).digest('hex') === digest;
  if (!valid(file)) {
    const staged = file + '.' + process.pid + '.part';
    try {
      execFileSync('curl', ['--fail','--location','--retry','2','--connect-timeout','15','--max-time','600',
        '-o',staged,releaseURL + '/' + encodeURIComponent(filename)], {stdio:'inherit'});
      if (!valid(staged)) throw Error('Release archive checksum mismatch for ' + filename);
      fs.renameSync(staged, file);
    } finally { fs.rmSync(staged, {force:true}); }
  }
  fs.mkdirSync(directory, {recursive:true});
  execFileSync('tar', ['-xzf', file, '-C', directory]);
}

export function unpackPython(directory) {
  unpack(directory, 'cpython-3.13.18+20261110-aarch64-apple-darwin-install_only_stripped.tar.gz',
    '7a2384641584ab9984221fcb0fc9265df465922af71be71410529e2a9c3f0540',
    'https://github.com/astral-sh/python-build-standalone/releases/download/20261110');
  return path.join(directory, 'python');
}

export function unpackGitTools(resources) {
  if (process.arch !== 'arm64') throw Error('The bundled Git tools require an Apple Silicon build.');
  unpack(path.join(resources, 'git'), 'dugite-native-v2.56.1-238326e-macOS-arm64.tar.gz',
    'a882ec6882416b57d212a22bac845ae27944f96cf73453a864f6a12c17863a79',
    'https://github.com/desktop/dugite-native/releases/download/v2.56.1');
}
