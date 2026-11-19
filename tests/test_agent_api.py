"""Integration tests for the multi-turn agent loop and FastAPI HTTP endpoints."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from member_agent.agent import MemberServicesAgent
from member_agent.api import create_app
from member_agent.api_client import MemberServicesClient
from member_agent.config import Settings
from member_agent.kb import TenantKnowledgeBase
from member_agent.llm import OpenAICompatibleLLM


def test_multi_turn_grievance_flow_via_http_api(tmp_path: Path):
    posted: list[dict] = []

    def api_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json={"status": "ok", "clock": "2026-11-15"})
        if request.method == "POST" and path.endswith("/grievances"):
            rec = {"grievance_id": "G26-700101", "status": "RECEIVED", **json.loads(request.content)}
            posted.append(rec)
            return httpx.Response(201, json=rec)
        if path.endswith("/members/M704649"):
            return httpx.Response(
                200,
                json={
                    "member_id": "M704649",
                    "ssn": "123-45-6789",
                    "product_code": "SHMO",
                    "plan_name": "Harborview Silver HMO",
                    "network_id": "HVH-HMO",
                    "address": {"zip": "43206", "state": "OH", "line1": "99 Secret Rd"},
                },
            )
        return httpx.Response(404)

    # Scripted LLM responses simulating a 2-turn propose -> confirm workflow
    llm_step = 0
    captured_token = {"token": ""}

    def llm_handler(request: httpx.Request) -> httpx.Response:
        nonlocal llm_step
        llm_step += 1
        body = json.loads(request.content)
        msgs = body["messages"]

        # Capture confirmation_token from previous tool message
        for m in msgs:
            if m.get("role") == "tool" and m.get("name") == "propose_grievance":
                data = json.loads(m["content"])
                if data.get("confirmation_token"):
                    captured_token["token"] = data["confirmation_token"]

        if llm_step == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "tc_1",
                                        "type": "function",
                                        "function": {
                                            "name": "propose_grievance",
                                            "arguments": json.dumps(
                                                {
                                                    "category": "billing",
                                                    "description": "Provider billed paid visit.",
                                                    "expedite_requested": False,
                                                }
                                            ),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 30},
                },
            )
        if llm_step == 2:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "Please confirm: file billing grievance 'Provider billed paid visit.'?",
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 150, "completion_tokens": 20},
                },
            )
        if llm_step == 3:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "finish_reason": "tool_calls",
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "tc_2",
                                        "type": "function",
                                        "function": {
                                            "name": "confirm_and_file_grievance",
                                            "arguments": json.dumps({"confirmation_token": captured_token["token"]}),
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 180, "completion_tokens": 20},
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Filed billing grievance G26-700101."},
                    }
                ],
                "usage": {"prompt_tokens": 200, "completion_tokens": 15},
            },
        )

    settings = Settings(pack_dir=tmp_path, tenant_tokens={"plan_a": "tok_a"}, directory_api_key="dir_k")
    api_client = MemberServicesClient(
        base_url="http://mock.local",
        tenant_tokens={"plan_a": "tok_a"},
        directory_api_key="dir_k",
        transport=httpx.MockTransport(api_handler),
    )
    llm = OpenAICompatibleLLM(
        base_url="http://llm.local/v1",
        api_key="test",
        model="mock-llm",
        transport=httpx.MockTransport(llm_handler),
    )
    agent = MemberServicesAgent(settings, api_client=api_client, kb=TenantKnowledgeBase(tmp_path / "kb"), llm=llm)
    client = TestClient(create_app(agent))

    assert client.get("/health").status_code == 200
    s_resp = client.post("/sessions", json={"tenant": "plan_a", "member_id": "M704649"})
    assert s_resp.status_code == 201
    sid = s_resp.json()["session_id"]

    # Turn 1: stage only, zero writes
    t1 = client.post(f"/sessions/{sid}/messages", json={"message": "File a billing grievance for this visit."})
    assert t1.status_code == 200
    assert t1.json()["pending_grievance"] is True
    assert len(posted) == 0

    # Turn 2: confirm -> writes once
    t2 = client.post(f"/sessions/{sid}/messages", json={"message": "Yes, confirmed. Go ahead and file it."})
    assert t2.status_code == 200
    assert t2.json()["pending_grievance"] is False
    assert "G26-700101" in t2.json()["reply"]
    assert len(posted) == 1

    # Trace endpoint returns redacted audit log
    trace = client.get(f"/sessions/{sid}/trace").json()
    assert trace["tenant"] == "plan_a"
    assert "123-45-6789" not in json.dumps(trace)
    assert "99 Secret Rd" not in json.dumps(trace)
    assert any(e["event_type"] == "gateway_decision" and e["payload"]["tool"] == "confirm_and_file_grievance" for e in trace["events"])
