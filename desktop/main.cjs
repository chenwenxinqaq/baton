'use strict';

const { app, BrowserWindow, Menu, Tray, clipboard, dialog, ipcMain, nativeImage, nativeTheme, net, session, shell } = require('electron');
const { execFile, spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');
const {
  RELEASES_URL,
  REPO_URL,
  consumeUpdateResult,
  createUpdateController,
  pruneOldUpdateDirs,
  readJsonSafe,
  writeJsonAtomic,
} = require('./updater.cjs');

app.setName('Unified Memory');
const UPDATE_CHECK_POLL_MS = 6 * 60 * 60 * 1000;
let win;
let backend;
let origin;
let quitting = false;
let tray;
let rebuildTrayMenu = () => {};
let openedAtLogin = false;
let updater;
let lastMenuKey = '';

function prefsFile() { return path.join(app.getPath('userData'), 'prefs.json'); }
function updateCacheRoot() { return path.join(app.getPath('userData'), 'updates'); }
function updateResultFile() { return path.join(app.getPath('userData'), 'update-result.json'); }
function updateLogFile() { return path.join(app.getPath('userData'), 'updater.log'); }

function readPrefs() {
  return readJsonSafe(prefsFile(), {});
}

function writePrefs(patch) {
  writeJsonAtomic(prefsFile(), { ...readPrefs(), ...patch });
}

function messageBox(options) {
  return win?.isVisible() ? dialog.showMessageBox(win, options) : dialog.showMessageBox(options);
}

function autoUpdateCheckEnabled() { return readPrefs().autoUpdateCheck !== false; }

function openSafeExternal(targetUrl) {
  const isRepo = typeof targetUrl === 'string' && (targetUrl === REPO_URL || targetUrl.startsWith(REPO_URL + '/'));
  const url = isRepo ? targetUrl : RELEASES_URL;
  return shell.openExternal(url);
}

function formatUpdateMenuLabel(state) {
  if (!state) return '检查更新…';
  if (state.status === 'checking') return '正在检查更新…';
  if (state.status === 'downloading') return '正在下载更新…';
  if (state.status === 'verifying') return '正在校验更新包…';
  if (state.status === 'ready') return `重启并安装 ${state.latestVersion}…`;
  if (state.status === 'installing') return '正在安装更新…';
  if (state.status === 'available' && state.latestVersion) return `发现新版本 ${state.latestVersion}…`;
  return '检查更新…';
}

function syncUpdateChrome(state) {
  if (win && !win.isDestroyed()) {
    win.webContents.send('umem:update-state', state);
  }
  if (tray) {
    if (state.status === 'downloading' && typeof state.progress?.percent === 'number') {
      tray.setToolTip(`Unified Memory · 正在下载 ${state.latestVersion} (${state.progress.percent}%)`);
    } else if (state.status === 'ready' && state.latestVersion) {
      tray.setToolTip(`Unified Memory · 新版本 ${state.latestVersion} 已就绪，点击重启安装`);
    } else if (state.status === 'available' && state.latestVersion) {
      tray.setToolTip(`Unified Memory · 发现新版本 ${state.latestVersion}`);
    } else {
      tray.setToolTip('Unified Memory');
    }
  }
  const label = formatUpdateMenuLabel(state);
  const key = `${label}|${state.autoCheck ? 1 : 0}`;
  if (key !== lastMenuKey) {
    lastMenuKey = key;
    if (origin) {
      installMenu();
      rebuildTrayMenu();
    }
  }
}

function ensureUpdater() {
  if (updater) return updater;
  updater = createUpdateController({
    currentVersion: app.getVersion(),
    arch: process.arch,
    platform: process.platform,
    isPackaged: app.isPackaged,
    execPath: process.execPath,
    cacheRoot: updateCacheRoot(),
    logFile: updateLogFile(),
    resultFile: updateResultFile(),
    readPrefs,
    writePrefs,
    fetchImpl: (url, opts) => net.fetch(url, opts),
    onStateChange: syncUpdateChrome,
  });
  return updater;
}

function formatReleaseNotesExcerpt(notes) {
  const clean = String(notes || '').trim();
  if (!clean) return '';
  const lines = clean.split(/\r?\n/).filter(Boolean).slice(0, 8).join('\n');
  return lines.length > 420 ? `${lines.slice(0, 420)}…` : lines;
}

async function triggerInstallFromUi() {
  const ctrl = ensureUpdater();
  const state = ctrl.getState();
  if (state.status !== 'ready') return state;
  try {
    ctrl.beginInstall({ oldPid: process.pid, backendPid: backend?.pid || '' });
    quitting = true;
    try { backend?.stdin.end(); } catch {}
    setTimeout(() => app.quit(), 120);
    const forceExit = setTimeout(() => {
      try { if (backend && backend.exitCode === null) backend.kill('SIGKILL'); } catch {}
      app.exit(0);
    }, 5000);
    forceExit.unref?.();
    return ctrl.getState();
  } catch (error) {
    const msg = String(error?.message || error || '启动安装程序失败');
    await messageBox({
      type: 'warning',
      message: '无法自动替换应用',
      detail: `${msg}\n\n你也可以前往 GitHub 发布页手动下载安装，记忆库数据不受影响。`,
      buttons: ['前往发布页', '关闭'],
      defaultId: 0,
      cancelId: 1,
    }).then(({ response }) => {
      if (response === 0) openSafeExternal(state.releaseUrl);
    });
    return ctrl.getState();
  }
}

async function handleUpdateMenuClick() {
  const ctrl = ensureUpdater();
  const current = ctrl.getState();
  if (current.status === 'ready') {
    const { response } = await messageBox({
      type: 'info',
      buttons: ['立即重启并安装', '稍后'],
      defaultId: 0,
      cancelId: 1,
      message: `Unified Memory ${current.latestVersion} 已校验就绪`,
      detail: '应用将自动退出、原子替换程序包并重新打开。记忆库数据不会受任何影响；若替换失败会自动回滚到当前版本。',
    });
    if (response === 0) await triggerInstallFromUi();
    return;
  }
  if (current.status === 'downloading' || current.status === 'verifying' || current.status === 'installing') {
    showWindow();
    return;
  }
  await checkForUpdates(true);
}

// Only release metadata is requested from GitHub; memory content never leaves the machine here.
async function checkForUpdates(manual) {
  const ctrl = ensureUpdater();
  const result = await ctrl.checkForUpdates({ manual: Boolean(manual) });
  const state = result.snapshot;
  if (!manual) {
    return result;
  }
  if (result.error) {
    const { response } = await messageBox({
      type: 'warning',
      buttons: ['重试', '打开发布页', '关闭'],
      defaultId: 0,
      cancelId: 2,
      message: '暂时无法检查更新',
      detail: `${result.error}\n\n如果当前网络受限，可以直接打开发布页或检查系统代理设置。`,
    });
    if (response === 0) return checkForUpdates(true);
    if (response === 1) openSafeExternal(RELEASES_URL);
    return result;
  }
  if (!result.newer) {
    await messageBox({
      type: 'info',
      message: '已是最新版本',
      detail: `当前版本 ${state.currentVersion}。`,
    });
    return result;
  }
  const notes = formatReleaseNotesExcerpt(state.releaseNotes);
  if (state.status === 'ready') {
    const { response } = await messageBox({
      type: 'info',
      buttons: ['立即重启并安装', '稍后'],
      defaultId: 0,
      cancelId: 1,
      message: `Unified Memory ${state.latestVersion} 已下载就绪`,
      detail: `当前版本 ${state.currentVersion}。${notes ? '\n\n' + notes : ''}`,
    });
    if (response === 0) await triggerInstallFromUi();
    return result;
  }
  if (state.canAutoInstall) {
    const { response } = await messageBox({
      type: 'info',
      buttons: ['立即下载并更新', '查看发布页', '跳过此版本', '稍后'],
      defaultId: 0,
      cancelId: 3,
      message: `Unified Memory ${state.latestVersion} 已发布`,
      detail: `当前版本 ${state.currentVersion}。应用将在后台下载并校验 SHA-256 与签名，记忆库不受影响。${notes ? '\n\n' + notes : ''}`,
    });
    if (response === 0) {
      await showWindow();
      const done = await ctrl.downloadUpdate();
      if (done.status === 'ready') {
        const prompt = await messageBox({
          type: 'info',
          buttons: ['立即重启并安装', '稍后'],
          defaultId: 0,
          cancelId: 1,
          message: `Unified Memory ${done.latestVersion} 已校验完成`,
          detail: '是否立即重启应用完成替换？记忆库不受影响。',
        });
        if (prompt.response === 0) await triggerInstallFromUi();
      }
    } else if (response === 1) {
      openSafeExternal(state.releaseUrl);
    } else if (response === 2) {
      ctrl.skipVersion(state.latestTag);
    }
    return result;
  }
  const { response } = await messageBox({
    type: 'info',
    buttons: ['前往发布页下载', '跳过此版本', '稍后'],
    defaultId: 0,
    cancelId: 2,
    message: `Unified Memory ${state.latestVersion} 已发布`,
    detail: `当前版本 ${state.currentVersion}。${state.installBlockDetail}${notes ? '\n\n' + notes : ''}`,
  });
  if (response === 0) openSafeExternal(state.releaseUrl);
  if (response === 1) ctrl.skipVersion(state.latestTag);
  return result;
}

function scheduleUpdateChecks() {
  if (!app.isPackaged) return;
  const ctrl = ensureUpdater();
  const run = () => {
    if (ctrl.shouldRunScheduledCheck()) {
      checkForUpdates(false).catch(() => {});
    }
  };
  setTimeout(run, 15000);
  setInterval(run, UPDATE_CHECK_POLL_MS);
}

function runPostUpgradeSync() {
  try {
    const { framework, python } = backendSettings();
    const script = path.join(framework, 'scripts', 'memoryctl.py');
    if (!fs.existsSync(script)) return;
    execFile(python, ['-B', script, 'doctor', '--fix', '--json'], {
      cwd: framework,
      timeout: 30000,
      env: {
        ...process.env,
        ...(app.isPackaged ? { UNIFIED_MEMORY_GIT_HOME: path.join(process.resourcesPath, 'git') } : {}),
        PYTHONDONTWRITEBYTECODE: '1',
      },
    }, () => {});
  } catch {}
}

function handlePostUpdateStartup() {
  const ctrl = ensureUpdater();
  const current = app.getVersion();
  const prefs = readPrefs();
  const previous = typeof prefs.lastInstalledVersion === 'string' ? prefs.lastInstalledVersion : '';
  const result = consumeUpdateResult(updateResultFile());
  let shouldSync = false;
  if (result && typeof result === 'object') {
    if (result.status === 'updated') {
      ctrl.setNotice({
        kind: 'success',
        message: result.message || `已更新至 Unified Memory ${current}`,
        detail: '已自动刷新本机启动器并同步已接入客户端规则，记忆库完整保留。',
      });
      shouldSync = true;
    } else if (result.status === 'rolled_back' || result.status === 'failed') {
      ctrl.setNotice({
        kind: 'error',
        message: result.status === 'rolled_back' ? '上次更新失败，已自动回滚到旧版本' : '上次更新未完成',
        detail: result.message || '请查看更新日志或前往 GitHub 发布页手动更新。',
      });
    }
  } else if (app.isPackaged && previous && previous !== current) {
    shouldSync = true;
    ctrl.setNotice({
      kind: 'success',
      message: `已更新至 Unified Memory ${current}`,
      detail: '检测到版本变更，已自动刷新启动器并同步客户端规则。',
    });
  }
  if (app.isPackaged && previous !== current) {
    writePrefs({ lastInstalledVersion: current });
  }
  if (shouldSync) {
    pruneOldUpdateDirs(updateCacheRoot(), '');
    runPostUpgradeSync();
  }
}

function registerUpdateIpc() {
  const guardSender = event => {
    try {
      return Boolean(origin && event?.senderFrame?.url && new URL(event.senderFrame.url).origin === origin);
    } catch {
      return false;
    }
  };
  const wrap = handler => async (event, ...args) => {
    if (!guardSender(event)) throw new Error('Forbidden');
    return handler(...args);
  };
  ipcMain.handle('umem:update-state', wrap(() => ensureUpdater().getState()));
  ipcMain.handle('umem:update-check', wrap(async () => (await ensureUpdater().checkForUpdates({ manual: true })).snapshot));
  ipcMain.handle('umem:update-download', wrap(() => ensureUpdater().downloadUpdate()));
  ipcMain.handle('umem:update-cancel', wrap(() => {
    ensureUpdater().cancelDownload();
    return ensureUpdater().getState();
  }));
  ipcMain.handle('umem:update-install', wrap(() => triggerInstallFromUi()));
  ipcMain.handle('umem:update-skip', wrap(version => ensureUpdater().skipVersion(version)));
  ipcMain.handle('umem:update-open-page', wrap(url => {
    openSafeExternal(url);
    return true;
  }));
  ipcMain.handle('umem:update-open-log', wrap(() => {
    const file = updateLogFile();
    if (fs.existsSync(file)) shell.showItemInFolder(file);
    else shell.openPath(app.getPath('userData'));
    return true;
  }));
  ipcMain.handle('umem:update-dismiss-notice', wrap(() => ensureUpdater().setNotice(null)));
  ipcMain.handle('umem:copy-text', wrap((text) => {
    clipboard.writeText(String(text ?? ''));
    return true;
  }));
}

function backendSettings() {
  // Developers and existing CLI installs can point both entry points at the same checkout.
  const file = path.join(app.getPath('userData'), 'backend.json');
  const config = fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : {};
  const framework = config.framework || (app.isPackaged
    ? path.join(process.resourcesPath, 'framework') : path.resolve(__dirname, '..'));
  const python = config.python || [
    path.join(process.resourcesPath, 'umem-python'),
    '/opt/homebrew/bin/python3', '/usr/local/bin/python3',
    '/Library/Developer/CommandLineTools/usr/bin/python3', '/usr/bin/python3'
  ].find(p => { try { fs.accessSync(p, fs.constants.X_OK); return true; } catch { return false; } });
  if (!python || !fs.existsSync(path.join(framework, 'manager', 'desktop.html')))
    throw new Error('未找到桌面后端或可用的 Python 3.9+。请检查程序安装。');
  return { framework, python };
}

function startBackend() {
  const { framework, python } = backendSettings();
  const child = spawn(python, [path.join(framework, 'manager', 'manager.py'), '--desktop', '--port', '0'], {
    cwd: framework, env: { ...process.env,
      ...(app.isPackaged ? { UNIFIED_MEMORY_GIT_HOME: path.join(process.resourcesPath, 'git') } : {}),
      PYTHONDONTWRITEBYTECODE: '1', PYTHONUNBUFFERED: '1' },
    stdio: ['pipe', 'pipe', 'pipe']
  });
  backend = child;
  let diagnostic = '';
  child.stderr.on('data', chunk => { diagnostic = (diagnostic + chunk.toString()).slice(-1600); });
  const lines = readline.createInterface({ input: child.stdout });
  child.on('exit', () => {
    if (origin && !quitting) {
      dialog.showErrorBox('本地记忆服务已退出', diagnostic.trim() || '请重新打开应用。');
      app.quit();
    }
  });
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { child.kill(); reject(new Error('本地服务启动超时。')); }, 20000);
    child.once('error', error => { clearTimeout(timer); reject(error); });
    child.once('exit', () => { clearTimeout(timer); reject(new Error(diagnostic || '启动本地服务失败。')); });
    lines.once('line', line => {
      clearTimeout(timer);
      if (!/^http:\/\/127\.0\.0\.1:\d+\/$/.test(line)) {
        reject(new Error('本地服务地址无效。'));
      } else {
        resolve(new URL(line).origin);
      }
    });
  });
}

