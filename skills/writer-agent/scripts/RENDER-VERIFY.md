# render verification

The repository-owned verifier checks a staged draft without mutating source:

```bash
bash render-verify-draft.sh --platform <note|zenn|substack|devto> --url <draft edit URL> --lang <ja|en>
# -> {"verdict":"PASS"|"FAIL","problems":[...blocking...],"advisory":[...never blocks...]}, exit 0/1

```

`render-verify-draft.sh` takes a real full-page screenshot of the draft editor over CDP and has a
fresh `claude -p` vision judge check it against a small blocking/advisory checklist (frontmatter
leaking into the body, unrendered images/mermaid, heading breaks, note eyecatch, paywall marker
position) -- it never caches a verdict. Source repair is owned by the registered
`article-repair-candidate` loop rather than an unregistered detached fixer.

## Verification (real output, 2026-07-17)

- `render-verify-draft.sh` against today's real note draft (`n5787e092451f`):
  `{"verdict":"PASS","problems":[],"advisory":[]}` — full-page screenshot was genuinely full-page
  (1905x8646px, all the way to the references section), own-eyes-checked.
- `render-verify-draft.sh` against a synthetic HTML page reproducing a frontmatter leak + unrendered
  mermaid fence: `{"verdict":"FAIL","problems":["rule: P1, ...","rule: P2, ..."],"advisory":["rule: A1, ..."]}`
  — both injected defects caught with real quotes from the screenshot, plus one correctly-scoped
  advisory-only observation.
