"use client";

import {
  Activity,
  AlertTriangle,
  Boxes,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Circle,
  CloudDownload,
  Copy,
  Database,
  ExternalLink,
  FileArchive,
  FolderOpen,
  FolderPlus,
  GitBranch,
  Heart,
  Info,
  KeyRound,
  Loader2,
  Logs,
  MoreHorizontal,
  PackageX,
  Play,
  PlusCircle,
  RefreshCcw,
  Search,
  Settings,
  ShieldCheck,
  Square,
  Terminal,
  Trash2,
  Upload,
} from "lucide-react";
import { DropdownMenu } from "@radix-ui/themes";
import { useCallback, useDeferredValue, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, InteractiveCard } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { FilePicker } from "@/components/ui/file-picker";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { ThemeToggle } from "@/components/theme-toggle";
import { cn } from "@/lib/utils";
import appIcon from "./icon.png";
import localIcon from "./local-icon.png";
import packageMetadata from "../package.json";

type InterfaceIcon = "manager" | "local";

type Project = {
  name: string;
  odoo_status: string;
  postgres_status: string;
  odoo_version?: string;
  url: string;
  database_manager_url: string;
  databases: string[];
  database_versions?: Record<string, string>;
};

type Overview = {
  workspace: string;
  docker_ok: boolean;
  docker_message: string;
  projects: Project[];
};

type DockerStatus = {
  state: "missing" | "starting" | "stopped" | "ready" | string;
  installed: boolean;
  running: boolean;
  message: string;
  platform: string;
  execution_mode: string;
  can_start: boolean;
  version?: string;
  install_guide?: InstallGuide;
};

type InstallGuide = {
  title: string;
  download_url: string;
  install_url: string;
  steps: string[];
};

type TraefikStatus = {
  state: "missing" | "invalid" | "stopped" | "running" | string;
  path: string;
  installed: boolean;
  running: boolean;
  message: string;
  repo?: string;
  requires_docker: boolean;
  can_install: boolean;
  can_start: boolean;
};

type SystemStatus = {
  docker: DockerStatus;
  traefik?: TraefikStatus;
  workspace: string;
  workspace_exists: boolean;
};

type BootstrapSnapshot = {
  overview: Overview;
  system_status: SystemStatus;
  settings: ManagerSettings;
  jobs: Job[];
};

type ManagerSettings = {
  version: number;
  workspace: string;
  execution_mode: "native" | "wsl" | string;
  wsl_distribution: string;
  docker_executable: string;
  traefik_directory: string;
  docker_poll_interval: number;
  api_port: number;
  api_port_actual?: number;
  start_project_before_open: boolean;
  show_technical_details: boolean;
  bases_layout: "classic" | "compact";
  modules_layout: "classic" | "compact";
  interface_icon: InterfaceIcon;
  onboarding_completed: boolean;
  config_file?: string;
  platform?: string;
  workspace_exists?: boolean;
};

type ProjectCreationPrerequisites = {
  workspace: string;
  workspace_exists: boolean;
  workspace_ready: boolean;
  git_available: boolean;
  git_version: string;
  git_install_supported: boolean;
  git_install_message: string;
  ssh_key_present: boolean;
  ssh_keys: string[];
  ssh_keygen_available: boolean;
  tool_environment?: string;
  gitlab_ssh_keys_url: string;
  supported_versions: string[];
};

type SshPublicKey = {
  name: string;
  public_key: string;
};

type Job = {
  id: number;
  title: string;
  project?: string | null;
  status: "running" | "done" | "error" | string;
  started_at: string;
  finished_at?: string | null;
  lines: string[];
  output?: string;
  result?: {
    kind?: string;
    scope?: string;
    mode?: string;
    modules?: string[];
  };
};

type RestoreDatabasePayload = {
  project: string;
  db: string;
  masterPwd: string;
  copy: boolean;
  neutralize: boolean;
  file: File;
};

type ModuleOrigin = "enterprise" | "other";

type ModuleInfo = {
  name: string;
  title: string;
  state: string;
  origin?: string;
  version?: string;
  installed_version?: string;
  path: string;
  source_path?: string;
  link_path?: string;
  path_kind?: string;
  removable?: boolean;
  removal_mode?: string;
  removal_note?: string;
};

const SOCLE_PRESETS = [
  { id: "sales", label: "Ventes", modules: ["sale_management"] },
  { id: "crm", label: "CRM", modules: ["crm"] },
  { id: "purchase", label: "Achats", modules: ["purchase"] },
  { id: "inventory", label: "Inventaire", modules: ["stock"] },
  { id: "accounting_fr", label: "Comptabilité française", modules: ["account_accountant", "l10n_fr"] },
  { id: "manufacturing", label: "Fabrication", modules: ["mrp"] },
  { id: "project", label: "Projet", modules: ["project"] },
  { id: "timesheets", label: "Feuilles de temps", modules: ["hr_timesheet"] },
  { id: "employees", label: "Employés", modules: ["hr"] },
  { id: "time_off", label: "Congés", modules: ["hr_holidays"] },
  { id: "expenses", label: "Notes de frais", modules: ["hr_expense"] },
  { id: "helpdesk", label: "Assistance", modules: ["helpdesk"] },
  { id: "field_service", label: "Services sur site", modules: ["industry_fsm"] },
  { id: "planning", label: "Planification", modules: ["planning"] },
  { id: "documents", label: "Documents", modules: ["documents"] },
  { id: "sign", label: "Signature", modules: ["sign"] },
  { id: "subscriptions", label: "Abonnements", modules: ["sale_subscription"] },
  { id: "point_of_sale", label: "Point de Vente", modules: ["point_of_sale"] },
  { id: "ecommerce", label: "eCommerce", modules: ["website_sale"] },
] as const;

type Toast = {
  id: number;
  kind: "success" | "error" | "info";
  message: string;
};

type DiagnosticIssue = {
  severity: "success" | "warning" | "error" | string;
  title: string;
  details?: string;
  items?: string[];
};

type FilestoreStatus = {
  path: string;
  referenced: number;
  referenced_unique: number;
  actual: number;
  physical_total?: number;
  missing: number;
  module_update_supported?: boolean;
};

type PendingModuleOperation = {
  name: string;
  state: string;
  code_available: boolean;
};

type ZipInspection = {
  modules: string[];
  ignored_symlinks: number;
};

type BackendDiagnostics = {
  log_path: string;
  details: string;
};

type ManagerErrorEntry = {
  id: number;
  timestamp: string;
  source: string;
  project?: string;
  message: string;
  details?: string;
};

type ProjectDiagnostics = {
  project: string;
  docker_ok: boolean;
  odoo_status?: string;
  postgres_status?: string;
  issues: DiagnosticIssue[];
  databases?: Array<{
    name: string;
    filestore?: FilestoreStatus;
    pending_modules?: PendingModuleOperation[];
    pending_missing_modules?: string[];
    ignored_missing_modules?: string[];
    local_excluded_modules?: string[];
  }>;
};

let API_BASE = process.env.NEXT_PUBLIC_ODOO_MANAGER_API?.replace(/\/$/, "") || "";
const FALLBACK_APP_VERSION = packageMetadata.version;
const TAURI_API_RETRY_DELAYS_MS = [0, 250, 750, 1500, 2500];
const BOOTSTRAP_RETRY_DELAYS_MS = [0, 500, 1000, 2000];
const DOCKER_CONFIRM_DELAY_MS = 700;
const API_TIMEOUT_MS = 20_000;
const UPLOAD_TIMEOUT_MS = 120_000;
const LOG_DESCRIPTION_MAX_LENGTH = 240;

class ApiUnavailableError extends Error {
  constructor(message = `Service local SDK Local Manager indisponible. L'application n'arrive pas à joindre l'API locale ${API_BASE || "http://127.0.0.1:18765"}.`) {
    super(message);
    this.name = "ApiUnavailableError";
  }
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response | undefined;
  const retryDelays = isTauriRuntime() ? TAURI_API_RETRY_DELAYS_MS : [0];
  try {
    for (const [index, delay] of retryDelays.entries()) {
      if (delay) await new Promise((resolve) => window.setTimeout(resolve, delay));
      const controller = new AbortController();
      let timedOut = false;
      const timeout = window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, path.includes("/module-zip") ? UPLOAD_TIMEOUT_MS : API_TIMEOUT_MS);
      try {
        response = await fetch(`${API_BASE}${path}`, {
          ...init,
          cache: "no-store",
          headers: init?.body instanceof FormData ? init.headers : { "Content-Type": "application/json", ...init?.headers },
          signal: controller.signal,
        });
        break;
      } catch (error) {
        if (timedOut) throw new ApiUnavailableError("Le service local ne répond pas dans le délai attendu.");
        if (index === retryDelays.length - 1) throw error;
      } finally {
        window.clearTimeout(timeout);
      }
    }
  } catch (error) {
    if (error instanceof ApiUnavailableError) throw error;
    throw new ApiUnavailableError();
  }
  if (!response) throw new ApiUnavailableError();
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error(payload.error || response.statusText);
  }
  return payload as T;
}

function uploadDatabaseBackup(
  payload: RestoreDatabasePayload,
  onProgress: (progress: number) => void,
): Promise<{ job: Job }> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open(
      "POST",
      `${API_BASE}/api/projects/${encodeURIComponent(payload.project)}/database-restore`,
    );
    request.setRequestHeader("Content-Type", "application/zip");
    request.setRequestHeader("X-Odoo-Database-Name", encodeURIComponent(payload.db));
    request.setRequestHeader("X-Odoo-Master-Password", encodeURIComponent(payload.masterPwd));
    request.setRequestHeader("X-Odoo-Copy", payload.copy ? "1" : "0");
    request.setRequestHeader("X-Odoo-Neutralize", payload.neutralize ? "1" : "0");
    request.setRequestHeader("X-File-Name", encodeURIComponent(payload.file.name));
    request.upload.onprogress = (event) => {
      if (event.lengthComputable && event.total > 0) {
        onProgress(Math.min(100, Math.round((event.loaded * 100) / event.total)));
      }
    };
    request.onerror = () => reject(new ApiUnavailableError("Le téléversement de la sauvegarde a échoué."));
    request.onabort = () => reject(new Error("Le téléversement de la sauvegarde a été annulé."));
    request.onload = () => {
      let response: { job?: Job; error?: string } = {};
      try {
        response = request.responseText ? JSON.parse(request.responseText) : {};
      } catch {
        reject(new Error("Le backend a renvoyé une réponse de restauration illisible."));
        return;
      }
      if (request.status < 200 || request.status >= 300 || !response.job) {
        reject(new Error(response.error || `La restauration a été refusée (HTTP ${request.status}).`));
        return;
      }
      onProgress(100);
      resolve({ job: response.job });
    };
    request.send(payload.file);
  });
}

function delay(milliseconds: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));
}

function jobsFingerprint(items: Job[]) {
  return items
    .map((job) => `${job.id}:${job.status}:${job.finished_at || ""}:${job.lines.length}:${job.lines.at(-1) || ""}:${job.output?.length || 0}`)
    .join("|");
}

