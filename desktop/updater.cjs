'use strict';

const crypto = require('node:crypto');
const { execFile, spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const { promisify } = require('node:util');

const execFileAsync = promisify(execFile);

const REPO_URL = 'https://github.com/chenwenxinqaq/baton';
const RELEASES_URL = REPO_URL + '/releases/latest';
const LATEST_RELEASE_API = 'https://api.github.com/repos/chenwenxinqaq/baton/releases/latest';
const EXPECTED_BUNDLE_ID = 'local.unifiedmemory.desktop';
const APP_BUNDLE_NAME = 'Unified Memory.app';
const DEFAULT_UPDATE_INTERVAL_MS = 24 * 60 * 60 * 1000;
const DEFAULT_ERROR_RETRY_MS = 60 * 60 * 1000;
const DEFAULT_REQUEST_TIMEOUT_MS = 15000;
const DEFAULT_STALL_TIMEOUT_MS = 30000;
function versionParts(value) {
  const match = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(value || '').trim());
  return match ? match.slice(1).map(Number) : null;
}

function normalizeVersion(value) {
  const parts = versionParts(value);
  return parts ? parts.join('.') : '';
}

function isNewerVersion(latest, current) {
  const a = versionParts(latest);
  const b = versionParts(current);
  if (!a || !b) return false;
  for (let i = 0; i < 3; i++) {
    if (a[i] !== b[i]) return a[i] > b[i];
  }
  return false;
}

function readJsonSafe(file, fallback = {}) {
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    return parsed && typeof parsed === 'object' ? parsed : fallback;
  } catch {
    return fallback;
  }
}

function writeJsonAtomic(file, data) {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true });
  const staged = `${file}.${process.pid}.${Date.now()}.tmp`;
  try {
    fs.writeFileSync(staged, JSON.stringify(data, null, 2), 'utf8');
    fs.renameSync(staged, file);
  } finally {
    try { fs.rmSync(staged, { force: true }); } catch {}
  }
}

function isAllowedReleaseUrl(rawUrl) {
  try {
    const parsed = new URL(String(rawUrl || ''));
    const host = parsed.hostname.toLowerCase();
    return parsed.protocol === 'https:' && (host === 'github.com' || host.endsWith('.githubusercontent.com'));
  } catch {
    return false;
  }
}

function parseSha256Digest(raw) {
  const text = String(raw || '').trim();
  const prefixed = /^sha256:([0-9a-fA-F]{64})$/.exec(text);
  if (prefixed) return prefixed[1].toLowerCase();
  const sumLine = /^([0-9a-fA-F]{64})(?:\s|$)/.exec(text);
  if (sumLine) return sumLine[1].toLowerCase();
  return '';
}

function expectedAssetName(arch = process.arch) {
  return `Unified-Memory-macOS-${arch}.zip`;
}

function normalizeReleaseFromApi(release, arch = process.arch) {
  if (!release || release.draft || release.prerelease) return null;
  const tag = typeof release?.tag_name === 'string' ? release.tag_name.trim() : '';
  const version = normalizeVersion(tag);
  if (!version) return null;
  const assetName = expectedAssetName(arch);
  const assets = Array.isArray(release.assets) ? release.assets : [];
  const zipAsset = assets.find(a => a && a.name === assetName);
  const shaAsset = assets.find(a => a && a.name === `${assetName}.sha256`);
  const downloadUrl = typeof zipAsset?.browser_download_url === 'string' && isAllowedReleaseUrl(zipAsset.browser_download_url)
    ? zipAsset.browser_download_url
    : `${REPO_URL}/releases/download/${encodeURIComponent(tag)}/${encodeURIComponent(assetName)}`;
  const sha256Url = typeof shaAsset?.browser_download_url === 'string' && isAllowedReleaseUrl(shaAsset.browser_download_url)
    ? shaAsset.browser_download_url
    : `${downloadUrl}.sha256`;
  const htmlUrl = typeof release.html_url === 'string' && release.html_url.startsWith(REPO_URL + '/releases/')
    ? release.html_url
    : `${REPO_URL}/releases/tag/${encodeURIComponent(tag)}`;
  return {
    tag,
    version,
    name: typeof release.name === 'string' && release.name.trim() ? release.name.trim() : `Unified Memory ${version}`,
    notes: typeof release.body === 'string' ? release.body.trim() : '',
    publishedAt: typeof release.published_at === 'string' ? release.published_at : '',
    htmlUrl,
    assetName,
    hasAsset: Boolean(zipAsset),
    downloadUrl,
    sha256: parseSha256Digest(zipAsset?.digest),
    sha256Url,
    size: typeof zipAsset?.size === 'number' && zipAsset.size > 0 ? zipAsset.size : 0,
    source: 'github-api',
  };
}

function buildTimeoutSignal(timeoutMs, externalSignal) {
  const signals = [];
  if (timeoutMs > 0 && typeof AbortSignal.timeout === 'function') {
    signals.push(AbortSignal.timeout(timeoutMs));
  }
  if (externalSignal) signals.push(externalSignal);
  if (!signals.length) return undefined;
  if (signals.length === 1) return signals[0];
  return AbortSignal.any(signals);
}

