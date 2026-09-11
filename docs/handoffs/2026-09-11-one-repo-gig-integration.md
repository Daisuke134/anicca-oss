# One-repository architecture → Gig Work integration handoff

## Exact source boundary

- Repository: `Daisuke134/life-manager`
- Source branch: `chore/one-repo-finalize-20260911`
- Architecture source head: `28055f999cc32b9d5e74466670a18a02f8b65da4`
- Main merged through: `5c007ea4752eb8b9cdfae5c49750d39296e93484`
- Divergence at handoff: `origin/main...source` = 0 behind / 35 ahead
- Exact changed-file inventory (84 paths):

  ```bash
  git diff --name-only 5c007ea4752eb8b9cdfae5c49750d39296e93484...28055f999cc32b9d5e74466670a18a02f8b65da4
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
- `ARCH-13f` and `ARCH-13g` are complete in the architecture spec.
- No active Gig Work loop was stopped, restarted, or directly edited.

## Integration instructions for the Gig Work owner

1. Preserve the separately owned, currently running Coconala/Lancers/CrowdWorks work. Do not replace its state,
   receipts, browser profiles, or provider readback with files from this branch.
2. Start from latest `origin/main`, then merge exact architecture source head
   `28055f999cc32b9d5e74466670a18a02f8b65da4` and the separately owned Gig Work result. The commit containing this
   handoff is documentation-only on top of that source boundary.
3. Resolve only real overlaps. The architecture branch's public 14-loop descriptions and
   `apps/life-manager/config/product-loop-catalog.json` must reflect the final Gig installers honestly; the Gig owner
   retains authority over provider-specific execution.
4. Converge Coconala, Lancers, and CrowdWorks on `skills/_shared/marketplace-core` one contract at a time:
   application, reply, storefront, paid work/delivery, financial record, and Telegram. Provider directories retain
   only provider-specific effects and official readback.
5. Run the Gig owner's acceptance suite plus the architecture checks listed below. Do not restart production merely
   to prove this structural merge.
6. Record the exact integrated head and PASS evidence in the architecture spec and repeat the structural gates on
   the integrated result.

## Architecture checks after integration

```bash
node --test \
  apps/life-manager/lib/product-onboarding.test.js \
  apps/life-manager/lib/mobile-product-bootstrap.test.js \
  apps/life-manager/lib/mobile-product-registry.test.js \
  apps/life-manager/lib/mobile-starter-pack.test.js \
  apps/life-manager/lib/mobile-product-assets.test.js
git diff --check
```

The final integrated branch must also run the Gig owner's focused Coconala/Lancers/CrowdWorks tests. A failure in a
provider-owned suite is fixed by that owner; it must not be hidden by weakening the shared contract.

## Remaining gates after the merge

1. Repeat `ARCH-13h` clean Local and fresh Cloud fixture acceptance on the integrated Gig result.
2. Final zero-reference and open-handle census for `~/.openclaw` and `~/.hermes`.
3. Delete those two regenerable legacy directories only when that census is zero and protected state is outside them.
4. Final latest-main verification, PR/main integration, and handover.

## User-sendable prompt

> You own the active Gig Work result. Integrate it without restarting or overwriting running Coconala/Lancers/
> CrowdWorks state. In `Daisuke134/life-manager`, start from latest `origin/main`, merge exact architecture head
> `28055f999cc32b9d5e74466670a18a02f8b65da4` from branch `chore/one-repo-finalize-20260911`, then merge your
> separately owned Gig result. Read
> `docs/handoffs/2026-09-11-one-repo-gig-integration.md` and
> `docs/superpowers/specs/2026-09-06-life-manager-one-repo-two-runtimes-design.md` first. Preserve provider-specific
> effects/readback, but converge Coconala, Lancers, and CrowdWorks on `skills/_shared/marketplace-core` for application,
> reply, storefront, paid delivery, financial records, and Telegram. Update the public 14-loop catalog only where your
> final installers prove a different truth. Run your full Gig acceptance plus the handoff architecture checks. Record
> the exact integrated head and evidence in the spec, push it, and return the branch/head. Do not touch
> unrelated Mobile App, ebook, Agent Economy, or Local/Cloud onboarding behavior.
