const { app, BrowserWindow, ipcMain, dialog, shell, Notification, protocol, net, session, Menu, safeStorage } = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { spawn } = require('node:child_process');
const { APP_ORIGIN, Backend, configPath, externalUrl, staticPath, contentPolicy } = require('./runtime.cjs');
const { CredentialStore } = require('./credentials.cjs');
const { GitLabClient } = require('./gitlab.cjs');
const { LINUX_WORKSPACE, WslEnvironment, imageFiles } = require('./wsl.cjs');

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
let wsl;
let quitting = false;
const notifications = new Set();

function wslResources() {
  const root = app.isPackaged ? process.resourcesPath : path.join(app.getAppPath(), 'electron/binaries');
  return {
    ...imageFiles(root, app.getVersion()),
    backendSource: path.join(root, 'backend-linux'),
  };
}

/** Prépare l'environnement Linux pour la version courante, puis dit si le backend peut y tourner. */
async function prepareWslEnvironment() {
  const resources = wslResources();
  const state = await wsl.prepare({ version: app.getVersion(), ...resources });
  return state;
}

/** Dossier de projets du backend Windows, vu depuis la distribution : source de la migration. */
function windowsWorkspaceSeenFromWsl() {
  try {
    return WslEnvironment.mountedWindowsPath(JSON.parse(fs.readFileSync(configPath(), 'utf8')).workspace);
  } catch { return ''; }
}

// Même règle que PROJECT_NAME_RE côté backend : l'interface ne peut pas fabriquer un chemin.
const PROJECT_NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$/;

function projectName(value) {
  if (typeof value !== 'string' || !PROJECT_NAME.test(value)) throw new Error('Nom de projet invalide.');
  return value;
}

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
  handle('wsl-status', () => (wsl ? wsl.status() : { wslInstalled: false, distributionInstalled: false, supported: false }));
  handle('wsl-install-wsl', () => (wsl ? wsl.installWsl() : { ok: false, message: 'Windows uniquement.' }));
  handle('wsl-prepare', () => prepareWslEnvironment());
  handle('wsl-legacy-workspace', () => windowsWorkspaceSeenFromWsl());
  handle('relaunch', () => { app.relaunch(); app.quit(); });
  handle('wsl-open-editor', project => wsl.openEditor(`${LINUX_WORKSPACE}/${projectName(project)}`));
  handle('wsl-open-explorer', project => shell.openPath(wsl.explorerPath(`${LINUX_WORKSPACE}/${projectName(project)}`)));
  handle('pick-directory', async defaultPath => {
    if (defaultPath !== undefined && (typeof defaultPath !== 'string' || defaultPath.length > 32768)) throw new Error('Chemin invalide.');
    const result = await dialog.showOpenDialog(window, {
      title: 'Choisir le dossier des projets Odoo', defaultPath: defaultPath || undefined,
      properties: ['openDirectory', 'createDirectory'],
    });
    return result.canceled ? null : result.filePaths[0] || null;
  });
  const credentials = new CredentialStore({ directory: app.getPath('userData'), safeStorage });
  handle('rika-credentials', () => credentials.read());
  handle('save-rika-credentials', payload => credentials.save(payload));
  handle('clear-rika-credentials', () => credentials.clear());
  // Le jeton GitLab reste dans ce processus : l'interface ne reçoit que des résultats.
  const gitlab = new GitLabClient({ directory: app.getPath('userData'), safeStorage, fetch: (url, init) => net.fetch(url, init) });
  handle('gitlab-status', () => gitlab.status());
  handle('gitlab-connect', token => gitlab.connect(token));
  handle('gitlab-disconnect', () => gitlab.disconnect());
  handle('gitlab-projects', search => gitlab.searchProjects(search));
  handle('gitlab-refs', (id, search) => gitlab.listRefs(id, search));
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
  const logDir = process.env.ODOO_MANAGER_LOG_DIR || app.getPath('logs');
  const options = {
    executable: path.join(binaryRoot, 'odoo-manager-backend' + (process.platform === 'win32' ? '.exe' : '')),
    logDir,
  };
  if (process.platform === 'win32') {
    wsl = new WslEnvironment({
      installRoot: path.join(app.getPath('userData'), 'wsl'),
      log: message => fs.appendFileSync(path.join(logDir, 'backend.log'), message + '\n'),
    });
    // `wsl --list` suffit pour choisir le backend et ne démarre pas la distribution : lire sa
    // version l'aurait démarrée avant même l'affichage de la fenêtre.
    const distributions = await wsl.distributions().catch(() => []);
    if (distributions.some(name => name.toLowerCase() === wsl.distribution.toLowerCase())) {
      const legacyWorkspace = windowsWorkspaceSeenFromWsl();
      options.command = (port, instance) => wsl.backendCommand(port, instance, legacyWorkspace);
      options.preferredPort = 18765;
      // Le backend de ce build doit être en place avant de démarrer : une mise à jour de
      // l'application change le backend sans forcément changer le numéro de version.
      options.beforeStart = () => prepareWslEnvironment().catch(error => {
        fs.appendFileSync(path.join(logDir, 'backend.log'), `Préparation de l'environnement : ${error.message}\n`);
      });
    }
    // Sinon le backend Windows prend le relais, et l'interface propose de préparer le poste.
  }
  backend = new Backend(options);
  // L'adresse suffit pour bâtir la fenêtre ; le backend démarre en parallèle. L'interface a son
  // écran de chargement et réessaie seule, donc rien n'attend ici la disponibilité du backend.
  await backend.reserve();
  const ready = backend.start().catch(error => {
    backend.log(error.stack || error.message);
    // Keep the existing frontend's diagnostics and recovery screen available.
  });
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
  await ready;
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
