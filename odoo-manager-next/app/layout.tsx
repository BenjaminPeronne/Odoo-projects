import type { Metadata } from "next";
import "@radix-ui/themes/styles.css";
import "./globals.css";
import { AppThemeProvider } from "@/components/theme-provider";

export const metadata: Metadata = {
  title: "Gestionnaire Odoo local",
  description: "Interface moderne pour piloter les projets Odoo locaux.",
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
