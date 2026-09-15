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

function secureStorageUnavailableReason(safeStorage, platform = process.platform) {
  if (!safeStorage.isEncryptionAvailable()) {
    return 'Le coffre-fort du système est indisponible sur cet ordinateur.';
  }
  if (platform === 'linux' && WEAK_LINUX_BACKENDS.has(safeStorage.getSelectedStorageBackend?.())) {
    return 'Aucun trousseau sécurisé (GNOME Keyring ou KWallet) n’est actif : enregistrement refusé.';
  }
  return '';
}

// Retourne undefined si le fichier n'existe pas ; lève une erreur s'il est illisible (clé changée, altération).
function readEncryptedJson(file, safeStorage) {
  let encrypted;
  try {
    encrypted = fs.readFileSync(file);
  } catch {
    return undefined;
  }
  const payload = JSON.parse(safeStorage.decryptString(encrypted));
  if (payload?.version !== FORMAT_VERSION) throw new Error('format');
  return payload;
}

function writeEncryptedJson(file, safeStorage, payload) {
  const encrypted = safeStorage.encryptString(JSON.stringify({ ...payload, version: FORMAT_VERSION }));
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const temporary = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(temporary, encrypted, { mode: 0o600 });
  fs.renameSync(temporary, file);
}

class CredentialStore {
  constructor({ directory, safeStorage, platform = process.platform }) {
    this.file = path.join(directory, FILE_NAME);
    this.safeStorage = safeStorage;
    this.platform = platform;
  }

  unavailableReason() {
    return secureStorageUnavailableReason(this.safeStorage, this.platform);
  }

  read() {
    const reason = this.unavailableReason();
    if (reason) return { available: false, reason, login: '', password: '' };
    try {
      const payload = readEncryptedJson(this.file, this.safeStorage);
      if (payload === undefined) return { available: true, reason: '', login: '', password: '' };
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
    writeEncryptedJson(this.file, this.safeStorage, { login, password });
  }

  clear() {
    fs.rmSync(this.file, { force: true });
  }
}

module.exports = { CredentialStore, FILE_NAME, readEncryptedJson, secureStorageUnavailableReason, writeEncryptedJson };
