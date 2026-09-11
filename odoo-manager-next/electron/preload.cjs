const { contextBridge, ipcRenderer } = require('electron');

// Expose capabilities individually; the renderer never receives raw IPC or Node access.
contextBridge.exposeInMainWorld('sdkDesktop', Object.freeze({
  getVersion: () => ipcRenderer.invoke('sdk:version'),
  backendEndpoint: () => ipcRenderer.invoke('sdk:backend-endpoint'),
  backendDiagnostics: () => ipcRenderer.invoke('sdk:backend-diagnostics'),
  openExternalUrl: url => ipcRenderer.invoke('sdk:open-external', url),
  openDockerDesktop: () => ipcRenderer.invoke('sdk:open-docker'),
  pickDirectory: defaultPath => ipcRenderer.invoke('sdk:pick-directory', defaultPath),
  notificationsSupported: () => ipcRenderer.invoke('sdk:notifications-supported'),
  notify: (title, body) => ipcRenderer.invoke('sdk:notify', { title, body }),
}));
