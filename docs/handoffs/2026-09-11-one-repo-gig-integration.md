# One-repository architecture → Gig Work integration handoff

## Exact source boundary

- Repository: `Daisuke134/life-manager`
- Source branch: `chore/one-repo-finalize-20260911`
- Architecture source head: `ba1d027b8870bf304aa215ae61d0693196bec952`
- Main merged through: `5c007ea4752eb8b9cdfae5c49750d39296e93484`
- Divergence at handoff: `origin/main...source` = 0 behind / 38 ahead
- Exact changed-file inventory (88 paths):

  ```bash
  git diff --name-only 5c007ea4752eb8b9cdfae5c49750d39296e93484...ba1d027b8870bf304aa215ae61d0693196bec952
  ```

The source branch owns the shared architecture, Mobile App bootstrap/assets/registry, ebook asset and HeyGen
portability, Marketing Engine shared product routing/measurement/reporting, Local/Cloud onboarding catalog, README,
and architecture spec. It does **not** own or intentionally edit the active Coconala, Lancers, or CrowdWorks provider
runtime. CrowdWorks changes visible in merge commits came from `origin/main`, not this branch.

## Verified source state

- Mobile App bootstrap: Astra `SHIP`; focused tests 30/30 after latest-main merge.
- Ebook asset pack: Astra `SHIP`; focused tests 26/26 plus real FFmpeg provision/replay.
- Shared Local/Cloud onboarding: Astra `SHIP`; new tests 7/7 and existing onboarding, Cloud `/start`, and daemon-free
  install tests 8/8 after latest-main merge.
- Structural acceptance: isolated fresh clone at `b321e674ee` passes OSS verification, daemon-free install, app tests
  975/975, eval and panel-privacy 24/24. Current source adds crash-safe HeyGen creation/download recovery and
  Telegram send-once claims; focused ebook tests pass 13/13, Local/Cloud acceptance passes 46/46, common contracts
  pass 15/15, and host-neutral lifecycle tests pass 392/392.
- Final structural fixes at reviewed source head `ba1d027b8870bf304aa215ae61d0693196bec952` pass Local
  credential/catalog tests 10/10, HeyGen rename-after-crash recovery and runner tests 9/9, and the OSS dependency
  fence. Fresh read-only Astra review reports no P0/P1/P2 issue and returns `SHIP`.
- `ARCH-13f` and `ARCH-13g` are complete in the architecture spec.
- No active Gig Work loop was stopped, restarted, or directly edited.

## Integration outcome

The Gig provider sequence and shared marketplace architecture were already merged through `origin/main`
`5c007ea4752eb8b9cdfae5c49750d39296e93484` (PR #5024), and that main is an ancestor of this architecture branch.
The combined fixture acceptance passes 318/318 for shared marketplace-core, changed CrowdWorks provider paths and
HeyGen replay, plus 10/10 for Local/Cloud onboarding and Telegram credentials. No production workload was invoked.

## Instructions for future Gig Work changes

1. Preserve the separately owned, currently running Coconala/Lancers/CrowdWorks work. Do not replace its state,
   receipts, browser profiles, or provider readback with files from this branch.
2. Start from latest `origin/main`; do not revive an older provider branch or reapply an already merged effect.
3. Resolve only real overlaps. The architecture branch's public 14-loop descriptions and
   `apps/life-manager/config/product-loop-catalog.json` must reflect the final Gig installers honestly; the Gig owner
   retains authority over provider-specific execution.
4. Converge Coconala, Lancers, and CrowdWorks on `skills/_shared/marketplace-core` one contract at a time:
   application, reply, storefront, paid work/delivery, financial record, and Telegram. Provider directories retain
   only provider-specific effects and official readback.
5. Run the Gig owner's acceptance suite plus the architecture checks listed below. Do not restart production merely
   to prove this structural merge.
6. Record the exact new head and PASS evidence for any later change.

## Architecture checks after integration

```bash
node --test \
  apps/life-manager/lib/telegram-credentials.test.js \
  apps/life-manager/lib/product-onboarding.test.js \
  apps/life-manager/lib/mobile-product-bootstrap.test.js \
  apps/life-manager/lib/mobile-product-registry.test.js \
  apps/life-manager/lib/mobile-starter-pack.test.js \
  apps/life-manager/lib/mobile-product-assets.test.js
git diff --check
```

The final integrated branch must also run the Gig owner's focused Coconala/Lancers/CrowdWorks tests. A failure in a
provider-owned suite is fixed by that owner; it must not be hidden by weakening the shared contract.

## Remaining gates

1. Retain `~/.openclaw` and `~/.hermes`: the latest read-only census found no matching process, but the directories
   still contain credentials, sessions, ledgers, protected state and Gig compatibility owned outside this branch.
   They are not public clean-clone dependencies and must not be bulk-deleted as regenerable cache.
2. Final latest-main verification, PR/main integration, and handover.

## User-sendable prompt

> The one-repository architecture is already integrated with Gig changes through main PR #5024. For future Gig work,
> start from current `origin/main` and read
> `docs/handoffs/2026-09-11-one-repo-gig-integration.md` and
> `docs/superpowers/specs/2026-09-06-life-manager-one-repo-two-runtimes-design.md` first. Preserve provider-specific
> effects/readback, but converge Coconala, Lancers, and CrowdWorks on `skills/_shared/marketplace-core` for application,
> reply, storefront, paid delivery, financial records, and Telegram. Update the public 14-loop catalog only where your
> final installers prove a different truth. Run your full Gig acceptance plus the handoff architecture checks. Record
> the exact integrated head and evidence in the spec, push it, and return the branch/head. Do not touch
> unrelated Mobile App, ebook, Agent Economy, or Local/Cloud onboarding behavior.
