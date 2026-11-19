"""Unit tests for grievance retry idempotency, claims 429 retry, and tenant auth binding."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from member_agent.api_client import APIError, MemberServicesClient, TenantAuthError


def test_grievance_503_after_write_retries_idempotently_and_creates_exactly_once():
    """Simulates the mock server's _ack_slow behavior: writes on attempt 1, returns 503,
    then replays on attempt 2 when the same Idempotency-Key + body bytes are sent."""
    grievances: list[dict] = []
    seen_keys: dict[str, tuple[str, dict]] = {}
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.headers["Authorization"] == "Bearer tok_a"
        ikey = request.headers["Idempotency-Key"]
        fp = hashlib.sha256(request.content).hexdigest()
        if ikey in seen_keys:
            prior_fp, prior_rec = seen_keys[ikey]
            if prior_fp != fp:
                return httpx.Response(422, json={"error": "idempotency_mismatch", "message": "Body mismatch"})
            return httpx.Response(200, json=prior_rec, headers={"Idempotent-Replayed": "true"})
        body = json.loads(request.content)
        rec = {"grievance_id": f"G26-70010{len(grievances)+1}", "status": "RECEIVED", **body}
        grievances.append(rec)
        seen_keys[ikey] = (fp, rec)
        # First attempt commits then times out with 503
        return httpx.Response(503, json={"error": "service_unavailable", "message": "Ack timed out"})

    client = MemberServicesClient(
        base_url="http://mock.local",
        tenant_tokens={"plan_a": "tok_a"},
        directory_api_key="dir_k",
        transport=httpx.MockTransport(handler),
        sleep_fn=lambda _: None,
    )
    res = client.create_grievance(
        "plan_a",
        "M704649",
        category="billing",
        description="Provider balance billed paid office visit.",
        expedite_requested=False,
        idempotency_key="idem-test-001",
    )
    assert calls == 2
    assert len(grievances) == 1
    assert res.attempts == 2
    assert res.status_code == 200
    assert res.idempotent_replayed is True
    assert res.grievance["grievance_id"] == "G26-700101"


def test_grievance_idempotency_key_mismatch_raises_422():
    seen: dict[str, tuple[str, dict]] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        ikey = request.headers["Idempotency-Key"]
        fp = hashlib.sha256(request.content).hexdigest()
        if ikey in seen and seen[ikey][0] != fp:
            return httpx.Response(422, json={"error": "idempotency_mismatch", "message": "Different body"})
        rec = {"grievance_id": "G26-700101", "status": "RECEIVED"}
        seen[ikey] = (fp, rec)
        return httpx.Response(201, json=rec)

    client = MemberServicesClient(
        base_url="http://mock.local",
        tenant_tokens={"plan_a": "tok_a"},
        directory_api_key="dir_k",
        transport=httpx.MockTransport(handler),
        sleep_fn=lambda _: None,
    )
    client.create_grievance(
        "plan_a",
        "M1",
        category="billing",
        description="First description",
        expedite_requested=False,
        idempotency_key="same-key",
    )
    with pytest.raises(APIError) as exc_info:
        client.create_grievance(
            "plan_a",
            "M1",
            category="billing",
            description="Different description",
            expedite_requested=False,
            idempotency_key="same-key",
        )
    assert exc_info.value.status_code == 422


def test_claims_pagination_and_429_retry():
    """Follows pagination cursor and retries transparently on 429 Retry-After."""
    sleep_calls: list[float] = []
    request_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 2:
            return httpx.Response(429, json={"error": "rate_limited"}, headers={"Retry-After": "1"})
        cursor = int(request.url.params.get("cursor", "0"))
        if cursor == 0:
            return httpx.Response(
                200,
                json={
                    "data": [{"claim_id": "C1"}, {"claim_id": "C2"}],
                    "page_size": 2,
                    "next": "/tenants/plan_a/members/M1/claims?cursor=2&page_size=2",
                },
            )
        return httpx.Response(200, json={"data": [{"claim_id": "C3"}], "page_size": 2, "next": None})

    client = MemberServicesClient(
        base_url="http://mock.local",
        tenant_tokens={"plan_a": "tok_a"},
        directory_api_key="dir_k",
        transport=httpx.MockTransport(handler),
        sleep_fn=lambda s: sleep_calls.append(s),
    )
    claims = client.list_claims("plan_a", "M1")
    assert [c["claim_id"] for c in claims] == ["C1", "C2", "C3"]
    assert request_count == 3
    assert sleep_calls == [1.0]


def test_unknown_tenant_raises_tenant_auth_error():
    client = MemberServicesClient(
        base_url="http://mock.local",
        tenant_tokens={"plan_a": "tok_a"},
        directory_api_key="dir_k",
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})),
    )
    with pytest.raises(TenantAuthError):
        client.get_member("plan_b", "M1")
