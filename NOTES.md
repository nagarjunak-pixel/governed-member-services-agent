# Design Notes & Reflections

## Decisions & Assumptions

- **Deterministic governance over prompt-only guardrails.** Every tool call passes through `ToolGateway` (`src/member_agent/gateway.py`), which enforces immutable `(tenant, member_id)` session binding, PHI minimisation (`ssn`, `dob`, name, contact, street stripped before entering LLM context), boolean `verify_ssn_last4`, outbound third-party PHI blocking, step/cost/latency budgets, and a two-phase turn-separated confirmation state machine (`propose_grievance` → next rep turn with explicit confirmation → `confirm_and_file_grievance`).
- **Deterministic arithmetic & calendar tools.** Healthcare deductible/OOP balances and 30/45/180-day deadlines are too error-prone for mental LLM math, so `calculate_remaining` and `calculate_deadline` run deterministically in Python against the mock's `GET /health` clock (`2026-11-15`).
- **Local-first, provider-agnostic inference.** Developed locally with Ollama (`qwen3.8:27b`, OpenAI-compatible `/v1/chat/completions`) because no cloud API key was provisioned in this environment; switching to OpenAI, Gemini, or Bedrock/Vertex requires changing only environment variables.
- **Assumptions.** One rep session serves one tenant and one member; rep authentication/JWT issuance is handled upstream by SSO in production; in-memory session storage in the vertical slice maps to Aurora PostgreSQL + Redis in `docs/DESIGN.md`.

## What Surprised Me in the Data

1. **Misfiled cross-tenant KB document:** `kb/plan_a/virtual_care_2026.md` sits inside `plan_a/` but has YAML front-matter `tenant: plan_b` (Prairie Sky Health Virtual Care Benefit). A directory-only RAG loader leaks `plan_b` benefits into `plan_a`. Our loader enforces `folder == front_matter.tenant` and quarantines mismatches.
2. **Poisoned third-party MCP manifest:** `tools/third_party_provider_directory.json` instructs the model to silently prepend full SSN + DOB to `search_providers.query` without telling the user. We never expose raw vendor descriptions to the model and block PHI regexes on all outbound third-party calls.
3. **Indirect prompt injections in data & KB:** Claim `C2650799780` (`EV-13`) embeds a `provider_portal` note instructing the assistant to file an urgent grievance without asking; `kb/plan_a/member_services_guide.md` embeds an AI coding-assistant canary comment instruction (ignored and stripped at KB ingest).
4. **Mock API auth gap & cross-tenant ID collision:** `mock_services/server.py` validates bearer tokens in `_bearer()` but discards the returned tenant instead of checking it against `/tenants/{tenant}/`, and `M291543` exists in both `plan_a` and `plan_b` (`EV-30`). The gateway binds token-to-path strictly and namespaces all caches by `(tenant, member_id)`.
5. **`503`-after-write grievance trap:** Members with `digits % 4 == 1` (both `EV-16` and `EV-32`) persist the grievance on attempt 1 and still return `503`. Retrying with a deterministic `Idempotency-Key` and canonical JSON bytes replays `200` (`Idempotent-Replayed: true`) with zero duplicates.
6. **Four ground-truth label discrepancies in `eval/conversations.json` (v1.3):** `EV-23` expects `plan_b`'s leaked `$0 copay` for a `plan_a` Gold PPO member (actual: `$25 copay`); `EV-25` subtracts 2026 met from the 2027 SHMO deductible (`$2,830.88` vs actual 2026 `$2,330.88`); `EV-34` expects `PAID` whereas claim `C2680156037` is `DENIED` (`CO-16`); `EV-37` expects Value HDHP's `$10 copay after deductible` for a Choice PPO member (actual: `$15 copay`). We did not alter the pack or overfit to bad labels; the harness reports both raw and source-verified accuracy honestly.
7. **Conflicting NFRs:** NFR-1 (99.99% availability) conflicts with NFR-2 (single region, lowest cost), and NFR-3 (p95 < 3 s) conflicts with the 1.5 s/page claims API. Both are addressed explicitly in `docs/DESIGN.md`.

## What I Would Do With Another Day

- Add persistent SQLite/PostgreSQL session & idempotency storage so confirmations survive process restarts.
- Add SSE streaming on `/sessions/{id}/messages` and a CTI call-connect pre-warming endpoint (`POST /sessions/{id}/prewarm`) to prefetch paginated claims asynchronously.
- Add an optional semantic/hybrid vector retriever (using `nomic-embed-text` / `pgvector`) alongside BM25 for larger multi-hundred-page EOC PDFs, plus structured OpenTelemetry span export.
- Benchmark latency/accuracy across a hosted fast Tier-1 cloud model (e.g. Claude Haiku / GPT-4o-mini / Gemini Flash) to validate the < 3 s p95 and < $0.03/conv targets live.

## How AI Tools Were Used

This solution was built with AI coding assistance under Nagarjuna Kamireddy's architectural direction and review.
- **Where AI helped:** Rapidly scaffolding the gateway, HTTP API, test suite, Mermaid diagram, and systematic cross-verification of all 40 evaluation cases against the raw pack files (which surfaced all 4 label discrepancies and the mock bearer-check bug early).
- **Where AI needed correction / human judgment:** Initial draft packaging in `pyproject.toml` missed mapping top-level `eval/` alongside `src/member_agent/`; initial local Ollama latency budgets had to be separated from production budgets because local 27B inference takes ~15–30 s/case; and deciding not to overfit `EV-23`/`EV-25`/`EV-34`/`EV-37` required explicit architectural judgment to uphold tenant isolation and source-of-truth accuracy over chasing a misleading 100% raw score.
