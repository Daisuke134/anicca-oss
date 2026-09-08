"""VISION/PREVIEW gate for Substack — the verification loop, automated. Connects to the REAL daily-driver browser
(CloakBrowser, CDP on :9222, the human's live logged-in session), opens the post's actual Substack PREVIEW in
DESKTOP mode, measures EVERY image's rendered px in the preview DOM (Substack stretches all body images to the
728px column), and FAILS if any image is taller than the budget (fills the page). Also saves section screenshots
so the agent can Read them. This is exactly the manual check we did, made repeatable for ANY article.
Usage: verify-preview.py <post_id> [max_on_screen_h=950]   → exit 1 (FAIL) if any image too tall.
Needs the daily-driver running with remote-debugging-port=9222 (it is, kept alive by dd-keepalive.py)."""
import os, sys, time
from playwright.sync_api import sync_playwright
from playwright._impl._errors import TargetClosedError

PUB = os.environ.get("SUBSTACK_PUBLICATION", "").strip().rstrip("/")
if not PUB:
    raise SystemExit("SUBSTACK_PUBLICATION is required")
W = os.path.expanduser("~/.cloak/note-work")
CDP = "http://localhost:9222"
MAX_TARGET_CLOSED_ATTEMPTS = 3


def verify_once(post_id: str, max_h: int) -> int:
    """Measure one fresh preview page.

    TargetClosedError is deliberately allowed to escape: it means the shared
    daily-driver closed this page/context, not that the article failed the
    image-height gate. main() owns the bounded fresh-page retry.
    """
    playwright = sync_playwright().start()
    page = None
    context = None
    try:
        browser = playwright.chromium.connect_over_cdp(CDP)
        context = browser.new_context()
        cookie_header = os.environ.get("SUBSTACK_SESSION_COOKIE", "")
        cookies = []
        for item in cookie_header.split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name:
                cookies.append({
                    "name": name,
                    "value": value,
                    "domain": ".substack.com",
                    "path": "/",
                })
        if not cookies:
            raise RuntimeError("SUBSTACK_SESSION_COOKIE is required")
        context.add_cookies(cookies)
        page = context.new_page()
        page.goto(
            f"https://{PUB}/publish/post/{post_id}",
            wait_until="domcontentloaded",
            timeout=60000,
        )
        time.sleep(11)
        body_text = page.evaluate("()=>document.body.innerText")
        if "Automaton" not in body_text and len(body_text) < 200:
            print("WARN: editor may not have loaded")
        # Open the real Substack preview and switch to the 728px desktop reader column.
        page.evaluate("""()=>{const b=[...document.querySelectorAll('button,a,span,div')].find(x=>['プレビュー','Preview'].includes((x.textContent||'').trim()));if(b)b.click();}""")
        time.sleep(6)
        page.evaluate("""()=>{const b=[...document.querySelectorAll('button,span,div,a')].find(x=>['デスクトップ','Desktop'].includes((x.textContent||'').trim()));if(b)b.click();}""")
        time.sleep(4)
        sizes = page.evaluate("""()=>{const r=[];document.querySelectorAll('img').forEach(im=>{const b=im.getBoundingClientRect();if(b.height>60)r.push([Math.round(b.width),Math.round(b.height)]);});return r;}""")
        tall = [(width, height) for (width, height) in sizes if height > max_h]
        print(f"== VERIFY-PREVIEW post {post_id} (max_on_screen_h={max_h}) ==")
        print(
            f"images measured in the real preview: {len(sizes)} | "
            f"tallest: {max((height for _, height in sizes), default=0)}px"
        )
        page.set_viewport_size({"width": 1000, "height": 1500})
        page.screenshot(path=f"{W}/preview-{post_id}.png", full_page=True)
        print(f"screenshot: {W}/preview-{post_id}.png (AGENT: Read it)")
        if tall:
            print(
                f"  ✗ FAIL: {len(tall)} image(s) exceed {max_h}px "
                f"on screen: {tall}"
            )
            print(
                "  → re-render those assets shorter (render-tables-autofit / "
                "stitch fund / pad figs) and rebuild the draft."
            )
            return 1
        print("  ✓ PASS: every image fits (no full-page image).")
        return 0
    finally:
        if page is not None and not page.is_closed():
            page.close()
        if context is not None:
            context.close()
        playwright.stop()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(
            "usage: verify-preview.py <post_id> [max_on_screen_h=950]",
            file=sys.stderr,
        )
        return 2
    post_id = args[0]
    max_h = int(args[1]) if len(args) > 1 else 950
    for attempt in range(1, MAX_TARGET_CLOSED_ATTEMPTS + 1):
        try:
            return verify_once(post_id, max_h)
        except TargetClosedError as error:
            print(
                f"TRANSIENT: preview page closed "
                f"(attempt {attempt}/{MAX_TARGET_CLOSED_ATTEMPTS}): {error}",
                file=sys.stderr,
            )
            if attempt == MAX_TARGET_CLOSED_ATTEMPTS:
                return 75
            time.sleep(2)
    return 75


if __name__ == "__main__":
    sys.exit(main())