function createWindow() {
  const background = () => nativeTheme.shouldUseDarkColors ? '#181818' : '#ffffff';
  win = new BrowserWindow({
    title: 'Unified Memory', width: 1140, height: 790, minWidth: 730, minHeight: 505,
    backgroundColor: background(), show: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
      partition: 'memory-workspace',
    }
  });
  const wc = win.webContents;
  const allowed = address => { try { return new URL(address).origin === origin; } catch { return false; } };
  wc.on('will-navigate', (event, address) => { if (!allowed(address)) event.preventDefault(); });
  wc.on('will-redirect', (event, address) => { if (!allowed(address)) event.preventDefault(); });
  wc.setWindowOpenHandler(() => ({ action: 'deny' }));
  win.once('ready-to-show', () => { if (!openedAtLogin) win?.show(); });
  const updateBackground = () => win.setBackgroundColor(background());
  nativeTheme.on('updated', updateBackground);
  win.on('close', event => {
    // Closing the window keeps the app in the menu bar; use the tray menu or Cmd+Q to quit.
    if (!quitting && process.platform === 'darwin') { event.preventDefault(); win.hide(); }
  });
  win.on('closed', () => {
    nativeTheme.off('updated', updateBackground);
    win = null;
  });
  return win.loadURL(origin + '/');
}

