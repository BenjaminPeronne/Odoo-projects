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
  FolderPlus,
  Link2,
  ListRestart,
  Loader2,
  Logs,
  PackageX,
  Play,
  PlusCircle,
  RefreshCcw,
  Search,
  Settings,
  SquareTerminal,
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
};

type SystemStatus = {
  docker: DockerStatus;
  workspace: string;
  workspace_exists: boolean;
};

type ManagerSettings = {
  version: number;
  workspace: string;
  execution_mode: "native" | "wsl" | string;
  wsl_distribution: string;
  docker_executable: string;
  brainkeys_executable: string;
  traefik_directory: string;
  terminal: string;
  docker_poll_interval: number;
  config_file?: string;
  platform?: string;
  workspace_exists?: boolean;
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

type ProjectDiagnostics = {
  project: string;
  docker_ok: boolean;
  odoo_status?: string;
  postgres_status?: string;
  issues: DiagnosticIssue[];
  databases?: Array<{
    name: string;
    filestore?: {
      path: string;
      referenced: number;
      referenced_unique: number;
      actual: number;
      missing: number;
    };
  }>;
};

const API_BASE = process.env.NEXT_PUBLIC_ODOO_MANAGER_API?.replace(/\/$/, "") || "";

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: init?.body instanceof FormData ? init.headers : { "Content-Type": "application/json", ...init?.headers },
  });
  const text = await response.text();
  const payload = text ? JSON.parse(text) : {};
  if (!response.ok) {
    throw new Error(payload.error || response.statusText);
  }
  return payload as T;
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
  return project?.databases?.find((db) => db !== "postgres") || project?.databases?.[0] || "";
}

