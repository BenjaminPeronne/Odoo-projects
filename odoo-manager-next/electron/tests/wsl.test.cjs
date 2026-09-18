const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const {
  BACKEND_PATH,
  WslEnvironment,
  backendCommand,
  decodeWslOutput,
  expectedChecksum,
  imageFiles,
  importArguments,
  parseDistributions,
  parseWslVersion,
  supportsFileImport,
} = require('../wsl.cjs');

const utf16 = text => Buffer.from(text, 'utf16le');

test('wsl.exe output is decoded from UTF-16', () => {
  assert.deepEqual(parseDistributions(utf16('Ubuntu\r\nSDK-Manager\r\ndocker-desktop\r\n')),
    ['Ubuntu', 'SDK-Manager', 'docker-desktop']);
  assert.equal(parseWslVersion(utf16('Version WSL : 2.4.12.0\r\nVersion du noyau : 5.15.167.4-1\r\n')), '2.4.12.0');
  assert.equal(decodeWslOutput(Buffer.from('déjà en UTF-8', 'utf8')), 'déjà en UTF-8');
});

test('file import needs WSL 2.4.4 or newer', () => {
  assert.equal(supportsFileImport('2.4.12.0'), true);
  assert.equal(supportsFileImport('2.4.4'), true);
  assert.equal(supportsFileImport('2.4.3'), false);
  assert.equal(supportsFileImport('2.0.9'), false);
  assert.equal(supportsFileImport(''), false);
});

test('import keeps the distribution out of the default location and does not launch it', () => {
  assert.deepEqual(
    importArguments({ archive: 'C:\\img.wsl', location: 'C:\\data\\wsl' }),
    ['--install', '--from-file', 'C:\\img.wsl', '--name', 'SDK-Manager', '--location', 'C:\\data\\wsl', '--no-launch'],
  );
});

test('backend runs inside the distribution with its port, since wsl.exe passes no environment', () => {
  const { executable, args } = backendCommand({ port: 18771, instance: 'abc-123' });
  assert.equal(executable, 'wsl.exe');
  assert.deepEqual(args, ['-d', 'SDK-Manager', '--exec', 'env', 'ODOO_GUI_HOST=127.0.0.1', 'ODOO_GUI_PORT=18771',
    'ODOO_MANAGER_INSTANCE_ID=abc-123', BACKEND_PATH]);
});

test('packaged image and checksum sit next to each other', () => {
  const files = imageFiles('C:\\app\\resources', '0.5.0');
  assert.equal(files.archive, path.join('C:\\app\\resources', 'wsl', 'sdk-manager-0.5.0.wsl'));
  assert.equal(files.checksum, files.archive + '.sha256');
});

test('checksum file is read strictly', () => {
  const digest = 'a'.repeat(64);
  assert.equal(expectedChecksum(`${digest}  sdk-manager-0.5.0.wsl\n`), digest);
  assert.throws(() => expectedChecksum('pas une empreinte'));
});

test('the Windows projects folder is seen from the distribution under /mnt', () => {
  assert.equal(WslEnvironment.mountedWindowsPath('C:\\Users\\aymerick\\Odoo-projects'), '/mnt/c/Users/aymerick/Odoo-projects');
  assert.equal(WslEnvironment.mountedWindowsPath('D:/Data/Odoo-projects/'), '/mnt/d/Data/Odoo-projects');
  assert.equal(WslEnvironment.mountedWindowsPath('\\\\wsl.localhost\\SDK-Manager\\home\\sdk'), '');
  assert.equal(WslEnvironment.mountedWindowsPath(''), '');
});

function fakeEnvironment({ distributions = [], release = '', version = '2.4.12.0', backendId = '' } = {}) {
  const calls = [];
  const runner = async (executable, args) => {
    calls.push([executable, ...args].join(' '));
    if (args.includes('--version')) return { stdout: utf16(`Version WSL : ${version}\r\n`), stderr: '', code: 0 };
    if (args.includes('--list')) return { stdout: utf16(distributions.join('\r\n') + '\r\n'), stderr: '', code: 0 };
    if (args.includes('cat')) {
      const value = args.some(arg => arg.endsWith('.build-id')) ? backendId : release;
      if (!value) throw new Error('fichier absent');
      return { stdout: Buffer.from(value + '\n'), stderr: '', code: 0 };
    }
    return { stdout: Buffer.alloc(0), stderr: Buffer.alloc(0), code: 0 };
  };
  return { calls, runner };
}

