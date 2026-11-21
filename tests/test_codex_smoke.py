"""Opt in with LANTERN_TEST_CODEX=1 and a native codex binary on PATH.

All three model providers are simulated. Native Codex, its on-disk thread,
the HTTP gateway, persona, review policy and chess engine are real.
"""

import asyncio
from contextlib import closing, contextmanager, redirect_stderr, redirect_stdout
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call, patch

from lantern.__main__ import MATE_FEN, arguments, main
from lantern.chess_tool import analyze_position
from lantern.opus import OPUS_MODEL
from lantern.persona import ThreadPersonas
from lantern.review_vendors import DEFAULT_DAILY_USD, ReviewerVendors
from lantern.transport import MODEL


class FixtureStream(BytesIO):
    status = 200

    def getheader(self, name, default=None):
        return "text/event-stream" if name == "Content-Type" else default


def reply(serial, item):
    response = {"id": f"resp_fixture_{serial}", "model": MODEL}
    if item["type"] == "function_call":
        initial = {**item, "status": "in_progress", "arguments": ""}
        delta = {"type": "response.function_call_arguments.delta", "delta": item["arguments"]}
    else:
        initial = {**item, "status": "in_progress", "content": []}
        delta = {"type": "response.output_text.delta", "content_index": 0, "delta": item["content"][0]["text"]}
    events = [
        {"type": "response.created", "response": {**response, "status": "in_progress"}},
        {"type": "response.output_item.added", "output_index": 0, "item": initial},
        {**delta, "output_index": 0, "item_id": item["id"]},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {**response, "status": "completed", "output": [item],
            "usage": {"input_tokens": 100, "output_tokens": 40, "total_tokens": 140, "cost": 0.00001}}},
    ]
    return FixtureStream(b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events))


