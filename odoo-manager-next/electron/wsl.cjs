'use strict';
// Environnement Linux du gestionnaire sous Windows.
//
// Les projets servis depuis C:\ traversent le pont 9P de Docker Desktop : Odoo met
// 48 à 95 s à démarrer, contre 4 à 6 s sur un système de fichiers Linux. Le
// gestionnaire installe donc sa propre distribution WSL, y lance le backend, et
// l'utilisateur n'ouvre jamais de terminal.
const fs = require('node:fs');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { execFile, spawn } = require('node:child_process');

const DISTRIBUTION = 'SDK-Manager';
const BACKEND_DIRECTORY = '/opt/sdk-manager';
const BACKEND_PATH = BACKEND_DIRECTORY + '/odoo-manager-backend';
const PROVISION_PATH = BACKEND_DIRECTORY + '/provision.sh';
const RELEASE_PATH = '/etc/sdk-manager-release';
const LINUX_WORKSPACE = '/home/sdk/Odoo-projects';
// wsl.exe écrit ses listes en UTF-16LE, y compris dans un tube.
const WSL_ENCODING = 'utf16le';

function decodeWslOutput(value) {
  const buffer = Buffer.isBuffer(value) ? value : Buffer.from(String(value ?? ''), 'binary');
  const text = buffer.includes(0) ? buffer.toString(WSL_ENCODING) : buffer.toString('utf8');
  return text.replace(/^\uFEFF/, '').replace(/\r/g, '');
}

function parseDistributions(output) {
  return decodeWslOutput(output).split('\n').map(line => line.trim()).filter(Boolean);
}