function backendBuild(content = 'ELF backend') {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sdk-backend-'));
  fs.writeFileSync(path.join(directory, 'odoo-manager-backend'), content);
  fs.mkdirSync(path.join(directory, 'odoo-manager-backend-runtime'));
  return { directory, id: createHash('sha256').update(content).digest('hex') };
}

test('status reports what the setup screen needs, without changing anything', async () => {
  const { calls, runner } = fakeEnvironment({ distributions: ['Ubuntu', 'SDK-Manager'], release: '0.5.0' });
  const environment = new WslEnvironment({ runner, installRoot: 'C:\\data\\wsl' });

  assert.deepEqual(await environment.status(), {
    wslInstalled: true,
    wslVersion: '2.4.12.0',
    supportsFileImport: true,
    distribution: 'SDK-Manager',
    distributionInstalled: true,
    release: '0.5.0',
  });
  assert.ok(calls.every(call => !call.includes('--install')));
});

test('an image that does not match its checksum is refused', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sdk-wsl-'));
  try {
    const archive = path.join(directory, 'image.wsl');
    fs.writeFileSync(archive, 'racine du système');
    fs.writeFileSync(archive + '.sha256', `${'b'.repeat(64)}  image.wsl\n`);
    const { calls, runner } = fakeEnvironment();
    const environment = new WslEnvironment({ runner, installRoot: path.join(directory, 'install') });

    await assert.rejects(() => environment.importDistribution({ archive, checksum: archive + '.sha256' }),
      /ne correspond pas à son empreinte/);
    assert.ok(calls.every(call => !call.includes('--install')));
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('a matching image is imported once and marked sparse', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sdk-wsl-'));
  try {
    const archive = path.join(directory, 'image.wsl');
    const content = 'racine du système';
    fs.writeFileSync(archive, content);
    fs.writeFileSync(archive + '.sha256', `${createHash('sha256').update(content).digest('hex')}  image.wsl\n`);
    const { calls, runner } = fakeEnvironment();
    const installRoot = path.join(directory, 'install');
    const environment = new WslEnvironment({ runner, installRoot });

    await environment.importDistribution({ archive, checksum: archive + '.sha256' });

    assert.ok(calls.some(call => call.includes('--from-file') && call.includes(installRoot)));
    assert.ok(calls.some(call => call.includes('--set-sparse true')));
    assert.ok(fs.existsSync(installRoot));
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('an up-to-date distribution is neither reimported, reprovisioned nor recopied', async () => {
  const build = backendBuild();
  try {
    const { calls, runner } = fakeEnvironment({ distributions: ['SDK-Manager'], release: '0.5.0', backendId: build.id });
    const environment = new WslEnvironment({ runner, installRoot: 'C:\\data\\wsl', mountPath: () => '/mnt/c/app/backend-linux' });

    await environment.prepare({ version: '0.5.0', backendSource: build.directory, archive: 'C:\\img.wsl', checksum: 'C:\\img.wsl.sha256' });

    assert.ok(calls.every(call => !call.includes('--from-file')));
    assert.ok(calls.every(call => !call.includes('provision.sh')));
    assert.ok(calls.every(call => !call.includes('install-backend')));
  } finally {
    fs.rmSync(build.directory, { recursive: true, force: true });
  }
});

test('a new build of the same version still replaces the backend', async () => {
  // Cas rencontré en recette : 0.5.0 installée, nouveau build 0.5.0 avec un autre backend.
  const build = backendBuild('nouveau backend');
  try {
    const { calls, runner } = fakeEnvironment({ distributions: ['SDK-Manager'], release: '0.5.0', backendId: 'ancien' });
    const environment = new WslEnvironment({ runner, installRoot: 'C:\\data\\wsl', mountPath: () => '/mnt/c/app/backend-linux' });

    await environment.prepare({ version: '0.5.0', backendSource: build.directory, archive: 'C:\\img.wsl', checksum: 'C:\\img.wsl.sha256' });

    const install = calls.find(call => call.includes('install-backend'));
    assert.ok(install, 'le backend doit être recopié');
    assert.ok(install.includes('/mnt/c/app/backend-linux'));
    assert.ok(install.includes(build.id));
    assert.ok(calls.every(call => !call.includes('provision.sh')), 'même version : pas de provisionnement');
  } finally {
    fs.rmSync(build.directory, { recursive: true, force: true });
  }
});

test('a new application version reprovisions without touching the projects', async () => {
  const build = backendBuild();
  try {
    const { calls, runner } = fakeEnvironment({ distributions: ['SDK-Manager'], release: '0.4.0', backendId: build.id });
    const environment = new WslEnvironment({ runner, installRoot: 'C:\\data\\wsl', mountPath: () => '/mnt/c/app/backend-linux' });

    await environment.prepare({ version: '0.5.0', backendSource: build.directory, archive: 'C:\\img.wsl', checksum: 'C:\\img.wsl.sha256' });

    assert.ok(calls.every(call => !call.includes('--from-file')), 'la distribution ne doit jamais être réimportée');
    assert.ok(calls.some(call => call.includes('provision.sh') && call.includes('SDK_MANAGER_VERSION=0.5.0')));
  } finally {
    fs.rmSync(build.directory, { recursive: true, force: true });
  }
});

test('the backend is copied as a whole folder and swapped in one step', async () => {
  const build = backendBuild();
  try {
    const { calls, runner } = fakeEnvironment();
    const environment = new WslEnvironment({ runner, installRoot: 'C:\\data\\wsl', mountPath: () => '/mnt/c/app/backend-linux' });

    await environment.installBackend(build.directory);

    const install = calls.find(call => call.includes('install-backend'));
    assert.match(install, /cp -R "\$1"\/\. \/opt\/sdk-manager\/backend\.tmp\//);
    assert.match(install, /mv \/opt\/sdk-manager\/backend\.tmp \/opt\/sdk-manager\/backend/);
    assert.ok(install.includes('-u root'));
  } finally {
    fs.rmSync(build.directory, { recursive: true, force: true });
  }
});

test('a backend outside any Windows drive is refused', async () => {
  const environment = new WslEnvironment({ runner: fakeEnvironment().runner, installRoot: 'C:\\data\\wsl', mountPath: () => '' });
  await assert.rejects(() => environment.installBackend('/somewhere'), /introuvable/);
});

test('the legacy Windows workspace is handed to the backend for migration', () => {
  const { args } = backendCommand({ port: 18765, legacyWorkspace: '/mnt/c/Users/a/Odoo-projects' });
  assert.ok(args.includes('ODOO_MANAGER_LEGACY_WORKSPACE=/mnt/c/Users/a/Odoo-projects'));
});

test('the Windows GitLab key is copied once, private to the environment user', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sdk-ssh-'));
  try {
    fs.writeFileSync(path.join(directory, 'id_rsa'), 'rsa');
    fs.writeFileSync(path.join(directory, 'id_rsa.pub'), 'rsa.pub');
    fs.writeFileSync(path.join(directory, 'id_ed25519'), 'ed');
    fs.writeFileSync(path.join(directory, 'id_ed25519.pub'), 'ed.pub');
    const { calls, runner } = fakeEnvironment();
    const environment = new WslEnvironment({ runner, installRoot: 'C:\data\wsl', mountPath: file => '/mnt/c/Users/a/.ssh/' + path.basename(file) });

    const result = await environment.importSshKey(directory);

    assert.equal(result.key, 'id_ed25519', 'la clé préférée de ssh-keygen passe en premier');
    const command = calls.find(call => call.includes('import-ssh-key'));
    assert.ok(command.includes('-u root'));
    assert.ok(command.includes('install -m 0600 -o sdk -g sdk "$1" "/home/sdk/.ssh/$3"'));
    assert.ok(command.includes('exit 3'), 'une clé existante ne doit jamais être écrasée');
    assert.ok(command.endsWith('/mnt/c/Users/a/.ssh/id_ed25519 /mnt/c/Users/a/.ssh/id_ed25519.pub id_ed25519'));
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});

test('a half key pair is not imported', async () => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sdk-ssh-'));
  try {
    fs.writeFileSync(path.join(directory, 'id_ed25519.pub'), 'ed.pub');
    const environment = new WslEnvironment({ runner: fakeEnvironment().runner, installRoot: 'C:\data\wsl', mountPath: file => file });
    await assert.rejects(() => environment.importSshKey(directory), /Aucune paire de clés/);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
