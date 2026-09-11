#!/usr/bin/env python3
"""Set and persistence-verify Instagram profile fields through repo-owned raw CDP."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path


CDP = str(Path(__file__).resolve().parents[3] / "browser/scripts/cdp.py")
PYTHON = sys.executable
BIO = "document.querySelector('textarea')"
WEBSITE = "([...document.querySelectorAll('input')].find(e=>{const p=((e.placeholder||'')+' '+(e.getAttribute('aria-label')||'')+' '+(e.name||'')).toLowerCase();return (e.type||'').toLowerCase()==='url'||/website|url|ウェブサイト|sitio web|site web|网站|웹사이트|webseite/.test(p);})||null)"
SAVE = "((()=>{const words=/送信する|submit|save|guardar|enregistrer|提交|保存|저장|speichern/i;const a=[...document.querySelectorAll('[role=button],button')].filter(e=>words.test((e.textContent||'').trim()));return a.at(-1)||document.querySelector('button[type=submit]');})())"


def cdp(*args: str, source: str | None = None) -> str:
    result = subprocess.run(
        [PYTHON, CDP, *args], input=source, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"cdp rc={result.returncode}")
    return result.stdout.strip()


def evaluate(tid: str, expression: str):
    value = json.loads(cdp("eval", tid, "-", source=expression))
    for _ in range(2):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            break
    return value


def coordinates(tid: str, element: str) -> dict | None:
    return evaluate(tid, f"(()=>{{const e={element};if(!e)return null;e.scrollIntoView({{block:'center'}});const r=e.getBoundingClientRect();return{{x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)}};}})()")


def replace_field(tid: str, element: str, value: str) -> bool:
    selected = evaluate(tid, f"(()=>{{const e={element};if(!e)return false;e.focus();e.select();return true;}})()")
    if not selected:
        return False
    cdp("insert", tid, value)
    return True


def field_value(tid: str, element: str) -> str:
    value = evaluate(tid, f"(()=>{{const e={element};return e?(e.value||''):'';}})()")
    return value if isinstance(value, str) else ""


def normalize_url(value: str) -> str:
    value = value.strip().lower()
    for prefix in ("https://", "http://"):
        if value.startswith(prefix):
            value = value[len(prefix):]
    if value.startswith("www."):
        value = value[4:]
    return value.split("#", 1)[0].rstrip("/")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tid", required=True)
    parser.add_argument("--icon")
    parser.add_argument("--bio")
    parser.add_argument("--website")
    parser.add_argument("--username")
    args = parser.parse_args(argv)
    cdp("nav", args.tid, "https://www.instagram.com/accounts/edit/")
    time.sleep(5)

    if args.icon:
        cdp("setfile", args.tid, "input[type=file]", args.icon)
        time.sleep(3)
    if args.bio and not replace_field(args.tid, BIO, args.bio):
        raise RuntimeError("bio field unavailable")
    if args.website and not replace_field(args.tid, WEBSITE, args.website):
        raise RuntimeError("website field unavailable")

    if args.bio or args.website:
        button = coordinates(args.tid, SAVE)
        if not button:
            raise RuntimeError("profile save button unavailable")
        cdp("clickxy", args.tid, str(button["x"]), str(button["y"]))
        time.sleep(5)

    result = {"result": "setup_profile", "bio_set": None, "avatar_set": None, "website_set": None}
    if args.website:
        cdp("nav", args.tid, "https://www.instagram.com/accounts/edit/")
        time.sleep(5)
        persisted = normalize_url(field_value(args.tid, WEBSITE))
        expected = normalize_url(args.website)
        result["website_set"] = bool(persisted) and expected in persisted

    if args.username and (args.bio or args.icon):
        cdp("nav", args.tid, f"https://www.instagram.com/{args.username}/")
        time.sleep(4)
        check = evaluate(args.tid, "(()=>({bio:document.body.innerText.includes(%s),avatar:((document.querySelector('header img,main img')||{}).src||'').includes('cdninstagram')}))()" % json.dumps((args.bio or "")[:20]))
        if isinstance(check, dict):
            if args.bio:
                result["bio_set"] = bool(check.get("bio"))
            if args.icon:
                result["avatar_set"] = bool(check.get("avatar"))

    checks = [value for value in (
        result["bio_set"] if args.bio else None,
        result["avatar_set"] if args.icon else None,
        result["website_set"] if args.website else None,
    ) if value is not None]
    result["ok"] = bool(checks) and all(checks)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
