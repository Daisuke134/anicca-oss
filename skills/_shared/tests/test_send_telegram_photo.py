import json
import os
import shutil
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "send-telegram-photo.sh"


def _run(tmp_path: Path, receipt: dict, *extra: str) -> subprocess.CompletedProcess[str]:
    work = tmp_path / "shared"
    work.mkdir()
    shutil.copy2(SCRIPT, work / SCRIPT.name)
    (work / "telegram.py").write_text(
        "import json, os, sys\n"
        "open(os.environ['ARGS_PATH'], 'w').write(json.dumps(sys.argv[1:]))\n"
        "print(os.environ['RECEIPT'])\n",
        encoding="utf-8",
    )
    photo = tmp_path / "proof.png"
    photo.write_bytes(b"png")
    env = dict(os.environ)
    env["ARGS_PATH"] = str(tmp_path / "args.json")
    env["RECEIPT"] = json.dumps(receipt)
    return subprocess.run(
        ["/bin/bash", str(work / SCRIPT.name), str(photo), "proof", *extra],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )


def _delivered(message_id=42):
    return {"status": "delivered", "method": "sendPhoto", "message_ids": [message_id]}


def test_default_chat_id_works_under_macos_bash_contract(tmp_path):
    result = _run(tmp_path, _delivered())
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "TELEGRAM_PHOTO_SENT=true MSGID=42"
    assert json.loads((tmp_path / "args.json").read_text())[:1] == ["photo"]


def test_explicit_chat_id_is_forwarded(tmp_path):
    result = _run(tmp_path, _delivered(43), "12345")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "TELEGRAM_PHOTO_SENT=true MSGID=43"
    assert json.loads((tmp_path / "args.json").read_text())[:3] == ["--chat-id", "12345", "photo"]


def test_invalid_receipts_fail_closed(tmp_path):
    invalid = [
        {"status": "delivered", "method": "sendPhoto", "message_ids": [None]},
        {"status": "failed", "method": "sendPhoto", "message_ids": [44]},
        {"status": "delivered", "method": "sendMessage", "message_ids": [45]},
        {"status": "delivered", "method": "sendPhoto", "message_ids": [0]},
    ]
    for index, receipt in enumerate(invalid):
        case = tmp_path / str(index)
        case.mkdir()
        result = _run(case, receipt)
        assert result.returncode != 0
        assert "TELEGRAM_PHOTO_SENT=true" not in result.stdout
