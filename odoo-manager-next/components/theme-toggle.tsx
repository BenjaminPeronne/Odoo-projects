"use client";

import { Switch } from "@radix-ui/themes";
import { Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useState } from "react";

export function ThemeToggle() {
  const [mounted, setMounted] = useState(false);
  const { resolvedTheme, setTheme } = useTheme();

  useEffect(() => setMounted(true), []);

  const dark = mounted && resolvedTheme === "dark";
  const label = dark ? "Activer le thème clair" : "Activer le thème sombre";

  return (
    <span className="theme-switch-wrap" data-state={dark ? "checked" : "unchecked"} title={label}>
      <Switch
        className="theme-switch"
        size="2"
        checked={dark}
        disabled={!mounted}
        onCheckedChange={(checked) => setTheme(checked ? "dark" : "light")}
        aria-label={label}
      />
      <span className="theme-switch-icon" aria-hidden="true">
        <Sun className="theme-switch-sun h-3.5 w-3.5" aria-hidden="true" />
        <Moon className="theme-switch-moon h-3.5 w-3.5" aria-hidden="true" />
      </span>
    </span>
  );
}
