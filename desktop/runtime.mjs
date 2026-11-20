import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// Digest from the upstream release asset. Cache files are verified before every build.
const filename = 'cpython-3.13.18+20261110-aarch64-apple-darwin-install_only_stripped.tar.gz';
const digest = '7a2384641584ab9984221fcb0fc9265df465922af71be71410529e2a9c3f0540';
const url = 'https://github.com/astral-sh/python-build-standalone/releases/download/20261110/' + encodeURIComponent(filename);

export function unpackPython(directory) {
  if (process.arch !== 'arm64') throw Error('The pinned Python runtime targets Apple Silicon. Supply UMEM_PYTHON_DIST for other architectures.');
  const cache = path.join(os.homedir(), 'Library', 'Caches', 'unified-memory-build');
  fs.mkdirSync(cache, {recursive:true});
  const file = path.join(cache, filename);
  const valid = name => fs.existsSync(name) && createHash('sha256').update(fs.readFileSync(name)).digest('hex') === digest;
  if (!valid(file)) {
    const staged = file + '.' + process.pid + '.part';
    try {
      execFileSync('curl', ['--fail','--location','--retry','2','--connect-timeout','15','--max-time','600','-o',staged,url],{stdio:'inherit'});
      if (!valid(staged)) throw Error('Bundled Python archive checksum mismatch.');
      fs.renameSync(staged,file);
    } finally { fs.rmSync(staged,{force:true}); }
  }
  execFileSync('tar', ['-xzf',file,'-C',directory]);
  return path.join(directory, 'python');
}
