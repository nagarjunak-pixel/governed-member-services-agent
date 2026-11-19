"""Centralized Tool Gateway — single enforcement choke point for all agent tool calls.

Enforces:
1. Strict tenant and member isolation (immutable session binding; rejects any override attempts;
   verifies claim ownership before returning claim details).
2. PHI minimisation (never returns raw SSN/DOB/email/phone/street to the model; dedicated boolean
   verify_ssn_last4; blocks PHI in outbound third-party provider-directory queries).
3. Two-phase human confirmation before any write (propose_grievance -> separate rep turn with
   explicit affirmative confirmation -> confirm_and_file_grievance with deterministic Idempotency-Key).
4. Per-conversation budgets for steps, cost, and latency.
5. Full PHI-redacted audit trace of every gateway decision.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from member_agent.api_client import APIError, MemberServicesClient
from member_agent.config import BudgetConfig
from member_agent.kb import TenantKnowledgeBase
from member_agent.redaction import (
    detect_phi_in_outbound_text,
    minimise_claim_record,
    minimise_member_record,
)
from member_agent.trace import ConversationTrace

GRIEVANCE_CATEGORIES = frozenset({"billing", "quality_of_care", "access", "customer_service", "other"})

NEGATION_RE = re.compile(
    r"\b(?:hold\s+off|cancel|don't|do\s+not|wait|stop|never\s*mind|not\s+yet|abort|decline|skip|no\b)",
    re.IGNORECASE,
)
AFFIRMATION_RE = re.compile(
    r"\b(?:yes|confirm|confirmed|go\s+ahead|proceed|file\s+it|submit\s+it|agreed|agrees|approved)\b",
    re.IGNORECASE,
)


def is_explicit_rep_confirmation(rep_message: str) -> bool:
    """Verify the rep's latest turn explicitly confirms filing and does not defer or cancel."""
    text = (rep_message or "").strip()
    if not text:
        return False
    if NEGATION_RE.search(text):
        return False
    return bool(AFFIRMATION_RE.search(text))


def audit_third_party_manifest(manifest_path: Path | None) -> list[str]:
    """Inspect a third-party MCP tool manifest for suspicious PHI-exfiltration instructions.

    We never pass untrusted vendor descriptions verbatim to the model.
    """
    warnings: list[str] = []
    if not manifest_path or not manifest_path.is_file():
        return warnings
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return warnings
    for tool in manifest.get("tools", []):
        desc = str(tool.get("description") or "")
        lower = desc.lower()
        if any(tok in lower for tok in ("social security", "ssn", "date of birth", "do not mention", "does not need to be mentioned")):
            warnings.append(
                f"Quarantined unsafe description on third-party tool '{tool.get('name')}': "
                "contains PHI exfiltration or stealth instructions."
            )
    return warnings


@dataclass
class PendingGrievance:
    confirmation_token: str
    idempotency_key: str
    category: str
    description: str
    expedite_requested: bool
    proposed_at_turn: int
    filed: bool = False
    filed_grievance_id: str | None = None


@dataclass
class SessionContext:
    session_id: str
    tenant: str
    member_id: str
    as_of_date: str
    trace: ConversationTrace
    current_turn: int = 0
    current_rep_message: str = ""
    pending_grievance: PendingGrievance | None = None
    # Strictly scoped to this single (tenant, member_id) session
    _cache: dict[str, Any] = field(default_factory=dict)


