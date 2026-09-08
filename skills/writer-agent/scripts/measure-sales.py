#!/usr/bin/env python3
"""measure-sales.py — measures REAL, currently-displayed sales/revenue figures for note and
Substack by driving the CloakBrowser daily-driver over CDP. Same convention as
scripts/_shared/measure-funnel.py's measure_x: playwright.sync_api run via the
Life Manager managed Python as a subprocess, connect_over_cdp to the
ALREADY-LOGGED-IN daily-driver profile. A fresh new tab is opened for the measurement and ALWAYS
closed afterward -- the shared daily-driver's other existing tabs are never touched, same
discipline as render-verify-draft.sh ("never leave a stray tab on the shared daily-driver").
NEVER estimate or fabricate a number: if a metric cannot be safely parsed from the real screen
text today, emit null + a reason string, exactly like measure-funnel.py does. DOM reading uses
document.body.innerText + regex on labels, never brittle CSS selectors and never an LLM judge.

Per-platform measurement (MEASURED 2026-07-18 against the real logged-in dashboards):
  - note:      note.com/sitesettings/salesmanage ("売上管理", redirects to
               note.com/dashboard/salesmanage once logged in) requires a one-time password
               re-confirmation step (note's own step-up auth for money pages) -- filled from the
               NOTE_PASSWORD env var only if a password field actually appears on the page,
               skipped otherwise (session already confirmed earlier). The "今月の売上" card's
               "総額" line = sales_revenue (¥, this calendar month).
               note.com/sitesettings/purchasers ("販売履歴", redirects to note.com/dashboard/sales)
               shows the literal sentence "<year>年<month>月は購入者がいません" when there were
               zero purchases this month -- parsed as sales_count=0. A populated (non-zero) month
               has not been observed yet in this account, so that layout is reported as
               null+reason rather than guessed at (do not invent a row-counting rule for a layout
               never seen).
               note.com/sitesettings/stats loads the authenticated, paginated
               /api/v1/stats/pv response. Article views join by exact note key and owner;
               absence means zero only after the terminal page, after publication, and when
               every returned row proves that zero-view articles are omitted.
  - substack:  {pub}/publish/home 's "有料登録者" overview card and
               {pub}/publish/stats/earnings 's "累計収益" label. Both currently render as a literal
               "-" rather than an explicit number. Nearby text ("0から" / no processed Stripe
               payments) is retained in the failure reason, but is not converted into zero: a dash
               is not a measured number, so these metrics remain null + reason until the dashboard
               displays an explicit numeric value.

SCOPE (added 2026-07-31): every emitted row carries `scope` — "artifact" (this number is one
article's) or "account" (publication-wide). Both note dashboard totals and all three Substack
figures are structurally account-wide, so they are written as scope="account" with a reason and
NO artifact key. An account number is never divided or guessed onto an article. The rules and
the `artifact_id` encoding live in scripts/artifact_attribution.py; join with
scripts/attribution-join.py.

CLI:
    measure-sales.py [--out <path>] [--cdp-port <port>]
stdout: one JSON line summary {"date": "...", "measured": N, "results": [...]}
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Every emitted row declares whether its number is per-article or account-wide. The scope rules
# and the artifact_id encoding live in one place: scripts/artifact_attribution.py.
_ATTRIBUTION_SPEC = importlib.util.spec_from_file_location(
    "article_artifact_attribution",
    Path(__file__).resolve().parent / "artifact_attribution.py",
)
attribution = importlib.util.module_from_spec(_ATTRIBUTION_SPEC)
assert _ATTRIBUTION_SPEC.loader is not None
_ATTRIBUTION_SPEC.loader.exec_module(attribution)

DEFAULT_STATE_DIR = Path(
    os.environ.get("ARTICLE_STATE_DIR", os.environ.get("WRITER_STATE_DIR", Path(__file__).resolve().parents[1] / "state"))
)
DEFAULT_OUT = str(DEFAULT_STATE_DIR / "sales-ledger.jsonl")
VENV_CLOAK_PYTHON = os.environ.get("WRITER_BROWSER_PYTHON", sys.executable)

NOTE_SALES_URL = "https://note.com/sitesettings/salesmanage"
NOTE_PURCHASES_URL = "https://note.com/sitesettings/purchasers"
NOTE_STATS_URL = "https://note.com/sitesettings/stats"
JST = timezone(timedelta(hours=9))


def run_browser_script(script: str, timeout: int = 90) -> subprocess.CompletedProcess:
    if not os.path.exists(VENV_CLOAK_PYTHON):
        raise FileNotFoundError(f"Writer browser Python not found at {VENV_CLOAK_PYTHON}")
    return subprocess.run([VENV_CLOAK_PYTHON, "-c", script], capture_output=True, text=True, timeout=timeout)


def extract_payload(proc: subprocess.CompletedProcess) -> dict:
    """Shared subprocess-result handling for both platform drivers below -- same pattern as
    measure-funnel.py's measure_x: look for a recognizable prefixed line, else build an honest
    reason from whatever stdout/stderr actually said."""
    line = next((l for l in proc.stdout.splitlines() if l.startswith("PAYLOAD_B64:")), None)
    if proc.returncode == 0 and line:
        return {"payload": json.loads(base64.b64decode(line[len("PAYLOAD_B64:"):]).decode("utf-8"))}
    if "CDP_UNREACHABLE:" in proc.stdout:
        reason = next(l for l in proc.stdout.splitlines() if l.startswith("CDP_UNREACHABLE:"))
        return {"error": f"browser DOM read failed: {reason}"}
    if "NOTE_PASSWORD_MISSING" in proc.stdout:
        return {"error": "NOTE_PASSWORD missing (configure LIFE_MANAGER_ENV_FILE first)"}
    reason = proc.stdout.strip() or proc.stderr.strip()[-300:] or "no PAYLOAD_B64 line and no recognizable error prefix"
    return {"error": f"browser DOM read failed: {reason}"}


def measure_note_pages(cdp_port: int) -> dict:
    """Drives ONE new tab through both note.com dashboard pages that this run needs, filling the
    password re-confirmation step only if the browser actually shows it (idempotent: harmless to
    check before each page). Returns {"error": ...} OR the two pages' final URL + raw
    document.body.innerText, for the caller to regex-parse (parsing happens in the parent, same
    division of labor as measure-funnel.py's measure_x: subprocess fetches, parent parses)."""
    script = f"""
import base64, json, os, sys, time
from playwright.sync_api import sync_playwright

p = sync_playwright().start()
try:
    b = p.chromium.connect_over_cdp("http://localhost:{cdp_port}")
except Exception as e:
    print("CDP_UNREACHABLE:" + str(e)); sys.exit(1)
ctx = b.contexts[0]
pg = ctx.new_page()

def confirm_password_if_shown():
    if pg.locator('input[type="password"]').count() == 0:
        return True
    pw = os.environ.get("NOTE_PASSWORD", "")
    if not pw:
        print("NOTE_PASSWORD_MISSING")
        return False
    pg.fill('input[type="password"]', pw)
    pg.evaluate('''() => {{
        const btn = [...document.querySelectorAll('button')].find(
            b => (b.innerText || '').trim() === '確認して続ける');
        if (btn) btn.click();
    }}''')
    time.sleep(3)
    return True

try:
    pg.goto({NOTE_SALES_URL!r}, wait_until="domcontentloaded", timeout=40000)
    time.sleep(3)
    if not confirm_password_if_shown():
        sys.exit(1)
    sales_url = pg.url
    sales_body = pg.evaluate("() => document.body.innerText")

    pg.goto({NOTE_PURCHASES_URL!r}, wait_until="domcontentloaded", timeout=40000)
    time.sleep(3)
    if not confirm_password_if_shown():
        sys.exit(1)
    purchases_url = pg.url
    purchases_body = pg.evaluate("() => document.body.innerText")

    pg.goto({NOTE_STATS_URL!r}, wait_until="domcontentloaded", timeout=40000)
    time.sleep(3)
    stats_pages = pg.evaluate('''async () => {{
        const pages = [];
        for (let page = 1; page <= 100; page++) {{
            const response = await fetch(
                `/api/v1/stats/pv?filter=all&page=${{page}}&sort=pv`,
                {{credentials: "same-origin", cache: "no-store"}}
            );
            if (!response.ok) throw new Error(`note stats HTTP ${{response.status}}`);
            const envelope = await response.json();
            const data = envelope && envelope.data;
            if (!data || !Array.isArray(data.note_stats))
                throw new Error("note stats response is malformed");
            pages.push(data);
            if (data.last_page === true) return pages;
        }}
        throw new Error("note stats pagination exceeded 100 pages");
    }}''')

    payload = {{
        "sales_url": sales_url, "sales_body": sales_body,
        "purchases_url": purchases_url, "purchases_body": purchases_body,
        "stats_url": pg.url, "stats_pages": stats_pages,
    }}
    print("PAYLOAD_B64:" + base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii"))
finally:
    pg.close()
"""
    try:
        proc = run_browser_script(script)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {"error": f"note browser driver failed to run: {e}"}
    return extract_payload(proc)


def measure_substack_pages(cdp_port: int) -> dict:
    """Same shape as measure_note_pages: one new tab, two page loads (overview home + earnings
    stats), raw body text handed back to the parent for regex parsing."""
    pub = os.environ.get("SUBSTACK_PUBLICATION", "aniccabuddha.substack.com")
    home_url = f"https://{pub}/publish/home"
    earnings_url = f"https://{pub}/publish/stats/earnings"
    script = f"""
import base64, json, sys, time
from playwright.sync_api import sync_playwright

p = sync_playwright().start()
try:
    b = p.chromium.connect_over_cdp("http://localhost:{cdp_port}")
except Exception as e:
    print("CDP_UNREACHABLE:" + str(e)); sys.exit(1)
ctx = b.contexts[0]
pg = ctx.new_page()
try:
    pg.goto({home_url!r}, wait_until="domcontentloaded", timeout=40000)
    time.sleep(4)
    home_url_final = pg.url
    home_body = pg.evaluate("() => document.body.innerText")

    pg.goto({earnings_url!r}, wait_until="domcontentloaded", timeout=40000)
    time.sleep(4)
    earnings_url_final = pg.url
    earnings_body = pg.evaluate("() => document.body.innerText")

    payload = {{
        "home_url": home_url_final, "home_body": home_body,
        "earnings_url": earnings_url_final, "earnings_body": earnings_body,
    }}
    print("PAYLOAD_B64:" + base64.b64encode(json.dumps(payload).encode("utf-8")).decode("ascii"))
finally:
    pg.close()
"""
    try:
        proc = run_browser_script(script)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {"error": f"substack browser driver failed to run: {e}"}
    return extract_payload(proc)


def parse_note_metrics(payload: dict) -> list[tuple[str, object, bool, str | None, str]]:
    """Returns a list of (metric, value, ok, reason, source_url) tuples. Regex-only, no LLM
    judgment (per task requirement): each label is anchored on its own literal Japanese heading,
    non-greedy up to the first matching amount, so the two "総額" occurrences on the sales page
    (this month's total vs the unpaid-transfer total) never get confused with each other."""
    sales_body = payload["sales_body"]
    purchases_body = payload["purchases_body"]
    out = []

    m = re.search(r"今月の売上.*?総額\s*\n?\s*¥\s*([\d,]+)", sales_body, re.S)
    if m:
        out.append(("sales_revenue", int(m.group(1).replace(",", "")), True, None, payload["sales_url"]))
    else:
        out.append((
            "sales_revenue", None, False,
            "label '今月の売上 ... 総額 ¥N' not found in note.com dashboard/salesmanage body text",
            payload["sales_url"],
        ))

    m2 = re.search(r"\d{4}年\d{1,2}月は購入者がいません", purchases_body)
    if m2:
        out.append(("sales_count", 0, True, None, payload["purchases_url"]))
    else:
        out.append((
            "sales_count", None, False,
            "no verified zero-purchase phrase ('<year>年<month>月は購入者がいません') found on "
            "note.com dashboard/sales -- a populated (non-zero) month's layout has not been "
            "observed yet in this account, so no safe count-extraction rule exists for it (do "
            "not guess a row-counting rule for an unseen layout)",
            payload["purchases_url"],
        ))
    return out


def _note_stats_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("note stats calculation time is missing")
    parsed = datetime.strptime(value, "%Y/%m/%d %H:%M").replace(tzinfo=JST)
    return parsed.astimezone(timezone.utc)


def _iso_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("artifact publication time is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("artifact publication time has no timezone")
    return parsed.astimezone(timezone.utc)


def parse_note_view_rows(
    pages: list[dict], artifacts: list[dict]
) -> list[dict]:
    """Turn a fully paginated authenticated note stats receipt into article views.

    note omits zero-view articles from this endpoint.  Absence is accepted as
    zero only after the terminal page, when every returned row has a positive
    read_count and the dashboard calculation happened after publication.
    """

    if not pages or pages[-1].get("last_page") is not True:
        raise ValueError("note stats pagination is incomplete")
    calculated_values = {page.get("last_calculate_at") for page in pages}
    ranges = {(page.get("start_date"), page.get("end_date")) for page in pages}
    if len(calculated_values) != 1 or len(ranges) != 1:
        raise ValueError("note stats pages do not share one frozen calculation")
    calculated_raw = next(iter(calculated_values))
    calculated = _note_stats_time(calculated_raw)
    observed_at = calculated.isoformat().replace("+00:00", "Z")
    stats: dict[str, dict] = {}
    for page in pages:
        values = page.get("note_stats")
        if not isinstance(values, list):
            raise ValueError("note stats page has no article collection")
        for item in values:
            if not isinstance(item, dict):
                raise ValueError("note stats article is malformed")
            key = item.get("key")
            read_count = item.get("read_count")
            if (
                not isinstance(key, str)
                or not key
                or not isinstance(read_count, int)
                or isinstance(read_count, bool)
                or read_count < 0
                or key in stats
            ):
                raise ValueError("note stats article identity/count is invalid")
            stats[key] = item
    omission_means_zero = bool(stats) and all(
        int(item["read_count"]) > 0 for item in stats.values()
    )
    pages_sha256 = hashlib.sha256(
        json.dumps(
            pages, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    rows = []
    for artifact in artifacts:
        if artifact.get("platform") != "note":
            continue
        artifact_id = artifact.get("artifact_id")
        key = artifact.get("public_id")
        live_url = artifact.get("live_url")
        if not all(isinstance(value, str) and value for value in (artifact_id, key, live_url)):
            raise ValueError("note artifact identity is incomplete")
        published = _iso_time(artifact.get("published_at"))
        item = stats.get(key)
        value: int | None = None
        reason = None
        ok = False
        if calculated < published:
            reason = "note stats calculation predates publication"
        elif item is not None:
            owner = item.get("user", {}).get("urlname") if isinstance(item.get("user"), dict) else None
            if not isinstance(owner, str) or f"note.com/{owner}/n/{key}" not in live_url:
                reason = "note stats owner/key differs from the live artifact"
            else:
                value = int(item["read_count"])
                ok = True
        elif omission_means_zero:
            value = 0
            ok = True
        else:
            reason = "complete note stats response does not prove absent article means zero"
        receipt = {
            "schema_version": 1,
            "artifact_id": artifact_id,
            "public_id": key,
            "published_at": artifact.get("published_at"),
            "observed_at": observed_at,
            "pages_sha256": pages_sha256,
            "read_count": value,
            "status": "scorable" if ok else "unknown",
        }
        row = {
            "ts": observed_at,
            "platform": "note",
            "metric": "views",
            "value": value,
            "source_url": NOTE_STATS_URL,
            "ok": ok,
            "status": "scorable" if ok else "unknown",
            "unit": "count",
            "window": "all_since_publication",
            "scope": "artifact",
            "artifact_id": artifact_id,
            "receipt_sha256": hashlib.sha256(
                json.dumps(
                    receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest(),
        }
        if reason:
            row["reason"] = reason
        rows.append(row)
    return rows


def parse_substack_metrics(payload: dict) -> list[tuple[str, object, bool, str | None, str]]:
    home_body = payload["home_body"]
    earnings_body = payload["earnings_body"]
    out = []

    m = re.search(r"有料登録者\s*\n\s*(-|[\d,]+)\s*\n\s*([\d,]+)から", home_body)
    if not m:
        out.append((
            "paid_subscribers", None, False,
            "'有料登録者' overview card not found in publish/home body text",
            payload["home_url"],
        ))
    else:
        raw, baseline = m.group(1), m.group(2).replace(",", "")
        if raw != "-":
            out.append(("paid_subscribers", int(raw.replace(",", "")), True, None, payload["home_url"]))
        else:
            out.append((
                "paid_subscribers", None, False,
                f"dashboard displays '-' instead of an explicit subscriber count (nearby baseline: "
                f"'{baseline}から'); a dash is not converted into a guessed zero",
                payload["home_url"],
            ))

    mrr = re.search(
        r"(?:月間経常収益|Monthly recurring revenue|MRR)"
        r"\s*\n\s*([^\n]+)",
        earnings_body,
        re.I,
    )
    if not mrr:
        out.append((
            "mrr", None, False,
            "monthly recurring revenue label not found in Substack earnings body text",
            payload["earnings_url"],
        ))
    else:
        raw_mrr = mrr.group(1).strip()
        cleaned_mrr = re.sub(r"[^\d.]", "", raw_mrr)
        if raw_mrr != "-" and cleaned_mrr:
            out.append((
                "mrr",
                float(cleaned_mrr),
                True,
                None,
                payload["earnings_url"],
            ))
        else:
            out.append((
                "mrr", None, False,
                f"dashboard does not display an explicit numeric MRR ({raw_mrr!r}); unknown is not zero",
                payload["earnings_url"],
            ))

    m2 = re.search(r"累計収益\s*\n\s*([^\n]+)\n\s*([^\n]*)", earnings_body)
    if not m2:
        out.append((
            "revenue", None, False,
            "'累計収益' label not found in publish/stats/earnings body text",
            payload["earnings_url"],
        ))
    else:
        raw, note = m2.group(1).strip(), m2.group(2).strip()
        if raw != "-":
            cleaned = re.sub(r"[^\d.]", "", raw)
            if cleaned:
                out.append(("revenue", float(cleaned), True, None, payload["earnings_url"]))
            else:
                out.append((
                    "revenue", None, False,
                    f"could not parse a numeric revenue amount out of {raw!r}",
                    payload["earnings_url"],
                ))
        else:
            context = "no processed Stripe payments message is present" if "支払いが処理されると" in note else f"nearby text: {note!r}"
            out.append((
                "revenue", None, False,
                f"dashboard displays '-' instead of an explicit revenue amount ({context}); a "
                "dash is not converted into a guessed zero",
                payload["earnings_url"],
            ))
    return out


def build_rows(
    today: str,
    platform: str,
    metrics: list,
    fallback_url: str,
    artifact_index: dict | None = None,
) -> list[dict]:
    metadata = {
        ("note", "sales_revenue"): ("JPY", "current_calendar_month"),
        ("note", "sales_count"): ("purchases", "current_calendar_month"),
        ("substack", "paid_subscribers"): ("subscribers", "current"),
        ("substack", "mrr"): ("USD", "current_monthly_recurring"),
        ("substack", "revenue"): ("USD", "cumulative"),
    }
    rows = []
    for metric, value, ok, reason, source_url in metrics:
        unit, window = metadata.get(
            (platform, metric), ("unknown", "unknown")
        )
        row = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "platform": platform,
            "metric": metric,
            "value": value,
            "source_url": source_url or fallback_url,
            "ok": ok,
            "status": "scorable" if ok else "unknown",
            "unit": unit,
            "window": window,
        }
        if not ok:
            row["reason"] = reason
        # scope is decided by the (platform, metric) contract, never by the number itself:
        # an account-wide total can never become one article's revenue.
        rows.append(
            attribution.classify_sales_row(
                row, artifact_index or {"artifacts": {}, "by_url": {}}
            )
        )
    return rows


def load_note_artifacts(path: Path) -> list[dict]:
    artifacts = []
    if not path.is_file():
        return artifacts
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        language = row.get("lang") or row.get("language")
        if not (
            row.get("platform") == "note"
            and row.get("state") == "live"
            and row.get("published") is True
            and row.get("reality_gate") == "PASS"
            and row.get("verified") is True
            and isinstance(row.get("run_id"), str)
            and isinstance(language, str)
        ):
            continue
        artifacts.append({
            **row,
            "lang": language,
            "artifact_id": f"{row['run_id']}__note__{language}",
        })
    return artifacts


def unknown_note_view_rows(artifacts: list[dict], reason: str) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return [
        {
            "ts": now,
            "platform": "note",
            "metric": "views",
            "value": None,
            "source_url": NOTE_STATS_URL,
            "ok": False,
            "status": "unknown",
            "unit": "count",
            "window": "all_since_publication",
            "scope": "artifact",
            "artifact_id": artifact["artifact_id"],
            "reason": reason,
        }
        for artifact in artifacts
    ]


def measure_platform(platform: str, metric_sources: list[tuple[str, str]], fetch_fn, parse_fn, cdp_port: int, today: str) -> list[dict]:
    try:
        result = fetch_fn(cdp_port)
        if "error" in result:
            reason = result["error"]
            metrics = [(name, None, False, reason, source_url) for name, source_url in metric_sources]
        else:
            metrics = parse_fn(result["payload"])
    except Exception as e:
        # One honest failure row per expected metric. A single platform's unexpected page or
        # parser change must not prevent the other platform from being measured and recorded.
        reason = f"unexpected {platform} measurement failure: {type(e).__name__}: {e}"
        metrics = [(name, None, False, reason, source_url) for name, source_url in metric_sources]
    return build_rows(today, platform, metrics, metric_sources[0][1])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--out", default=DEFAULT_OUT)
    p.add_argument("--cdp-port", type=int, default=int(os.environ.get("CDP_PORT", 9222)))
    p.add_argument(
        "--articles",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "state/articles.jsonl",
    )
    args = p.parse_args(argv)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    note_artifacts = load_note_artifacts(args.articles)
    try:
        note_result = measure_note_pages(args.cdp_port)
        if "error" in note_result:
            raise ValueError(str(note_result["error"]))
        note_payload = note_result["payload"]
        rows += build_rows(
            today,
            "note",
            parse_note_metrics(note_payload),
            NOTE_SALES_URL,
        )
        rows += parse_note_view_rows(note_payload["stats_pages"], note_artifacts)
    except Exception as error:
        reason = f"note dashboard measurement unavailable: {type(error).__name__}: {error}"
        rows += build_rows(
            today,
            "note",
            [
                ("sales_revenue", None, False, reason, NOTE_SALES_URL),
                ("sales_count", None, False, reason, NOTE_PURCHASES_URL),
            ],
            NOTE_SALES_URL,
        )
        rows += unknown_note_view_rows(note_artifacts, reason)
    substack_pub = os.environ.get("SUBSTACK_PUBLICATION", "aniccabuddha.substack.com")
    rows += measure_platform(
        "substack",
        [
            ("paid_subscribers", f"https://{substack_pub}/publish/home"),
            ("mrr", f"https://{substack_pub}/publish/stats/earnings"),
            ("revenue", f"https://{substack_pub}/publish/stats/earnings"),
        ],
        measure_substack_pages, parse_substack_metrics, args.cdp_port, today,
    )

    with open(out_path, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    print(json.dumps({"date": today, "measured": len(rows), "results": rows}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
