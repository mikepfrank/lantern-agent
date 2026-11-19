"""Daily-cap admission races through the worker, real HTTP gateway, supervisor and durable queue."""

import asyncio
from contextlib import contextmanager, redirect_stderr
from datetime import datetime, timezone
from decimal import Decimal
import http.client
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from lantern.__main__ import main
from lantern.budget import Budget, BudgetUnavailable
from lantern.chat_store import ChatStore
from lantern.chat_worker import process_batch
from lantern.codex_session import CodexError
from lantern.review_vendors import ReviewerVendors, ReviewBudgetUnavailable, VendorError
from lantern.scheduler import next_reset
from lantern.transport import MODEL
from test_codex_smoke import reply


class WorkerBudgetIntegration(unittest.TestCase):
    def exercise(self, failure, *, native_reports_completion=False):
        state = Path(self.enterContext(TemporaryDirectory()))
        store = ChatStore(state)
        event_id = store.submit("Operator input")
        now = datetime.now(timezone.utc)
        day = now.date().isoformat()
        requests = []
        paid = []
        notices = []
        real_reserve = Budget.reserve

        def reserve(ledger, payload, **attribution):
            # Simulate a concurrent reservation after the outer preflight succeeded.
            if failure == "qwen" or (failure == "after_effect" and paid):
                raise BudgetUnavailable(day)
            return real_reserve(ledger, payload, **attribution)

        def reviewer(vendors, vendor, payload, *, purpose, **attribution):
            if failure == "supervisor_context" or (failure == "supervisor_tool" and purpose == "tool"):
                raise ReviewBudgetUnavailable(day)
            if failure == "other_vendor_error":
                raise VendorError("A different vendor problem")
            report = json.dumps({"escalate": False, "evidence": "", "summary": "Routine test request."})
            return {"status": "completed", "output": [{"type": "message", "role": "assistant",
                    "status": "completed", "content": [{"type": "output_text", "text": report}]}]}

        @contextmanager
        def provider(body, headers):
            paid.append(json.loads(body))
            item = {"type": "function_call", "call_id": "one-call", "id": "fc_one",
                    "status": "completed", "name": "message_operator",
                    "arguments": json.dumps({"text": "Checkpoint", "rationale": "Report progress."})}
            yield reply(len(paid), item)

        async def native(**kw):
            kw["on_thread"]("fixture-thread")
            url = urlsplit(kw["base_url"])
            body = json.dumps({"model": MODEL, "stream": True, "input": "Operator input",
                               "tools": [{"type": "function", "name": t["name"]} for t in kw["tools"]]})

            def request():
                connection = http.client.HTTPConnection(url.hostname, url.port, timeout=5)
                try:
                    token = kw["env"]["LANTERN_GATEWAY_TOKEN"]
                    connection.request("POST", url.path + "/responses", body,
                                       {"Content-Type": "application/json", "Authorization": "Bearer " + token})
                    response = connection.getresponse()
                    response.read()
                    requests.append(response.status)
                    return response.status
                finally:
                    connection.close()

            if request() == 200:
                notices.append(kw["dispatch"]("one-call", "message_operator",
                                              {"text": "Checkpoint", "rationale": "Report progress."}))
                request()
            if native_reports_completion:
                return SimpleNamespace(text="Native reported completion")
            raise CodexError("Codex reported a provider or turn error")

        async def single_batch(state_path, chat, turn, route, *, budget_check, **options):
            batch = chat.claim(budget_check=budget_check)
            self.assertIsNotNone(batch)
            await process_batch(chat, batch, turn, route)

        args = SimpleNamespace(command="worker", state=state, codex="offline", persona=None, route="fastest",
                               private_lightsail_cli=True, heritage_dir=None, heartbeat_minutes=60)
        with patch.dict(os.environ, OPENROUTER_API_KEY="offline"), \
             patch("lantern.__main__.serve", single_batch), patch("lantern.__main__.run_turn", native), \
             patch.object(Budget, "reserve", reserve), patch.object(ReviewerVendors, "call", reviewer), \
             patch("lantern.transport._open_upstream", provider), redirect_stderr(StringIO()):
            asyncio.run(main(args))

        if failure == "other_vendor_error":
            self.assertIsNone(store.schedule_status()["budget_until"])
            self.assertEqual(0, store.status()["queued"])
            self.assertIn("failed", store.events()[-1].text)
            return
        self.assertEqual(next_reset(now).isoformat(), store.schedule_status()["budget_until"])
        self.assertEqual(1, store.status()["queued"])
        self.assertIsNone(store.claim())
        retry = ChatStore(state, clock=lambda: next_reset(now)).claim()
        self.assertIsNotNone(retry)
        if failure in {"qwen", "supervisor_context"}:
            self.assertEqual([event_id], [e.id for e in retry.messages])
            self.assertEqual([], paid)
            self.assertEqual([402] if failure == "qwen" else [503], requests)
            self.assertEqual(Decimal(0), Decimal(Budget(state / "usage.sqlite", Decimal(1)).today()["reserved_usd"]))
        else:
            self.assertEqual("system", retry.messages[0].role)
            self.assertIn("continuation", retry.messages[0].text)
            self.assertEqual(1, len(paid))
            self.assertEqual([200, 402], requests)
            if failure == "after_effect":
                self.assertIn("event_id", notices[0])
                self.assertEqual(1, sum(e.text == "Checkpoint" for e in store.events()))
            else:
                self.assertIn("held", notices[0]["error"])
                self.assertFalse(any(e.text == "Checkpoint" for e in store.events()))

    def test_qwen_and_initial_context_review_budget_requeue_original(self):
        for source in ("qwen", "supervisor_context"):
            with self.subTest(source=source):
                self.exercise(source)

    def test_later_qwen_or_tool_review_budget_keeps_progress_and_blocks_next_generation(self):
        for source in ("after_effect", "supervisor_tool"):
            with self.subTest(source=source):
                self.exercise(source)

    def test_gateway_audit_catches_native_completion_after_budget_rejection(self):
        self.exercise("after_effect", native_reports_completion=True)

    def test_vendor_errors_do_not_trigger_daily_hold(self):
        self.exercise("other_vendor_error")
