import { test } from "node:test";
import assert from "node:assert/strict";
import { isWslSetupPending, wslSetupError, wslSetupState, type WslStatus } from "./wsl-setup.ts";

const status = (overrides: Partial<WslStatus> = {}): WslStatus => ({
  wslInstalled: true,
  wslVersion: "2.4.12.0",
  supportsFileImport: true,
  distribution: "SDK-Manager",
  distributionInstalled: true,
  release: "0.5.0",
  ...overrides,
});

test("a prepared machine asks for nothing", () => {
  assert.equal(wslSetupState(status(), "0.5.0").step, "ready");
  assert.equal(isWslSetupPending(status(), "0.5.0"), false);
});

test("WSL is installed first, with a single elevation", () => {
  const state = wslSetupState(status({ wslInstalled: false, wslVersion: "" }), "0.5.0");
  assert.equal(state.step, "install-wsl");
  assert.equal(state.needsElevation, true);
});

test("a WSL too old to import a file is called out instead of failing later", () => {
  const state = wslSetupState(status({ supportsFileImport: false, wslVersion: "2.0.9" }), "0.5.0");
  assert.equal(state.step, "outdated-wsl");
  assert.match(state.detail, /2\.0\.9/);
});

test("the environment is installed in one click once WSL is there", () => {
  const state = wslSetupState(status({ distributionInstalled: false, release: "" }), "0.5.0");
  assert.equal(state.step, "install-environment");
  assert.equal(state.needsElevation, false);
});

test("an application update reprovisions the environment, never the projects", () => {
  const state = wslSetupState(status({ release: "0.4.0" }), "0.5.0");
  assert.equal(state.step, "update-environment");
  assert.match(state.detail, /pas touchés/);
});

test("a machine without the bridge is reported as unsupported, not broken", () => {
  assert.equal(wslSetupState(null, "0.5.0").step, "unsupported");
  assert.equal(isWslSetupPending(null, "0.5.0"), false);
});

test("failures are explained in terms the user can act on", () => {
  assert.match(wslSetupError(new Error("L'image ne correspond pas à son empreinte : installation refusée.")), /Retélécharge/);
  assert.match(wslSetupError(new Error("ENOENT: no such file or directory")), /absente/);
  assert.match(wslSetupError(new Error("Erreur 0x80370102")), /virtualisation/i);
  assert.equal(wslSetupError(new Error("Échec inattendu")), "Échec inattendu");
});

test("Electron's technical wrapper is removed from native errors", () => {
  const wrapped = new Error("Error invoking remote method 'sdk:wsl-prepare': Error: Échec inattendu");
  assert.equal(wslSetupError(wrapped), "Échec inattendu");
});