function installMenu() {
  const go = dir => {
    const history = win?.webContents.navigationHistory;
    if (dir < 0 && history?.canGoBack()) history.goBack();
    if (dir > 0 && history?.canGoForward()) history.goForward();
  };
  const appMenu = { label: app.name, submenu: [
    { role: 'about' },
    { label: formatUpdateMenuLabel(ensureUpdater().getState()), click: () => handleUpdateMenuClick().catch(() => {}) },
    { label: '自动检查更新', type: 'checkbox', checked: autoUpdateCheckEnabled(),
      click: item => ensureUpdater().setAutoCheck(item.checked) },
    { label: '在 GitHub 上查看', click: () => shell.openExternal(REPO_URL) },
    { type: 'separator' }, { role: 'services' },
    { type: 'separator' }, { role: 'hide' }, { role: 'hideOthers' }, { role: 'unhide' },
    { type: 'separator' }, { role: 'quit' }
  ] };
  const menu = [
    ...(process.platform === 'darwin' ? [appMenu] : []),
    { role: 'editMenu' },
    { label: '工作空间', submenu: [
      { label: '快速跳转与搜索', accelerator: 'CmdOrCtrl+K', click: () =>
        win?.webContents.executeJavaScript("window.__openQuickPalette?.()").catch(() => {}) },
      { label: '新建记忆', accelerator: 'CmdOrCtrl+N', click: () => showWindow('/?view=new#detail-title') },
      { label: '搜索记忆或技能', accelerator: 'CmdOrCtrl+F', click: () =>
        win?.webContents.executeJavaScript("document.getElementById('filter-q')?.focus()").catch(() => {}) },
      { label: '后退', accelerator: 'CmdOrCtrl+[', click: () => go(-1) },
      { label: '前进', accelerator: 'CmdOrCtrl+]', click: () => go(1) },
      { type: 'separator' }, { role: 'reload' }, { role: 'togglefullscreen' }
    ] }, { role: 'windowMenu' },
    { role: 'help', submenu: [
      { label: 'GitHub 仓库', click: () => shell.openExternal(REPO_URL) },
      { label: '版本发布页', click: () => shell.openExternal(RELEASES_URL) },
      { label: '反馈问题', click: () => shell.openExternal(REPO_URL + '/issues') }
    ] }
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(menu));
}

