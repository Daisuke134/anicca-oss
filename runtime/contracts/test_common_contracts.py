import json
import hashlib
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from runtime.loop import runtime_event


ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "runtime/contracts/common-record.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def financial_id(subject_id, idempotency_key):
    digest = hashlib.sha256(f"{subject_id}\n{idempotency_key}".encode()).hexdigest()
    return f"financial:{digest}"


def validate(value):
    errors = sorted(VALIDATOR.iter_errors(value), key=lambda error: list(error.path))
    if errors:
        raise AssertionError(errors[0].message)


class CommonContractTests(unittest.TestCase):
    def test_citizen_identity_matches_the_shared_schema(self):
        script = (
            "const c=require('./runtime/contracts/common-record.cjs');"
            "process.stdout.write(JSON.stringify(c.createCitizenIdentity({"
            "tenantId:'tenant-1',citizenId:'citizen-1',instanceId:'instance-1',"
            "walletAddress:'0x'+'a'.repeat(40)})));"
        )
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, text=True, capture_output=True, check=True,
        )
        validate(json.loads(result.stdout))

    def test_sqlite_outbox_adapter_output_matches_the_common_schema(self):
        path = ROOT / "skills/_shared/marketplace-core/scripts/telegram_outbox.py"
        spec = importlib.util.spec_from_file_location("contract_test_telegram_outbox", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        item = module.OutboxItem(
            event_key="writer:daily:1", message_sha256="a" * 64, message="report",
            status="delivery_uncertain", attempt_count=1, provider_message_id=None,
            created_at="2026-09-07T00:00:00Z", claimed_at="2026-09-07T00:00:01Z",
            delivered_at=None, last_error_code="sender_abandoned",
        )
        validate(module.to_common_outbox(item, loop_id="writer", tenant_id="user-1"))
        with self.assertRaisesRegex(ValueError, "message_key bounds"):
            module.to_common_outbox(
                module.OutboxItem(**{**item.__dict__, "event_key": "x" * 1025}),
                loop_id="writer", tenant_id="user-1",
            )
        with self.assertRaisesRegex(ValueError, "created_at is invalid"):
            module.to_common_outbox(
                module.OutboxItem(**{**item.__dict__, "created_at": "not-a-time"}),
                loop_id="writer", tenant_id="user-1",
            )

    def test_jsonl_job_adapter_output_matches_the_common_schema(self):
        legacy_job = {
            "job_id": "job-1", "tenant_id": "user-1", "loop_id": "marketing.video",
            "capability": "marketing.video.publish", "effect_class": "publish",
            "effect_key": "📣" * 300, "input_refs": {"content_ref": "🎬" * 600},
            "max_attempts": 3,
        }
        script = (
            "const {projectJob}=require('./runtime/contracts/common-record.cjs');"
            "let s='';process.stdin.on('data',c=>s+=c);"
            "process.stdin.on('end',()=>process.stdout.write(JSON.stringify(projectJob(JSON.parse(s)))));"
        )
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, input=json.dumps(legacy_job),
            text=True, capture_output=True, check=True,
        )
        validate(json.loads(result.stdout))

    def test_postgres_receipt_adapter_output_matches_the_common_schema(self):
        row = {
            "job_id": "job-1", "tenant_id": "user-1", "attempt": 1,
            "outcome": "completed", "effect_key": "postiz:post-1",
            "loop_id": "marketing.video", "effect_class": "publish",
            "created_at": "2026-09-07T00:00:00Z",
            "receipt": {"provider": "postiz", "provider_post_id": "post-1", "run_id": "run-1",
                        "evidence_refs": ["postiz://post/post-1"]},
        }
        script = (
            "const store=require('./apps/life-manager/lib/runtime-job-store.js');"
            "let s='';process.stdin.on('data',c=>s+=c);process.stdin.on('end',async()=>{"
            "const row=JSON.parse(s);const out=await store.readCommonReceipt("
            "{tenantId:row.tenant_id,jobId:row.job_id,attempt:row.attempt},"
            "{query:async()=>({rows:[row]})});process.stdout.write(JSON.stringify(out));});"
        )
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, input=json.dumps(row),
            text=True, capture_output=True, check=True,
        )
        validate(json.loads(result.stdout))

    def test_moneytree_adapter_output_matches_the_financial_record_schema(self):
        script = (
            "const m=require('./apps/life-manager/lib/moneytree-local-adapter.js');"
            "const at='2026-09-07T06:00:00.000Z';"
            "const [a]=m.normalizeAccounts({structuredContent:{data:{baseCurrency:'JPY',"
            "accountGroups:{banks:[{institutionKey:'bank',accounts:[{id:'a1',current_balance:5000}]}]}}}},at);"
            "process.stdout.write(JSON.stringify(m.accountToFinancialRecord(a,{subjectId:'user-1',recordedAt:at})));"
        )
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, text=True, capture_output=True, check=True,
        )
        validate(json.loads(result.stdout))

    def test_agent_economy_revenue_adapter_outputs_match_financial_schema(self):
        script = (
            "import('./skills/agent-economy/lib/revenue-receipt.mjs').then(async r=>{"
            "const a=await import('./skills/agent-economy/lib/financial-record-adapter.mjs');"
            "const receipt=r.normalizeRevenueReceipt({provider:'x402',payer:'buyer',recipient:'seller',"
            "gross:'1.25',fee:'0.05',refund:'0',asset:'USDC',terminal_state:'settled',"
            "occurred_at:'2026-09-07T01:00:00Z',proof:{chain_id:8453,tx_hash:'0x'+'a'.repeat(64),log_index:1,verified:true}});"
            "process.stdout.write(JSON.stringify(a.revenueReceiptToFinancialRecords(receipt,{subjectId:'tenant-1'})));});"
        )
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, text=True, capture_output=True, check=True,
        )
        for record in json.loads(result.stdout):
            validate(record)

    def test_runtime_event_schema_matches_runtime_vocabulary(self):
        definition = SCHEMA["$defs"]["RuntimeEvent"]["properties"]
        self.assertEqual(set(definition["domain"]["enum"]), runtime_event.DOMAINS)
        self.assertEqual(set(definition["phase"]["enum"]), runtime_event.PHASES)
        self.assertEqual(set(definition["status"]["enum"]), runtime_event.STATUSES)
        self.assertEqual(set(definition["effect_class"]["enum"]), runtime_event.EFFECTS)
        self.assertEqual(set(definition["effect_status"]["enum"]), runtime_event.EFFECT_STATUSES)
        event = {"version": 1, "event_id": "a" * 24, "timestamp": "2026-09-07T00:00:00Z", "loop_id": "example", "domain": "earn", "run_id": "run-1", "phase": "report", "status": "pass", "release_sha": "b" * 40, "provider": "deterministic", "profile_alias": None, "effect_class": "none", "effect_status": "not_applicable", "blocker": None, "evidence_refs": ["lm-loop://example/run-1/summary.json"]}
        validate(event)
        runtime_event.validate_runtime_event(event)
        event["evidence_refs"] = ["lm-loop://example/run-1/summary.json?query=1"]
        with self.assertRaises(AssertionError):
            validate(event)
        with self.assertRaises(ValueError):
            runtime_event.validate_runtime_event(event)
        long_scheme_ref = f"{'a' * 200}://example/run-1/summary.json"
        event["evidence_refs"] = [long_scheme_ref, long_scheme_ref]
        validate(event)
        runtime_event.validate_runtime_event(event)

    def test_verified_business_revenue_requires_evidence(self):
        key = "stripe:payment:1"
        record = {
            "schema_version": 1, "record_type": "financial_record", "record_id": financial_id("user-1", key),
            "subject_id": "user-1", "scope": "business", "kind": "business_revenue",
            "direction": "credit", "amount_minor": 12500, "currency": "JPY",
            "occurred_at": "2026-09-07T00:00:00Z", "recorded_at": "2026-09-07T00:01:00Z",
            "idempotency_key": key,
            "source": {"provider": "stripe", "source_type": "payment_processor", "external_ref": "payment-1"},
            "verification": {"status": "verified", "observed_at": "2026-09-07T00:01:00Z", "evidence_refs": ["stripe://payment/payment-1"]}
        }
        validate(record)
        record["verification"]["evidence_refs"] = []
        with self.assertRaises(AssertionError):
            validate(record)

    def test_personal_balance_cannot_be_booked_as_business_revenue(self):
        key = "moneytree:account:1:2026-09-07"
        record = {
            "schema_version": 1, "record_type": "financial_record", "record_id": financial_id("user-1", key),
            "subject_id": "user-1", "scope": "business", "kind": "business_revenue",
            "direction": "credit", "amount_minor": 500000, "currency": "JPY",
            "occurred_at": "2026-09-07T00:00:00Z", "recorded_at": "2026-09-07T00:01:00Z",
            "idempotency_key": key,
            "source": {"provider": "moneytree", "source_type": "moneytree", "external_ref": "account-1"},
            "verification": {"status": "stale", "observed_at": "2026-09-07T00:01:00Z", "evidence_refs": []}
        }
        with self.assertRaises(AssertionError):
            validate(record)

    def test_financial_kind_requires_its_direction(self):
        key = "cost:1"
        record = {"schema_version": 1, "record_type": "financial_record", "record_id": financial_id("user-1", key), "subject_id": "user-1", "scope": "business", "kind": "business_cost", "direction": "credit", "amount_minor": 500, "currency": "JPY", "occurred_at": "2026-09-07T00:00:00Z", "recorded_at": "2026-09-07T00:01:00Z", "idempotency_key": key, "source": {"provider": "stripe", "source_type": "payment_processor", "external_ref": "fee-1"}, "verification": {"status": "verified", "observed_at": "2026-09-07T00:01:00Z", "evidence_refs": ["stripe://fee/fee-1"]}}
        with self.assertRaises(AssertionError):
            validate(record)

    def test_effect_receipt_and_outbox_have_distinct_truth(self):
        job = {"schema_version": 1, "record_type": "job", "job_id": "j-1", "tenant_id": "user-1", "loop_id": "writer", "capability": "marketplace.apply", "effect_class": "application", "effect_key": "writer:1", "input_refs": {"opportunity_ref": "writer://job/1"}, "max_attempts": 3}
        effect = {"schema_version": 1, "record_type": "effect", "effect_id": "e-1", "loop_id": "writer", "run_id": "r-1", "effect_class": "application", "idempotency_key": "writer:1", "status": "started", "provider": "writer", "attempted_at": "2026-09-07T00:00:00Z", "evidence_refs": []}
        receipt = {"schema_version": 1, "record_type": "receipt", "receipt_id": "p-1", "effect_id": "e-1", "loop_id": "writer", "run_id": "r-1", "outcome": "verified", "provider": "writer", "recorded_at": "2026-09-07T00:01:00Z", "external_ref": "job-1", "payload_sha256": "a" * 64, "evidence_refs": ["writer://job/job-1"]}
        outbox = {"schema_version": 1, "record_type": "outbox_item", "message_key": "writer:report:1", "tenant_id": "user-1", "job_id": "j-1", "effect_id": "e-1", "loop_id": "writer", "status": "delivered", "attempt_count": 1, "payload_sha256": "b" * 64, "provider": "telegram", "created_at": "2026-09-07T00:01:00Z", "claimed_at": "2026-09-07T00:01:01Z", "delivered_at": "2026-09-07T00:01:02Z", "provider_message_ids": ["123"]}
        for value in (job, effect, receipt, outbox):
            validate(value)

    def test_verified_receipt_and_delivered_outbox_require_provider_proof(self):
        receipt = {"schema_version": 1, "record_type": "receipt", "receipt_id": "p-1", "effect_id": "e-1", "loop_id": "writer", "run_id": "r-1", "outcome": "verified", "provider": "writer", "recorded_at": "2026-09-07T00:01:00Z", "external_ref": None, "payload_sha256": "a" * 64, "evidence_refs": []}
        outbox = {"schema_version": 1, "record_type": "outbox_item", "message_key": "writer:1", "tenant_id": None, "job_id": None, "effect_id": None, "loop_id": "writer", "status": "delivered", "attempt_count": 0, "payload_sha256": "b" * 64, "provider": "telegram", "created_at": "2026-09-07T00:00:00Z", "claimed_at": None, "delivered_at": None, "provider_message_ids": []}
        for value in (receipt, outbox):
            with self.assertRaises(AssertionError):
                validate(value)

    def test_pending_outbox_keeps_attempt_count_after_safe_pre_send_retry(self):
        outbox = {"schema_version": 1, "record_type": "outbox_item", "message_key": "writer:1", "tenant_id": None, "job_id": None, "effect_id": None, "loop_id": "writer", "status": "pending", "attempt_count": 2, "payload_sha256": "b" * 64, "provider": "telegram", "created_at": "2026-09-07T00:00:00Z", "claimed_at": None, "delivered_at": None, "provider_message_ids": []}
        validate(outbox)

    def test_effectful_job_requires_effect_key(self):
        job = {"schema_version": 1, "record_type": "job", "job_id": "j-1", "tenant_id": "user-1", "loop_id": "writer", "capability": "marketplace.apply", "effect_class": "application", "effect_key": None, "input_refs": {"opportunity_ref": "writer://job/1"}, "max_attempts": 3}
        with self.assertRaises(AssertionError):
            validate(job)

    def test_connector_browser_target_lease_is_the_common_contract(self):
        lease = {
            "schema_version": 1,
            "owner_token": "connector-owner-token-0001",
            "generation": 1,
            "target_id": "TARGET_A",
            "page_websocket": "ws://[::1]:9222/devtools/page/TARGET_A",
            "canonical_url": "https://luma.com/tokyo-ai",
            "claimed_at": "2026-09-07T00:00:00Z",
            "heartbeat_at": "2026-09-07T00:00:10Z",
        }
        validate(lease)
        lease["page_websocket"] = "ws://[::1]:9222/devtools/browser/FOREIGN"
        with self.assertRaises(AssertionError):
            validate(lease)


if __name__ == "__main__":
    unittest.main()
