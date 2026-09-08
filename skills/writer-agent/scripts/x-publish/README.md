# publish-to-x (cloakbrowser/CDP, optimized for our daily-driver — NO Playwright MCP, NO API credits)
Posts a Markdown article to X Articles via the REAL daily-driver browser (CDP :9222). The parser,
browser clipboard and table/image preparation code are owned by this repository.
Flow (each a script here, driven by CDP):
 1. prep-x-md.py     : source md → X md (tables→PNG via table_to_image, mermaid→PNG via kroki, H1 title + cover)
 2. parse_markdown.py: → title, cover, content_images[block_index], html, dividers
 3. copy_to_clipboard.py html --file body.html  (NSPasteboardTypeHTML = rich)
 4. x_core.py        : ROBUST open editor (poll for title textarea, re-click Write) → type title → click
                       [data-testid=composer] → Meta+v (rich body paste). KEY FIX = polling (was flaky).
 5. x_cover2.py      : cover = set_input_files on input[type=file] → Edit-media dialog → Apply
 6. x_images.py      : insert content images REVERSE block_index — copy_to_clipboard image → select the bi-th
                       [data-block=true] (cursor to end) → Meta+v → wait upload. 124 editor blocks ≈ 117 md.
 7. x_verify.py      : measure every body img px (X col = 501px wide) → FAIL if any >900px. Browser eyes-on.
VERIFIED 2026-06-25: draft edit/2070054710833545216 — title+cover+rich body+18 inline imgs, all ≤668px, clean.
Stage 1 = PUBLIC free article (no funnel). Stage 2 (≥2000 followers + 5M imp) = Subscriber-only paid Article.

## delete-drafts.py (cleanup — VERIFIED 2026-06-25)
Deletes ALL X Article drafts (the empties our editor-debug runs create). Mechanic: More → 'Delete Article'
→ confirm 'Yes, delete' (the confirm button is "Yes, delete", NOT "Delete" — that was the earlier bug).
Loops + self-verifies (re-count after each). VERIFIED: Drafts tab → "Your drafts live here" (empty) by eye.
