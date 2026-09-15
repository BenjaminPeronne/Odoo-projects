const fs = require('node:fs');
const path = require('node:path');

const FILE_NAME = 'rika-credentials.bin';
const FORMAT_VERSION = 1;
const MAX_FIELD_LENGTH = 512;
// Sous Linux sans trousseau, Electron chiffre avec une clé codée en dur : autant du texte clair.
const WEAK_LINUX_BACKENDS = new Set(['basic_text', 'unknown']);

function field(value, label, { required }) {
  if (value === null || value === undefined || value === '') {
    if (required) throw new Error(`${label} requis.`);
    return null;
  }
  if (typeof value !== 'string' || value.length > MAX_FIELD_LENGTH || /[\0\r\n]/.test(value)) {
    throw new Error(`${label} invalide.`);
  }
  return value;
}

class CredentialStore {
  constructor({ directory, safeStorage, platform = process.platform }) {
    this.file = path.join(directory, FILE_NAME);
    this.safeStorage = safeStorage;
    this.platform = platform;
  }

  unavailableReason() {
    if (!this.safeStorage.isEncryptionAvailable()) {
      return 'Le coffre-fort du système est indisponible sur cet ordinateur.';
    }
    if (this.platform === 'linux' && WEAK_LINUX_BACKENDS.has(this.safeStorage.getSelectedStorageBackend?.())) {
      return 'Aucun trousseau sécurisé (GNOME Keyring ou KWallet) n’est actif : enregistrement refusé.';
    }
    return '';
  }

  read() {
    const reason = this.unavailableReason();
    if (reason) return { available: false, reason, login: '', password: '' };
    let encrypted;
    try {
      encrypted = fs.readFileSync(this.file);
    } catch {
      return { available: true, reason: '', login: '', password: '' };
    }
    try {
      const payload = JSON.parse(this.safeStorage.decryptString(encrypted));
      if (payload?.version !== FORMAT_VERSION) throw new Error('format');
      return {
        available: true,
        reason: '',
        login: field(payload.login, 'Identifiant', { required: true }),
        password: field(payload.password, 'Mot de passe', { required: false }) || '',
      };
    } catch {
      // Clé du trousseau refusée ou changée : le fichier est inexploitable, on le traite comme absent.
      return { available: true, reason: 'Les identifiants enregistrés ne sont plus lisibles. Ressaisis-les.', login: '', password: '' };
    }
  }

  save(credentials) {
    const reason = this.unavailableReason();
    if (reason) throw new Error(reason);
    const login = field(typeof credentials?.login === 'string' ? credentials.login.trim() : credentials?.login, 'Identifiant', { required: true });
    const password = field(credentials?.password, 'Mot de passe', { required: false });
    const encrypted = this.safeStorage.encryptString(JSON.stringify({ version: FORMAT_VERSION, login, password }));
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    const temporary = `${this.file}.${process.pid}.tmp`;
    fs.writeFileSync(temporary, encrypted, { mode: 0o600 });
    fs.renameSync(temporary, this.file);
  }

  clear() {
    fs.rmSync(this.file, { force: true });
  }
}

module.exports = { CredentialStore, FILE_NAME };
