import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// Un menu modal verrouille le défilement du body ; avec html en overflow-x: clip,
// l'en-tête du projet et la barre latérale collés disparaissent pendant l'ouverture.
test("every dropdown menu is non-modal to keep sticky layout in place", () => {
  const page = readFileSync(new URL("../app/page.tsx", import.meta.url), "utf8");
  const roots = page.match(/<DropdownMenu\.Root\b[^>]*>/g) ?? [];
  assert.ok(roots.length > 0);
  assert.deepEqual(roots.filter((root) => !root.includes("modal={false}")), []);
});
