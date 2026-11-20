'use strict';

const { app, BrowserWindow, Menu, Tray, dialog, nativeImage, nativeTheme, session } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const readline = require('node:readline');

app.setName('Unified Memory');
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
    cwd: framework, env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1', PYTHONUNBUFFERED: '1' },
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
  const menu = [
    ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
    { role: 'editMenu' },
    { label: '工作空间', submenu: [
      { label: '搜索记忆或技能', accelerator: 'CmdOrCtrl+F', click: () =>
        win?.webContents.executeJavaScript("document.getElementById('filter-q')?.focus()").catch(() => {}) },
      { label: '后退', accelerator: 'CmdOrCtrl+[', click: () => go(-1) },
      { label: '前进', accelerator: 'CmdOrCtrl+]', click: () => go(1) },
      { type: 'separator' }, { role: 'reload' }, { role: 'togglefullscreen' }
    ] }, { role: 'windowMenu' }
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
