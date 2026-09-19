"use client";

import { CheckCircle2, CircuitBoard, Loader2, ShieldCheck, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { desktopBridge } from "@/lib/desktop";
import { wslSetupError, wslSetupState, type WslStatus } from "@/lib/wsl-setup";

/** Écran de préparation du poste : un bouton, aucune commande à taper. */
export function WslSetupDialog({
  open,
  onOpenChange,
  applicationVersion,
  onReady,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  applicationVersion: string;
  onReady?: () => void;
}) {
  const [status, setStatus] = useState<WslStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [rebootRequired, setRebootRequired] = useState(false);

  const refresh = useCallback(async () => {
    const bridge = desktopBridge();
    if (!bridge?.wslStatus) return;
    try {
      setStatus(await bridge.wslStatus());
    } catch (cause) {
      setError(wslSetupError(cause));
    }
  }, []);

  useEffect(() => {
    if (open) void refresh();
  }, [open, refresh]);

  const state = wslSetupState(status, applicationVersion);

  const act = useCallback(async () => {
    const bridge = desktopBridge();
    if (!bridge) return;
    setBusy(true);
    setError("");
    try {
      if (state.step === "install-wsl" || state.step === "outdated-wsl") {
        const result = await bridge.wslInstallWsl!();
        setRebootRequired(Boolean(result?.rebootRequired));
        if (!result?.ok && !result?.rebootRequired) setError(result?.message || "L'activation de WSL a échoué.");
      } else {
        const updated = await bridge.wslPrepare!();
        setStatus(updated);
        if (updated?.distributionInstalled && updated.release === applicationVersion) onReady?.();
      }
      await refresh();
    } catch (cause) {
      setError(wslSetupError(cause));
    } finally {
      setBusy(false);
    }
  }, [applicationVersion, onReady, refresh, state.step]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl space-y-5">
        <DialogHeader>
          <DialogTitle>{state.title}</DialogTitle>
          <DialogDescription>{state.detail}</DialogDescription>
        </DialogHeader>

        <div className="divide-y overflow-hidden rounded-md border">
          <SetupRow
            ready={Boolean(status?.wslInstalled)}
            title="WSL"
            detail={status?.wslInstalled ? `Version ${status.wslVersion}` : "Sera activé par Windows, avec une autorisation."}
          />
          <SetupRow
            ready={Boolean(status?.distributionInstalled)}
            title="Environnement Linux"
            detail={
              status?.distributionInstalled
                ? `${status.distribution} · version ${status.release || "inconnue"}`
                : "Docker, Git et le gestionnaire, installés en une fois."
            }
          />
        </div>

        {rebootRequired && (
          <p className="flex items-start gap-2 text-sm text-amber-600 dark:text-amber-400">
            <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
            Windows doit redémarrer pour terminer l’activation de WSL. La préparation reprendra au prochain lancement.
          </p>
        )}
        {error && (
          <p className="flex items-start gap-2 text-sm text-destructive">
            <TriangleAlert className="mt-0.5 h-4 w-4 shrink-0" />
            {error}
          </p>
        )}
        {state.step === "install-environment" && (
          <p className="flex items-start gap-2 text-sm text-muted-foreground">
            <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" />
            L’installation dure quelques minutes. Tes projets déjà présents sur ce poste ne sont pas modifiés.
          </p>
        )}

        <div className="flex flex-col-reverse gap-2 pt-1 sm:flex-row sm:justify-end">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>
            Plus tard
          </Button>
          {state.actionLabel && (
            <Button onClick={act} disabled={busy}>
              {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <CircuitBoard className="h-4 w-4" />}
              {busy ? "Préparation…" : state.actionLabel}
            </Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function SetupRow({ ready, title, detail }: { ready: boolean; title: string; detail: string }) {
  return (
    <div className="flex items-center gap-3 px-3 py-3">
      {ready ? (
        <CheckCircle2 className="h-5 w-5 shrink-0 text-emerald-600 dark:text-emerald-400" />
      ) : (
        <CircuitBoard className="h-5 w-5 shrink-0 text-muted-foreground" />
      )}
      <div className="min-w-0">
        <p className="text-sm font-medium">{title}</p>
        <p className="truncate text-sm text-muted-foreground">{detail}</p>
      </div>
    </div>
  );
}
