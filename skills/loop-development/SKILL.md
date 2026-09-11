---
name: loop-development
description: Develop, fix, deploy, migrate, or retire macOS Life Manager loops without modifying another loop's code, state, browser, launchd job, provider profile, or production release. Use for changes to loop entrypoints, config/loop-registry.json, launchd cadence, runtime events, releases, healthchecks, or cleanup.
---

# Life Manager Loop Development

Life Manager has one code source: GitHub `main`; one lifecycle registry:
`config/loop-registry.json`; and one operator interface: `bin/lm-loop`. A loop
owns business effects, not its plist, release selector, provider route, sibling
restart, global monitor, or shared cleanup.

```text
locked worktree -> focused test -> merged main -> immutable release -> lm-loop apply -> readback
                                                           |
                                                           +-> mutable private state outside release
```

## Before editing

1. Read the current spec, registry row, entrypoint, state path, loaded plist
   arguments, latest terminal event, and official effect receipt. Never infer
   production from a checkout or PID.
2. Fetch and record `HEAD`, upstream, `origin/main`, and dirty state. Work only
   in a dedicated linked worktree. Acquire and renew its owner/task lease with
   `scripts/worktree-lease.py`; see `docs/runbooks/worktree-lifecycle.md`. Never
   edit the shared checkout.
3. Name the exact loop IDs and files owned by one registry TODO. Do not modify a
   sibling loop unless the root cause is its shared runtime boundary.

## Source, state, and ownership

- Executable code, adapters, schemas, prompts, and dependency lockfiles live in
  this repository. Runtime may not depend on another checkout, worktree, or
  `~/.../skills` source tree.
- Credentials, state, logs, ledgers, receipts, sessions, browser profiles,
  evidence, and duplicate fences live outside Git and immutable releases.
- Local/self-hosted and Cloud/hosted are host adapters for one Product Loop,
  never separate business implementations. Share the loop ID, objective,
  domain kernel, provider adapter, effect fence, receipt vocabulary, Telegram
  report, and CFO event. Vary only supervisor, secret store, durable state and
  browser transport.
- Use this canonical ownership shape when adding or converging a loop:

  ```text
  loops/<loop>/                 objective, orchestration, durable cursor
  skills/_shared/<domain>/      reusable domain kernel proven by 2+ consumers
  runtime/                      lifecycle, agent runner, receipts, Telegram, CFO
  providers/<provider>/         provider-specific effects and official readback
  config/loop-registry.json     one lifecycle row and repository-relative entrypoint
  ```

  Existing repository paths may remain when moving them would be cosmetic; the
  ownership boundary and shared contract are mandatory, not the directory spelling.
- Telegram delivery must use the repository-owned shared Telegram transport.
  Revenue, expense and balance facts must use the shared financial/CFO event
  contract. External effects must produce a shared receipt plus provider-owned
  readback. Do not implement these separately inside a loop.
- OpenClaw, Hermes, another checkout, a worktree or a home-directory source tree
  may not be a Local or Cloud runtime dependency. An external service such as
  Postiz remains allowed only behind a repository-owned provider adapter; its
  source code is not imported at runtime.
- Add a loop with one registry row and one tested repository-relative
  entrypoint. Use `runtime/loop/entry_dispatch.py` when argv is required.
- Model work goes through `runtime/agent-runner/agent_runner.py` with a task
  class. Do not select provider credentials, `CODEX_HOME`, auth files, or a
  direct provider API inside loop code.
- Preserve the working interpreter/runtime named by the old loaded argv. Never
  replace a loop-specific venv with the control-plane Python merely because the
  script path is portable; generate the command, then import-smoke its runtime
  dependency on the target host before apply.
- Do not create production plists, loop installers, release watchers, or raw
  mutating `launchctl` calls. Use `lm-loop apply/start/stop/restart`. Never
  restart another loop's process, browser, profile, or state owner.
- Treat `launchctl` exit 141 as an unavailable control path, not permission to
  repair the host. Never stop, signal, replace, or restart ChatGPT/Codex
  app-server, Remote Control, the GUI bootstrap, loginwindow, or the Mac to
  recover a loop. Preserve the user's remote session and use read-only process
  evidence plus the next natural owner wake.
- Loaded `ProgramArguments` must contain one exact release directory, never a
  branch, worktree, mutable checkout, or `~/loops/current` symlink.

## Sustainable 24/7 loops

24/7 means durable progress across finite process lifetimes, not one immortal
agent, Node process, browser, or tab. Each scheduled wake performs one bounded,
idempotent transition from durable state, records a terminal receipt, and exits.
Long waits belong in persisted `next_eligible_at` state and launchd cadence.

- Put judgment in the model. Put leases, resource accounting, backoff, durable
  cursors, effect fences, and receipt verification in deterministic shared code.
- Every process, browser profile, CDP port, context, tab, renderer, worker, and
  mutable state root has one registry owner. Starting a second owner for the same
  resource fails closed; IPv4 and IPv6 listeners on one port are still one port.
- Every owner declares finite run time, concurrency, child-process, browser
  context/tab retention, artifact retention, and retry/backoff contracts. The
  fleet admits new work only while host headroom remains; pressure delays work
  instead of increasing concurrency or killing unrelated owners.
