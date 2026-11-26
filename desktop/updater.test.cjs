'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const { execFileSync, spawn } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const updater = require('./updater.cjs');

function makeHeaders(map = {}) {
  const lower = new Map(Object.entries(map).map(([k, v]) => [k.toLowerCase(), String(v)]));
  return {
    get(name) {
      return lower.get(String(name).toLowerCase()) ?? null;
    },
  };
}

function makeByteStream(chunks) {
  let index = 0;
  return {
    getReader() {
      return {
        async read() {
          if (index >= chunks.length) return { done: true, value: undefined };
          const value = chunks[index++];
          return { done: false, value };
        },
        releaseLock() {},
      };
    },
  };
}

function makeFakeAppBundle(parentDir, version, { sign = true, bundleId = updater.EXPECTED_BUNDLE_ID } = {}) {
  const appPath = path.join(parentDir, updater.APP_BUNDLE_NAME);
  const macosDir = path.join(appPath, 'Contents', 'MacOS');
  const resDir = path.join(appPath, 'Contents', 'Resources');
  const mgrDir = path.join(resDir, 'framework', 'manager');
  fs.mkdirSync(macosDir, { recursive: true });
  fs.mkdirSync(mgrDir, { recursive: true });

  const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleExecutable</key><string>Unified Memory</string>
  <key>CFBundleIdentifier</key><string>${bundleId}</string>
  <key>CFBundleName</key><string>Unified Memory</string>
  <key>CFBundleShortVersionString</key><string>${version}</string>
  <key>CFBundleVersion</key><string>${version}</string>
  <key>CFBundlePackageType</key><string>APPL</string>
</dict></plist>
`;
  fs.writeFileSync(path.join(appPath, 'Contents', 'Info.plist'), plist, 'utf8');
  const binPath = path.join(macosDir, 'Unified Memory');
  fs.writeFileSync(binPath, '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  fs.writeFileSync(path.join(resDir, 'umem-python'), '#!/bin/sh\nexit 0\n', { mode: 0o755 });
  fs.writeFileSync(path.join(mgrDir, 'manager.py'), '# marker ' + version + '\n', 'utf8');
  if (sign && process.platform === 'darwin') {
    execFileSync('/usr/bin/codesign', ['--force', '--deep', '--sign', '-', appPath]);
  }
  return appPath;
}

test('version parsing, SHA-256 parsing, and URL allowlist', () => {
  assert.equal(updater.isNewerVersion('v0.1.23', '0.1.22'), true);
  assert.equal(updater.isNewerVersion('0.2.0', '0.1.99'), true);
  assert.equal(updater.isNewerVersion('v0.1.22', '0.1.22'), false);
  assert.equal(updater.isNewerVersion('0.1.21', '0.1.22'), false);
  assert.equal(updater.isNewerVersion('invalid', '0.1.22'), false);

  const hex = 'a'.repeat(64);
  assert.equal(updater.parseSha256Digest(`sha256:${hex.toUpperCase()}`), hex);
  assert.equal(updater.parseSha256Digest(`${hex}  Unified-Memory-macOS-arm64.zip\n`), hex);
  assert.equal(updater.parseSha256Digest('not-a-hash'), '');

  assert.equal(updater.isAllowedReleaseUrl('https://github.com/chenwenxinqaq/baton/releases/download/v0.1.22/a.zip'), true);
  assert.equal(updater.isAllowedReleaseUrl('https://release-assets.githubusercontent.com/some/path'), true);
  assert.equal(updater.isAllowedReleaseUrl('http://github.com/chenwenxinqaq/baton'), false);
  assert.equal(updater.isAllowedReleaseUrl('https://evil.example.com/a.zip'), false);
});

test('fetchLatestRelease uses ETag 304 cache and falls back to github web JSON on API 403 rate limit', async () => {
  const cachedRelease = {
    tag: 'v0.1.23',
    version: '0.1.23',
    name: 'Unified Memory 0.1.23',
    notes: 'Cached notes',
    publishedAt: '2026-11-26T00:00:00Z',
    htmlUrl: 'https://github.com/chenwenxinqaq/baton/releases/tag/v0.1.23',
    assetName: 'Unified-Memory-macOS-arm64.zip',
    hasAsset: true,
    downloadUrl: 'https://github.com/chenwenxinqaq/baton/releases/download/v0.1.23/Unified-Memory-macOS-arm64.zip',
    sha256: 'b'.repeat(64),
    sha256Url: 'https://github.com/chenwenxinqaq/baton/releases/download/v0.1.23/Unified-Memory-macOS-arm64.zip.sha256',
    size: 12345,
    source: 'github-api',
  };

  let seenIfNoneMatch = '';
  const cachedRes = await updater.fetchLatestRelease({
    currentVersion: '0.1.22',
    arch: 'arm64',
    cache: { etag: 'W/"abc"', arch: 'arm64', release: cachedRelease },
    fetchImpl: async (_url, opts) => {
      seenIfNoneMatch = opts.headers['If-None-Match'] || '';
      return { status: 304, ok: false, headers: makeHeaders({ etag: 'W/"abc"' }) };
    },
  });
  assert.equal(seenIfNoneMatch, 'W/"abc"');
  assert.equal(cachedRes.fromCache, true);
  assert.equal(cachedRes.release.version, '0.1.23');

  const sha = 'c'.repeat(64);
  const fallbackRes = await updater.fetchLatestRelease({
    currentVersion: '0.1.22',
    arch: 'arm64',
    fetchImpl: async (url) => {
      if (url === updater.LATEST_RELEASE_API) {
        return { status: 403, ok: false, headers: makeHeaders({ 'x-ratelimit-remaining': '0' }) };
      }
      if (url === updater.RELEASES_URL) {
        return {
          status: 200,
          ok: true,
          url: '',
          headers: makeHeaders({}),
          text: async () => JSON.stringify({ id: 1, tag_name: 'v0.1.24' }),
        };
      }
      if (url.endsWith('.sha256')) {
        return {
          status: 200,
          ok: true,
          headers: makeHeaders({}),
          text: async () => `${sha}  Unified-Memory-macOS-arm64.zip\n`,
        };
      }
      throw new Error(`unexpected url ${url}`);
    },
  });
  assert.equal(fallbackRes.release.version, '0.1.24');
  assert.equal(fallbackRes.release.source, 'github-web');
  assert.equal(fallbackRes.release.sha256, sha);
});

test('downloadAsset streams, verifies SHA-256, reuses valid cache, and rejects bad digest or untrusted redirect', async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'umem-dl-test-'));
  try {
    const payload = Buffer.from('hello unified memory update zip payload');
    const digest = crypto.createHash('sha256').update(payload).digest('hex');
    const dest = path.join(tmp, 'Unified-Memory-macOS-arm64.zip');
    let fetchCalls = 0;
    const progressEvents = [];

    const fakeFetch = async () => {
      fetchCalls++;
      return {
        ok: true,
        status: 200,
        url: 'https://release-assets.githubusercontent.com/asset.zip',
        headers: makeHeaders({ 'content-length': payload.length }),
        body: makeByteStream([payload.subarray(0, 12), payload.subarray(12)]),
      };
    };

    const res1 = await updater.downloadAsset({
      url: 'https://github.com/chenwenxinqaq/baton/releases/download/v0.1.23/Unified-Memory-macOS-arm64.zip',
      destPath: dest,
      expectedSha256: digest,
      expectedSize: payload.length,
      fetchImpl: fakeFetch,
      onProgress: p => progressEvents.push(p),
    });
    assert.equal(res1.reused, false);
    assert.equal(res1.sha256, digest);
    assert.equal(fetchCalls, 1);
    assert.ok(progressEvents.some(p => p.percent === 100));

    const res2 = await updater.downloadAsset({
      url: 'https://github.com/chenwenxinqaq/baton/releases/download/v0.1.23/Unified-Memory-macOS-arm64.zip',
      destPath: dest,
      expectedSha256: digest,
      expectedSize: payload.length,
      fetchImpl: fakeFetch,
    });
    assert.equal(res2.reused, true);
    assert.equal(fetchCalls, 1);

    const badDest = path.join(tmp, 'bad.zip');
    await assert.rejects(
      updater.downloadAsset({
        url: 'https://github.com/chenwenxinqaq/baton/releases/download/v0.1.23/Unified-Memory-macOS-arm64.zip',
        destPath: badDest,
        expectedSha256: '0'.repeat(64),
        fetchImpl: fakeFetch,
        maxRetries: 0,
      }),
      /SHA-256 校验失败/
    );
    assert.equal(fs.existsSync(badDest), false);

    await assert.rejects(
      updater.downloadAsset({
        url: 'https://github.com/chenwenxinqaq/baton/releases/download/v0.1.23/Unified-Memory-macOS-arm64.zip',
        destPath: path.join(tmp, 'evil.zip'),
        fetchImpl: async () => ({
          ok: true,
          status: 200,
          url: 'https://evil.example.com/payload.zip',
          headers: makeHeaders({}),
          body: makeByteStream([payload]),
        }),
        maxRetries: 0,
      }),
      /重定向到非官方地址/
    );
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test('inspectInstallTarget detects AppTranslocation, Volumes, dev-mode, and permission issues', () => {
  assert.equal(
    updater.inspectInstallTarget({
      execPath: '/Applications/Unified Memory.app/Contents/MacOS/Unified Memory',
      platform: 'darwin',
      isPackaged: true,
      accessCheck: () => {},
    }).canReplaceInPlace,
    true
  );
  assert.equal(
    updater.inspectInstallTarget({
      execPath: '/private/var/folders/x/AppTranslocation/ABC/d/Unified Memory.app/Contents/MacOS/Unified Memory',
      platform: 'darwin',
      isPackaged: true,
      accessCheck: () => {},
    }).reason,
    'translocated'
  );
  assert.equal(
    updater.inspectInstallTarget({
      execPath: '/Volumes/Unified Memory/Unified Memory.app/Contents/MacOS/Unified Memory',
      platform: 'darwin',
      isPackaged: true,
      accessCheck: () => {},
    }).reason,
    'readonly-volume'
  );
  assert.equal(
    updater.inspectInstallTarget({
      execPath: '/Applications/Unified Memory.app/Contents/MacOS/Unified Memory',
      platform: 'darwin',
      isPackaged: true,
      accessCheck: () => { throw new Error('EACCES'); },
    }).reason,
    'permission-denied'
  );
  assert.equal(
    updater.inspectInstallTarget({
      execPath: '/usr/local/bin/electron',
      platform: 'darwin',
      isPackaged: false,
      accessCheck: () => {},
    }).reason,
    'dev-mode'
  );
});

test('update controller coalesces checks, keeps silent failures quiet, and respects skip/backoff', async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'umem-ctrl-test-'));
  try {
    const prefsPath = path.join(tmp, 'prefs.json');
    let now = 1_700_000_000_000;
    let apiCalls = 0;
    let failMode = false;
    const ctrl = updater.createUpdateController({
      currentVersion: '0.1.22',
      arch: 'arm64',
      platform: 'darwin',
      isPackaged: true,
      execPath: '/Applications/Unified Memory.app/Contents/MacOS/Unified Memory',
      cacheRoot: path.join(tmp, 'updates'),
      logFile: path.join(tmp, 'updater.log'),
      resultFile: path.join(tmp, 'update-result.json'),
      readPrefs: () => updater.readJsonSafe(prefsPath, {}),
      writePrefs: patch => updater.writeJsonAtomic(prefsPath, { ...updater.readJsonSafe(prefsPath, {}), ...patch }),
      nowFn: () => now,
      fetchImpl: async () => {
        apiCalls++;
        await new Promise(r => setTimeout(r, 20));
        if (failMode) throw new Error('offline');
        return {
          ok: true,
          status: 200,
          headers: makeHeaders({ etag: 'W/"v23"' }),
          json: async () => ({
            tag_name: 'v0.1.23',
            name: 'Unified Memory 0.1.23',
            body: 'Fixes auto-updater',
            html_url: 'https://github.com/chenwenxinqaq/baton/releases/tag/v0.1.23',
            assets: [{
              name: 'Unified-Memory-macOS-arm64.zip',
              size: 100,
              digest: 'sha256:' + 'd'.repeat(64),
              browser_download_url: 'https://github.com/chenwenxinqaq/baton/releases/download/v0.1.23/Unified-Memory-macOS-arm64.zip',
            }],
          }),
        };
      },
    });

    const [r1, r2] = await Promise.all([
      ctrl.checkForUpdates({ manual: false }),
      ctrl.checkForUpdates({ manual: false }),
    ]);
    assert.equal(apiCalls, 1);
    assert.equal(r1.newer, true);
    assert.equal(r2.snapshot.latestVersion, '0.1.23');

    ctrl.skipVersion('v0.1.23');
    const skippedCheck = await ctrl.checkForUpdates({ manual: false });
    assert.equal(skippedCheck.skipped, true);

    // Manual check clears skippedVersion for the same version.
    const manualCheck = await ctrl.checkForUpdates({ manual: true });
    assert.equal(manualCheck.skipped, false);
    assert.equal(manualCheck.snapshot.skippedVersion, '');

    // Background failure must not surface an error banner; error retry backoff kicks in.
    failMode = true;
    now += 25 * 60 * 60 * 1000;
    assert.equal(ctrl.shouldRunScheduledCheck(), true);
    const silentErr = await ctrl.checkForUpdates({ manual: false });
    assert.ok(silentErr.error);
    assert.notEqual(ctrl.getState().status, 'error');
    assert.equal(ctrl.shouldRunScheduledCheck(), false);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});

test('macOS stageMacApp + atomic installer swap and automatic rollback on bad signature', { skip: process.platform !== 'darwin' }, async () => {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'umem-swap-test-'));
  try {
    const targetDir = path.join(tmp, 'Apps');
    const buildDir = path.join(tmp, 'Build');
    fs.mkdirSync(targetDir, { recursive: true });
    fs.mkdirSync(buildDir, { recursive: true });

    const targetApp = makeFakeAppBundle(targetDir, '0.1.22');
    const newApp = makeFakeAppBundle(buildDir, '0.1.99');
    const zipPath = path.join(tmp, 'Unified-Memory-macOS-arm64.zip');
    execFileSync('/usr/bin/ditto', ['-c', '-k', '--sequesterRsrc', '--keepParent', newApp, zipPath]);

    // Wrong expected version fails and cleans up staging dir.
    const badStageDir = path.join(tmp, 'stage-bad');
    await assert.rejects(
      updater.stageMacApp({ zipPath, stagingDir: badStageDir, expectedVersion: '0.1.98' }),
      /版本不匹配/
    );
    assert.equal(fs.existsSync(badStageDir), false);

    // Valid staging succeeds.
    const stageDir = path.join(tmp, 'stage-ok');
    const staged = await updater.stageMacApp({ zipPath, stagingDir: stageDir, expectedVersion: '0.1.99' });
    assert.equal(staged.bundleVersion, '0.1.99');

    // Spawn a short-lived dummy process to simulate the exiting app PID.
    const sleeper = spawn('/bin/sleep', ['0.4']);
    const resultFile = path.join(tmp, 'update-result.json');
    const logFile = path.join(tmp, 'updater.log');
    const { child } = updater.launchMacInstaller({
      stagedApp: staged.stagedApp,
      targetApp,
      oldPid: sleeper.pid,
      resultFile,
      logFile,
      scriptDir: tmp,
      fromVersion: '0.1.22',
      toVersion: '0.1.99',
      verifyCodesign: true,
      relaunch: false,
    });
    child.ref();
    await new Promise(resolve => child.once('exit', resolve));
    const res = updater.consumeUpdateResult(resultFile);
    assert.equal(res.status, 'updated');
    assert.equal(res.to, '0.1.99');
    assert.equal(fs.existsSync(resultFile), false);
    assert.match(
      fs.readFileSync(path.join(targetApp, 'Contents', 'Resources', 'framework', 'manager', 'manager.py'), 'utf8'),
      /# marker 0\.1\.99/
    );

    // Now test rollback: tamper with staged app after staging so post-swap codesign fails.
    const stageDir2 = path.join(tmp, 'stage-tampered');
    const staged2 = await updater.stageMacApp({ zipPath, stagingDir: stageDir2, expectedVersion: '0.1.99' });
    fs.writeFileSync(
      path.join(staged2.stagedApp, 'Contents', 'Resources', 'framework', 'manager', 'manager.py'),
      '# tampered after signing\n',
      'utf8'
    );
    const sleeper2 = spawn('/bin/sleep', ['0.2']);
    const { child: child2 } = updater.launchMacInstaller({
      stagedApp: staged2.stagedApp,
      targetApp,
      oldPid: sleeper2.pid,
      resultFile,
      logFile,
      scriptDir: tmp,
      fromVersion: '0.1.99',
      toVersion: '0.2.0',
      verifyCodesign: true,
      relaunch: false,
    });
    child2.ref();
    await new Promise(resolve => child2.once('exit', resolve));
    const rollbackRes = updater.consumeUpdateResult(resultFile);
    assert.equal(rollbackRes.status, 'rolled_back');
    // Target app must still be intact v0.1.99!
    assert.match(
      fs.readFileSync(path.join(targetApp, 'Contents', 'Resources', 'framework', 'manager', 'manager.py'), 'utf8'),
      /# marker 0\.1\.99/
    );
    execFileSync('/usr/bin/codesign', ['--verify', '--deep', '--strict', targetApp]);
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
});
