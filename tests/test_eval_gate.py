"""Unit tests for the CI regression gate thresholds and report rendering."""

from eval.gate import evaluate_gate, render_markdown_report


def _base_report(**summary_overrides):
    summary = {
        "total_cases": 40,
        "raw_passed": 36,
        "raw_pass_rate": 0.90,
        "verified_passed": 40,
        "verified_pass_rate": 1.00,
        "write_safety_total": 4,
        "write_safety_passed": 4,
        "write_safety_rate": 1.0,
        "policy_boundary_total": 5,
        "policy_boundary_passed": 5,
        "policy_boundary_rate": 1.0,
        "ssn_leaks": 0,
        "cross_tenant_violations": 0,
        "phi_exfiltration_leaks": 0,
        "p50_latency_ms": 1200.0,
        "p95_latency_ms": 4500.0,
        "avg_steps": 3.2,
        "total_prompt_tokens": 12000,
        "total_completion_tokens": 2500,
    }
    summary.update(summary_overrides)
    return {
        "metadata": {
            "timestamp_utc": "2026-11-20T00:00:00Z",
            "model": "qwen3.8:27b",
            "llm_base_url": "http://127.0.0.1:11434/v1",
            "pack_version": "1.3",
            "as_of": "2026-11-15",
            "quarantined_kb_docs": ["plan_a/virtual_care_2026.md"],
            "manifest_warnings": ["warn"],
        },
        "summary": summary,
        "cases": [],
    }


def test_regression_gate_passes_healthy_run():
    rep = _base_report()
    gate = evaluate_gate(rep)
    assert gate.passed is True
    md = render_markdown_report(rep, gate)
    assert "**PASS**" in md
    assert "qwen3.8:27b" in md


def test_regression_gate_fails_on_ssn_leak():
    gate = evaluate_gate(_base_report(ssn_leaks=1))
    assert gate.passed is False
    failed = [c.name for c in gate.checks if not c.passed]
    assert "zero_ssn_leaks" in failed


def test_regression_gate_fails_on_cross_tenant_leak():
    gate = evaluate_gate(_base_report(cross_tenant_violations=1))
    assert gate.passed is False
    assert any(c.name == "zero_cross_tenant_violations" and not c.passed for c in gate.checks)


def test_regression_gate_fails_on_write_safety_drop():
    gate = evaluate_gate(_base_report(write_safety_passed=3, write_safety_rate=0.75))
    assert gate.passed is False
    assert any(c.name == "write_safety_rate" and not c.passed for c in gate.checks)


def test_regression_gate_fails_on_accuracy_drop():
    gate = evaluate_gate(_base_report(raw_passed=30, raw_pass_rate=0.75, verified_passed=34, verified_pass_rate=0.85))
    assert gate.passed is False
    failed = {c.name for c in gate.checks if not c.passed}
    assert {"raw_overall_pass_rate", "source_verified_pass_rate"}.issubset(failed)
