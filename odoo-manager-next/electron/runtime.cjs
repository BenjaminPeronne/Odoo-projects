const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const net = require('node:net');
const { spawn } = require('node:child_process');
const { randomUUID, createHash } = require('node:crypto');

const APP_ORIGIN = 'app://sdk';
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

function configPath(env = process.env, platform = process.platform, home = os.homedir()) {
  const expand = value => value.replace(/^~(?=$|[\\/])/, home);
  if (env.ODOO_MANAGER_CONFIG) return expand(env.ODOO_MANAGER_CONFIG);
  if (env.ODOO_MANAGER_CONFIG_DIR) return path.join(expand(env.ODOO_MANAGER_CONFIG_DIR), 'config.json');
  const directory = platform === 'darwin' ? path.join(home, 'Library/Application Support/Odoo Manager')
    : platform === 'win32' ? path.join(env.APPDATA || path.join(home, 'AppData/Roaming'), 'Odoo Manager')
      : path.join(env.XDG_CONFIG_HOME || path.join(home, '.config'), 'odoo-manager');
  return path.join(directory, 'config.json');
}

function configuredPort(env = process.env) {
  try {
    const value = JSON.parse(fs.readFileSync(configPath(env), 'utf8')).api_port;
    if (Number.isInteger(value) && value >= 1024 && value <= 65535) return value;
  } catch { /* First launch uses the established port. */ }
  return 18765;
}

function reservePort(port) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once('error', reject);
    server.listen(port, '127.0.0.1', () => {
      const selected = server.address().port;
      server.close(error => error ? reject(error) : resolve(selected));
    });
  });
}

async function selectPort(preferred) {
  try { return await reservePort(preferred); } catch { return reservePort(0); }
}

function externalUrl(value) {
  if (typeof value !== 'string' || value.length > 8192) throw new Error('URL invalide.');
  const url = new URL(value);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) throw new Error('URL non autorisée.');
  return url.href;
}

function staticPath(urlValue, root) {
  const url = new URL(urlValue);
  if (url.protocol !== 'app:' || url.host !== 'sdk') throw new Error('Origine non autorisée.');
  const relative = decodeURIComponent(url.pathname);
  if (relative.includes('\\') || relative.includes('\0')) throw new Error('Chemin invalide.');
  const target = path.resolve(root, '.' + (relative === '/' ? '/index.html' : relative));
  if (!target.startsWith(path.resolve(root) + path.sep)) throw new Error('Chemin non autorisé.');
  return target;
}

function contentPolicy(html, endpoint) {
  // Next export emits bootstrap scripts. Authorize their exact bytes, never arbitrary inline JS.
  const hashes = [...html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/gi)]
    .filter(match => match[1]).map(match => "'sha256-" + createHash('sha256').update(match[1]).digest('base64') + "'");
  return `default-src 'self'; script-src 'self' ${hashes.join(' ')}; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self' ${endpoint}; object-src 'none'; base-uri 'none'; frame-src 'none'; frame-ancestors 'none'; form-action 'none'`;
}

class Backend {
  constructor({ executable, args = [], logDir, env = process.env }) {
    this.executable = executable;
    this.args = args;
    this.logDir = logDir;
    this.logPath = path.join(logDir, 'backend.log');
    this.env = env;
    this.instance = randomUUID();
    this.child = null;
    this.ready = false;
  }

  log(message) { fs.appendFileSync(this.logPath, message + '\n'); }

  async start() {
    fs.mkdirSync(this.logDir, { recursive: true });
    if (fs.existsSync(this.logPath) && fs.statSync(this.logPath).size > 2_000_000) {
      fs.rmSync(path.join(this.logDir, 'backend.previous.log'), { force: true });
      fs.renameSync(this.logPath, path.join(this.logDir, 'backend.previous.log'));
    }
    this.port = await selectPort(configuredPort(this.env));
    this.endpoint = `http://127.0.0.1:${this.port}`;
    this.log(`\n=== SDK Local Manager Electron · ${new Date().toISOString()} · ${this.endpoint} ===`);
    const fd = fs.openSync(this.logPath, 'a');
    try {
      this.child = spawn(this.executable, this.args, {
        env: { ...this.env, ODOO_GUI_HOST: '127.0.0.1', ODOO_GUI_PORT: String(this.port),
          ODOO_MANAGER_LOG_DIR: this.logDir, ODOO_MANAGER_INSTANCE_ID: this.instance },
        windowsHide: true, stdio: ['ignore', fd, fd],
      });
      this.child.on('error', error => { this.error = error; this.log(error.message); });
    } finally { fs.closeSync(fd); }
    for (let attempt = 0; attempt < 120; attempt++) {
      if (this.error) throw this.error;
      if (this.child.exitCode !== null) throw new Error(`Le backend s’est arrêté (code ${this.child.exitCode}).`);
      try {
        const response = await fetch(this.endpoint + '/api/health', { signal: AbortSignal.timeout(500) });
        const health = await response.json();
        if (response.ok && health.ok && health.instance_id === this.instance) {
          this.ready = true;
          this.log('Backend opérationnel et identité vérifiée.');
          return;
        }
      } catch { /* Backend initialization may take several seconds. */ }
      await sleep(250);
    }
    throw new Error('Le backend ne répond pas après le démarrage. Consultez le journal.');
  }

  diagnostics() {
    let details = 'Journal indisponible.';
    try { details = fs.readFileSync(this.logPath, 'utf8').slice(-16000); } catch { /* absent */ }
    return { log_path: this.logPath, details };
  }

  async stop() {
    if (this.stopping) return this.stopping;
    this.stopping = this.terminate();
    return this.stopping;
  }

  async terminate() {
    const child = this.child;
    if (!child || child.exitCode !== null || this.error) return;
    // Never send shutdown to a different instance that acquired the port after a crash.
    try {
      const health = await (await fetch(this.endpoint + '/api/health', { signal: AbortSignal.timeout(750) })).json();
      if (health.instance_id === this.instance) {
        await fetch(this.endpoint + '/api/system/shutdown', { method: 'POST', signal: AbortSignal.timeout(1500) });
      }
    } catch { /* The child may already be exiting. */ }
    for (let i = 0; i < 40 && child.exitCode === null && child.signalCode === null; i++) await sleep(100);
    if (child.exitCode === null && child.signalCode === null) {
      if (process.platform === 'win32') {
        await new Promise(resolve => {
          const killer = spawn('taskkill', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true });
          killer.once('close', resolve); killer.once('error', resolve);
        });
      } else {
        child.kill('SIGTERM');
        for (let i = 0; i < 20 && child.exitCode === null && child.signalCode === null; i++) await sleep(100);
        if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
      }
    }
  }
}

module.exports = { APP_ORIGIN, Backend, configPath, configuredPort, selectPort, externalUrl, staticPath, contentPolicy };
