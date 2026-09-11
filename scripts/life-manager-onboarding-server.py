#!/usr/bin/env python3
"""Serve the local Life Manager onboarding and integration control surface."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import subprocess
import sys
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
HTML = REPO / "apps" / "oss-onboarding" / "index.html"
GRAPH = REPO / "scripts" / "integration-onboarding.py"
PROFILE_CLI = REPO / "scripts" / "life-manager-profile.py"
CHILDREN: dict[str, subprocess.Popen[bytes]] = {}


def _json_command(command: list[str], timeout: int = 30) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(
        command, cwd=REPO, capture_output=True, text=True, timeout=timeout, check=False,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    try:
        value = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError:
        value = {"status": "issue", "error": "invalid structured output"}
    return completed.returncode, value


def _graph() -> dict[str, Any]:
    code, graph = _json_command([sys.executable, str(GRAPH), "graph"])
    if code != 0:
        raise RuntimeError("integration graph unavailable")
    for integration in graph["integrations"]:
        integration_id = integration["integration_id"]
        child = CHILDREN.get(integration_id)
        if child is not None and child.poll() is None:
            integration["state"] = "waiting"
            continue
        preflight_code, preflight = _json_command(integration["preflight"]["command"])
        if preflight_code != 0 or preflight.get("status") != "ready":
            integration["state"] = "needs_you"
            integration["next_action"] = "Prepare this Mac"
            continue
        readiness_code, readiness = _json_command(integration["readiness"]["command"])
        status = readiness.get("status")
        if readiness_code == 0 and status == "ready":
            owners = integration["activation"]["owners"]
            loaded = 0
            if "macos-arm64" in integration["platforms"] and sys.platform == "darwin":
                domain = f"gui/{os.getuid()}"
                loaded = sum(subprocess.run(
                    ["/bin/launchctl", "print", f"{domain}/{owner}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                ).returncode == 0 for owner in owners)
            else:
                loaded = len(owners)
            integration["owner_readback"] = {"declared": len(owners), "loaded": loaded}
            if loaded == len(owners):
                integration["state"] = "ready"
                integration["next_action"] = None
            else:
                integration["state"] = "issue"
                integration["next_action"] = "Restore persistent loop owners"
        elif status in {"uninitialized", "needs_setup", "blocked"}:
            integration["state"] = "needs_you"
            integration["next_action"] = "Connect or resume official setup"
        else:
            integration["state"] = "issue"
            integration["next_action"] = "Open diagnostics"
        integration["readiness_state"] = status or "unknown"
        outcome_code, outcomes = _json_command(integration["outcome_status"]["command"])
        integration["outcomes"] = outcomes.get("receipts", []) if outcome_code == 0 else []
        integration["outcome_state"] = outcomes.get("status", "issue") if outcome_code == 0 else "issue"
    return graph


def _manifest(integration_id: str) -> dict[str, Any]:
    path = REPO / "integrations" / f"{integration_id}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("integration_id") != integration_id:
        raise ValueError("unknown integration")
    return value


def _connect(integration_id: str) -> dict[str, Any]:
    child = CHILDREN.get(integration_id)
    if child is not None and child.poll() is None:
        return {"status": "already_running", "integration_id": integration_id}
    manifest = _manifest(integration_id)
    child = subprocess.Popen(
        manifest["connect"], cwd=REPO, stdin=None,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    CHILDREN[integration_id] = child
    return {"status": "started", "integration_id": integration_id}


def _profile_status() -> dict[str, Any]:
    _code, value = _json_command([sys.executable, str(PROFILE_CLI), "status"])
    return value


def _store_basics(value: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "locale.language": ("Use one owner language across Life Manager", {"en", "ja"}),
        "locale.timezone": ("Schedule every loop in the owner's timezone", None),
        "notifications.channel": ("Send owner-visible outcomes through the chosen channel", {"local", "email", "telegram"}),
    }
    now = datetime.now(timezone.utc).isoformat()
    stored = []
    for field_id, (purpose, choices) in allowed.items():
        raw = value.get(field_id)
        if not isinstance(raw, str) or not raw.strip() or len(raw) > 120:
            raise ValueError(f"invalid profile field: {field_id}")
        clean = raw.strip()
        if choices is not None and clean not in choices:
            raise ValueError(f"invalid profile field: {field_id}")
        if field_id == "locale.timezone" and not all(
            part and part.replace("_", "").replace("-", "").isalnum()
            for part in clean.split("/")
        ):
            raise ValueError("invalid profile field: locale.timezone")
        field = {
            "privacy": "reusable", "source": "owner_input", "purpose": purpose,
            "scopes": ["*"], "updated_at": now, "expires_at": None,
            "consent": {"granted": True, "granted_at": now},
            "value": clean, "secret_ref": None, "evidence_sha256": None,
        }
        completed = subprocess.run(
            [sys.executable, str(PROFILE_CLI), "put", "--field-id", field_id],
            cwd=REPO, input=json.dumps(field), text=True, capture_output=True,
            timeout=10, check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"profile storage failed: {field_id}")
        stored.append(field_id)
    return {"status": "stored", "fields": stored}


def _export_profile() -> dict[str, Any]:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = Path.home() / "Documents" / "Life Manager" / f"profile-{stamp}.json"
    code, value = _json_command([
        sys.executable, str(PROFILE_CLI), "export", "--output", str(output),
    ])
    if code != 0:
        raise RuntimeError("profile export failed")
    return {**value, "location": "Documents/Life Manager"}


def _uninstall(integration_id: str) -> dict[str, Any]:
    manifest = _manifest(integration_id)
    completed = subprocess.run(
        manifest["lifecycle"]["uninstall"], cwd=REPO, capture_output=True,
        text=True, timeout=60, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("integration uninstall failed")
    return {"status": "uninstalled", "integration_id": integration_id,
            "private_state": "preserved"}


class Handler(BaseHTTPRequestHandler):
    token = ""

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.send_header("x-content-type-options", "nosniff")
        self.send_header("content-security-policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src data:")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict[str, Any]) -> None:
        self._send(status, "application/json; charset=utf-8",
                   json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8"))

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/" or self.path.startswith("/?"):
            body = HTML.read_text(encoding="utf-8").replace("__TOKEN__", self.token)
            self._send(HTTPStatus.OK, "text/html; charset=utf-8", body.encode("utf-8"))
            return
        if self.path == "/api/graph":
            try:
                self._json(HTTPStatus.OK, _graph())
            except Exception as error:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)[:160]})
            return
        if self.path == "/api/profile":
            self._json(HTTPStatus.OK, _profile_status())
            return
        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path not in {"/api/connect", "/api/profile", "/api/export", "/api/uninstall"}:
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        if self.headers.get("x-life-manager-token") != self.token:
            self._json(HTTPStatus.FORBIDDEN, {"error": "invalid local action token"})
            return
        try:
            if self.path == "/api/profile":
                length = int(self.headers.get("content-length") or 0)
                if length <= 0 or length > 8192:
                    raise ValueError("invalid request size")
                self._json(HTTPStatus.OK, _store_basics(json.loads(self.rfile.read(length))))
                return
            if self.path == "/api/export":
                self._json(HTTPStatus.OK, _export_profile())
                return
            if self.path == "/api/uninstall":
                length = int(self.headers.get("content-length") or 0)
                if length <= 0 or length > 8192:
                    raise ValueError("invalid request size")
                value = json.loads(self.rfile.read(length))
                if value.get("confirm") != "UNINSTALL":
                    raise ValueError("explicit uninstall confirmation required")
                self._json(HTTPStatus.OK, _uninstall(str(value.get("integration_id") or "")))
                return
            length = int(self.headers.get("content-length") or 0)
            if length <= 0 or length > 8192:
                raise ValueError("invalid request size")
            value = json.loads(self.rfile.read(length))
            integration_id = str(value.get("integration_id") or "")
            self._json(HTTPStatus.ACCEPTED, _connect(integration_id))
        except Exception as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)[:160]})

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18791)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        raise SystemExit("port must be between 1024 and 65535")
    Handler.token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(json.dumps({"status": "ready", "url": url}), flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
