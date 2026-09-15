export interface DesktopBridge {
  getVersion(): Promise<string>;
  backendEndpoint(): Promise<string>;
  backendDiagnostics(): Promise<{ log_path: string; details: string }>;
  openExternalUrl(url: string): Promise<void>;
  openDockerDesktop(): Promise<void>;
  pickDirectory(defaultPath?: string): Promise<string | null>;
  notificationsSupported(): Promise<boolean>;
  notify(title: string, body: string): Promise<void>;
  rikaCredentials(): Promise<StoredRikaCredentials>;
  saveRikaCredentials(login: string, password: string | null): Promise<void>;
  clearRikaCredentials(): Promise<void>;
}

export interface StoredRikaCredentials {
  available: boolean;
  reason: string;
  login: string;
  password: string;
}

declare global {
  interface Window { sdkDesktop?: DesktopBridge }
}

export function desktopBridge() {
  return typeof window === 'undefined' ? undefined : window.sdkDesktop;
}
