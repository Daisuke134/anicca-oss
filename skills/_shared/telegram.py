#!/usr/bin/env python3
"""Direct Telegram Bot API sender with truthful delivery receipts.

This module deliberately has no OpenClaw or third-party Python dependency.
Configuration comes from the process environment and the private Life Manager state env.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import socket
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping


DEFAULT_ENV_FILE = Path.home() / ".local" / "state" / "life-manager" / ".env"
DEFAULT_API_BASE = "https://api.telegram.org"
TEXT_CHUNK_LIMIT = 4000
CAPTION_LIMIT = 1024
CLOUD_FILE_LIMIT = 50 * 1024 * 1024


class TelegramError(RuntimeError):
    """A confirmed Telegram API or local validation failure."""

    def __init__(
        self,
        message: str,
        *,
        error_code: int | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.retry_after = retry_after


class TelegramDeliveryUnknown(TelegramError):
    """The transport failed after a send began, so delivery is unknown."""


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_config(
    environ: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> tuple[str, str]:
    process_env = dict(os.environ if environ is None else environ)
    selected_file = env_file
    if selected_file is None:
        selected_file = Path(process_env.get("LIFE_MANAGER_ENV_FILE", DEFAULT_ENV_FILE))
    file_env = _parse_env_file(selected_file)
    token = process_env.get("TELEGRAM_BOT_TOKEN") or file_env.get("TELEGRAM_BOT_TOKEN")
    chat_id = (
        process_env.get("TELEGRAM_CHAT_ID")
        or process_env.get("TELEGRAM_ALERT_CHAT_ID")
        or file_env.get("TELEGRAM_CHAT_ID")
        or file_env.get("TELEGRAM_ALERT_CHAT_ID")
    )
    missing = [
        name
        for name, value in (
            ("TELEGRAM_BOT_TOKEN", token),
            ("TELEGRAM_CHAT_ID", chat_id),
        )
        if not value
    ]
    if missing:
        raise TelegramError(
            f"missing required configuration: {', '.join(missing)} "
            f"(checked process environment and {selected_file})"
        )
    return str(token), str(chat_id)


def _split_text(text: str, limit: int = TEXT_CHUNK_LIMIT) -> list[str]:
    if not text:
        raise TelegramError("text must not be empty")
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit + 1)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
        if remaining.startswith("\n") or remaining.startswith(" "):
            remaining = remaining[1:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _multipart_body(
    fields: Mapping[str, str],
    file_field: str,
    path: Path,
) -> tuple[bytes, str]:
    boundary = f"anicca-{uuid.uuid4().hex}"
    marker = boundary.encode("ascii")
    body = bytearray()
    for name, value in fields.items():
        body.extend(b"--" + marker + b"\r\n")
        body.extend(
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8")
        )
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    mime_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    safe_name = path.name.replace('"', "_").replace("\r", "_").replace("\n", "_")
    body.extend(b"--" + marker + b"\r\n")
    body.extend(
        (
            f'Content-Disposition: form-data; name="{file_field}"; '
            f'filename="{safe_name}"\r\n'
        ).encode("utf-8")
    )
    body.extend(f"Content-Type: {mime_type}\r\n\r\n".encode("ascii"))
    body.extend(path.read_bytes())
    body.extend(b"\r\n--" + marker + b"--\r\n")
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def _validate_media(path: str | Path, caption: str | None) -> Path:
    media_path = Path(path).expanduser().resolve()
    if not media_path.is_file():
        raise TelegramError(f"media file does not exist or is not readable: {media_path}")
    if media_path.stat().st_size > CLOUD_FILE_LIMIT:
        raise TelegramError(
            f"media exceeds Telegram cloud Bot API 50 MB limit: {media_path.name}"
        )
    if caption is not None and len(caption) > CAPTION_LIMIT:
        raise TelegramError(
            f"caption exceeds Telegram {CAPTION_LIMIT}-character limit"
        )
    return media_path


@dataclass
class TelegramClient:
    token: str
    chat_id: str
    api_base: str = DEFAULT_API_BASE
    timeout: float = 20.0
    max_retry_after: int = 30
    opener: Callable[..., Any] = urllib.request.urlopen
    sleeper: Callable[[float], None] = time.sleep

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        env_file: Path | None = None,
        **kwargs: Any,
    ) -> "TelegramClient":
        token, chat_id = load_config(environ=environ, env_file=env_file)
        return cls(token=token, chat_id=chat_id, **kwargs)

    def _redact(self, value: object) -> str:
        return str(value).replace(self.token, "***")

    def _decode_response(self, raw: bytes) -> dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelegramError("Telegram returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise TelegramError("Telegram returned an invalid response object")
        return payload

    def _request(
        self,
        method: str,
        fields: Mapping[str, Any] | None = None,
        *,
        file_field: str | None = None,
        file_path: Path | None = None,
        retry_count: int = 0,
    ) -> Any:
        clean_fields = {
            key: ("true" if value is True else "false" if value is False else str(value))
            for key, value in (fields or {}).items()
            if value is not None
        }
        if file_field and file_path:
            data, content_type = _multipart_body(clean_fields, file_field, file_path)
        else:
            data = json.dumps(clean_fields).encode("utf-8")
            content_type = "application/json"
        url = f"{self.api_base.rstrip('/')}/bot{self.token}/{method}"
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": content_type, "Accept": "application/json"},
            method="POST",
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = self._decode_response(response.read())
        except urllib.error.HTTPError as exc:
            try:
                raw_error = exc.read()
            finally:
                exc.close()
            try:
                payload = self._decode_response(raw_error)
            except TelegramError:
                raise TelegramError(
                    f"Telegram HTTP error {exc.code}",
                    error_code=exc.code,
                ) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            raise TelegramDeliveryUnknown(
                f"Telegram transport failed; delivery unknown: {self._redact(exc)}"
            ) from None

        if not payload.get("ok"):
            error_code = payload.get("error_code")
            parameters = payload.get("parameters") or {}
            retry_after = parameters.get("retry_after")
            if (
                error_code == 429
                and retry_count == 0
                and isinstance(retry_after, int)
                and 0 <= retry_after <= self.max_retry_after
            ):
                self.sleeper(retry_after)
                return self._request(
                    method,
                    fields,
                    file_field=file_field,
                    file_path=file_path,
                    retry_count=1,
                )
            description = self._redact(payload.get("description", "Telegram API error"))
            raise TelegramError(
                description,
                error_code=error_code if isinstance(error_code, int) else None,
                retry_after=retry_after if isinstance(retry_after, int) else None,
            )
        return payload.get("result")

    @staticmethod
    def _receipt(method: str, result: Mapping[str, Any]) -> dict[str, Any]:
        chat = result.get("chat") or {}
        return {
            "status": "delivered",
            "method": method,
            "chat_id": chat.get("id"),
            "message_ids": [result.get("message_id")],
            "date": result.get("date"),
        }

    def get_me(self) -> dict[str, Any]:
        result = self._request("getMe")
        if not isinstance(result, dict):
            raise TelegramError("getMe returned an invalid result")
        return {
            "status": "verified",
            "bot_id": result.get("id"),
            "username": result.get("username"),
        }

    def send_text(self, text: str, *, chat_id: str | None = None) -> dict[str, Any]:
        receipts = []
        for chunk in _split_text(text):
            result = self._request(
                "sendMessage",
                {"chat_id": chat_id or self.chat_id, "text": chunk},
            )
            if not isinstance(result, dict):
                raise TelegramError("sendMessage returned an invalid result")
            receipts.append(self._receipt("sendMessage", result))
        return {
            "status": "delivered",
            "method": "sendMessage",
            "chat_id": receipts[-1]["chat_id"],
            "message_ids": [
                message_id
                for receipt in receipts
                for message_id in receipt["message_ids"]
            ],
            "chunks": len(receipts),
        }

    def _send_media(
        self,
        method: str,
        file_field: str,
        path: str | Path,
        *,
        caption: str | None = None,
        chat_id: str | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        media_path = _validate_media(path, caption)
        fields: dict[str, Any] = {"chat_id": chat_id or self.chat_id}
        if caption is not None:
            fields["caption"] = caption
        fields.update(extra or {})
        result = self._request(
            method,
            fields,
            file_field=file_field,
            file_path=media_path,
        )
        if not isinstance(result, dict):
            raise TelegramError(f"{method} returned an invalid result")
        return self._receipt(method, result)

    def send_document(
        self, path: str | Path, *, caption: str | None = None, chat_id: str | None = None
    ) -> dict[str, Any]:
        return self._send_media(
            "sendDocument", "document", path, caption=caption, chat_id=chat_id
        )

    def send_photo(
        self, path: str | Path, *, caption: str | None = None, chat_id: str | None = None
    ) -> dict[str, Any]:
        return self._send_media(
            "sendPhoto", "photo", path, caption=caption, chat_id=chat_id
        )

    def send_video(
        self, path: str | Path, *, caption: str | None = None, chat_id: str | None = None
    ) -> dict[str, Any]:
        return self._send_media(
            "sendVideo",
            "video",
            path,
            caption=caption,
            chat_id=chat_id,
            extra={"supports_streaming": True},
        )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chat-id", help="override TELEGRAM_CHAT_ID")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("get-me")
    text_parser = subparsers.add_parser("text")
    text_parser.add_argument("text")
    for command in ("document", "photo", "video"):
        media_parser = subparsers.add_parser(command)
        media_parser.add_argument("path")
        media_parser.add_argument("--caption")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        client = TelegramClient.from_env()
        if args.command == "get-me":
            receipt = client.get_me()
        elif args.command == "text":
            receipt = client.send_text(args.text, chat_id=args.chat_id)
        else:
            sender = getattr(client, f"send_{args.command}")
            receipt = sender(args.path, caption=args.caption, chat_id=args.chat_id)
        print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
        return 0
    except TelegramDeliveryUnknown as exc:
        print(
            json.dumps(
                {"status": "delivery_unknown", "error": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except TelegramError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "error_code": exc.error_code,
                    "retry_after": exc.retry_after,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
