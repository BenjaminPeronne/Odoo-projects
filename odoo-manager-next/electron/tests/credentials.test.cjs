const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { CredentialStore, FILE_NAME } = require('../credentials.cjs');

function fakeSafeStorage({ available = true, backend = 'gnome_libsecret' } = {}) {
  return {
    isEncryptionAvailable: () => available,
    getSelectedStorageBackend: () => backend,
    encryptString: value => Buffer.from('enc:' + Buffer.from(value, 'utf8').toString('base64')),
    decryptString: buffer => {
      const text = buffer.toString();
      if (!text.startsWith('enc:')) throw new Error('clé différente');
      return Buffer.from(text.slice(4), 'base64').toString('utf8');
    },
  };
}

function temporaryDirectory(t) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sdk-credentials-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return directory;
}

test('stores login and password encrypted, never in clear text', t => {
  const directory = temporaryDirectory(t);
  const store = new CredentialStore({ directory, safeStorage: fakeSafeStorage(), platform: 'darwin' });

  store.save({ login: ' b.peronne ', password: 'S3cret!' });

  const raw = fs.readFileSync(path.join(directory, FILE_NAME), 'utf8');
  assert.ok(!raw.includes('S3cret!') && !raw.includes('b.peronne'));
  if (process.platform !== 'win32') assert.equal(fs.statSync(path.join(directory, FILE_NAME)).mode & 0o777, 0o600);
  assert.deepEqual(store.read(), { available: true, reason: '', login: 'b.peronne', password: 'S3cret!' });
});

test('login can be remembered without the password, then forgotten', t => {
  const store = new CredentialStore({ directory: temporaryDirectory(t), safeStorage: fakeSafeStorage(), platform: 'win32' });

  store.save({ login: 'b.peronne', password: null });
  assert.equal(store.read().password, '');

  store.clear();
  assert.equal(store.read().login, '');
});

test('refuses weak Linux storage and unavailable encryption', t => {
  const directory = temporaryDirectory(t);
  for (const safeStorage of [fakeSafeStorage({ backend: 'basic_text' }), fakeSafeStorage({ available: false })]) {
    const store = new CredentialStore({ directory, safeStorage, platform: 'linux' });
    assert.equal(store.read().available, false);
    assert.throws(() => store.save({ login: 'user', password: 'secret' }));
  }
  assert.ok(!fs.existsSync(path.join(directory, FILE_NAME)));
});

test('unreadable or tampered files are ignored and invalid values rejected', t => {
  const directory = temporaryDirectory(t);
  const store = new CredentialStore({ directory, safeStorage: fakeSafeStorage(), platform: 'darwin' });
  fs.writeFileSync(path.join(directory, FILE_NAME), 'garbage');

  const result = store.read();
  assert.equal(result.login, '');
  assert.match(result.reason, /plus lisibles/);
  for (const payload of [{ login: '' }, { login: 'a\nb' }, { login: 'user', password: 'x'.repeat(513) }, { login: 42 }]) {
    assert.throws(() => store.save(payload));
  }
});
