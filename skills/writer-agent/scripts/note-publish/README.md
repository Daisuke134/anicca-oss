# note publishing pipeline (repeatable, verified 2026-06-24)

One article (markdown) → a fully monetized note post: cover image, manual 目次, free hook,
member-only paid section, ¥500/月 membership gate. Auth = the daily-driver browser (Dais logged in once,
forever). Everything runs headless via an ephemeral CloakBrowser context + the decrypted note cookies —
it NEVER touches the live daily-driver tab.

## Pipeline (run in this order for a NEW article)

```
 SOURCE                         AUTH                         PUBLISH STEPS (each = one script)
 ──────                         ────                         ────────────────────────────────
 docs/articles/<x>.md  ──┐   life-manager-daily-driver       ┌─► note-stage1-render.py  render md → tables/figs → PNG
   (markdown + images)   │   extract-note-cookies.py         │      note-stage2-publish.py  upload imgs + create DRAFT
                         │     → $WRITER_STATE_DIR/note-work/ │   set-eyecatch-republish.py  cover image (見出し画像)
                         └────► note-cookies.json  ───────────┤   insert-toc-save.py        manual 目次 = big titles only
                                (mock-keychain decrypt)       │   delete-toc-node.py        kill any auto-<table-of-contents>
                                                              │   publish.py               無料 + membership 特典 + 試し読み
                                                              │                              line before paid section → 投稿/更新
                                                              │   publish-membership.py / toggle-plan.py  plan 公開 ON
                                                              └─► verify-public.py + shot-gate.py  VISITOR view + API truth
```

## Verify = ground truth (NEVER trust the owner/editor view)
- `GET https://note.com/api/v3/notes/<key>` → `price`(=0), `is_limited`(=true), `can_read`(=false), `eyecatch`(set).
- A NO-cookie `launch_context` screenshot of the gate (free preview + ¥500「参加手続きへ」).

## Canonical files
| file | role |
|---|---|
| `../../../../skills/browser/owned-persistent-context` | registry-owned daily-driver process |
| `extract-note-cookies.py` | decrypt note.com cookies from the daily-driver profile (run with /opt/homebrew/bin/python3) |
| `../note-stage1-render.py`,`../note-stage2-publish.py` | md → draft (tables/figs as uniform PNGs, hero, compact figure HTML) |
| `set-eyecatch-republish.py` | set the 見出し画像 (top `画像を追加` button → upload → crop 保存) |
| `insert-toc-save.py` + `delete-toc-node.py` | manual 目次 (big titles) + remove note's auto-目次 atom node |
| `publish.py` / `republish-only.py` | 無料 + メンバー全員に公開 + 試し読みエリア line + 投稿する/更新する |
| `publish-membership.py`,`toggle-plan.py` | publish the ¥500 plan (公開 toggle) |
| `verify-public.py`,`shot-gate.py` | visitor-view + API verification |

## Hard rules baked in (see SKILL.md "NOTE PUBLISH — HARD LESSONS")
1. eyecatch ≠ body image.  2. never keyboard-delete body content (use guarded node ops).
3. verify as a logged-out visitor + API.  4. 一時保存 ≠ live; re-publish = 更新する.
5. plan must be 公開 ON or gate says 現在販売されていません.  6. pure membership = price 0 + 特典 + 試し読み.
7. manual 目次 of big titles only (note auto-目次 can't filter levels).  8. all files in real dirs, never /tmp.

## TODO to make it ONE command
`publish-to-note.sh <markdown> --price 500 --paywall-before "<heading>" --toc-from h2` — orchestrate the
steps above + the guards + the verify gate, idempotent (skip already-done). Then "post this to note" = instant.
