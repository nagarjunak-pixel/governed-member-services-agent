"""Command-line interface for interactive or scripted multi-turn rep conversations."""

from __future__ import annotations

import argparse
import json
import sys

from member_agent.agent import MemberServicesAgent
from member_agent.config import Settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Governed Member-Services Agent CLI")
    ap.add_argument("--tenant", required=True, help="Tenant id (e.g. plan_a or plan_b)")
    ap.add_argument("--member", required=True, dest="member_id", help="Member id (e.g. M749817)")
    ap.add_argument("--pack-dir", default=None, help="Path to Emids pack directory (default: $PACK_DIR or ../pack)")
    ap.add_argument("--mock-url", default=None, help="Mock API base URL (default: http://127.0.0.1:8090)")
    ap.add_argument("--llm-base-url", default=None, help="OpenAI-compatible LLM base URL")
    ap.add_argument("--model", default=None, help="LLM model name")
    ap.add_argument(
        "--message",
        action="append",
        dest="messages",
        help="Non-interactive rep message (pass multiple times for multi-turn flows)",
    )
    ap.add_argument("--show-trace", action="store_true", help="Print PHI-redacted conversation trace JSON at exit")
    args = ap.parse_args(argv)

    settings = Settings.from_env(
        pack_dir=args.pack_dir,
        mock_api_base_url=args.mock_url,
        llm_base_url=args.llm_base_url,
        llm_model=args.model,
    )
    agent = MemberServicesAgent(settings)
    try:
        session = agent.create_session(args.tenant, args.member_id)
        if args.messages:
            for idx, msg in enumerate(args.messages, start=1):
                print(f"[Rep turn {idx}] {msg}")
                reply = session.send(msg)
                print(f"[Assistant] {reply}\n")
        else:
            print(
                f"Connected (tenant={args.tenant}, member={args.member_id}, "
                f"as_of={agent.as_of_date}, model={settings.llm_model}). Type 'exit' to quit."
            )
            while True:
                try:
                    user_in = input("Rep> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print()
                    break
                if not user_in or user_in.lower() in {"exit", "quit"}:
                    break
                reply = session.send(user_in)
                print(f"Assistant> {reply}\n")

        if args.show_trace:
            print(json.dumps(session.ctx.trace.to_dict(), indent=2))
        return 0
    finally:
        agent.close()


if __name__ == "__main__":
    sys.exit(main())
