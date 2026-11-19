"""Run a single local Lantern turn or the two-process milestone check."""

import argparse
import asyncio
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys

from .budget import Budget, MODEL_CONTEXT_TOKENS
from .chess_tool import CHESS_TOOL, analyze_position
from .codex_session import run_turn
from .persona import PROTOTYPE_PERSONA, ThreadPersonas
from .transport import (
    MAX_COMPLETION_PRICE, MAX_OUTPUT_TOKENS, MAX_PROMPT_PRICE, MODEL, ROUTES, OpenRouterGateway,
)


MATE_FEN = "7k/8/5KQ1/8/8/8/8/8 w - - 0 1"


def default_state() -> Path:
    if os.name == "nt":
        return Path(os.environ["LOCALAPPDATA"]) / "Lantern"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "lantern"


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=default_state(), help="Private operator state directory")
    parser.add_argument("--persona", type=Path, help="Lantern's UTF-8 persona statement")
    parser.add_argument("--codex", default=shutil.which("codex"), help="Path to the Codex executable")
    commands = parser.add_subparsers(dest="command", required=True)
    turn = commands.add_parser("say", help="Send one message; optionally resume a previous thread")
    turn.add_argument("message")
    turn.add_argument("--thread", help="Thread ID printed by a previous turn")
    turn.add_argument("--route", choices=ROUTES, default="fastest")
    commands.add_parser("smoke", help="Test both routes, a real chess tool call and cross-process recall")
    commands.add_parser("usage", help="Read this state directory's UTC-day spending and holds")
    return parser.parse_args()


async def main(args):
    state = args.state.resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    reservation = Decimal(MODEL_CONTEXT_TOKENS * MAX_PROMPT_PRICE +
                          MAX_OUTPUT_TOKENS * MAX_COMPLETION_PRICE) / 1_000_000
    ledger = Budget(state / "usage.sqlite", reservation)
    if args.command == "usage":
        print(json.dumps(ledger.today(), indent=2))
        return
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("OPENROUTER_API_KEY is missing from the operator environment")
    if not args.codex:
        raise ValueError("Set --codex to the Codex executable")
    personas = ThreadPersonas(state)
    supplied = args.persona.read_text(encoding="utf-8-sig") if args.persona else None
    persona = personas.select(getattr(args, "thread", None), supplied)
    if persona == PROTOTYPE_PERSONA:
        print("Using the temporary prototype persona.", file=sys.stderr)
    codex_home = state / "codex"
    workspace = state / "workspace"
    for directory in (codex_home, workspace):
        directory.mkdir(exist_ok=True, mode=0o700)

    async def turn(prompt, route, thread=None):
        positions = []

        def dispatch(name, arguments):
            if name != CHESS_TOOL["name"]:
                raise ValueError("Tool is not registered")
            result = analyze_position(arguments)
            positions.append(result["fen"])
            return result

        def record_thread(tid):
            personas.bind(tid, persona)
            print(f"Thread {tid}", file=sys.stderr, flush=True)

        with OpenRouterGateway(key, route=route, tools=[CHESS_TOOL], reserve=ledger.reserve,
                               settle=ledger.settle) as gateway:
            try:
                result = await run_turn(
                    codex_bin=args.codex, codex_home=codex_home, workspace=workspace, model=MODEL,
                    provider="lantern", base_url=gateway.base_url, persona=persona, prompt=prompt,
                    tools=[CHESS_TOOL], dispatch=dispatch, thread_id=thread,
                    on_thread=record_thread,
                    env={"LANTERN_GATEWAY_TOKEN": gateway.token},
                )
            finally:
                for entry in gateway.evidence:
                    print(json.dumps(entry), file=sys.stderr)
        if not gateway.evidence or any(e.get("status") != "completed" for e in gateway.evidence):
            raise RuntimeError("The gateway did not observe a completed response for every request")
        return result, positions

    if args.command == "say":
        result, _ = await turn(args.message, args.route, args.thread)
        print(result.text)
        return

    marker = "Beacon-" + secrets.token_hex(5)
    result, positions = await turn(
        f"Remember this operator ticket for later: {marker}. Use chess_analyze on the exact FEN "
        f"{MATE_FEN} and give its best move in SAN and one short sentence explaining why it wins.",
        "fastest",
    )
    print(result.text, flush=True)
    if MATE_FEN not in positions or "Qg7#" not in result.text:
        raise RuntimeError("The first turn did not analyze the fixture and report its mating move")
    recalled, _ = await turn(
        "From our preceding exchange, give the operator ticket and the mating move in SAN.",
        "cheapest", result.thread_id,
    )
    print(recalled.text, flush=True)
    if recalled.thread_id != result.thread_id or marker not in recalled.text or "Qg7#" not in recalled.text:
        raise RuntimeError("The resumed process did not recall the unique ticket and move")
    print(json.dumps({"smoke": "passed", "thread": result.thread_id,
                      "persona_sha256": hashlib.sha256(persona.encode()).hexdigest(),
                      "budget": ledger.today()}, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(main(arguments()))
    except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
        sys.exit(str(exc) or type(exc).__name__)
