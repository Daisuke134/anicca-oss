import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");

test("root installer dispatches Connector before the generic installer", () => {
  const source = readFileSync(join(root, "install.sh"), "utf8");
  const connector = source.indexOf('connector)');
  const generic = source.indexOf('LIFE_MANAGER_HOME=');
  assert.ok(connector > 0 && connector < generic);
  assert.match(source, /skills\/connector\/install\.sh/);
});

test("Connector one-command installer reuses immutable release and lm-loop control plane", () => {
  const source = readFileSync(join(root, "skills/connector/install.sh"), "utf8");
  assert.match(source, /bin\/cut-loop-release\.sh/);
  assert.match(source, /lm-loop" apply "\$LOOP_ID"/);
  assert.match(source, /lm-loop" start "\$LOOP_ID"/);
  assert.match(source, /lm-loop" status "\$LOOP_ID"/);
  assert.doesNotMatch(source, /launchctl (?:load|unload|bootstrap|bootout|kickstart)/);
});