async function fetchOptionalSha256(sha256Url, { fetchImpl, userAgent, timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS, signal }) {
  if (!sha256Url || !isAllowedReleaseUrl(sha256Url)) return '';
  try {
    const res = await fetchImpl(sha256Url, {
      headers: { 'User-Agent': userAgent },
      signal: buildTimeoutSignal(timeoutMs, signal),
    });
    if (!res.ok) return '';
    const text = await res.text();
    return parseSha256Digest(text);
  } catch {
    return '';
  }
}

function parseTagFromLocation(locationOrUrl) {
  const match = /\/releases\/tag\/(v?\d+\.\d+\.\d+)(?:[/?#]|$)/.exec(String(locationOrUrl || ''));
  return match ? match[1] : '';
}

async function fetchLatestViaWebRedirect({ arch = process.arch, fetchImpl, userAgent, timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS, signal }) {
  // Electron net.fetch throws on redirect:'manual' and leaves res.url blank, whereas
  // github.com/releases/latest with Accept: application/json follows the redirect and returns
  // {"tag_name":"vX.Y.Z",...} without consuming api.github.com's 60 req/hr IP quota.
  const res = await fetchImpl(RELEASES_URL, {
    method: 'GET',
    headers: { Accept: 'application/json', 'User-Agent': userAgent },
    signal: buildTimeoutSignal(timeoutMs, signal),
  });
  let tag = parseTagFromLocation(res.headers?.get?.('location') || res.url || '');
  if (!tag && res.ok) {
    const raw = await res.text();
    try {
      const parsed = JSON.parse(raw);
      if (typeof parsed?.tag_name === 'string') tag = parsed.tag_name.trim();
    } catch {
      tag = parseTagFromLocation(raw);
    }
  }
  const version = normalizeVersion(tag);
  if (!version) {
    throw new Error(`无法从 GitHub 发布页解析最新版本号（HTTP ${res.status}）`);
  }
  const assetName = expectedAssetName(arch);
  const downloadUrl = `${REPO_URL}/releases/download/${encodeURIComponent(tag)}/${encodeURIComponent(assetName)}`;
  const sha256Url = `${downloadUrl}.sha256`;
  const sha256 = await fetchOptionalSha256(sha256Url, { fetchImpl, userAgent, timeoutMs, signal });
  return {
    tag,
    version,
    name: `Unified Memory ${version}`,
    notes: '',
    publishedAt: '',
    htmlUrl: `${REPO_URL}/releases/tag/${encodeURIComponent(tag)}`,
    assetName,
    hasAsset: true,
    downloadUrl,
    sha256,
    sha256Url,
    size: 0,
    source: 'github-web',
  };
}

async function fetchLatestRelease({
  currentVersion,
  arch = process.arch,
  cache = null,
  fetchImpl = globalThis.fetch,
  timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS,
  signal,
} = {}) {
  const userAgent = `UnifiedMemory/${currentVersion || '0.0.0'}`;
  const headers = {
    Accept: 'application/vnd.github+json',
    'User-Agent': userAgent,
  };
  if (cache?.etag && cache?.release?.version && cache?.arch === arch) {
    headers['If-None-Match'] = cache.etag;
  }

  let apiError = null;
  try {
    const res = await fetchImpl(LATEST_RELEASE_API, {
      headers,
      signal: buildTimeoutSignal(timeoutMs, signal),
    });
    if (res.status === 304 && cache?.release?.version) {
      return {
        release: { ...cache.release, source: cache.release.source || 'github-api' },
        etag: res.headers?.get?.('etag') || cache.etag || '',
        fromCache: true,
      };
    }
    if (!res.ok) {
      const remaining = res.headers?.get?.('x-ratelimit-remaining');
      if (res.status === 403 && remaining === '0') {
        throw new Error('GitHub API 配额已耗尽（HTTP 403）');
      }
      throw new Error(`GitHub API 返回 HTTP ${res.status}`);
    }
    const payload = await res.json();
    const release = normalizeReleaseFromApi(payload, arch);
    if (!release) {
      throw new Error('GitHub 返回的发布信息缺少有效版本号');
    }
    if (!release.sha256 && release.sha256Url) {
      release.sha256 = await fetchOptionalSha256(release.sha256Url, { fetchImpl, userAgent, timeoutMs, signal });
    }
    return {
      release,
      etag: res.headers?.get?.('etag') || '',
      fromCache: false,
    };
  } catch (error) {
    if (signal?.aborted) throw error;
    apiError = error;
  }

  // Fallback avoids unauthenticated GitHub API rate limits (60 req/hr per shared IP).
  try {
    const fallback = await fetchLatestViaWebRedirect({ arch, fetchImpl, userAgent, timeoutMs, signal });
    return { release: fallback, etag: cache?.etag || '', fromCache: false };
  } catch (fallbackError) {
    const reason = apiError
      ? `${apiError.message || apiError}；备用通道失败：${fallbackError.message || fallbackError}`
      : String(fallbackError.message || fallbackError);
    throw new Error(reason);
  }
}

async function hashFileSha256(filePath) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash('sha256');
    const stream = fs.createReadStream(filePath);
    stream.on('data', chunk => hash.update(chunk));
    stream.on('error', reject);
    stream.on('end', () => resolve(hash.digest('hex')));
  });
}