- Reconcile from desired and observed state after every natural wake. A stale
  heartbeat is evidence to inspect, not permission for blind restart. Recovery
  is bounded, records the first failure separately, and never retries an
  uncertain external effect without official readback.
- Browser cleanup is owner-scoped: prove profile/port/PID ownership and open
  resources before closing stale contexts or tabs. Never use a global Chromium,
  WindowServer, loginwindow, GUI-session, or host restart as loop recovery.
- Local macOS uses short launchd jobs plus external durable state. Cloud uses the
  platform's equivalent scheduler and durable store; both implement the same
  `observe -> decide -> act -> verify -> persist -> exit` contract. A second host
  improves availability only after effect leases and receipts prevent two hosts
  from acting on the same provider resource.

Turn every production outage into one retained, secret-free regression fixture:
trigger evidence, first failing component boundary, forbidden recovery actions,
and the observable acceptance condition. Never claim “self-healing” from PID or
restart counts; require resumed durable progress and official effect separation.

Reuse in layers. Before adding code to a gig, affiliate, trading, publishing,
health, or future loop, search the shared runtime and registry first:

1. `runtime/loop` owns lifecycle, admission, durable execution, retry/backoff,
   events, effect receipts, and recovery for every domain.
2. A domain module owns behavior genuinely shared by multiple loops in that
   domain, such as marketplace work items or publishing attribution.
3. A provider adapter owns only its API/DOM/session vocabulary and official
   effect/readback implementation.
4. A loop owns only its business objective, model context, and durable cursor.

Do not copy a shared primitive into a provider or loop directory. Do not extract
an abstraction from one speculative consumer: route a second real consumer
through the existing contract first, then move only proven duplicate behavior.
Keep failure classes separate. Memory pressure, disk pressure, authentication,
provider failure, and GUI-session loss have different evidence and recovery;
one threshold or watchdog must not treat them as interchangeable.

## Develop, merge, and deploy

1. Write the focused failing test first. Cover the real failure boundary:
   state outside release, exact argv, no sibling mutation, replay-zero,
   secret-free event, cleanup protection, or official effect readback.
2. Make the smallest root-cause change. Normal target is at most three files
   and 100 production LOC; split larger work by registry TODO.
3. Run focused tests, `git diff --check`, then:

   ```bash
   python3 -m unittest discover -s runtime/loop/tests -p 'test_*.py'
   node --test apps/life-manager/lib/loop-adapter-registry.test.js
   ~/loops/current/bin/lm-loop doctor
   ```

4. Fetch again, preserve concurrent commits, commit and push the task branch,
   and merge or verified-fast-forward `main`. Never force-push.
5. Cut only a pushed main commit with `bin/cut-loop-release.sh origin/main`.
   Build installs locked production dependencies before sealing the release
   read-only. Never patch an active release.
6. The host-wide apply lock must be free. Validate the full registry before
   mutation. Apply one label at a time in domain order; after every swap require
   plist argv, loaded argv, release SHA, state path, and rollback receipt.
7. Require a natural scheduled terminal event from the installed SHA. Keep
   launchd state, process result, and official effect result separate. Only
   official provider/account readback can set an external effect `verified`.

## Done gates

- Every managed label exists once, is enabled and loaded, and points to one
  existing immutable release. `doctor` reports unmanaged 0, missing 0, and
  installed-retired 0.
- Active entrypoints have no legacy installer, managed raw launchd mutation,
  direct provider selection, worktree source, or release-local mutable state.
- Dependency import smoke tests pass through the exact interpreter returned by
  the generated command. Runtime events validate with secret
  violations 0. Process success never substitutes for payment, message,
  publication, application, or trade readback.
- Cleanup replay has errors 0 and protected deletions 0 and preserves every
  loaded release, active run, receipt, ledger, credential, and session.
- 500-loop scale, clean-user install, reboot recovery, natural pass, official
  effect separation, gitleaks, and replay-zero pass.
- The worktree is clean, merged, and unused before its owner unlocks and removes
  the exact path. Lease expiry only requests owner verification; it never permits
  deletion. Never remove another session's dirty, unmerged, locked, or active worktree.

## One runtime table

```bash
~/loops/current/bin/lm-loop doctor
~/loops/current/bin/lm-loop status all
~/loops/current/bin/lm-loop watch all
```

Do not build another inventory or infer health from process searches.

## Recovery

Fail closed. Preserve old plist/release, use the apply lock, and restore prior
loaded argv on swap failure. Never retry an uncertain external effect. Recover
lifecycle through `lm-loop`; recover source by building a new pushed release.
Record the incident and missing gate in the control-plane spec.

## References

- `docs/loops/README.md`
- `docs/superpowers/specs/2026-08-27-macos-loop-control-plane-design.md`
- https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html
- https://git-scm.com/docs/git-worktree
- https://12factor.net/build-release-run
- https://github.com/kubernetes-sigs/controller-runtime/blob/main/pkg/reconcile/reconcile.go
- https://github.com/temporalio/temporal/blob/main/docs/architecture/README.md
- https://github.com/systemd/systemd/blob/main/man/systemd.resource-control.xml