@unittest.skipUnless(os.environ.get("LANTERN_TEST_CODEX") == "1", "opt in with LANTERN_TEST_CODEX=1")
class NativeCodexSmoke(unittest.TestCase):
    def test_private_cli_operator_on_new_and_preexisting_threads(self):
        binary = os.environ.get("LANTERN_CODEX_BIN") or shutil.which("codex")
        self.assertTrue(binary, "Cannot find the native codex executable")
        requests = []

        @contextmanager
        def provider(body, headers):
            requests.append(json.loads(body))
            n = len(requests)
            answer = "I do not know who the operator is." if n == 1 else "Received."
            yield reply(n, {"type": "message", "role": "assistant", "id": f"msg_operator_{n}",
                           "status": "completed", "content": [{"type": "output_text", "text": answer}]})

        with TemporaryDirectory(prefix="lantern-operator-") as tmp:
            state = Path(tmp)
            profile = state / "persona.md"
            old_persona = "You are the lantern in the old reading room."
            new_persona = "You are the lantern beside the bridge."
            profile.write_text(old_persona, encoding="utf-8")

            def say(options, message, thread=None):
                argv = ["lantern", "--state", str(state), "--codex", binary, *options, "say", message]
                if thread:
                    argv.extend(["--thread", thread])
                with patch.object(sys, "argv", argv), redirect_stdout(StringIO()), redirect_stderr(StringIO()) as log:
                    asyncio.run(main(arguments()))
                return re.search(r"^Thread ([0-9a-f-]+)$", log.getvalue(), re.MULTILINE).group(1)

            with patch.dict(os.environ, OPENROUTER_API_KEY="offline"), \
                 patch("lantern.transport._open_upstream", provider), \
                 patch("lantern.luna.Luna.screen", return_value={"escalate": False, "evidence": "", "summary": "Test chat."}), \
                 patch("lantern.review_vendors.request_json", side_effect=AssertionError("Unexpected vendor call")):
                original = say(["--persona", str(profile)], "First visit to the reading room.")
                profile.write_text(new_persona, encoding="utf-8")
                resumed = say(["--private-lightsail-cli"], "Who is operating this terminal?", original)
                fresh = say(["--private-lightsail-cli", "--persona", str(profile)], "A fresh conversation.")
                personas = ThreadPersonas(state)
                self.assertEqual(personas.select(resumed, None), old_persona)
                self.assertEqual(personas.select(fresh, None), new_persona)

        self.assertEqual(original, resumed)
        self.assertNotEqual(original, fresh)
        self.assertEqual(len(requests), 3)
        for index, request in enumerate(requests):
            instructions = request.get("instructions", "")
            if index == 0:
                self.assertNotIn("Michael P. Frank", instructions)
            else:
                self.assertIn("Michael P. Frank", instructions)
                self.assertIn("only human", instructions.lower())
            history = request["input"]
            first_user = next(i for i, item in enumerate(history) if item.get("role") == "user")
            developers = [part["text"] for item in history[:first_user] if item.get("role") == "developer"
                          for part in item["content"] if part["type"] == "input_text"]
            self.assertTrue(any((old_persona if index < 2 else new_persona) in text for text in developers))
        self.assertIn("First visit to the reading room.", json.dumps(requests[1]["input"]))
        self.assertIn("I do not know who the operator is.", json.dumps(requests[1]["input"]))
        self.assertNotIn("First visit to the reading room.", json.dumps(requests[2]["input"]))

    def test_native_thread_tool_roundtrip_and_resume(self):
        binary = os.environ.get("LANTERN_CODEX_BIN") or shutil.which("codex")
        self.assertTrue(binary, "Cannot find the native codex executable")
        persona_file = Path(__file__).resolve().parents[1] / "lantern" / "personas" / "lantern-v1.md"
        persona = persona_file.read_text(encoding="utf-8")
        routes = []
        errors = []
        child_pids = []
        review_calls = []
        start_process = asyncio.create_subprocess_exec
        initial_reason = "I intend to use a private puzzle before its author grants permission."
        denial = "Wait for permission or switch to an openly shared position."
        advice = "Use the public mate-in-one fixture."
        side_remarks = {"allow": "The owner clarified the scope.", "deny": "This applies to the private puzzle.",
                        "advice": "An open puzzle is available."}
        allowed_reason = "The supplied fixture is public and the operator requests chess analysis."
        private_fen = "7k/8/5KQ1/8/8/8/P7/8 w - - 0 1"
        authorization = f"I own this private puzzle and now authorize its analysis: {private_fen}"

        def fake_review(vendor, key, body, *, audit=None):
            payload = json.loads(body)
            usage = {"input_tokens": 60, "output_tokens": 30}
            if vendor == "luna":
                self.assertEqual(key, "offline-luna-key")
                packet = json.loads(payload["input"][0]["content"])
                review_calls.append((vendor, packet["kind"]))
                escalate = packet["kind"] == "tool" and packet["rationale"] == initial_reason
                report = {"escalate": escalate, "evidence": "The agent reports missing permission." if escalate else "",
                          "summary": "The operator asked Lantern to analyze a chess position."}
                if f"user: {authorization}" in packet.get("recent_context", ""):
                    report["summary"] = authorization
                return {"model": payload["model"], "status": "completed", "usage": usage, "output": [
                    {"type": "message", "role": "assistant", "status": "completed",
                     "content": [{"type": "output_text", "text": json.dumps(report)}]}]}
            if vendor != "opus":
                raise AssertionError("Unexpected reviewer")
            self.assertEqual(key, "offline-opus-key")
            packet = json.loads(payload["messages"][0]["content"])
            case = packet["case"]
            review_calls.append((vendor, case["tool"]))
            if case["tool"] == "chess_analyze":
                self.assertEqual(case["arguments"], {"fen": private_fen})
                if packet["host_facts"]["prior_exact_denial"] == denial:
                    self.assertEqual(case["summary"], authorization)
                    self.assertEqual(case["evidence"], "")  # Luna cleared the exact retry.
                    text = '<review decision="allow">The author directly authorized this puzzle.</review>' + side_remarks["allow"]
                else:
                    self.assertEqual(case["rationale"], initial_reason)
                    self.assertIn("permission", case["evidence"])
                    text = side_remarks["deny"] + f'<review decision="deny">{denial}</review>'
            else:
                self.assertEqual(case["tool"], "consult_opus")
                self.assertIn("question", case["arguments"])
                text = f"<advice>{advice}</advice>" + side_remarks["advice"]
            return {"model": OPUS_MODEL, "stop_reason": "end_turn", "usage": usage,
                    "content": [{"type": "text", "text": text}]}

        async def record_process(*args, **kwargs):
            process = await start_process(*args, **kwargs)
            child_pids.append(process.pid)
            return process

        @contextmanager
        def fake_provider(body, headers):
            try:
                request = json.loads(body)
                self.assertEqual(request["model"], MODEL)
                self.assertNotIn("Michael P. Frank", request.get("instructions", ""))
                self.assertEqual({t["name"] for t in request["tools"]}, {"chess_analyze", "consult_opus"})
                self.assertEqual(headers["Authorization"], "Bearer offline-test-value")
                routes.append(request["provider"]["sort"])
                serial = len(routes)
                history = request["input"]
                first_user = next(index for index, item in enumerate(history) if item.get("role") == "user")
                developer_texts = [
                    part["text"] for message in history[:first_user] if message.get("role") == "developer"
                    for part in message["content"] if part["type"] == "input_text"
                ]
                self.assertTrue(any(persona in text for text in developer_texts),
                                f"Request {serial} is missing the full persona before its first user message")
                users = [json.dumps(i["content"]) for i in history if i.get("role") == "user"]
                ticket = re.search(r"Beacon-[0-9a-f]+", " ".join(users))
                self.assertIsNotNone(ticket, "operator ticket missing from the actual native thread")
                tool_outputs = {i["call_id"]: json.dumps(i["output"]) for i in history
                                if i.get("type") == "function_call_output"}
                if serial == 1:
                    self.assertIn(MATE_FEN, users[-1])
                    tool, args = "chess_analyze", {"fen": private_fen, "rationale": initial_reason}
                elif serial == 2:
                    self.assertIn(denial, tool_outputs["call_1"])
                    self.assertIn(side_remarks["deny"], tool_outputs["call_1"])
                    self.assertNotIn("Qg7#", tool_outputs["call_1"])
                    tool, args = "consult_opus", {"question": "Which alternative should I choose?",
                                                  "rationale": "I would like advice on obtaining permission."}
                elif serial == 3:
                    self.assertIn(advice, tool_outputs["call_2"])
                    self.assertIn(side_remarks["advice"], tool_outputs["call_2"])
                    tool, args = "chess_analyze", {"fen": MATE_FEN, "rationale": allowed_reason}
                elif serial == 6:
                    self.assertIn(authorization, users[-1])
                    tool, args = "chess_analyze", {"fen": private_fen, "rationale": "The author now consents."}
                if serial <= 3 or serial == 6:
                    item = {"type": "function_call", "id": f"fc_{serial}", "call_id": f"call_{serial}",
                            "status": "completed", "name": tool, "arguments": json.dumps(args)}
                else:
                    self.assertIn("Qg7#", tool_outputs["call_3"])
                    if serial == 4:
                        answer = "Qg7# is checkmate."
                    elif serial == 5:
                        self.assertIn("preceding exchange", users[-1])
                        self.assertNotIn(ticket.group(), users[-1])
                        answer = f"{ticket.group()} Qg7#"
                    else:
                        self.assertEqual(serial, 7, "unexpected extra API request")
                        self.assertIn("Qg7#", tool_outputs["call_6"])
                        self.assertIn(side_remarks["allow"], tool_outputs["call_6"])
                        answer = "The private puzzle also ends with Qg7#."
                    item = {"type": "message", "role": "assistant", "id": f"msg_fixture_{serial}",
                            "status": "completed", "content": [{"type": "output_text", "annotations": [],
                                                                  "text": answer}]}
            except (AssertionError, IndexError, KeyError, TypeError) as error:
                errors.append(str(error))
                raise
            yield reply(serial, item)

        with TemporaryDirectory(prefix="lantern-smoke-") as tmp:
            state = Path(tmp)
            with patch.object(sys, "argv", ["lantern", "--state", str(state), "--codex", binary,
                                            "--private-lightsail-cli", "smoke"]):
                args = arguments()
            with patch.dict(os.environ, OPENROUTER_API_KEY="offline-test-value",
                            OPENAI_API_KEY="offline-luna-key", ANTHROPIC_API_KEY="offline-opus-key",
                            LANTERN_SPOT_EVERY="12", LANTERN_REVIEW_DAILY_USD=str(DEFAULT_DAILY_USD)), \
                 patch("lantern.transport._open_upstream", fake_provider), \
                 patch("lantern.review_vendors.request_json", side_effect=fake_review), \
                 patch("lantern.__main__.analyze_position", wraps=analyze_position) as engine, \
                 patch("asyncio.create_subprocess_exec", record_process), redirect_stdout(StringIO()) as output:
                try:
                    asyncio.run(main(args))
                    tid = re.search(r'"thread": "([0-9a-f-]+)"', output.getvalue()).group(1)
                    with patch.object(sys, "argv", ["lantern", "--state", str(state), "--codex", binary,
                                                    "say", authorization, "--thread", tid]):
                        asyncio.run(main(arguments()))
                finally:
                    self.assertEqual(errors, [])
            ledger = ReviewerVendors(state, DEFAULT_DAILY_USD).today()
            self.assertEqual({entry["purpose"]: entry["calls"] for entry in ledger["by_purpose"]},
                             {"spot": 1, "tool": 4, "escalation": 2, "consult": 1})
            self.assertEqual(ledger["reserved_usd"], "0")
            with closing(sqlite3.connect(state / "review-costs.sqlite")) as db:
                tagged = db.execute("SELECT thread_id,batch_id,vendor,purpose,tool FROM calls ORDER BY rowid").fetchall()
            self.assertEqual(tagged, [(tid, None, *fields) for fields in [
                ("luna", "spot", None), ("luna", "tool", "chess_analyze"),
                ("opus", "escalation", "chess_analyze"), ("luna", "tool", "consult_opus"),
                ("opus", "consult", "consult_opus"), ("luna", "tool", "chess_analyze"),
                ("luna", "tool", "chess_analyze"), ("opus", "escalation", "chess_analyze"),
            ]])
        self.assertEqual(engine.call_args_list, [call({"fen": MATE_FEN}), call({"fen": private_fen})])
        self.assertEqual(review_calls, [("luna", "context"), ("luna", "tool"), ("opus", "chess_analyze"),
                                       ("luna", "tool"), ("opus", "consult_opus"), ("luna", "tool"),
                                       ("luna", "tool"), ("opus", "chess_analyze")])
        self.assertEqual(routes, ["throughput"] * 4 + ["price"] + ["throughput"] * 2)
        self.assertEqual(len(set(child_pids)), 3)
        self.assertIn('"smoke": "passed"', output.getvalue())