async function* iterateBodyChunks(body) {
  if (body && typeof body.getReader === 'function') {
    const reader = body.getReader();
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        if (value !== undefined) yield value;
      }
    } finally {
      try { reader.releaseLock(); } catch {}
    }
    return;
  }
  for await (const chunk of body) {
    yield chunk;
  }
}

async function downloadAsset({
  url,
  destPath,
  expectedSha256 = '',
  expectedSize = 0,
  userAgent = 'UnifiedMemory/0.0.0',
  fetchImpl = globalThis.fetch,
  signal,
  onProgress,
  stallTimeoutMs = DEFAULT_STALL_TIMEOUT_MS,
  maxRetries = 1,
}) {
  if (!isAllowedReleaseUrl(url)) {
    throw new Error(`拒绝从非官方地址下载更新：${url}`);
  }
  fs.mkdirSync(path.dirname(destPath), { recursive: true });
  try {
    const dir = path.dirname(destPath);
    const prefix = `${path.basename(destPath)}.`;
    for (const entry of fs.readdirSync(dir)) {
      if (entry.startsWith(prefix) && entry.endsWith('.part')) {
        try { fs.rmSync(path.join(dir, entry), { force: true }); } catch {}
      }
    }
  } catch {}

  if (fs.existsSync(destPath)) {
    try {
      const stat = fs.statSync(destPath);
      if (stat.isFile() && stat.size > 0) {
        const sizeOk = !expectedSize || stat.size === expectedSize;
        if (expectedSha256) {
          const existingHash = await hashFileSha256(destPath);
          if (sizeOk && existingHash === expectedSha256.toLowerCase()) {
            if (onProgress) {
              onProgress({ transferred: stat.size, total: stat.size, percent: 100, bytesPerSecond: 0 });
            }
            return { path: destPath, size: stat.size, sha256: existingHash, reused: true };
          }
        } else if (expectedSize && stat.size === expectedSize) {
          const existingHash = await hashFileSha256(destPath);
          if (onProgress) {
            onProgress({ transferred: stat.size, total: stat.size, percent: 100, bytesPerSecond: 0 });
          }
          return { path: destPath, size: stat.size, sha256: existingHash, reused: true };
        }
      }
    } catch {}
  }

  let lastError;
  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    if (signal?.aborted) {
      throw new Error('下载已取消');
    }
    const partPath = `${destPath}.${process.pid}.${attempt}.part`;
    const attemptController = new AbortController();
    const onAbort = () => attemptController.abort(signal.reason || new Error('下载已取消'));
    if (signal) signal.addEventListener('abort', onAbort, { once: true });

    let stallTimer = null;
    const resetStallTimer = () => {
      if (stallTimer) clearTimeout(stallTimer);
      if (stallTimeoutMs > 0) {
        stallTimer = setTimeout(() => {
          attemptController.abort(new Error(`下载超过 ${Math.round(stallTimeoutMs / 1000)} 秒无响应`));
        }, stallTimeoutMs);
      }
    };

    try {
      resetStallTimer();
      const res = await fetchImpl(url, {
        headers: { 'User-Agent': userAgent },
        signal: attemptController.signal,
      });
      if (!res.ok) {
        throw new Error(`下载更新包失败（HTTP ${res.status}）`);
      }
      if (res.url && !isAllowedReleaseUrl(res.url)) {
        throw new Error(`下载被重定向到非官方地址：${res.url}`);
      }
      if (!res.body) {
        throw new Error('下载响应缺少数据流');
      }
      const contentLength = Number(res.headers?.get?.('content-length') || 0) || expectedSize || 0;
      const hash = crypto.createHash('sha256');
      const writer = fs.createWriteStream(partPath);
      let transferred = 0;
      const startedAt = Date.now();
      let lastEmit = 0;

      const emit = force => {
        if (!onProgress) return;
        const now = Date.now();
        if (!force && now - lastEmit < 150) return;
        lastEmit = now;
        const elapsedSec = Math.max(0.05, (now - startedAt) / 1000);
        const percent = contentLength > 0 ? Math.min(100, Math.round((transferred / contentLength) * 100)) : null;
        onProgress({
          transferred,
          total: contentLength,
          percent,
          bytesPerSecond: Math.round(transferred / elapsedSec),
        });
      };

      try {
        for await (const rawChunk of iterateBodyChunks(res.body)) {
          resetStallTimer();
          const chunk = Buffer.isBuffer(rawChunk) ? rawChunk : Buffer.from(rawChunk);
          transferred += chunk.length;
          hash.update(chunk);
          if (!writer.write(chunk)) {
            await new Promise((resolve, reject) => {
              writer.once('drain', resolve);
              writer.once('error', reject);
            });
          }
          emit(false);
        }
        await new Promise((resolve, reject) => {
          writer.once('error', reject);
          writer.end(resolve);
        });
      } catch (streamError) {
        writer.destroy();
        throw streamError;
      }

      if (transferred <= 0) {
        throw new Error('下载到的更新包为空');
      }
      if (expectedSize && transferred !== expectedSize) {
        throw new Error(`更新包大小不匹配（预期 ${expectedSize} 字节，实际 ${transferred} 字节）`);
      }
      const digest = hash.digest('hex');
      if (expectedSha256 && digest !== expectedSha256.toLowerCase()) {
        throw new Error('更新包 SHA-256 校验失败，文件可能已损坏，已自动丢弃');
      }
      fs.renameSync(partPath, destPath);
      emit(true);
      return { path: destPath, size: transferred, sha256: digest, reused: false };
    } catch (error) {
      try { fs.rmSync(partPath, { force: true }); } catch {}
      if (signal?.aborted) {
        throw new Error('下载已取消');
      }
      lastError = error;
      if (attempt < maxRetries) {
        await new Promise(resolve => setTimeout(resolve, 600 * (attempt + 1)));
      }
    } finally {
      if (stallTimer) clearTimeout(stallTimer);
      if (signal) signal.removeEventListener('abort', onAbort);
    }
  }
  throw lastError || new Error('下载更新包失败');
}