function parseWslVersion(output) {
  const match = decodeWslOutput(output).match(/(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?/);
  return match ? match[0] : '';
}

function supportsFileImport(version) {
  // `wsl --install --from-file` et `--name` datent de WSL 2.4.4.
  const parts = String(version || '').split('.').map(Number);
  if (parts.length < 3 || parts.some(Number.isNaN)) return false;
  const [major, minor, patch] = parts;
  if (major !== 2) return major > 2;
  if (minor !== 4) return minor > 4;
  return patch >= 4;
}

function importArguments({ archive, distribution = DISTRIBUTION, location }) {
  return ['--install', '--from-file', archive, '--name', distribution, '--location', location, '--no-launch'];
}

function backendCommand({ distribution = DISTRIBUTION, port, instance, logLevel } = {}) {
  // `wsl.exe --exec` ne transmet aucune variable d'environnement de Windows : les
  // réglages du backend passent par `env`. Tuer wsl.exe arrête le processus Linux,
  // ce qui interdit un backend orphelin.
  const variables = [`ODOO_GUI_HOST=127.0.0.1`, `ODOO_GUI_PORT=${port}`];
  if (instance) variables.push(`ODOO_MANAGER_INSTANCE_ID=${instance}`);
  if (logLevel) variables.push(`ODOO_MANAGER_LOG_LEVEL=${logLevel}`);
  return { executable: 'wsl.exe', args: ['-d', distribution, '--exec', 'env', ...variables, BACKEND_PATH] };
}

function sha256OfFile(file) {
  return new Promise((resolve, reject) => {
    const digest = createHash('sha256');
    const stream = fs.createReadStream(file);
    stream.on('data', chunk => digest.update(chunk));
    stream.on('error', reject);
    stream.on('end', () => resolve(digest.digest('hex')));
  });
}

function expectedChecksum(text) {
  const match = String(text || '').trim().match(/^([0-9a-f]{64})\b/i);
  if (!match) throw new Error("Fichier d'empreinte illisible.");
  return match[1].toLowerCase();
}

function imageFiles(resourcesRoot, version) {
  const archive = path.join(resourcesRoot, 'wsl', `sdk-manager-${version}.wsl`);
  return { archive, checksum: archive + '.sha256' };
}

class WslEnvironment {
  constructor({
    distribution = DISTRIBUTION,
    installRoot,
    runner = defaultRunner,
    spawner = spawn,
    log = () => {},
  } = {}) {
    this.distribution = distribution;
    this.installRoot = installRoot;
    this.run = runner;
    this.spawn = spawner;
    this.log = log;
  }

  async wslVersion() {
    try {
      const { stdout } = await this.run('wsl.exe', ['--version']);
      return parseWslVersion(stdout);
    } catch {
      return '';
    }
  }

  async distributions() {
    try {
      const { stdout } = await this.run('wsl.exe', ['--list', '--quiet']);
      return parseDistributions(stdout);
    } catch {
      return [];
    }
  }

  async installedRelease() {
    try {
      const { stdout } = await this.runInDistribution(['cat', RELEASE_PATH]);
      return decodeWslOutput(stdout).trim();
    } catch {
      return '';
    }
  }

  /** État complet, sans rien modifier : c'est ce que l'écran d'installation affiche. */
  async status() {
    const version = await this.wslVersion();
    const names = version ? await this.distributions() : [];
    const installed = names.some(name => name.toLowerCase() === this.distribution.toLowerCase());
    return {
      wslInstalled: Boolean(version),
      wslVersion: version,
      supportsFileImport: supportsFileImport(version),
      distribution: this.distribution,
      distributionInstalled: installed,
      release: installed ? await this.installedRelease() : '',
    };
  }

  runInDistribution(command, options = {}) {
    const user = options.asRoot ? ['-u', 'root'] : [];
    return this.run('wsl.exe', ['-d', this.distribution, ...user, '--exec', ...command], options);
  }

  /** Installe WSL lui-même. Une seule élévation, et Windows peut demander un redémarrage. */
  async installWsl() {
    const { stdout, stderr, code } = await this.run(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-Command',
        "$p = Start-Process -FilePath wsl.exe -ArgumentList '--install','--no-distribution' -Verb RunAs -Wait -PassThru; exit $p.ExitCode"],
      { allowFailure: true },
    );
    const output = decodeWslOutput(stdout) + decodeWslOutput(stderr);
    const rebootRequired = code !== 0 || /redémarr|restart/i.test(output);
    this.log(`Installation de WSL: code ${code}. ${output.trim()}`);
    return { ok: code === 0, rebootRequired, message: output.trim() };
  }

  /** Importe l'image, après vérification de son empreinte. */
  async importDistribution({ archive, checksum }) {
    const expected = expectedChecksum(fs.readFileSync(checksum, 'utf8'));
    const actual = await sha256OfFile(archive);
    if (actual !== expected) {
      throw new Error("L'image de l'environnement ne correspond pas à son empreinte : installation refusée.");
    }
    fs.mkdirSync(this.installRoot, { recursive: true });
    await this.run('wsl.exe', importArguments({
      archive, distribution: this.distribution, location: this.installRoot,
    }));
    // Disque creux : l'espace libéré dans la distribution revient à Windows.
    await this.run('wsl.exe', ['--manage', this.distribution, '--set-sparse', 'true'], { allowFailure: true });
    this.log(`Environnement ${this.distribution} installé dans ${this.installRoot}.`);
  }

  /** Copie le backend dans la distribution par un tube : aucun chemin Windows à traduire. */
  async installBackend(source) {
    await this.runInDistribution(['mkdir', '-p', BACKEND_DIRECTORY], { asRoot: true });
    await new Promise((resolve, reject) => {
      const child = this.spawn(
        'wsl.exe',
        ['-d', this.distribution, '-u', 'root', '--exec', 'sh', '-c', `cat > ${BACKEND_PATH}.tmp`],
        { windowsHide: true, stdio: ['pipe', 'ignore', 'pipe'] },
      );
      let error = '';
      child.stderr?.on('data', chunk => { error += chunk.toString(); });
      child.on('error', reject);
      child.on('close', code => (code === 0
        ? resolve()
        : reject(new Error(`Copie du backend refusée (code ${code}). ${error.trim()}`))));
      fs.createReadStream(source).pipe(child.stdin);
    });
    await this.runInDistribution(['chmod', '0755', `${BACKEND_PATH}.tmp`], { asRoot: true });
    await this.runInDistribution(['mv', `${BACKEND_PATH}.tmp`, BACKEND_PATH], { asRoot: true });
  }

  /** Met la distribution au niveau de la version de l'application. Rejouable. */
  async provision(version) {
    await this.runInDistribution(['env', `SDK_MANAGER_VERSION=${version}`, 'sh', PROVISION_PATH], { asRoot: true });
    this.log(`Environnement provisionné en version ${version}.`);
  }

  /** Prépare l'environnement pour la version courante, sans jamais réimporter la distribution. */
  async prepare({ version, backendSource, archive, checksum }) {
    const state = await this.status();
    if (!state.wslInstalled) throw new Error("WSL n'est pas installé.");
    if (!state.distributionInstalled) {
      await this.importDistribution({ archive, checksum });
    }
    if (state.release !== version) {
      await this.installBackend(backendSource);
      await this.provision(version);
    }
    return this.status();
  }

  backendCommand(port, instance) {
    return backendCommand({ distribution: this.distribution, port, instance });
  }

  async openEditor(projectPath) {
    // VS Code ouvre le dossier dans la distribution, pas à travers \\wsl.localhost.
    return this.run('code.cmd', ['--remote', `wsl+${this.distribution}`, projectPath], { allowFailure: true });
  }

  explorerPath(linuxPath) {
    const relative = String(linuxPath || '/').replace(/^\//, '').replace(/\//g, '\\');
    return `\\\\wsl.localhost\\${this.distribution}\\${relative}`;
  }
}

function defaultRunner(executable, args, options = {}) {
  return new Promise((resolve, reject) => {
    execFile(
      executable,
      args,
      { windowsHide: true, encoding: 'buffer', maxBuffer: 8 * 1024 * 1024, timeout: options.timeout ?? 600000 },
      (error, stdout, stderr) => {
        const code = error?.code ?? 0;
        if (error && !options.allowFailure) {
          const detail = decodeWslOutput(stderr) || decodeWslOutput(stdout) || error.message;
          reject(new Error(detail.trim() || error.message));
          return;
        }
        resolve({ stdout, stderr, code });
      },
    );
  });
}

module.exports = {
  BACKEND_PATH,
  DISTRIBUTION,
  LINUX_WORKSPACE,
  WslEnvironment,
  backendCommand,
  decodeWslOutput,
  expectedChecksum,
  imageFiles,
  importArguments,
  parseDistributions,
  parseWslVersion,
  supportsFileImport,
};