function showWindow(route) {
  if (!win) return createWindow().then(() => win?.show());
  if (win.isMinimized()) win.restore();
  if (route) win.loadURL(origin + route);
  win.show();
  win.focus();
}

function setLoginItem(enabled) {
  app.setLoginItemSettings({ openAtLogin: enabled, openAsHidden: true });
  writePrefs({ loginItemAsked: true });
}

function installTray() {
  const base = path.join(__dirname, 'trayTemplate.png');
  const image = nativeImage.createFromPath(base);
  if (image.isEmpty()) return;
  image.setTemplateImage(true);
  tray = new Tray(image);
  tray.setToolTip('Unified Memory');
  const rebuild = () => tray.setContextMenu(Menu.buildFromTemplate([
    { label: '打开 Unified Memory', click: () => showWindow('/') },
    { label: '读写动态', click: () => showWindow('/?view=activity') },
    { label: '共享与同步', click: () => showWindow('/?view=sync_hub') },
    { type: 'separator' },
    { label: formatUpdateMenuLabel(ensureUpdater().getState()), click: () => handleUpdateMenuClick().catch(() => {}) },
    { label: '开机自动启动', type: 'checkbox', checked: app.getLoginItemSettings().openAtLogin,
      click: item => { setLoginItem(item.checked); rebuild(); } },
    { type: 'separator' },
    { label: '退出 Unified Memory', click: () => app.quit() }
  ]));
  rebuildTrayMenu = rebuild;
  rebuild();
  tray.on('click', () => showWindow('/'));
}