async function stageMacApp({
  zipPath,
  stagingDir,
  expectedVersion = '',
  expectedBundleId = EXPECTED_BUNDLE_ID,
  execFileImpl = execFileAsync,
  verifyCodesign = true,
  verifyRuntime = true,
}) {
  fs.rmSync(stagingDir, { recursive: true, force: true });
  fs.mkdirSync(stagingDir, { recursive: true });

  try {
    await execFileImpl('/usr/bin/ditto', ['-x', '-k', zipPath, stagingDir], { timeout: 120000 });

    const stagedApp = path.join(stagingDir, APP_BUNDLE_NAME);
    if (!fs.existsSync(stagedApp) || !fs.statSync(stagedApp).isDirectory()) {
      throw new Error(`更新包中未找到 ${APP_BUNDLE_NAME}`);
    }

    try {
      await execFileImpl('/usr/bin/xattr', ['-dr', 'com.apple.quarantine', stagedApp], { timeout: 15000 });
    } catch {
      // Quarantine attribute is optional; ignore if absent.
    }

    const infoPlistPath = path.join(stagedApp, 'Contents', 'Info.plist');
    if (!fs.existsSync(infoPlistPath)) {
      throw new Error('更新包缺少 Contents/Info.plist，已中止安装');
    }
    const { stdout: plistJson } = await execFileImpl('/usr/bin/plutil', ['-convert', 'json', '-o', '-', infoPlistPath], { timeout: 15000 });
    const plist = JSON.parse(plistJson);
    if (expectedBundleId && plist.CFBundleIdentifier !== expectedBundleId) {
      throw new Error(`更新包应用标识不匹配（预期 ${expectedBundleId}，实际 ${plist.CFBundleIdentifier || '空'}）`);
    }
    const bundleVersion = normalizeVersion(plist.CFBundleShortVersionString || '');
    const wantVersion = normalizeVersion(expectedVersion);
    if (wantVersion && bundleVersion !== wantVersion) {
      throw new Error(`更新包版本不匹配（预期 ${wantVersion}，实际 ${bundleVersion || '未知'}）`);
    }

    if (verifyCodesign) {
      await execFileImpl('/usr/bin/codesign', ['--verify', '--deep', '--strict', stagedApp], { timeout: 60000 });
    }

    if (verifyRuntime) {
      const py = path.join(stagedApp, 'Contents', 'Resources', 'umem-python');
      const mgr = path.join(stagedApp, 'Contents', 'Resources', 'framework', 'manager', 'manager.py');
      try {
        fs.accessSync(py, fs.constants.X_OK);
        fs.accessSync(mgr, fs.constants.R_OK);
      } catch {
        throw new Error('更新包缺少内嵌运行环境，已中止安装');
      }
    }

    return { stagedApp, bundleVersion: bundleVersion || wantVersion, bundleId: plist.CFBundleIdentifier || '' };
  } catch (error) {
    try { fs.rmSync(stagingDir, { recursive: true, force: true }); } catch {}
    throw error;
  }
}

function resolveAppBundlePath(execPath = process.execPath) {
  const normalized = path.resolve(String(execPath || ''));
  const marker = `.app${path.sep}Contents${path.sep}`;
  const idx = normalized.indexOf(marker);
  if (idx === -1) return '';
  return normalized.slice(0, idx + 4);
}