export default function Home() {
  const [overview, setOverview] = useState<Overview | null>(null);
  const [systemStatus, setSystemStatus] = useState<SystemStatus | null>(null);
  const [settings, setSettings] = useState<ManagerSettings | null>(null);
  const [settingsDraft, setSettingsDraft] = useState<ManagerSettings | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [savingSettings, setSavingSettings] = useState(false);
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
  const [error, setError] = useState("");
  const [toasts, setToasts] = useState<Toast[]>([]);
  const [zipDialogOpen, setZipDialogOpen] = useState(false);
  const [createDbOpen, setCreateDbOpen] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [uninstallDialogOpen, setUninstallDialogOpen] = useState(false);
  const [deleteCodeDialogOpen, setDeleteCodeDialogOpen] = useState(false);
  const [replaceZipModules, setReplaceZipModules] = useState(true);
  const [deleteCodeUninstallFirst, setDeleteCodeUninstallFirst] = useState(true);
  const [sourcePath, setSourcePath] = useState("");
  const [moduleNames, setModuleNames] = useState("");
  const [deleteConfirm, setDeleteConfirm] = useState("");
  const [pendingUninstallModules, setPendingUninstallModules] = useState<string[]>([]);
  const [pendingDeleteCodeModules, setPendingDeleteCodeModules] = useState<string[]>([]);
  const zipInputRef = useRef<HTMLInputElement>(null);
  const toastId = useRef(1);
  const lastDockerState = useRef<string | null>(null);

  const selectedProject = useMemo(
    () => overview?.projects.find((project) => project.name === selectedProjectName) || overview?.projects[0],
    [overview, selectedProjectName],
  );

  const selectedJob = useMemo(() => jobs.find((job) => job.id === selectedJobId) || jobs[0], [jobs, selectedJobId]);

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

  const pushToast = useCallback((kind: Toast["kind"], message: string) => {
    const id = toastId.current++;
    setToasts((current) => [...current, { id, kind, message }]);
    window.setTimeout(() => setToasts((current) => current.filter((toast) => toast.id !== id)), 4200);
  }, []);

  const refreshOverview = useCallback(async () => {
    try {
      const payload = await api<Overview>("/api/overview");
      setOverview(payload);
      setError("");
      const current = payload.projects.find((project) => project.name === selectedProjectName) || payload.projects[0];
      if (current && current.name !== selectedProjectName) {
        setSelectedProjectName(current.name);
        setSelectedDb(firstOdooDatabase(current));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Impossible de charger l'overview.");
    }
  }, [selectedProjectName]);

  const refreshSystemStatus = useCallback(async () => {
    try {
      const payload = await api<SystemStatus>("/api/system/status");
      setSystemStatus(payload);
      const previous = lastDockerState.current;
      if (previous && previous !== payload.docker.state) {
        if (payload.docker.running) pushToast("success", "Docker est maintenant disponible.");
        else pushToast("error", payload.docker.message || "Docker n'est plus disponible.");
      }
      lastDockerState.current = payload.docker.state;
    } catch (err) {
      setSystemStatus(null);
      if (lastDockerState.current !== "api-error") {
        pushToast("error", err instanceof Error ? err.message : "État système indisponible.");
        lastDockerState.current = "api-error";
      }
    }
  }, [pushToast]);

  const loadSettings = useCallback(async () => {
    try {
      const payload = await api<{ settings: ManagerSettings }>("/api/settings");
      setSettings(payload.settings);
      setSettingsDraft(payload.settings);
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Paramètres indisponibles.");
    }
  }, [pushToast]);

  const refreshJobs = useCallback(async () => {
    try {
      const payload = await api<{ jobs: Job[] }>("/api/jobs");
      setJobs(payload.jobs);
      if (!selectedJobId && payload.jobs[0]) setSelectedJobId(payload.jobs[0].id);
    } catch {
      // Jobs polling should not break the whole screen.
    }
  }, [selectedJobId]);

  const refreshModules = useCallback(async () => {
    if (!selectedProject || !selectedDb) return;
    try {
      const payload = await api<{ modules: ModuleInfo[] }>(
        `/api/projects/${encodeURIComponent(selectedProject.name)}/modules?db=${encodeURIComponent(selectedDb)}`,
      );
      setModules(payload.modules);
      setSelectedModules((current) => {
        const available = new Set(payload.modules.map((module) => module.name));
        return new Set(Array.from(current).filter((name) => available.has(name)));
      });
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Impossible de charger les modules.");
    }
  }, [pushToast, selectedDb, selectedProject]);

  useEffect(() => {
    refreshOverview();
    refreshJobs();
    refreshSystemStatus();
    loadSettings();
    const timer = window.setInterval(() => {
      refreshOverview();
      refreshJobs();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [loadSettings, refreshJobs, refreshOverview, refreshSystemStatus]);

  useEffect(() => {
    const interval = Math.max(3, settings?.docker_poll_interval || 10) * 1000;
    const timer = window.setInterval(refreshSystemStatus, interval);
    return () => window.clearInterval(timer);
  }, [refreshSystemStatus, settings?.docker_poll_interval]);

  useEffect(() => {
    if (selectedProject) {
      setSelectedDb((current) => current || firstOdooDatabase(selectedProject));
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

  async function requestDockerStart() {
    setLoading(true);
    try {
      const result = await api<{ ok: boolean; message: string }>("/api/system/docker/start", { method: "POST" });
      pushToast("info", result.message || "Démarrage de Docker demandé.");
      window.setTimeout(refreshSystemStatus, 1500);
      window.setTimeout(refreshSystemStatus, 5000);
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Impossible de démarrer Docker.");
    } finally {
      setLoading(false);
    }
  }

  async function saveSettings() {
    if (!settingsDraft) return;
    setSavingSettings(true);
    try {
      const payload = await api<{ settings: ManagerSettings }>("/api/settings", {
        method: "POST",
        body: JSON.stringify({ ...settingsDraft, create_workspace: true }),
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

  function requestUpdateAllOdooModules() {
    if (!selectedProject || !canUseDb) return;
    const confirmed = window.confirm(
      `Lancer une mise à jour complète Odoo sur la base ${selectedDb} ?\n\nEquivalent: odoo -d ${selectedDb} -u all --stop-after-init`,
    );
    if (!confirmed) return;
    createJob("update_all_modules", { project: selectedProject.name, db: selectedDb }).then(() => window.setTimeout(refreshModules, 2500));
  }

  async function showLogs() {
    if (!selectedProject) return;
    try {
      const payload = await api<{ logs: string }>(`/api/projects/${encodeURIComponent(selectedProject.name)}/logs`);
      setExternalLogView({
        title: `Logs Odoo - ${selectedProject.name}`,
        content: payload.logs || "Aucun log.",
      });
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
      pushToast("info", "Diagnostic projet chargé.");
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Diagnostic indisponible.");
    }
  }

  async function clearJobs() {
    await fetch("/api/jobs", { method: "DELETE" });
    setSelectedJobId(null);
    setExternalLogView(null);
    await refreshJobs();
  }

  async function deleteJob(jobId: number) {
    await fetch(`/api/jobs/${jobId}`, { method: "DELETE" });
    if (selectedJobId === jobId) {
      setSelectedJobId(null);
      setExternalLogView(null);
    }
    await refreshJobs();
  }

  function selectJob(jobId: number) {
    setExternalLogView(null);
    setSelectedJobId(jobId);
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
      window.setTimeout(refreshModules, 2500);
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
      window.setTimeout(refreshModules, 2500);
    }
  }

  async function importZip() {
    if (!selectedProject) return;
    const file = zipInputRef.current?.files?.[0];
    if (!file) {
      pushToast("error", "Sélectionne un fichier ZIP.");
      return;
    }
    const form = new FormData();
    form.append("zip", file);
    form.append("replace_existing", replaceZipModules ? "1" : "0");
    setLoading(true);
    try {
      const result = await api<{ job: Job }>(`/api/projects/${encodeURIComponent(selectedProject.name)}/module-zip`, {
        method: "POST",
        body: form,
      });
      setSelectedJobId(result.job.id);
      setExternalLogView(null);
      setZipDialogOpen(false);
      pushToast("success", "Import ZIP lancé.");
      window.setTimeout(refreshModules, 1800);
      await refreshJobs();
    } catch (err) {
      pushToast("error", err instanceof Error ? err.message : "Import ZIP impossible.");
    } finally {
      setLoading(false);
    }
  }

  const selectedModuleList = Array.from(selectedModules);
  const selectedInstalledModuleList = selectedModuleList.filter((name) => modules.find((module) => module.name === name)?.state === "installed");
  const selectedRemovableModuleList = selectedModuleList.filter((name) => modules.find((module) => module.name === name)?.removable);
  const selectedProjectReady = Boolean(selectedProject);
  const canUseDb = Boolean(selectedDb && selectedDb !== "postgres");
  const outputTitle = externalLogView?.title || selectedJob?.title || "Aucune action sélectionnée";
  const outputContent = externalLogView?.content || selectedJob?.output || selectedJob?.lines?.join("\n") || "Aucune sortie.";

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
            <div className="min-h-0 flex-1 overflow-auto p-2">
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
              <Button className="w-full" variant="outline" onClick={() => createJob("create_project_terminal")}>
                <FolderPlus className="h-4 w-4" />
                Nouveau projet
              </Button>
              <Button
                className="w-full"
                variant="ghost"
                onClick={() => {
                  setSettingsDraft(settings);
                  setSettingsOpen(true);
                }}
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
                <Button className="w-full sm:w-auto" variant="outline" onClick={refreshOverview}>
                  <RefreshCcw className="h-4 w-4" />
                  Actualiser
                </Button>
                <Button className="w-full sm:w-auto" disabled={!selectedProjectReady || loading} onClick={() => createJob("start_project", { project: selectedProject?.name })}>
                  <Play className="h-4 w-4" />
                  Démarrer
                </Button>
                <Button
                  className="w-full sm:w-auto"
                  variant="outline"
                  disabled={!selectedProjectReady || !canUseDb || loading}
                  onClick={() => createJob("update_local_modules", { project: selectedProject?.name, db: selectedDb })}
                >
                  <ListRestart className="h-4 w-4" />
                  MAJ addons projet
                </Button>
                <Button
                  className="w-full sm:w-auto"
                  variant="outline"
                  disabled={!selectedProjectReady || !canUseDb || loading}
                  onClick={requestUpdateAllOdooModules}
                >
                  <RefreshCcw className="h-4 w-4" />
                  MAJ complète Odoo (-u all)
                </Button>
                {selectedProject && (
                  <Button className="w-full sm:w-auto" variant="outline" asChild>
                    <a href={selectedProject.url} target="_blank">
                      <ExternalLink className="h-4 w-4" />
                      Odoo
                    </a>
                  </Button>
                )}
              </div>
            </div>
          </header>

          <div className="mx-auto max-w-[1500px] px-4 py-4">
            {systemStatus && !systemStatus.docker.running && (
              <div className="mb-4 flex flex-col gap-3 border-y border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-950 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex min-w-0 items-start gap-3">
                  <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-amber-600" />
                  <div className="min-w-0">
                    <div className="font-semibold">Docker n’est pas disponible</div>
                    <div className="mt-0.5 break-words text-amber-800">{systemStatus.docker.message}</div>
                  </div>
                </div>
                <div className="flex shrink-0 flex-col gap-2 sm:flex-row">
                  {systemStatus.docker.can_start && (
                    <Button className="w-full sm:w-auto" size="sm" disabled={loading} onClick={requestDockerStart}>
                      {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <Play className="h-4 w-4" />}
                      Ouvrir Docker
                    </Button>
                  )}
                  <Button
                    className="w-full sm:w-auto"
                    size="sm"
                    variant="outline"
                    onClick={() => {
                      setSettingsDraft(settings);
                      setSettingsOpen(true);
                    }}
                  >
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

            <Tabs defaultValue="bases">
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
                <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_420px]">
                  <Card>
                    <CardHeader>
                      <CardTitle>Bases PostgreSQL</CardTitle>
                      <CardDescription>Sélectionne la base utilisée pour les actions modules.</CardDescription>
                    </CardHeader>
                    <CardContent>
                      <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
                        {(selectedProject?.databases || []).map((db) => (
                          <button
                            key={db}
                            className={cn(
                              "rounded-md border p-4 text-left transition-colors hover:bg-muted",
                              selectedDb === db ? "border-primary bg-primary/8" : "bg-card",
                            )}
                            onClick={() => setSelectedDb(db)}
                          >
                            <div className="flex items-center justify-between gap-2">
                              <span className="font-medium">{db}</span>
                              {db === selectedDb && <CheckCircle2 className="h-4 w-4 text-primary" />}
                            </div>
                            <div className="mt-2 text-sm text-muted-foreground">
                              {db === "postgres" ? "Base système" : selectedProject?.database_versions?.[db] || "Base Odoo"}
                            </div>
                          </button>
                        ))}
                      </div>
                    </CardContent>
                  </Card>
                  <Card>
                    <CardHeader>
                      <CardTitle>Créer une base</CardTitle>
                      <CardDescription>Formulaire guidé branché sur l’API existante.</CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-3">
                      <Button className="w-full" disabled={!selectedProjectReady} onClick={() => setCreateDbOpen(true)}>
                        <PlusCircle className="h-4 w-4" />
                        Créer une base
                      </Button>
                      {selectedProject && (
                        <Button className="w-full" variant="outline" asChild>
                          <a href={selectedProject.database_manager_url} target="_blank">
                            <ExternalLink className="h-4 w-4" />
                            Gestionnaire Odoo natif
                          </a>
                        </Button>
                      )}
                    </CardContent>
                  </Card>
                </div>
              </TabsContent>

              <TabsContent value="modules">
                <Card>
                  <CardHeader className="gap-3 lg:flex-row lg:items-center lg:justify-between">
                    <div>
                      <CardTitle>Modules</CardTitle>
                      <CardDescription>Recherche par nom technique, sélection multiple et actions groupées.</CardDescription>
                    </div>
                    <div className="flex w-full flex-wrap gap-2 lg:w-auto lg:justify-end">
                      <Button className="w-full sm:w-auto" variant="outline" onClick={() => setZipDialogOpen(true)} disabled={!selectedProjectReady}>
                        <FileArchive className="h-4 w-4" />
                        Import ZIP
                      </Button>
                      <Button
                        className="w-full sm:w-auto"
                        disabled={!selectedModuleList.length || !canUseDb || loading}
                        onClick={() => createJob("install_module", { project: selectedProject?.name, db: selectedDb, modules: selectedModuleList.join(",") })}
                      >
                        <PlusCircle className="h-4 w-4" />
                        Installer sélection
                      </Button>
                      <Button
                        className="w-full sm:w-auto"
                        variant="outline"
                        disabled={!selectedModuleList.length || !canUseDb || loading}
                        onClick={() => createJob("update_module", { project: selectedProject?.name, db: selectedDb, modules: selectedModuleList.join(",") })}
                      >
                        <RefreshCcw className="h-4 w-4" />
                        Mettre à jour sélection
                      </Button>
                      <Button
                        className="w-full sm:w-auto"
                        variant="destructive"
                        disabled={!selectedInstalledModuleList.length || !canUseDb || loading}
                        onClick={() => requestUninstall(selectedModuleList)}
                      >
                        <Trash2 className="h-4 w-4" />
                        Désinstaller sélection
                      </Button>
                      <Button
                        className="w-full sm:w-auto"
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
                    <div className="mb-4 grid gap-3 lg:grid-cols-[minmax(0,1fr)_220px_220px]">
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
                          <SelectValue placeholder="Base" />
                        </SelectTrigger>
                        <SelectContent>
                          {(selectedProject?.databases || []).map((db) => (
                            <SelectItem key={db} value={db}>
                              {db}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </div>
                    <div className="overflow-auto rounded-md border">
                      <table className="w-full min-w-[1040px] text-sm">
                        <thead className="bg-muted text-left">
                          <tr>
                            <th className="w-12 px-3 py-3">
                              <span className="sr-only">Sélection</span>
                            </th>
                            <th className="px-3 py-3">Module</th>
                            <th className="px-3 py-3">État</th>
                            <th className="px-3 py-3">Version</th>
                            <th className="px-3 py-3">Chemin</th>
                            <th className="px-3 py-3 text-right">Actions</th>
                          </tr>
                        </thead>
                        <tbody>
                          {filteredModules.map((module) => (
                            <tr key={module.name} className="border-t">
                              <td className="px-3 py-3">
                                <input
                                  aria-label={`Sélectionner ${module.name}`}
                                  type="checkbox"
                                  checked={selectedModules.has(module.name)}
                                  onChange={(event) => {
                                    const next = new Set(selectedModules);
                                    if (event.target.checked) next.add(module.name);
                                    else next.delete(module.name);
                                    setSelectedModules(next);
                                  }}
                                />
                              </td>
                              <td className="px-3 py-3">
                                <div className="font-medium">{module.name}</div>
                                <div className="text-xs text-muted-foreground">{module.title}</div>
                              </td>
                              <td className="px-3 py-3">
                                <Badge variant={module.state === "installed" ? "success" : "secondary"}>{module.state}</Badge>
                              </td>
                              <td className="px-3 py-3">{module.installed_version || module.version || "-"}</td>
                              <td className="max-w-[360px] truncate px-3 py-3 font-mono text-xs text-pink-700" title={module.path}>
                                {overview ? module.path.replace(`${overview.workspace}/`, "") : module.path}
                              </td>
                              <td className="px-3 py-3">
                                <div className="flex justify-end gap-2">
                                  <Button size="icon" variant="outline" disabled={!canUseDb} onClick={() => createJob("install_module", { project: selectedProject?.name, db: selectedDb, modules: module.name })}>
                                    <PlusCircle className="h-4 w-4" />
                                  </Button>
                                  <Button size="icon" disabled={!canUseDb} onClick={() => createJob("update_module", { project: selectedProject?.name, db: selectedDb, modules: module.name })}>
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
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </CardContent>
                </Card>
              </TabsContent>

              <TabsContent value="logs">
                <div className="grid gap-4 xl:grid-cols-[420px_minmax(0,1fr)]">
                  <Card>
                    <CardHeader className="gap-3 sm:flex-row sm:items-center sm:justify-between">
                      <div>
                        <CardTitle>Historique</CardTitle>
                        <CardDescription>Actions lancées depuis le gestionnaire.</CardDescription>
                      </div>
                      <Button variant="outline" size="sm" onClick={clearJobs}>
                        <Trash2 className="h-4 w-4" />
                        Effacer
                      </Button>
                    </CardHeader>
                    <CardContent className="max-h-[520px] space-y-2 overflow-auto">
                      {jobs.map((job) => (
                        <div
                          key={job.id}
                          role="button"
                          tabIndex={0}
                          className={cn(
                            "w-full cursor-pointer rounded-md border p-3 text-left transition-colors hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                            !externalLogView && selectedJob?.id === job.id && "border-primary bg-primary/8",
                          )}
                          onClick={() => selectJob(job.id)}
                          onKeyDown={(event) => {
                            if (event.target !== event.currentTarget) return;
                            if (event.key === "Enter" || event.key === " ") {
                              event.preventDefault();
                              selectJob(job.id);
                            }
                          }}
                        >
                          <div className="flex items-start justify-between gap-2">
                            <div className="min-w-0 flex-1">
                              <div className="break-words font-medium">{job.title}</div>
                              <div className="mt-1 text-xs text-muted-foreground">{job.started_at}</div>
                            </div>
                            <Badge className="shrink-0" variant={statusVariant(job.status)}>{job.status}</Badge>
                          </div>
                          <Button
                            className="mt-2"
                            variant="ghost"
                            size="sm"
                            onClick={(event) => {
                              event.stopPropagation();
                              deleteJob(job.id);
                            }}
                          >
                            <Trash2 className="h-4 w-4" />
                            Supprimer
                          </Button>
                        </div>
                      ))}
                    </CardContent>
                  </Card>
                  <Card>
                    <CardHeader className="gap-3 sm:flex-row sm:items-center sm:justify-between">
                      <div>
                        <CardTitle>Sortie</CardTitle>
                        <CardDescription>{outputTitle}</CardDescription>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Button variant="outline" size="sm" onClick={showDiagnostics} disabled={!selectedProjectReady}>
                          <Activity className="h-4 w-4" />
                          Diagnostic
                        </Button>
                        <Button variant="outline" size="sm" onClick={showLogs} disabled={!selectedProjectReady}>
                          <Logs className="h-4 w-4" />
                          Logs Odoo
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => navigator.clipboard.writeText(outputContent)}
                        >
                          <Copy className="h-4 w-4" />
                          Copier
                        </Button>
                      </div>
                    </CardHeader>
                    <CardContent>
                      <pre className="h-[520px] overflow-auto rounded-md bg-slate-950 p-4 text-xs leading-relaxed text-emerald-100">
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
                      <Button className="w-full" variant="outline" disabled={!selectedProjectReady || !canUseDb || loading} onClick={() => createJob("update_local_modules", { project: selectedProject?.name, db: selectedDb })}>
                        <ListRestart className="h-4 w-4" />
                        MAJ addons projet
                      </Button>
                      <Button className="w-full" disabled={!selectedProjectReady || !canUseDb || loading} onClick={requestUpdateAllOdooModules}>
                        <RefreshCcw className="h-4 w-4" />
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
                      <CardTitle>Lier des modules</CardTitle>
                      <CardDescription>Crée les liens symboliques vers le dossier addons du projet.</CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-2">
                      <Input placeholder="/Users/.../addons" value={sourcePath} onChange={(event) => setSourcePath(event.target.value)} />
                      <Button className="w-full" variant="outline" disabled={!selectedProjectReady || !sourcePath} onClick={() => createJob("link_modules", { project: selectedProject?.name, source: sourcePath })}>
                        <Link2 className="h-4 w-4" />
                        Lier au projet
                      </Button>
                    </CardContent>
                  </Card>
                  <Card>
                    <CardHeader>
                      <CardTitle>Zone sensible</CardTitle>
                      <CardDescription>Actions irréversibles ou externes.</CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-2">
                      <Button className="w-full" variant="outline" onClick={() => createJob("create_project_terminal")}>
                        <SquareTerminal className="h-4 w-4" />
                        Nouveau projet via Terminal
                      </Button>
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

      <Dialog open={settingsOpen} onOpenChange={setSettingsOpen}>
        <DialogContent className="max-h-[90vh] max-w-2xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Paramètres du gestionnaire</DialogTitle>
            <DialogDescription>
              Le workspace est le dossier analysé pour lister les projets et celui utilisé lors des prochaines créations.
            </DialogDescription>
          </DialogHeader>
          {settingsDraft && (
            <div className="grid gap-4">
              <label className="grid min-w-0 gap-1.5 text-sm font-medium">
                Dossier des projets
                <Input
                  value={settingsDraft.workspace}
                  onChange={(event) => setSettingsDraft({ ...settingsDraft, workspace: event.target.value })}
                  placeholder="/chemin/vers/Odoo-projects"
                />
                <span className="break-all text-xs font-normal text-muted-foreground">
                  Le dossier est créé s’il n’existe pas encore.
                </span>
              </label>

              <div className="grid gap-4 md:grid-cols-2">
                <label className="grid gap-1.5 text-sm font-medium">
                  Mode d’exécution
                  <Select
                    value={settingsDraft.execution_mode}
                    onValueChange={(value) => setSettingsDraft({ ...settingsDraft, execution_mode: value })}
                  >
                    <SelectTrigger><SelectValue /></SelectTrigger>
                    <SelectContent>
                      <SelectItem value="native">Natif</SelectItem>
                      <SelectItem value="wsl">WSL 2 (Windows)</SelectItem>
                    </SelectContent>
                  </Select>
                </label>
                <label className="grid gap-1.5 text-sm font-medium">
                  Distribution WSL
                  <Input
                    value={settingsDraft.wsl_distribution}
                    disabled={settingsDraft.execution_mode !== "wsl"}
                    onChange={(event) => setSettingsDraft({ ...settingsDraft, wsl_distribution: event.target.value })}
                    placeholder="Ubuntu"
                  />
                </label>
              </div>

              <div className="grid gap-4 md:grid-cols-2">
                <label className="grid gap-1.5 text-sm font-medium">
                  Commande Docker
                  <Input
                    value={settingsDraft.docker_executable}
                    onChange={(event) => setSettingsDraft({ ...settingsDraft, docker_executable: event.target.value })}
                    placeholder="docker"
                  />
                </label>
                <label className="grid gap-1.5 text-sm font-medium">
                  Commande Brainkeys
                  <Input
                    value={settingsDraft.brainkeys_executable}
                    onChange={(event) => setSettingsDraft({ ...settingsDraft, brainkeys_executable: event.target.value })}
                    placeholder="brainkeys"
                  />
                </label>
              </div>

              <label className="grid min-w-0 gap-1.5 text-sm font-medium">
                Dossier Traefik
                <Input
                  value={settingsDraft.traefik_directory}
                  onChange={(event) => setSettingsDraft({ ...settingsDraft, traefik_directory: event.target.value })}
                  placeholder="Détection automatique si vide"
                />
              </label>

              <div className="grid gap-4 md:grid-cols-2">
                <label className="grid gap-1.5 text-sm font-medium">
                  Terminal
                  <Input
                    value={settingsDraft.terminal}
                    onChange={(event) => setSettingsDraft({ ...settingsDraft, terminal: event.target.value })}
                    placeholder="auto"
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
              </div>

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
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={zipDialogOpen} onOpenChange={setZipDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Importer un ZIP de modules</DialogTitle>
            <DialogDescription>
              Le backend détecte les dossiers contenant un manifeste Odoo, extrait le ZIP puis crée les liens symboliques.
            </DialogDescription>
          </DialogHeader>
          <Input ref={zipInputRef} type="file" accept=".zip" />
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
          <Button onClick={importZip} disabled={loading}>
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <FileArchive className="h-4 w-4" />}
            Importer
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
              Cette action retire les modules de `odoo/addons`. Si le module vient d’un ZIP importé, le lien et le dossier extrait dans
              `.odoo_manager_imports` sont retirés du chemin actif.
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
