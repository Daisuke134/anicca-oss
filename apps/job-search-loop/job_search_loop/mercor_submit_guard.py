from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .mercor_provider import MercorListing, is_approved_mercor_url, ready_for_submit


class MercorSubmitGuardError(ValueError):
    pass


@dataclass(frozen=True)
class MercorSubmitClaim:
    listing_id: str
    title: str
    url: str
    claim_token: str
    pre_submit_evidence_sha256: str


def fenced_listing_ids(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    identifiers: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        listing_id = value.get("listing_id") if isinstance(value, dict) else None
        if isinstance(listing_id, str) and listing_id.strip():
            normalized = listing_id.strip()
            if value.get("status") == "claim_released_no_effect":
                identifiers.discard(normalized)
            else:
                identifiers.add(normalized)
    return identifiers


def release_claim_without_effect(
    *, fence_ledger: Path, listing_id: str, readback_evidence: Path, run_id: str,
) -> None:
    """Release a false claim only after fresh evidence proves the final submit is still available."""
    evidence = readback_evidence.expanduser().resolve()
    if not evidence.is_file():
        raise MercorSubmitGuardError("no-effect readback evidence is missing")
    text = evidence.read_text(encoding="utf-8", errors="replace").casefold()
    if "submit application" not in text:
        raise MercorSubmitGuardError("no-effect readback does not show submit application")
    ledger = fence_ledger.expanduser().resolve()
    lock_path = ledger.with_name(f"{ledger.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if listing_id not in fenced_listing_ids(ledger):
            return
        event = {
            "listing_id": listing_id,
            "status": "claim_released_no_effect",
            "run_id": run_id,
            "readback_evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        with ledger.open("a", encoding="utf-8") as output:
            output.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(ledger, 0o600)


def claim_submission_once(
    *,
    fence_ledger: Path,
    listing_id: str,
    title: str,
    url: str,
    pre_submit_evidence: Path,
    run_id: str,
) -> bool:
    """Durably fence one listing before the browser click."""
    listing_id = listing_id.strip()
    if not listing_id or not is_approved_mercor_url(url):
        raise MercorSubmitGuardError("invalid Mercor submission identity")
    evidence = pre_submit_evidence.expanduser().resolve()
    if not evidence.is_file():
        raise MercorSubmitGuardError("pre-submit evidence is missing")
    ledger = fence_ledger.expanduser().resolve()
    ledger.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = ledger.with_name(f"{ledger.name}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        if listing_id in fenced_listing_ids(ledger):
            return False
        evidence_sha256 = hashlib.sha256(evidence.read_bytes()).hexdigest()
        claim_token = hashlib.sha256(
            f"{listing_id}\n{url}\n{evidence_sha256}".encode("utf-8")
        ).hexdigest()
        event = {
            "listing_id": listing_id,
            "title": title,
            "url": url,
            "status": "submit_claimed",
            "run_id": run_id,
            "claim_token": claim_token,
            "pre_submit_evidence_sha256": evidence_sha256,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        with ledger.open("a", encoding="utf-8") as output:
            output.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(ledger, 0o600)
        return True


def claim_ready_submission(
    listing: MercorListing,
    *,
    submitted_listing_ids: set[str],
    pre_submit_evidence: Path,
) -> MercorSubmitClaim | None:
    """Create the one-click claim only after ready state and fresh evidence exist."""
    if listing.listing_id in submitted_listing_ids:
        return None
    if not is_approved_mercor_url(listing.url):
        raise MercorSubmitGuardError("listing URL is outside the approved Mercor domain")
    if not ready_for_submit(listing):
        raise MercorSubmitGuardError("listing is not in the live 3/3 ready state")
    evidence_path = Path(pre_submit_evidence).expanduser().resolve()
    if not evidence_path.is_file():
        raise MercorSubmitGuardError("pre-submit evidence is missing")
    evidence_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    claim_token = hashlib.sha256(
        f"{listing.listing_id}\n{listing.url}\n{evidence_sha256}".encode("utf-8")
    ).hexdigest()
    return MercorSubmitClaim(
        listing_id=listing.listing_id,
        title=listing.title,
        url=listing.url,
        claim_token=claim_token,
        pre_submit_evidence_sha256=evidence_sha256,
    )


def classify_submit_readback(*, page_url: str, visible_text: str) -> str:
    """Classify only an authoritative visible result; ambiguous means no retry."""
    text = visible_text.casefold()
    if (
        is_approved_mercor_url(page_url)
        and "your application has been submitted" in text
        and "/jobs/apply/" in page_url
    ):
        return "submitted_pending_review"
    return "submit_unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fence-ledger", required=True, type=Path)
    parser.add_argument("--listing-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--url", required=True)
    parser.add_argument("--pre-submit-evidence", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args(argv)
    claimed = claim_submission_once(
        fence_ledger=args.fence_ledger,
        listing_id=args.listing_id,
        title=args.title,
        url=args.url,
        pre_submit_evidence=args.pre_submit_evidence,
        run_id=args.run_id,
    )
    print(json.dumps({"claimed": claimed, "listing_id": args.listing_id}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
