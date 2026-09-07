import importlib.util
import json
from pathlib import Path
import sys

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[4]
MODULE = Path(__file__).resolve().parents[1] / "scripts/financial_record.py"
SPEC = importlib.util.spec_from_file_location("marketplace_financial_record_test", MODULE)
financial = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = financial
SPEC.loader.exec_module(financial)
SCHEMA = json.loads((ROOT / "runtime/contracts/common-record.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def _validate(record):
    errors = list(VALIDATOR.iter_errors(record))
    assert not errors, errors[0].message if errors else ""


def payment():
    return {
        "schema_version": 1, "record_type": "payment_receipt", "platform": "mercor",
        "work_external_id": "work-1", "payment_external_id": "payment-1", "receipt_id": "receipt-1",
        "gross_amount_minor": 10000, "fee_amount_minor": 1000, "cost_amount_minor": 500,
        "net_amount_minor": 8500, "currency": "USD", "status": "settled",
        "occurred_at": "2026-09-07T01:00:00Z", "observed_at": "2026-09-07T01:01:00Z",
    }


def test_payment_projects_gross_fee_and_cost_without_net_double_counting():
    records = financial.payment_to_financial_records(payment(), subject_id="tenant-1")
    assert [(row["kind"], row["amount_minor"]) for row in records] == [
        ("business_revenue", 10000), ("fee", 1000), ("business_cost", 500),
    ]
    assert records == financial.payment_to_financial_records(payment(), subject_id="tenant-1")
    assert records[0]["record_id"] != financial.payment_to_financial_records(payment(), subject_id="tenant-2")[0]["record_id"]
    for record in records:
        _validate(record)


def test_matched_payout_projects_separately_from_revenue():
    value = {
        "schema_version": 1, "record_type": "payout_match_receipt", "platform": "mercor",
        "payment_external_id": "payment-1", "payout_external_id": "payout-1",
        "bank_transaction_external_id": "bank-1", "status": "matched", "amount_minor": 8500,
        "currency": "USD", "observed_at": "2026-09-07T01:02:00Z",
    }
    record = financial.payout_to_financial_record(value, subject_id="tenant-1")
    assert (record["kind"], record["amount_minor"]) == ("payout", 8500)
    _validate(record)
    sibling = financial.payout_to_financial_record(
        {**value, "payment_external_id": "payment-2"}, subject_id="tenant-1",
    )
    assert sibling["record_id"] != record["record_id"]


def test_platform_with_underscore_uses_fixed_evidence_scheme():
    value = {**payment(), "platform": "crowd_works"}
    [record, *_] = financial.payment_to_financial_records(value, subject_id="tenant-1")
    assert record["verification"]["evidence_refs"][0].startswith("marketplace://crowd_works/")
    _validate(record)
