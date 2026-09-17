import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Le verrou de défilement des dialogues passe body en overflow: hidden ; avec html en
// overflow-x: clip, body devient un conteneur de défilement et la barre latérale collée remonte.
test("dialog scroll lock is moved to the root to keep the sticky sidebar in place", () => {
  const css = readFileSync(new URL("../app/globals.css", import.meta.url), "utf8");
  assert.match(css, /html:has\(> body\[data-scroll-locked\]\)\s*\{\s*overflow: hidden;/);
  assert.match(css, /html body\[data-scroll-locked\]\s*\{\s*overflow: visible !important;/);
});
