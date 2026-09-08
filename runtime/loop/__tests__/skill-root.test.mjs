import assert from 'node:assert/strict';
import { accessSync, chmodSync, constants, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import test from 'node:test';
import { resolveSkillPath, runSkill } from '../run-skill.mjs';

test('daemon never creates a second source tree', () => {
  const repoRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..', '..');
  const source = readFileSync(path.join(repoRoot, 'runtime', 'anicca-daemon.sh'), 'utf8');
  assert.doesNotMatch(source, /\brsync\b/);
  assert.doesNotMatch(source, /\$ANICCA_HOME\/skills(?:\/|["' }]|$)/m);
  assert.match(source, /export LIFE_MANAGER_SKILLS_ROOT=/);
  assert.match(source, /export LIFE_MANAGER_SKILLS_STATE_ROOT=/);
  assert.match(source, /export ANICCA_REPO=/);
});

test('registry entrypoint resolves from a skills root separate from ANICCA_HOME', async () => {
  const root = mkdtempSync(path.join(tmpdir(), 'lm-skill-root-'));
  const skillsRoot = path.join(root, 'release', 'skills');
  const home = path.join(root, 'instance');
  const stateRoot = path.join(home, 'state', 'skills');
  const slotDir = path.join(skillsRoot, 'custom');
  mkdirSync(slotDir, { recursive: true });
  writeFileSync(path.join(skillsRoot, 'registry.json'), JSON.stringify({
    slots: { custom: { status: 'live', dir: 'skills/custom', entrypoint: 'actual-entry.sh' } },
  }));
  const entry = path.join(slotDir, 'actual-entry.sh');
  writeFileSync(entry, '#!/bin/sh\nprintf "%s\\n" "$LIFE_MANAGER_SKILLS_STATE_ROOT|$EARN_LEDGER"\n');
  chmodSync(entry, 0o755);

  const config = { ANICCA_HOME: home, LIFE_MANAGER_SKILLS_ROOT: skillsRoot, LIFE_MANAGER_SKILLS_STATE_ROOT: stateRoot };
  assert.equal(resolveSkillPath('custom', config), entry);
  const result = await runSkill('custom', {}, 'wake-1', config);
  assert.equal(result.exitCode, 0);
  assert.equal(result.output.trim(), `${stateRoot}|${stateRoot}/earn/earn-ledger.jsonl`);
});

test('canonical registry non-run.sh entrypoints are executable', () => {
  const repoRoot = path.resolve(path.dirname(new URL(import.meta.url).pathname), '..', '..', '..');
  const skillsRoot = path.join(repoRoot, 'skills');
  const config = { ANICCA_HOME: '/tmp/unused-instance', LIFE_MANAGER_SKILLS_ROOT: skillsRoot };
  const resolved = {
    'x-repost': path.join(skillsRoot, 'x-repost', 'x-repost-cli.sh'),
    report: path.join(skillsRoot, 'report', 'anicca-report.sh'),
    'economy/ubi': path.join(skillsRoot, 'economy', 'ubi', 'run.sh'),
  };
  for (const [slot, entrypoint] of Object.entries(resolved)) {
    assert.equal(resolveSkillPath(slot, config), entrypoint);
    accessSync(entrypoint, constants.X_OK);
  }
  assert.match(readFileSync(path.join(skillsRoot, 'registry.json'), 'utf8'), /"entrypoint": "run\.sh"/);
});
