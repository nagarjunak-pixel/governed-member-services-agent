"""Configuration loaded from environment variables and optional CLI overrides."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


def _load_dotenv(env_path: Path) -> None:
    """Populate os.environ from a local .env file without overriding existing vars."""
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _load_local_mock_tokens(pack_dir: Path) -> tuple[dict[str, str], str]:
    """Fallback for local development: read only API credentials from pack's tenants.json.

    In production, credentials come strictly from a secrets manager / environment variables.
    """
    tenants_file = pack_dir / "mock_services" / "data" / "tenants.json"
    if not tenants_file.is_file():
        return {}, ""
    raw = json.loads(tenants_file.read_text(encoding="utf-8"))
    tokens = {
        tid: info["api_token"]
        for tid, info in raw.get("tenants", {}).items()
        if info.get("api_token")
    }
    dir_key = raw.get("directory", {}).get("api_key", "")
    return tokens, dir_key


@dataclass(frozen=True)
class BudgetConfig:
    max_steps: int = 10
    max_cost_usd: float = 0.03
    max_latency_ms: int = 60_000


@dataclass(frozen=True)
class Settings:
    pack_dir: Path
    mock_api_base_url: str = "http://127.0.0.1:8090"
    tenant_tokens: dict[str, str] = field(default_factory=dict)
    directory_api_key: str = ""
    llm_base_url: str = "http://127.0.0.1:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen3.8:27b"
    llm_temperature: float = 0.0
    llm_timeout_s: float = 60.0
    llm_reasoning_effort: str | None = None
    llm_input_cost_per_1m: float = 0.0
    llm_output_cost_per_1m: float = 0.0
    budgets: BudgetConfig = field(default_factory=BudgetConfig)
    trace_dir: Path | None = None

    @property
    def kb_dir(self) -> Path:
        return self.pack_dir / "kb"

    @property
    def eval_file(self) -> Path:
        return self.pack_dir / "eval" / "conversations.json"

    @property
    def provider_manifest_file(self) -> Path:
        return self.pack_dir / "tools" / "third_party_provider_directory.json"

    @classmethod
    def from_env(
        cls,
        *,
        pack_dir: str | Path | None = None,
        mock_api_base_url: str | None = None,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        llm_api_key: str | None = None,
        dotenv_path: Path | None = None,
    ) -> Settings:
        _load_dotenv(dotenv_path or Path(".env"))

        resolved_pack = Path(pack_dir or os.environ.get("PACK_DIR", "../pack")).expanduser().resolve()

        tokens: dict[str, str] = {}
        raw_json = os.environ.get("TENANT_TOKENS_JSON", "").strip()
        if raw_json:
            tokens.update(json.loads(raw_json))
        if os.environ.get("TENANT_TOKEN_PLAN_A"):
            tokens["plan_a"] = os.environ["TENANT_TOKEN_PLAN_A"].strip()
        if os.environ.get("TENANT_TOKEN_PLAN_B"):
            tokens["plan_b"] = os.environ["TENANT_TOKEN_PLAN_B"].strip()

        dir_key = os.environ.get("DIRECTORY_API_KEY", "").strip()

        if not tokens or not dir_key:
            fallback_tokens, fallback_dir_key = _load_local_mock_tokens(resolved_pack)
            for tid, tok in fallback_tokens.items():
                tokens.setdefault(tid, tok)
            if not dir_key:
                dir_key = fallback_dir_key

        trace_dir_raw = os.environ.get("TRACE_DIR", "").strip()
        trace_dir = Path(trace_dir_raw).expanduser().resolve() if trace_dir_raw else None

        budgets = BudgetConfig(
            max_steps=int(os.environ.get("BUDGET_MAX_STEPS", "10")),
            max_cost_usd=float(os.environ.get("BUDGET_MAX_COST_USD", "0.03")),
            max_latency_ms=int(os.environ.get("BUDGET_MAX_LATENCY_MS", "60000")),
        )

        resolved_llm_url = (
            llm_base_url or os.environ.get("LLM_BASE_URL", "http://127.0.0.1:11434/v1")
        ).rstrip("/")
        raw_effort = os.environ.get("LLM_REASONING_EFFORT")
        if raw_effort is None:
            # Default to non-thinking mode on local Ollama for fast, concise tool calling
            is_local_ollama = "11434" in resolved_llm_url
            reasoning_effort: str | None = "none" if is_local_ollama else None
        else:
            reasoning_effort = raw_effort.strip() or None

        return cls(
            pack_dir=resolved_pack,
            mock_api_base_url=(
                mock_api_base_url or os.environ.get("MOCK_API_BASE_URL", "http://127.0.0.1:8090")
            ).rstrip("/"),
            tenant_tokens=tokens,
            directory_api_key=dir_key,
            llm_base_url=resolved_llm_url,
            llm_api_key=llm_api_key or os.environ.get("LLM_API_KEY", "ollama"),
            llm_model=llm_model or os.environ.get("LLM_MODEL", "qwen3.8:27b"),
            llm_temperature=float(os.environ.get("LLM_TEMPERATURE", "0.0")),
            llm_timeout_s=float(os.environ.get("LLM_TIMEOUT_S", "60")),
            llm_reasoning_effort=reasoning_effort,
            llm_input_cost_per_1m=float(os.environ.get("LLM_INPUT_COST_PER_1M", "0.0")),
            llm_output_cost_per_1m=float(os.environ.get("LLM_OUTPUT_COST_PER_1M", "0.0")),
            budgets=budgets,
            trace_dir=trace_dir,
        )
