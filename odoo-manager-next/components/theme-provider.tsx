"use client";

import { Theme } from "@radix-ui/themes";
import { ThemeProvider as NextThemeProvider, useTheme } from "next-themes";
import { useEffect, useState, type ReactNode } from "react";

function RadixTheme({ children }: { children: ReactNode }) {
  const [mounted, setMounted] = useState(false);
  const { resolvedTheme } = useTheme();

  useEffect(() => setMounted(true), []);

  return (
    <Theme
      appearance={mounted && resolvedTheme === "dark" ? "dark" : "light"}
      accentColor="orange"
      grayColor="sand"
      radius="medium"
      scaling="100%"
      hasBackground={false}
      className="min-h-screen"
    >
      {children}
    </Theme>
  );
}

export function AppThemeProvider({ children }: { children: ReactNode }) {
  return (
    <NextThemeProvider
      attribute="class"
      defaultTheme="dark"
      enableSystem
      enableColorScheme
      storageKey="odoo-manager-theme"
    >
      <RadixTheme>{children}</RadixTheme>
    </NextThemeProvider>
  );
}
