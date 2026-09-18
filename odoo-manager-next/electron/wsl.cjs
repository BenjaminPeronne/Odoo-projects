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
const { execFile } = require('node:child_process');

const DISTRIBUTION = 'SDK-Manager';
const SDK_DIRECTORY = '/opt/sdk-manager';
// Le backend est un exécutable accompagné de son dossier de runtime (PyInstaller onedir) :
// il vit dans son propre dossier, remplacé d'un bloc à chaque mise à jour.
const BACKEND_DIRECTORY = SDK_DIRECTORY + '/backend';
const BACKEND_EXECUTABLE = 'odoo-manager-backend';
const BACKEND_PATH = BACKEND_DIRECTORY + '/' + BACKEND_EXECUTABLE;
const BUILD_ID_FILE = '.build-id';
const PROVISION_PATH = SDK_DIRECTORY + '/provision.sh';
const RELEASE_PATH = '/etc/sdk-manager-release';
const LINUX_WORKSPACE = '/home/sdk/Odoo-projects';
// Clés recherchées dans %USERPROFILE%\.ssh, dans l'ordre de préférence de ssh-keygen.
const SSH_KEY_NAMES = ['id_ed25519', 'id_ecdsa', 'id_rsa'];
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

function backendCommand({ distribution = DISTRIBUTION, port, instance, logLevel, legacyWorkspace } = {}) {
  // `wsl.exe --exec` ne transmet aucune variable d'environnement de Windows : les
  // réglages du backend passent par `env`. Tuer wsl.exe arrête le processus Linux,
  // ce qui interdit un backend orphelin.
  const variables = [`ODOO_GUI_HOST=127.0.0.1`, `ODOO_GUI_PORT=${port}`];
  if (instance) variables.push(`ODOO_MANAGER_INSTANCE_ID=${instance}`);
  if (logLevel) variables.push(`ODOO_MANAGER_LOG_LEVEL=${logLevel}`);
  // Ancien dossier de projets Windows, vu sous /mnt : le backend y propose la migration.
  if (legacyWorkspace) variables.push(`ODOO_MANAGER_LEGACY_WORKSPACE=${legacyWorkspace}`);
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
    log = () => {},
    mountPath = WslEnvironment.mountedWindowsPath,
  } = {}) {
    this.distribution = distribution;
    this.installRoot = installRoot;
    this.run = runner;
    this.log = log;
    this.mountPath = mountPath;
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

  /**
   * Copie le backend Linux (exécutable et dossier de runtime) dans la distribution.
   *
   * La copie passe par le montage /mnt des disques Windows. Elle est préparée à côté puis
   * permutée : une mise à jour interrompue laisse l'ancien backend intact. Le chemin source
   * est un argument, jamais interpolé dans le script.
   */
  async installBackend(source) {
    const mounted = this.mountPath(source);
    if (!mounted) throw new Error(`Backend Linux introuvable : ${source}`);
    const buildId = await sha256OfFile(path.join(source, BACKEND_EXECUTABLE));
    const script = [
      'set -eu',
      `rm -rf ${BACKEND_DIRECTORY}.tmp`,
      `mkdir -p ${BACKEND_DIRECTORY}.tmp`,
      `cp -R "$1"/. ${BACKEND_DIRECTORY}.tmp/`,
      `chmod 0755 ${BACKEND_DIRECTORY}.tmp/${BACKEND_EXECUTABLE}`,
      `printf '%s\\n' "$2" > ${BACKEND_DIRECTORY}.tmp/${BUILD_ID_FILE}`,
      `rm -rf ${BACKEND_DIRECTORY}`,
      `mv ${BACKEND_DIRECTORY}.tmp ${BACKEND_DIRECTORY}`,
      // Emplacement des versions précédentes : fichier unique directement dans /opt/sdk-manager.
      `rm -f ${SDK_DIRECTORY}/${BACKEND_EXECUTABLE}`,
    ].join('\n');
    await this.runInDistribution(['sh', '-c', script, 'install-backend', mounted, buildId], { asRoot: true });
    this.log(`Backend installé dans l'environnement (${buildId.slice(0, 12)}).`);
  }

  async installedBackendId() {
    try {
      const { stdout } = await this.runInDistribution(['cat', `${BACKEND_DIRECTORY}/${BUILD_ID_FILE}`]);
      return decodeWslOutput(stdout).trim();
    } catch {
      return '';
    }
  }

  /** Met la distribution au niveau de la version de l'application. Rejouable. */
  async provision(version) {
    await this.runInDistribution(['env', `SDK_MANAGER_VERSION=${version}`, 'sh', PROVISION_PATH], { asRoot: true });
    this.log(`Environnement provisionné en version ${version}.`);
  }

  /**
   * Prépare l'environnement pour ce build, sans jamais réimporter la distribution.
   *
   * Le backend est comparé par l'empreinte de son exécutable, pas par le numéro de version :
   * deux builds d'une même version (0.5.0-build8, build9…) embarquent des backends différents.
   */
  async prepare({ version, backendSource, archive, checksum }) {
    const state = await this.status();
    if (!state.wslInstalled) throw new Error("WSL n'est pas installé.");
    if (!state.distributionInstalled) {
      await this.importDistribution({ archive, checksum });
    }
    const expected = await sha256OfFile(path.join(backendSource, BACKEND_EXECUTABLE));
    if (await this.installedBackendId() !== expected) {
      await this.installBackend(backendSource);
    }
    if (state.release !== version) {
      await this.provision(version);
    }
    return this.status();
  }

  backendCommand(port, instance, legacyWorkspace = '') {
    return backendCommand({ distribution: this.distribution, port, instance, legacyWorkspace });
  }

  /**
   * Copie la clé SSH GitLab de Windows dans l'environnement, sur demande de l'utilisateur.
   *
   * La clé reste sur le poste. Une clé déjà présente dans l'environnement n'est jamais
   * écrasée ; la clé privée y est lisible par le seul utilisateur `sdk` (0600).
   */
  async importSshKey(windowsSshDirectory) {
    const name = SSH_KEY_NAMES.find(candidate => (
      fs.existsSync(path.join(windowsSshDirectory, candidate))
      && fs.existsSync(path.join(windowsSshDirectory, candidate + '.pub'))));
    if (!name) throw new Error(`Aucune paire de clés SSH dans ${windowsSshDirectory}.`);
    const privateKey = this.mountPath(path.join(windowsSshDirectory, name));
    const publicKey = this.mountPath(path.join(windowsSshDirectory, name + '.pub'));
    if (!privateKey || !publicKey) throw new Error(`Clé SSH hors d'un disque Windows : ${windowsSshDirectory}.`);
    const script = [
      'set -eu',
      'install -d -m 0700 -o sdk -g sdk /home/sdk/.ssh',
      'if [ -e "/home/sdk/.ssh/$3" ]; then echo "Une clé $3 existe déjà dans l\'environnement." >&2; exit 3; fi',
      'install -m 0600 -o sdk -g sdk "$1" "/home/sdk/.ssh/$3"',
      'install -m 0644 -o sdk -g sdk "$2" "/home/sdk/.ssh/$3.pub"',
    ].join('\n');
    await this.runInDistribution(['sh', '-c', script, 'import-ssh-key', privateKey, publicKey, name], { asRoot: true });
    this.log(`Clé SSH ${name} copiée dans l'environnement.`);
    return { ok: true, key: name };
  }

  async openEditor(projectPath) {
    // VS Code ouvre le dossier dans la distribution, pas à travers \\wsl.localhost.
    return this.run('code.cmd', ['--remote', `wsl+${this.distribution}`, projectPath], { allowFailure: true });
  }

  /** Chemin Linux du dossier de projets Windows, pour proposer la migration. */
  static mountedWindowsPath(windowsPath) {
    const match = /^([A-Za-z]):[\\/](.*)$/.exec(String(windowsPath || ''));
    if (!match) return '';
    return `/mnt/${match[1].toLowerCase()}/${match[2].replace(/\\/g, '/')}`.replace(/\/+$/, '');
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
