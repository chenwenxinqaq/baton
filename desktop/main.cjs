'use strict';

const { app, BrowserWindow, Menu, Tray, dialog, nativeImage, nativeTheme, net, session, shell } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');

app.setName('Unified Memory');
const REPO_URL = 'https://github.com/chenwenxinqaq/baton';
const RELEASES_URL = REPO_URL + '/releases/latest';
const LATEST_RELEASE_API = 'https://api.github.com/repos/chenwenxinqaq/baton/releases/latest';
const UPDATE_INTERVAL = 24 * 60 * 60 * 1000;
let win;
let backend;
let origin;
let quitting = false;
let tray;
let openedAtLogin = false;

function prefsFile() { return path.join(app.getPath('userData'), 'prefs.json'); }
function readPrefs() {
  try { return JSON.parse(fs.readFileSync(prefsFile(), 'utf8')); } catch { return {}; }
}
function writePrefs(patch) {
  fs.mkdirSync(app.getPath('userData'), { recursive: true });
  fs.writeFileSync(prefsFile(), JSON.stringify({ ...readPrefs(), ...patch }, null, 2));
}

function versionParts(value) {
  const match = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(value || '').trim());
  return match ? match.slice(1).map(Number) : null;
}

function isNewerVersion(latest, current) {
  const a = versionParts(latest), b = versionParts(current);
  if (!a || !b) return false;
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i];
  return false;
}

function messageBox(options) {
  return win?.isVisible() ? dialog.showMessageBox(win, options) : dialog.showMessageBox(options);
}

function autoUpdateCheckEnabled() { return readPrefs().autoUpdateCheck !== false; }

// Only the release tag is requested from GitHub; memory content never leaves the machine here.
async function checkForUpdates(manual) {
  let release;
  try {
    const response = await net.fetch(LATEST_RELEASE_API, { headers: {
      Accept: 'application/vnd.github+json', 'User-Agent': `UnifiedMemory/${app.getVersion()}` } });
    if (!response.ok) throw new Error(`GitHub 返回 HTTP ${response.status}`);
    release = await response.json();
  } catch (error) {
    if (manual) await messageBox({ type: 'warning', message: '暂时无法检查更新', detail: String(error.message || error) });
    return;
  }
  writePrefs({ lastUpdateCheck: Date.now() });
  const latest = typeof release?.tag_name === 'string' ? release.tag_name.trim() : '';
  const current = app.getVersion();
  if (!isNewerVersion(latest, current)) {
    if (manual) await messageBox({ type: 'info', message: '已是最新版本', detail: `当前版本 ${current}。` });
    return;
  }
  if (!manual && readPrefs().skippedVersion === latest) return;
  const page = typeof release.html_url === 'string' && release.html_url.startsWith(REPO_URL + '/releases/')
    ? release.html_url : RELEASES_URL;
  const { response } = await messageBox({
    type: 'info', buttons: ['前往下载', '跳过此版本', '稍后'], defaultId: 0, cancelId: 2,
    message: `Unified Memory ${latest.replace(/^v/, '')} 已发布`,
    detail: `当前版本 ${current}。下载 ZIP 后替换「应用程序」中的 Unified Memory，记忆库不受影响。`
  });
  if (response === 0) shell.openExternal(page);
  if (response === 1) writePrefs({ skippedVersion: latest });
}

function scheduleUpdateChecks() {
  if (!app.isPackaged) return;
  const run = () => {
    if (autoUpdateCheckEnabled() && Date.now() - (readPrefs().lastUpdateCheck || 0) >= UPDATE_INTERVAL)
      checkForUpdates(false).catch(() => {});
  };
  setTimeout(run, 15000);
  setInterval(run, 6 * 60 * 60 * 1000);
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
    webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false, partition: 'memory-workspace' }
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
    { label: '检查更新…', click: () => checkForUpdates(true).catch(() => {}) },
    { label: '自动检查更新', type: 'checkbox', checked: autoUpdateCheckEnabled(),
      click: item => writePrefs({ autoUpdateCheck: item.checked }) },
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
    { label: '检查更新…', click: () => checkForUpdates(true).catch(() => {}) },
    { label: '开机自动启动', type: 'checkbox', checked: app.getLoginItemSettings().openAtLogin,
      click: item => { setLoginItem(item.checked); rebuild(); } },
    { type: 'separator' },
    { label: '退出 Unified Memory', click: () => app.quit() }
  ]));
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
  isolated.setPermissionRequestHandler((_wc, _permission, callback) => callback(false));
  isolated.setPermissionCheckHandler(() => false);
  origin = await startBackend();
  // The renderer only needs resources from this instance's loopback port.
  isolated.webRequest.onBeforeRequest({ urls: ['http://*/*','https://*/*'] }, (details, callback) => {
    callback({ cancel: new URL(details.url).origin !== origin });
  });
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
