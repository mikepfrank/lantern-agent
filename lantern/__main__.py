"""Chat with Lantern, run individual supervised turns, or inspect usage."""

import argparse
import asyncio
from decimal import Decimal, InvalidOperation
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sys

from .budget import Budget, MODEL_CONTEXT_TOKENS
from .chess_tool import CHESS_TOOL, analyze_position
from .codex_session import CodexError, run_turn
from .chat_store import ChatStore
from .chat_worker import MESSAGE_TOOL, serve, worker_online
from .persona import ThreadPersonas
from . import opus
from .luna import Luna
from .review_vendors import DEFAULT_DAILY_USD, ReviewerVendors
from .supervision import Supervisor, review_summary, supervised_tools
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
    parser.add_argument("--persona", type=Path, help="Use a custom UTF-8 persona for a new thread")
    parser.add_argument("--codex", default=shutil.which("codex"), help="Path to the Codex executable")
    commands = parser.add_subparsers(dest="command", required=True)
    turn = commands.add_parser("say", help="Send one message; optionally resume a previous thread")
    turn.add_argument("message")
    turn.add_argument("--thread", help="Thread ID printed by a previous turn")
    turn.add_argument("--route", choices=ROUTES, default="fastest")
    commands.add_parser("smoke", help="Test both routes, a real chess tool call and cross-process recall")
    commands.add_parser("usage", help="Read this state directory's UTC-day spending and holds")
    commands.add_parser("chat", help="Attach to the persistent operator conversation")
    send = commands.add_parser("send", help="Queue a message without attaching to chat")
    send.add_argument("message")
    send.add_argument("--id", help="Optional stable ID for retrying the same submission")
    history = commands.add_parser("history", help="Print recent conversation events as JSON")
    history.add_argument("--limit", type=int, default=30)
    commands.add_parser("status", help="Read worker, queue, and active turn status")
    worker = commands.add_parser("worker", help="Serve the durable chat until stopped")
    worker.add_argument("--route", choices=ROUTES, default="fastest")
    return parser.parse_args()


async def main(args):
    state = args.state.resolve()
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.command in {"chat", "send", "history", "status"}:
        store = ChatStore(state)
        if args.command == "chat":
            from .chat_cli import attach
            if not worker_online(state):
                print("Worker offline. Messages will wait in the queue.", file=sys.stderr)
            attach(store)
        elif args.command == "send":
            print(store.submit(args.message, client_id=args.id))
        elif args.command == "history":
            print(json.dumps([{"id": e.id, "at": e.at, "role": e.role, "text": e.text}
                              for e in store.recent(args.limit)], ensure_ascii=False, indent=2))
        else:
            print(json.dumps({**store.status(), "worker_online": worker_online(state)}, indent=2))
        return
    reservation = Decimal(MODEL_CONTEXT_TOKENS * MAX_PROMPT_PRICE +
                          MAX_OUTPUT_TOKENS * MAX_COMPLETION_PRICE) / 1_000_000
    ledger = Budget(state / "usage.sqlite", reservation)
    try:
        limit = Decimal(os.environ.get("LANTERN_REVIEW_DAILY_USD", str(DEFAULT_DAILY_USD)))
    except InvalidOperation as exc:
        raise ValueError("LANTERN_REVIEW_DAILY_USD must be a dollar amount") from exc
    vendors = ReviewerVendors(state, limit)
    if args.command == "usage":
        print(json.dumps({**ledger.today(), "supervisors": vendors.today(),
                          "review_events_all_time": review_summary(state)}, indent=2))
        return
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise ValueError("OPENROUTER_API_KEY is missing from the operator environment")
    if not args.codex:
        raise ValueError("Set --codex to the Codex executable")
    personas = ThreadPersonas(state)
    supplied = args.persona.read_text(encoding="utf-8-sig") if args.persona else None
    codex_home = state / "codex"
    workspace = state / "workspace"
    for directory in (codex_home, workspace):
        directory.mkdir(exist_ok=True, mode=0o700)

    async def turn(prompt, route, thread=None, *, on_thread=None, take_input=None, return_input=None, notify=None):
        persona = personas.select(thread, supplied)
        positions = []
        tools = supervised_tools([CHESS_TOOL, MESSAGE_TOOL] if notify else [CHESS_TOOL])
        supervisor = Supervisor(state, Luna(vendors.call), partial(opus.review, vendors.call),
                                partial(opus.consult, vendors.call),
                                sample_every=int(os.environ.get("LANTERN_SPOT_EVERY", "12")))

        def execute(call_id, name, arguments):
            if name == MESSAGE_TOOL["name"] and notify is not None:
                return notify(call_id, arguments)
            if name != CHESS_TOOL["name"]:
                raise ValueError("Tool is not registered")
            result = analyze_position(arguments)
            positions.append(result["fen"])
            return result

        def record_thread(tid):
            personas.bind(tid, persona)
            supervisor.bind(tid)
            if on_thread is not None:
                on_thread(tid)
            print(f"Thread {tid}", file=sys.stderr, flush=True)

        gateway = OpenRouterGateway(key, route=route, tools=tools, reserve=ledger.reserve,
                                    settle=ledger.settle, observe=supervisor.observe,
                                    unregistered=supervisor.unregistered, resuming=bool(thread))
        try:
            with gateway:
                result = await run_turn(
                    codex_bin=args.codex, codex_home=codex_home, workspace=workspace, model=MODEL,
                    provider="lantern", base_url=gateway.base_url, persona=persona, prompt=prompt,
                    tools=tools, dispatch=lambda cid, name, args: supervisor.dispatch(cid, name, args, partial(execute, cid)),
                    thread_id=thread,
                    on_thread=record_thread, take_input=take_input, return_input=return_input,
                    env={"LANTERN_GATEWAY_TOKEN": gateway.token},
                    timeout=600 if args.command == "worker" else 180,
                )
        except CodexError as exc:
            last = gateway.evidence[-1] if gateway.evidence else {}
            if last.get("status") == "incomplete" and (last.get("output_limit_reached")
                                                        or last.get("output_tokens") == MAX_OUTPUT_TOKENS):
                raise CodexError(f"The provider reached the {MAX_OUTPUT_TOKENS:,}-token response limit.") from exc
            raise
        finally:
            for entry in gateway.evidence:
                print(json.dumps(entry), file=sys.stderr, flush=True)
        if not gateway.evidence or any(e.get("status") != "completed" for e in gateway.evidence):
            raise RuntimeError("The gateway did not observe a completed response for every request")
        return result, positions

    if args.command == "worker":
        await serve(state, ChatStore(state), turn, args.route)
        return
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
                      "persona_sha256": hashlib.sha256(personas.select(result.thread_id, supplied).encode()).hexdigest(),
                      "budget": ledger.today()}, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(main(arguments()))
    except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
        sys.exit(str(exc) or type(exc).__name__)
