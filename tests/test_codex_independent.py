"""Upgrade the same private room across tool releases with native Codex and offline providers."""

import asyncio
from contextlib import closing, contextmanager, redirect_stderr
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest
from unittest.mock import patch

from lantern.__main__ import arguments, main
from lantern.chat_store import ChatStore
from lantern.file_grants import FileGrants
from lantern.chat_worker import process_batch
from test_codex_smoke import reply
from test_github_tools import sample_proposal


# The exact November 18 deployment. Its original instructions were persisted by
# native Codex in the same private room Mike resumed after the milestone upgrade.
OLD_BASE = (
    "You are Lantern, an agent in a conversation with your operator. "
    "Your persona follows in the developer instructions. In this prototype, "
    "use the registered tools for chess analysis and ethical consultation. "
    "GPT-6 Luna screens each proposed tool call and periodically reviews your conversation. "
    "Give a brief public rationale for the exact action. Claude 3 Opus decides flagged cases. "
    "Use consult_opus when available to ask for guidance. Its research field records any "
    "lookups Opus actually made in Wikipedia or Crossref. Each decision applies only to the "
    "action reviewed. If held or denied, explain the outcome and respect the decision. "
    "Describe other capabilities in the persona as planned. "
    "Codex saves this conversation for resumption. You cannot write a notes file in this "
    "session. Only report an external action as complete when a tool has confirmed it. "
    "Use the supplied chess tool "
    "when the operator requests an engine analysis, then interpret its actual result. "
    "When message_operator is available, use it for a useful interim update. "
    "The operator can add a message while you work. Take it into account when it arrives. "
    "Give the operator a final answer in ordinary text."
)


