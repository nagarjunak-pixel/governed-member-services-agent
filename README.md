# Governed Multi-Tenant Member-Services Agent

A governed multi-tenant AI assistant for healthcare member-services representatives serving **Harborview Health Plan (`plan_a`)** and **Prairie Sky Health (`plan_b`)**.

Every tool invocation passes through a centralized **Tool Gateway** (`src/member_agent/gateway.py`) that enforces tenant isolation, HIPAA minimum-necessary PHI minimisation, outbound third-party PHI blocking, two-phase turn-separated human confirmation for grievance writes, deterministic idempotency under `503` retries, per-conversation step/cost/latency budgets, and PHI-redacted audit tracing.

## Repository Layout

```text
├── src/member_agent/
│   ├── config.py        # Environment & CLI configuration (PACK_DIR, LLM provider, budgets)
│   ├── redaction.py     # PHI minimisation, outbound PHI detection, log & trace scrubber
│   ├── trace.py         # Per-conversation structured audit trace
│   ├── kb.py            # Tenant-isolated Markdown KB loader with mismatch quarantine
│   ├── api_client.py    # Tenant-bound HTTP client (429 backoff, 503 idempotent retries)
│   ├── gateway.py       # Centralized Tool Gateway (single governance choke point)
│   ├── llm.py           # OpenAI-compatible Chat Completions client
│   ├── agent.py         # Multi-turn conversation orchestrator
│   ├── api.py           # FastAPI HTTP service (/sessions, /messages, /trace)
│   └── cli.py           # Interactive & scripted multi-turn CLI
├── src/eval/
│   ├── harness.py       # 40-case evaluation runner against live mock API + LLM
│   └── gate.py          # CI regression gate with justified thresholds + report generator
├── tests/               # Unit & integration tests (gateway, idempotency, KB, redaction, gate)
├── docs/
│   ├── DESIGN.md        # Target AWS architecture, 1-page GCP mapping, threat model, 4 ADRs
│   └── architecture.mmd # Mermaid source for the production architecture diagram
├── reports/
│   ├── eval_report.md   # Committed evaluation report from the latest full run
│   └── eval_report.json # Machine-readable report checked by CI
└── NOTES.md             # Decisions, assumptions, data findings, next steps, AI tool usage
```

> **Note:** The supplied assessment pack (`data/`, `kb/`, `mock_services/`, `tools/`, `eval/conversations.json`) is **not** committed to this repository and is ignored via `.gitignore`. Point the application to your local copy of the pack using `PACK_DIR` or `--pack-dir`.

## Prerequisites

- Python **3.10+** (developed and tested on Python **3.12**)
- The supplied Emids assessment pack (referenced below as `../pack`)
- An OpenAI-compatible LLM endpoint:
  - **Default (developed with):** Local **Ollama** (`http://127.0.0.1:11434/v1`) running `qwen3.8:27b` (also tested with `qwen3.6:35b-a3b-agent`).
  - Any OpenAI-compatible cloud API (OpenAI, Gemini, or Anthropic-compatible gateway) can be configured via environment variables.

## 1. Setup

```bash
git clone https://github.com/nagarjunak-pixel/governed-member-services-agent.git
cd governed-member-services-agent

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
```

Edit `.env` if your pack is not at `../pack` or if you want to override the LLM provider or API tokens:

- `PACK_DIR`: path to your copy of the supplied pack (default: `../pack`).
- `TENANT_TOKEN_PLAN_A`, `TENANT_TOKEN_PLAN_B`, `DIRECTORY_API_KEY`: API credentials from the pack's `data/README.md`. (For local development, if unset in `.env`, `config.py` falls back to reading only the auth tokens from `<PACK_DIR>/mock_services/data/tenants.json`.)

### Switching Model Providers

The agent uses a standard OpenAI-compatible `/chat/completions` client (`src/member_agent/llm.py`):

| Provider | `LLM_BASE_URL` | `LLM_MODEL` | `LLM_API_KEY` |
|---|---|---|---|
| **Ollama (default)** | `http://127.0.0.1:11434/v1` | `qwen3.8:27b` | `ollama` |
| **OpenAI** | `https://api.openai.com/v1` | `gpt-4o-mini` | `<OPENAI_API_KEY>` |
| **Google Gemini** | `https://generativelanguage.googleapis.com/v1beta/openai` | `gemini-2.5-flash` | `<GEMINI_API_KEY>` |
| **Anthropic (via OpenAI-compatible gateway / LiteLLM / Bedrock proxy)** | `<PROXY_BASE_URL>/v1` | `claude-haiku-4-5` | `<PROXY_API_KEY>` |

Set `LLM_INPUT_COST_PER_1M` and `LLM_OUTPUT_COST_PER_1M` when using paid APIs so the gateway enforces `BUDGET_MAX_COST_USD` (default `$0.03`).

## 2. Start the Mock Services

In terminal 1, start the supplied mock server from your copy of the pack:

```bash
python3 ../pack/mock_services/server.py --port 8090
```

Verify it is running (`GET /health` returns clock `2026-11-15`):

```bash
curl http://127.0.0.1:8090/health
```

## 3. Run the Agent

### Option A — Interactive or Scripted CLI

Interactive multi-turn session:

```bash
member-agent --tenant plan_a --member M704649 --pack-dir ../pack
```

Scripted multi-turn confirmation example (with PHI-redacted trace):

```bash
member-agent \
  --tenant plan_a \
  --member M704649 \
  --pack-dir ../pack \
  --message "The member wants to file a billing grievance: the provider billed full amount for the 2026-08-14 visit even though the plan paid it." \
  --message "Yes, I read it back to the member and the member agrees. Go ahead and file it." \
  --show-trace
```

### Option B — HTTP API (FastAPI)

Start the API server:

```bash
member-agent-api --port 8000 --pack-dir ../pack
```

Run a multi-turn conversation via `curl`:

```bash
# 1. Create a tenant- and member-bound session
SESSION_ID=$(curl -s -X POST http://127.0.0.1:8000/sessions \
  -H "Content-Type: application/json" \
  -d '{"tenant":"plan_a","member_id":"M749817"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['session_id'])")

# 2. Ask a question
curl -s -X POST "http://127.0.0.1:8000/sessions/${SESSION_ID}/messages" \
  -H "Content-Type: application/json" \
  -d '{"message":"What does an in-network specialist visit cost this member?"}'

# 3. Inspect the PHI-redacted audit trace
curl -s "http://127.0.0.1:8000/sessions/${SESSION_ID}/trace"
```

## 4. Run Unit & Governance Tests

```bash
pytest -v
```

Covers tenant isolation, misfiled KB quarantine, PHI minimisation and log scrubbing, third-party SSN exfiltration blocking, indirect prompt-injection sanitization, two-phase confirmation state machine, `503` grievance retry idempotency, `429` claims backoff, budgets, and CI regression gate checks.

## 5. Run the Full 40-Case Evaluation & Regression Gate

Start a fresh mock server (`python3 ../pack/mock_services/server.py --port 8090`), then run:

```bash
member-agent-eval --pack-dir ../pack --mock-url http://127.0.0.1:8090 --out-dir reports
```

- Writes `reports/eval_report.md` and `reports/eval_report.json`.
- Exits `0` if all CI regression gates pass, or `1` if quality or any zero-tolerance security/governance invariant drops below threshold.
- See [`reports/eval_report.md`](./reports/eval_report.md) for the committed results from our run, [`docs/DESIGN.md`](./docs/DESIGN.md) for the production architecture, and [`NOTES.md`](./NOTES.md) for design decisions and findings in the pack data.
