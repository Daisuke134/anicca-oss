// RED-first: pure earn-slot predicate + strategy mapping (REQ-1/REQ-3/REQ-4 NEW-003).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { isEarnSlot, earnStrategyFor, earnSkillRelPath } from '../earn-slot.mjs';
import { classifyEarnResult } from '../earn-detect.mjs';
import { isProfitable } from '../../../skills/_shared/lib/ledger.mjs';
test('isEarnSlot: legacy action slots are earn', () => { for (const s of ['earn','yield','hl_trade','x402_sell','token_launch']) assert.equal(isEarnSlot(s), true, s); });
test('isEarnSlot: any earn/<sub> is earn', () => { for (const s of ['earn/gig','earn/clip','earn/affiliate','earn/example','earn/audit','earn/_probe']) assert.equal(isEarnSlot(s), true, s); });
test('isEarnSlot: non-earn slots NOT earn', () => { for (const s of ['cook','report','self/issue-dev','self/spawn','economy/ubi','',undefined,null]) assert.equal(isEarnSlot(s), false, String(s)); });
test('earnStrategyFor: legacy map', () => { assert.equal(earnStrategyFor('yield'),'yield'); assert.equal(earnStrategyFor('hl_trade'),'hl'); assert.equal(earnStrategyFor('x402_sell'),'x402'); assert.equal(earnStrategyFor('token_launch'),'token'); });
test('earnStrategyFor: fat earn → null', () => assert.equal(earnStrategyFor('earn'), null));
test('earnStrategyFor: earn/<sub> → <sub>', () => { assert.equal(earnStrategyFor('earn/gig'),'gig'); assert.equal(earnStrategyFor('earn/_probe'),'_probe'); });
test('earnStrategyFor: non-earn → null', () => assert.equal(earnStrategyFor('cook'), null));

// REQ-4 (FIND-IMPL-003): path resolution rule — action slots → fat earn/run.sh; earn/<sub> + non-earn → <slot>/run.sh
test('earnSkillRelPath: legacy action slots → earn/run.sh', () => {
  for (const s of ['earn', 'yield', 'hl_trade', 'x402_sell', 'token_launch']) assert.equal(earnSkillRelPath(s), 'earn/run.sh', s);
});
test('earnSkillRelPath: earn/<sub> → earn/<sub>/run.sh', () => {
  assert.equal(earnSkillRelPath('earn/gig'), 'earn/gig/run.sh');
  assert.equal(earnSkillRelPath('earn/_probe'), 'earn/_probe/run.sh');
});
test('earnSkillRelPath: non-earn → <slot>/run.sh', () => {
  assert.equal(earnSkillRelPath('cook'), 'cook/run.sh');
  assert.equal(earnSkillRelPath('self/issue-dev'), 'self/issue-dev/run.sh');
});

// REQ-2 / FIND-IMPL-002 (GAP-A): the classify mechanism the gate calls — a profitable earn-ledger line
// (correlated by `wake`===wakeId) yields profitable:true; a non-profitable one yields false. The gate is
// `isEarnSlot(slot) ? classifyEarnResult(...)`; isEarnSlot is tested above, this tests classifyEarnResult.
function tmpLedger(lines) {
  const p = path.join(os.tmpdir(), `earn-ledger-${Date.now()}-${Math.floor(performance.now())}.jsonl`);
  fs.writeFileSync(p, lines.map(l => JSON.stringify(l)).join('\n') + '\n');
  return p;
}
test('classifyEarnResult: profitable line (wake-correlated) → profitable:true', async () => {
  const p = tmpLedger([{ wake: 'W1', source: 'gig', tx: '0xabc', status: '0x1', net_usdc: 2.5, external: true }]);
  const r = await classifyEarnResult('W1', p, isProfitable);
  assert.equal(r.profitable, true);
  fs.unlinkSync(p);
});
test('classifyEarnResult: non-profitable (no external/tx) → profitable:false', async () => {
  const p = tmpLedger([{ wake: 'W1', source: '_probe', earn_usdc: 0.01 }]);
  const r = await classifyEarnResult('W1', p, isProfitable);
  assert.equal(r.profitable, false);
  fs.unlinkSync(p);
});
test('classifyEarnResult: no line for this wake → profitable:false', async () => {
  const p = tmpLedger([{ wake: 'OTHER', tx: '0x1', status: '0x1', net_usdc: 9, external: true }]);
  const r = await classifyEarnResult('W1', p, isProfitable);
  assert.equal(r.profitable, false);
  fs.unlinkSync(p);
});
