import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Gestionnaire Odoo local",
  description: "Interface moderne pour piloter les projets Odoo locaux.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="fr">
      <body>{children}</body>
    </html>
  );
}
