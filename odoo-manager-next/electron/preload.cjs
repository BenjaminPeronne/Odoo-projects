const { contextBridge, ipcRenderer } = require('electron');

// Expose capabilities individually; the renderer never receives raw IPC or Node access.
contextBridge.exposeInMainWorld('sdkDesktop', Object.freeze({
  getVersion: () => ipcRenderer.invoke('sdk:version'),
  backendEndpoint: () => ipcRenderer.invoke('sdk:backend-endpoint'),
  backendDiagnostics: () => ipcRenderer.invoke('sdk:backend-diagnostics'),
  openExternalUrl: url => ipcRenderer.invoke('sdk:open-external', url),
  openDockerDesktop: () => ipcRenderer.invoke('sdk:open-docker'),
  wslStatus: () => ipcRenderer.invoke('sdk:wsl-status'),
  wslInstallWsl: () => ipcRenderer.invoke('sdk:wsl-install-wsl'),
  wslPrepare: () => ipcRenderer.invoke('sdk:wsl-prepare'),
  wslLegacyWorkspace: () => ipcRenderer.invoke('sdk:wsl-legacy-workspace'),
  relaunch: () => ipcRenderer.invoke('sdk:relaunch'),
  stopLegacyTraefik: () => ipcRenderer.invoke('sdk:stop-legacy-traefik'),
  wslOpenEditor: project => ipcRenderer.invoke('sdk:wsl-open-editor', project),
  wslOpenExplorer: project => ipcRenderer.invoke('sdk:wsl-open-explorer', project),
  pickDirectory: defaultPath => ipcRenderer.invoke('sdk:pick-directory', defaultPath),
  notificationsSupported: () => ipcRenderer.invoke('sdk:notifications-supported'),
  notify: (title, body) => ipcRenderer.invoke('sdk:notify', { title, body }),
  rikaCredentials: () => ipcRenderer.invoke('sdk:rika-credentials'),
  saveRikaCredentials: (login, password) => ipcRenderer.invoke('sdk:save-rika-credentials', { login, password }),
  clearRikaCredentials: () => ipcRenderer.invoke('sdk:clear-rika-credentials'),
  gitlabStatus: () => ipcRenderer.invoke('sdk:gitlab-status'),
  gitlabConnect: token => ipcRenderer.invoke('sdk:gitlab-connect', token),
  gitlabDisconnect: () => ipcRenderer.invoke('sdk:gitlab-disconnect'),
  gitlabProjects: search => ipcRenderer.invoke('sdk:gitlab-projects', search),
  gitlabRefs: (projectId, search) => ipcRenderer.invoke('sdk:gitlab-refs', projectId, search),
}));
