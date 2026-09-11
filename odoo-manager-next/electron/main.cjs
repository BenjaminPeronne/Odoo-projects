const { app, BrowserWindow, ipcMain, dialog, shell, Notification, protocol, net, session, Menu } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { spawn } = require('node:child_process');
const { APP_ORIGIN, Backend, externalUrl, staticPath, contentPolicy } = require('./runtime.cjs');

app.setName('SDK Local Manager');
app.setAppUserModelId('com.sudokeys.odoo-manager');
protocol.registerSchemesAsPrivileged([{ scheme: 'app', privileges: {
  standard: true, secure: true, supportFetchAPI: true, corsEnabled: true, stream: true,
} }]);
const smokePath = process.env.ODOO_MANAGER_SMOKE_REPORT;
// Isolated smoke runs must never attach to or close the user's existing application.
if (smokePath && !process.env.ODOO_MANAGER_CONFIG_DIR) throw new Error('Le smoke test exige une configuration isolée.');
if (smokePath) app.setPath('userData', path.join(process.env.ODOO_MANAGER_CONFIG_DIR, 'electron'));
let window;
let backend;
let quitting = false;
const notifications = new Set();

function run(command, args) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { windowsHide: true, stdio: 'ignore' });
    child.once('error', reject);
    child.once('spawn', () => { child.unref(); resolve(); });
  });
}

async function openDocker() {
  if (process.platform === 'darwin') return run('open', ['-a', 'Docker']);
  if (process.platform === 'win32') {
    const candidates = [path.join(process.env.ProgramFiles || 'C:\\Program Files', 'Docker/Docker/Docker Desktop.exe'),
      path.join(process.env.LOCALAPPDATA || '', 'Programs/Docker/Docker/Docker Desktop.exe')];
    const executable = candidates.find(candidate => fs.existsSync(candidate));
    if (executable) return run(executable, []);
    throw new Error('Docker Desktop est introuvable.');
  }
  return new Promise((resolve, reject) => {
    const child = spawn('systemctl', ['--user', 'start', 'docker-desktop'], { stdio: 'ignore' });
    child.once('error', reject);
    child.once('exit', code => code === 0 ? resolve() : reject(new Error('Démarrez Docker depuis le système.')));
  });
}

function installHandlers() {
  const handle = (name, callback) => ipcMain.handle('sdk:' + name, (event, ...args) => {
    const frame = event.senderFrame;
    if (!window || event.sender !== window.webContents || frame !== window.webContents.mainFrame
        || !frame.url.startsWith(APP_ORIGIN + '/')) throw new Error('Appel natif non autorisé.');
    return callback(...args);
  });
  handle('version', () => app.getVersion());
  handle('backend-endpoint', () => backend.endpoint);
  handle('backend-diagnostics', () => backend.diagnostics());
  handle('open-external', url => shell.openExternal(externalUrl(url)));
  handle('open-docker', openDocker);
  handle('pick-directory', async defaultPath => {
    if (defaultPath !== undefined && (typeof defaultPath !== 'string' || defaultPath.length > 32768)) throw new Error('Chemin invalide.');
    const result = await dialog.showOpenDialog(window, {
      title: 'Choisir le dossier des projets Odoo', defaultPath: defaultPath || undefined,
      properties: ['openDirectory', 'createDirectory'],
    });
    return result.canceled ? null : result.filePaths[0] || null;
  });
  handle('notifications-supported', () => Notification.isSupported());
  handle('notify', payload => {
    if (!payload || typeof payload.title !== 'string' || typeof payload.body !== 'string'
        || payload.title.length > 256 || payload.body.length > 8192) throw new Error('Notification invalide.');
    if (!Notification.isSupported()) return;
    const notification = new Notification(payload);
    notifications.add(notification);
    notification.once('close', () => notifications.delete(notification));
    notification.once('click', () => { window?.show(); window?.focus(); });
    notification.show();
  });
}

