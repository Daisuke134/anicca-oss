#!/usr/bin/env python3
"""Extract note.com cookies from the daily-driver Chromium profile (mock-keychain).
Browser must be CLOSED (or copy the sqlite) to avoid a lock. Writes a real persistent
JSON the ephemeral note scripts reuse. NEVER /tmp — survives reboot/cleanup."""
import sqlite3, shutil, json, os
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend

PROFILE = os.path.expanduser("~/.cloak/profiles/daily-driver")
SRC = PROFILE + "/Default/Cookies"
WORK = os.path.join(os.path.expanduser(os.environ.get("WRITER_STATE_DIR", "~/.local/state/life-manager/writer")), "note-work")
COPY = WORK + "/cookies-copy.db"
OUT = WORK + "/note-cookies.json"

kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=b"saltysalt",
                 iterations=1003, backend=default_backend())
KEY = kdf.derive(b"mock_password")

def decrypt(enc):
    if not enc or enc[:3] != b"v10":
        return None
    enc = enc[3:]
    dec = Cipher(algorithms.AES(KEY), modes.CBC(b" " * 16),
                 backend=default_backend()).decryptor()
    val = dec.update(enc) + dec.finalize()
    if val:
        val = val[:-val[-1]]  # strip PKCS7 pad
    # Chrome 130+ prepends a 32-byte SHA256 domain hash; strip if present
    out = val[32:].decode("utf-8", "ignore")
    if not out:
        out = val.decode("utf-8", "ignore")
    return out

shutil.copy2(SRC, COPY)
con = sqlite3.connect(COPY)
rows = con.execute("SELECT name, encrypted_value FROM cookies WHERE host_key LIKE '%note.com%'").fetchall()
con.close()
ck = {}
for name, enc in rows:
    v = decrypt(enc)
    if v:
        ck[name] = v
json.dump(ck, open(OUT, "w"))
print(f"extracted {len(ck)} note.com cookies -> {OUT}")
print("keys:", ",".join(sorted(ck.keys()))[:200])