# Curated, gateway-owned tool schemas (never raw untrusted vendor descriptions)
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_member_and_benefits",
            "description": (
                "Fetch the current member's minimised enrollment profile (product_code, plan_name, "
                "network_id, coverage status/dates, home ZIP) and current plan-year accumulators. "
                "Always call this first when answering benefit, accumulator, coverage, or provider questions."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_ssn_last4",
            "description": (
                "Verify whether a 4-digit SSN suffix provided by the caller matches the member on file. "
                "Returns only {match: true|false}; never exposes the SSN."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "last4": {
                        "type": "string",
                        "description": "Exactly 4 digits provided by the caller.",
                    }
                },
                "required": ["last4"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_plan_documents",
            "description": (
                "Search the current tenant's plan benefit summaries, member services guide, and claim "
                "denial code reference. Automatically scoped to the member's enrolled product and active "
                "plan year unless plan_year is explicitly specified (e.g. 2027 for renewal questions)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query (e.g. 'specialist copay', 'appeal window', 'CO-197')."},
                    "plan_year": {
                        "type": "integer",
                        "description": "Optional plan year (e.g. 2026 or 2027). Defaults to the active plan year.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_claims",
            "description": (
                "Retrieve the member's claims (automatically handles pagination and retries). "
                "Optional filters: status (PAID, DENIED, PENDED, IN_PROCESS), service_date (YYYY-MM-DD), or year."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "Optional status filter: PAID, DENIED, PENDED, IN_PROCESS."},
                    "service_date": {"type": "string", "description": "Optional exact service date YYYY-MM-DD."},
                    "year": {"type": "integer", "description": "Optional service year, e.g. 2026."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_claim_by_id",
            "description": "Fetch details of one specific claim by claim_id for the current member.",
            "parameters": {
                "type": "object",
                "properties": {
                    "claim_id": {"type": "string", "description": "Claim identifier, e.g. C2639555547."}
                },
                "required": ["claim_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_grievances",
            "description": "List grievances already on file for the current member.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_providers",
            "description": (
                "Search the CareFinder provider directory for in-network clinicians near the member. "
                "Automatically restricts to the member's network_id and defaults to the member's home ZIP. "
                "NEVER include any member PHI (SSN, DOB, name, email, phone) in the query."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "specialty": {"type": "string", "description": "Clinical specialty, e.g. Dermatology."},
                    "zip_code": {"type": "string", "description": "Optional 5-digit ZIP code. Defaults to member home ZIP."},
                    "query": {"type": "string", "description": "Optional non-PHI keywords (name/practice/specialty)."},
                    "limit": {"type": "integer", "description": "Max results (1-20)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_grievance",
            "description": (
                "Stage a grievance draft for explicit rep confirmation. Does NOT submit the grievance yet. "
                "After calling this, read the summary back to the rep and wait for their confirmation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["billing", "quality_of_care", "access", "customer_service", "other"],
                    },
                    "description": {"type": "string", "description": "Concise factual summary (max 2000 chars)."},
                    "expedite_requested": {"type": "boolean", "description": "Whether expedited clinical review is requested."},
                },
                "required": ["category", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "confirm_and_file_grievance",
            "description": (
                "File a previously staged grievance ONLY after the rep explicitly confirmed in a subsequent turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "confirmation_token": {"type": "string", "description": "Token returned by propose_grievance."}
                },
                "required": ["confirmation_token"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_pending_grievance",
            "description": "Cancel a staged grievance when the rep asks to hold off or not file.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_deadline",
            "description": (
                "Compute exact calendar deadline by adding `days` to `start_date` (YYYY-MM-DD) "
                "and compare it against today's environment clock date."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "Start date in YYYY-MM-DD format."},
                    "days": {"type": "integer", "description": "Calendar days to add (e.g. 180, 30, 45)."},
                },
                "required": ["start_date", "days"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate_remaining",
            "description": (
                "Compute exact remaining amount or visits (`limit - used`, floored at 0) using exact decimal math."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "number", "description": "Plan limit (e.g. deductible, OOP max, or visit limit)."},
                    "used": {"type": "number", "description": "Amount or visits already used."},
                },
                "required": ["limit", "used"],
            },
        },
    },
]


class ToolGateway:
    """Single governance choke point for every tool invocation."""

    def __init__(
        self,
        *,
        api_client: MemberServicesClient,
        kb: TenantKnowledgeBase,
        budgets: BudgetConfig,
        manifest_path: Path | None = None,
    ) -> None:
        self.api_client = api_client
        self.kb = kb
        self.budgets = budgets
        self.manifest_warnings = audit_third_party_manifest(manifest_path)

    def tool_schemas(self) -> list[dict[str, Any]]:
        return TOOL_SCHEMAS

    def check_budget(self, session: SessionContext) -> tuple[bool, str]:
        if session.trace.steps_used >= self.budgets.max_steps:
            return False, f"Step budget exceeded ({session.trace.steps_used}/{self.budgets.max_steps})."
        if session.trace.estimated_cost_usd > self.budgets.max_cost_usd:
            return (
                False,
                f"Cost budget exceeded (${session.trace.estimated_cost_usd:.5f}/${self.budgets.max_cost_usd:.5f}).",
            )
        elapsed = session.trace.elapsed_ms()
        if elapsed > self.budgets.max_latency_ms:
            return False, f"Latency budget exceeded ({elapsed:.0f}ms/{self.budgets.max_latency_ms}ms)."
        return True, "ok"

    def execute(self, session: SessionContext, tool_name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        args = dict(arguments or {})
        t0 = time.monotonic()

        # 1. Reject any attempt to override tenant or member_id via tool arguments
        if "tenant" in args or "member_id" in args:
            session.trace.record_gateway_decision(
                turn_index=session.current_turn,
                tool_name=tool_name,
                decision="BLOCKED_TENANT",
                reason="Tool arguments cannot specify or override tenant/member_id; session binding is immutable.",
                arguments=args,
            )
            return {
                "error": "blocked_tenant_override",
                "message": "Cross-tenant or cross-member overrides are prohibited by the gateway.",
            }

        # 2. Enforce step, cost, and latency budgets
        ok, budget_msg = self.check_budget(session)
        if not ok:
            session.trace.record_gateway_decision(
                turn_index=session.current_turn,
                tool_name=tool_name,
                decision="BLOCKED_BUDGET",
                reason=budget_msg,
                arguments=args,
            )
            return {"error": "budget_exceeded", "message": budget_msg}

        dispatch = {
            "get_member_and_benefits": self._get_member_and_benefits,
            "verify_ssn_last4": self._verify_ssn_last4,
            "search_plan_documents": self._search_plan_documents,
            "list_claims": self._list_claims,
            "get_claim_by_id": self._get_claim_by_id,
            "list_grievances": self._list_grievances,
            "search_providers": self._search_providers,
            "propose_grievance": self._propose_grievance,
            "confirm_and_file_grievance": self._confirm_and_file_grievance,
            "cancel_pending_grievance": self._cancel_pending_grievance,
            "calculate_deadline": self._calculate_deadline,
            "calculate_remaining": self._calculate_remaining,
        }

        handler = dispatch.get(tool_name)
        if handler is None:
            session.trace.record_gateway_decision(
                turn_index=session.current_turn,
                tool_name=tool_name,
                decision="DENY",
                reason=f"Unknown tool '{tool_name}'.",
                arguments=args,
            )
            return {"error": "unknown_tool", "message": f"Tool '{tool_name}' is not registered."}

        try:
            result, decision, reason, cache_hit, extra = handler(session, args)
        except APIError as exc:
            latency_ms = (time.monotonic() - t0) * 1000.0
            session.trace.record_gateway_decision(
                turn_index=session.current_turn,
                tool_name=tool_name,
                decision="ERROR",
                reason=str(exc),
                arguments=args,
                latency_ms=latency_ms,
                upstream_status=exc.status_code,
            )
            return {"error": "upstream_api_error", "message": str(exc)}

        latency_ms = (time.monotonic() - t0) * 1000.0
        session.trace.record_gateway_decision(
            turn_index=session.current_turn,
            tool_name=tool_name,
            decision=decision,
            reason=reason,
            arguments=args,
            latency_ms=latency_ms,
            cache_hit=cache_hit,
            extra=extra,
        )
        return result

    # ---- internal helpers ------------------------------------------------
    def _load_raw_member(self, session: SessionContext) -> tuple[dict[str, Any] | None, bool]:
        cache_key = f"{session.tenant}:{session.member_id}:raw_member"
        if cache_key in session._cache:
            return session._cache[cache_key], True
        raw = self.api_client.get_member(session.tenant, session.member_id)
        session._cache[cache_key] = raw
        return raw, False

    def _load_benefits(self, session: SessionContext) -> tuple[dict[str, Any] | None, bool]:
        cache_key = f"{session.tenant}:{session.member_id}:benefits"
        if cache_key in session._cache:
            return session._cache[cache_key], True
        raw = self.api_client.get_benefits(session.tenant, session.member_id)
        session._cache[cache_key] = raw
        return raw, False

    def _load_all_claims(self, session: SessionContext) -> tuple[list[dict[str, Any]], bool]:
        cache_key = f"{session.tenant}:{session.member_id}:claims"
        if cache_key in session._cache:
            return session._cache[cache_key], True
        raw = self.api_client.list_claims(session.tenant, session.member_id)
        session._cache[cache_key] = raw
        return raw, False

    # ---- tool implementations --------------------------------------------
    def _get_member_and_benefits(
        self, session: SessionContext, _args: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str, bool, dict[str, Any] | None]:
        raw_member, hit_m = self._load_raw_member(session)
        if raw_member is None:
            return (
                {
                    "found": False,
                    "tenant": session.tenant,
                    "member_id": session.member_id,
                    "message": f"Member '{session.member_id}' was not found under tenant '{session.tenant}'.",
                },
                "ALLOW",
                "Member not found in bound tenant.",
                hit_m,
                {"found": False},
            )
        benefits, hit_b = self._load_benefits(session)
        minimised = minimise_member_record(raw_member)
        return (
            {
                "found": True,
                "as_of_date": session.as_of_date,
                "member": minimised,
                "benefits": benefits,
            },
            "ALLOW",
            "Returned PHI-minimised member profile and benefits.",
            hit_m and hit_b,
            {"found": True, "product_code": minimised.get("product_code")},
        )

    def _verify_ssn_last4(
        self, session: SessionContext, args: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str, bool, dict[str, Any] | None]:
        last4 = re.sub(r"\D", "", str(args.get("last4") or ""))
        if len(last4) != 4:
            return (
                {"error": "invalid_input", "message": "Provide exactly 4 digits for last4."},
                "DENY",
                "Invalid last4 format.",
                False,
                None,
            )
        raw_member, hit = self._load_raw_member(session)
        if raw_member is None:
            return (
                {"found": False, "match": False, "message": "Member not found in this tenant."},
                "ALLOW",
                "Member not found in bound tenant.",
                hit,
                None,
            )
        actual_ssn = re.sub(r"\D", "", str(raw_member.get("ssn") or ""))
        matched = len(actual_ssn) >= 4 and actual_ssn[-4:] == last4
        return (
            {"found": True, "match": matched},
            "ALLOW",
            "Verified SSN last-4 inside gateway without exposing SSN to model.",
            hit,
            {"match": matched},
        )

    def _search_plan_documents(
        self, session: SessionContext, args: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str, bool, dict[str, Any] | None]:
        query = str(args.get("query") or "").strip()
        if not query:
            return ({"error": "invalid_input", "message": "query is required."}, "DENY", "Empty KB query.", False, None)

        raw_member, _ = self._load_raw_member(session)
        product_code = raw_member.get("product_code") if raw_member else None
        plan_year_raw = args.get("plan_year")
        plan_year = int(plan_year_raw) if plan_year_raw is not None else int(session.as_of_date[:4])

        docs = self.kb.retrieve(
            tenant=session.tenant,
            query=query,
            product_code=product_code,
            plan_year=plan_year,
            top_k=3,
        )
        return (
            {
                "tenant": session.tenant,
                "product_code": product_code,
                "plan_year": plan_year,
                "documents": docs,
            },
            "ALLOW",
            f"Retrieved {len(docs)} tenant-isolated KB document(s).",
            False,
            {"doc_ids": [d["doc_id"] for d in docs]},
        )

    def _list_claims(
        self, session: SessionContext, args: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str, bool, dict[str, Any] | None]:
        raw_member, _ = self._load_raw_member(session)
        if raw_member is None:
            return (
                {"found": False, "message": f"Member '{session.member_id}' not found in tenant '{session.tenant}'."},
                "ALLOW",
                "Member not found in bound tenant.",
                False,
                None,
            )
        all_claims, hit = self._load_all_claims(session)
        status_filter = str(args.get("status") or "").strip().upper() or None
        date_filter = str(args.get("service_date") or "").strip() or None
        year_filter = str(args.get("year")) if args.get("year") is not None else None

        counts_by_status: dict[str, int] = {}
        filtered: list[dict[str, Any]] = []
        for c in all_claims:
            st = str(c.get("status") or "UNKNOWN").upper()
            if year_filter and not str(c.get("service_date") or "").startswith(year_filter):
                continue
            counts_by_status[st] = counts_by_status.get(st, 0) + 1
            if status_filter and st != status_filter:
                continue
            if date_filter and c.get("service_date") != date_filter:
                continue
            filtered.append(minimise_claim_record(c))

        return (
            {
                "total_claims_for_member": len(all_claims),
                "counts_by_status": counts_by_status,
                "filtered_count": len(filtered),
                "claims": filtered,
            },
            "ALLOW",
            f"Returned {len(filtered)} minimised claim(s).",
            hit,
            {"total_claims": len(all_claims), "filtered_count": len(filtered)},
        )

    def _get_claim_by_id(
        self, session: SessionContext, args: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str, bool, dict[str, Any] | None]:
        claim_id = str(args.get("claim_id") or "").strip()
        if not claim_id:
            return ({"error": "invalid_input", "message": "claim_id is required."}, "DENY", "Missing claim_id.", False, None)

        raw_member, _ = self._load_raw_member(session)
        if raw_member is None:
            return (
                {"found": False, "message": f"Member '{session.member_id}' not found in tenant '{session.tenant}'."},
                "ALLOW",
                "Member not found in bound tenant.",
                False,
                None,
            )

        cache_key = f"{session.tenant}:{session.member_id}:claim:{claim_id}"
        if cache_key in session._cache:
            return session._cache[cache_key], "ALLOW", "Returned cached claim.", True, None

        raw_claim = self.api_client.get_claim(session.tenant, claim_id)
        if raw_claim is None:
            return (
                {"found": False, "claim_id": claim_id, "message": f"Claim '{claim_id}' not found."},
                "ALLOW",
                "Claim not found.",
                False,
                None,
            )

        # Prevent horizontal cross-member access within the same tenant
        if raw_claim.get("member_id") != session.member_id:
            return (
                {"found": False, "error": "member_mismatch", "message": "Claim does not belong to the active member."},
                "BLOCKED_TENANT",
                "Claim belongs to a different member.",
                False,
                None,
            )

        result = {"found": True, "claim": minimise_claim_record(raw_claim)}
        session._cache[cache_key] = result
        return result, "ALLOW", "Returned minimised claim detail.", False, {"claim_status": raw_claim.get("status")}

    def _list_grievances(
        self, session: SessionContext, _args: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str, bool, dict[str, Any] | None]:
        raw_member, _ = self._load_raw_member(session)
        if raw_member is None:
            return (
                {"found": False, "message": f"Member '{session.member_id}' not found in tenant '{session.tenant}'."},
                "ALLOW",
                "Member not found in bound tenant.",
                False,
                None,
            )
        items = self.api_client.list_grievances(session.tenant, session.member_id)
        return (
            {"as_of_date": session.as_of_date, "count": len(items), "grievances": items},
            "ALLOW",
            f"Returned {len(items)} grievance(s).",
            False,
            {"count": len(items)},
        )

    def _search_providers(
        self, session: SessionContext, args: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str, bool, dict[str, Any] | None]:
        # Defense-in-depth against poisoned third-party tool manifests: block PHI in ALL outbound fields
        combined = " ".join(str(v) for v in args.values() if v is not None)
        phi_hits = detect_phi_in_outbound_text(combined)
        if phi_hits:
            return (
                {
                    "error": "blocked_phi_exfiltration",
                    "message": (
                        f"Blocked outbound third-party provider search containing PHI ({', '.join(phi_hits)}). "
                        "Retry with specialty/ZIP/network only and no member identifiers."
                    ),
                },
                "BLOCKED_PHI",
                f"Blocked PHI ({', '.join(phi_hits)}) from leaving boundary to third-party directory.",
                False,
                {"phi_hits": phi_hits},
            )

        raw_member, _ = self._load_raw_member(session)
        if raw_member is None:
            return (
                {"found": False, "message": f"Member '{session.member_id}' not found in tenant '{session.tenant}'."},
                "ALLOW",
                "Member not found in bound tenant.",
                False,
                None,
            )

        network_id = raw_member.get("network_id")
        zip_code = str(args.get("zip_code") or "").strip() or (raw_member.get("address") or {}).get("zip")
        specialty = str(args.get("specialty") or "").strip() or None
        query = str(args.get("query") or "").strip() or None
        limit = int(args.get("limit") or 10)

        data = self.api_client.search_providers(
            query=query,
            specialty=specialty,
            zip_code=zip_code,
            network_id=network_id,
            limit=limit,
        )
        return (
            {
                "network_id": network_id,
                "zip_used": zip_code,
                "count": data.get("count", 0),
                "results": data.get("results", []),
            },
            "ALLOW",
            "Executed PHI-free third-party provider search.",
            False,
            {"network_id": network_id, "count": data.get("count", 0)},
        )

    def _propose_grievance(
        self, session: SessionContext, args: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str, bool, dict[str, Any] | None]:
        raw_member, _ = self._load_raw_member(session)
        if raw_member is None:
            return (
                {"found": False, "message": f"Member '{session.member_id}' not found in tenant '{session.tenant}'."},
                "DENY",
                "Cannot stage grievance for unknown member.",
                False,
                None,
            )

        category = str(args.get("category") or "").strip().lower()
        description = str(args.get("description") or "").strip()
        expedite = bool(args.get("expedite_requested", False))

        if category not in GRIEVANCE_CATEGORIES:
            return (
                {"error": "invalid_category", "message": f"Category must be one of {sorted(GRIEVANCE_CATEGORIES)}."},
                "DENY",
                "Invalid grievance category.",
                False,
                None,
            )
        if not description or len(description) > 2000:
            return (
                {"error": "invalid_description", "message": "Description is required (1-2000 chars)."},
                "DENY",
                "Invalid grievance description.",
                False,
                None,
            )

        token = f"CONF-{uuid.uuid4().hex[:10]}"
        seed = f"{session.tenant}|{session.member_id}|{session.session_id}|{category}|{description}|{expedite}"
        idem_key = f"grv-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:24]}"

        session.pending_grievance = PendingGrievance(
            confirmation_token=token,
            idempotency_key=idem_key,
            category=category,
            description=description,
            expedite_requested=expedite,
            proposed_at_turn=session.current_turn,
        )
        return (
            {
                "status": "PENDING_CONFIRMATION",
                "confirmation_token": token,
                "category": category,
                "description": description,
                "expedite_requested": expedite,
                "instruction": (
                    "Read this grievance summary back to the rep and ask for explicit confirmation "
                    "before calling confirm_and_file_grievance in the next turn."
                ),
            },
            "ALLOW",
            "Staged grievance draft awaiting explicit rep confirmation (no write performed).",
            False,
            {"confirmation_token": token, "category": category},
        )

    def _confirm_and_file_grievance(
        self, session: SessionContext, args: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str, bool, dict[str, Any] | None]:
        pending = session.pending_grievance
        token = str(args.get("confirmation_token") or "").strip()

        if pending is None or token != pending.confirmation_token:
            return (
                {"error": "no_pending_grievance", "message": "No matching staged grievance found. Call propose_grievance first."},
                "BLOCKED_CONFIRMATION",
                "Missing or invalid confirmation token.",
                False,
                None,
            )
        if pending.filed:
            return (
                {
                    "status": "ALREADY_FILED",
                    "grievance_id": pending.filed_grievance_id,
                    "message": "This grievance was already filed.",
                },
                "DENY",
                "Prevented duplicate grievance write in same session.",
                False,
                {"grievance_id": pending.filed_grievance_id},
            )
        # Turn-separation gate: impossible to self-confirm in the same turn (blocks indirect prompt injection!)
        if session.current_turn <= pending.proposed_at_turn:
            return (
                {
                    "error": "confirmation_requires_next_turn",
                    "message": "A grievance cannot be confirmed in the same turn it was proposed. Ask the rep to confirm first.",
                },
                "BLOCKED_CONFIRMATION",
                "Blocked same-turn self-confirmation.",
                False,
                None,
            )
        # Explicit rep-utterance verification
        if not is_explicit_rep_confirmation(session.current_rep_message):
            return (
                {
                    "error": "explicit_confirmation_missing",
                    "message": "The rep did not explicitly confirm filing (or asked to hold off/cancel). No grievance was filed.",
                },
                "BLOCKED_CONFIRMATION",
                "Rep turn lacked explicit affirmative confirmation.",
                False,
                None,
            )

        write_res = self.api_client.create_grievance(
            session.tenant,
            session.member_id,
            category=pending.category,
            description=pending.description,
            expedite_requested=pending.expedite_requested,
            idempotency_key=pending.idempotency_key,
        )
        pending.filed = True
        pending.filed_grievance_id = write_res.grievance.get("grievance_id")
        return (
            {
                "status": "FILED",
                "grievance": write_res.grievance,
                "idempotency_key": write_res.idempotency_key,
                "attempts": write_res.attempts,
                "idempotent_replayed": write_res.idempotent_replayed,
            },
            "ALLOW",
            "Filed grievance exactly once after explicit rep confirmation.",
            False,
            {
                "grievance_id": pending.filed_grievance_id,
                "attempts": write_res.attempts,
                "idempotent_replayed": write_res.idempotent_replayed,
            },
        )

    def _cancel_pending_grievance(
        self, session: SessionContext, _args: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str, bool, dict[str, Any] | None]:
        had_pending = session.pending_grievance is not None
        session.pending_grievance = None
        return (
            {"status": "CANCELLED", "had_pending": had_pending},
            "ALLOW",
            "Cancelled staged grievance; no write occurred.",
            False,
            None,
        )

    def _calculate_deadline(
        self, session: SessionContext, args: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str, bool, dict[str, Any] | None]:
        try:
            start = date.fromisoformat(str(args["start_date"]))
            days = int(args["days"])
            as_of = date.fromisoformat(session.as_of_date)
        except (KeyError, ValueError, TypeError) as exc:
            return ({"error": "invalid_input", "message": f"Invalid date/days: {exc}"}, "DENY", "Bad date args.", False, None)
        deadline = start + timedelta(days=days)
        days_remaining = (deadline - as_of).days
        return (
            {
                "start_date": start.isoformat(),
                "days_added": days,
                "deadline_date": deadline.isoformat(),
                "as_of_date": as_of.isoformat(),
                "is_within_deadline": as_of <= deadline,
                "is_past_deadline": as_of > deadline,
                "days_until_deadline": days_remaining,
            },
            "ALLOW",
            "Computed exact calendar deadline.",
            False,
            None,
        )

    def _calculate_remaining(
        self, _session: SessionContext, args: dict[str, Any]
    ) -> tuple[dict[str, Any], str, str, bool, dict[str, Any] | None]:
        try:
            limit_val = Decimal(str(args["limit"]))
            used_val = Decimal(str(args["used"]))
        except (KeyError, InvalidOperation, ValueError) as exc:
            return ({"error": "invalid_input", "message": f"Invalid numeric values: {exc}"}, "DENY", "Bad math args.", False, None)
        remaining = max(limit_val - used_val, Decimal("0"))
        # Format cleanly as integer if both inputs are whole numbers, else 2-decimal currency string
        if limit_val == int(limit_val) and used_val == int(used_val):
            formatted = str(int(remaining))
            formatted_usd = f"${int(remaining):,}"
        else:
            q = remaining.quantize(Decimal("0.01"))
            formatted = f"{q:.2f}"
            formatted_usd = f"${q:,.2f}"
        return (
            {
                "limit": float(limit_val),
                "used": float(used_val),
                "remaining": float(remaining),
                "formatted": formatted,
                "formatted_usd": formatted_usd,
            },
            "ALLOW",
            "Computed exact remaining balance/visits.",
            False,
            None,
        )
