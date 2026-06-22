import type { NextConfig } from "next";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const backend = process.env.ODOO_MANAGER_API || "http://127.0.0.1:8765";
const configDir = dirname(fileURLToPath(import.meta.url));
const desktopBuild = process.env.TAURI_BUILD === "1";

const browserConfig: NextConfig = {
  outputFileTracingRoot: configDir,
  async rewrites() {
    return [
      {
        source: "/api/:path*",
        destination: `${backend}/api/:path*`,
      },
    ];
  },
};

const nextConfig: NextConfig = desktopBuild
  ? { output: "export", outputFileTracingRoot: configDir }
  : browserConfig;

export default nextConfig;
