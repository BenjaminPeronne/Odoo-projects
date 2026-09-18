import type { WslStatus } from "./wsl-setup";

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
  gitlabStatus(): Promise<GitLabStatus>;
  gitlabConnect(token: string): Promise<GitLabStatus>;
  gitlabDisconnect(): Promise<GitLabStatus>;
  gitlabProjects(search: string): Promise<GitLabProject[]>;
  gitlabRefs(projectId: number, search: string): Promise<GitLabRefs>;
  // Environnement Linux sous Windows : absent des autres systèmes.
  wslStatus?(): Promise<WslStatus>;
  wslInstallWsl?(): Promise<{ ok: boolean; rebootRequired: boolean; message: string }>;
  wslPrepare?(): Promise<WslStatus>;
  wslLegacyWorkspace?(): Promise<string>;
  relaunch?(): Promise<void>;
  stopLegacyTraefik?(): Promise<{ ok: boolean; message: string }>;
  wslImportSshKey?(): Promise<{ ok: boolean; key: string }>;
  wslOpenEditor?(project: string): Promise<void>;
  wslOpenExplorer?(project: string): Promise<void>;
}

export type { WslStatus } from "./wsl-setup";

export interface GitLabStatus {
  available: boolean;
  reason: string;
  connected: boolean;
  username: string;
  url: string;
}

export interface GitLabProject {
  id: number;
  name: string;
  path: string;
  sshUrl: string;
  defaultBranch: string;
  lastActivityAt: string;
}

export interface GitLabRefs {
  branches: { name: string; default: boolean }[];
  tags: string[];
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
