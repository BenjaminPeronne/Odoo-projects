import type { NextConfig } from "next";
import { execFileSync } from "node:child_process";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";

const backend = process.env.ODOO_MANAGER_API || "http://127.0.0.1:18765";
const configDir = dirname(fileURLToPath(import.meta.url));
const desktopBuild = process.env.ELECTRON_BUILD === "1";

function git(...args: string[]) {
  try {
    return execFileSync("git", args, { cwd: configDir, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  } catch {
    return "";
  }
}

// Numéro de build figé dans l'interface : tag app-v<version>-build<N> du build (CI, script
// build_all_platforms.sh) ou tag posé sur le commit courant, et commit court pour le support.
function buildIdentity() {
  const tag = process.env.ODOO_MANAGER_BUILD_TAG
    || (process.env.GITHUB_REF_TYPE === "tag" ? process.env.GITHUB_REF_NAME : "")
    || git("describe", "--tags", "--exact-match", "--match", "app-v*");
  const build = /^app-v\d+\.\d+\.\d+-build(\d+)$/.exec(tag || "")?.[1] ?? "";
  const commit = (process.env.GITHUB_SHA || git("rev-parse", "HEAD")).slice(0, 7);
  return { NEXT_PUBLIC_APP_BUILD: build, NEXT_PUBLIC_APP_COMMIT: commit };
}

const env = buildIdentity();

const browserConfig: NextConfig = {
  agentRules: false,
  env,
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
  ? { agentRules: false, env, output: "export", outputFileTracingRoot: configDir }
  : browserConfig;

export default nextConfig;
