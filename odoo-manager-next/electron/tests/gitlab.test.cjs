const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { GitLabClient, TOKEN_FILE } = require('../gitlab.cjs');

const TOKEN = 'glpat-abcdefghijklmnopqrst';

function safeStorage() {
  return {
    isEncryptionAvailable: () => true,
    getSelectedStorageBackend: () => 'gnome_libsecret',
    encryptString: value => Buffer.from('enc:' + Buffer.from(value).toString('base64')),
    decryptString: buffer => Buffer.from(buffer.toString().slice(4), 'base64').toString(),
  };
}

function fakeFetch(routes, calls) {
  return async (url, init) => {
    calls.push({ url: new URL(url), headers: init.headers, redirect: init.redirect });
    const route = routes[new URL(url).pathname];
    if (!route) return { ok: false, status: 404, json: async () => ({}) };
    const [status, body] = typeof route === 'function' ? route(new URL(url)) : route;
    return { ok: status < 400, status, json: async () => body };
  };
}

function client(t, routes, calls = []) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sdk-gitlab-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  return { directory, gitlab: new GitLabClient({ directory, safeStorage: safeStorage(), fetch: fakeFetch(routes, calls), platform: 'darwin' }) };
}

test('connects with a validated token stored encrypted, then disconnects', async t => {
  const calls = [];
  const { directory, gitlab } = client(t, { '/api/v4/user': [200, { username: 'bperonne' }] }, calls);

  const status = await gitlab.connect(`  ${TOKEN} `);

  assert.deepEqual(status, { available: true, reason: '', connected: true, username: 'bperonne', url: 'https://gitlab.sudokeys.com' });
  assert.equal(calls[0].url.origin, 'https://gitlab.sudokeys.com');
  assert.equal(calls[0].headers['PRIVATE-TOKEN'], TOKEN);
  assert.equal(calls[0].redirect, 'error');
  assert.ok(!fs.readFileSync(path.join(directory, TOKEN_FILE), 'utf8').includes(TOKEN));
  assert.equal(gitlab.disconnect().connected, false);
});

test('rejects malformed or refused tokens without storing them', async t => {
  const { directory, gitlab } = client(t, { '/api/v4/user': [401, {}] });
  await assert.rejects(gitlab.connect('short'), /Jeton GitLab invalide/);
  await assert.rejects(gitlab.connect(TOKEN), /refusé ou expiré/);
  assert.ok(!fs.existsSync(path.join(directory, TOKEN_FILE)));
});

test('searches member projects and lists branches and tags of the chosen one', async t => {
  const calls = [];
  const { gitlab } = client(t, {
    '/api/v4/user': [200, { username: 'bperonne' }],
    '/api/v4/projects': [200, [
      { id: 42, name: 'protex-addons', path_with_namespace: 'sudokeys/protex-addons', ssh_url_to_repo: 'ssh://git@gitlab.sudokeys.com:10022/sudokeys/protex-addons.git', default_branch: 'DEV' },
      { id: 'bad', name: 'ignored' },
    ]],
    '/api/v4/projects/42/repository/branches': [200, [{ name: 'DEV', default: true }, { name: '17.0' }]],
    '/api/v4/projects/42/repository/tags': [200, [{ name: 'v1.0' }]],
  }, calls);
  await gitlab.connect(TOKEN);

  const projects = await gitlab.searchProjects('protex');
  const refs = await gitlab.listRefs(42, '');

  assert.deepEqual(projects.map(project => [project.id, project.sshUrl, project.defaultBranch]),
    [[42, 'ssh://git@gitlab.sudokeys.com:10022/sudokeys/protex-addons.git', 'DEV']]);
  const search = calls.find(call => call.url.pathname === '/api/v4/projects').url.searchParams;
  assert.equal(search.get('search'), 'protex');
  assert.equal(search.get('membership'), 'true');
  assert.deepEqual(refs, { branches: [{ name: 'DEV', default: true }, { name: '17.0', default: false }], tags: ['v1.0'] });
});

test('refuses API calls while disconnected and invalid identifiers', async t => {
  const { gitlab } = client(t, { '/api/v4/user': [200, { username: 'bperonne' }] });
  await assert.rejects(gitlab.searchProjects('x'), /pas connecté/);
  await assert.rejects(gitlab.listRefs(1, ''), /pas connecté/);

  await gitlab.connect(TOKEN);
  await assert.rejects(gitlab.listRefs('42/../../user', ''), /Dépôt GitLab invalide/);
  await assert.rejects(gitlab.searchProjects('x'.repeat(101)), /Recherche GitLab invalide/);
});
