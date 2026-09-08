import importlib.util, json, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[4]; PATH=ROOT/"skills/earn/mercor/scripts/paid_adapter.py"
def load():
    spec=importlib.util.spec_from_file_location("mercor_paid_adapter_test",PATH); module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; spec.loader.exec_module(module); return module
def event(state,event_id="event-1"): return {"work_id":"work-1","event_id":event_id,"state":state,"evidence_ref":"https://work.mercor.com/jobs/work-1","observed_at":"2026-09-07T00:00:00Z"}
def snapshot(path, contracts, observed_at=None): path.write_text(json.dumps({"version":1,"observed_at":observed_at or datetime.now(timezone.utc).isoformat(),"contracts":contracts}))
def test_missing_inventory_is_pending(tmp_path):
    m=load(); a=m.MercorPaidAdapter(account_id="default",official_snapshot=tmp_path/"missing",work_events=tmp_path/"events")
    with pytest.raises(m.MercorPaidWait): a.observe_active()
def test_non_official_inventory_evidence_is_pending(tmp_path):
    m=load(); s=tmp_path/"snapshot"; snapshot(s,[]); p=tmp_path/"events"; row=event("contracted"); row["evidence_ref"]="gmail://message/123"; p.write_text(json.dumps(row)+"\n"); a=m.MercorPaidAdapter(account_id="default",official_snapshot=s,work_events=p)
    with pytest.raises(m.MercorPaidWait, match="official_work_receipt_required"): a.observe_active()
def test_latest_active_event_is_normalized(tmp_path):
    m=load(); s=tmp_path/"snapshot"; snapshot(s,[]); p=tmp_path/"events"; p.write_text(json.dumps(event("selected"))+"\n"+json.dumps(event("contracted","event-2"))+"\n"); a=m.MercorPaidAdapter(account_id="default",official_snapshot=s,work_events=p)
    assert a.observe_active()[0]["latest_event_id"]=="event-2"
def test_official_empty_contract_inventory_is_available(tmp_path):
    m=load(); s=tmp_path/"snapshot"; snapshot(s,[]); a=m.MercorPaidAdapter(account_id="default",official_snapshot=s,work_events=tmp_path/"missing")
    assert a.observe_active()==[]
def test_official_active_contract_becomes_paid_work_item(tmp_path):
    m=load(); s=tmp_path/"snapshot"; snapshot(s,[{"jobId":"job-1","title":"Japanese Writer","status":"active"}]); a=m.MercorPaidAdapter(account_id="default",official_snapshot=s,work_events=tmp_path/"missing")
    row=a.observe_active()[0]; assert row["work_id"]=="job-1" and row["provider_state"]=="contracted"
def test_newer_official_contract_state_wins_over_older_work_event(tmp_path):
    m=load(); s=tmp_path/"snapshot"; snapshot(s,[{"jobId":"work-1","status":"active"}]); p=tmp_path/"events"; p.write_text(json.dumps(event("accepted"))+"\n"); a=m.MercorPaidAdapter(account_id="default",official_snapshot=s,work_events=p)
    assert a.observe_active()[0]["provider_state"]=="contracted"
def test_stale_official_contract_snapshot_is_pending(tmp_path):
    m=load(); s=tmp_path/"snapshot"; snapshot(s,[],"2026-01-01T00:00:00Z"); a=m.MercorPaidAdapter(account_id="default",official_snapshot=s,work_events=tmp_path/"missing")
    with pytest.raises(m.MercorPaidWait, match="official_work_inventory_stale"): a.observe_active()
def test_wrong_official_snapshot_version_fails_closed(tmp_path):
    m=load(); s=tmp_path/"snapshot"; s.write_text(json.dumps({"version":2,"observed_at":datetime.now(timezone.utc).isoformat(),"contracts":[]})); a=m.MercorPaidAdapter(account_id="default",official_snapshot=s,work_events=tmp_path/"missing")
    with pytest.raises(RuntimeError, match="mercor_paid_inventory_unavailable"): a.observe_active()
def test_work_event_order_uses_normalized_rfc3339_time(tmp_path):
    m=load(); now=datetime.now(timezone.utc); jst=timezone(timedelta(hours=9)); s=tmp_path/"snapshot"; snapshot(s,[{"jobId":"work-1","status":"active"}],now.astimezone(jst).isoformat()); p=tmp_path/"events"; newer=event("accepted"); newer["observed_at"]=(now+timedelta(seconds=30)).isoformat(); p.write_text(json.dumps(newer)+"\n"); a=m.MercorPaidAdapter(account_id="default",official_snapshot=s,work_events=p)
    assert a.observe_active()==[]
def test_invalid_work_event_timestamp_fails_closed(tmp_path):
    m=load(); s=tmp_path/"snapshot"; snapshot(s,[]); p=tmp_path/"events"; row=event("contracted"); row["observed_at"]="yesterday"; p.write_text(json.dumps(row)+"\n"); a=m.MercorPaidAdapter(account_id="default",official_snapshot=s,work_events=p)
    with pytest.raises(RuntimeError, match="mercor_paid_inventory_unavailable"): a.observe_active()
def test_human_submission_is_wait():
    m=load(); decision=m.decide({"provider_state":"authorized_work"}); assert decision["action"]=="wait" and decision["remaining_work"]
def test_submitted_is_noop():
    m=load(); assert m.decide({"provider_state":"work_submitted"})=={"action":"noop","classification":"awaiting_buyer"}