function inspectInstallTarget({
  execPath = process.execPath,
  platform = process.platform,
  isPackaged = true,
  accessCheck = (target, mode) => fs.accessSync(target, mode),
} = {}) {
  if (platform !== 'darwin') {
    return { canReplaceInPlace: false, reason: 'unsupported-platform', detail: '当前平台请前往 GitHub 发布页下载安装。', targetApp: '' };
  }
  if (!isPackaged) {
    return { canReplaceInPlace: false, reason: 'dev-mode', detail: '当前处于源码开发模式，不执行原地应用覆盖。', targetApp: '' };
  }
  const targetApp = resolveAppBundlePath(execPath);
  if (!targetApp) {
    return { canReplaceInPlace: false, reason: 'unknown-bundle', detail: '无法识别当前应用包路径，请手动替换「应用程序」中的应用。', targetApp: '' };
  }
  if (targetApp.includes('/AppTranslocation/')) {
    return {
      canReplaceInPlace: false,
      reason: 'translocated',
      detail: '应用正在 macOS 临时隔离路径运行。请先将其拖入「应用程序」文件夹后再使用应用内自动更新。',
      targetApp,
    };
  }
  if (targetApp.startsWith('/Volumes/')) {
    return {
      canReplaceInPlace: false,
      reason: 'readonly-volume',
      detail: '应用正在磁盘镜像或外部卷中运行。请先复制到「应用程序」文件夹后再使用应用内自动更新。',
      targetApp,
    };
  }
  const parentDir = path.dirname(targetApp);
  try {
    accessCheck(parentDir, fs.constants.W_OK);
    accessCheck(targetApp, fs.constants.W_OK);
  } catch {
    return {
      canReplaceInPlace: false,
      reason: 'permission-denied',
      detail: `当前账户对 ${targetApp} 缺少写入权限，无法直接原地替换。`,
      targetApp,
    };
  }
  return { canReplaceInPlace: true, reason: 'ok', detail: '', targetApp };
}

function buildInstallerScript({ verifyCodesign = true, relaunch = true } = {}) {
  const verifyBlock = verifyCodesign
    ? `if ! /usr/bin/codesign --verify --deep --strict "$TARGET" >/dev/null 2>&1; then
  rollback "替换后签名校验失败，已恢复旧版本"
fi`
    : ':';
  const relaunchLine = relaunch
    ? '/usr/bin/open "$TARGET" >/dev/null 2>&1 || /usr/bin/open -n "$TARGET" >/dev/null 2>&1 || true'
    : ':';

  return `#!/bin/sh
set -u
STAGED="$1"
TARGET="$2"
OLD_PID="$3"
RESULT_FILE="$4"
FROM_VER="$5"
TO_VER="$6"
BACKEND_PID="\${7:-}"
BACKUP="\${TARGET}.umem-backup.$$"

escape_json() {
  printf '%s' "$1" | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g' | tr '\\n' ' '
}

write_result() {
  STATUS="$1"
  MSG="$(escape_json "$2")"
  TMP_RES="\${RESULT_FILE}.tmp.$$"
  mkdir -p "$(dirname "$RESULT_FILE")"
  printf '{"status":"%s","from":"%s","to":"%s","message":"%s","at":%s}\\n' \\
    "$STATUS" "$(escape_json "$FROM_VER")" "$(escape_json "$TO_VER")" "$MSG" "$(date +%s)000" > "$TMP_RES"
  mv -f "$TMP_RES" "$RESULT_FILE"
}

rollback() {
  ERR="$1"
  echo "[updater] error: $ERR"
  rm -rf "$TARGET" 2>/dev/null || true
  if [ -d "$BACKUP" ]; then
    mv "$BACKUP" "$TARGET" 2>/dev/null || true
  fi
  write_result "rolled_back" "$ERR"
  sleep 0.5
  ${relaunchLine}
  rm -f "$0" 2>/dev/null || true
  exit 1
}

echo "[updater] waiting for pid $OLD_PID to exit..."
WAIT_TICKS=0
while [ "$WAIT_TICKS" -lt 100 ]; do
  APP_ALIVE=0
  BACKEND_ALIVE=0
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    APP_ALIVE=1
  fi
  if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
    BACKEND_ALIVE=1
  fi
  if [ "$APP_ALIVE" -eq 0 ] && [ "$BACKEND_ALIVE" -eq 0 ]; then
    break
  fi
  sleep 0.2
  WAIT_TICKS=$((WAIT_TICKS + 1))
done

if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
  write_result "failed" "旧版本进程未在规定时间内退出，已取消替换"
  exit 1
fi
if [ -n "$BACKEND_PID" ] && kill -0 "$BACKEND_PID" 2>/dev/null; then
  kill -TERM "$BACKEND_PID" 2>/dev/null || true
  sleep 0.5
fi

if [ ! -d "$STAGED" ]; then
  write_result "failed" "待安装应用目录不存在"
  exit 1
fi

NEW_STAGE="\${TARGET}.umem-new.$$"
rm -rf "$BACKUP" "$NEW_STAGE" 2>/dev/null || true

if ! mv "$STAGED" "$NEW_STAGE" 2>/dev/null; then
  if ! /usr/bin/ditto "$STAGED" "$NEW_STAGE"; then
    rm -rf "$NEW_STAGE" 2>/dev/null || true
    write_result "failed" "无法将新版本复制到目标目录（可能缺少目录写权限或空间不足）"
    ${relaunchLine}
    exit 1
  fi
fi

if [ -d "$TARGET" ]; then
  if ! mv "$TARGET" "$BACKUP"; then
    rm -rf "$NEW_STAGE" 2>/dev/null || true
    write_result "failed" "无法备份当前应用（可能缺少目录写权限）"
    sleep 0.5
    ${relaunchLine}
    rm -f "$0" 2>/dev/null || true
    exit 1
  fi
fi

if ! mv "$NEW_STAGE" "$TARGET"; then
  rm -rf "$NEW_STAGE" 2>/dev/null || true
  rollback "替换应用目录失败，已恢复旧版本"
fi

/usr/bin/xattr -dr com.apple.quarantine "$TARGET" 2>/dev/null || true
${verifyBlock}

rm -rf "$BACKUP" 2>/dev/null || true
rm -rf "$(dirname "$STAGED")" 2>/dev/null || true
write_result "updated" "已更新至 Unified Memory $TO_VER"
echo "[updater] updated $FROM_VER -> $TO_VER successfully"
sleep 0.3
${relaunchLine}
rm -f "$0" 2>/dev/null || true
exit 0
`;
}

