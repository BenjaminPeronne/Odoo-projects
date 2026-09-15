const fs = require('node:fs');
const path = require('node:path');
const { readEncryptedJson, secureStorageUnavailableReason, writeEncryptedJson } = require('./credentials.cjs');

// Seule instance autorisée : un jeton ne doit jamais partir vers un autre hôte.
const GITLAB_URL = 'https://gitlab.sudokeys.com';
const TOKEN_FILE = 'gitlab-token.bin';
const REQUEST_TIMEOUT_MS = 15000;
const MAX_QUERY_LENGTH = 100;

function query(value) {
  if (value === undefined || value === null) return '';
  if (typeof value !== 'string' || value.length > MAX_QUERY_LENGTH || /[\0\r\n]/.test(value)) {
    throw new Error('Recherche GitLab invalide.');
  }
  return value.trim();
}

function projectId(value) {
  if (!Number.isInteger(value) || value <= 0) throw new Error('Dépôt GitLab invalide.');
  return value;
}

class GitLabClient {
  constructor({ directory, safeStorage, fetch, platform = process.platform, baseUrl = GITLAB_URL }) {
    this.file = path.join(directory, TOKEN_FILE);
    this.safeStorage = safeStorage;
    this.fetch = fetch;
    this.platform = platform;
    this.baseUrl = baseUrl;
  }

  unavailableReason() {
    return secureStorageUnavailableReason(this.safeStorage, this.platform);
  }

  storedAccount() {
    if (this.unavailableReason()) return undefined;
    try {
      const payload = readEncryptedJson(this.file, this.safeStorage);
      if (payload && typeof payload.token === 'string' && payload.token) return payload;
    } catch {
      // Jeton illisible (trousseau changé) : considéré comme déconnecté.
    }
    return undefined;
  }

  async request(pathname, params, token) {
    const url = new URL(`/api/v4${pathname}`, this.baseUrl);
    for (const [key, value] of Object.entries(params || {})) {
      if (value !== '' && value !== undefined) url.searchParams.set(key, String(value));
    }
    let response;
    try {
      response = await this.fetch(url.href, {
        headers: { 'PRIVATE-TOKEN': token, Accept: 'application/json' },
        redirect: 'error',
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });
    } catch {
      throw new Error('GitLab est injoignable depuis cet ordinateur.');
    }
    if (response.status === 401) throw new Error('Jeton GitLab refusé ou expiré. Reconnecte GitLab dans les paramètres.');
    if (response.status === 403) throw new Error('Le jeton GitLab doit avoir la portée read_api.');
    if (!response.ok) throw new Error(`GitLab a répondu HTTP ${response.status}.`);
    return response.json();
  }

  status() {
    const reason = this.unavailableReason();
    const account = this.storedAccount();
    return { available: !reason, reason, connected: Boolean(account), username: account?.username || '', url: this.baseUrl };
  }

  async connect(token) {
    const reason = this.unavailableReason();
    if (reason) throw new Error(reason);
    if (typeof token !== 'string' || !/^[A-Za-z0-9_.-]{20,255}$/.test(token.trim())) {
      throw new Error('Jeton GitLab invalide.');
    }
    const user = await this.request('/user', {}, token.trim());
    const username = typeof user?.username === 'string' ? user.username : '';
    writeEncryptedJson(this.file, this.safeStorage, { token: token.trim(), username });
    return this.status();
  }

  disconnect() {
    fs.rmSync(this.file, { force: true });
    return this.status();
  }

  requireToken() {
    const account = this.storedAccount();
    if (!account) throw new Error('GitLab n’est pas connecté.');
    return account.token;
  }

  async searchProjects(search) {
    const token = this.requireToken();
    const projects = await this.request('/projects', {
      search: query(search),
      membership: true,
      simple: true,
      archived: false,
      order_by: 'last_activity_at',
      per_page: 20,
    }, token);
    return (Array.isArray(projects) ? projects : []).map(project => ({
      id: project.id,
      name: project.name,
      path: project.path_with_namespace,
      sshUrl: project.ssh_url_to_repo,
      defaultBranch: project.default_branch || '',
      lastActivityAt: project.last_activity_at || '',
    })).filter(project => Number.isInteger(project.id) && typeof project.sshUrl === 'string');
  }

  async listRefs(id, search) {
    const token = this.requireToken();
    const params = { search: query(search), per_page: 50 };
    const [branches, tags] = await Promise.all([
      this.request(`/projects/${projectId(id)}/repository/branches`, params, token),
      this.request(`/projects/${projectId(id)}/repository/tags`, { ...params, per_page: 20 }, token),
    ]);
    const names = items => (Array.isArray(items) ? items : []).map(item => item?.name).filter(name => typeof name === 'string');
    return {
      branches: (Array.isArray(branches) ? branches : [])
        .filter(branch => typeof branch?.name === 'string')
        .map(branch => ({ name: branch.name, default: Boolean(branch.default) })),
      tags: names(tags),
    };
  }
}

module.exports = { GitLabClient, GITLAB_URL, TOKEN_FILE };