async function start() {
  const root = app.getAppPath();
  const out = path.join(root, 'out');
  const binaryRoot = app.isPackaged ? path.join(process.resourcesPath, 'backend') : path.join(root, 'electron/binaries');
  backend = new Backend({
    executable: path.join(binaryRoot, 'odoo-manager-backend' + (process.platform === 'win32' ? '.exe' : '')),
    logDir: process.env.ODOO_MANAGER_LOG_DIR || app.getPath('logs'),
  });
  try { await backend.start(); } catch (error) {
    backend.log(error.stack || error.message);
    // Keep the existing frontend's diagnostics and recovery screen available.
  }
  const csp = contentPolicy(fs.readFileSync(path.join(out, 'index.html'), 'utf8'), backend.endpoint);
  protocol.handle('app', async request => {
    try {
      const target = staticPath(request.url, out);
      if (!fs.existsSync(target) || !fs.statSync(target).isFile()) return new Response('Not found', { status: 404 });
      const response = await net.fetch(pathToFileURL(target).href);
      const headers = new Headers(response.headers);
      headers.set('Content-Security-Policy', csp);
      headers.set('X-Content-Type-Options', 'nosniff');
      return new Response(response.body, { status: response.status, headers });
    } catch { return new Response('Forbidden', { status: 403 }); }
  });
  const mayWriteClipboard = (contents, permission) => contents === window?.webContents
    && contents.getURL().startsWith(APP_ORIGIN + '/') && permission === 'clipboard-sanitized-write';
  session.defaultSession.setPermissionRequestHandler((contents, permission, callback) => callback(mayWriteClipboard(contents, permission)));
  session.defaultSession.setPermissionCheckHandler((contents, permission) => mayWriteClipboard(contents, permission));
  installHandlers();
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    ...(process.platform === 'darwin' ? [{ role: 'appMenu' }] : []),
    { role: 'fileMenu' }, { role: 'editMenu' }, { role: 'viewMenu' }, { role: 'windowMenu' },
  ]));
  window = new BrowserWindow({
    title: 'SDK Local Manager', width: 1440, height: 900, minWidth: 360, minHeight: 640,
    show: false, icon: path.join(root, 'electron/icons/128x128@2x.png'),
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true,
      sandbox: true, nodeIntegration: false, webSecurity: true, webviewTag: false },
  });
  window.webContents.setWindowOpenHandler(({ url }) => {
    try { void shell.openExternal(externalUrl(url)).catch(error => backend.log(error.message)); } catch { /* forbidden scheme */ }
    return { action: 'deny' };
  });
  window.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(APP_ORIGIN + '/')) event.preventDefault();
  });
  window.webContents.on('will-attach-webview', event => event.preventDefault());
  window.webContents.on('render-process-gone', (_event, details) => backend.log(JSON.stringify(details)));
  window.once('ready-to-show', () => { if (!smokePath) window.show(); });
  await window.loadURL(APP_ORIGIN + '/');
  if (smokePath) await smokeCheck();
}

async function smokeCheck() {
  const report = { electron: process.versions.electron, backendReady: backend.ready };
  try {
    report.renderer = await window.webContents.executeJavaScript(`(async () => {
      for (let i = 0; i < 120 && !document.querySelector('button'); i++) await new Promise(r => setTimeout(r, 250));
      return ({
      bridge: typeof window.sdkDesktop?.backendEndpoint === 'function',
      nodeExposed: typeof window.require !== 'undefined' || typeof window.process !== 'undefined',
      version: await window.sdkDesktop.getVersion(),
      health: await (await fetch((await window.sdkDesktop.backendEndpoint()) + '/api/health')).json(),
      title: document.title,
      rendered: !!document.querySelector('button'),
      bootstrap: Object.keys(await (await fetch((await window.sdkDesktop.backendEndpoint()) + '/api/bootstrap')).json())
    }); })()`);
    report.ok = report.backendReady && report.renderer.bridge && !report.renderer.nodeExposed && report.renderer.health.ok
      && report.renderer.rendered && ['overview', 'settings', 'jobs', 'system_status'].every(key => report.renderer.bootstrap.includes(key));
  } catch (error) { report.ok = false; report.error = String(error); }
  fs.writeFileSync(smokePath, JSON.stringify(report, null, 2));
  app.quit();
}

if (!app.requestSingleInstanceLock()) app.quit();
else {
  app.on('second-instance', () => { window?.show(); if (window?.isMinimized()) window.restore(); window?.focus(); });
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', event => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
    void (backend?.stop() || Promise.resolve()).finally(() => app.quit());
  });
  app.whenReady().then(start).catch(async error => {
    if (smokePath) fs.writeFileSync(smokePath, JSON.stringify({ ok: false, error: String(error) }));
    else dialog.showErrorBox('Démarrage impossible', String(error));
    await backend?.stop();
    app.quit();
  });
}