function launchMacInstaller({
  stagedApp,
  targetApp,
  oldPid = process.pid,
  backendPid = '',
  resultFile,
  logFile,
  scriptDir,
  fromVersion = '',
  toVersion = '',
  verifyCodesign = true,
  relaunch = true,
  spawnImpl = spawn,
}) {
  const dir = scriptDir || path.dirname(resultFile);
  fs.mkdirSync(dir, { recursive: true });
  const scriptPath = path.join(dir, `apply-update-${process.pid}-${Date.now()}.sh`);
  fs.writeFileSync(scriptPath, buildInstallerScript({ verifyCodesign, relaunch }), { mode: 0o700 });

  let logFd = 'ignore';
  if (logFile) {
    fs.mkdirSync(path.dirname(logFile), { recursive: true });
    logFd = fs.openSync(logFile, 'a');
  }
  try {
    const child = spawnImpl('/bin/sh', [
      scriptPath,
      stagedApp,
      targetApp,
      String(oldPid),
      resultFile,
      String(fromVersion || ''),
      String(toVersion || ''),
      String(backendPid || ''),
    ], {
      detached: true,
      stdio: ['ignore', logFd, logFd],
    });
    if (typeof child.unref === 'function') child.unref();
    return { scriptPath, child };
  } finally {
    if (typeof logFd === 'number') {
      try { fs.closeSync(logFd); } catch {}
    }
  }
}

function consumeUpdateResult(resultFile) {
  if (!resultFile || !fs.existsSync(resultFile)) return null;
  const data = readJsonSafe(resultFile, null);
  try { fs.rmSync(resultFile, { force: true }); } catch {}
  return data;
}

function pruneOldUpdateDirs(cacheRoot, keepVersion = '') {
  if (!cacheRoot || !fs.existsSync(cacheRoot)) return;
  try {
    for (const entry of fs.readdirSync(cacheRoot, { withFileTypes: true })) {
      if (!entry.isDirectory()) continue;
      if (keepVersion && entry.name === keepVersion) continue;
      try {
        fs.rmSync(path.join(cacheRoot, entry.name), { recursive: true, force: true });
      } catch {}
    }
  } catch {}
}

