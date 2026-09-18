// Préparation du poste Windows : ce qu'il reste à faire, et dans quel ordre.
//
// Les projets servis depuis C:\ traversent le pont 9P de Docker Desktop : Odoo met
// 48 à 95 s à démarrer contre 4 à 6 s dans l'environnement Linux. L'utilisateur ne
// voit qu'un bouton ; cette logique dit lequel afficher.

export interface WslStatus {
  wslInstalled: boolean;
  wslVersion: string;
  supportsFileImport: boolean;
  distribution: string;
  distributionInstalled: boolean;
  release: string;
}

export type WslSetupStep = "unsupported" | "install-wsl" | "outdated-wsl" | "install-environment" | "update-environment" | "ready";

export interface WslSetupState {
  step: WslSetupStep;
  title: string;
  detail: string;
  actionLabel: string;
  /** Vrai quand l'action demande les droits administrateur une seule fois. */
  needsElevation: boolean;
}

const READY: WslSetupState = {
  step: "ready",
  title: "Environnement Linux prêt",
  detail: "Les projets tournent sur un système de fichiers Linux : Odoo démarre en quelques secondes.",
  actionLabel: "",
  needsElevation: false,
};

export function wslSetupState(status: WslStatus | null, applicationVersion: string): WslSetupState {
  if (!status) {
    return {
      step: "unsupported",
      title: "Environnement Linux indisponible",
      detail: "Cette version de l'application ne gère pas l'environnement Linux sur ce système.",
      actionLabel: "",
      needsElevation: false,
    };
  }
  if (!status.wslInstalled) {
    return {
      step: "install-wsl",
      title: "Activer WSL",
      detail: "Windows doit installer WSL. Une autorisation est demandée, puis un redémarrage peut être nécessaire.",
      actionLabel: "Activer WSL",
      needsElevation: true,
    };
  }
  if (!status.supportsFileImport) {
    return {
      step: "outdated-wsl",
      title: "Mettre à jour WSL",
      detail: `WSL ${status.wslVersion} est trop ancien pour installer l'environnement. Mets-le à jour, puis reviens ici.`,
      actionLabel: "Mettre à jour WSL",
      needsElevation: true,
    };
  }
  if (!status.distributionInstalled) {
    return {
      step: "install-environment",
      title: "Préparer mon poste",
      detail: "L'application installe son environnement Linux, avec Docker et Git. Aucune autre action ne sera demandée.",
      actionLabel: "Préparer mon poste",
      needsElevation: false,
    };
  }
  if (status.release !== applicationVersion) {
    return {
      step: "update-environment",
      title: "Mettre à jour l'environnement",
      detail: `L'environnement est en version ${status.release || "inconnue"}, l'application en ${applicationVersion}. Les projets et les bases ne sont pas touchés.`,
      actionLabel: "Mettre à jour",
      needsElevation: false,
    };
  }
  return READY;
}

export function isWslSetupPending(status: WslStatus | null, applicationVersion: string): boolean {
  const { step } = wslSetupState(status, applicationVersion);
  return step !== "ready" && step !== "unsupported";
}

/** Message lisible pour un échec de préparation, sans jargon. */
export function wslSetupError(error: unknown): string {
  const message = (error instanceof Error ? error.message : String(error ?? "")).replace(/^Error invoking remote method '[^']+': (Error: )?/, "");
  if (/empreinte/i.test(message)) {
    return "L'image de l'environnement ne correspond pas à son empreinte. Retélécharge l'application.";
  }
  if (/ENOENT|introuvable|no such file/i.test(message)) {
    return "L'image de l'environnement est absente de cette installation. Retélécharge l'application complète.";
  }
  if (/0x80370102|virtualis/i.test(message)) {
    return "La virtualisation est désactivée dans le BIOS de ce poste. Active-la, puis réessaie.";
  }
  return message || "La préparation a échoué.";
}
