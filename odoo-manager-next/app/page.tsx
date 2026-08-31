"use client";

import {
  Activity,
  AlertTriangle,
  Boxes,
  CheckCircle2,
  Circle,
  CloudDownload,
  Copy,
  Database,
  ExternalLink,
  FileArchive,
  FolderOpen,
  FolderPlus,
  GitBranch,
  KeyRound,
  ListRestart,
  Loader2,
  Logs,
  PackageX,
  Play,
  PlusCircle,
  RefreshCcw,
  Search,
  Settings,
  Square,
  Terminal,
  Trash2,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";

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
  start_project_before_open: boolean;
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
  status: "running" | "done" | "error" | string;
  started_at: string;
  finished_at?: string | null;
  lines: string[];
  output?: string;
};

type ModuleInfo = {
  name: string;
  title: string;
  state: string;
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

const API_BASE = process.env.NEXT_PUBLIC_ODOO_MANAGER_API?.replace(/\/$/, "") || "";
const TAURI_API_RETRY_DELAYS_MS = [0, 250, 750, 1500, 2500];
const BOOTSTRAP_RETRY_DELAYS_MS = [0, 500, 1000, 2000];
const DOCKER_CONFIRM_DELAY_MS = 700;
const API_TIMEOUT_MS = 20_000;
const UPLOAD_TIMEOUT_MS = 120_000;

class ApiUnavailableError extends Error {
  constructor(message = "Service local Odoo Manager indisponible. L'application n'arrive pas à joindre l'API locale sur 127.0.0.1:8765.") {
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

function delay(milliseconds: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, milliseconds));
}

function isTauriRuntime() {
  return typeof window !== "undefined" && Boolean((window as Window & { __TAURI_INTERNALS__?: unknown }).__TAURI_INTERNALS__);
}

async function invokeDesktop<T>(command: string, args?: Record<string, unknown>) {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(command, args);
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

function statusDot(status: string) {
  if (status === "running" || status === "healthy") return "bg-emerald-500";
  if (status === "exited" || status === "created") return "bg-amber-400";
  if (status === "error") return "bg-red-500";
  return "bg-slate-400";
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
    start_project_before_open: current?.start_project_before_open ?? false,
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
  const [moduleSearch, setModuleSearch] = useState("");
  const [moduleFilter, setModuleFilter] = useState("all");
  const [selectedModules, setSelectedModules] = useState<Set<string>>(new Set());
  const [jobs, setJobs] = useState<Job[]>([]);
  const [selectedJobId, setSelectedJobId] = useState<number | null>(null);
  const [externalLogView, setExternalLogView] = useState<{ title: string; content: string } | null>(null);
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
  const [zipDialogOpen, setZipDialogOpen] = useState(false);
  const [createDbOpen, setCreateDbOpen] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [updateAllDialogOpen, setUpdateAllDialogOpen] = useState(false);
  const [updateFilestoreStatus, setUpdateFilestoreStatus] = useState<FilestoreStatus | null>(null);
  const [updatePendingModules, setUpdatePendingModules] = useState<PendingModuleOperation[]>([]);
  const [updateLocalExcludedModules, setUpdateLocalExcludedModules] = useState<string[]>([]);
  const [missingModulesToIgnore, setMissingModulesToIgnore] = useState<Set<string>>(new Set());
  const [allowMissingFilestore, setAllowMissingFilestore] = useState(false);
  const [checkingUpdatePrerequisites, setCheckingUpdatePrerequisites] = useState(false);
  const [uninstallDialogOpen, setUninstallDialogOpen] = useState(false);
  const [deleteCodeDialogOpen, setDeleteCodeDialogOpen] = useState(false);
  const [replaceZipModules, setReplaceZipModules] = useState(true);
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
  const modulesRequestGeneration = useRef(0);
  const zipInspectionGeneration = useRef(0);
  const scheduledTimeouts = useRef<Set<number>>(new Set());
  const onboardingPrompted = useRef(false);
  const logOutputRef = useRef<HTMLPreElement>(null);
  const logAutoFollow = useRef(true);
  const lastLogOutputSource = useRef("");

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
  const odooDatabases = useMemo(
    () => (selectedProject?.databases || []).filter((database) => database !== "postgres"),
    [selectedProject],
  );

  const selectedJob = useMemo(() => jobs.find((job) => job.id === selectedJobId) || jobs[0], [jobs, selectedJobId]);
  const gitInstallRunning = jobs.some((job) => job.status === "running" && job.title === "Installer Git pour Windows");
  const traefikInstallRunning = jobs.some((job) => job.status === "running" && job.title === "Installer Traefik");
  const selectedSshKey = useMemo(
    () => sshKeys.find((key) => key.name === selectedSshKeyName) || sshKeys[0] || null,
    [selectedSshKeyName, sshKeys],
  );

  const filteredProjects = useMemo(() => {
    const query = projectsFilter.trim().toLowerCase();
    return (overview?.projects || []).filter((project) => !query || project.name.toLowerCase().includes(query));
  }, [overview, projectsFilter]);

  const filteredModules = useMemo(() => {
    const query = moduleSearch.trim().toLowerCase();
    return modules
      .filter((module) => !query || module.name.toLowerCase().includes(query))
      .filter((module) => moduleFilter === "all" || module.state === moduleFilter)
      .slice(0, 300);
  }, [modules, moduleFilter, moduleSearch]);

  const moduleByName = useMemo(() => new Map(modules.map((module) => [module.name, module])), [modules]);
  const filteredModuleNames = useMemo(() => filteredModules.map((module) => module.name), [filteredModules]);
  const selectedFilteredModuleCount = useMemo(
    () => filteredModuleNames.filter((name) => selectedModules.has(name)).length,
    [filteredModuleNames, selectedModules],
  );
  const allFilteredModulesSelected = filteredModuleNames.length > 0 && selectedFilteredModuleCount === filteredModuleNames.length;
  const someFilteredModulesSelected = selectedFilteredModuleCount > 0 && !allFilteredModulesSelected;
  const fallbackDockerGuide = useMemo(() => offlineDockerGuide(), []);

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
  }, [schedule]);

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
    setJobs(payload.jobs);
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
  }, [markApiSuccess]);

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

  const refreshOverview = useCallback(async () => {
    if (overviewRefreshInFlight.current) return;
    overviewRefreshInFlight.current = true;
    try {
      const payload = await api<Overview>("/api/overview");
      setOverview(payload);
      markApiSuccess();
      setError("");
      const current = payload.projects.find((project) => project.name === selectedProjectName) || payload.projects[0];
      if (current && current.name !== selectedProjectName) {
        setSelectedProjectName(current.name);
        setSelectedDb(firstOdooDatabase(current));
      }
    } catch (err) {
      markApiFailure(err);
      setError(!initializingRef.current && !(err instanceof ApiUnavailableError) ? err instanceof Error ? err.message : "Impossible de charger l'overview." : "");
    } finally {
      overviewRefreshInFlight.current = false;
    }
  }, [markApiFailure, markApiSuccess, selectedProjectName]);

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

  const refreshJobs = useCallback(async () => {
    if (jobsRefreshInFlight.current) return;
    jobsRefreshInFlight.current = true;
    try {
      const payload = await api<{ jobs: Job[] }>("/api/jobs");
      setJobs(payload.jobs);
      markApiSuccess();
      if (!selectedJobId && payload.jobs[0]) setSelectedJobId(payload.jobs[0].id);
    } catch (err) {
      markApiFailure(err);
      // Jobs polling should not break the whole screen.
    } finally {
      jobsRefreshInFlight.current = false;
    }
  }, [markApiFailure, markApiSuccess, selectedJobId]);

  const refreshModules = useCallback(async () => {
    const projectName = selectedProject?.name;
    const generation = ++modulesRequestGeneration.current;
    if (!projectName || !selectedDb || selectedDb === "postgres") {
      setModules([]);
      return;
    }
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
    }
  }, [pushToast, selectedDb, selectedProject?.name]);

  useEffect(() => () => {
    for (const timeout of scheduledTimeouts.current) window.clearTimeout(timeout);
    scheduledTimeouts.current.clear();
  }, []);

  useEffect(() => {
    setDesktopRuntime(isTauriRuntime());
    void initializeApplication();
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
    const timer = window.setInterval(() => {
      refreshOverview();
      refreshJobs();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [initializing, refreshJobs, refreshOverview]);

  useEffect(() => {
    if (initializing) return;
    const interval = Math.max(3, settings?.docker_poll_interval || 10) * 1000;
    const timer = window.setInterval(refreshSystemStatus, interval);
    return () => window.clearInterval(timer);
  }, [initializing, refreshSystemStatus, settings?.docker_poll_interval]);

  useEffect(() => {
    if (selectedProject) {
      setSelectedDb((current) => current !== "postgres" && selectedProject.databases?.includes(current) ? current : firstOdooDatabase(selectedProject));
    }
  }, [selectedProject]);

  useEffect(() => {
    refreshModules();
  }, [refreshModules]);

  async function createJob(action: string, payload: Record<string, unknown> = {}) {
    setLoading(true);
    try {
      const result = await api<{ job: Job }>("/api/jobs", {
        method: "POST",
        body: JSON.stringify({ action, ...payload }),
      });
      setSelectedJobId(result.job.id);
      setExternalLogView(null);
      enableLogAutoFollow();
      pushToast("success", `Action lancée : ${result.job.title}`);
      await refreshJobs();
      return result.job;
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Action impossible.");
      return null;
    } finally {
      setLoading(false);
    }
  }

  async function waitForJob(jobId: number, timeoutMilliseconds = 960000) {
    const deadline = Date.now() + timeoutMilliseconds;
    while (Date.now() < deadline) {
      const payload = await api<{ jobs: Job[] }>("/api/jobs");
      setJobs(payload.jobs);
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

  async function saveSettings() {
    if (!settingsDraft) return;
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
      pushToast("success", "Paramètres enregistrés.");
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

  async function requestUpdateLocalModules() {
    const db = selectedDatabaseOrNotify("la MAJ addons projet");
    if (!db || !selectedProject) return;
    await createJob("update_local_modules", { project: selectedProject.name, db });
    schedule(refreshModules, 2500);
  }

  async function requestUpdateAllOdooModules() {
    const db = selectedDatabaseOrNotify("la MAJ complète Odoo");
    if (!db || !selectedProject) return;
    setAllowMissingFilestore(false);
    setUpdateFilestoreStatus(null);
    setUpdatePendingModules([]);
    setUpdateLocalExcludedModules([]);
    setMissingModulesToIgnore(new Set());
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
      schedule(refreshModules, 1500);
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
    const job = await createJob("update_all_modules", {
      project: selectedProject.name,
      db,
      allow_missing_filestore: allowMissingFilestore,
    });
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

  async function showLogs() {
    if (!selectedProject) return;
    try {
      const payload = await api<{ logs: string }>(`/api/projects/${encodeURIComponent(selectedProject.name)}/logs`);
      setExternalLogView({
        title: `Logs Odoo - ${selectedProject.name}`,
        content: payload.logs || "Aucun log.",
      });
      enableLogAutoFollow();
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Logs indisponibles.");
    }
  }

  async function showDiagnostics() {
    if (!selectedProject) return;
    try {
      const payload = await api<ProjectDiagnostics>(`/api/projects/${encodeURIComponent(selectedProject.name)}/diagnostics`);
      setExternalLogView({
        title: `Diagnostic - ${selectedProject.name}`,
        content: formatDiagnostics(payload),
      });
      enableLogAutoFollow();
      pushToast("info", "Diagnostic projet chargé.");
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Diagnostic indisponible.");
    }
  }

  async function clearJobs() {
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
        setSelectedJobId(null);
        setExternalLogView(null);
      }
      await refreshJobs();
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Suppression de l'entrée impossible.");
    }
  }

  function selectJob(jobId: number) {
    setExternalLogView(null);
    setSelectedJobId(jobId);
    enableLogAutoFollow();
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
    const removable = moduleNames.filter((name) => modules.find((module) => module.name === name)?.removable);
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

  function resetZipImport() {
    zipInspectionGeneration.current += 1;
    setZipModuleCandidates([]);
    setSelectedZipModules(new Set());
    setInspectingZip(false);
    if (zipInputRef.current) zipInputRef.current.value = "";
  }

  async function inspectZipFile(file?: File) {
    const generation = ++zipInspectionGeneration.current;
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
  const selectedRemovableModuleList = useMemo(
    () => selectedModuleList.filter((name) => moduleByName.get(name)?.removable),
    [moduleByName, selectedModuleList],
  );
  const selectedProjectReady = Boolean(selectedProject);
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
  const outputTitle = externalLogView?.title || selectedJob?.title || "Aucune action sélectionnée";
  const outputContent = externalLogView?.content || selectedJob?.output || selectedJob?.lines?.join("\n") || "Aucune sortie.";
  const outputSource = externalLogView ? `external:${externalLogView.title}` : `job:${selectedJob?.id || "none"}`;

  useEffect(() => {
    if (activeTab !== "logs") return;
    if (lastLogOutputSource.current !== outputSource) {
      lastLogOutputSource.current = outputSource;
      logAutoFollow.current = true;
    }
    if (logAutoFollow.current) scrollLogOutputToBottom();
  }, [activeTab, outputContent, outputSource, scrollLogOutputToBottom]);

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
      <main className="grid min-h-screen place-items-center bg-background px-6">
        <div className="w-full max-w-md rounded-lg border bg-card p-6 text-center shadow-sm">
          {initializationError ? (
            <AlertTriangle className="mx-auto h-8 w-8 text-amber-600" />
          ) : (
            <Loader2 className="mx-auto h-8 w-8 animate-spin text-primary" />
          )}
          <h1 className="mt-4 text-lg font-semibold">Chargement du gestionnaire</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            {initializationMessage}
          </p>
          {initializationError && (
            <div className="mt-4 space-y-3">
              <p className="break-words rounded-md border border-amber-200 bg-amber-50 p-3 text-left text-xs text-amber-900">
                {initializationError}
              </p>
              {backendDiagnostics && (
                <details className="rounded-md border bg-muted/40 p-3 text-left text-xs">
                  <summary className="cursor-pointer font-medium">Détails techniques</summary>
                  <div className="mt-2 break-all text-muted-foreground">Journal : {backendDiagnostics.log_path}</div>
                  <pre className="mt-2 max-h-40 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-950 p-2 text-[11px] text-slate-100">
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
    <main className="min-h-screen overflow-x-hidden">
      <div className="flex min-h-screen min-w-0 flex-col lg:flex-row">
        <aside className="min-w-0 border-b bg-card lg:sticky lg:top-0 lg:h-screen lg:w-80 lg:flex-none lg:border-b-0 lg:border-r">
          <div className="flex h-full flex-col">
            <div className="border-b p-4">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <h1 className="truncate text-lg font-semibold">Gestionnaire Odoo</h1>
                  <p className="mt-1 max-w-full truncate text-xs text-muted-foreground" title={overview?.workspace || "Workspace local"}>
                    {overview?.workspace || "Workspace local"}
                  </p>
                </div>
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
            <div className="min-h-0 max-h-[45vh] flex-1 overflow-auto p-2 sm:max-h-[50vh] lg:max-h-none">
              {filteredProjects.map((project) => (
                <button
                  key={project.name}
                  className={cn(
                    "mb-1 w-full rounded-md border p-3 text-left transition-colors hover:bg-muted",
                    selectedProject?.name === project.name ? "border-primary bg-primary/8 shadow-sm" : "border-transparent",
                  )}
                  onClick={() => {
                    setSelectedProjectName(project.name);
                    setSelectedDb(firstOdooDatabase(project));
                    setExternalLogView(null);
                  }}
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="truncate font-semibold">{project.name}</div>
                      <div className="mt-1 text-xs text-muted-foreground">
                        {project.odoo_version ? `Odoo ${project.odoo_version}` : "Version inconnue"}
                      </div>
                    </div>
                    <div className="flex shrink-0 items-center gap-1.5 whitespace-nowrap text-xs text-muted-foreground">
                      <span className={cn("h-2 w-2 rounded-full", statusDot(project.odoo_status))} />
                      {project.odoo_status}
                    </div>
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2 text-xs">
                    <Badge variant={statusVariant(project.postgres_status)}>PostgreSQL {project.postgres_status}</Badge>
                    <Badge variant="outline">{project.databases?.filter((db) => db !== "postgres").length || 0} base(s)</Badge>
                  </div>
                </button>
              ))}
            </div>
            <div className="grid gap-2 border-t p-3">
              <Button className="w-full" variant="outline" onClick={openCreateProjectDialog}>
                <FolderPlus className="h-4 w-4" />
                Nouveau projet
              </Button>
              <Button
                className="w-full"
                variant="ghost"
                onClick={openSettingsDialog}
              >
                <Settings className="h-4 w-4" />
                Paramètres
              </Button>
            </div>
          </div>
        </aside>

        <section className="min-w-0 flex-1">
          <header className="border-b bg-card">
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
              <div className="flex w-full shrink-0 flex-wrap gap-2 xl:w-auto xl:max-w-[660px] xl:justify-end">
                <Button className="w-full sm:w-auto" variant="outline" onClick={refreshAllViews}>
                  <RefreshCcw className="h-4 w-4" />
                  Actualiser
                </Button>
                <Button
                  className="w-full sm:w-auto"
                  disabled={!selectedProjectReady || loading || Boolean(selectedProjectLifecycleJob)}
                  onClick={requestStartProject}
                >
                  {selectedProjectStarting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                  {selectedProjectStarting ? "Démarrage…" : "Démarrer"}
                </Button>
                <Button
                  className="w-full sm:w-auto"
                  variant="outline"
                  disabled={!selectedProjectReady || !selectedProjectHasContainers || loading || Boolean(selectedProjectLifecycleJob)}
                  onClick={requestStopProject}
                >
                  {selectedProjectStopping ? <Loader2 className="h-4 w-4 animate-spin" /> : <Square className="h-4 w-4" />}
                  {selectedProjectStopping ? "Arrêt…" : "Arrêter"}
                </Button>
                {selectedProject && (
                  <Button
                    className="w-full sm:w-auto"
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
              <div className="mb-4 flex flex-col gap-3 border-y border-red-300 bg-red-50 px-4 py-3 text-sm text-red-950 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex min-w-0 items-start gap-3">
                  <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-red-600" />
                  <div className="min-w-0">
                    <div className="font-semibold">Service local indisponible</div>
                    <div className="mt-0.5 break-words text-red-800">
                      L'application n'arrive pas à joindre son API locale. Attends quelques secondes puis actualise. Si Docker n'est pas encore installé,
                      installe Docker Desktop avant de lancer les projets Odoo.
                    </div>
                    <div className="mt-3 rounded-md border border-red-200 bg-white/70 p-3">
                      <div className="font-medium">{fallbackDockerGuide.title}</div>
                      <ol className="mt-2 list-decimal space-y-1 pl-4 text-xs leading-5 text-red-900">
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
              <div className="mb-4 flex flex-col gap-3 border-y border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex min-w-0 items-start gap-3">
                  <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
                  <div className="min-w-0">
                    <div className="font-semibold">Docker n’est pas disponible</div>
                    <div className="mt-0.5 break-words text-amber-800">{systemStatus.docker.message}</div>
                    {systemStatus.docker.state === "missing" && systemStatus.docker.install_guide && (
                      <div className="mt-3 rounded-md border border-amber-200 bg-white/70 p-3">
                        <div className="font-medium">{systemStatus.docker.install_guide.title}</div>
                        <ol className="mt-2 list-decimal space-y-1 pl-4 text-xs leading-5 text-amber-900">
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
              <div className="mb-4 flex flex-col gap-3 border-y border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-950 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex min-w-0 items-start gap-3">
                  <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-sky-700" />
                  <div className="min-w-0">
                    <div className="font-semibold">Traefik n'est pas prêt</div>
                    <div className="mt-0.5 break-words text-sky-800">
                      {systemStatus.traefik.message}
                      {systemStatus.traefik.requires_docker ? " Docker doit être installé et démarré avant cette étape." : ""}
                    </div>
                    <div className="mt-1 break-all text-xs text-sky-700">Dossier attendu : {systemStatus.traefik.path}</div>
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
              <div className="mb-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
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
              <TabsList className="w-full justify-start overflow-x-auto lg:w-auto">
                <TabsTrigger value="bases">
                  <Database className="mr-2 h-4 w-4" />
                  Bases
                </TabsTrigger>
                <TabsTrigger value="modules">
                  <Boxes className="mr-2 h-4 w-4" />
                  Modules
                </TabsTrigger>
                <TabsTrigger value="logs">
                  <Logs className="mr-2 h-4 w-4" />
                  Logs
                </TabsTrigger>
                <TabsTrigger value="actions">
                  <Settings className="mr-2 h-4 w-4" />
                  Actions
                </TabsTrigger>
              </TabsList>

              <TabsContent value="bases">
                <div className="grid min-w-0 gap-4 xl:grid-cols-[minmax(0,1fr)_400px]">
                  <Card>
                    <CardHeader>
                      <CardTitle>Bases Odoo</CardTitle>
                      <CardDescription>Sélectionne l’environnement Odoo utilisé pour les modules et les actions.</CardDescription>
                    </CardHeader>
                    <CardContent>
                      {odooDatabases.length ? (
                        <div className="grid gap-3 sm:grid-cols-2 2xl:grid-cols-3">
                          {odooDatabases.map((db) => (
                            <button
                              key={db}
                              className={cn(
                                "min-w-0 rounded-md border p-4 text-left transition-colors hover:bg-muted",
                                selectedDb === db ? "border-primary bg-primary/8" : "bg-card",
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
                            </button>
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
                  <div className="grid min-w-0 content-start gap-4">
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
                        {selectedProject && (
                          <Button className="w-full" variant="outline" onClick={() => openUrl(selectedProject.database_manager_url)}>
                            <ExternalLink className="h-4 w-4" />
                            Gestionnaire de bases Odoo
                          </Button>
                        )}
                      </CardContent>
                    </Card>

                    <Card>
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

              <TabsContent value="modules">
                <Card>
                  <CardHeader className="gap-3 xl:flex-row xl:items-start xl:justify-between">
                    <div className="min-w-0">
                      <CardTitle>Modules</CardTitle>
                      <CardDescription>Recherche par nom technique, sélection multiple et actions groupées.</CardDescription>
                    </div>
                    <div className="grid w-full min-w-0 grid-cols-1 gap-2 sm:grid-cols-2 xl:flex xl:w-auto xl:max-w-[760px] xl:flex-wrap xl:justify-end">
                      <Button variant="outline" onClick={() => setZipDialogOpen(true)} disabled={!selectedProjectReady}>
                        <FileArchive className="h-4 w-4" />
                        Import ZIP
                      </Button>
                      <Button
                        disabled={!selectedModuleList.length || !canUseDb || loading}
                        onClick={() => createJob("install_module", { project: selectedProject?.name, db: selectedDb, modules: selectedModuleList.join(",") })}
                      >
                        <PlusCircle className="h-4 w-4" />
                        Installer sélection
                      </Button>
                      <Button
                        variant="outline"
                        disabled={!selectedModuleList.length || !canUseDb || loading}
                        onClick={() => createJob("update_module", { project: selectedProject?.name, db: selectedDb, modules: selectedModuleList.join(",") })}
                      >
                        <RefreshCcw className="h-4 w-4" />
                        Mettre à jour sélection
                      </Button>
                      <Button
                        variant="destructive"
                        disabled={!selectedInstalledModuleList.length || !canUseDb || loading}
                        onClick={() => requestUninstall(selectedModuleList)}
                      >
                        <Trash2 className="h-4 w-4" />
                        Désinstaller sélection
                      </Button>
                      <Button
                        variant="destructive"
                        disabled={!selectedRemovableModuleList.length || loading}
                        onClick={() => requestDeleteCode(selectedModuleList)}
                      >
                        <PackageX className="h-4 w-4" />
                        Supprimer du projet
                      </Button>
                    </div>
                  </CardHeader>
                  <CardContent>
                    <div className="mb-4 grid min-w-0 gap-3 md:grid-cols-[minmax(0,1fr)_minmax(150px,220px)] xl:grid-cols-[minmax(0,1fr)_220px_220px]">
                      <div className="relative">
                        <Search className="absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
                        <Input className="pl-9" placeholder="Rechercher par nom de module" value={moduleSearch} onChange={(event) => setModuleSearch(event.target.value)} />
                      </div>
                      <Select value={moduleFilter} onValueChange={setModuleFilter}>
                        <SelectTrigger>
                          <SelectValue placeholder="État" />
                        </SelectTrigger>
                        <SelectContent>
                          <SelectItem value="all">Tous les états</SelectItem>
                          <SelectItem value="installed">Installés</SelectItem>
                          <SelectItem value="uninstalled">Disponibles</SelectItem>
                        </SelectContent>
                      </Select>
                      <Select value={selectedDb} onValueChange={setSelectedDb}>
                        <SelectTrigger>
                          <SelectValue placeholder="Base Odoo" />
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
                    <div className="mb-3 flex flex-col gap-2 rounded-md border bg-muted/45 p-3 text-sm sm:flex-row sm:items-center sm:justify-between">
                      <label className="flex min-w-0 cursor-pointer items-start gap-3">
                        <input
                          className="mt-0.5 h-4 w-4 shrink-0"
                          aria-checked={someFilteredModulesSelected ? "mixed" : allFilteredModulesSelected}
                          ref={(input) => {
                            if (input) input.indeterminate = someFilteredModulesSelected;
                          }}
                          type="checkbox"
                          checked={allFilteredModulesSelected}
                          disabled={!filteredModuleNames.length}
                          onChange={(event) => toggleFilteredModules(event.target.checked)}
                        />
                        <span className="min-w-0">
                          <span className="block font-medium">Sélectionner les résultats affichés</span>
                          <span className="block text-xs text-muted-foreground">
                            Coche automatiquement les modules présents dans la recherche courante.
                          </span>
                        </span>
                      </label>
                      <Badge className="w-fit shrink-0" variant="outline">
                        {selectedFilteredModuleCount}/{filteredModuleNames.length} sélectionné(s)
                      </Badge>
                    </div>
                    <div className="overflow-hidden rounded-md border">
                      <div className="hidden border-b bg-muted px-3 py-2 text-xs font-medium uppercase text-muted-foreground lg:grid lg:grid-cols-[minmax(220px,1.35fr)_120px_130px_minmax(260px,1.15fr)_168px] lg:items-center lg:gap-3">
                        <div>Module</div>
                        <div>État</div>
                        <div>Version</div>
                        <div>Emplacements</div>
                        <div className="text-right">Actions</div>
                      </div>
                      <div className="max-h-[min(62vh,720px)] overflow-y-auto">
                        {filteredModules.length ? (
                          filteredModules.map((module) => {
                            const sourcePath = module.source_path || module.path;
                            const linkPath = module.link_path || (module.path_kind?.startsWith("lien") ? module.path : "");
                            const displaySourcePath = compactWorkspacePath(sourcePath, overview?.workspace);
                            const displayLinkPath = compactWorkspacePath(linkPath, overview?.workspace);
                            const samePaths = Boolean(linkPath && sourcePath && linkPath === sourcePath);
                            return (
                              <div
                                key={module.name}
                                className="grid min-w-0 gap-3 border-t p-3 first:border-t-0 lg:grid-cols-[minmax(220px,1.35fr)_120px_130px_minmax(260px,1.15fr)_168px] lg:items-center"
                              >
                                <div className="flex min-w-0 items-start gap-3">
                                  <input
                                    className="mt-1 h-4 w-4 shrink-0"
                                    aria-label={`Sélectionner ${module.name}`}
                                    type="checkbox"
                                    checked={selectedModules.has(module.name)}
                                    onChange={(event) => toggleModuleSelection(module.name, event.target.checked)}
                                  />
                                  <div className="min-w-0">
                                    <div className="break-words font-medium">{module.name}</div>
                                    <div className="mt-0.5 break-words text-xs text-muted-foreground">{module.title || module.name}</div>
                                  </div>
                                </div>
                                <div className="flex min-w-0 items-center justify-between gap-3 lg:block">
                                  <span className="text-xs font-medium text-muted-foreground lg:hidden">État</span>
                                  <Badge className="shrink-0" variant={module.state === "installed" ? "success" : "secondary"}>{module.state}</Badge>
                                </div>
                                <div className="flex min-w-0 items-start justify-between gap-3 text-sm lg:block">
                                  <span className="text-xs font-medium text-muted-foreground lg:hidden">Version</span>
                                  <span className="min-w-0 break-words">{module.installed_version || module.version || "-"}</span>
                                </div>
                                <div className="min-w-0">
                                  <div className="mb-1 text-xs font-medium text-muted-foreground lg:hidden">Emplacements</div>
                                  <div className="space-y-1">
                                    {module.path_kind && (
                                      <Badge className="w-fit max-w-full truncate" variant="outline" title={module.path_kind}>
                                        {module.path_kind}
                                      </Badge>
                                    )}
                                    <div className="min-w-0 text-xs">
                                      <span className="font-medium text-muted-foreground">Source</span>
                                      <div className="break-all font-mono text-pink-700" title={sourcePath}>
                                        {displaySourcePath || "-"}
                                      </div>
                                    </div>
                                    {displayLinkPath && !samePaths && (
                                      <div className="min-w-0 text-xs">
                                        <span className="font-medium text-muted-foreground">Lien Odoo</span>
                                        <div className="break-all font-mono text-slate-600" title={linkPath}>
                                          {displayLinkPath}
                                        </div>
                                      </div>
                                    )}
                                  </div>
                                </div>
                                <div className="grid grid-cols-4 gap-2 sm:flex sm:justify-end">
                                  <Button size="icon" variant="outline" disabled={!canUseDb} title={`Installer ${module.name}`} aria-label={`Installer ${module.name}`} onClick={() => createJob("install_module", { project: selectedProject?.name, db: selectedDb, modules: module.name })}>
                                    <PlusCircle className="h-4 w-4" />
                                  </Button>
                                  <Button size="icon" disabled={!canUseDb} title={`Mettre à jour ${module.name}`} aria-label={`Mettre à jour ${module.name}`} onClick={() => createJob("update_module", { project: selectedProject?.name, db: selectedDb, modules: module.name })}>
                                    <RefreshCcw className="h-4 w-4" />
                                  </Button>
                                  <Button
                                    size="icon"
                                    variant="destructive"
                                    disabled={!canUseDb || module.state !== "installed"}
                                    title={`Désinstaller ${module.name}`}
                                    aria-label={`Désinstaller ${module.name}`}
                                    onClick={() => requestUninstall([module.name])}
                                  >
                                    <PackageX className="h-4 w-4" />
                                  </Button>
                                  <Button
                                    size="icon"
                                    variant="destructive"
                                    disabled={!module.removable}
                                    title={module.removal_note || `Supprimer ${module.name} du projet`}
                                    aria-label={`Supprimer le code ${module.name}`}
                                    onClick={() => requestDeleteCode([module.name])}
                                  >
                                    <Trash2 className="h-4 w-4" />
                                  </Button>
                                </div>
                              </div>
                            );
                          })
                        ) : (
                          <div className="p-6 text-center text-sm text-muted-foreground">
                            Aucun module ne correspond à la recherche.
                          </div>
                        )}
                      </div>
                    </div>
                  </CardContent>
                </Card>
              </TabsContent>

              <TabsContent value="logs">
                <div className="grid min-w-0 gap-4 min-[1500px]:grid-cols-[minmax(300px,360px)_minmax(0,1fr)]">
                  <Card className="min-w-0">
                    <CardHeader className="gap-3 sm:flex-row sm:items-center sm:justify-between">
                      <div className="min-w-0">
                        <CardTitle>Historique</CardTitle>
                        <CardDescription>Actions lancées depuis le gestionnaire.</CardDescription>
                      </div>
                      <Button className="w-full shrink-0 sm:w-auto" variant="outline" size="sm" onClick={clearJobs}>
                        <Trash2 className="h-4 w-4" />
                        Effacer
                      </Button>
                    </CardHeader>
                    <CardContent className="max-h-[min(58vh,620px)] min-w-0 space-y-2 overflow-y-auto">
                      {jobs.map((job) => (
                        <div
                          key={job.id}
                          className={cn(
                            "group min-w-0 rounded-md border p-2 transition-colors hover:bg-muted",
                            !externalLogView && selectedJob?.id === job.id && "border-primary bg-primary/8",
                          )}
                        >
                          <button
                            type="button"
                            className="w-full min-w-0 rounded-md p-2 text-left outline-none transition-colors hover:bg-card/70 focus-visible:ring-2 focus-visible:ring-ring"
                            onClick={() => selectJob(job.id)}
                          >
                            <div className="flex items-start justify-between gap-2">
                              <div className="min-w-0 flex-1">
                                <div className="break-words font-medium leading-snug">{job.title}</div>
                                <div className="mt-1 text-xs text-muted-foreground">{job.started_at}</div>
                              </div>
                              <Badge className="shrink-0" variant={statusVariant(job.status)}>{job.status}</Badge>
                            </div>
                          </button>
                          <Button
                            className="relative z-10 mt-1 w-full shrink-0 border-red-200 text-red-700 hover:border-red-300 hover:bg-red-50 hover:text-red-800 active:bg-red-100 focus-visible:ring-red-500"
                            variant="outline"
                            size="sm"
                            title={`Supprimer l'historique ${job.title}`}
                            aria-label={`Supprimer l'historique ${job.title}`}
                            onClick={(event) => {
                              event.preventDefault();
                              event.stopPropagation();
                              deleteJob(job.id);
                            }}
                          >
                            Supprimer
                          </Button>
                        </div>
                      ))}
                    </CardContent>
                  </Card>
                  <Card className="min-w-0">
                    <CardHeader className="min-w-0 gap-3 min-[1900px]:flex-row min-[1900px]:items-start min-[1900px]:justify-between">
                      <div className="min-w-0 flex-1">
                        <CardTitle>Sortie</CardTitle>
                        <CardDescription className="break-words">{outputTitle}</CardDescription>
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
                        className="min-h-[260px] max-h-[min(58vh,620px)] max-w-full overflow-auto whitespace-pre-wrap break-words rounded-md bg-slate-950 p-3 text-xs leading-relaxed text-emerald-100 sm:p-4"
                        onScroll={handleLogOutputScroll}
                      >
                        {outputContent}
                      </pre>
                    </CardContent>
                  </Card>
                </div>
              </TabsContent>

              <TabsContent value="actions">
                <div className="grid gap-4 xl:grid-cols-3">
                  <Card>
                    <CardHeader>
                      <CardTitle>Actions Odoo</CardTitle>
                      <CardDescription>Met à jour les modules dans la base sélectionnée.</CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-2">
                      <Button className="w-full" variant="outline" disabled={!selectedProjectReady || loading} onClick={requestUpdateLocalModules}>
                        <ListRestart className="h-4 w-4" />
                        MAJ addons projet
                      </Button>
                      <Button
                        className="w-full"
                        disabled={!selectedProjectReady || loading || checkingUpdatePrerequisites}
                        onClick={requestUpdateAllOdooModules}
                      >
                        {checkingUpdatePrerequisites ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCcw className="h-4 w-4" />}
                        MAJ complète Odoo (-u all)
                      </Button>
                    </CardContent>
                  </Card>
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
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Préparer le gestionnaire Odoo</DialogTitle>
            <DialogDescription>
              Vérifie les prérequis une seule fois, puis crée ton premier environnement depuis l’application.
            </DialogDescription>
          </DialogHeader>
          <div className="divide-y rounded-md border">
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
              detail={creationPrerequisites?.git_version || creationPrerequisites?.git_install_message || "Git doit être disponible sur la machine."}
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
                  ? creationPrerequisites.ssh_keys.join(", ")
                  : "Ajoute ta clé publique dans ton profil GitLab avant la première création."
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
          <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
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
                <span className="break-all text-xs font-normal text-muted-foreground">
                  Le dossier est créé s’il n’existe pas encore. Dans l’application installée, « Choisir » ouvre le sélecteur du système.
                </span>
              </div>

              <div className="rounded-md border bg-muted/40 p-3">
                <div className="text-sm font-medium">Exécution automatique</div>
                <p className="mt-1 text-xs font-normal leading-relaxed text-muted-foreground">
                  Le gestionnaire choisit automatiquement les outils adaptés au système. Sous Windows, Docker,
                  Git et Traefik restent natifs ; WSL est utilisé uniquement lorsqu’une opération le nécessite.
                </p>
              </div>

              <label className="flex cursor-pointer items-start gap-3 rounded-md border p-3 text-sm">
                <input
                  className="mt-0.5 h-4 w-4 shrink-0"
                  type="checkbox"
                  checked={settingsDraft.start_project_before_open}
                  onChange={(event) =>
                    setSettingsDraft({ ...settingsDraft, start_project_before_open: event.target.checked })
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

              <div className="rounded-md border bg-muted/40 p-3 text-xs text-muted-foreground">
                <div>Plateforme : {settingsDraft.platform || systemStatus?.docker.platform || "-"}</div>
                <div className="mt-1 break-all">Configuration : {settingsDraft.config_file || "-"}</div>
              </div>

              <div className="flex flex-col-reverse gap-2 sm:flex-row sm:justify-end">
                <Button variant="outline" onClick={() => setSettingsOpen(false)}>Annuler</Button>
                <Button disabled={savingSettings || !settingsDraft.workspace.trim()} onClick={saveSettings}>
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
          <Input
            ref={zipInputRef}
            type="file"
            accept=".zip"
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
                <input
                  className="mt-0.5 h-4 w-4 shrink-0"
                  type="checkbox"
                  checked={selectedZipModules.size === zipModuleCandidates.length}
                  ref={(input) => {
                    if (input) {
                      input.indeterminate = selectedZipModules.size > 0 && selectedZipModules.size < zipModuleCandidates.length;
                    }
                  }}
                  onChange={(event) => toggleAllZipModules(event.target.checked)}
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
                    <input
                      className="mt-0.5 h-4 w-4 shrink-0"
                      type="checkbox"
                      checked={selectedZipModules.has(moduleName)}
                      onChange={(event) => toggleZipModule(moduleName, event.target.checked)}
                    />
                    <span className="min-w-0 break-all font-mono">{moduleName}</span>
                  </label>
                ))}
              </div>
            </div>
          )}
          <label className="flex items-start gap-2 rounded-md border bg-muted/40 p-3 text-sm">
            <input
              className="mt-1"
              type="checkbox"
              checked={replaceZipModules}
              onChange={(event) => setReplaceZipModules(event.target.checked)}
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
        <DialogContent className="max-h-[90vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>MAJ complète Odoo</DialogTitle>
            <DialogDescription>
              Cette action lance une mise à jour de tous les modules installés sur la base sélectionnée.
            </DialogDescription>
          </DialogHeader>
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
                odoo -d {selectedDb || "BASE"} -u {updateLocalExcludedModules.length ? "<modules disponibles non exclus>" : "all"} --stop-after-init
              </code>
            </div>
          </div>
          {updateLocalExcludedModules.length ? (
            <div className="grid gap-2 rounded-md border border-blue-200 bg-blue-50 p-3 text-sm text-blue-950">
              <div className="font-medium">Mode avec exceptions locales</div>
              <p>
                Le gestionnaire utilisera une liste explicite des modules dont le code est disponible. Les modules suivants ne seront pas remis en
                attente par un nouvel appel à <code>-u all</code> :
              </p>
              <div className="flex flex-wrap gap-1.5">
                {updateLocalExcludedModules.map((moduleName) => (
                  <Badge key={moduleName} variant="outline" className="border-blue-300 bg-white font-mono text-blue-950">
                    {moduleName}
                  </Badge>
                ))}
              </div>
              <Button variant="outline" className="border-blue-300 bg-white hover:bg-blue-100" onClick={restoreLocalModuleExclusions} disabled={loading}>
                <RefreshCcw className="h-4 w-4" />
                Réactiver toutes les exclusions
              </Button>
            </div>
          ) : null}
          {updatePendingModules.length ? (
            <div className="grid gap-3 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-950">
              <div className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-red-600" />
                <div className="grid gap-1">
                  <span className="font-medium">Opérations de modules en attente</span>
                  <span>
                    Sélectionne uniquement les modules que tu ne veux pas mettre à jour pour tes tests. Le gestionnaire annulera leur opération sur cette
                    copie locale sans les désinstaller. Une dépendance nécessaire à un module actif non sélectionné sera automatiquement refusée.
                  </span>
                </div>
              </div>
              <div className="max-h-48 space-y-2 overflow-y-auto rounded-md border border-red-200 bg-white p-2">
                {updatePendingModules.map((module) => (
                  <label key={module.name} className="flex cursor-pointer items-center gap-3 rounded px-2 py-2 hover:bg-red-50">
                    <input
                      type="checkbox"
                      className="h-4 w-4 shrink-0 accent-red-600"
                      checked={missingModulesToIgnore.has(module.name)}
                      onChange={(event) => toggleMissingModuleToIgnore(module.name, event.target.checked)}
                    />
                    <span className="min-w-0 flex-1 break-all font-mono text-xs">{module.name}</span>
                    <Badge className="shrink-0" variant={module.code_available ? "outline" : "destructive"}>
                      {module.code_available ? module.state : "code absent"}
                    </Badge>
                  </label>
                ))}
              </div>
              <Button
                variant="destructive"
                disabled={!missingModulesToIgnore.size || loading}
                onClick={ignoreSelectedMissingModulesLocally}
              >
                {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <PackageX className="h-4 w-4" />}
                Ignorer la sélection sur cette copie locale
              </Button>
              <p className="text-xs text-red-800">
                Relance ensuite cette fenêtre. Les modules non exclus dont le code manque devront être restaurés avant la mise à jour.
              </p>
            </div>
          ) : null}
          {updateFilestoreStatus && updateFilestoreStatus.missing > 0 ? (
            <div className="grid gap-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950">
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
              <label className="flex cursor-pointer items-start gap-3 rounded-md border border-amber-300 bg-white p-3 hover:bg-amber-100/60">
                <input
                  type="checkbox"
                  className="mt-0.5 h-4 w-4 shrink-0 accent-blue-600"
                  checked={allowMissingFilestore}
                  onChange={(event) => setAllowMissingFilestore(event.target.checked)}
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
                Boolean(updatePendingModules.length) ||
                Boolean(updateFilestoreStatus?.missing && !allowMissingFilestore)
              }
              onClick={confirmUpdateAllOdooModules}
            >
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCcw className="h-4 w-4" />}
              Lancer la MAJ complète
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
            <input
              className="mt-1"
              type="checkbox"
              checked={deleteCodeUninstallFirst}
              disabled={!canUseDb}
              onChange={(event) => setDeleteCodeUninstallFirst(event.target.checked)}
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
              toast.kind === "error" && "border-red-200 bg-red-50 text-red-800",
              toast.kind === "success" && "border-emerald-200 bg-emerald-50 text-emerald-800",
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
    <div className="flex min-w-0 flex-col gap-3 p-3 sm:flex-row sm:items-center">
      <div className="flex min-w-0 flex-1 items-start gap-3">
        <div className={cn("mt-0.5 rounded-md p-2", ready ? "bg-emerald-50 text-emerald-700" : "bg-amber-50 text-amber-700")}>
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
      {action && <div className="shrink-0 pl-11 sm:pl-0">{action}</div>}
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
  const [sourceType, setSourceType] = useState<"standard" | "gitlab">("standard");
  const [repositoryUrl, setRepositoryUrl] = useState("");
  const [repositoryBranch, setRepositoryBranch] = useState("master");
  const [startAfterCreation, setStartAfterCreation] = useState(true);

  useEffect(() => {
    if (!open) return;
    setStartAfterCreation(dockerReady);
    if (prerequisites?.supported_versions?.length && !prerequisites.supported_versions.includes(version)) {
      setVersion(prerequisites.supported_versions.at(-1) || "19.0");
    }
  }, [dockerReady, open, prerequisites?.supported_versions, version]);

  const prerequisitesReady = Boolean(
    prerequisites?.workspace_ready && prerequisites.git_available && prerequisites.ssh_key_present,
  );
  const gitlabFieldsReady = sourceType === "standard" || Boolean(repositoryUrl.trim() && repositoryBranch.trim());

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
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
              <Select value={version} onValueChange={setVersion}>
                <SelectTrigger id="new-project-version"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {(prerequisites?.supported_versions || ["15.0", "16.0", "17.0", "18.0", "19.0"]).map((item) => (
                    <SelectItem key={item} value={item}>Odoo {item}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <span aria-hidden="true" className="min-h-4 text-xs font-normal">&nbsp;</span>
            </div>
          </div>

          <fieldset className="grid gap-2">
            <legend className="mb-1 text-sm font-medium">Source du projet</legend>
            <div className="grid gap-2 sm:grid-cols-2">
              <button
                type="button"
                className={cn(
                  "min-h-20 rounded-md border p-3 text-left transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  sourceType === "standard" && "border-primary bg-primary/5",
                )}
                onClick={() => setSourceType("standard")}
              >
                <span className="block font-medium">Odoo standard</span>
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">Odoo Community et Enterprise Sudokeys.</span>
              </button>
              <button
                type="button"
                className={cn(
                  "min-h-20 rounded-md border p-3 text-left transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  sourceType === "gitlab" && "border-primary bg-primary/5",
                )}
                onClick={() => setSourceType("gitlab")}
              >
                <span className="block font-medium">Dépôt d’addons GitLab</span>
                <span className="mt-1 block text-xs leading-5 text-muted-foreground">Ajoute le dépôt client au socle standard.</span>
              </button>
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

          <label className={cn("flex items-start gap-3 rounded-md border p-3 text-sm", !dockerReady && "bg-muted/40")}>
            <input
              className="mt-0.5 h-4 w-4 shrink-0 accent-blue-600"
              type="checkbox"
              checked={startAfterCreation}
              disabled={!dockerReady}
              onChange={(event) => setStartAfterCreation(event.target.checked)}
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
            <div className="flex flex-col gap-3 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-950 sm:flex-row sm:items-center sm:justify-between">
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
              disabled={loading || !name.trim() || !prerequisitesReady || !gitlabFieldsReady}
              onClick={() => onSubmit({
                name: name.trim(),
                version,
                source_type: sourceType,
                repository_url: repositoryUrl.trim(),
                repository_branch: repositoryBranch.trim(),
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
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Créer une base Odoo</DialogTitle>
          <DialogDescription>{project ? `Projet cible : ${project.name}` : "Sélectionne un projet."}</DialogDescription>
        </DialogHeader>
        <div className="grid gap-3 md:grid-cols-2">
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
          <input type="checkbox" checked={demo} onChange={(event) => setDemo(event.target.checked)} />
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