function isTauriRuntime() {
  return typeof window !== "undefined" && Boolean((window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__);
}

async function invokeDesktop<T>(command: string, args?: Record<string, unknown>) {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(command, args);
}

async function applicationVersion() {
  if (!isTauriRuntime()) return FALLBACK_APP_VERSION;
  try {
    const { getVersion } = await import("@tauri-apps/api/app");
    return await getVersion();
  } catch {
    return FALLBACK_APP_VERSION;
  }
}

async function configureRuntimeApiBase() {
  if (!isTauriRuntime()) return;
  API_BASE = (await invokeDesktop<string>("backend_endpoint")).replace(/\/$/, "");
}

async function openExternalUrl(url?: string) {
  if (!url || url === "#") return false;
  if (!isTauriRuntime()) {
    return Boolean(window.open(url, "_blank", "noopener,noreferrer"));
  }
  await invokeDesktop<void>("open_external_url", { url });
  return true;
}

async function openDockerDesktopNative() {
  await invokeDesktop<void>("open_docker_desktop");
}

async function pickDirectory(defaultPath?: string) {
  if (!isTauriRuntime()) return null;
  const { open } = await import("@tauri-apps/plugin-dialog");
  const selected = await open({
    directory: true,
    multiple: false,
    canCreateDirectories: true,
    defaultPath: defaultPath || undefined,
    title: "Choisir le dossier des projets Odoo",
  });
  return typeof selected === "string" ? selected : null;
}

async function requestTaskNotificationPermission() {
  if (isTauriRuntime()) {
    const { isPermissionGranted, requestPermission } = await import("@tauri-apps/plugin-notification");
    if (await isPermissionGranted()) return true;
    return (await requestPermission()) === "granted";
  }
  if (typeof window === "undefined" || !("Notification" in window)) return false;
  if (window.Notification.permission === "granted") return true;
  if (window.Notification.permission === "denied") return false;
  return (await window.Notification.requestPermission()) === "granted";
}

async function sendTaskNotification(job: Job) {
  const successful = job.status === "done";
  const title = successful ? "Tâche terminée" : "Tâche en erreur";
  const body = job.title;
  if (isTauriRuntime()) {
    const { isPermissionGranted, sendNotification } = await import("@tauri-apps/plugin-notification");
    if (await isPermissionGranted()) sendNotification({ title, body });
    return;
  }
  if (typeof window !== "undefined" && "Notification" in window && window.Notification.permission === "granted") {
    new window.Notification(title, { body });
  }
}

function offlineDockerGuide(): InstallGuide {
  const platform = typeof navigator === "undefined" ? "" : navigator.userAgent.toLowerCase();
  if (platform.includes("windows")) {
    return {
      title: "Installer Docker Desktop pour Windows",
      download_url: "https://www.docker.com/products/docker-desktop/",
      install_url: "https://docs.docker.com/desktop/setup/install/windows-install/",
      steps: [
        "Télécharge Docker Desktop pour Windows depuis le site officiel Docker.",
        "Installe Docker Desktop avec le backend WSL 2 activé.",
        "Redémarre Windows si demandé, lance Docker Desktop, puis clique sur Actualiser.",
      ],
    };
  }
  if (platform.includes("mac")) {
    return {
      title: "Installer Docker Desktop pour Mac",
      download_url: "https://www.docker.com/products/docker-desktop/",
      install_url: "https://docs.docker.com/desktop/setup/install/mac-install/",
      steps: [
        "Télécharge Docker Desktop pour Mac depuis le site officiel Docker.",
        "Ouvre le fichier .dmg, place Docker dans Applications, puis lance Docker Desktop.",
        "Attends que Docker soit démarré, puis clique sur Actualiser.",
      ],
    };
  }
  return {
    title: "Installer Docker",
    download_url: "https://www.docker.com/products/docker-desktop/",
    install_url: "https://docs.docker.com/desktop/setup/install/linux/",
    steps: [
      "Installe Docker Desktop ou Docker Engine selon ta distribution.",
      "Lance Docker et vérifie que la commande docker info répond.",
      "Reviens dans le gestionnaire puis clique sur Actualiser.",
    ],
  };
}

function formatDiagnostics(payload: ProjectDiagnostics) {
  const lines = [
    `Diagnostic projet: ${payload.project}`,
    `Docker: ${payload.docker_ok ? "ok" : "indisponible"}`,
    `Odoo: ${payload.odoo_status || "-"}`,
    `PostgreSQL: ${payload.postgres_status || "-"}`,
    "",
  ];

  if (payload.databases?.length) {
    lines.push("Bases:");
    for (const database of payload.databases) {
      lines.push(`- ${database.name}`);
      if (database.filestore) {
        lines.push(
          `  Filestore: ${database.filestore.actual}/${database.filestore.referenced_unique} fichier(s) unique(s) présents`,
        );
        lines.push(`  Fichiers manquants: ${database.filestore.missing}`);
        lines.push(`  Chemin: ${database.filestore.path}`);
      }
    }
    lines.push("");
  }

  lines.push("Points détectés:");
  for (const issue of payload.issues || []) {
    lines.push(`[${issue.severity.toUpperCase()}] ${issue.title}`);
    if (issue.details) lines.push(issue.details);
    for (const item of issue.items || []) {
      lines.push(`  - ${item}`);
    }
    lines.push("");
  }

  return lines.join("\n").trim();
}

function statusVariant(status: string): "success" | "warning" | "outline" | "destructive" | "secondary" {
  if (status === "running" || status === "healthy" || status === "done") return "success";
  if (status === "error") return "destructive";
  if (status === "exited" || status === "created") return "warning";
  return "secondary";
}

function statusLabel(status: string) {
  if (status === "running") return "En cours";
  if (status === "done") return "Terminée";
  if (status === "error") return "Erreur";
  return status;
}

function statusDot(status: string) {
  if (status === "running" || status === "healthy") return "bg-emerald-500";
  if (status === "exited" || status === "created") return "bg-amber-400";
  if (status === "error") return "bg-red-500";
  return "bg-slate-400";
}

function normalizedModuleOrigin(origin?: string, sourcePath?: string): ModuleOrigin {
  if (origin === "enterprise") return "enterprise";
  const normalizedPath = (sourcePath || "").replace(/\\/g, "/").toLowerCase();
  return normalizedPath.includes("/addons-store/odoo_entreprise/") || normalizedPath.includes("/addons-store/odoo_enterprise/")
    ? "enterprise"
    : "other";
}

function moduleOriginLabel(origin: ModuleOrigin) {
  return origin === "enterprise" ? "Odoo Enterprise" : "Autre";
}

function firstOdooDatabase(project?: Project) {
  return project?.databases?.find((db) => db !== "postgres") || "";
}

function odooAccessUrl(project?: Project, db?: string) {
  if (!project?.url) return "#";
  if (!db || db === "postgres") return project.url;
  try {
    const url = new URL("/web", project.url);
    url.searchParams.set("db", db);
    return url.toString();
  } catch {
    const separator = project.url.includes("?") ? "&" : "?";
    return `${project.url.replace(/\/$/, "")}/web${separator}db=${encodeURIComponent(db)}`;
  }
}

function moduleRepositoryUrlError(value: string) {
  const rawUrl = value.trim();
  if (!rawUrl) return "";
  const validSshUrl = /^(?:ssh:\/\/git@gitlab\.sudokeys\.com:10022\/|git@gitlab\.sudokeys\.com:)[A-Za-z0-9._/-]+\.git$/;
  return validSshUrl.test(rawUrl)
    ? ""
    : "Utilise l’URL SSH du dépôt GitLab Sudokeys, par exemple ssh://git@gitlab.sudokeys.com:10022/equipe/depot.git.";
}

function compactWorkspacePath(path: string | undefined, workspace: string | undefined) {
  if (!path) return "";
  if (!workspace) return path;
  return path.replace(`${workspace.replace(/\/$/, "")}/`, "");
}

function fallbackManagerSettings(
  current: ManagerSettings | null,
  overview: Overview | null,
  systemStatus: SystemStatus | null,
): ManagerSettings {
  return {
    version: current?.version ?? 1,
    workspace: current?.workspace || systemStatus?.workspace || overview?.workspace || "",
    execution_mode: current?.execution_mode || systemStatus?.docker.execution_mode || "native",
    wsl_distribution: current?.wsl_distribution || "",
    docker_executable: current?.docker_executable || "docker",
    traefik_directory: current?.traefik_directory || systemStatus?.traefik?.path || "",
    docker_poll_interval: current?.docker_poll_interval || 10,
    api_port: current?.api_port || 18765,
    api_port_actual: current?.api_port_actual,
    start_project_before_open: current?.start_project_before_open ?? false,
    show_technical_details: current?.show_technical_details ?? false,
    bases_layout: current?.bases_layout === "compact" ? "compact" : "classic",
    modules_layout: current?.modules_layout === "compact" ? "compact" : "classic",
    interface_icon: current?.interface_icon === "local" ? "local" : "manager",
    onboarding_completed: current?.onboarding_completed ?? false,
    config_file: current?.config_file,
    platform: current?.platform || systemStatus?.docker.platform || "",
    workspace_exists: current?.workspace_exists ?? systemStatus?.workspace_exists,
  };
}

export default function Home() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null);
  const [settings, setSettings] = useState<ManagerSettings | null>(null);
  const [settingsDraft, setSettingsDraft] = useState<ManagerSettings | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [managerErrors, setManagerErrors] = useState<ManagerErrorEntry[]>([]);
  const [managerErrorLogPath, setManagerErrorLogPath] = useState("");
  const [loadingManagerErrors, setLoadingManagerErrors] = useState(false);
  const [aboutOpen, setAboutOpen] = useState(false);
  const [appVersion, setAppVersion] = useState(FALLBACK_APP_VERSION);
  const [onboardingOpen, setOnboardingOpen] = useState(false);
  const [createProjectOpen, setCreateProjectOpen] = useState(false);
  const [creationPrerequisites, setCreationPrerequisites] = useState<ProjectCreationPrerequisites | null>(null);
  const [loadingCreationPrerequisites, setLoadingCreationPrerequisites] = useState(false);
  const [sshDialogOpen, setSshDialogOpen] = useState(false);
  const [sshKeys, setSshKeys] = useState<SshPublicKey[]>([]);
  const [selectedSshKeyName, setSelectedSshKeyName] = useState("");
  const [sshComment, setSshComment] = useState("");
  const [generatingSshKey, setGeneratingSshKey] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
  const [selectingWorkspace, setSelectingWorkspace] = useState(false);
  const [projectsFilter, setProjectsFilter] = useState("");
  const [selectedProjectName, setSelectedProjectName] = useState("");
  const [selectedDb, setSelectedDb] = useState("");
  const [modules, setModules] = useState<ModuleInfo[]>([]);
  const [loadingModules, setLoadingModules] = useState(false);
  const [moduleSearch, setModuleSearch] = useState("");
  const [moduleFilter, setModuleFilter] = useState("all");
  const [moduleOriginFilter, setModuleOriginFilter] = useState("all");
  const [modulePage, setModulePage] = useState(1);
  const [selectedModules, setSelectedModules] = useState<Set<string>>(new Set());
  const [socleDialogOpen, setSocleDialogOpen] = useState(false);
  const [selectedSoclePresets, setSelectedSoclePresets] = useState<Set<string>>(new Set());
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<number | null>(null);
  const [logDescriptionExpanded, setLogDescriptionExpanded] = useState(false);
  const [externalLogView, setExternalLogView] = useState<{ title: string; content: string; project: string } | null>(null);
  const [loading, setLoading] = useState(false);
  const [openingOdoo, setOpeningOdoo] = useState(false);
  const [openingPostgresql, setOpeningPostgresql] = useState(false);
  const [initializing, setInitializing] = useState(true);
  const [initializationMessage, setInitializationMessage] = useState("Démarrage du service local…");
  const [initializationError, setInitializationError] = useState("");
  const [backendDiagnostics, setBackendDiagnostics] = useState<BackendDiagnostics | null>(null);
  const [error, setError] = useState("");
  const [apiUnavailable, setApiUnavailable] = useState(false);
  const [desktopRuntime, setDesktopRuntime] = useState(false);
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [repositoryOpen, setRepositoryOpen] = useState(false);
  const [repositorySubmitting, setRepositorySubmitting] = useState(false);
  const [repositoryUrl, setRepositoryUrl] = useState("");
  const [repositoryBranch, setRepositoryBranch] = useState("");
  const [repositoryMode, setRepositoryMode] = useState("add");
  const [repositoryModules, setRepositoryModules] = useState("");
  const repositoryUrlError = moduleRepositoryUrlError(repositoryUrl);
  const [zipDialogOpen, setZipDialogOpen] = useState(false);
  const [createDbOpen, setCreateDbOpen] = useState(false);
  const [restoreDbOpen, setRestoreDbOpen] = useState(false);
  const [neutralizeDbOpen, setNeutralizeDbOpen] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [updateAllDialogOpen, setUpdateAllDialogOpen] = useState(false);
  const [updateScope, setUpdateScope] = useState<"imported" | "all">("all");
  const [updateFilestoreStatus, setUpdateFilestoreStatus] = useState<FilestoreStatus | null>(null);
  const [updatePendingModules, setUpdatePendingModules] = useState<PendingModuleOperation[]>([]);
  const [updateLocalExcludedModules, setUpdateLocalExcludedModules] = useState<string[]>([]);
  const [missingModulesToIgnore, setMissingModulesToIgnore] = useState<Set<string>>(new Set());
  const [allowMissingFilestore, setAllowMissingFilestore] = useState(false);
  const [checkingUpdatePrerequisites, setCheckingUpdatePrerequisites] = useState(false);
  const [uninstallDialogOpen, setUninstallDialogOpen] = useState(false);
  const [deleteCodeDialogOpen, setDeleteCodeDialogOpen] = useState(false);
  const [replaceZipModules, setReplaceZipModules] = useState(true);
  const [zipFile, setZipFile] = useState<File | null>(null);
  const [zipModuleCandidates, setZipModuleCandidates] = useState<string[]>([]);
  const [selectedZipModules, setSelectedZipModules] = useState<Set<string>>(new Set());
  const [inspectingZip, setInspectingZip] = useState(false);
  const [deleteCodeUninstallFirst, setDeleteCodeUninstallFirst] = useState(true);
  const [moduleNames, setModuleNames] = useState("");
  const [deleteConfirm, setDeleteConfirm] = useState("");
  const [pendingUninstallModules, setPendingUninstallModules] = useState<string[]>([]);
  const [pendingDeleteCodeModules, setPendingDeleteCodeModules] = useState<string[]>([]);
  const [activeTab, setActiveTab] = useState("bases");
  const [pendingCreatedProjectName, setPendingCreatedProjectName] = useState("");
  const zipInputRef = useRef<HTMLInputElement>(null);
  const toastId = useRef(1);
  const lastDockerState = useRef<string | null>(null);
  const pendingDockerState = useRef<{ state: string; count: number } | null>(null);
  const consecutiveApiFailures = useRef(0);
  const initializingRef = useRef(true);
  const bootstrapGeneration = useRef(0);
  const overviewRefreshInFlight = useRef(false);
  const systemRefreshInFlight = useRef(false);
  const jobsRefreshInFlight = useRef(false);
  const selectedJobIdRef = useRef<number | null>(null);
  const jobStatuses = useRef<Map<number, string>>(new Map());
  const jobNotificationsInitialized = useRef(false);
  const lastSynchronizedJobCompletion = useRef("");
  const modulesRequestGeneration = useRef(0);
  const zipInspectionGeneration = useRef(0);
  const scheduledTimeouts = useRef<Set<number>>(new Set());
  const onboardingPrompted = useRef(false);
  const logOutputRef = useRef<HTMLPreElement>(null);
  const logAutoFollow = useRef(true);
  const lastLogOutputSource = useRef("");
  const logStreamRef = useRef<EventSource | null>(null);
  const logStreamFirstLineRef = useRef(true);

  const stopLiveLogStream = useCallback(() => {
    logStreamRef.current?.close();
    logStreamRef.current = null;
  }, []);

  const scrollLogOutputToBottom = useCallback(() => {
    window.requestAnimationFrame(() => {
      const output = logOutputRef.current;
      if (output) output.scrollTop = output.scrollHeight;
    });
  }, []);

  const enableLogAutoFollow = useCallback(() => {
    logAutoFollow.current = true;
    scrollLogOutputToBottom();
  }, [scrollLogOutputToBottom]);

  const handleLogOutputScroll = useCallback(() => {
    const output = logOutputRef.current;
    if (!output) return;
    const distanceFromBottom = output.scrollHeight - output.scrollTop - output.clientHeight;
    logAutoFollow.current = distanceFromBottom <= 48;
  }, []);

  const selectedProject = useMemo(
    () => overview?.projects.find((project) => project.name === selectedProjectName) || overview?.projects[0],
    [overview, selectedProjectName],
  );
  const selectedAppIcon = settings?.interface_icon === "local" ? localIcon : appIcon;
  const odooDatabases = useMemo(
    () => (selectedProject?.databases || []).filter((database) => database !== "postgres"),
    [selectedProject],
  );

  const projectJobs = useMemo(
    () => jobs.filter((job) => job.project === selectedProject?.name),
    [jobs, selectedProject?.name],
  );
  const selectedJob = useMemo(
    () => projectJobs.find((job) => job.id === selectedJobId) || projectJobs[0],
    [projectJobs, selectedJobId],
  );
  const hasRunningJobs = useMemo(() => jobs.some((job) => job.status === "running"), [jobs]);
  const projectLifecycleJobs = useMemo(() => {
    const runningJobs = new Map<string, Job>();
    for (const job of jobs) {
      if (job.status === "running") runningJobs.set(job.title, job);
    }
    return runningJobs;
  }, [jobs]);
  const gitInstallRunning = jobs.some((job) => job.status === "running" && job.title === "Installer Git pour Windows");
  const traefikInstallRunning = jobs.some((job) => job.status === "running" && job.title === "Installer Traefik");
  const selectedSshKey = useMemo(
    () => sshKeys.find((key) => key.name === selectedSshKeyName) || sshKeys[0] || null,
    [selectedSshKeyName, sshKeys],
  );

  const filteredProjects = useMemo(() => {
    const query = projectsFilter.trim().toLowerCase();
    return (overview?.projects || [])
      .filter((project) => !query || project.name.toLowerCase().includes(query))
      .map((project, index) => ({ project, index }))
      .sort((left, right) => {
        const rank = (project: Project) => {
          if (project.odoo_status === "running") return 0;
          if (project.odoo_status === "absent" || project.odoo_status === "docker off") return 2;
          return 1;
        };
        return rank(left.project) - rank(right.project) || left.index - right.index;
      })
      .map(({ project }) => project);
  }, [overview, projectsFilter]);

  const deferredModuleSearch = useDeferredValue(moduleSearch);
  const filteredModules = useMemo(() => {
    const query = deferredModuleSearch.trim().toLowerCase();
    return modules
      .filter((module) => !query || module.name.toLowerCase().includes(query))
      .filter((module) => (
        moduleFilter === "all" ||
        module.state === moduleFilter ||
        (moduleFilter === "uninstalled" && module.state === "disponible")
      ))
      .filter((module) => moduleOriginFilter === "all" || normalizedModuleOrigin(module.origin, module.source_path || module.path) === moduleOriginFilter);
  }, [deferredModuleSearch, modules, moduleFilter, moduleOriginFilter]);
  const compactBases = settings?.bases_layout === "compact";
  const compactModules = settings?.modules_layout === "compact";
  const modulesPerPage = compactModules ? 20 : 50;
  const modulePageCount = Math.max(1, Math.ceil(filteredModules.length / modulesPerPage));
  const visibleModules = useMemo(() => {
    const start = (modulePage - 1) * modulesPerPage;
    return filteredModules.slice(start, start + modulesPerPage);
  }, [filteredModules, modulePage, modulesPerPage]);

  const moduleByName = useMemo(() => new Map(modules.map((module) => [module.name, module])), [modules]);
  const installedSoclePresetIds = useMemo<Set<string>>(
    () => new Set<string>(
      SOCLE_PRESETS
        .filter((preset) => preset.modules.every((moduleName) => moduleByName.get(moduleName)?.state === "installed"))
        .map((preset) => preset.id),
    ),
    [moduleByName],
  );
  const pendingModulesWithMissingCode = useMemo(
    () => updatePendingModules.filter((module) => !module.code_available),
    [updatePendingModules],
  );
  const pendingModulesWithAvailableCode = useMemo(
    () => updatePendingModules.filter((module) => module.code_available),
    [updatePendingModules],
  );
  const detectedImportedModules = useMemo(() => {
    const relevantJobs = jobs
      .filter((job) => job.project === selectedProject?.name && job.status === "done")
      .sort((left, right) => right.id - left.id);
    for (const job of relevantJobs) {
      if (job.result?.kind === "module_update") return [];
      if (job.result?.kind === "repository_modules") return job.result.modules || [];
    }
    return [];
  }, [jobs, selectedProject?.name]);
  const allMissingPendingModulesSelected =
    pendingModulesWithMissingCode.length > 0 &&
    pendingModulesWithMissingCode.every((module) => missingModulesToIgnore.has(module.name));
  const someMissingPendingModulesSelected =
    pendingModulesWithMissingCode.some((module) => missingModulesToIgnore.has(module.name)) && !allMissingPendingModulesSelected;
  const filteredModuleNames = useMemo(() => filteredModules.map((module) => module.name), [filteredModules]);
  const selectedFilteredModuleCount = useMemo(
    () => filteredModuleNames.filter((name) => selectedModules.has(name)).length,
    [filteredModuleNames, selectedModules],
  );
  const allFilteredModulesSelected = filteredModuleNames.length > 0 && selectedFilteredModuleCount === filteredModuleNames.length;
  const someFilteredModulesSelected = selectedFilteredModuleCount > 0 && !allFilteredModulesSelected;
  const fallbackDockerGuide = useMemo(() => offlineDockerGuide(), []);
  const showModuleLocations = settings?.show_technical_details ?? false;
  const moduleTableGridColumns = showModuleLocations
    ? "xl:grid-cols-[minmax(210px,1.35fr)_100px_110px_150px_minmax(220px,1.15fr)_200px]"
    : "xl:grid-cols-[minmax(210px,1.35fr)_100px_110px_150px_200px]";

  const schedule = useCallback((callback: () => void | Promise<void>, delay: number) => {
    const timeout = window.setTimeout(() => {
      scheduledTimeouts.current.delete(timeout);
      void callback();
    }, delay);
    scheduledTimeouts.current.add(timeout);
  }, []);

  const pushToast = useCallback((kind: Toast["kind"], message: string) => {
    const id = toastId.current++;
    setToasts((current) => [...current, { id, kind, message }]);
    schedule(() => setToasts((current) => current.filter((toast) => toast.id !== id)), 4200);
    if (kind === "error") {
      void fetch(`${API_BASE}/api/errors/report`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      }).catch(() => undefined);
    }
  }, [schedule]);

  const notifyJobCompletion = useCallback((job: Job) => {
    const successful = job.status === "done";
    pushToast(successful ? "success" : "error", `${successful ? "Tâche terminée" : "Tâche en erreur"} : ${job.title}`);
    void sendTaskNotification(job).catch(() => {
      // A refused system permission must not affect job polling.
    });
  }, [pushToast]);

  const applyJobs = useCallback((nextJobs: Job[], notify = true) => {
    const previousStatuses = jobStatuses.current;
    if (notify && jobNotificationsInitialized.current) {
      for (const job of nextJobs) {
        const previousStatus = previousStatuses.get(job.id);
        if (previousStatus === "running" && (job.status === "done" || job.status === "error")) {
          notifyJobCompletion(job);
        }
      }
    }
    jobStatuses.current = new Map(nextJobs.map((job) => [job.id, job.status]));
    jobNotificationsInitialized.current = true;
    setJobs((current) => jobsFingerprint(current) === jobsFingerprint(nextJobs) ? current : nextJobs);
  }, [notifyJobCompletion]);

  const markApiSuccess = useCallback(() => {
    consecutiveApiFailures.current = 0;
    setApiUnavailable(false);
  }, []);

  const markApiFailure = useCallback((error: unknown) => {
    if (!(error instanceof ApiUnavailableError)) return false;
    consecutiveApiFailures.current += 1;
    if (consecutiveApiFailures.current >= 2) setApiUnavailable(true);
    return consecutiveApiFailures.current === 2;
  }, []);

  const applyBootstrapSnapshot = useCallback((payload: BootstrapSnapshot) => {
    setOverview(payload.overview);
    setSystemStatus(payload.system_status);
    setSettings(payload.settings);
    setSettingsDraft(payload.settings);
    applyJobs(payload.jobs, false);
    setSelectedProjectName((currentName) => {
      const project = payload.overview.projects.find((item) => item.name === currentName) || payload.overview.projects[0];
      setSelectedDb((currentDb) => currentDb !== "postgres" && project?.databases?.includes(currentDb) ? currentDb : firstOdooDatabase(project));
      return project?.name || "";
    });
    setSelectedJobId((currentId) => payload.jobs.some((job) => job.id === currentId) ? currentId : payload.jobs[0]?.id ?? null);
    lastDockerState.current = payload.system_status.docker.state;
    pendingDockerState.current = null;
    markApiSuccess();
    setError("");
  }, [applyJobs, markApiSuccess]);

  const commitSystemStatus = useCallback((payload: SystemStatus, immediate = false) => {
    markApiSuccess();
    const previous = lastDockerState.current;
    const next = payload.docker.state;
    const sameState = previous === next;
    const recoverToReady = payload.docker.running;

    if (!immediate && previous && !sameState && !recoverToReady) {
      const pending = pendingDockerState.current;
      const count = pending?.state === next ? pending.count + 1 : 1;
      pendingDockerState.current = { state: next, count };
      if (count < 2) return false;
    }

    pendingDockerState.current = null;
    setSystemStatus(payload);
    if (!immediate && previous && previous !== next) {
      if (payload.docker.running) pushToast("success", "Docker est maintenant disponible.");
      else pushToast("error", payload.docker.message || "Docker n'est plus disponible.");
    }
    lastDockerState.current = next;
    return true;
  }, [markApiSuccess, pushToast]);

  const initializeApplication = useCallback(async () => {
    const generation = ++bootstrapGeneration.current;
    initializingRef.current = true;
    setInitializing(true);
    setInitializationError("");
    setBackendDiagnostics(null);
    markApiSuccess();

    for (const [attempt, retryDelay] of BOOTSTRAP_RETRY_DELAYS_MS.entries()) {
      if (retryDelay) await delay(retryDelay);
      if (generation !== bootstrapGeneration.current) return;
      setInitializationMessage(attempt === 0 ? "Démarrage du service local…" : "Connexion au service local…");
      try {
        let payload = await api<BootstrapSnapshot>("/api/bootstrap");
        if (!payload.system_status.docker.running) {
          setInitializationMessage("Vérification de Docker et des projets…");
          await delay(DOCKER_CONFIRM_DELAY_MS);
          const confirmation = await api<BootstrapSnapshot>("/api/bootstrap");
          if (
            confirmation.system_status.docker.state !== payload.system_status.docker.state &&
            !confirmation.system_status.docker.running
          ) {
            await delay(DOCKER_CONFIRM_DELAY_MS);
            payload = await api<BootstrapSnapshot>("/api/bootstrap");
          } else {
            payload = confirmation;
          }
        }
        if (generation !== bootstrapGeneration.current) return;
        applyBootstrapSnapshot(payload);
        initializingRef.current = false;
        setInitializing(false);
        return;
      } catch (err) {
        if (generation !== bootstrapGeneration.current) return;
        if (attempt === BOOTSTRAP_RETRY_DELAYS_MS.length - 1) {
          setInitializationError(err instanceof Error ? err.message : "Le service local ne répond pas.");
          setInitializationMessage("Le gestionnaire n’est pas encore prêt.");
          if (isTauriRuntime()) {
            try {
              setBackendDiagnostics(await invokeDesktop<BackendDiagnostics>("backend_diagnostics"));
            } catch {
              setBackendDiagnostics(null);
            }
          }
        }
      }
    }
  }, [applyBootstrapSnapshot, markApiSuccess]);

  const applyOverview = useCallback((payload: Overview) => {
    setOverview((currentOverview) =>
      currentOverview && JSON.stringify(currentOverview) === JSON.stringify(payload) ? currentOverview : payload,
    );
    markApiSuccess();
    setError("");
    setSelectedProjectName((currentName) => {
      const current = payload.projects.find((project) => project.name === currentName) || payload.projects[0];
      if (current && current.name !== currentName) setSelectedDb(firstOdooDatabase(current));
      return current?.name || "";
    });
  }, [markApiSuccess]);

  const refreshOverview = useCallback(async () => {
    if (overviewRefreshInFlight.current) return;
    overviewRefreshInFlight.current = true;
    try {
      const payload = await api<Overview>("/api/overview");
      applyOverview(payload);
    } catch (err) {
      markApiFailure(err);
      setError(!initializingRef.current && !(err instanceof ApiUnavailableError) ? err instanceof Error ? err.message : "Impossible de charger l'overview." : "");
    } finally {
      overviewRefreshInFlight.current = false;
    }
  }, [applyOverview, markApiFailure]);

  const refreshSystemStatus = useCallback(async () => {
    if (systemRefreshInFlight.current) return;
    systemRefreshInFlight.current = true;
    try {
      const payload = await api<SystemStatus>("/api/system/status");
      commitSystemStatus(payload);
    } catch (err) {
      const newlyUnavailable = markApiFailure(err);
      if (!initializingRef.current && (newlyUnavailable || !(err instanceof ApiUnavailableError))) {
        pushToast("error", err instanceof Error ? err.message : "État système indisponible.");
      }
    } finally {
      systemRefreshInFlight.current = false;
    }
  }, [commitSystemStatus, markApiFailure, pushToast]);

  const loadSettings = useCallback(async () => {
    try {
      const payload = await api<{ settings: ManagerSettings }>("/api/settings");
      setSettings(payload.settings);
      setSettingsDraft(payload.settings);
      markApiSuccess();
    } catch (err) {
      markApiFailure(err);
      if (!initializingRef.current && !(err instanceof ApiUnavailableError)) {
        pushToast("error", err instanceof Error ? err.message : "Paramètres indisponibles.");
      }
    }
  }, [markApiFailure, markApiSuccess, pushToast]);

  const openSettingsDialog = useCallback(() => {
    setSettingsDraft(fallbackManagerSettings(settings, overview, systemStatus));
    setSettingsOpen(true);
    void loadSettings();
    void loadSshKeys();
    void loadManagerErrors();
  }, [loadSettings, overview, settings, systemStatus]);

  const loadCreationPrerequisites = useCallback(async () => {
    setLoadingCreationPrerequisites(true);
    try {
      const payload = await api<ProjectCreationPrerequisites>("/api/system/project-creation-prerequisites");
      setCreationPrerequisites(payload);
      markApiSuccess();
      return payload;
    } catch (err) {
      markApiFailure(err);
      pushToast("error", err instanceof Error ? err.message : "Vérification GitLab impossible.");
      return null;
    } finally {
      setLoadingCreationPrerequisites(false);
    }
  }, [markApiFailure, markApiSuccess, pushToast]);

  const reopenInitialConfiguration = useCallback(() => {
    setSettingsOpen(false);
    setOnboardingOpen(true);
    void loadCreationPrerequisites();
  }, [loadCreationPrerequisites]);

  const openCreateProjectDialog = useCallback(() => {
    setCreateProjectOpen(true);
    void loadCreationPrerequisites();
  }, [loadCreationPrerequisites]);

  const completeOnboarding = useCallback(async () => {
    try {
      const payload = await api<{ settings: ManagerSettings }>("/api/settings", {
        method: "POST",
        body: JSON.stringify({ onboarding_completed: true, create_workspace: true }),
      });
      setSettings(payload.settings);
      setSettingsDraft(payload.settings);
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Enregistrement impossible.");
    }
  }, [pushToast]);

  const refreshJobs = useCallback(async (detailJobId?: number | null) => {
    if (jobsRefreshInFlight.current) return;
    jobsRefreshInFlight.current = true;
    try {
      const requestedJobId = detailJobId ?? selectedJobIdRef.current;
      const query = requestedJobId ? `?detail=${encodeURIComponent(requestedJobId)}` : "";
      const payload = await api<{ jobs: Job[] }>(`/api/jobs${query}`);
      applyJobs(payload.jobs);
      markApiSuccess();
      setSelectedJobId((currentId) => currentId ?? payload.jobs[0]?.id ?? null);
    } catch (err) {
      markApiFailure(err);
      // Jobs polling should not break the whole screen.
    } finally {
      jobsRefreshInFlight.current = false;
    }
  }, [applyJobs, markApiFailure, markApiSuccess]);

  useEffect(() => {
    selectedJobIdRef.current = selectedJobId;
  }, [selectedJobId]);

  const refreshModules = useCallback(async () => {
    const projectName = selectedProject?.name;
    const generation = ++modulesRequestGeneration.current;
    if (!projectName || !selectedDb || selectedDb === "postgres") {
      setModules([]);
      setLoadingModules(false);
      return;
    }
    setLoadingModules(true);
    try {
      const payload = await api<{ modules: ModuleInfo[] }>(
        `/api/projects/${encodeURIComponent(projectName)}/modules?db=${encodeURIComponent(selectedDb)}`,
      );
      if (generation !== modulesRequestGeneration.current) return;
      setModules(payload.modules);
      setSelectedModules((current) => {
        const available = new Set(payload.modules.map((module) => module.name));
        return new Set(Array.from(current).filter((name) => available.has(name)));
      });
    } catch (err) {
      if (generation !== modulesRequestGeneration.current) return;
      pushToast("error", err instanceof Error ? err.message : "Impossible de charger les modules.");
    } finally {
      if (generation === modulesRequestGeneration.current) setLoadingModules(false);
    }
  }, [pushToast, selectedDb, selectedProject?.name]);

  useEffect(() => {
    const completionKey = jobs
      .filter((job) => job.project === selectedProject?.name && job.status !== "running")
      .map((job) => `${job.id}:${job.status}:${job.finished_at || ""}`)
      .join("|");
    if (!completionKey || completionKey === lastSynchronizedJobCompletion.current) return;
    lastSynchronizedJobCompletion.current = completionKey;
    void Promise.all([refreshOverview(), refreshModules()]);
  }, [jobs, refreshModules, refreshOverview, selectedProject?.name]);

  useEffect(() => () => {
    for (const timeout of scheduledTimeouts.current) window.clearTimeout(timeout);
    scheduledTimeouts.current.clear();
  }, []);

  useEffect(() => {
    setDesktopRuntime(isTauriRuntime());
    void applicationVersion().then(setAppVersion);
    void configureRuntimeApiBase()
      .then(initializeApplication)
      .catch((err) => {
        setInitializationError(err instanceof Error ? err.message : "Impossible de déterminer le port du gestionnaire.");
        setInitializationMessage("Le gestionnaire n’est pas encore prêt.");
      });
    return () => {
      bootstrapGeneration.current += 1;
    };
  }, []);

  useEffect(() => {
    if (
      initializing ||
      onboardingPrompted.current ||
      !overview ||
      !settings ||
      settings.onboarding_completed ||
      overview.projects.length > 0
    ) return;
    onboardingPrompted.current = true;
    setOnboardingOpen(true);
    void loadCreationPrerequisites();
  }, [initializing, loadCreationPrerequisites, overview, settings]);

  useEffect(() => {
    if (!pendingCreatedProjectName || !overview) return;
    const created = overview.projects.find((project) => project.name === pendingCreatedProjectName);
    if (!created) return;
    setSelectedProjectName(created.name);
    setSelectedDb(firstOdooDatabase(created));
    setPendingCreatedProjectName("");
  }, [overview, pendingCreatedProjectName]);

  useEffect(() => {
    if (initializing) return;
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void refreshJobs();
    };
    const timer = window.setInterval(refreshWhenVisible, hasRunningJobs ? 1200 : 10000);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [hasRunningJobs, initializing, refreshJobs]);

  useEffect(() => {
    if (initializing) return;
    // Safety-net fallback only: /api/stream (below) pushes overview changes live.
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void refreshOverview();
    };
    const timer = window.setInterval(refreshWhenVisible, hasRunningJobs ? 15000 : 45000);
    return () => window.clearInterval(timer);
  }, [hasRunningJobs, initializing, refreshOverview]);

  useEffect(() => {
    if (initializing) return;
    // Safety-net fallback only: /api/stream (below) pushes system status changes live.
    const interval = Math.max(3, settings?.docker_poll_interval || 10) * 1000 * 3;
    const refreshWhenVisible = () => {
      if (document.visibilityState === "visible") void refreshSystemStatus();
    };
    const timer = window.setInterval(refreshWhenVisible, interval);
    return () => window.clearInterval(timer);
  }, [initializing, refreshSystemStatus, settings?.docker_poll_interval]);

  useEffect(() => {
    if (initializing || typeof EventSource === "undefined") return;
    const source = new EventSource(`${API_BASE}/api/stream`);
    source.addEventListener("overview", (event) => {
      try {
        applyOverview(JSON.parse((event as MessageEvent<string>).data) as Overview);
      } catch {
        // Malformed live update: the safety-net poll will resync state.
      }
    });
    source.addEventListener("system_status", (event) => {
      try {
        commitSystemStatus(JSON.parse((event as MessageEvent<string>).data) as SystemStatus);
      } catch {
        // Malformed live update: the safety-net poll will resync state.
      }
    });
    source.addEventListener("job_completed", () => {
      // Applying completed jobs triggers the module synchronization effect.
      // Fetching modules here as well duplicated the filesystem/SQL scan.
      void Promise.all([refreshJobs(), refreshOverview()]);
    });
    return () => source.close();
  }, [applyOverview, commitSystemStatus, initializing, refreshJobs, refreshOverview]);

  useEffect(() => {
    if (selectedProject) {
      setSelectedDb((current) => current !== "postgres" && selectedProject.databases?.includes(current) ? current : firstOdooDatabase(selectedProject));
    }
  }, [selectedProject]);

  useEffect(() => stopLiveLogStream, [stopLiveLogStream]);

  useEffect(() => {
    stopLiveLogStream();
  }, [selectedProject?.name, stopLiveLogStream]);

  useEffect(() => {
    refreshModules();
  }, [refreshModules]);

  async function createJob(action: string, payload: Record<string, unknown> = {}) {
    const useGlobalLoading = action !== "repository_modules";
    if (useGlobalLoading) setLoading(true);
    void requestTaskNotificationPermission().catch(() => {
      // The in-app completion toast remains available if system notifications are refused.
    });
    try {
      const result = await api<{ job: Job }>("/api/jobs", {
        method: "POST",
        body: JSON.stringify({ action, ...payload }),
      });
      setSelectedJobId(result.job.id);
      jobStatuses.current.set(result.job.id, result.job.status);
      setExternalLogView(null);
      enableLogAutoFollow();
      pushToast("success", `Action lancée : ${result.job.title}`);
      void refreshJobs();
      return result.job;
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Action impossible.");
      return null;
    } finally {
      if (useGlobalLoading) setLoading(false);
    }
  }

  function toggleSoclePreset(presetId: string, checked: boolean) {
    setSelectedSoclePresets((current) => {
      const next = new Set(current);
      if (checked) next.add(presetId);
      else next.delete(presetId);
      return next;
    });
  }

  function openSocleDialog() {
    setSelectedSoclePresets(new Set(installedSoclePresetIds));
    setSocleDialogOpen(true);
    void refreshModules();
  }

  async function installSelectedSocle() {
    if (!selectedProject || !selectedDb || !selectedSoclePresets.size) return;
    const presetsToInstall = Array.from(selectedSoclePresets).filter((presetId) => !installedSoclePresetIds.has(presetId));
    if (!presetsToInstall.length) return;
    const job = await createJob("install_socle", {
      project: selectedProject.name,
      db: selectedDb,
      presets: presetsToInstall.join(","),
    });
    if (job) {
      setSocleDialogOpen(false);
      setActiveTab("logs");
      schedule(refreshModules, 2500);
    }
  }

  async function repairEnterpriseLinks() {
    if (!selectedProject) return;
    const job = await createJob("repair_enterprise_links", { project: selectedProject.name });
    if (job) {
      setSocleDialogOpen(false);
      setActiveTab("logs");
      schedule(refreshModules, 1500);
    }
  }

  async function waitForJob(jobId: number, timeoutMilliseconds = 960000) {
    const deadline = Date.now() + timeoutMilliseconds;
    while (Date.now() < deadline) {
      const payload = await api<{ jobs: Job[] }>("/api/jobs");
      applyJobs(payload.jobs);
      const current = payload.jobs.find((job) => job.id === jobId);
      if (!current) throw new Error("L'action de démarrage est introuvable dans l'historique.");
      if (current.status === "done") return current;
      if (current.status === "error") {
        const detail = current.lines.filter(Boolean).at(-1) || "Le projet n'a pas pu démarrer.";
        throw new Error(detail);
      }
      await delay(800);
    }
    throw new Error("Le démarrage d'Odoo prend trop de temps. Consulte les logs de l'action.");
  }

  async function requestDockerStart() {
    setLoading(true);
    try {
      const result = await api<{ ok: boolean; message: string }>("/api/system/docker/start", { method: "POST" });
      pushToast("info", result.message || "Démarrage de Docker demandé.");
      schedule(refreshSystemStatus, 1500);
      schedule(refreshSystemStatus, 5000);
    } catch (err) {
      if (err instanceof ApiUnavailableError && isTauriRuntime()) {
        try {
          await openDockerDesktopNative();
          pushToast("info", "Ouverture de Docker Desktop demandée.");
          schedule(refreshSystemStatus, 3000);
          return;
        } catch (nativeError) {
          pushToast("error", nativeError instanceof Error ? nativeError.message : "Impossible d'ouvrir Docker Desktop.");
          return;
        }
      }
      pushToast("error", err instanceof Error ? err.message : "Impossible de démarrer Docker.");
    } finally {
      setLoading(false);
    }
  }

  async function openUrl(url?: string) {
    try {
      const opened = await openExternalUrl(url);
      if (!opened) pushToast("error", "Lien impossible à ouvrir depuis l'application.");
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Lien impossible à ouvrir depuis l'application.");
    }
  }

  async function openPostgresqlConsole() {
    const db = selectedDatabaseOrNotify("la console PostgreSQL");
    if (!db || !selectedProject || openingPostgresql) return;
    setOpeningPostgresql(true);
    try {
      const result = await api<{ ok: boolean; message: string }>(
        `/api/projects/${encodeURIComponent(selectedProject.name)}/postgresql/open`,
        {
          method: "POST",
          body: JSON.stringify({ db }),
        },
      );
      pushToast("success", result.message || "Console PostgreSQL ouverte.");
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Impossible d’ouvrir la console PostgreSQL.");
    } finally {
      setOpeningPostgresql(false);
    }
  }

  async function requestTraefikInstall() {
    if (traefikInstallRunning) return;
    if (!systemStatus?.docker.running) {
      pushToast("error", "Installe et démarre Docker avant d'installer Traefik.");
      return;
    }
    const prerequisites = creationPrerequisites || await loadCreationPrerequisites();
    if (!prerequisites?.git_available) {
      pushToast("error", "Installe Git avant d'installer Traefik.");
      return;
    }
    const job = await createJob("install_traefik");
    if (job) {
      schedule(refreshSystemStatus, 2500);
      schedule(refreshOverview, 4000);
    }
  }

  async function requestGitInstall() {
    if (gitInstallRunning) return;
    const job = await createJob("install_git");
    if (!job) return;
    const refreshPrerequisites = async () => { await loadCreationPrerequisites(); };
    schedule(refreshPrerequisites, 3000);
    schedule(refreshPrerequisites, 10000);
    schedule(refreshPrerequisites, 25000);
  }

  async function loadSshKeys() {
    try {
      const payload = await api<{ keys: SshPublicKey[] }>("/api/system/ssh-keys");
      setSshKeys(payload.keys);
      setSelectedSshKeyName((current) => {
        if (payload.keys.some((key) => key.name === current)) return current;
        return payload.keys.find((key) => key.name === "id_ed25519.pub")?.name || payload.keys[0]?.name || "";
      });
      return payload.keys;
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Impossible de lire les clés SSH.");
      return [];
    }
  }

  async function loadManagerErrors() {
    setLoadingManagerErrors(true);
    try {
      const payload = await api<{ entries: ManagerErrorEntry[]; path: string }>("/api/errors");
      setManagerErrors(payload.entries);
      setManagerErrorLogPath(payload.path);
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Impossible de charger le journal d’erreurs.");
    } finally {
      setLoadingManagerErrors(false);
    }
  }

  async function copyManagerErrors() {
    const content = managerErrors.map((entry) => [
      `[${entry.timestamp}] ${entry.source}${entry.project ? ` · ${entry.project}` : ""}`,
      entry.message,
      entry.details || "",
    ].filter(Boolean).join("\n")).join("\n\n");
    try {
      await navigator.clipboard.writeText(content);
      pushToast("success", "Journal d’erreurs copié.");
    } catch {
      pushToast("error", "Impossible de copier le journal d’erreurs.");
    }
  }

  async function clearManagerErrors() {
    try {
      await api<{ ok: boolean }>("/api/errors", { method: "DELETE" });
      setManagerErrors([]);
      pushToast("success", "Journal d’erreurs effacé.");
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Impossible d’effacer le journal d’erreurs.");
    }
  }

  async function openSshAssistant() {
    setSshDialogOpen(true);
    await loadSshKeys();
  }

  async function requestSshKeyGeneration() {
    setGeneratingSshKey(true);
    try {
      const key = await api<SshPublicKey & { created: boolean; message: string }>("/api/system/ssh-key/generate", {
        method: "POST",
        body: JSON.stringify({ comment: sshComment }),
      });
      pushToast("success", key.message);
      await loadSshKeys();
      setSelectedSshKeyName(key.name);
      await loadCreationPrerequisites();
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Impossible de générer la clé SSH.");
    } finally {
      setGeneratingSshKey(false);
    }
  }

  async function copySshPublicKey() {
    const key = sshKeys.find((item) => item.name === selectedSshKeyName);
    if (!key) return;
    try {
      await navigator.clipboard.writeText(key.public_key);
      pushToast("success", "Clé publique copiée.");
    } catch {
      pushToast("error", "Impossible de copier la clé publique.");
    }
  }

  async function requestProjectCreation(payload: Record<string, unknown>) {
    const projectName = String(payload.name || "").trim();
    const job = await createJob("create_project", payload);
    if (!job) return;
    setPendingCreatedProjectName(projectName);
    setCreateProjectOpen(false);
    setOnboardingOpen(false);
    setActiveTab("logs");
    await completeOnboarding();
    schedule(refreshOverview, 2500);
  }

  async function submitRepositoryModules() {
    if (!selectedProject || repositorySubmitting) return;
    setRepositorySubmitting(true);
    try {
      const job = await createJob("repository_modules", {
        project: selectedProject.name,
        url: repositoryUrl.trim(),
        branch: repositoryBranch.trim(),
        mode: repositoryMode,
        modules: repositoryModules.trim(),
      });
      if (!job) return;
      setRepositoryOpen(false);
      setActiveTab("logs");
    } finally {
      setRepositorySubmitting(false);
    }
  }

  async function saveSettings() {
    if (!settingsDraft) return;
    const apiPortChanged = settings?.api_port !== settingsDraft.api_port;
    setSavingSettings(true);
    try {
      const payload = await api<{ settings: ManagerSettings }>("/api/settings", {
        method: "POST",
        body: JSON.stringify({ ...settingsDraft, execution_mode: "native", create_workspace: true }),
      });
      setSettings(payload.settings);
      setSettingsDraft(payload.settings);
      setSettingsOpen(false);
      setSelectedProjectName("");
      setSelectedDb("");
      setModules([]);
      pushToast("success", apiPortChanged ? "Paramètres enregistrés. Redémarre le gestionnaire pour appliquer le nouveau port." : "Paramètres enregistrés.");
      await Promise.all([refreshOverview(), refreshSystemStatus()]);
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Enregistrement impossible.");
    } finally {
      setSavingSettings(false);
    }
  }

  async function selectWorkspaceDirectory() {
    if (!settingsDraft || !desktopRuntime) return;
    setSelectingWorkspace(true);
    try {
      const selected = await pickDirectory(settingsDraft.workspace);
      if (selected) {
        setSettingsDraft({ ...settingsDraft, workspace: selected });
      }
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Impossible d’ouvrir le sélecteur de dossier.");
    } finally {
      setSelectingWorkspace(false);
    }
  }

  function selectedDatabaseOrNotify(action: string) {
    if (!selectedProject) {
      pushToast("error", `Sélectionne un projet avant de lancer ${action}.`);
      return "";
    }
    if (!canUseDb) {
      pushToast("error", `Sélectionne une base Odoo avant de lancer ${action}. La base technique postgres n'est pas utilisable ici.`);
      return "";
    }
    return selectedDb;
  }

  async function requestUpdateAllOdooModules() {
    const db = selectedDatabaseOrNotify("la MAJ complète Odoo");
    if (!db || !selectedProject) return;
    setAllowMissingFilestore(false);
    setUpdateFilestoreStatus(null);
    setUpdatePendingModules([]);
    setUpdateLocalExcludedModules([]);
    setMissingModulesToIgnore(new Set());
    setUpdateScope(detectedImportedModules.length ? "imported" : "all");
    setCheckingUpdatePrerequisites(true);
    try {
      const diagnostics = await api<ProjectDiagnostics>(`/api/projects/${encodeURIComponent(selectedProject.name)}/diagnostics`);
      const database = diagnostics.databases?.find((item) => item.name === db);
      setUpdateFilestoreStatus(database?.filestore || null);
      setUpdatePendingModules(
        database?.pending_modules ||
        (database?.pending_missing_modules || []).map((name) => ({ name, state: "en attente", code_available: false })),
      );
      setUpdateLocalExcludedModules(database?.local_excluded_modules || database?.ignored_missing_modules || []);
    } catch (err) {
      pushToast("info", err instanceof Error ? err.message : "Précontrôle du filestore indisponible.");
    } finally {
      setCheckingUpdatePrerequisites(false);
      setUpdateAllDialogOpen(true);
    }
  }

  function toggleMissingModuleToIgnore(moduleName: string, checked: boolean) {
    setMissingModulesToIgnore((current) => {
      const next = new Set(current);
      if (checked) next.add(moduleName);
      else next.delete(moduleName);
      return next;
    });
  }

  function toggleAllMissingModulesToIgnore(checked: boolean) {
    setMissingModulesToIgnore(checked ? new Set(pendingModulesWithMissingCode.map((module) => module.name)) : new Set());
  }

  async function ignoreSelectedMissingModulesLocally() {
    const db = selectedDatabaseOrNotify("l'annulation locale des opérations module");
    if (!db || !selectedProject || !missingModulesToIgnore.size) return;
    const modulesToIgnore = Array.from(missingModulesToIgnore).sort();
    const job = await createJob("ignore_missing_modules_locally", {
      project: selectedProject.name,
      db,
      modules: modulesToIgnore.join(","),
    });
    if (job) {
      setUpdateAllDialogOpen(false);
      try {
        await waitForJob(job.id);
        pushToast("success", "Les opérations locales ont été annulées. Le précontrôle est actualisé.");
        await refreshModules();
        await requestUpdateAllOdooModules();
      } catch (err) {
        pushToast("error", err instanceof Error ? err.message : "Impossible d’actualiser le précontrôle.");
        setActiveTab("logs");
      }
    }
  }

  async function restoreLocalModuleExclusions() {
    const db = selectedDatabaseOrNotify("la réactivation des mises à jour module");
    if (!db || !selectedProject || !updateLocalExcludedModules.length) return;
    const job = await createJob("restore_module_update_exclusions", {
      project: selectedProject.name,
      db,
      modules: updateLocalExcludedModules.join(","),
    });
    if (job) {
      setUpdateAllDialogOpen(false);
      schedule(refreshModules, 1500);
    }
  }

  async function confirmUpdateAllOdooModules() {
    const db = selectedDatabaseOrNotify("la MAJ complète Odoo");
    if (!db || !selectedProject) return;
    const targeted = updateScope === "imported" && detectedImportedModules.length > 0;
    const job = await createJob(
      targeted ? "update_imported_modules" : "update_all_modules",
      targeted
        ? { project: selectedProject.name, db, modules: detectedImportedModules.join(",") }
        : { project: selectedProject.name, db, allow_missing_filestore: allowMissingFilestore },
    );
    if (job) {
      setUpdateAllDialogOpen(false);
      schedule(refreshModules, 2500);
    }
  }

  async function refreshAllViews() {
    await Promise.all([refreshOverview(), refreshSystemStatus(), refreshJobs()]);
  }

  async function copyOutput() {
    try {
      await navigator.clipboard.writeText(outputContent);
      pushToast("success", "Sortie copiée.");
    } catch {
      pushToast("error", "Impossible de copier la sortie.");
    }
  }

  function showLogs() {
    if (!selectedProject) return;
    const projectName = selectedProject.name;
    stopLiveLogStream();
    logStreamFirstLineRef.current = true;
    setExternalLogView({
      title: `Logs Odoo (direct) - ${projectName}`,
      content: "Connexion au flux de logs en direct…",
      project: projectName,
    });
    enableLogAutoFollow();
    if (typeof EventSource === "undefined") {
      pushToast("error", "Le suivi en direct des logs n'est pas disponible dans cet environnement.");
      return;
    }
    const source = new EventSource(`${API_BASE}/api/projects/${encodeURIComponent(projectName)}/logs/stream`);
    logStreamRef.current = source;
    source.addEventListener("log", (event) => {
      let line = "";
      try {
        line = (JSON.parse((event as MessageEvent<string>).data) as { line?: string }).line || "";
      } catch {
        return;
      }
      setExternalLogView((current) => {
        if (!current || current.project !== projectName) return current;
        const content = logStreamFirstLineRef.current ? line : `${current.content}\n${line}`;
        logStreamFirstLineRef.current = false;
        return { ...current, content };
      });
    });
    source.addEventListener("log_end", () => {
      if (logStreamRef.current === source) stopLiveLogStream();
    });
    source.onerror = () => {
      if (logStreamRef.current === source) pushToast("error", "Flux de logs interrompu, nouvelle tentative en cours…");
    };
  }

  async function showDiagnostics() {
    if (!selectedProject) return;
    stopLiveLogStream();
    try {
      const payload = await api<ProjectDiagnostics>(`/api/projects/${encodeURIComponent(selectedProject.name)}/diagnostics`);
      setExternalLogView({
        title: `Diagnostic - ${selectedProject.name}`,
        content: formatDiagnostics(payload),
        project: selectedProject.name,
      });
      enableLogAutoFollow();
      pushToast("info", "Diagnostic projet chargé.");
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Diagnostic indisponible.");
    }
  }

  async function clearJobs() {
    stopLiveLogStream();
    try {
      await api<{ ok: boolean }>("/api/jobs", { method: "DELETE" });
      setSelectedJobId(null);
      setExternalLogView(null);
      await refreshJobs();
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Suppression de l'historique impossible.");
    }
  }

  async function deleteJob(jobId: number) {
    try {
      await api<{ ok: boolean }>(`/api/jobs/${jobId}`, { method: "DELETE" });
      if (selectedJobId === jobId) {
        stopLiveLogStream();
        setSelectedJobId(null);
        setExternalLogView(null);
      }
      await refreshJobs();
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Suppression de l'entrée impossible.");
    }
  }

  function selectJob(jobId: number) {
    stopLiveLogStream();
    setExternalLogView(null);
    setSelectedJobId(jobId);
    selectedJobIdRef.current = jobId;
    enableLogAutoFollow();
    void refreshJobs(jobId);
  }

  function requestUninstall(moduleNames: string[]) {
    const installed = moduleNames.filter((name) => modules.find((module) => module.name === name)?.state === "installed");
    if (!installed.length) {
      pushToast("error", "Sélectionne au moins un module installé.");
      return;
    }
    setPendingUninstallModules(installed);
    setUninstallDialogOpen(true);
  }

  async function confirmUninstall() {
    if (!selectedProject || !selectedDb || !pendingUninstallModules.length) return;
    const job = await createJob("uninstall_module", {
      project: selectedProject.name,
      db: selectedDb,
      modules: pendingUninstallModules.join(","),
    });
    if (job) {
      setUninstallDialogOpen(false);
      setPendingUninstallModules([]);
      setSelectedModules(new Set());
      schedule(refreshModules, 2500);
    }
  }

  function requestDeleteCode(moduleNames: string[]) {
    const removable = moduleNames.filter((name) => {
      const module = modules.find((candidate) => candidate.name === name);
      return module?.removable && module.removal_mode !== "link_only";
    });
    if (!removable.length) {
      pushToast("error", "Sélectionne au moins un module supprimable du dossier addons.");
      return;
    }
    if (removable.length < moduleNames.length) {
      pushToast("info", "Certains modules protégés ont été ignorés.");
    }
    setPendingDeleteCodeModules(removable);
    setDeleteCodeUninstallFirst(Boolean(canUseDb));
    setDeleteCodeDialogOpen(true);
  }

  async function confirmDeleteCode() {
    if (!selectedProject || !pendingDeleteCodeModules.length) return;
    const job = await createJob("delete_module_code", {
      project: selectedProject.name,
      db: deleteCodeUninstallFirst ? selectedDb : "",
      modules: pendingDeleteCodeModules.join(","),
      uninstall_first: deleteCodeUninstallFirst,
    });
    if (job) {
      setDeleteCodeDialogOpen(false);
      setPendingDeleteCodeModules([]);
      setSelectedModules(new Set());
      schedule(refreshModules, 2500);
    }
  }

  async function importZip() {
    if (!selectedProject) return;
    const file = zipInputRef.current?.files?.[0];
    if (!file) {
      pushToast("error", "Sélectionne un fichier ZIP.");
      return;
    }
    const selected = Array.from(selectedZipModules).sort();
    if (!selected.length) {
      pushToast("error", "Sélectionne au moins un module à importer.");
      return;
    }
    const form = new FormData();
    form.append("zip", file);
    form.append("replace_existing", replaceZipModules ? "1" : "0");
    form.append("modules", selected.join(","));
    setLoading(true);
    try {
      const result = await api<{ job: Job }>(`/api/projects/${encodeURIComponent(selectedProject.name)}/module-zip`, {
        method: "POST",
        body: form,
      });
      setSelectedJobId(result.job.id);
      setExternalLogView(null);
      setZipDialogOpen(false);
      resetZipImport();
      pushToast("success", `Import de ${selected.length} module(s) lancé.`);
      schedule(refreshModules, 1800);
      await refreshJobs();
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Import ZIP impossible.");
    } finally {
      setLoading(false);
    }
  }

  async function restoreDatabaseBackup(
    payload: RestoreDatabasePayload,
    onProgress: (progress: number) => void,
  ) {
    setLoading(true);
    void requestTaskNotificationPermission().catch(() => {
      // The in-app completion toast remains available if system notifications are refused.
    });
    try {
      const result = await uploadDatabaseBackup(payload, onProgress);
      setSelectedJobId(result.job.id);
      jobStatuses.current.set(result.job.id, result.job.status);
      setExternalLogView(null);
      enableLogAutoFollow();
      setRestoreDbOpen(false);
      pushToast("success", `Restauration lancée : ${payload.db}`);
      await refreshJobs();
      schedule(refreshOverview, 2500);
      return true;
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Restauration impossible.");
      return false;
    } finally {
      setLoading(false);
    }
  }

  function resetZipImport() {
    zipInspectionGeneration.current += 1;
    setZipFile(null);
    setZipModuleCandidates([]);
    setSelectedZipModules(new Set());
    setInspectingZip(false);
    if (zipInputRef.current) zipInputRef.current.value = "";
  }

  async function inspectZipFile(file?: File) {
    const generation = ++zipInspectionGeneration.current;
    setZipFile(file || null);
    setZipModuleCandidates([]);
    setSelectedZipModules(new Set());
    if (!file || !selectedProject) {
      setInspectingZip(false);
      return;
    }

    const form = new FormData();
    form.append("zip", file);
    setInspectingZip(true);
    try {
      const result = await api<ZipInspection>(
        `/api/projects/${encodeURIComponent(selectedProject.name)}/module-zip/inspect`,
        { method: "POST", body: form },
      );
      if (generation !== zipInspectionGeneration.current) return;
      setZipModuleCandidates(result.modules);
      setSelectedZipModules(new Set(result.modules));
      if (!result.modules.length) {
        pushToast("error", "Aucun module Odoo détecté dans cette archive.");
      } else if (result.ignored_symlinks) {
        pushToast(
          "info",
          `${result.modules.length} module(s) détecté(s). ${result.ignored_symlinks} lien(s) de packaging ignoré(s).`,
        );
      }
    } catch (err) {
      if (generation !== zipInspectionGeneration.current) return;
      pushToast("error", err instanceof Error ? err.message : "Analyse du ZIP impossible.");
    } finally {
      if (generation === zipInspectionGeneration.current) setInspectingZip(false);
    }
  }

  function toggleZipModule(moduleName: string, checked: boolean) {
    setSelectedZipModules((current) => {
      const next = new Set(current);
      if (checked) next.add(moduleName);
      else next.delete(moduleName);
      return next;
    });
  }

  function toggleAllZipModules(checked: boolean) {
    setSelectedZipModules(checked ? new Set(zipModuleCandidates) : new Set());
  }

  const selectedModuleList = useMemo(() => Array.from(selectedModules), [selectedModules]);
  const selectedInstalledModuleList = useMemo(
    () => selectedModuleList.filter((name) => moduleByName.get(name)?.state === "installed"),
    [moduleByName, selectedModuleList],
  );
  const selectedInstallableModuleList = useMemo(
    () => selectedModuleList.filter((name) => moduleByName.get(name)?.state !== "installed"),
    [moduleByName, selectedModuleList],
  );
  const selectedRemovableModuleList = useMemo(
    () =>
      selectedModuleList.filter((name) => {
        const module = moduleByName.get(name);
        return module?.removable && module.removal_mode !== "link_only";
      }),
    [moduleByName, selectedModuleList],
  );
  const selectedProjectReady = Boolean(selectedProject);
  const selectedProjectOnline = selectedProject?.odoo_status === "running";
  const selectedProjectHasContainers = Boolean(
    selectedProject &&
    [selectedProject.odoo_status, selectedProject.postgres_status].some((status) => status && status !== "absent" && status !== "docker off"),
  );
  const selectedProjectLifecycleJob = useMemo(
    () =>
      jobs.find(
        (job) =>
          job.status === "running" &&
          selectedProject &&
          (job.title === `Démarrer ${selectedProject.name}` || job.title === `Arrêter ${selectedProject.name}`),
      ),
    [jobs, selectedProject],
  );
  const selectedProjectStarting = selectedProjectLifecycleJob?.title.startsWith("Démarrer ") ?? false;
  const selectedProjectStopping = selectedProjectLifecycleJob?.title.startsWith("Arrêter ") ?? false;
  const canUseDb = Boolean(selectedDb && odooDatabases.includes(selectedDb));
  const selectedOdooUrl = odooAccessUrl(selectedProject, selectedDb);
  const scopedExternalLogView = externalLogView?.project === selectedProject?.name ? externalLogView : null;
  const outputTitle = scopedExternalLogView?.title || selectedJob?.title || "Aucune action sélectionnée";
  const outputContent = scopedExternalLogView?.content || selectedJob?.output || selectedJob?.lines?.join("\n") || "Aucune sortie.";
  const outputSource = scopedExternalLogView ? `external:${scopedExternalLogView.title}` : `job:${selectedJob?.id || "none"}`;
  const outputTitleIsLong = outputTitle.length > LOG_DESCRIPTION_MAX_LENGTH;
  const displayedOutputTitle = outputTitleIsLong && !logDescriptionExpanded
    ? `${outputTitle.slice(0, LOG_DESCRIPTION_MAX_LENGTH).trimEnd()}…`
    : outputTitle;

  useEffect(() => {
    setLogDescriptionExpanded(false);
  }, [outputSource]);

  useEffect(() => {
    if (activeTab !== "logs") return;
    if (lastLogOutputSource.current !== outputSource) {
      lastLogOutputSource.current = outputSource;
      logAutoFollow.current = true;
    }
    if (logAutoFollow.current) scrollLogOutputToBottom();
  }, [activeTab, outputContent, outputSource, scrollLogOutputToBottom]);

  useEffect(() => {
    if (!selectedProjectOnline && (activeTab === "bases" || activeTab === "modules")) {
      setActiveTab("logs");
    }
  }, [activeTab, selectedProjectOnline]);

  useEffect(() => {
    setModulePage(1);
  }, [deferredModuleSearch, moduleFilter, moduleOriginFilter, selectedDb, selectedProject?.name, modulesPerPage]);

  useEffect(() => {
    if (modulePage > modulePageCount) setModulePage(modulePageCount);
  }, [modulePage, modulePageCount]);

  const toggleModuleSelection = useCallback((name: string, checked: boolean) => {
    setSelectedModules((current) => {
      const next = new Set(current);
      if (checked) next.add(name);
      else next.delete(name);
      return next;
    });
  }, []);

  const toggleFilteredModules = useCallback(
    (checked: boolean) => {
      setSelectedModules((current) => {
        const next = new Set(current);
        for (const name of filteredModuleNames) {
          if (checked) next.add(name);
          else next.delete(name);
        }
        return next;
      });
    },
    [filteredModuleNames],
  );

  async function requestStartProject() {
    if (!selectedProject) return;
    const job = await createJob("start_project", { project: selectedProject.name });
    if (job) {
      schedule(refreshOverview, 1800);
      schedule(refreshSystemStatus, 2200);
    }
  }

  async function requestStopProject() {
    if (!selectedProject) return;
    const job = await createJob("stop_project", { project: selectedProject.name });
    if (job) {
      schedule(refreshOverview, 1200);
      schedule(refreshSystemStatus, 1600);
    }
  }

  async function requestOpenOdoo() {
    const startBeforeOpen = settings?.start_project_before_open ?? false;
    if (!selectedProject || openingOdoo || (startBeforeOpen && selectedProjectLifecycleJob)) return;
    setOpeningOdoo(true);
    try {
      if (startBeforeOpen) {
        const job = await createJob("start_project", { project: selectedProject.name });
        if (!job) return;
        await waitForJob(job.id);
        await refreshOverview();
      }
      const opened = await openExternalUrl(selectedOdooUrl);
      if (!opened) throw new Error("Lien impossible à ouvrir depuis l'application.");
      pushToast(
        "success",
        startBeforeOpen
          ? "Odoo est prêt et a été ouvert dans le navigateur."
          : "La base Odoo a été ouverte dans le navigateur.",
      );
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Impossible d'ouvrir Odoo.");
    } finally {
      setOpeningOdoo(false);
    }
  }

  if (initializing) {
    return (
      <main className="sdk-shell grid min-h-screen place-items-center bg-background px-6">
        <div className="w-full max-w-md rounded-lg border bg-card p-6 text-center shadow-sm">
          <img
            src={selectedAppIcon.src}
            alt="SDK Local Manager"
            className={cn(
              "mx-auto h-16 w-16 object-cover",
              settings?.interface_icon === "local" ? "rounded-full" : "rounded-[15px]",
            )}
          />
          <div className="mt-3 flex justify-center">
            {initializationError ? (
              <AlertTriangle className="h-6 w-6 text-amber-600" />
            ) : (
              <Loader2 className="h-6 w-6 animate-spin text-primary" />
            )}
          </div>
          <h1 className="mt-4 text-lg font-semibold">Chargement du gestionnaire</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            {initializationMessage}
          </p>
          {initializationError && (
            <div className="mt-4 space-y-3">
              <p className="break-words rounded-md border border-amber-200 bg-amber-50 p-3 text-left text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">
                {initializationError}
              </p>
              {backendDiagnostics && (
                <details className="rounded-md border bg-muted/40 p-3 text-left text-xs">
                  <summary className="cursor-pointer font-medium">Détails techniques</summary>
                  <div className="mt-2 break-all text-muted-foreground">Journal : {backendDiagnostics.log_path}</div>
                  <pre className="log-terminal mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-950 p-2 text-[11px] text-slate-100">
                    {backendDiagnostics.details}
                  </pre>
                </details>
              )}
              <Button className="w-full" onClick={initializeApplication}>
                <RefreshCcw className="h-4 w-4" />
                Réessayer
              </Button>
            </div>
          )}
        </div>
      </main>
    );
  }

  return (
    <main className="sdk-shell min-h-screen overflow-x-clip">
      <div className="flex min-h-screen min-w-0 flex-col lg:flex-row">
        <aside className="min-w-0 border-b bg-card lg:sticky lg:top-0 lg:h-screen lg:w-80 lg:flex-none lg:border-b-0 lg:border-r">
          <div className="flex h-full flex-col">
            <div className="sdk-brand border-b p-4">
              <div className="flex items-center justify-between gap-3">
                <div className="flex min-w-0 items-center gap-2.5">
                  <img
                    src={selectedAppIcon.src}
                    alt=""
                    aria-hidden="true"
                    className={cn(
                      "sdk-logo h-10 w-10 shrink-0 object-contain",
                      settings?.interface_icon === "local" ? "rounded-full" : "rounded-[9px]",
                    )}
                  />
                  <div className="min-w-0"><p className="sdk-eyebrow">Sudokeys</p><h1 className="sdk-brand-name text-sm font-extrabold leading-tight">SDK Local Manager</h1></div>
                </div>
                <ThemeToggle />
              </div>
              <div className="mt-1 flex min-w-0 items-center gap-2">
                <p className="min-w-0 flex-1 truncate text-xs text-muted-foreground" title={overview?.workspace || "Workspace local"}>
                  {overview?.workspace || "Workspace local"}
                </p>
                <Badge className="shrink-0" variant={(systemStatus?.docker.running ?? overview?.docker_ok) ? "success" : "destructive"}>
                  {(systemStatus?.docker.running ?? overview?.docker_ok) ? "Docker" : "Docker off"}
                </Badge>
              </div>
              <div className="relative mt-4">
                <Search className="absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                <Input
                  className="pl-9"
                  placeholder="Rechercher un projet"
                  value={projectsFilter}
                  onChange={(event) => setProjectsFilter(event.target.value)}
                />
              </div>
            </div>
            <div className="min-h-0 max-h-[260px] flex-1 overflow-auto px-2 py-1 sm:max-h-[340px] lg:max-h-none">
              {filteredProjects.map((project) => {
                const running = project.odoo_status === "running";
                const absent = project.odoo_status === "absent" || project.odoo_status === "docker off";
                const lifecycleJob =
                  projectLifecycleJobs.get(`Démarrer ${project.name}`) ||
                  projectLifecycleJobs.get(`Arrêter ${project.name}`);
                const switchingOn = lifecycleJob?.title.startsWith("Démarrer ") ?? false;
                const displayedRunning = running || switchingOn;

                return (
                  <div
                    key={project.name}
                    className={cn(
                      "border-b border-border/70 transition-[background-color,border-color] duration-150 hover:border-primary/35 hover:bg-muted/70 dark:hover:bg-muted/60 last:border-b-0",
                      selectedProject?.name === project.name && "bg-primary/[0.07] hover:bg-primary/[0.10] dark:bg-primary/[0.12] dark:hover:bg-primary/[0.16]",
                      absent && "bg-muted/35 text-muted-foreground",
                    )}
                  >
                    <div className="flex min-h-16 items-center gap-2 px-2">
                      <button
                        type="button"
                        className="min-w-0 flex-1 py-2 text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                        onClick={() => {
                          setSelectedProjectName(project.name);
                          setSelectedDb(firstOdooDatabase(project));
                          setExternalLogView(null);
                          setActiveTab(project.odoo_status === "running" ? "bases" : "logs");
                        }}
                      >
                        <span className={cn("block truncate text-sm font-semibold", absent ? "text-muted-foreground" : "text-foreground")}>
                          {project.name}
                        </span>
                        <span className="mt-0.5 block text-xs text-muted-foreground">
                          {project.odoo_version ? `Odoo ${project.odoo_version}` : "Version inconnue"}
                        </span>
                      </button>
                      <div className="flex w-[74px] shrink-0 items-center justify-end gap-2">
                        {lifecycleJob ? (
                          <Loader2 className="h-4 w-4 animate-spin text-primary" aria-label="Changement d’état en cours" />
                        ) : (
                          <span
                            className={cn("inline-flex h-6 w-6 shrink-0 items-center justify-center", displayedRunning ? "text-emerald-500" : "text-red-500")}
                            role="img"
                            aria-label={`${project.name} : ${displayedRunning ? "allumé" : "éteint"}`}
                          >
                            <Circle className="h-5 w-5 fill-current" aria-hidden="true" />
                          </span>
                        )}
                        <span className={cn("w-7 text-xs font-semibold", displayedRunning ? "text-emerald-600 dark:text-emerald-400" : "text-muted-foreground")}>
                          {displayedRunning ? "ON" : "OFF"}
                        </span>
                      </div>
                    </div>
                    {settings?.show_technical_details && (
                      <div className="flex flex-wrap items-center gap-1.5 px-2 pb-2 text-xs">
                        <Badge variant={statusVariant(project.odoo_status)}>Odoo {project.odoo_status}</Badge>
                        <Badge variant={statusVariant(project.postgres_status)}>PostgreSQL {project.postgres_status}</Badge>
                        <Badge variant="outline">
                          {project.databases?.filter((db) => db !== "postgres").length || 0} base(s)
                        </Badge>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
            <div className="grid grid-cols-2 gap-2 border-t p-3 lg:grid-cols-1">
              <Button className="col-span-2 w-full lg:col-span-1" onClick={openCreateProjectDialog}>
                <FolderPlus className="h-4 w-4" />
                Nouveau projet
              </Button>
              <Button
                className="w-full"
                variant="outline"
                onClick={openSettingsDialog}
              >
                <Settings className="h-4 w-4" />
                Paramètres
              </Button>
              <Button
                className="w-full"
                variant="secondary"
                onClick={() => setAboutOpen(true)}
              >
                <Info className="h-4 w-4" />
                À propos
              </Button>
            </div>
          </div>
        </aside>

        <section className="min-w-0 flex-1">
          <header className="sdk-project-header border-b bg-card">
            <div className="mx-auto flex max-w-[1500px] flex-col gap-4 px-4 py-4 xl:flex-row xl:items-start xl:justify-between">
              <div className="min-w-0 flex-1">
                <div className="flex min-w-0 flex-wrap items-start gap-2">
                  <h2 className="min-w-0 max-w-full break-words text-2xl font-semibold leading-tight sm:text-3xl">
                    {selectedProject?.name || "Aucun projet"}
                  </h2>
                  {selectedProject?.odoo_version && (
                    <Badge className="mt-0.5 shrink-0" variant="outline">
                      Odoo {selectedProject.odoo_version}
                    </Badge>
                  )}
                </div>
                <p className="mt-1 max-w-full break-all text-sm text-muted-foreground">
                  {selectedProject?.url || "Sélectionne un projet."}
                </p>
              </div>
              <div className="grid w-full shrink-0 grid-cols-2 items-stretch gap-2 sm:grid-cols-3 xl:w-[480px]">
                <Button className="w-full" variant="outline" onClick={refreshAllViews}>
                  <RefreshCcw className="h-4 w-4" />
                  Actualiser
                </Button>
                {selectedProjectOnline ? (
                  <Button
                    key="stop-project"
                    className="w-full"
                    variant="destructive"
                    disabled={!selectedProjectReady || !selectedProjectHasContainers || loading || Boolean(selectedProjectLifecycleJob)}
                    onClick={requestStopProject}
                  >
                    {selectedProjectStopping ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
                    {selectedProjectStopping ? "Arrêt…" : "Arrêter"}
                  </Button>
                ) : (
                  <Button
                    key="start-project"
                    className="w-full"
                    disabled={!selectedProjectReady || loading || Boolean(selectedProjectLifecycleJob)}
                    onClick={requestStartProject}
                  >
                    {selectedProjectStarting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                    {selectedProjectStarting ? "Démarrage…" : "Démarrer"}
                  </Button>
                )}
                {selectedProject && (
                  <Button
                    className="col-span-2 w-full sm:col-span-1"
                    variant="outline"
                    disabled={
                      !selectedProjectReady ||
                      openingOdoo ||
                      Boolean(settings?.start_project_before_open && selectedProjectLifecycleJob)
                    }
                    onClick={requestOpenOdoo}
                  >
                    {openingOdoo ? <Loader2 className="h-4 w-4 animate-spin" /> : <ExternalLink className="h-4 w-4" />}
                    {openingOdoo
                      ? settings?.start_project_before_open
                        ? "Préparation d’Odoo…"
                        : "Ouverture…"
                      : "Ouvrir Odoo"}
                  </Button>
                )}
              </div>
            </div>
          </header>

          <div className="mx-auto max-w-[1500px] px-4 py-4">
            {apiUnavailable && (
              <div className="mb-4 flex flex-col gap-3 border-y border-red-300 bg-red-50 px-4 py-3 text-sm text-red-950 dark:border-red-800 dark:bg-red-950/45 dark:text-red-100 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex min-w-0 items-start gap-3">
                  <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-red-600" />
                  <div className="min-w-0">
                    <div className="font-semibold">Service local indisponible</div>
                    <div className="mt-0.5 break-words text-red-800 dark:text-red-200">
                      L'application n'arrive pas à joindre son API locale. Attends quelques secondes puis actualise. Si Docker n'est pas encore installé,
                      installe Docker Desktop avant de lancer les projets Odoo.
                    </div>
                    <div className="mt-3 rounded-md border border-red-200 bg-white/70 p-3 dark:border-red-800 dark:bg-red-950/55">
                      <div className="font-medium">{fallbackDockerGuide.title}</div>
                      <ol className="mt-2 list-decimal space-y-1 pl-4 text-xs leading-5 text-red-900 dark:text-red-100">
                        {fallbackDockerGuide.steps.map((step) => (
                          <li key={step}>{step}</li>
                        ))}
                      </ol>
                    </div>
                  </div>
                </div>
                <div className="flex shrink-0 flex-col gap-2 sm:flex-row">
                  <Button className="w-full sm:w-auto" size="sm" onClick={() => Promise.all([refreshOverview(), refreshSystemStatus(), loadSettings()])}>
                    <RefreshCcw className="h-4 w-4" />
                    Réessayer
                  </Button>
                  <Button className="w-full sm:w-auto" size="sm" variant="outline" onClick={() => openUrl(fallbackDockerGuide.download_url)}>
                    <CloudDownload className="h-4 w-4" />
                    Télécharger Docker
                  </Button>
                  <Button className="w-full sm:w-auto" size="sm" variant="outline" onClick={() => openUrl(fallbackDockerGuide.install_url)}>
                    <ExternalLink className="h-4 w-4" />
                    Guide Docker
                  </Button>
                  <Button className="w-full sm:w-auto" size="sm" variant="outline" disabled={!desktopRuntime} onClick={requestDockerStart}>
                    <Play className="h-4 w-4" />
                    Ouvrir Docker
                  </Button>
                </div>
              </div>
            )}
            {systemStatus && !systemStatus.docker.running && (
              <div className="mb-4 flex flex-col gap-3 border-y border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950/45 dark:text-amber-100 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex min-w-0 items-start gap-3">
                  <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
                  <div className="min-w-0">
                    <div className="font-semibold">Docker n’est pas disponible</div>
                    <div className="mt-0.5 break-words text-amber-800 dark:text-amber-200">{systemStatus.docker.message}</div>
                    {systemStatus.docker.state === "missing" && systemStatus.docker.install_guide && (
                      <div className="mt-3 rounded-md border border-amber-200 bg-white/70 p-3 dark:border-amber-800 dark:bg-amber-950/55">
                        <div className="font-medium">{systemStatus.docker.install_guide.title}</div>
                        <ol className="mt-2 list-decimal space-y-1 pl-4 text-xs leading-5 text-amber-900 dark:text-amber-100">
                          {systemStatus.docker.install_guide.steps.map((step) => (
                            <li key={step}>{step}</li>
                          ))}
                        </ol>
                      </div>
                    )}
                  </div>
                </div>
                <div className="flex shrink-0 flex-col gap-2 sm:flex-row">
                  {systemStatus.docker.state === "missing" && systemStatus.docker.install_guide?.download_url && (
                    <Button className="w-full sm:w-auto" size="sm" onClick={() => openUrl(systemStatus.docker.install_guide?.download_url)}>
                      <CloudDownload className="h-4 w-4" />
                      Télécharger Docker
                    </Button>
                  )}
                  {systemStatus.docker.can_start && (
                    <Button className="w-full sm:w-auto" size="sm" disabled={loading} onClick={requestDockerStart}>
                      {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                      Ouvrir Docker
                    </Button>
                  )}
                  {systemStatus.docker.install_guide?.install_url && (
                    <Button className="w-full sm:w-auto" size="sm" variant="outline" onClick={() => openUrl(systemStatus.docker.install_guide?.install_url)}>
                      <ExternalLink className="h-4 w-4" />
                      Guide Docker
                    </Button>
                  )}
                  <Button
                    className="w-full sm:w-auto"
                    size="sm"
                    variant="outline"
                    onClick={openSettingsDialog}
                  >
                    <Settings className="h-4 w-4" />
                    Paramètres
                  </Button>
                </div>
              </div>
            )}
            {systemStatus?.traefik && !systemStatus.traefik.running && (
              <div className="mb-4 flex flex-col gap-3 border-y border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-950 dark:border-sky-800 dark:bg-sky-950/45 dark:text-sky-100 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex min-w-0 items-start gap-3">
                  <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-sky-700" />
                  <div className="min-w-0">
                    <div className="font-semibold">Traefik n'est pas prêt</div>
                    <div className="mt-0.5 break-words text-sky-800 dark:text-sky-200">
                      {systemStatus.traefik.message}
                      {systemStatus.traefik.requires_docker ? " Docker doit être installé et démarré avant cette étape." : ""}
                    </div>
                    <div className="mt-1 break-all text-xs text-sky-700 dark:text-sky-300">Dossier attendu : {systemStatus.traefik.path}</div>
                  </div>
                </div>
                <div className="flex shrink-0 flex-col gap-2 sm:flex-row">
                  <Button
                    className="w-full sm:w-auto"
                    size="sm"
                    disabled={!systemStatus.docker.running || loading}
                    onClick={requestTraefikInstall}
                  >
                    {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                    {systemStatus.traefik.installed ? "Démarrer Traefik" : "Installer Traefik"}
                  </Button>
                  <Button className="w-full sm:w-auto" size="sm" variant="outline" onClick={openSettingsDialog}>
                    <Settings className="h-4 w-4" />
                    Paramètres
                  </Button>
                </div>
              </div>
            )}
            {error && (
              <div className="mb-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/45 dark:text-red-200">
                <AlertTriangle className="mr-2 inline h-4 w-4" />
                {error}
              </div>
            )}

            <Tabs
              value={activeTab}
              onValueChange={(value) => {
                if (value === "logs") enableLogAutoFollow();
                setActiveTab(value);
              }}
            >
              <TabsList
                className={cn(
                  "grid w-full overflow-hidden transition-[grid-template-columns] duration-200 ease-out motion-reduce:transition-none lg:w-fit",
                  selectedProjectOnline
                    ? "grid-cols-4"
                    : "[grid-template-columns:minmax(0,0fr)_minmax(0,0fr)_minmax(0,1fr)_minmax(0,1fr)]",
                )}
              >
                <TabsTrigger
                  value="bases"
                  disabled={!selectedProjectOnline}
                  className={cn(
                    "transition-[opacity,transform] duration-200 ease-out motion-reduce:transition-none",
                    !selectedProjectOnline && "pointer-events-none -translate-x-1 opacity-0",
                  )}
                >
                  <Database className="mr-1.5 h-4 w-4" />
                  Bases
                </TabsTrigger>
                <TabsTrigger
                  value="modules"
                  disabled={!selectedProjectOnline}
                  className={cn(
                    "transition-[opacity,transform] duration-200 ease-out motion-reduce:transition-none",
                    !selectedProjectOnline && "pointer-events-none -translate-x-1 opacity-0",
                  )}
                >
                  <Boxes className="mr-1.5 h-4 w-4" />
                  Modules
                </TabsTrigger>
                <TabsTrigger value="logs">
                  <Logs className="mr-1.5 h-4 w-4" />
                  Logs
                </TabsTrigger>
                <TabsTrigger value="actions">
                  <Settings className="mr-1.5 h-4 w-4" />
                  Actions
                </TabsTrigger>
              </TabsList>

              {selectedProjectOnline && (
                <TabsContent value="bases">
                  <div className={cn("grid min-w-0 gap-4", !compactBases && "xl:grid-cols-[minmax(0,1fr)_400px]")}>
                    <Card>
                      <CardHeader>
                        <CardTitle>Bases Odoo</CardTitle>
                        <CardDescription>Sélectionne l’environnement Odoo utilisé pour les modules et les actions.</CardDescription>
                      </CardHeader>
                      <CardContent>
                        {odooDatabases.length ? (
                          <div className="grid gap-3 sm:grid-cols-2 2xl:grid-cols-3">
                            {odooDatabases.map((db) => (
                              <InteractiveCard
                                key={db}
                                className={cn(
                                  "min-w-0 p-4",
                                  selectedDb === db && "border-primary bg-primary/[0.08] ring-1 ring-primary/25 dark:bg-primary/[0.14]",
                                )}
                                onClick={() => setSelectedDb(db)}
                              >
                                <div className="flex min-w-0 items-start justify-between gap-2">
                                  <span className="min-w-0 break-words font-medium">{db}</span>
                                  {db === selectedDb && <CheckCircle2 className="h-4 w-4 shrink-0 text-primary" />}
                                </div>
                                <div className="mt-2 text-sm text-muted-foreground">
                                  {selectedProject?.database_versions?.[db] || "Base Odoo"}
                                </div>
                              </InteractiveCard>
                            ))}
                          </div>
                        ) : (
                          <div className="rounded-md border border-dashed p-6 text-center">
                            <Database className="mx-auto h-6 w-6 text-muted-foreground" />
                            <p className="mt-3 font-medium">Aucune base Odoo</p>
                            <p className="mt-1 text-sm text-muted-foreground">Crée une base pour commencer à utiliser ce projet.</p>
                          </div>
                        )}
                      </CardContent>
                    </Card>
                    <div className={cn("grid min-w-0 content-start gap-4", compactBases && "items-start md:grid-cols-2 2xl:grid-cols-3")}>
                      <Card>
                        <CardHeader>
                          <CardTitle>Créer une base Odoo</CardTitle>
                          <CardDescription>Ajoute une nouvelle base métier au projet sélectionné.</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-3">
                          <Button className="w-full" disabled={!selectedProjectReady} onClick={() => setCreateDbOpen(true)}>
                            <PlusCircle className="h-4 w-4" />
                            Créer une base Odoo
                          </Button>
                          <Button
                            className="w-full"
                            variant="outline"
                            disabled={!selectedProjectReady}
                            onClick={() => setRestoreDbOpen(true)}
                          >
                            <Upload className="h-4 w-4" />
                            Restaurer une sauvegarde ZIP
                          </Button>
                          {selectedProject && (
                            <Button className="w-full" variant="ghost" onClick={() => openUrl(selectedProject.database_manager_url)}>
                              <ExternalLink className="h-4 w-4" />
                              Gestionnaire de bases Odoo
                            </Button>
                          )}
                        </CardContent>
                      </Card>

                      <Card>
                        <CardHeader>
                          <CardTitle>Neutraliser la base</CardTitle>
                          <CardDescription>
                            Coupe les crons métier et les serveurs de messagerie, puis vérifie le résultat.
                          </CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-3">
                          <div className="min-w-0 rounded-md bg-muted/55 p-3 text-sm">
                            <div className="text-muted-foreground">Base ciblée</div>
                            <div className="mt-1 break-words font-medium">{selectedDb || "Aucune base sélectionnée"}</div>
                          </div>
                          <Button
                            className="w-full"
                            variant="outline"
                            disabled={!canUseDb || loading}
                            onClick={() => setNeutralizeDbOpen(true)}
                          >
                            <ShieldCheck className="h-4 w-4" />
                            Neutraliser et contrôler
                          </Button>
                        </CardContent>
                      </Card>

                      <Card className={cn(compactBases && "md:col-span-2 2xl:col-span-1")}>
                        <CardHeader>
                          <div className="flex items-start justify-between gap-3">
                            <div className="min-w-0">
                              <CardTitle>Serveur PostgreSQL</CardTitle>
                              <CardDescription className="mt-1">
                                Service technique qui stocke les bases Odoo. Il ne se sélectionne pas comme une base métier.
                              </CardDescription>
                            </div>
                            <Badge variant={statusVariant(selectedProject?.postgres_status || "absent")} className="shrink-0">
                              {selectedProject?.postgres_status || "absent"}
                            </Badge>
                          </div>
                        </CardHeader>
                        <CardContent className="space-y-4">
                          <div className="min-w-0 rounded-md bg-muted/55 p-3 text-sm">
                            <div className="text-muted-foreground">Conteneur</div>
                            <div className="mt-1 break-all font-medium">{selectedProject ? `postgresql-${selectedProject.name}` : "-"}</div>
                            <div className="mt-3 text-muted-foreground">Base Odoo ciblée</div>
                            <div className="mt-1 break-words font-medium">{selectedDb || "Aucune base sélectionnée"}</div>
                          </div>
                          <Button
                            className="w-full"
                            variant="outline"
                            disabled={!canUseDb || selectedProject?.postgres_status !== "running" || openingPostgresql}
                            onClick={openPostgresqlConsole}
                          >
                            {openingPostgresql ? <Loader2 className="h-4 w-4 animate-spin" /> : <Terminal className="h-4 w-4" />}
                            Ouvrir psql
                          </Button>
                          <p className="text-xs text-muted-foreground">La console s’ouvre dans le terminal du système avec la base Odoo sélectionnée.</p>
                        </CardContent>
                      </Card>
                    </div>
                  </div>
                </TabsContent>
              )}

              {selectedProjectOnline && (
                <TabsContent value="modules">
                  <Card>
                    <CardHeader className="gap-3 sm:flex-row sm:items-start sm:justify-between">
                      <div className="min-w-0">
                        <div className="flex items-center gap-2">
                          <CardTitle>Modules</CardTitle>
                          <Button
                            type="button"
                            size="icon"
                            variant="ghost"
                            onClick={() => void refreshModules()}
                            disabled={loadingModules}
                            aria-label="Actualiser la liste des modules"
                            title="Actualiser la liste des modules"
                          >
                            <RefreshCcw className={cn("h-4 w-4", loadingModules && "animate-spin")} />
                          </Button>
                        </div>
                        <CardDescription>Recherche, sélection et mise à jour des modules de la base Odoo choisie.</CardDescription>
                      </div>
                      <div className="grid w-full gap-2 sm:w-auto sm:grid-cols-2">
                        <Button
                          className="w-full"
                          variant="outline"
                          onClick={openSocleDialog}
                          disabled={!selectedProjectReady || loading}
                        >
                          <Boxes className="h-4 w-4" />
                          Installer un socle
                        </Button>
                        <Button
                          className="w-full"
                          disabled={!selectedProjectReady || loading || checkingUpdatePrerequisites}
                          onClick={requestUpdateAllOdooModules}
                        >
                          {checkingUpdatePrerequisites ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCcw className="h-4 w-4" />}
                          MAJ complète Odoo
                        </Button>
                        <Button className="w-full" variant="outline" onClick={() => setRepositoryOpen(true)} disabled={!selectedProjectReady || loading}>
                          <CloudDownload className="h-4 w-4" />
                          Dépôt SSH · Ajout / MAJ
                        </Button>
                        <Button className="w-full" variant="outline" onClick={() => setZipDialogOpen(true)} disabled={!selectedProjectReady}>
                          <FileArchive className="h-4 w-4" />
                          Ajouter un pauvre zip
                        </Button>
                      </div>
                    </CardHeader>
                    <CardContent>
                      <div className="mb-4 grid min-w-0 gap-3 md:grid-cols-2 xl:grid-cols-[minmax(0,1fr)_180px_210px_220px]">
                        <div className="relative">
                          <Search className="absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                          <Input className="pl-9" placeholder="Rechercher par nom de module" value={moduleSearch} onChange={(event) => setModuleSearch(event.target.value)} />
                        </div>
                        <Select value={moduleFilter} onValueChange={setModuleFilter}>
                          <SelectTrigger placeholder="État">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="all">Tous les états</SelectItem>
                            <SelectItem value="installed">Installés</SelectItem>
                            <SelectItem value="uninstalled">Disponibles</SelectItem>
                            <SelectItem value="to upgrade">À mettre à jour</SelectItem>
                          </SelectContent>
                        </Select>
                        <Select value={moduleOriginFilter} onValueChange={setModuleOriginFilter}>
                          <SelectTrigger placeholder="Origine">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            <SelectItem value="all">Toutes les origines</SelectItem>
                            <SelectItem value="enterprise">Odoo Enterprise</SelectItem>
                            <SelectItem value="other">Autre</SelectItem>
                          </SelectContent>
                        </Select>
                        <Select value={selectedDb} onValueChange={setSelectedDb}>
                          <SelectTrigger placeholder="Base Odoo">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent>
                            {odooDatabases.map((db) => (
                              <SelectItem key={db} value={db}>
                                {db}
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                      </div>
                      <div className="mb-3 flex flex-col gap-3 rounded-md border bg-muted/45 p-3 text-sm sm:flex-row sm:items-center sm:justify-between">
                        <label className="flex min-w-0 cursor-pointer items-start gap-3">
                          <Checkbox
                            className="mt-0.5"
                            checked={someFilteredModulesSelected ? "indeterminate" : allFilteredModulesSelected}
                            disabled={!filteredModuleNames.length}
                            onCheckedChange={(checked) => toggleFilteredModules(checked === true)}
                          />
                          <span className="min-w-0">
                            <span className="block font-medium">Sélectionner les {filteredModuleNames.length} résultats</span>
                            <span className="block text-xs text-muted-foreground">
                              La sélection s’applique à toutes les pages de la recherche courante.
                            </span>
                          </span>
                        </label>
                        <div className="flex shrink-0 items-center gap-2">
                          <Badge className="w-fit" variant="outline">
                            {selectedModuleList.length} sélectionné(s)
                          </Badge>
                          {selectedModuleList.length > 0 && (
                            <Button size="sm" variant="ghost" onClick={() => setSelectedModules(new Set())}>
                              Effacer
                            </Button>
                          )}
                        </div>
                      </div>
                      {selectedModuleList.length > 0 && (
                        <div className="mb-3 rounded-md border border-primary/25 bg-primary/[0.06] p-3 dark:bg-primary/[0.12]">
                          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                            <div>
                              <div className="text-sm font-semibold">Actions sur la sélection</div>
                              <div className="mt-0.5 text-xs text-muted-foreground">
                                {selectedInstallableModuleList.length} disponible(s), {selectedInstalledModuleList.length} installé(s)
                              </div>
                            </div>
                            <Badge variant="default">{selectedModuleList.length} module(s)</Badge>
                          </div>
                          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 xl:grid-cols-4">
                            <Button
                              variant="secondary"
                              disabled={!selectedInstallableModuleList.length || !canUseDb || loading}
                              onClick={() => createJob("install_module", { project: selectedProject?.name, db: selectedDb, modules: selectedInstallableModuleList.join(",") })}
                            >
                              <PlusCircle className="h-4 w-4" />
                              Installer ({selectedInstallableModuleList.length})
                            </Button>
                            <Button
                              disabled={!selectedInstalledModuleList.length || !canUseDb || loading}
                              onClick={() => createJob("update_module", { project: selectedProject?.name, db: selectedDb, modules: selectedInstalledModuleList.join(",") })}
                            >
                              <RefreshCcw className="h-4 w-4" />
                              Mettre à jour ({selectedInstalledModuleList.length})
                            </Button>
                            <Button
                              className="border-red-300 text-red-700 hover:border-red-400 hover:bg-red-50 hover:text-red-800 dark:border-red-800 dark:text-red-300 dark:hover:bg-red-950/60"
                              variant="outline"
                              disabled={!selectedInstalledModuleList.length || !canUseDb || loading}
                              onClick={() => requestUninstall(selectedInstalledModuleList)}
                            >
                              <PackageX className="h-4 w-4" />
                              Désinstaller ({selectedInstalledModuleList.length})
                            </Button>
                            <Button
                              variant="destructive"
                              disabled={!selectedRemovableModuleList.length || loading}
                              onClick={() => requestDeleteCode(selectedRemovableModuleList)}
                            >
                              <Trash2 className="h-4 w-4" />
                              Supprimer ({selectedRemovableModuleList.length})
                            </Button>
                          </div>
                        </div>
                      )}
                      <div className="overflow-hidden rounded-md border">
                        <div className={cn("hidden border-b bg-muted px-3 py-2 text-xs font-medium uppercase text-muted-foreground xl:grid xl:items-center xl:gap-3", moduleTableGridColumns)}>
                          <div>Module</div>
                          <div>État</div>
                          <div>Version</div>
                          <div>Origine</div>
                          {showModuleLocations && <div>Emplacements</div>}
                          <div className="text-right">Actions</div>
                        </div>
                        <div className={cn("min-w-0", !compactModules && "max-h-[min(62vh,720px)] overflow-y-auto")}>
                          {visibleModules.length ? (
                            visibleModules.map((module) => {
                              const sourcePath = module.source_path || module.path;
                              const linkPath = module.link_path || (module.path_kind?.startsWith("lien") ? module.path : "");
                              const displaySourcePath = compactWorkspacePath(sourcePath, overview?.workspace);
                              const displayLinkPath = compactWorkspacePath(linkPath, overview?.workspace);
                              const samePaths = Boolean(linkPath && sourcePath && linkPath === sourcePath);
                              const origin = normalizedModuleOrigin(module.origin, sourcePath);
                              return (
                                <div
                                  key={module.name}
                                  className={cn(
                                    "grid min-w-0 gap-3 border-t p-3 transition-colors first:border-t-0 hover:bg-muted/35 xl:items-center",
                                    moduleTableGridColumns,
                                    selectedModules.has(module.name) && "bg-primary/[0.06] dark:bg-primary/[0.12]",
                                  )}
                                >
                                  <label className="flex min-w-0 cursor-pointer items-start gap-3 rounded-sm focus-within:ring-2 focus-within:ring-ring">
                                    <Checkbox
                                      className="mt-1"
                                      aria-label={`Sélectionner ${module.name}`}
                                      checked={selectedModules.has(module.name)}
                                      onCheckedChange={(checked) => toggleModuleSelection(module.name, checked === true)}
                                    />
                                    <span className="min-w-0">
                                      <div className="break-words font-medium">{module.name}</div>
                                      <div className="mt-0.5 break-words text-xs text-muted-foreground">{module.title || module.name}</div>
                                    </span>
                                  </label>
                                  <div className="flex min-w-0 items-center justify-between gap-3 xl:block">
                                    <span className="text-xs font-medium text-muted-foreground xl:hidden">État</span>
                                    <Badge className="shrink-0" variant={module.state === "installed" ? "success" : "secondary"}>{module.state}</Badge>
                                  </div>
                                  <div className="flex min-w-0 items-start justify-between gap-3 text-sm xl:block">
                                    <span className="text-xs font-medium text-muted-foreground xl:hidden">Version</span>
                                    <span className="min-w-0 break-words">{module.installed_version || module.version || "-"}</span>
                                  </div>
                                  <div className="flex min-w-0 items-center justify-between gap-3 xl:block">
                                    <span className="text-xs font-medium text-muted-foreground xl:hidden">Origine</span>
                                    <Badge className="shrink-0" variant="outline">{moduleOriginLabel(origin)}</Badge>
                                  </div>
                                  {showModuleLocations && (
                                    <div className="min-w-0">
                                      <div className="mb-1 text-xs font-medium text-muted-foreground xl:hidden">Emplacements</div>
                                      <div className="space-y-1">
                                        {module.path_kind && (
                                          <Badge className="w-fit max-w-full truncate" variant="outline" title={module.path_kind}>
                                            {module.path_kind}
                                          </Badge>
                                        )}
                                        <div className="min-w-0 text-xs">
                                          <span className="font-medium text-teal-700 dark:text-teal-300">Source</span>
                                          <div className="truncate font-mono text-teal-800 dark:text-teal-200" title={sourcePath}>
                                            {displaySourcePath || "-"}
                                          </div>
                                        </div>
                                        {displayLinkPath && !samePaths && (
                                          <div className="min-w-0 text-xs">
                                            <span className="font-medium text-blue-700 dark:text-blue-300">Lien Odoo</span>
                                            <div className="truncate font-mono text-blue-800 dark:text-blue-200" title={linkPath}>
                                              {displayLinkPath}
                                            </div>
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  )}
                                  <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_40px] gap-2">
                                    {module.state === "installed" ? (
                                      <Button
                                        className="w-full"
                                        size="sm"
                                        disabled={!canUseDb}
                                        onClick={() => createJob("update_module", { project: selectedProject?.name, db: selectedDb, modules: module.name })}
                                      >
                                        <RefreshCcw className="h-4 w-4" />
                                        Mettre à jour
                                      </Button>
                                    ) : (
                                      <Button
                                        className="w-full"
                                        size="sm"
                                        variant="secondary"
                                        disabled={!canUseDb}
                                        onClick={() => createJob("install_module", { project: selectedProject?.name, db: selectedDb, modules: module.name })}
                                      >
                                        <PlusCircle className="h-4 w-4" />
                                        Installer
                                      </Button>
                                    )}
                                    <DropdownMenu.Root>
                                      <DropdownMenu.Trigger>
                                        <Button size="icon" variant="outline" title={`Autres actions pour ${module.name}`} aria-label={`Autres actions pour ${module.name}`}>
                                          <MoreHorizontal className="h-4 w-4" />
                                        </Button>
                                      </DropdownMenu.Trigger>
                                      <DropdownMenu.Content align="end" className="min-w-52">
                                        <DropdownMenu.Label>Actions sur {module.name}</DropdownMenu.Label>
                                        {module.state === "installed" && (
                                          <DropdownMenu.Item color="red" disabled={!canUseDb} onSelect={() => requestUninstall([module.name])}>
                                            <PackageX className="h-4 w-4" />
                                            Désinstaller de la base
                                          </DropdownMenu.Item>
                                        )}
                                        {module.removal_mode !== "link_only" && (
                                          <DropdownMenu.Item color="red" disabled={!module.removable} onSelect={() => requestDeleteCode([module.name])}>
                                            <Trash2 className="h-4 w-4" />
                                            Supprimer du projet
                                          </DropdownMenu.Item>
                                        )}
                                        {module.state !== "installed" && module.removal_mode === "link_only" && (
                                          <DropdownMenu.Item disabled>Module protégé</DropdownMenu.Item>
                                        )}
                                      </DropdownMenu.Content>
                                    </DropdownMenu.Root>
                                  </div>
                                </div>
                              );
                            })
                          ) : (
                            <div className="grid justify-items-center gap-3 p-6 text-center text-sm text-muted-foreground">
                              <span>
                                {loadingModules
                                  ? "Lecture des modules du projet…"
                                  : modules.length
                                    ? "Aucun module ne correspond aux filtres actuels."
                                    : "Aucun module Odoo n’a été détecté dans les dossiers addons du projet."}
                              </span>
                              {!loadingModules && (moduleSearch || moduleFilter !== "all" || moduleOriginFilter !== "all") && (
                                <Button
                                  type="button"
                                  size="sm"
                                  variant="outline"
                                  onClick={() => {
                                    setModuleSearch("");
                                    setModuleFilter("all");
                                    setModuleOriginFilter("all");
                                  }}
                                >
                                  Réinitialiser les filtres
                                </Button>
                              )}
                            </div>
                          )}
                        </div>
                        {filteredModules.length > 0 && (
                          <div className="flex flex-col gap-3 border-t bg-muted/30 px-3 py-2 text-sm sm:flex-row sm:items-center sm:justify-between">
                            <span className="text-muted-foreground">
                              {Math.min((modulePage - 1) * modulesPerPage + 1, filteredModules.length)}–{Math.min(modulePage * modulesPerPage, filteredModules.length)} sur {filteredModules.length} module(s)
                            </span>
                            <div className="flex items-center gap-2">
                              <Button size="icon" variant="outline" disabled={modulePage <= 1} onClick={() => setModulePage((page) => Math.max(1, page - 1))} aria-label="Page précédente" title="Page précédente">
                                <ChevronLeft className="h-4 w-4" />
                              </Button>
                              <span className="min-w-20 text-center tabular-nums">Page {modulePage}/{modulePageCount}</span>
                              <Button size="icon" variant="outline" disabled={modulePage >= modulePageCount} onClick={() => setModulePage((page) => Math.min(modulePageCount, page + 1))} aria-label="Page suivante" title="Page suivante">
                                <ChevronRight className="h-4 w-4" />
                              </Button>
                            </div>
                          </div>
                        )}
                      </div>
                    </CardContent>
                  </Card>
                </TabsContent>
              )}

              <TabsContent value="logs">
                <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(340px,400px)_minmax(0,1fr)]">
                  <Card className="min-w-0">
                    <CardHeader className="gap-3 sm:flex-row sm:items-center sm:justify-between">
                      <div className="min-w-0">
                        <CardTitle>Historique</CardTitle>
                        <CardDescription>Actions du projet sélectionné.</CardDescription>
                      </div>
                      <Button className="w-full shrink-0 sm:w-auto" variant="outline" size="sm" onClick={clearJobs}>
                        <Trash2 className="h-4 w-4" />
                        Effacer
                      </Button>
                    </CardHeader>
                    <CardContent className="max-h-[min(62vh,680px)] min-w-0 space-y-3 overflow-y-auto">
                      {projectJobs.length ? projectJobs.map((job) => (
                        <div
                          key={job.id}
                          className={cn(
                            "group grid h-[172px] min-w-0 grid-rows-[minmax(0,1fr)_36px] gap-2 rounded-md border bg-card p-3 shadow-sm transition-[background-color,border-color,box-shadow] hover:border-primary/40 hover:shadow-md",
                            !scopedExternalLogView && selectedJob?.id === job.id && "border-primary bg-primary/[0.08] ring-1 ring-primary/25 dark:bg-primary/[0.14]",
                          )}
                        >
                          <button
                            type="button"
                            className="grid min-h-0 w-full min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-3 rounded-md p-2 text-left transition-colors hover:bg-primary/[0.06] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring dark:hover:bg-primary/[0.12]"
                            aria-pressed={!scopedExternalLogView && selectedJob?.id === job.id}
                            title={job.title}
                            onClick={() => selectJob(job.id)}
                          >
                            <span className="flex h-full min-w-0 flex-col justify-between gap-2">
                              <span className="line-clamp-3 break-words text-sm font-semibold leading-5">{job.title}</span>
                              <span className="block text-xs tabular-nums text-muted-foreground">{job.started_at}</span>
                            </span>
                            <Badge className="min-w-[74px] shrink-0 justify-self-end" variant={statusVariant(job.status)}>
                              {statusLabel(job.status)}
                            </Badge>
                          </button>
                          <Button
                            className="w-full border-red-300 text-red-700 hover:border-red-400 hover:bg-red-50 hover:text-red-800 active:bg-red-100 focus-visible:ring-red-500 dark:border-red-800 dark:text-red-300 dark:hover:border-red-700 dark:hover:bg-red-950/60 dark:hover:text-red-200 dark:active:bg-red-950"
                            variant="outline"
                            size="sm"
                            title={`Supprimer l'historique ${job.title}`}
                            aria-label={`Supprimer l'historique ${job.title}`}
                            onClick={() => deleteJob(job.id)}
                          >
                            Supprimer
                          </Button>
                        </div>
                      )) : (
                        <div className="rounded-md border border-dashed p-6 text-center">
                          <Logs className="mx-auto h-6 w-6 text-muted-foreground" />
                          <p className="mt-3 font-medium">Aucune action enregistrée</p>
                          <p className="mt-1 text-sm text-muted-foreground">Les prochaines opérations apparaîtront ici avec leur statut.</p>
                        </div>
                      )}
                    </CardContent>
                  </Card>
                  <Card className="min-w-0">
                    <CardHeader className="min-w-0 gap-3 min-[1900px]:flex-row min-[1900px]:items-start min-[1900px]:justify-between">
                      <div className="min-w-0 flex-1">
                        <CardTitle>Sortie</CardTitle>
                        <CardDescription className="break-words">{displayedOutputTitle}</CardDescription>
                        {outputTitleIsLong && (
                          <button
                            type="button"
                            className="mt-1 text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                            aria-expanded={logDescriptionExpanded}
                            onClick={() => setLogDescriptionExpanded((expanded) => !expanded)}
                          >
                            {logDescriptionExpanded ? "Voir moins" : "Voir plus"}
                          </button>
                        )}
                      </div>
                      <div className="grid w-full min-w-0 grid-cols-1 gap-2 sm:grid-cols-3 min-[1900px]:w-auto min-[1900px]:shrink-0">
                        <Button className="w-full justify-start sm:justify-center" variant="outline" size="sm" onClick={showDiagnostics} disabled={!selectedProjectReady}>
                          <Activity className="h-4 w-4" />
                          Diagnostic
                        </Button>
                        <Button className="w-full justify-start sm:justify-center" variant="outline" size="sm" onClick={showLogs} disabled={!selectedProjectReady}>
                          <Logs className="h-4 w-4" />
                          Logs Odoo
                        </Button>
                        <Button
                          className="w-full justify-start sm:justify-center"
                          variant="outline"
                          size="sm"
                          onClick={copyOutput}
                        >
                          <Copy className="h-4 w-4" />
                          Copier
                        </Button>
                      </div>
                    </CardHeader>
                    <CardContent className="min-w-0">
                      <pre
                        ref={logOutputRef}
                        className="log-terminal min-h-[260px] max-h-[min(58vh,620px)] max-w-full overflow-auto whitespace-pre-wrap break-words rounded-md bg-slate-950 p-3 text-xs leading-relaxed text-emerald-100 sm:p-4"
                        onScroll={handleLogOutputScroll}
                      >
                        {outputContent}
                      </pre>
                    </CardContent>
                  </Card>
                </div>
              </TabsContent>

              <TabsContent value="actions">
                <div className={cn("grid gap-4", settings?.show_technical_details && "xl:grid-cols-2")}>
                  {settings?.show_technical_details && (
                    <Card>
                      <CardHeader>
                        <CardTitle>Code et images</CardTitle>
                        <CardDescription>Met à jour les sources et images Docker.</CardDescription>
                      </CardHeader>
                      <CardContent className="space-y-2">
                        <Button className="w-full" variant="outline" disabled={!selectedProjectReady} onClick={() => createJob("update_project", { project: selectedProject?.name })}>
                          <CloudDownload className="h-4 w-4" />
                          MAJ projet
                        </Button>
                        <Button className="w-full" onClick={() => createJob("update_all")}>
                          <CloudDownload className="h-4 w-4" />
                          MAJ tous les projets
                        </Button>
                      </CardContent>
                    </Card>
                  )}
                  <Card>
                    <CardHeader>
                      <CardTitle>Zone sensible</CardTitle>
                      <CardDescription>Suppression du projet local sélectionné.</CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-2">
                      <Button className="w-full" variant="destructive" disabled={!selectedProjectReady} onClick={() => setDeleteDialogOpen(true)}>
                        <Trash2 className="h-4 w-4" />
                        Supprimer projet
                      </Button>
                    </CardContent>
                  </Card>
                </div>
              </TabsContent>
            </Tabs>
          </div>
        </section>
      </div>

      <Dialog open={onboardingOpen} onOpenChange={setOnboardingOpen}>
        <DialogContent className="max-h-[90vh] max-w-2xl space-y-5 overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Préparer le gestionnaire Odoo</DialogTitle>
            <DialogDescription>
              Vérifie les prérequis une seule fois, puis crée ton premier environnement depuis l’application.
            </DialogDescription>
          </DialogHeader>
          <div className="divide-y overflow-hidden rounded-md border">
            <PrerequisiteRow
              ready={Boolean(creationPrerequisites?.workspace_ready)}
              icon={FolderPlus}
              title="Dossier des projets"
              detail={creationPrerequisites?.workspace || overview?.workspace || "Vérification en cours…"}
            />
            <PrerequisiteRow
              ready={Boolean(systemStatus?.docker.running)}
              icon={Boxes}
              title="Docker"
              detail={systemStatus?.docker.message || "Vérification en cours…"}
              action={
                systemStatus?.docker.running ? undefined : (
                  <Button size="sm" variant="outline" onClick={requestDockerStart} disabled={loading}>
                    {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                    Ouvrir
                  </Button>
                )
              }
            />
            <PrerequisiteRow
              ready={Boolean(creationPrerequisites?.git_available)}
              icon={GitBranch}
              title="Git"
              detail={[
                creationPrerequisites?.git_version || creationPrerequisites?.git_install_message || "Git doit être disponible sur la machine.",
                creationPrerequisites?.tool_environment,
              ].filter(Boolean).join(" · ")}
              action={
                !creationPrerequisites?.git_available && creationPrerequisites?.git_install_supported ? (
                  <Button size="sm" variant="outline" onClick={requestGitInstall} disabled={loading || gitInstallRunning}>
                    {gitInstallRunning ? <Loader2 className="h-4 w-4 animate-spin" /> : <CloudDownload className="h-4 w-4" />}
                    {gitInstallRunning ? "Installation…" : "Installer"}
                  </Button>
                ) : undefined
              }
            />
            <PrerequisiteRow
              ready={Boolean(creationPrerequisites?.ssh_key_present)}
              icon={KeyRound}
              title="Clé SSH GitLab"
              detail={
                creationPrerequisites?.ssh_key_present
                  ? `${creationPrerequisites.ssh_keys.join(", ")}${creationPrerequisites.tool_environment ? ` · ${creationPrerequisites.tool_environment}` : ""}`
                  : `Ajoute ta clé publique dans ton profil GitLab avant la première création.${creationPrerequisites?.tool_environment ? ` · ${creationPrerequisites.tool_environment}` : ""}`
              }
              action={
                creationPrerequisites?.ssh_keygen_available || creationPrerequisites?.ssh_key_present ? (
                  <Button size="sm" variant="outline" onClick={openSshAssistant}>
                    <KeyRound className="h-4 w-4" />
                    {creationPrerequisites.ssh_key_present ? "Voir la clé" : "Générer"}
                  </Button>
                ) : undefined
              }
            />
            <PrerequisiteRow
              ready={Boolean(systemStatus?.traefik?.running)}
              icon={Activity}
              title="Traefik"
              detail={systemStatus?.traefik?.message || "Vérification en cours…"}
              action={
                systemStatus?.traefik && !systemStatus.traefik.running && !systemStatus.traefik.requires_docker ? (
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={requestTraefikInstall}
                    disabled={!creationPrerequisites?.git_available || loading || traefikInstallRunning}
                    title={!creationPrerequisites?.git_available ? "Installe Git avant Traefik" : undefined}
                  >
                    {traefikInstallRunning ? <Loader2 className="h-4 w-4 animate-spin" /> : systemStatus.traefik.installed ? <Play className="h-4 w-4" /> : <CloudDownload className="h-4 w-4" />}
                    {traefikInstallRunning ? "Installation…" : systemStatus.traefik.installed ? "Démarrer" : "Installer"}
                  </Button>
                ) : undefined
              }
            />
          </div>
          {loadingCreationPrerequisites && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Vérification de Git et de la clé SSH…
            </div>
          )}
          <div className="flex flex-col-reverse gap-2 pt-1 sm:flex-row sm:justify-end">
            <Button
              variant="outline"
              onClick={async () => {
                await completeOnboarding();
                setOnboardingOpen(false);
              }}
            >
              Configurer plus tard
            </Button>
            <Button
              disabled={
                loadingCreationPrerequisites ||
                !creationPrerequisites?.workspace_ready ||
                !creationPrerequisites.git_available ||
                !creationPrerequisites.ssh_key_present
              }
              onClick={async () => {
                await completeOnboarding();
                setOnboardingOpen(false);
                openCreateProjectDialog();
              }}
            >
              <FolderPlus className="h-4 w-4" />
              Créer mon premier projet
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={sshDialogOpen} onOpenChange={setSshDialogOpen}>
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Clé SSH GitLab</DialogTitle>
            <DialogDescription>
              Le gestionnaire génère la clé sur cette machine. Seule la clé publique est affichée et peut être copiée.
            </DialogDescription>
          </DialogHeader>
          {sshKeys.length === 0 ? (
            <div className="space-y-4">
              <div className="grid gap-1.5">
                <label className="text-sm font-medium" htmlFor="ssh-key-comment">E-mail professionnel ou commentaire</label>
                <Input
                  id="ssh-key-comment"
                  value={sshComment}
                  onChange={(event) => setSshComment(event.target.value)}
                  placeholder="prenom.nom@sudokeys.com"
                  autoComplete="email"
                />
                <p className="text-xs text-muted-foreground">Ce texte sert uniquement à identifier la clé dans GitLab.</p>
              </div>
              <Button className="w-full" onClick={requestSshKeyGeneration} disabled={generatingSshKey}>
                {generatingSshKey ? <Loader2 className="h-4 w-4 animate-spin" /> : <KeyRound className="h-4 w-4" />}
                Générer une clé Ed25519
              </Button>
            </div>
          ) : (
            <div className="space-y-4">
              {sshKeys.length > 1 && (
                <div className="grid gap-1.5">
                  <label className="text-sm font-medium" htmlFor="ssh-public-key-select">Clé publique</label>
                  <Select value={selectedSshKey?.name || ""} onValueChange={setSelectedSshKeyName}>
                    <SelectTrigger id="ssh-public-key-select"><SelectValue /></SelectTrigger>
                    <SelectContent>
                      {sshKeys.map((key) => <SelectItem key={key.name} value={key.name}>{key.name}</SelectItem>)}
                    </SelectContent>
                  </Select>
                </div>
              )}
              <div className="grid gap-1.5">
                <label className="text-sm font-medium" htmlFor="ssh-public-key">Clé publique à ajouter dans GitLab</label>
                <Textarea
                  id="ssh-public-key"
                  className="min-h-32 resize-y break-all font-mono text-xs"
                  readOnly
                  value={selectedSshKey?.public_key || ""}
                />
              </div>
              <div className="grid gap-2 sm:grid-cols-2">
                <Button variant="outline" onClick={copySshPublicKey} disabled={!selectedSshKey}>
                  <Copy className="h-4 w-4" />
                  Copier la clé
                </Button>
                <Button
                  onClick={() => openUrl(creationPrerequisites?.gitlab_ssh_keys_url)}
                  disabled={!creationPrerequisites?.gitlab_ssh_keys_url}
                >
                  <ExternalLink className="h-4 w-4" />
                  Ouvrir GitLab
                </Button>
              </div>
              <p className="text-xs leading-5 text-muted-foreground">
                Dans GitLab, colle cette valeur dans le champ Clé SSH, donne-lui un titre correspondant à cet ordinateur, puis valide.
              </p>
            </div>
          )}
        </DialogContent>
      </Dialog>

      <CreateProjectDialog
        open={createProjectOpen}
        onOpenChange={setCreateProjectOpen}
        prerequisites={creationPrerequisites}
        dockerReady={Boolean(systemStatus?.docker.running)}
        loading={loading}
        onRefreshPrerequisites={loadCreationPrerequisites}
        onSubmit={requestProjectCreation}
      />

      <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Paramètres du gestionnaire</DialogTitle>
            <DialogDescription>
              Le workspace est le dossier analysé pour lister les projets et celui utilisé lors des prochaines créations.
            </DialogDescription>
          </DialogHeader>
          {settingsDraft ? (
            <div className="grid gap-4">
              <div className="grid min-w-0 gap-1.5 text-sm font-medium">
                <label htmlFor="projects-workspace">Dossier des projets</label>
                <div className="flex min-w-0 flex-col gap-2 sm:flex-row">
                  <Input
                    id="projects-workspace"
                    className="min-w-0 flex-1"
                    value={settingsDraft.workspace}
                    onChange={(event) => setSettingsDraft({ ...settingsDraft, workspace: event.target.value })}
                    placeholder="/chemin/vers/Odoo-projects"
                  />
                  <Button
                    className="shrink-0"
                    type="button"
                    variant="outline"
                    disabled={!desktopRuntime || selectingWorkspace}
                    title={desktopRuntime ? "Choisir un dossier" : "Disponible dans l’application installée"}
                    onClick={selectWorkspaceDirectory}
                  >
                    {selectingWorkspace ? <Loader2 className="h-4 w-4 animate-spin" /> : <FolderOpen className="h-4 w-4" />}
                    Choisir
                  </Button>
                </div>
                <span className="text-xs font-normal leading-relaxed text-muted-foreground">
                  Le dossier est créé s’il n’existe pas encore. Dans l’application installée, « Choisir » ouvre le sélecteur du système.
                </span>
              </div>

              <div className="rounded-md border bg-muted/40 p-3">
                <div className="text-sm font-medium">Exécution automatique</div>
                <p className="mt-1 text-xs font-normal leading-relaxed text-muted-foreground">
                  Le gestionnaire choisit automatiquement les outils adaptés au système. Sous Windows, Docker,
                  Git et les chemins sont exécutés dans l’environnement compatible avec le workspace. Les chemins Windows
                  sont traduits automatiquement lorsque Docker ou Git passe par WSL.
                </p>
              </div>

              <div className="grid gap-3 rounded-md border p-3">
                <div className="flex min-w-0 items-start gap-3">
                  <KeyRound className="mt-0.5 h-5 w-5 shrink-0 text-primary" />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-medium">Clé SSH GitLab</div>
                    <p className="mt-1 break-words text-xs leading-relaxed text-muted-foreground">
                      {selectedSshKey
                        ? `${selectedSshKey.name} · ${selectedSshKey.public_key}`
                        : "Aucune clé publique détectée dans l’environnement Git utilisé par le gestionnaire."}
                    </p>
                  </div>
                </div>
                <Button type="button" variant="outline" onClick={openSshAssistant}>
                  <KeyRound className="h-4 w-4" />
                  {selectedSshKey ? "Gérer la clé SSH" : "Configurer une clé"}
                </Button>
              </div>

              <div className="grid gap-3 rounded-md border p-3">
                <div>
                  <div className="text-sm font-medium">Configuration initiale</div>
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                    Rouvre l’assistant du premier démarrage pour vérifier le workspace, Docker, Git, SSH et Traefik.
                  </p>
                </div>
                <Button type="button" variant="outline" onClick={reopenInitialConfiguration}>
                  <Settings className="h-4 w-4" />
                  Ouvrir l’assistant de configuration
                </Button>
              </div>

              <div className="grid gap-3 rounded-md border p-3">
                <div className="text-sm font-medium">Affichage des pages</div>
                <p className="text-xs text-muted-foreground">L’affichage classique est utilisé par défaut. Tu peux choisir chaque page séparément.</p>
                {([
                  ["bases_layout", "Bases", "Liste à gauche et actions à droite", "Liste en haut et actions en dessous"],
                  ["modules_layout", "Modules", "50 lignes par page, défilement dans la liste", "20 lignes par page, défilement de la page"],
                ] as const).map(([key, title, classicDescription, compactDescription]) => (
                  <div key={key} className="grid gap-2">
                    <div className="text-sm font-medium">{title}</div>
                    <div className="grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label={`Affichage ${title}`}>
                      {(["classic", "compact"] as const).map((value) => (
                        <InteractiveCard key={value} role="radio" aria-checked={(settingsDraft[key] ?? "classic") === value}
                          className={cn("p-3 text-left", (settingsDraft[key] ?? "classic") === value && "border-primary bg-primary/10")}
                          onClick={() => setSettingsDraft({ ...settingsDraft, [key]: value })}>
                          <span className="block text-sm font-medium">{value === "classic" ? "Classique (ancien)" : "Compact (nouveau)"}</span>
                          <span className="mt-1 block text-xs text-muted-foreground">{value === "classic" ? classicDescription : compactDescription}</span>
                        </InteractiveCard>
                      ))}
                    </div>
                  </div>
                ))}
              </div>

              <div className="grid gap-2">
                <div>
                  <div className="text-sm font-medium">Icône affichée</div>
                  <p className="mt-1 text-xs font-normal text-muted-foreground">
                    Choisis l’identité visuelle utilisée dans le gestionnaire.
                  </p>
                </div>
                <div className="grid gap-2 sm:grid-cols-2" role="radiogroup" aria-label="Icône affichée">
                  <InteractiveCard
                    className={cn(
                      "flex min-h-24 items-center gap-3 p-3",
                      settingsDraft.interface_icon === "manager" && "border-primary bg-primary/[0.08] ring-1 ring-primary/25 dark:bg-primary/[0.14]",
                    )}
                    role="radio"
                    aria-checked={settingsDraft.interface_icon === "manager"}
                    onClick={() => setSettingsDraft({ ...settingsDraft, interface_icon: "manager" })}
                  >
                    <img src={appIcon.src} alt="" aria-hidden="true" className="h-14 w-14 shrink-0 rounded-[13px] object-cover" />
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-semibold">SDK Local Manager</span>
                      <span className="mt-1 block text-xs text-muted-foreground">Logo Sudokeys</span>
                    </span>
                    {settingsDraft.interface_icon === "manager" && <CheckCircle2 className="h-5 w-5 shrink-0 text-primary" aria-hidden="true" />}
                  </InteractiveCard>
                  <InteractiveCard
                    className={cn(
                      "flex min-h-24 items-center gap-3 p-3",
                      settingsDraft.interface_icon === "local" && "border-primary bg-primary/[0.08] ring-1 ring-primary/25 dark:bg-primary/[0.14]",
                    )}
                    role="radio"
                    aria-checked={settingsDraft.interface_icon === "local"}
                    onClick={() => setSettingsDraft({ ...settingsDraft, interface_icon: "local" })}
                  >
                    <img src={localIcon.src} alt="" aria-hidden="true" className="h-14 w-14 shrink-0 rounded-full object-cover" />
                    <span className="min-w-0 flex-1">
                      <span className="block text-sm font-semibold">Logo Local</span>
                      <span className="mt-1 block text-xs text-muted-foreground">Nouvelle icône</span>
                    </span>
                    {settingsDraft.interface_icon === "local" && <CheckCircle2 className="h-5 w-5 shrink-0 text-primary" aria-hidden="true" />}
                  </InteractiveCard>
                </div>
              </div>

              <label className="flex cursor-pointer items-start gap-3 rounded-md border p-3 text-sm">
                <Checkbox
                  className="mt-0.5"
                  checked={settingsDraft.show_technical_details}
                  onCheckedChange={(checked) =>
                    setSettingsDraft({ ...settingsDraft, show_technical_details: checked === true })
                  }
                />
                <span className="min-w-0">
                  <span className="block font-medium">Afficher les détails techniques</span>
                  <span className="mt-1 block text-xs font-normal leading-relaxed text-muted-foreground">
                    Affiche les états Odoo et PostgreSQL ainsi que le nombre de bases dans la liste des projets,
                    les emplacements des modules, et les actions de mise à jour du code et des images Docker.
                    Désactivé, le gestionnaire présente uniquement le voyant d’état des projets et une liste de modules compacte.
                  </span>
                </span>
              </label>

              <label className="flex cursor-pointer items-start gap-3 rounded-md border p-3 text-sm">
                <Checkbox
                  className="mt-0.5"
                  checked={settingsDraft.start_project_before_open}
                  onCheckedChange={(checked) =>
                    setSettingsDraft({ ...settingsDraft, start_project_before_open: checked === true })
                  }
                />
                <span className="min-w-0">
                  <span className="block font-medium">Démarrer le projet avant d’ouvrir Odoo</span>
                  <span className="mt-1 block text-xs font-normal leading-relaxed text-muted-foreground">
                    Si cette option est activée, le bouton « Ouvrir Odoo » démarre et attend le projet avant
                    d’ouvrir le navigateur. Par défaut, le bouton ouvre uniquement la base sélectionnée.
                  </span>
                </span>
              </label>

              <label className="grid gap-1.5 text-sm font-medium">
                Commande Docker
                <Input
                  value={settingsDraft.docker_executable}
                  onChange={(event) => setSettingsDraft({ ...settingsDraft, docker_executable: event.target.value })}
                  placeholder="docker"
                />
              </label>

              <label className="grid min-w-0 gap-1.5 text-sm font-medium">
                Dossier Traefik
                <Input
                  value={settingsDraft.traefik_directory}
                  onChange={(event) => setSettingsDraft({ ...settingsDraft, traefik_directory: event.target.value })}
                  placeholder="Détection automatique si vide"
                />
              </label>

              <label className="grid gap-1.5 text-sm font-medium">
                Vérification Docker (secondes)
                <Input
                  type="number"
                  min={3}
                  max={60}
                  value={settingsDraft.docker_poll_interval}
                  onChange={(event) => setSettingsDraft({ ...settingsDraft, docker_poll_interval: Number(event.target.value) })}
                />
              </label>

              <label className="grid gap-1.5 text-sm font-medium">
                Port local du gestionnaire
                <Input
                  type="number"
                  min={1024}
                  max={65535}
                  value={settingsDraft.api_port}
                  onChange={(event) => setSettingsDraft({ ...settingsDraft, api_port: Number(event.target.value) })}
                />
                <span className="text-xs font-normal leading-relaxed text-muted-foreground">
                  Port préféré de l’API locale. Un redémarrage est nécessaire après modification. S’il est occupé, notamment par Docker, le gestionnaire choisit automatiquement un port libre.
                </span>
              </label>

              <div className="grid gap-3 rounded-md border p-3">
                <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                  <div className="min-w-0">
                    <div className="text-sm font-medium">Journal d’erreurs du gestionnaire</div>
                    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                      Les erreurs d’API, de jobs et d’interface sont conservées localement. Les mots de passe, jetons et secrets détectés sont masqués.
                    </p>
                    {managerErrorLogPath && <p className="mt-1 break-all text-xs text-muted-foreground">Fichier : {managerErrorLogPath}</p>}
                  </div>
                  <Badge className="shrink-0" variant={managerErrors.length ? "warning" : "secondary"}>
                    {managerErrors.length} erreur(s)
                  </Badge>
                </div>
                <div className="max-h-72 space-y-2 overflow-y-auto rounded-md border bg-muted/30 p-2">
                  {loadingManagerErrors ? (
                    <div className="flex items-center gap-2 p-2 text-xs text-muted-foreground">
                      <Loader2 className="h-4 w-4 animate-spin" /> Chargement…
                    </div>
                  ) : managerErrors.length ? managerErrors.map((entry) => (
                    <details key={entry.id} className="rounded-md border bg-card p-2 text-xs">
                      <summary className="cursor-pointer break-words font-medium">
                        {entry.timestamp} · {entry.source}{entry.project ? ` · ${entry.project}` : ""}
                      </summary>
                      <p className="mt-2 whitespace-pre-wrap break-words text-destructive">{entry.message}</p>
                      {entry.details && <pre className="log-terminal mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-950 p-2 text-[11px] text-slate-100">{entry.details}</pre>}
                    </details>
                  )) : (
                    <p className="p-2 text-xs text-muted-foreground">Aucune erreur enregistrée.</p>
                  )}
                </div>
                <div className="grid gap-2 sm:grid-cols-3">
                  <Button type="button" variant="outline" onClick={loadManagerErrors} disabled={loadingManagerErrors}>
                    <RefreshCcw className="h-4 w-4" /> Actualiser
                  </Button>
                  <Button type="button" variant="outline" onClick={copyManagerErrors} disabled={!managerErrors.length}>
                    <Copy className="h-4 w-4" /> Copier
                  </Button>
                  <Button type="button" variant="outline" onClick={clearManagerErrors} disabled={!managerErrors.length}>
                    <Trash2 className="h-4 w-4" /> Effacer
                  </Button>
                </div>
              </div>

              <div className="grid gap-3 rounded-md border bg-muted/40 p-3 text-sm">
                <div>
                  <div className="font-medium">Ports utilisés ou contactés</div>
                  <p className="mt-1 text-xs text-muted-foreground">Les ports internes Docker ne sont pas réservés sur Windows sauf publication explicite du projet.</p>
                </div>
                <div className="grid gap-x-4 gap-y-2 text-xs sm:grid-cols-[100px_minmax(0,1fr)]">
                  <code>{settingsDraft.api_port_actual || settingsDraft.api_port}</code><span>API locale du gestionnaire, sur <code>127.0.0.1</code> uniquement</span>
                  <code>80 / 443</code><span>Traefik, accès HTTP/HTTPS aux projets</span>
                  <code>8069</code><span>Odoo à l’intérieur de chaque conteneur</span>
                  <code>5432</code><span>PostgreSQL à l’intérieur de chaque conteneur</span>
                  <code>10022</code><span>Connexion SSH sortante vers GitLab Sudokeys</span>
                  <code>3000</code><span>Interface Next.js, uniquement en mode développement</span>
                </div>
                {settingsDraft.api_port_actual && settingsDraft.api_port_actual !== settingsDraft.api_port && (
                  <p className="text-xs text-amber-700 dark:text-amber-300">
                    Le port {settingsDraft.api_port} était occupé au démarrage. Cette session utilise automatiquement le port {settingsDraft.api_port_actual}.
                  </p>
                )}
              </div>

              <div className="rounded-md border bg-muted/40 p-3 text-xs text-muted-foreground">
                <div>Plateforme : {settingsDraft.platform || systemStatus?.docker.platform || "-"}</div>
                <div className="mt-1 break-all">Configuration : {settingsDraft.config_file || "-"}</div>
              </div>

              <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
                <Button variant="outline" onClick={() => setSettingsOpen(false)}>Annuler</Button>
                <Button
                  disabled={savingSettings || !settingsDraft.workspace.trim() || settingsDraft.api_port < 1024 || settingsDraft.api_port > 65535}
                  onClick={saveSettings}
                >
                  {savingSettings && <Loader2 className="h-4 w-4 animate-spin" />}
                  Enregistrer
                </Button>
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-2 rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Chargement des paramètres...
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={aboutOpen} onOpenChange={setAboutOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>À propos d’SDK Local Manager</DialogTitle>
            <DialogDescription>
              Gestionnaire local pour créer, administrer et maintenir des environnements Odoo.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4">
            <div className="flex min-w-0 flex-col items-start justify-between gap-3 rounded-md border bg-muted/35 p-4 sm:flex-row sm:items-center">
              <div className="flex min-w-0 items-center gap-3">
                <img
                  src={selectedAppIcon.src}
                  alt=""
                  aria-hidden="true"
                  className={cn(
                    "h-14 w-14 shrink-0 object-cover",
                    settings?.interface_icon === "local" ? "rounded-full" : "rounded-[13px]",
                  )}
                />
                <div className="min-w-0">
                  <div className="font-semibold">SDK Local Manager</div>
                  <div className="mt-1 text-sm text-muted-foreground">Application desktop multi-plateforme</div>
                </div>
              </div>
              <Badge className="shrink-0" variant="outline">Version {appVersion}</Badge>
            </div>
            <div className="rounded-md border p-4">
              <div className="text-sm font-semibold">À propos du créateur</div>
              <div className="mt-3 flex items-start gap-2 text-sm leading-relaxed text-muted-foreground">
                <Heart className="mt-0.5 h-4 w-4 shrink-0 fill-current text-red-500" aria-hidden="true" />
                <p>Fait avec amour par Aymerick Benjamin LAURETTA-PERONNE</p>
              </div>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={socleDialogOpen} onOpenChange={setSocleDialogOpen}>
        <DialogContent className="max-h-[90vh] max-w-3xl space-y-5 overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Installer un socle Odoo</DialogTitle>
            <DialogDescription>
              Sélectionne les applications à installer dans {selectedDb || "la base choisie"}. Le manager vérifie et crée d’abord les liens symboliques Enterprise manquants.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-3 sm:grid-cols-2">
            {SOCLE_PRESETS.map((preset) => {
              const missing = preset.modules.filter((moduleName) => !moduleByName.has(moduleName));
              const installed = preset.modules.filter((moduleName) => moduleByName.get(moduleName)?.state === "installed");
              const unavailable = missing.length > 0;
              const alreadyInstalled = !unavailable && installed.length === preset.modules.length;
              return (
                <label
                  key={preset.id}
                  className={cn(
                    "flex items-start gap-3 rounded-md border p-3 text-sm",
                    unavailable || alreadyInstalled ? "cursor-not-allowed bg-muted/35 opacity-60" : "cursor-pointer hover:bg-muted/45",
                  )}
                >
                  <Checkbox
                    className="mt-0.5"
                    checked={alreadyInstalled || selectedSoclePresets.has(preset.id)}
                    disabled={unavailable || alreadyInstalled || loading}
                    onCheckedChange={(checked) => toggleSoclePreset(preset.id, checked === true)}
                  />
                  <img src={`/odoo-apps/${preset.id}.svg`} alt="" aria-hidden="true" className="h-12 w-12 shrink-0 object-contain" />
                  <span className="min-w-0">
                    <span className="block font-medium">{preset.label}</span>
                    <span className="mt-1 block break-all text-xs text-muted-foreground">{preset.modules.join(" + ")}</span>
                    {unavailable && <span className="mt-1 block text-xs text-destructive">Absent : {missing.join(", ")}</span>}
                    {!unavailable && installed.length > 0 && (
                      <span className="mt-1 block text-xs text-muted-foreground">Déjà installé : {installed.join(", ")}</span>
                    )}
                  </span>
                </label>
              );
            })}
          </div>
          <div className="rounded-md border bg-muted/35 p-3 text-sm">
            <div className="font-medium">Comptabilité</div>
            <p className="mt-1 text-xs text-muted-foreground">
              Le socle comptable installe uniquement Comptabilité et la localisation française (`account_accountant` + `l10n_fr`).
            </p>
          </div>
          <div className="grid gap-2 sm:grid-cols-2">
            <Button variant="outline" disabled={!selectedProject || loading} onClick={repairEnterpriseLinks}>
              <RefreshCcw className="h-4 w-4" />
              Vérifier / créer les liens uniquement
            </Button>
            <Button
              disabled={!selectedDb || loading || !Array.from(selectedSoclePresets).some((presetId) => !installedSoclePresetIds.has(presetId))}
              onClick={installSelectedSocle}
            >
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Boxes className="h-4 w-4" />}
              Installer la sélection
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={repositoryOpen} onOpenChange={setRepositoryOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Modules depuis un dépôt SSH</DialogTitle>
            <DialogDescription>Copie le code dans le projet {selectedProject?.name}. Choisis une branche compatible avec sa version Odoo.</DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <label className="block space-y-2">
              <span>URL SSH du dépôt</span>
              <Input
                value={repositoryUrl}
                onChange={(e) => setRepositoryUrl(e.target.value)}
                placeholder="ssh://git@gitlab.sudokeys.com:10022/equipe/depot.git"
                aria-invalid={Boolean(repositoryUrlError)}
                aria-describedby={repositoryUrlError ? "repository-url-error" : undefined}
              />
              {repositoryUrlError ? <span id="repository-url-error" className="block text-sm text-destructive">{repositoryUrlError}</span> : null}
            </label>
            <label className="block space-y-2"><span>Branche ou tag</span><Input value={repositoryBranch} onChange={(e) => setRepositoryBranch(e.target.value)} placeholder="18.0" /></label>
            <Select value={repositoryMode} onValueChange={setRepositoryMode}>
              <SelectTrigger aria-label="Opération"><SelectValue /></SelectTrigger>
              <SelectContent><SelectItem value="add">Ajouter des modules</SelectItem><SelectItem value="update">Mettre à jour le code existant</SelectItem></SelectContent>
            </Select>
            <label className="block space-y-2"><span>Noms techniques, séparés par des virgules</span><Input value={repositoryModules} onChange={(e) => setRepositoryModules(e.target.value)} placeholder="sale_exception, sale_order_type" /></label>
            <p className="text-sm text-muted-foreground">{repositoryMode === "add" ? "Laisse les noms vides pour ajouter tous les modules du dépôt. Tout doublon bloque l’import." : "Les noms sont obligatoires. Seules les copies gérées dans addons-store sont remplacées, avec sauvegarde et restauration en cas d’échec."}</p>
            <div className="flex flex-col gap-2 rounded-md border p-3 text-sm text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
              <span>Le manager utilise la clé SSH de cette machine. Aucun jeton GitLab n’est demandé ni stocké.</span>
              <Button type="button" size="sm" variant="outline" onClick={openSshAssistant}>
                <KeyRound className="h-4 w-4" />
                Gérer la clé SSH
              </Button>
            </div>
            <p className="text-sm text-muted-foreground">Après l’import, lance l’installation ou la mise à jour dans la base Odoo.</p>
            <Button
              disabled={repositorySubmitting || !selectedProjectReady || !repositoryUrl.trim() || Boolean(repositoryUrlError) || !repositoryBranch.trim() || (repositoryMode === "update" && !repositoryModules.trim())}
              onClick={submitRepositoryModules}
            >
              {repositorySubmitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <CloudDownload className="h-4 w-4" />}
              {repositorySubmitting
                ? "Lancement de l’import…"
                : repositoryMode === "add" ? "Ajouter depuis le dépôt" : "Sauvegarder et remplacer le code"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog
        open={zipDialogOpen}
        onOpenChange={(open) => {
          setZipDialogOpen(open);
          if (!open) resetZipImport();
        }}
      >
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Importer un ZIP de modules</DialogTitle>
            <DialogDescription>
              Analyse l’archive, choisis les modules à copier dans addons-store, puis confirme l’import.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4">
            <FilePicker
              ref={zipInputRef}
              accept=".zip"
              file={zipFile}
              buttonLabel="Choisir un ZIP"
              disabled={loading || inspectingZip}
              onChange={(event) => void inspectZipFile(event.target.files?.[0])}
            />
            {inspectingZip && (
              <div className="flex items-center gap-2 rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Analyse sécurisée de l’archive…
              </div>
            )}
            {!inspectingZip && zipModuleCandidates.length > 0 && (
              <div className="min-w-0 rounded-md border">
                <label className="flex cursor-pointer items-start gap-3 border-b bg-muted/40 p-3 text-sm">
                  <Checkbox
                    className="mt-0.5"
                    checked={
                      selectedZipModules.size > 0 && selectedZipModules.size < zipModuleCandidates.length
                        ? "indeterminate"
                        : selectedZipModules.size === zipModuleCandidates.length
                    }
                    onCheckedChange={(checked) => toggleAllZipModules(checked === true)}
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block font-medium">Sélectionner tous les modules détectés</span>
                    <span className="block text-xs text-muted-foreground">
                      {selectedZipModules.size}/{zipModuleCandidates.length} module(s) sélectionné(s)
                    </span>
                  </span>
                </label>
                <div className="max-h-64 overflow-y-auto p-2">
                  {zipModuleCandidates.map((moduleName) => (
                    <label
                      key={moduleName}
                      className="flex min-w-0 cursor-pointer items-start gap-3 rounded-md p-2 text-sm hover:bg-muted"
                    >
                      <Checkbox
                        className="mt-0.5"
                        checked={selectedZipModules.has(moduleName)}
                        onCheckedChange={(checked) => toggleZipModule(moduleName, checked === true)}
                      />
                      <span className="min-w-0 break-all font-mono">{moduleName}</span>
                    </label>
                  ))}
                </div>
              </div>
            )}
            <label className="flex items-start gap-2 rounded-md border bg-muted/40 p-3 text-sm">
              <Checkbox
                className="mt-1"
                checked={replaceZipModules}
                onCheckedChange={(checked) => setReplaceZipModules(checked === true)}
              />
              <span>
                <span className="block font-medium">Remplacer les modules existants</span>
                <span className="block text-xs text-muted-foreground">
                  L’ancien dossier ou lien est sauvegardé dans `.odoo_manager_backups` avant remplacement.
                </span>
              </span>
            </label>
            <Button onClick={importZip} disabled={loading || inspectingZip || !selectedZipModules.size}>
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileArchive className="h-4 w-4" />}
              Importer {selectedZipModules.size || ""} module(s)
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <CreateDatabaseDialog
        open={createDbOpen}
        onOpenChange={setCreateDbOpen}
        project={selectedProject}
        onSubmit={async (payload) => {
          const job = await createJob("create_database", payload);
          if (job) setCreateDbOpen(false);
        }}
      />

      <RestoreDatabaseDialog
        open={restoreDbOpen}
        onOpenChange={setRestoreDbOpen}
        project={selectedProject}
        onSubmit={restoreDatabaseBackup}
      />

      <Dialog open={neutralizeDbOpen} onOpenChange={setNeutralizeDbOpen}>
        <DialogContent className="space-y-5">
          <DialogHeader>
            <DialogTitle>Neutraliser {selectedDb || "la base"}</DialogTitle>
            <DialogDescription>
              Odoo sera arrêté brièvement. Tous les crons métier, dont le contrôle d’abonnement,
              ainsi que les serveurs de messagerie entrants et sortants seront désactivés.
              Sur les versions récentes, Odoo efface aussi les identifiants SMTP. Cette opération
              n’est pas réversible automatiquement.
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950/45 dark:text-amber-100">
            À utiliser uniquement sur une copie locale ou une base de test, jamais sur la production.
          </div>
          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <Button variant="outline" onClick={() => setNeutralizeDbOpen(false)}>Annuler</Button>
            <Button
              disabled={!selectedProject || !canUseDb || loading}
              onClick={async () => {
                const job = await createJob("neutralize_database", {
                  project: selectedProject?.name,
                  db: selectedDb,
                });
                if (job) setNeutralizeDbOpen(false);
              }}
            >
              <ShieldCheck className="h-4 w-4" />
              Confirmer la neutralisation
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Supprimer {selectedProject?.name}</DialogTitle>
            <DialogDescription>Le projet sera déplacé dans `.odoo_manager_deleted`. Saisis le nom du projet pour confirmer.</DialogDescription>
          </DialogHeader>
          <Input value={deleteConfirm} onChange={(event) => setDeleteConfirm(event.target.value)} placeholder={selectedProject?.name} />
          <Button
            variant="destructive"
            disabled={!selectedProject || deleteConfirm !== selectedProject.name}
            onClick={async () => {
              await createJob("delete_project", { project: selectedProject?.name });
              setDeleteConfirm("");
              setDeleteDialogOpen(false);
            }}
          >
            <Trash2 className="h-4 w-4" />
            Supprimer
          </Button>
        </DialogContent>
      </Dialog>

      <Dialog
        open={updateAllDialogOpen}
        onOpenChange={(open) => {
          setUpdateAllDialogOpen(open);
          if (!open) {
            setAllowMissingFilestore(false);
            setUpdateFilestoreStatus(null);
            setUpdatePendingModules([]);
            setUpdateLocalExcludedModules([]);
            setMissingModulesToIgnore(new Set());
          }
        }}
      >
        <DialogContent className="max-h-[90vh] max-w-2xl space-y-5 overflow-y-auto">
          <DialogHeader>
            <DialogTitle>MAJ complète Odoo</DialogTitle>
            <DialogDescription>
              Choisis la portée de l’opération. Les modules détectés après le dernier import SSH sont proposés en priorité.
            </DialogDescription>
          </DialogHeader>
          {detectedImportedModules.length ? (
            <div className="grid gap-3 rounded-md border bg-muted/35 p-3 sm:grid-cols-2">
              <button
                type="button"
                className={cn(
                  "rounded-md border p-3 text-left text-sm transition-colors",
                  updateScope === "imported" ? "border-primary bg-primary/[0.08] ring-1 ring-primary/25" : "bg-background hover:bg-muted/55",
                )}
                onClick={() => setUpdateScope("imported")}
              >
                <span className="block font-medium">Modules importés détectés</span>
                <span className="mt-1 block text-xs text-muted-foreground">
                  Installer ou mettre à jour uniquement {detectedImportedModules.length} module(s).
                </span>
              </button>
              <button
                type="button"
                className={cn(
                  "rounded-md border p-3 text-left text-sm transition-colors",
                  updateScope === "all" ? "border-primary bg-primary/[0.08] ring-1 ring-primary/25" : "bg-background hover:bg-muted/55",
                )}
                onClick={() => setUpdateScope("all")}
              >
                <span className="block font-medium">Forcer la MAJ complète</span>
                <span className="mt-1 block text-xs text-muted-foreground">Exécuter la mise à jour de l’ensemble des modules installés.</span>
              </button>
              {updateScope === "imported" && (
                <div className="flex max-h-28 flex-wrap gap-1.5 overflow-y-auto sm:col-span-2">
                  {detectedImportedModules.map((moduleName) => (
                    <Badge key={moduleName} variant="outline" className="bg-background font-mono">{moduleName}</Badge>
                  ))}
                </div>
              )}
            </div>
          ) : null}
          <div className="grid gap-3 rounded-md border bg-muted/40 p-3 text-sm">
            <div className="grid gap-1">
              <span className="text-xs font-medium uppercase text-muted-foreground">Projet</span>
              <span className="break-words font-medium">{selectedProject?.name || "-"}</span>
            </div>
            <div className="grid gap-1">
              <span className="text-xs font-medium uppercase text-muted-foreground">Base</span>
              <span className="break-words font-medium">{selectedDb || "-"}</span>
            </div>
            <div className="grid gap-1">
              <span className="text-xs font-medium uppercase text-muted-foreground">Commande</span>
              <code className="break-all rounded bg-slate-950 px-2 py-1 text-xs text-emerald-100">
                {updateScope === "imported" && detectedImportedModules.length
                  ? `odoo -d ${selectedDb || "BASE"} -i/-u ${detectedImportedModules.join(",")} --stop-after-init`
                  : `odoo -d ${selectedDb || "BASE"} -u ${updateLocalExcludedModules.length ? "<modules disponibles non exclus>" : "all"} --stop-after-init`}
              </code>
            </div>
          </div>
          {updateScope === "all" && updateLocalExcludedModules.length ? (
            <div className="grid gap-2 rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-950 dark:border-blue-800 dark:bg-blue-950/45 dark:text-blue-100">
              <div className="font-medium">Mode avec exceptions locales</div>
              <p>
                Le gestionnaire utilisera une liste explicite des modules dont le code est disponible. Les modules suivants ne seront pas remis en
                attente par un nouvel appel à <code>-u all</code> :
              </p>
              <div className="flex flex-wrap gap-1.5">
                {updateLocalExcludedModules.map((moduleName) => (
                  <Badge key={moduleName} variant="outline" className="border-blue-300 bg-white font-mono text-blue-950 dark:border-blue-700 dark:bg-blue-950/70 dark:text-blue-100">
                    {moduleName}
                  </Badge>
                ))}
              </div>
              <Button variant="outline" className="border-blue-300 bg-white hover:bg-blue-100 dark:border-blue-700 dark:bg-blue-950/70 dark:hover:bg-blue-900/70" onClick={restoreLocalModuleExclusions} disabled={loading}>
                <RefreshCcw className="h-4 w-4" />
                Réactiver toutes les exclusions
              </Button>
            </div>
          ) : null}
          {updateScope === "all" && pendingModulesWithAvailableCode.length ? (
            <div className="grid gap-2 rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-950 dark:border-blue-800 dark:bg-blue-950/45 dark:text-blue-100">
              <div className="flex items-start gap-2">
                <Info className="mt-0.5 h-4 w-4 shrink-0" />
                <div className="grid gap-1">
                  <span className="font-medium">Opérations Odoo à terminer</span>
                  <span>
                    Une installation ou une mise à jour précédente a laissé {pendingModulesWithAvailableCode.length} module(s) en attente. Leur code est présent : la mise à jour complète peut les reprendre automatiquement.
                  </span>
                </div>
              </div>
              <div className="flex max-h-28 flex-wrap gap-1.5 overflow-y-auto">
                {pendingModulesWithAvailableCode.map((module) => (
                  <Badge key={module.name} variant="outline" className="border-blue-300 bg-white font-mono text-blue-950 dark:border-blue-700 dark:bg-blue-950/70 dark:text-blue-100">
                    {module.name} · {module.state}
                  </Badge>
                ))}
              </div>
            </div>
          ) : null}
          {updateScope === "all" && pendingModulesWithMissingCode.length ? (
            <div className="grid gap-3 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-950 dark:border-red-800 dark:bg-red-950/45 dark:text-red-100">
              <div className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-red-600" />
                <div className="grid gap-1">
                  <span className="font-medium">Code source manquant pour {pendingModulesWithMissingCode.length} module(s)</span>
                  <span>
                    Odoo avait prévu de les installer, mettre à jour ou supprimer, mais leur dossier n’existe plus dans le projet. Restaure leur code si tu veux conserver l’opération. Sur une copie locale de test, tu peux aussi annuler leur opération sans désinstaller les modules déjà actifs.
                  </span>
                  <span>
                    Les modules qui en dépendent seront détectés et exclus automatiquement de cette mise à jour locale afin de conserver un ensemble cohérent.
                  </span>
                </div>
              </div>
              <label className="flex cursor-pointer items-center gap-3 rounded-md border border-red-200 bg-white px-3 py-2 font-medium dark:border-red-800 dark:bg-red-950/55">
                <Checkbox
                  color="red"
                  checked={someMissingPendingModulesSelected ? "indeterminate" : allMissingPendingModulesSelected}
                  disabled={loading}
                  onCheckedChange={(checked) => toggleAllMissingModulesToIgnore(checked === true)}
                />
                Tout sélectionner ({pendingModulesWithMissingCode.length})
              </label>
              <div className="max-h-48 space-y-2 overflow-y-auto rounded-md border border-red-200 bg-white p-2 dark:border-red-800 dark:bg-red-950/55">
                {pendingModulesWithMissingCode.map((module) => (
                  <label key={module.name} className="flex cursor-pointer items-center gap-3 rounded px-2 py-2 hover:bg-red-50 dark:hover:bg-red-900/50">
                    <Checkbox
                      color="red"
                      checked={missingModulesToIgnore.has(module.name)}
                      onCheckedChange={(checked) => toggleMissingModuleToIgnore(module.name, checked === true)}
                    />
                    <span className="min-w-0 flex-1 break-all font-mono text-xs">{module.name}</span>
                    <Badge className="shrink-0" variant="destructive">{module.state} · code absent</Badge>
                  </label>
                ))}
              </div>
              <Button
                variant="destructive"
                disabled={!missingModulesToIgnore.size || loading}
                onClick={ignoreSelectedMissingModulesLocally}
              >
                {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <PackageX className="h-4 w-4" />}
                Annuler localement {missingModulesToIgnore.size || "la sélection"} opération(s)
              </Button>
              <p className="text-xs text-red-800 dark:text-red-200">
                Cette action ne désinstalle aucun module et ne supprime aucune donnée. Le détail des exclusions automatiques apparaîtra dans les logs.
              </p>
            </div>
          ) : null}
          {updateScope === "all" && updateFilestoreStatus && updateFilestoreStatus.missing > 0 ? (
            <div className="grid gap-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950/45 dark:text-amber-100">
              <div className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <div className="grid gap-1">
                  <span className="font-medium">Filestore incomplet</span>
                  <span>
                    {updateFilestoreStatus.missing.toLocaleString("fr-FR")} fichier(s) manquent. Leur téléchargement n&apos;est pas nécessaire pour
                    mettre à jour les modules : aucune référence ne sera supprimée, mais les médias absents resteront indisponibles.
                  </span>
                </div>
              </div>
              <label className="flex cursor-pointer items-start gap-3 rounded-md border border-amber-300 bg-white p-3 hover:bg-amber-100/60 dark:border-amber-800 dark:bg-amber-950/55 dark:hover:bg-amber-900/50">
                <Checkbox
                  className="mt-0.5"
                  checked={allowMissingFilestore}
                  onCheckedChange={(checked) => setAllowMissingFilestore(checked === true)}
                />
                <span className="font-medium">Continuer sans télécharger le filestore</span>
              </label>
            </div>
          ) : null}
          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <Button variant="outline" onClick={() => setUpdateAllDialogOpen(false)}>
              Annuler
            </Button>
            <Button
              disabled={
                !selectedProjectReady ||
                !canUseDb ||
                loading ||
                checkingUpdatePrerequisites ||
                Boolean(updateScope === "all" && pendingModulesWithMissingCode.length) ||
                Boolean(updateScope === "all" && updateFilestoreStatus?.missing && !allowMissingFilestore)
              }
              onClick={confirmUpdateAllOdooModules}
            >
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCcw className="h-4 w-4" />}
              {updateScope === "imported" && detectedImportedModules.length ? "Traiter les modules importés" : "Lancer la MAJ complète"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <Dialog open={uninstallDialogOpen} onOpenChange={setUninstallDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Désinstaller les modules</DialogTitle>
            <DialogDescription>
              Cette action désinstalle les modules de la base {selectedDb || "sélectionnée"}. Les dossiers addons et les liens symboliques ne seront pas
              supprimés.
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-52 overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-xs">
            {pendingUninstallModules.map((name) => (
              <div key={name}>{name}</div>
            ))}
          </div>
          <Button variant="destructive" disabled={!pendingUninstallModules.length || loading} onClick={confirmUninstall}>
            <Trash2 className="h-4 w-4" />
            Confirmer la désinstallation
          </Button>
        </DialogContent>
      </Dialog>

      <Dialog open={deleteCodeDialogOpen} onOpenChange={setDeleteCodeDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Supprimer les modules du projet</DialogTitle>
            <DialogDescription>
              Cette action retire les modules de `odoo/addons` et supprime le dossier géré dans `odoo/addons-store`.
              Les anciens imports encore liés depuis `.odoo_manager_imports` restent aussi nettoyés.
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-52 overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-xs">
            {pendingDeleteCodeModules.map((name) => (
              <div key={name}>{name}</div>
            ))}
          </div>
          <label className="flex items-start gap-2 rounded-md border bg-muted/40 p-3 text-sm">
            <Checkbox
              className="mt-1"
              checked={deleteCodeUninstallFirst}
              disabled={!canUseDb}
              onCheckedChange={(checked) => setDeleteCodeUninstallFirst(checked === true)}
            />
            <span>
              <span className="block font-medium">Désinstaller de la base avant suppression</span>
              <span className="block text-xs text-muted-foreground">
                Recommandé si la base sélectionnée contient encore le module installé.
              </span>
            </span>
          </label>
          <Button variant="destructive" disabled={!pendingDeleteCodeModules.length || loading} onClick={confirmDeleteCode}>
            <PackageX className="h-4 w-4" />
            Confirmer la suppression du projet
          </Button>
        </DialogContent>
      </Dialog>

      <div className="fixed bottom-4 left-4 right-4 z-50 grid gap-2 sm:left-auto sm:w-96">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={cn(
              "w-full break-words rounded-md border bg-card p-3 text-sm shadow-lg",
              toast.kind === "error" && "border-red-200 bg-red-50 text-red-800 dark:border-red-800 dark:bg-red-950 dark:text-red-200",
              toast.kind === "success" && "border-emerald-200 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950 dark:text-emerald-200",
            )}
          >
            {toast.message}
          </div>
        ))}
      </div>
    </main>
  );
}

function PrerequisiteRow({
  ready,
  icon: Icon,
  title,
  detail,
  action,
}: {
  ready: boolean;
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  detail: string;
  action?: React.ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-4 p-4 sm:flex-row sm:items-center">
      <div className="flex min-w-0 flex-1 items-start gap-4">
        <div className={cn("mt-0.5 rounded-md p-2", ready ? "bg-emerald-50 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300" : "bg-amber-50 text-amber-700 dark:bg-amber-950 dark:text-amber-300")}>
          <Icon className="h-4 w-4" />
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2 font-medium">
            {title}
            {ready ? <CheckCircle2 className="h-4 w-4 text-emerald-600" /> : <AlertTriangle className="h-4 w-4 text-amber-600" />}
          </div>
          <div className="mt-0.5 break-words text-xs leading-5 text-muted-foreground">{detail}</div>
        </div>
      </div>
      {action && <div className="shrink-0 pl-12 sm:pl-0">{action}</div>}
    </div>
  );
}

function CreateProjectDialog({
  open,
  onOpenChange,
  prerequisites,
  dockerReady,
  loading,
  onRefreshPrerequisites,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  prerequisites: ProjectCreationPrerequisites | null;
  dockerReady: boolean;
  loading: boolean;
  onRefreshPrerequisites: () => Promise<ProjectCreationPrerequisites | null>;
  onSubmit: (payload: Record<string, unknown>) => Promise<void>;
}) {
  const [name, setName] = useState("");
  const [version, setVersion] = useState("19.0");
  const [sourceType, setSourceType] = useState<"standard" | "gitlab" | "rika">("standard");
  const [repositoryUrl, setRepositoryUrl] = useState("");
  const [repositoryBranch, setRepositoryBranch] = useState("master");
  const [rikaInstance, setRikaInstance] = useState("");
  const [rikaLogin, setRikaLogin] = useState("");
  const [rikaPassword, setRikaPassword] = useState("");
  const [startAfterCreation, setStartAfterCreation] = useState(true);

  useEffect(() => {
    if (!open) {
      setRikaPassword("");
      return;
    }
    setStartAfterCreation(dockerReady);
    if (prerequisites?.supported_versions?.length && !prerequisites.supported_versions.includes(version)) {
      setVersion(prerequisites.supported_versions.at(-1) || "19.0");
    }
  }, [dockerReady, open, prerequisites?.supported_versions, version]);

  const prerequisitesReady = Boolean(
    prerequisites?.workspace_ready && prerequisites.git_available && prerequisites.ssh_key_present,
  );
  const sourceFieldsReady = sourceType === "standard"
    || (sourceType === "gitlab" && Boolean(repositoryUrl.trim() && repositoryBranch.trim()))
    || (sourceType === "rika" && Boolean(rikaInstance.trim() && rikaLogin.trim() && rikaPassword));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
        <DialogHeader>
          <DialogTitle>Créer un projet Odoo local</DialogTitle>
          <DialogDescription>
            Le gestionnaire prépare Odoo, Enterprise, Docker et les liens d’addons sans ouvrir de terminal.
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-5">
          <div className="grid items-start gap-3 sm:grid-cols-2">
            <div className="grid content-start gap-1.5 text-sm font-medium">
              <label htmlFor="new-project-name">Nom du projet</label>
              <Input
                id="new-project-name"
                value={name}
                maxLength={63}
                onChange={(event) => setName(event.target.value)}
                placeholder="CLIENT_V19"
                autoFocus
              />
              <span className="min-h-4 text-xs font-normal text-muted-foreground">Lettres, chiffres, tirets, points et underscores.</span>
            </div>
            <div className="grid content-start gap-1.5 text-sm font-medium">
              <label htmlFor="new-project-version">Version Odoo</label>
              <Select value={version} onValueChange={setVersion} disabled={sourceType === "rika"}>
                <SelectTrigger id="new-project-version"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {(prerequisites?.supported_versions || ["15.0", "16.0", "17.0", "18.0", "19.0"]).map((item) => (
                    <SelectItem key={item} value={item}>Odoo {item}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <span className="min-h-4 text-xs font-normal text-muted-foreground">
                {sourceType === "rika" ? "Détectée automatiquement dans la copie RIKA." : " "}
              </span>
            </div>
          </div>

          <fieldset className="grid gap-2">
            <legend className="mb-1 text-sm font-medium">Source du projet</legend>
            <div className="grid gap-2 sm:grid-cols-3">
              <InteractiveCard
                className={cn(
                  "min-h-20 p-3",
                  sourceType === "standard" && "border-primary bg-primary/5",
                )}
                onClick={() => setSourceType("standard")}
              >
                <span className="block font-medium">Odoo standard</span>
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">Odoo Community et Enterprise Sudokeys.</span>
              </InteractiveCard>
              <InteractiveCard
                className={cn(
                  "min-h-20 p-3",
                  sourceType === "gitlab" && "border-primary bg-primary/5",
                )}
                onClick={() => setSourceType("gitlab")}
              >
                <span className="block font-medium">Dépôt d’addons GitLab</span>
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">Ajoute le dépôt client au socle standard.</span>
              </InteractiveCard>
              <InteractiveCard
                className={cn(
                  "min-h-20 p-3",
                  sourceType === "rika" && "border-primary bg-primary/5",
                )}
                onClick={() => setSourceType("rika")}
              >
                <span className="block font-medium">Copie depuis RIKA</span>
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">Récupère une instance et détecte sa version Odoo.</span>
              </InteractiveCard>
            </div>
          </fieldset>

          {sourceType === "gitlab" && (
            <div className="grid gap-3 border-l-2 border-primary pl-4 sm:grid-cols-[minmax(0,1fr)_10rem]">
              <label className="grid min-w-0 gap-1.5 text-sm font-medium">
                URL SSH du dépôt d’addons
                <Input
                  value={repositoryUrl}
                  onChange={(event) => setRepositoryUrl(event.target.value)}
                  placeholder="ssh://git@gitlab.sudokeys.com:10022/sudokeys/client-addons.git"
                />
              </label>
              <label className="grid gap-1.5 text-sm font-medium">
                Branche
                <Input value={repositoryBranch} onChange={(event) => setRepositoryBranch(event.target.value)} placeholder="master" />
              </label>
            </div>
          )}

          {sourceType === "rika" && (
            <div className="grid gap-3 border-l-2 border-primary pl-4 sm:grid-cols-2">
              <label className="grid min-w-0 gap-1.5 text-sm font-medium sm:col-span-2">
                Instance RIKA
                <Input
                  value={rikaInstance}
                  onChange={(event) => setRikaInstance(event.target.value)}
                  placeholder="prod01"
                />
              </label>
              <label className="grid min-w-0 gap-1.5 text-sm font-medium">
                Identifiant Sudokeys
                <Input
                  value={rikaLogin}
                  onChange={(event) => setRikaLogin(event.target.value)}
                  autoComplete="username"
                />
              </label>
              <label className="grid min-w-0 gap-1.5 text-sm font-medium">
                Mot de passe
                <Input
                  type="password"
                  value={rikaPassword}
                  onChange={(event) => setRikaPassword(event.target.value)}
                  autoComplete="current-password"
                />
              </label>
              <p className="text-xs leading-5 text-muted-foreground sm:col-span-2">
                Ces identifiants sont transmis uniquement à RIKA pendant cette création et ne sont pas enregistrés par le gestionnaire.
              </p>
            </div>
          )}

          <label className={cn("flex items-start gap-3 rounded-md border p-3 text-sm", !dockerReady && "bg-muted/40")}>
            <Checkbox
              className="mt-0.5"
              checked={startAfterCreation}
              disabled={!dockerReady}
              onCheckedChange={(checked) => setStartAfterCreation(checked === true)}
            />
            <span>
              <span className="block font-medium">Démarrer le projet après la création</span>
              <span className="block text-xs leading-5 text-muted-foreground">
                {dockerReady
                  ? "Traefik sera installé automatiquement s’il manque."
                  : "Docker n’est pas démarré. Le projet pourra être créé puis démarré plus tard."}
              </span>
            </span>
          </label>

          {!prerequisitesReady && (
            <div className="flex flex-col gap-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950 dark:border-amber-800 dark:bg-amber-950/45 dark:text-amber-100 sm:flex-row sm:items-center sm:justify-between">
              <div className="flex min-w-0 items-start gap-2">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                <span>Git, le workspace et une clé SSH publique sont requis pour récupérer les dépôts privés.</span>
              </div>
              <Button size="sm" variant="outline" onClick={() => void onRefreshPrerequisites()}>
                <RefreshCcw className="h-4 w-4" />
                Revérifier
              </Button>
            </div>
          )}

          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
            <Button variant="outline" onClick={() => onOpenChange(false)}>Annuler</Button>
            <Button
              disabled={loading || !name.trim() || !prerequisitesReady || !sourceFieldsReady}
              onClick={() => onSubmit({
                name: name.trim(),
                version,
                source_type: sourceType,
                repository_url: repositoryUrl.trim(),
                repository_branch: repositoryBranch.trim(),
                rika_instance: rikaInstance.trim(),
                rika_login: rikaLogin.trim(),
                rika_password: rikaPassword,
                start_after_creation: startAfterCreation,
              })}
            >
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <FolderPlus className="h-4 w-4" />}
              Créer le projet
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

function CreateDatabaseDialog({
  open,
  onOpenChange,
  project,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  project?: Project;
  onSubmit: (payload: Record<string, unknown>) => Promise<void>;
}) {
  const [db, setDb] = useState("");
  const [masterPwd, setMasterPwd] = useState("odoo");
  const [login, setLogin] = useState("admin");
  const [password, setPassword] = useState("admin");
  const [lang, setLang] = useState("fr_FR");
  const [country, setCountry] = useState("FR");
  const [demo, setDemo] = useState(false);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl space-y-5">
        <DialogHeader>
          <DialogTitle>Créer une base Odoo</DialogTitle>
          <DialogDescription>{project ? `Projet cible : ${project.name}` : "Sélectionne un projet."}</DialogDescription>
        </DialogHeader>
        <div className="grid gap-x-4 gap-y-4 md:grid-cols-2">
          <label className="grid gap-1.5 text-sm font-medium">
            Nom de base
            <Input value={db} onChange={(event) => setDb(event.target.value)} placeholder="ma_base_locale" />
          </label>
          <label className="grid gap-1.5 text-sm font-medium">
            Master password
            <Input value={masterPwd} onChange={(event) => setMasterPwd(event.target.value)} type="password" />
          </label>
          <label className="grid gap-1.5 text-sm font-medium">
            Login admin
            <Input value={login} onChange={(event) => setLogin(event.target.value)} />
          </label>
          <label className="grid gap-1.5 text-sm font-medium">
            Mot de passe admin
            <Input value={password} onChange={(event) => setPassword(event.target.value)} type="password" />
          </label>
          <label className="grid gap-1.5 text-sm font-medium">
            Langue
            <Input value={lang} onChange={(event) => setLang(event.target.value)} />
          </label>
          <label className="grid gap-1.5 text-sm font-medium">
            Pays
            <Input value={country} onChange={(event) => setCountry(event.target.value.toUpperCase())} />
          </label>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <Checkbox checked={demo} onCheckedChange={(checked) => setDemo(checked === true)} />
          Charger les données de démonstration
        </label>
        <Button
          disabled={!project || !db}
          onClick={() =>
            onSubmit({
              project: project?.name,
              db,
              master_pwd: masterPwd,
              login,
              password,
              lang,
              country,
              demo,
            })
          }
        >
          <Database className="h-4 w-4" />
          Créer la base
        </Button>
      </DialogContent>
    </Dialog>
  );
}

function RestoreDatabaseDialog({
  open,
  onOpenChange,
  project,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  project?: Project;
  onSubmit: (
    payload: RestoreDatabasePayload,
    onProgress: (progress: number) => void,
  ) => Promise<boolean>;
}) {
  const [db, setDb] = useState("");
  const [masterPwd, setMasterPwd] = useState("odoo");
  const [neutralize, setNeutralize] = useState(true);
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [progress, setProgress] = useState(0);

  useEffect(() => {
    setDb("");
    setFile(null);
    setProgress(0);
  }, [project?.name]);

  async function submit() {
    if (!project || !file || !db.trim() || !masterPwd) return;
    setSubmitting(true);
    setProgress(0);
    const successful = await onSubmit(
      {
        project: project.name,
        db: db.trim(),
        masterPwd,
        copy: true,
        neutralize,
        file,
      },
      setProgress,
    );
    if (successful) {
      setDb("");
      setFile(null);
      setProgress(0);
    }
    setSubmitting(false);
  }

  return (
    <Dialog open={open} onOpenChange={(nextOpen) => !submitting && onOpenChange(nextOpen)}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Restaurer une sauvegarde Odoo</DialogTitle>
          <DialogDescription>
            {project
              ? `Le ZIP sera restauré dans le projet ${project.name} sans ouvrir le gestionnaire de bases Odoo.`
              : "Sélectionne un projet."}
          </DialogDescription>
        </DialogHeader>

        <div className="grid gap-4">
          <div className="grid min-w-0 gap-1.5 text-sm font-medium">
            <label htmlFor="database-backup-file">Sauvegarde ZIP Odoo</label>
            <FilePicker
              id="database-backup-file"
              accept=".zip,application/zip"
              file={file}
              buttonLabel="Choisir une sauvegarde"
              disabled={submitting}
              onChange={(event) => setFile(event.target.files?.[0] || null)}
            />
            {file && (
              <span className="break-all text-xs font-normal text-muted-foreground">
                {file.name} · {(file.size / (1024 * 1024)).toLocaleString("fr-FR", { maximumFractionDigits: 1 })} Mo
              </span>
            )}
          </div>

          <div className="grid gap-3 md:grid-cols-2">
            <label className="grid gap-1.5 text-sm font-medium">
              Nom de la nouvelle base
              <Input
                value={db}
                disabled={submitting}
                onChange={(event) => setDb(event.target.value)}
                placeholder="client_recette"
              />
            </label>
            <label className="grid gap-1.5 text-sm font-medium">
              Master password
              <Input
                value={masterPwd}
                disabled={submitting}
                onChange={(event) => setMasterPwd(event.target.value)}
                type="password"
              />
            </label>
          </div>

          <label className="flex items-start gap-3 rounded-md border bg-muted/35 p-3 text-sm">
            <Checkbox
              className="mt-0.5"
              checked={neutralize}
              disabled={submitting}
              onCheckedChange={(checked) => setNeutralize(checked === true)}
            />
            <span>
              <span className="block font-medium">Neutraliser la base pour les tests</span>
              <span className="mt-0.5 block text-xs text-muted-foreground">
                Recommandé en local : désactive notamment les envois d’e-mails et les actions externes. La restauration est toujours déclarée comme une copie.
              </span>
            </span>
          </label>

          {submitting && (
            <div className="grid gap-2" aria-live="polite">
              <div className="flex items-center justify-between gap-3 text-sm">
                <span className="font-medium">Téléversement vers le gestionnaire</span>
                <span className="tabular-nums text-muted-foreground">{progress} %</span>
              </div>
              <div
                className="h-2 overflow-hidden rounded-full bg-muted"
                role="progressbar"
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={progress}
              >
                <div
                  className="h-full rounded-full bg-primary transition-[width] duration-200 ease-out"
                  style={{ width: `${progress}%` }}
                />
              </div>
              {progress === 100 && (
                <p className="text-xs text-muted-foreground">Validation du ZIP et démarrage de la restauration…</p>
              )}
            </div>
          )}

          <Button
            className="w-full"
            disabled={!project || !file || !db.trim() || !masterPwd || submitting}
            onClick={submit}
          >
            {submitting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Upload className="h-4 w-4" />}
            {submitting ? "Préparation de la restauration…" : "Restaurer la base"}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
