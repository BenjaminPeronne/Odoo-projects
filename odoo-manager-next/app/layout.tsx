import type { Metadata } from "next";
import "@radix-ui/themes/styles.css";
import "./globals.css";
import { AppThemeProvider } from "@/components/theme-provider";

export const metadata: Metadata = {
  title: "SDK Local Manager",
  description: "Sudokeys — vos projets Odoo locaux, réunis dans un seul espace.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="fr" suppressHydrationWarning>
      <body>
        <AppThemeProvider>{children}</AppThemeProvider>
      </body>
    </html>
  );
}
