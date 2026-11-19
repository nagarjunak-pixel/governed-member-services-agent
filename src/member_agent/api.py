"""FastAPI HTTP service exposing multi-turn rep conversations and PHI-redacted traces."""

from __future__ import annotations

import argparse
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from member_agent.agent import AgentSession, MemberServicesAgent
from member_agent.config import Settings


class CreateSessionRequest(BaseModel):
    tenant: str = Field(..., examples=["plan_a"])
    member_id: str = Field(..., examples=["M749817"])


class CreateSessionResponse(BaseModel):
    session_id: str
    tenant: str
    member_id: str
    as_of_date: str


class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)


class SendMessageResponse(BaseModel):
    session_id: str
    turn: int
    reply: str
    pending_grievance: bool
    summary: dict[str, Any]


def create_app(agent: MemberServicesAgent | None = None) -> FastAPI:
    svc = agent or MemberServicesAgent(Settings.from_env())
    sessions: dict[str, AgentSession] = {}

    app = FastAPI(
        title="Governed Member-Services Agent API",
        version="0.1.0",
        description="Multi-tenant healthcare rep assistant with centralized ToolGateway governance.",
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "as_of_date": svc.as_of_date,
            "model": svc.settings.llm_model,
            "quarantined_kb_docs": [d.relative_path for d in svc.kb.quarantined_documents],
            "manifest_warnings": svc.gateway.manifest_warnings,
        }

    @app.post("/sessions", response_model=CreateSessionResponse, status_code=201)
    def create_session(req: CreateSessionRequest) -> CreateSessionResponse:
        if req.tenant not in svc.settings.tenant_tokens:
            raise HTTPException(status_code=400, detail=f"Unknown or unconfigured tenant '{req.tenant}'.")
        sess = svc.create_session(req.tenant, req.member_id)
        sessions[sess.ctx.session_id] = sess
        return CreateSessionResponse(
            session_id=sess.ctx.session_id,
            tenant=req.tenant,
            member_id=req.member_id,
            as_of_date=svc.as_of_date,
        )

    @app.post("/sessions/{session_id}/messages", response_model=SendMessageResponse)
    def send_message(session_id: str, req: SendMessageRequest) -> SendMessageResponse:
        sess = sessions.get(session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
        reply = sess.send(req.message)
        trace_dict = sess.ctx.trace.to_dict()
        return SendMessageResponse(
            session_id=session_id,
            turn=sess.ctx.current_turn,
            reply=reply,
            pending_grievance=bool(sess.ctx.pending_grievance and not sess.ctx.pending_grievance.filed),
            summary=trace_dict["summary"],
        )

    @app.get("/sessions/{session_id}/trace")
    def get_trace(session_id: str) -> dict[str, Any]:
        sess = sessions.get(session_id)
        if sess is None:
            raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found.")
        return sess.ctx.trace.to_dict()

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the Governed Member-Services Agent HTTP API")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--pack-dir", default=None)
    ap.add_argument("--mock-url", default=None)
    ap.add_argument("--llm-base-url", default=None)
    ap.add_argument("--model", default=None)
    args = ap.parse_args()

    settings = Settings.from_env(
        pack_dir=args.pack_dir,
        mock_api_base_url=args.mock_url,
        llm_base_url=args.llm_base_url,
        llm_model=args.model,
    )
    app = create_app(MemberServicesAgent(settings))
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
