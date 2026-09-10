from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[4]
PATH = ROOT / "skills/earn/crowdworks/scripts/paid_adapter.py"
OWNER = ROOT / "skills/earn/crowdworks/scripts/paid-owner"


def load():
    spec = importlib.util.spec_from_file_location("crowdworks_paid_adapter_test", PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def funded():
    return {"work_id": "63570481", "title": "Webデザイン業務", "client": "buyer",
            "provider_state": "funded", "milestone_id": "13798056",
            "form_url": "https://forms.gle/abc123", "application_date": "2026-09-09"}


def escrow():
    return {"work_id": "63568785", "title": "教材フィードバック", "client": "buyer2",
            "provider_state": "awaiting_escrow", "milestone_id": None, "form_url": None,
            "application_date": "2026-09-10"}


def delivered():
    return {"work_id": "63570481", "title": "Webデザイン業務", "client": "buyer",
            "provider_state": "delivered", "milestone_id": None, "form_url": None,
            "application_date": "2026-09-09"}


def load_kernel():
    path = ROOT / "skills/_shared/marketplace-core/scripts/paid_kernel.py"
    spec = importlib.util.spec_from_file_location("paid_kernel_crowdworks_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_two_official_active_contracts_normalize_to_unique_stable_observations():
    module = load()
    adapter = module.CrowdWorksPaidAdapter(
        account_id="7145638", inventory_reader=lambda: {"ok": True, "source_complete": True,
            "contract_candidates": [funded(), escrow()]})
    rows = adapter.observe_active()
    assert [(row["work_id"], row["provider_state"]) for row in rows] == [
        ("63570481", "funded"), ("63568785", "awaiting_escrow")]
    first = {key: value for key, value in rows[0].items() if key != "observed_at"}
    assert first == {key: value for key, value in adapter._observation(funded()).items()
                     if key != "observed_at"}
    assert len({row["latest_event_id"] for row in rows}) == 2


def test_funded_contract_decides_one_form_then_one_milestone_submission():
    module = load()
    action = module.decide({"context": {"contract": funded()}})
    assert action["action"] == "submit"
    assert action["payload"] == {"form_url": "https://forms.gle/abc123",
                                  "form_sha256": hashlib.sha256(b"https://forms.gle/abc123").hexdigest(),
                                  "milestone_id": "13798056"}


def test_funded_contract_without_labeled_official_application_date_waits_truthfully():
    module = load()
    action = module.decide({"context": {"contract": {key: value for key, value in funded().items()
                                                         if key != "application_date"}}})
    assert action["action"] == "wait"
    assert action["reason"] == "official_application_date_required"


def test_exact_verified_apply_receipt_is_jst_application_date_fallback(tmp_path):
    module = load()
    receipt = {"record_type": "application_receipt", "platform": "crowdworks", "status": "verified",
               "opportunity_external_id": "13440101", "application_external_id": "305139864",
               "opportunity_title": funded()["title"], "observed_at": "2026-09-09T11:34:46.521685+00:00"}
    path = tmp_path / "application-receipts.jsonl"; path.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
    adapter = module.CrowdWorksPaidAdapter(account_id="7145638", application_receipts_path=path)
    assert adapter._receipt_application_date(funded()["title"], "305139864") == "2026-09-09"


def test_ambiguous_or_nonexact_apply_receipt_never_supplies_date(tmp_path):
    module = load()
    valid = {"record_type": "application_receipt", "platform": "crowdworks", "status": "verified",
             "opportunity_external_id": "13440101", "application_external_id": "305139864",
             "opportunity_title": funded()["title"], "observed_at": "2026-09-09T11:34:46+00:00"}
    path = tmp_path / "application-receipts.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in (valid, {**valid, "observed_at": "2026-09-10T00:00:00+00:00"})) + "\n", encoding="utf-8")
    adapter = module.CrowdWorksPaidAdapter(account_id="7145638", application_receipts_path=path)
    assert adapter._receipt_application_date(funded()["title"], "305139864") is None
    assert adapter._receipt_application_date("other title", "305139864") is None


def test_apply_receipt_date_converts_to_asia_tokyo_before_form_use(tmp_path):
    module = load()
    row = {"record_type": "application_receipt", "platform": "crowdworks", "status": "verified",
           "opportunity_external_id": "13440101", "application_external_id": "305139864",
           "opportunity_title": funded()["title"], "observed_at": "2026-09-09T16:00:00+00:00"}
    path = tmp_path / "application-receipts.jsonl"; path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    assert module.CrowdWorksPaidAdapter(account_id="7145638", application_receipts_path=path)._receipt_application_date(
        funded()["title"], "305139864") == "2026-09-10"


def test_adapter_opens_injected_thread_owned_cdp_connection_not_account_browser():
    module = load()
    calls = []

    class Page:
        def set_default_timeout(self, timeout):
            calls.append(("timeout", timeout))

        def close(self):
            calls.append(("page_close",))

    class Context:
        def new_page(self):
            return Page()

    class Browser:
        contexts = [Context()]

    class Runtime:
        def stop(self):
            calls.append(("runtime_stop",))

    original = module.account._browser
    module.account._browser = lambda *_: (_ for _ in ()).throw(AssertionError("global browser forbidden"))
    try:
        adapter = module.CrowdWorksPaidAdapter(account_id="7145638",
            connection_factory=lambda: (Runtime(), Browser()))
        adapter._open()
        adapter.close()
    finally:
        module.account._browser = original
    assert calls == [("timeout", 15_000), ("page_close",), ("runtime_stop",)]


def test_observation_keeps_adapter_account_identity():
    module = load()
    adapter = module.CrowdWorksPaidAdapter(account_id="different-account")
    assert adapter._observation(funded())["account_id"] == "different-account"


def test_awaiting_escrow_never_authorizes_work_or_delivery():
    module = load()
    action = module.decide({"context": {"contract": escrow()}})
    assert action["action"] == "wait"
    assert action["reason"] == "awaiting_client_escrow"


def test_delivered_contract_is_kernel_noop_and_replay_zero(tmp_path):
    module, kernel = load(), load_kernel()
    adapter = module.CrowdWorksPaidAdapter(account_id="7145638", inventory_reader=lambda: {
        "ok": True, "source_complete": True, "contract_candidates": [delivered()]})
    result = kernel.run_wake(adapter=adapter, decide=module.decide, state_root=tmp_path)
    replay = kernel.run_wake(adapter=adapter, decide=module.decide, state_root=tmp_path)
    assert result["effect"] == replay["effect"] == 0
    assert result["items"][0]["status"] == replay["items"][0]["status"] == "completed"


def test_kernel_concurrency_keeps_paid_adapter_thread_state_isolated(tmp_path):
    module, kernel = load(), load_kernel()
    rows = [funded(), {**funded(), "work_id": "63570482", "milestone_id": "13798057"}]
    adapter = module.CrowdWorksPaidAdapter(account_id="7145638", inventory_reader=lambda: {
        "ok": True, "source_complete": True, "contract_candidates": rows})
    barrier, seen, mutated = threading.Barrier(2), [], set()

    def observe_one(work_id):
        item = next(row for row in rows if row["work_id"] == work_id)
        adapter._items = {work_id: item}
        return adapter._observation(item)

    def context(work_id):
        return {"contract": adapter._items[work_id]}

    def mutate(intent):
        barrier.wait(timeout=3)
        seen.append((threading.get_ident(), id(adapter._items), intent["work_id"]))
        mutated.add(intent["work_id"])

    adapter.observe_one, adapter.context, adapter.mutate = observe_one, context, mutate
    adapter.readback = lambda intent: ({"verified": True, "provider_receipt_id": intent["work_id"], "observed_at": "now"}
                                      if intent["work_id"] in mutated else {"authoritative_absent": True})
    result = kernel.run_wake(adapter=adapter, decide=module.decide, state_root=tmp_path, max_workers=2)
    assert result["effect"] == 2 and result["failed"] == 0
    assert len({thread for thread, _, _ in seen}) == len({items for _, items, _ in seen}) == 2


def test_prepared_form_receipt_fences_replay_before_any_second_post(tmp_path):
    module = load()
    url = funded()["form_url"]
    digest = hashlib.sha256(url.encode()).hexdigest()
    adapter = module.CrowdWorksPaidAdapter(account_id="7145638", state_path=tmp_path)
    binding = adapter._form_binding(funded(), digest)
    receipt = tmp_path / "external-actions" / f"{digest}.json"
    receipt.parent.mkdir()
    (tmp_path / "external-actions" / f"index-{module.google_form._identity(binding)}.json").write_text(
        json.dumps({"version": 1, "status": "prepared", "receipt_key": digest}), encoding="utf-8")
    receipt.write_text(json.dumps({"version": 1, "status": "prepared", "url_sha256": digest,
                                   "binding": binding}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="google_form_submission_uncertain"):
        adapter._submit_form_once(funded())


def test_paid_form_receipts_are_isolated_by_contract_binding(tmp_path):
    module = load()
    binding = module.CrowdWorksPaidAdapter(account_id="7145638", state_path=tmp_path)._form_binding(
        funded(), hashlib.sha256(funded()["form_url"].encode()).hexdigest())
    other = {**binding, "contract_id": "63570482", "milestone_id": "13798057"}
    key = module.google_form._identity({**binding, "submission_payload_sha256": "payload"})
    receipt = tmp_path / "external-actions" / f"{key}.json"
    receipt.parent.mkdir()
    receipt.write_text(json.dumps({"binding": binding, "confirmation_sha256": "receipt"}), encoding="utf-8")
    (tmp_path / "external-actions" / f"index-{module.google_form._identity(binding)}.json").write_text(
        json.dumps({"receipt_key": key, "status": "confirmed"}), encoding="utf-8")
    assert module.google_form.bound_receipt(tmp_path, binding)["confirmation_sha256"] == "receipt"
    assert module.google_form.bound_receipt(tmp_path, other) is None


def test_date_question_uses_google_forms_year_month_day_fields(tmp_path):
    module = load()

    class Locator:
        def inner_text(self):
            return "task"

    class Page:
        def locator(self, selector):
            assert selector == "body"
            return Locator()

        def evaluate(self, expression):
            return [[None, "応募日", None, 9, [[12, [], True]]]]

    adapter = module.CrowdWorksPaidAdapter(account_id="7145638", state_path=tmp_path)
    fields = dict(adapter._form_fields(Page(), funded()))
    assert set(fields) == {"entry.12_year", "entry.12_month", "entry.12_day"}


def test_readback_rejects_non_submit_without_browser_mutation(tmp_path):
    module = load()
    adapter = module.CrowdWorksPaidAdapter(account_id="7145638", state_path=tmp_path)
    assert adapter.readback({"action": "noop"}) == {"authoritative_absent": True}


def test_owner_uses_shared_kernel_and_provider_adapter_state_root():
    source = OWNER.read_text(encoding="utf-8")
    assert "skills/_shared/marketplace-core/scripts/paid_kernel.py" in source
    assert "skills/earn/crowdworks/scripts/paid_adapter.py" in source
    assert '--state-root "$STATE_ROOT/paid"' in source
    assert '--state-path "$STATE_ROOT/paid"' in source