async function askLoginItemOnce() {
  if (readPrefs().loginItemAsked || !app.isPackaged) return;
  const { response } = await dialog.showMessageBox(win, {
    type: 'question', buttons: ['开机自动启动', '暂不'], defaultId: 0, cancelId: 1,
    message: '让 Unified Memory 常驻菜单栏？',
    detail: '常驻后可随时查看 AI 读写动态与同步状态，关闭窗口不会退出。之后可在菜单栏图标里修改。'
  });
  setLoginItem(response === 0);
}

async function launch() {
  openedAtLogin = app.getLoginItemSettings().wasOpenedAtLogin;
  app.setAboutPanelOptions({ applicationName: 'Unified Memory', applicationVersion: app.getVersion(),
    credits: REPO_URL, website: REPO_URL });
  const isolated = session.fromPartition('memory-workspace');
  const allowClipboardWrite = (permission, requestingOrigin) => {
    if (permission !== 'clipboard-sanitized-write') return false;
    if (!origin || !requestingOrigin) return false;
    try { return new URL(requestingOrigin).origin === origin; } catch { return false; }
  };
  isolated.setPermissionRequestHandler((wc, permission, callback, details) => {
    const reqOrigin = (details && details.requestingUrl) || (wc && wc.getURL ? wc.getURL() : '');
    callback(allowClipboardWrite(permission, reqOrigin));
  });
  isolated.setPermissionCheckHandler((_wc, permission, requestingOrigin) => (
    allowClipboardWrite(permission, requestingOrigin)
  ));
  origin = await startBackend();
  // The renderer only needs resources from this instance's loopback port.
  isolated.webRequest.onBeforeRequest({ urls: ['http://*/*','https://*/*'] }, (details, callback) => {
    callback({ cancel: new URL(details.url).origin !== origin });
  });
  ensureUpdater();
  handlePostUpdateStartup();
  registerUpdateIpc();
  installMenu();
  installTray();
  await createWindow();
  askLoginItemOnce().catch(() => {});
  scheduleUpdateChecks();
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => showWindow());
  app.on('activate', () => { if (origin) showWindow(); });
  app.whenReady().then(launch).catch(error => {
    dialog.showErrorBox('无法打开 Unified Memory', String(error.message));
    app.quit();
  });
  app.on('before-quit', () => { quitting = true; backend?.stdin.end(); });
  app.on('will-quit', () => { if (backend && backend.exitCode === null) backend.kill(); });
  app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });
}
