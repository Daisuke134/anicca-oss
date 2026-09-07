import importlib.util, json, sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[4]; PATH=ROOT/"skills/earn/mercor/scripts/paid_adapter.py"
def load():
    spec=importlib.util.spec_from_file_location("mercor_paid_adapter_test",PATH); module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module); return module
def event(state,event_id="event-1"): return {"work_id":"work-1","event_id":event_id,"state":state,"evidence_ref":"https://work.mercor.com/jobs/work-1","observed_at":"2026-09-07T00:00:00Z"}
def test_missing_inventory_is_pending(tmp_path):
    m=load(); a=m.MercorPaidAdapter(account_id="default",work_events=tmp_path/"missing")
    with pytest.raises(m.MercorPaidWait): a.observe_active()
def test_non_official_inventory_evidence_is_pending(tmp_path):
    m=load(); p=tmp_path/"events"; row=event("contracted"); row["evidence_ref"]="gmail://message/123"; p.write_text(json.dumps(row)+"\n"); a=m.MercorPaidAdapter(account_id="default",work_events=p)
    with pytest.raises(m.MercorPaidWait, match="official_work_receipt_required"): a.observe_active()
def test_latest_active_event_is_normalized(tmp_path):
    m=load(); p=tmp_path/"events"; p.write_text(json.dumps(event("selected"))+"\n"+json.dumps(event("contracted","event-2"))+"\n"); a=m.MercorPaidAdapter(account_id="default",work_events=p)
    assert a.observe_active()[0]["latest_event_id"]=="event-2"
def test_human_submission_is_wait():
    m=load(); decision=m.decide({"provider_state":"authorized_work"}); assert decision["action"]=="wait" and decision["remaining_work"]
def test_submitted_is_noop():
    m=load(); assert m.decide({"provider_state":"work_submitted"})=={"action":"noop","classification":"awaiting_buyer"}
