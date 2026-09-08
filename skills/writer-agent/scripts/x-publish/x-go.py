"""Publish an existing X Article draft to PUBLIC (free).

Open the draft URL on the daily-driver (CDP :9222) → click Publish → confirm
in the dialog (audience defaults to Everyone = public, free) → dismiss the
post-publish "Try Boosting" promo (boosting costs money — never).

Exit 0 only when X ITSELF proves the article went live: the draft id's status
page must stop rendering X's not-found empty state. A clicked button is not
evidence — on 2026-07-25 the confirm click silently missed (exact-text match
on "Publish" inside [role=dialog]) while the run still reported a
post-publish signal, and the article stayed a draft. argv[1] = draft URL.

GATED upstream by publish-to-x.sh (sentinel + X_MODE=go); this script just
executes the publish on a verified draft.
"""
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright

ACCOUNT = os.environ.get("X_ACCOUNT_HANDLE", "").strip().lstrip("@")
if not ACCOUNT:
    raise SystemExit("X_ACCOUNT_HANDLE is required")

# X renames this control between locales and releases; match the family, not
# one exact string.
PUBLISH_PATTERN = r"^(publish|publish now|publish article|update|公開する|公開|更新)$"
DISMISS_PATTERN = r"(maybe later|not now|あとで|後で|dismiss)"
# The editor URL renders the Articles > Drafts LIST, not the editor itself
# (measured live 2026-07-26): the draft row must be opened first, otherwise
# no publish control exists on the page and the publish silently no-ops.
OPEN_DRAFT_JS = """
(title) => {
  const needle = (title || '').slice(0, 12);
  if (needle) {
    // The row's own link text is only "Draft · Last saved"; the title sits in
    // a sibling node, so walk up from the title to the row's link.
    const holders = [...document.querySelectorAll('span,div')].filter((node) => {
      const text = node.innerText || '';
      return text.includes(needle) && text.length < 600;
    });
    for (const holder of holders) {
      let node = holder;
      for (let depth = 0; depth < 8 && node; depth += 1) {
        const link = node.tagName === 'A' ? node : node.querySelector('a');
        if (link) { link.click(); return (holder.innerText || '').trim().slice(0, 80); }
        node = node.parentElement;
      }
    }
  }
  const anchors = [...document.querySelectorAll('a')].filter((node) => {
    const text = (node.innerText || '').trim();
    return text && text.length < 400 && /Last saved|最終保存|Draft/i.test(text);
  });
  if (!anchors.length) return null;
  const chosen = anchors[anchors.length - 1];
  chosen.click();
  return (chosen.innerText || '').trim().slice(0, 80);
}
"""

MISSING_STATUS_MARKERS = (
    "this page doesn’t exist",
    "this page doesn't exist",
    "このページは存在しません",
)

CLICK_JS = """
(pattern) => {
  const re = new RegExp(pattern, 'i');
  const dialogs = [...document.querySelectorAll('[role=dialog],[aria-modal=true]')];
  const scopes = dialogs.length ? dialogs : [document];
  for (const scope of scopes) {
    const nodes = [...scope.querySelectorAll('button,div[role=button],a[role=button]')];
    const hit = nodes.reverse().find((node) => {
      const label = (node.innerText || node.textContent || '').trim();
      return re.test(label) && node.offsetParent !== null && !node.disabled;
    });
    if (hit) { hit.click(); return true; }
  }
  return false;
}
"""


def status_url(draft_url: str, account: str = ACCOUNT) -> str:
    """The draft id doubles as the public status id once published."""
    article_id = draft_url.rstrip("/").split("/")[-1]
    return f"https://x.com/{account}/status/{article_id}"


def page_is_missing(body: str) -> bool:
    """X's not-found empty state means the article is still a draft."""
    text = (body or "").casefold()
    return any(marker.casefold() in text for marker in MISSING_STATUS_MARKERS)


def main(url: str) -> int:
    playwright = sync_playwright().start()
    result = {"clicked_publish": False, "clicked_confirm": False, "live": False}
    try:
        browser = playwright.chromium.connect_over_cdp("http://localhost:9222")
        page = browser.contexts[0].new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=50_000)
            time.sleep(8)
            result["clicked_publish"] = bool(
                page.evaluate(CLICK_JS, PUBLISH_PATTERN)
            )
            if not result["clicked_publish"]:
                # We are on the drafts list: open the draft, then publish.
                result["opened_draft"] = page.evaluate(
                    OPEN_DRAFT_JS, os.environ.get("X_ARTICLE_TITLE", "")
                )
                time.sleep(8)
                result["clicked_publish"] = bool(
                    page.evaluate(CLICK_JS, PUBLISH_PATTERN)
                )
            time.sleep(4)
            result["clicked_confirm"] = bool(
                page.evaluate(CLICK_JS, PUBLISH_PATTERN)
            )
            result["editor_url"] = page.url
            time.sleep(10)
            page.evaluate(CLICK_JS, DISMISS_PATTERN)
            time.sleep(2)

            # Platform-proven verdict: the status page must now exist.
            # Opening a draft row navigates to that draft's own editor id;
            # prove the article we actually published, not the argv id.
            proof_url = status_url(result.get("editor_url") or url)
            page.goto(proof_url, wait_until="load", timeout=45_000)
            time.sleep(7)
            body = page.locator("body").inner_text(timeout=10_000)
            result["live"] = not page_is_missing(body)
            result["proof_url"] = proof_url
        finally:
            page.close()
    finally:
        playwright.stop()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["live"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
