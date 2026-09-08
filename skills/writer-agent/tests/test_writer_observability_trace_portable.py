import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "scripts" / "writer_observability_trace.py"


def test_trace_builder_has_no_undeclared_opentelemetry_runtime_dependency() -> None:
    source = TRACE.read_text(encoding="utf-8")
    assert "opentelemetry" not in source


def test_stdlib_trace_ids_preserve_one_trace_and_unique_spans(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("writer_observability_trace", TRACE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    run = tmp_path / "run-portable"
    gates = run / "gates"
    gates.mkdir(parents=True)
    (gates / "generation-state.json").write_text(
        json.dumps({"run_id": run.name, "status": "provider-returned"})
    )
    (gates / "quality-self-heal.json").write_text(
        json.dumps({"run_id": run.name, "action": "ready_to_freeze"})
    )

    trace = module.build_trace(run, "2026-09-09T00:00:00Z")
    trace_ids = {span["trace_id"] for span in trace["spans"]}
    span_ids = [span["span_id"] for span in trace["spans"]]
    assert len(trace_ids) == 1
    assert all(len(value) == 32 and int(value, 16) >= 0 for value in trace_ids)
    assert len(span_ids) == len(set(span_ids))
    assert all(len(value) == 16 and int(value, 16) >= 0 for value in span_ids)