@unittest.skipUnless(os.environ.get("LANTERN_TEST_CODEX") == "1", "requires native Codex")
class NativeIndependentWorker(unittest.TestCase):
    def test_existing_room_resumes_for_supervised_independent_work(self):
        binary = os.environ.get("LANTERN_CODEX_BIN") or shutil.which("codex")
        self.assertTrue(binary, "Cannot find the native codex executable")
        note = "Fixture journal: the rook endgame is next."
        filename = "journal/entry.md"
        operator_text = "Remember the fixture clue: blue rook."
        old_answer, new_answer = "I have recorded the clue in our conversation.", "The private journal is saved."
        actions = [
            ("workspace_write", {"path": filename, "text": note}),
            ("workspace_read", {"path": filename}),
            ("budget_status", {}),
        ]
        requests, reviews, threads = [], [], []
        proposal = sample_proposal()
        journal_edits = [
            ("workspace_append", {"path": filename, "text": "\nQueued.", "expected_suffix": "endgame is next."}),
            ("workspace_replace", {"path": filename, "find": "rook endgame", "replace": "pawn endgame"}),
        ]

        @contextmanager
        def provider(body, headers):
            requests.append(json.loads(body))
            serial = len(requests)
            if 2 <= serial < 2 + len(actions):
                name, args = actions[serial - 2]
                item = {"type": "function_call", "id": f"fc_{serial}", "call_id": f"call_{serial}",
                        "status": "completed", "name": name,
                        "arguments": json.dumps({**args, "rationale": "Maintain the private fixture journal."})}
            elif serial == 6:
                item = {"type": "function_call", "id": "fc_6", "call_id": "call_6", "status": "completed",
                        "name": "coding_run", "arguments": json.dumps({"command": "python -V",
                        "rationale": "Identify the Python interpreter in my offline checkout."})}
            elif serial == 7:
                item = {"type": "function_call", "id": "fc_7", "call_id": "call_7", "status": "completed",
                        "name": "harness_logs", "arguments": json.dumps({"batch_id": 2, "limit": 3,
                        "rationale": "Check the recorded outcome of my earlier heartbeat."})}
            elif serial in (8, 9):
                name, args = (("github_preview", {"commit": proposal["source_commit"]}) if serial == 8 else
                              ("github_open_draft", {"proposal_id": proposal["proposal_id"][:12]}))
                item = {"type": "function_call", "id": f"fc_{serial}", "call_id": f"call_{serial}",
                        "status": "completed", "name": name,
                        "arguments": json.dumps({**args, "rationale": "Propose the fixture change for review."})}
            elif serial in (10, 11):
                name, args = journal_edits[serial - 10]
                item = {"type": "function_call", "id": f"fc_{serial}", "call_id": f"call_{serial}",
                        "status": "completed", "name": name,
                        "arguments": json.dumps({**args, "rationale": "Edit the private fixture journal."})}
            elif serial in (12, 13):
                name, args = (("file_import_status", {}) if serial == 12 else
                              ("import_file", {"grant_id": 1, "filename": "tides.csv"}))
                item = {"type": "function_call", "id": f"fc_{serial}", "call_id": f"call_{serial}",
                        "status": "completed", "name": name, "arguments": json.dumps(
                            {**args, "rationale": "Analyze the approved tide observations offline."})}
            else:
                answer = old_answer if serial == 1 else new_answer
                item = {"type": "message", "role": "assistant", "id": f"msg_{serial}", "status": "completed",
                        "content": [{"type": "output_text", "text": "\n\n" + answer + "\n\n"}]}
            yield reply(serial, item)

        def reviewer(vendor, key, body):
            self.assertEqual(vendor, "luna")
            payload = json.loads(body)
            packet = json.loads(payload["input"][0]["content"])
            if packet["kind"] == "tool":
                reviews.append((packet["tool"], payload["instructions"]))
                if packet["tool"] == "github_open_draft":
                    self.assertEqual(packet["host_facts"]["public_draft"], proposal)
                if packet["tool"] == "import_file":
                    grant = packet["host_facts"]["file_import_grant"]
                    self.assertEqual((grant["grant_id"], grant["mode"]), (1, "once"))
                    self.assertEqual(grant["file"]["url"], "https://example.org/tides.csv")
            report = {"escalate": False, "summary": "Private worker fixture.", "evidence": ""}
            return {"model": payload["model"], "status": "completed",
                    "usage": {"input_tokens": 30, "output_tokens": 15}, "output": [
                        {"type": "message", "role": "assistant", "status": "completed",
                         "content": [{"type": "output_text", "text": json.dumps(report)}]}]}

        def settlement(*args, **kwargs):
            thread = Thread(*args, **kwargs)
            threads.append(thread)
            return thread

        class BatchFinished(Exception):
            pass

        async def finish_one(*args, **kwargs):
            await process_batch(*args, **kwargs)
            raise BatchFinished

        def boot(state, *options):
            argv = ["lantern", "--state", str(state), "--codex", binary, "--private-lightsail-cli",
                    "--heritage-dir", str(state / "heritage"), "worker", *options]
            with patch.object(sys, "argv", argv), self.assertRaises(BatchFinished):
                asyncio.run(asyncio.wait_for(main(arguments()), 40))
            for thread in threads:
                thread.join(timeout=6)
                self.assertFalse(thread.is_alive())
            threads.clear()

        with TemporaryDirectory(prefix="lantern-native-independent-") as tmp:
            state = Path(tmp).resolve()
            (state / "heritage").mkdir()
            (state / "heritage" / "Lantern.md").write_text("Private persona fixture.", encoding="utf-8")
            store = ChatStore(state)
            store.submit(operator_text)
            with patch.dict(os.environ, OPENROUTER_API_KEY="offline", OPENAI_API_KEY="offline",
                            ANTHROPIC_API_KEY="offline", LANTERN_REVIEW_DAILY_USD="5"), \
                 patch("lantern.transport._open_upstream", provider), \
                 patch("lantern.review_vendors.request_json", side_effect=reviewer), \
                 patch("lantern.chat_worker.process_batch", finish_one), \
                 patch("lantern.__main__.Thread", settlement), redirect_stderr(StringIO()):
                with patch("lantern.codex_session.BASE_INSTRUCTIONS", OLD_BASE):
                    boot(state)
                tid = store.status()["thread_id"]
                self.assertTrue(tid)
                self.assertEqual(store.recent(1)[0].text, old_answer)

                earlier = datetime.now(timezone.utc) - timedelta(hours=2)
                ChatStore(state, clock=lambda: earlier).initialize_heartbeat(60)
                # The fake provider reports 140 tokens per request. Cross that
                # boundary as the existing room resumes with workspace tools.
                with patch("lantern.budget.HANDOFF_REMINDER_TOKENS", 140):
                    boot(state, "--heartbeat-minutes", "60")
                self.assertEqual(store.status()["thread_id"], tid)
                self.assertEqual([e.role for e in store.events()], ["operator", "lantern", "system", "lantern"])
                self.assertEqual(store.recent(1)[0].text, new_answer)
                self.assertEqual((state / "workspace" / "files" / filename).read_text(encoding="utf-8"), note)
                with closing(sqlite3.connect(state / "usage.sqlite")) as db:
                    calls = db.execute("""SELECT thread,batch_id,trigger,input_tokens,output_tokens,actual
                                          FROM request_details JOIN requests USING(id)
                                          ORDER BY request_details.rowid""").fetchall()
                self.assertEqual(calls, [(tid, 1, "operator", 100, 40, 10000)]
                                 + [(tid, 2, "heartbeat", 100, 40, 10000)] * 4)
                with closing(sqlite3.connect(state / "review-costs.sqlite")) as db:
                    reviews_by_batch = db.execute("""SELECT thread_id,batch_id,vendor,purpose,tool,tool_call_id FROM calls
                                                     ORDER BY rowid""").fetchall()
                self.assertEqual(reviews_by_batch, [(tid, 1, "luna", "spot", None, None)]
                                 + [(tid, 2, "luna", "tool", name, f"call_{n}")
                                    for n, (name, _) in enumerate(actions, 2)])

                def reviewed_run(socket_path, args):
                    self.assertEqual(reviews[-1][0], "coding_run")
                    self.assertEqual(socket_path, state / "broker.sock")
                    self.assertEqual(args, {"command": "python -V"})
                    return {"exit_code": 0, "output": "Python offline-coding-fixture"}

                store.submit("Check Python from the coding environment.")
                gh_calls = []
                def github(socket_path, op, args):
                    self.assertEqual(socket_path, state / "github.sock")
                    gh_calls.append(op)
                    if op == "open_draft":
                        self.assertEqual(reviews[-1][0], "github_open_draft")
                        self.assertEqual(args, {"proposal_id": proposal["proposal_id"]})
                        return {"proposal_id": proposal["proposal_id"], "draft": True}
                    if op == "evidence":
                        self.assertEqual(args, {"proposal_id": proposal["proposal_id"][:12]})
                    return proposal

                grants = FileGrants(state)
                grants.propose("oceans", "tidal observations", [
                    {"url": "https://example.org/tides.csv", "filename": "tides.csv"}])
                grants.decide(1, "allow_once")
                spool = state / "test-objects"
                spool.mkdir()

                def download(url, redirects, destination, limit):
                    destination.write_bytes(b"hour,height\n0,21\n")
                    return url

                with patch.dict(os.environ, LANTERN_FILE_IMPORTS="1"), \
                        patch("lantern.file_imports.OBJECTS", spool), \
                        patch("lantern.file_imports.fetch_import", side_effect=download), \
                        patch("lantern.file_imports.deliver_import",
                              return_value={"status": "imported", "path": "/work/imports/tides.csv"}), \
                        patch("lantern.coding_tools.run_code", side_effect=reviewed_run) as runner, \
                        patch("lantern.github_tools._call", side_effect=github):
                    boot(state, "--heartbeat-minutes", "60", "--coding-socket", str(state / "broker.sock"),
                         "--github-socket", str(state / "github.sock"))
                    runner.assert_called_once()
                self.assertEqual(gh_calls, ["preview", "evidence", "open_draft"])
                self.assertEqual(store.status()["thread_id"], tid)
                self.assertEqual(len(requests), 14)
                self.assertEqual(grants.status(1)["status"], "fulfilled")
                self.assertEqual((state / "workspace" / "files" / filename).read_text(encoding="utf-8"),
                                 "Fixture journal: the pawn endgame is next.\nQueued.")
                self.assertTrue({"coding_run", "github_sync", "github_preview", "github_open_draft",
                                 "file_import_status", "request_file_import", "import_file"} <=
                                {tool["name"] for tool in requests[5]["tools"]})
                schemas = {tool["name"]: tool["parameters"] for tool in requests[5]["tools"]}
                self.assertEqual(set(schemas["workspace_append"]["required"]), {"path", "text", "rationale"})
                self.assertEqual(set(schemas["workspace_replace"]["required"]), {"path", "find", "replace", "rationale"})
                for name, _ in journal_edits:
                    self.assertIn("expected_sha256", schemas[name]["properties"])
                self.assertIn("administrator sign-in or his SSH operator link", requests[5]["instructions"])
                self.assertIn("Python offline-coding-fixture", json.dumps(requests[-1]["input"]))
                self.assertIn("private journal is saved", json.dumps(requests[-1]["input"]))
                diagnostic = next(item for item in requests[-1]["input"] if item.get("type") == "function_call_output"
                                  and item["call_id"] == "call_7")
                self.assertIn("at_utc", json.dumps(diagnostic["output"]))

                store.submit("Keep going in the same conversation with independent work disabled.")
                boot(state)
                self.assertEqual(store.status()["thread_id"], tid)
                self.assertEqual(store.recent(1)[0].text, new_answer)
                self.assertEqual({t["name"] for t in requests[-1]["tools"]},
                                 {"chess_analyze", "read_heritage", "message_operator", "consult_opus"})
                prior = {i["name"] for i in requests[-1]["input"] if i.get("type") == "function_call"}
                self.assertTrue({"workspace_read", "workspace_append", "workspace_replace", "coding_run",
                                 "harness_logs", "github_open_draft"}.issubset(prior))

        requests = requests[:5]
        self.assertEqual(["notes/handoff.md" in request["instructions"] for request in requests],
                         [False, True, False, False, False])
        previous = {tool["name"] for tool in requests[0]["tools"]}
        self.assertEqual(previous, {"read_heritage", "consult_opus", "chess_analyze", "message_operator"})
        def instructions(request):
            history = [item for item in request["input"] if item.get("role") in ("developer", "system")]
            return request.get("instructions", "") + "\n" + json.dumps(history)

        self.assertIn("You cannot write a notes file", instructions(requests[0]))
        scheduled_marker = "opening input of THIS turn was delivered by the host scheduler"
        self.assertNotIn(scheduled_marker, requests[0]["instructions"])
        for request in requests[1:]:
            tools = {tool["name"] for tool in request["tools"]}
            self.assertEqual(tools, previous | {"workspace_write", "workspace_append", "workspace_replace",
                                                "workspace_list", "workspace_read", "heartbeat",
                                                "set_alarm", "cancel_alarm", "schedule_status", "web_search", "read_web_page", "budget_status",
                                                "harness_logs", "set_provider_route", "provider_stats"})
            self.assertNotIn("You cannot write a notes file", instructions(request))
            self.assertIn(scheduled_marker, request["instructions"])
            history = json.dumps(request["input"])
            for content in (operator_text, old_answer, "Scheduled heartbeat.", "Private persona fixture."):
                self.assertIn(content, history)
        outputs = {item["call_id"]: json.dumps(item["output"]) for item in requests[-1]["input"]
                   if item.get("type") == "function_call_output"}
        self.assertIn("sha256_prefix", outputs["call_2"])
        self.assertIn("sha256_prefix", outputs["call_3"])
        self.assertIn(note, outputs["call_3"])
        self.assertIn("seconds_until_reset", outputs["call_4"])
        self.assertIn("reserved_usd", outputs["call_4"])
        self.assertEqual([tool for tool, _ in reviews], [name for name, _ in actions]
                         + ["coding_run", "harness_logs", "github_preview", "github_open_draft"]
                         + [name for name, _ in journal_edits] + ["file_import_status", "import_file"])
        self.assertTrue(all(scheduled_marker in instructions for _, instructions in reviews[:len(actions)]))