function createUpdateController(options = {}) {
  const {
    currentVersion,
    arch = process.arch,
    platform = process.platform,
    isPackaged = true,
    execPath = process.execPath,
    cacheRoot,
    logFile,
    resultFile,
    readPrefs,
    writePrefs,
    fetchImpl = globalThis.fetch,
    execFileImpl = execFileAsync,
    spawnImpl = spawn,
    onStateChange = () => {},
    updateIntervalMs = DEFAULT_UPDATE_INTERVAL_MS,
    errorRetryMs = DEFAULT_ERROR_RETRY_MS,
    nowFn = () => Date.now(),
  } = options;

  const installTarget = inspectInstallTarget({ execPath, platform, isPackaged });
  let checkPromise = null;
  let downloadPromise = null;
  let downloadAbort = null;
  let stagedInfo = null;
  let state = {
    status: 'idle',
    currentVersion: normalizeVersion(currentVersion) || String(currentVersion || ''),
    latestVersion: '',
    latestTag: '',
    releaseName: '',
    releaseNotes: '',
    publishedAt: '',
    releaseUrl: RELEASES_URL,
    downloadUrl: '',
    assetSize: 0,
    sha256: '',
    source: '',
    progress: null,
    error: '',
    lastCheckedAt: Number(readPrefs?.()?.lastUpdateCheck || 0),
    autoCheck: readPrefs?.()?.autoUpdateCheck !== false,
    skippedVersion: String(readPrefs?.()?.skippedVersion || ''),
    canAutoInstall: installTarget.canReplaceInPlace,
    installBlockReason: installTarget.reason,
    installBlockDetail: installTarget.detail,
    targetApp: installTarget.targetApp,
    notice: null,
  };

  function snapshot() {
    return {
      ...state,
      progress: state.progress ? { ...state.progress } : null,
      notice: state.notice ? { ...state.notice } : null,
    };
  }

  function setState(patch) {
    state = { ...state, ...patch };
    try { onStateChange(snapshot()); } catch {}
    return snapshot();
  }

  function setAutoCheck(enabled) {
    const value = Boolean(enabled);
    writePrefs?.({ autoUpdateCheck: value });
    return setState({ autoCheck: value });
  }

  function skipVersion(tagOrVersion) {
    const tag = String(tagOrVersion || state.latestTag || '').trim();
    if (!tag) return snapshot();
    writePrefs?.({ skippedVersion: tag });
    return setState({ skippedVersion: tag });
  }

  function setNotice(notice) {
    return setState({ notice: notice || null });
  }

  function shouldRunScheduledCheck() {
    const prefs = readPrefs?.() || {};
    if (prefs.autoUpdateCheck === false) return false;
    if (['checking', 'downloading', 'verifying', 'ready', 'installing'].includes(state.status)) return false;
    const now = nowFn();
    const lastSuccess = Number(prefs.lastUpdateCheck || 0);
    const lastFailure = Number(prefs.lastUpdateErrorAt || 0);
    if (now - lastSuccess < updateIntervalMs) return false;
    if (lastFailure && now - lastFailure < errorRetryMs) return false;
    return true;
  }

  async function checkForUpdates({ manual = false } = {}) {
    if (downloadPromise || state.status === 'installing') {
      return { snapshot: snapshot(), triggered: false };
    }
    if (checkPromise) {
      return checkPromise;
    }
    checkPromise = (async () => {
      const prevStatus = state.status;
      setState({ status: 'checking', error: '' });
      const prefs = readPrefs?.() || {};
      try {
        const { release, etag } = await fetchLatestRelease({
          currentVersion: state.currentVersion,
          arch,
          cache: prefs.updateCache || null,
          fetchImpl,
        });
        const checkedAt = nowFn();
        writePrefs?.({
          lastUpdateCheck: checkedAt,
          lastUpdateErrorAt: 0,
          updateCache: { etag, arch, release },
        });
        const newer = isNewerVersion(release.version, state.currentVersion);
        if (newer && release.hasAsset === false) {
          const next = setState({
            status: manual ? 'error' : (prevStatus === 'checking' ? 'idle' : prevStatus),
            latestVersion: release.version,
            latestTag: release.tag,
            releaseName: release.name,
            releaseNotes: release.notes,
            publishedAt: release.publishedAt,
            releaseUrl: release.htmlUrl,
            lastCheckedAt: checkedAt,
            error: manual ? `新版本 ${release.version} 的 macOS 安装包尚未上传完成，请稍后重试。` : '',
          });
          return { snapshot: next, newer: false, skipped: false, manual, error: next.error || undefined };
        }
        if (!newer) {
          pruneOldUpdateDirs(cacheRoot, '');
          const next = setState({
            status: 'up-to-date',
            latestVersion: release.version,
            latestTag: release.tag,
            releaseName: release.name,
            releaseNotes: release.notes,
            publishedAt: release.publishedAt,
            releaseUrl: release.htmlUrl,
            downloadUrl: release.downloadUrl,
            assetSize: release.size,
            sha256: release.sha256,
            source: release.source,
            lastCheckedAt: checkedAt,
            progress: null,
            error: '',
          });
          return { snapshot: next, newer: false, skipped: false, manual };
        }
        const skipped = !manual && (prefs.skippedVersion === release.tag || prefs.skippedVersion === release.version);
        if (manual && (prefs.skippedVersion === release.tag || prefs.skippedVersion === release.version)) {
          writePrefs?.({ skippedVersion: '' });
        }
        const alreadyReady = stagedInfo && stagedInfo.bundleVersion === release.version && fs.existsSync(stagedInfo.stagedApp);
        const next = setState({
          status: alreadyReady ? 'ready' : 'available',
          latestVersion: release.version,
          latestTag: release.tag,
          releaseName: release.name,
          releaseNotes: release.notes,
          publishedAt: release.publishedAt,
          releaseUrl: release.htmlUrl,
          downloadUrl: release.downloadUrl,
          assetSize: release.size,
          sha256: release.sha256,
          source: release.source,
          lastCheckedAt: checkedAt,
          skippedVersion: manual && (prefs.skippedVersion === release.tag || prefs.skippedVersion === release.version)
            ? ''
            : String(prefs.skippedVersion || ''),
          progress: alreadyReady ? { transferred: release.size, total: release.size, percent: 100, bytesPerSecond: 0 } : null,
          error: '',
        });
        return { snapshot: next, newer: true, skipped, manual };
      } catch (error) {
        writePrefs?.({ lastUpdateErrorAt: nowFn() });
        const msg = String(error?.message || error || '检查更新失败');
        if (!manual) {
          // Silent background check failures (e.g. offline/VPN) must not nag the user with an error banner.
          const next = setState({
            status: prevStatus === 'checking' ? 'idle' : prevStatus,
            error: '',
          });
          return { snapshot: next, newer: false, skipped: false, manual, error: msg };
        }
        const next = setState({
          status: 'error',
          error: msg,
        });
        return { snapshot: next, newer: false, skipped: false, manual, error: msg };
      } finally {
        checkPromise = null;
      }
    })();
    return checkPromise;
  }

  async function downloadUpdate() {
    if (downloadPromise) return downloadPromise;
    if (state.status === 'ready' && stagedInfo && fs.existsSync(stagedInfo.stagedApp)) {
      return snapshot();
    }
    if (!state.latestVersion || !state.downloadUrl || !isNewerVersion(state.latestVersion, state.currentVersion)) {
      const checked = await checkForUpdates({ manual: true });
      if (!checked.newer) return snapshot();
    }
    if (!state.canAutoInstall) {
      return setState({ status: 'error', error: state.installBlockDetail || '当前安装位置不支持自动替换，请前往 GitHub 发布页手动更新。' });
    }

    downloadAbort = new AbortController();
    downloadPromise = (async () => {
      const versionDir = path.join(cacheRoot, state.latestVersion);
      const zipPath = path.join(versionDir, expectedAssetName(arch));
      const stagingDir = path.join(versionDir, 'staged');
      pruneOldUpdateDirs(cacheRoot, state.latestVersion);
      setState({
        status: 'downloading',
        error: '',
        progress: { transferred: 0, total: state.assetSize || 0, percent: 0, bytesPerSecond: 0 },
      });
      try {
        await downloadAsset({
          url: state.downloadUrl,
          destPath: zipPath,
          expectedSha256: state.sha256,
          expectedSize: state.assetSize,
          userAgent: `UnifiedMemory/${state.currentVersion}`,
          fetchImpl,
          signal: downloadAbort.signal,
          onProgress: progress => setState({ status: 'downloading', progress }),
        });
        setState({
          status: 'verifying',
          progress: { transferred: state.assetSize || 1, total: state.assetSize || 1, percent: 100, bytesPerSecond: 0 },
        });
        stagedInfo = await stageMacApp({
          zipPath,
          stagingDir,
          expectedVersion: state.latestVersion,
          execFileImpl,
        });
        return setState({ status: 'ready', error: '' });
      } catch (error) {
        stagedInfo = null;
        const msg = String(error?.message || error || '下载更新失败');
        if (msg === '下载已取消') {
          return setState({ status: 'available', progress: null, error: '' });
        }
        return setState({ status: 'error', progress: null, error: msg });
      } finally {
        downloadPromise = null;
        downloadAbort = null;
      }
    })();
    return downloadPromise;
  }

  function cancelDownload() {
    if (downloadAbort) {
      downloadAbort.abort(new Error('下载已取消'));
      return true;
    }
    return false;
  }

  function beginInstall({ oldPid = process.pid, backendPid = '', verifyCodesign = true, relaunch = true } = {}) {
    if (state.status !== 'ready' || !stagedInfo || !fs.existsSync(stagedInfo.stagedApp)) {
      throw new Error('更新包尚未就绪，请先下载完成。');
    }
    if (!state.canAutoInstall || !state.targetApp) {
      throw new Error(state.installBlockDetail || '当前安装位置不支持自动替换。');
    }
    setState({ status: 'installing', error: '' });
    return launchMacInstaller({
      stagedApp: stagedInfo.stagedApp,
      targetApp: state.targetApp,
      oldPid,
      backendPid,
      resultFile,
      logFile,
      scriptDir: path.join(cacheRoot, state.latestVersion),
      fromVersion: state.currentVersion,
      toVersion: state.latestVersion,
      verifyCodesign,
      relaunch,
      spawnImpl,
    });
  }

  return {
    getState: snapshot,
    setAutoCheck,
    skipVersion,
    setNotice,
    shouldRunScheduledCheck,
    checkForUpdates,
    downloadUpdate,
    cancelDownload,
    beginInstall,
  };
}

module.exports = {
  APP_BUNDLE_NAME,
  EXPECTED_BUNDLE_ID,
  LATEST_RELEASE_API,
  RELEASES_URL,
  REPO_URL,
  buildInstallerScript,
  consumeUpdateResult,
  createUpdateController,
  downloadAsset,
  expectedAssetName,
  fetchLatestRelease,
  hashFileSha256,
  inspectInstallTarget,
  isAllowedReleaseUrl,
  isNewerVersion,
  launchMacInstaller,
  normalizeReleaseFromApi,
  normalizeVersion,
  parseSha256Digest,
  parseTagFromLocation,
  pruneOldUpdateDirs,
  readJsonSafe,
  resolveAppBundlePath,
  stageMacApp,
  versionParts,
  writeJsonAtomic,
};
