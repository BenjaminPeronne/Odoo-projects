export interface DesktopBridge {
  getVersion(): Promise<string>;
  backendEndpoint(): Promise<string>;
  backendDiagnostics(): Promise<{ log_path: string; details: string }>;
  openExternalUrl(url: string): Promise<void>;
  openDockerDesktop(): Promise<void>;
  pickDirectory(defaultPath?: string): Promise<string | null>;
  notificationsSupported(): Promise<boolean>;
  notify(title: string, body: string): Promise<void>;
}

declare global {
  interface Window { sdkDesktop?: DesktopBridge }
}

export function desktopBridge() {
  return typeof window === 'undefined' ? undefined : window.sdkDesktop;
}
