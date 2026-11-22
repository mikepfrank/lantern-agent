"""Exercise Lantern's supervised routing decisions through the native Codex worker."""

import asyncio
from contextlib import closing, contextmanager, redirect_stderr
from datetime import datetime, timedelta
from decimal import Decimal
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import sqlite3
from statistics import median
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
import unittest
from unittest.mock import patch

from lantern.__main__ import arguments, main
from lantern.chat_store import ChatStore
from lantern.chat_worker import process_batch
from lantern.usage_report import usage_report
from test_codex_smoke import reply


@unittest.skipUnless(os.environ.get("LANTERN_TEST_CODEX") == "1", "requires native Codex")
class NativeProviderRouting(unittest.TestCase):
    def test_heartbeat_switch_restart_and_denial(self):
        binary = os.environ.get("LANTERN_CODEX_BIN") or shutil.which("codex")
        self.assertTrue(binary, "Cannot find the native Codex executable")
        state = Path(self.enterContext(TemporaryDirectory()))
        store = ChatStore(state)
        requests, tool_reviews, settlements = [], [], []
        denial = "Keep the current route for this fixture."
        actions = {
            1: ("set_provider_route", {"route": "cheapest"}, "Save money on this idle wake."),
            2: ("budget_status", {}, "Check the remaining allowance."),
            4: ("set_provider_route", {"route": "fastest"}, "Prioritize interactive replies."),
            5: ("set_provider_route", {"route": "cheapest"}, "Exercise a denied route change."),
            6: ("provider_stats", {}, "Compare measured provider costs and response times."),
        }

        @contextmanager
        def provider(body, headers):
            requests.append(json.loads(body))
            n = len(requests)
            if n in actions:
                name, args, reason = actions[n]
                item = {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "status": "completed",
                        "name": name, "arguments": json.dumps({**args, "rationale": reason})}
            else:
                item = {"type": "message", "id": f"msg_{n}", "role": "assistant", "status": "completed",
                        "content": [{"type": "output_text", "text": "Routing check complete."}]}
            speed_first = requests[-1]["provider"]["sort"] == "throughput"
            yield reply(n, item, cost=0.00002 if speed_first else 0.00001,
                        provider="FixtureFast" if speed_first else "FixtureEconomy")

        def reviewer(vendor, key, body, **kwargs):
            payload = json.loads(body)
            usage = {"input_tokens": 72, "output_tokens": 20}
            if vendor == "opus":
                self.assertEqual(json.loads(payload["messages"][0]["content"])["case"]["rationale"], actions[5][2])
                return {"model": payload["model"], "stop_reason": "end_turn", "usage": usage,
                        "content": [{"type": "text", "text": f'<review decision="deny">{denial}</review>'}]}
            self.assertEqual(vendor, "luna")
            packet = json.loads(payload["input"][0]["content"])
            flag = packet.get("rationale") == actions[5][2]
            if packet["kind"] == "tool":
                tool_reviews.append((packet["tool"], packet["rationale"]))
            report = {"escalate": flag, "summary": "Synthetic routing test.",
                      "evidence": "This fixture asks for a denied change." if flag else ""}
            return {"model": payload["model"], "status": "completed", "usage": usage,
                    "output": [{"type": "message", "role": "assistant", "status": "completed",
                                "content": [{"type": "output_text", "text": json.dumps(report)}]}]}

        async def one_batch(state, store, turn, route, *, on_start, heartbeat_minutes, budget_check):
            on_start()
            store.initialize_heartbeat(heartbeat_minutes)
            batch = store.claim(budget_check=budget_check)
            self.assertIsNotNone(batch)
            await process_batch(store, batch, turn, route)

        def settlement(*args, **kwargs):
            t = Thread(*args, **kwargs)
            settlements.append(t)
            return t

        def boot():
            argv = ["lantern", "--state", str(state), "--codex", binary, "--private-lightsail-cli",
                    "worker", "--route", "fastest", "--heartbeat-minutes", "60"]
            with patch.object(sys, "argv", argv):
                asyncio.run(asyncio.wait_for(main(arguments()), 35))
            for t in settlements:
                t.join(8)
                self.assertFalse(t.is_alive())
            settlements.clear()
            self.assertEqual(store.recent(1)[0].text, "Routing check complete.")

        with (patch.dict(os.environ, OPENROUTER_API_KEY="offline", OPENAI_API_KEY="offline",
                         ANTHROPIC_API_KEY="offline"),
              patch("lantern.transport._open_upstream", provider),
              patch("lantern.review_vendors.request_json", side_effect=reviewer),
              patch("lantern.__main__.serve", one_batch), patch("lantern.__main__.Thread", settlement),
              patch("lantern.transport.time", wraps=time) as gateway_clock,
              redirect_stderr(StringIO())):
            # Give the two simulated endpoints different, deterministic speeds.
            elapsed = 0

            def measured_time():
                nonlocal elapsed
                economy = requests and requests[-1]["provider"]["sort"] == "price"
                elapsed += 30 if economy else 10
                return elapsed / 1000

            gateway_clock.monotonic.side_effect = measured_time
            boot()
            tid = store.status()["thread_id"]
            self.assertTrue(tid)
            self.assertIn("Scheduled heartbeat.", store.events()[0].text)
            store.submit("Use the faster routing preference for this conversation.")
            boot()
            self.assertEqual(store.status()["thread_id"], tid)

        self.assertEqual([q["provider"]["sort"] for q in requests],
                         ["throughput", "price", "price", "price", "throughput", "throughput", "throughput"])
        self.assertEqual(tool_reviews, [(tool, reason) for tool, _, reason in actions.values()])
        outputs = {item["call_id"]: json.dumps(item["output"]) for item in requests[-1]["input"]
                   if item.get("type") == "function_call_output"}
        for n, words in [(1, ["cheapest", "price"]), (2, ["cheapest", "price", "limit_usd"]),
                         (4, ["fastest", "throughput"]), (5, [denial])]:
            for word in words:
                self.assertIn(word, outputs[f"call_{n}"])
        self.assertTrue(all(q["provider"]["max_price"] == requests[0]["provider"]["max_price"] for q in requests))
        with closing(sqlite3.connect(state / "usage.sqlite")) as db:
            rows = db.execute("""SELECT d.thread,d.batch_id,d.trigger,r.reserved,r.actual FROM requests r
                JOIN request_details d USING(id) ORDER BY d.rowid""").fetchall()
            recorded = db.execute("""SELECT t.route FROM request_details d
                JOIN request_routes t ON t.request_id=d.id ORDER BY d.rowid""").fetchall()
        self.assertGreater(rows[0][3], 0)
        admission_and_batch = [(tid, 1, "heartbeat", rows[0][3])] * 3 + [(tid, 2, "operator", rows[0][3])] * 4
        self.assertEqual([row[:4] for row in rows], admission_and_batch)
        self.assertEqual([row[4] for row in rows], [20000, 10000, 10000, 10000, 20000, 20000, 20000])
        routes = ["fastest", "cheapest", "cheapest", "cheapest", "fastest", "fastest", "fastest"]
        self.assertEqual([row[0] for row in recorded], routes)

        report = usage_report(state, calls=True)
        calls = [entry for entry in report["calls"] if entry["model"] == "qwen"]
        self.assertEqual([entry["route"] for entry in calls], routes)
        for n, entry in enumerate(calls, 1):
            speed_first = entry["route"] == "fastest"
            self.assertEqual(entry["provider"], "FixtureFast" if speed_first else "FixtureEconomy")
            expected_cost = Decimal("0.00002" if speed_first else "0.00001")
            self.assertEqual(Decimal(entry["reported_cost_usd"]), expected_cost)
            self.assertEqual(Decimal(entry["settled_spend_usd"]), expected_cost)
            self.assertEqual(datetime.fromisoformat(entry["dispatch_at"]).utcoffset(), timedelta(0))
            self.assertGreater(entry["response_start_ms"], 0)
            self.assertGreater(entry["first_token_ms"], entry["response_start_ms"])
            self.assertGreater(entry["response_end_ms"], entry["first_token_ms"])
            if n in actions:  # Tool arguments do not count as visible model text.
                self.assertIsNone(entry["first_output_ms"])
            else:
                self.assertLess(entry["response_start_ms"], entry["first_output_ms"])
                self.assertLess(entry["first_output_ms"], entry["response_end_ms"])
        for route in set(routes):
            group = report["qwen_by_route"][route]
            matching = [entry for entry in calls if entry["route"] == route]
            for key in ("calls", "completed_calls", "reported_cost_calls", "throughput_samples"):
                self.assertEqual(group[key], len(matching))
            self.assertEqual(Decimal(group["reported_cost_usd"]),
                             sum(Decimal(entry["reported_cost_usd"]) for entry in matching))
            for field in ("response_start_ms", "first_token_ms", "first_output_ms", "response_end_ms"):
                times = [entry[field] for entry in matching if entry[field] is not None]
                actual = group["completed_timings"][field]
                self.assertEqual(actual["samples"], len(times))
                self.assertEqual(actual["median_ms"], median(times))
                self.assertEqual(actual["mean_ms"], round(sum(times) / len(times), 1))
            rate = round(1000 * 40 * len(matching) / sum(entry["response_end_ms"] for entry in matching), 2)
            self.assertEqual(group["effective_output_tokens_per_second"], rate)

        # The seventh request contains the exact tool result available to Lantern
        # while it chooses a route. Its own bill cannot be present in that result.
        raw_stats = next(item["output"] for item in requests[-1]["input"]
                         if item.get("type") == "function_call_output" and item["call_id"] == "call_6")
        stats = json.loads(raw_stats if isinstance(raw_stats, str) else raw_stats[0]["text"])
        self.assertEqual(stats["current_route"], "fastest")
        self.assertEqual((datetime.fromisoformat(stats["end_day"]) - datetime.fromisoformat(stats["start_day"])).days, 6)
        groups = stats["qwen_by_provider_and_route"]
        self.assertEqual({(g["provider"], g["route"]) for g in groups},
                         {("FixtureFast", "fastest"), ("FixtureEconomy", "cheapest")})
        for group in groups:
            route = group["route"]
            samples = [c for c in calls[:-1] if c["route"] == route]
            self.assertEqual(len(samples), 3)
            cost = sum(Decimal(c["reported_cost_usd"]) for c in samples)
            output_tokens = sum(c["output_tokens"] for c in samples)
            priced_tokens = sum(c["input_tokens"] + c["output_tokens"] for c in samples)
            decode_ms = sum(c["response_end_ms"] - c["first_token_ms"] for c in samples)
            for observed in (group, stats["qwen_by_route"][route]):
                self.assertEqual(Decimal(observed["reported_cost_usd"]), cost)
                self.assertEqual(observed["priced_token_samples"], len(samples))
                self.assertEqual(observed["priced_tokens"], priced_tokens)
                self.assertAlmostEqual(Decimal(observed["usd_per_million_tokens"]), cost * 1_000_000 / priced_tokens,
                                       places=6)
                self.assertEqual(observed["decoding_samples"], len(samples))
                self.assertEqual(observed["decoding_output_tokens_per_second"],
                                 round(1000 * (output_tokens - len(samples)) / decode_ms, 2))
                self.assertEqual(observed["completed_timings"]["first_token_ms"]["median_ms"],
                                 median(c["first_token_ms"] for c in samples))
        fastest, cheapest = (stats["qwen_by_route"][key] for key in ("fastest", "cheapest"))
        self.assertLess(fastest["completed_timings"]["first_token_ms"]["median_ms"],
                        cheapest["completed_timings"]["first_token_ms"]["median_ms"])
        self.assertGreater(fastest["decoding_output_tokens_per_second"], cheapest["decoding_output_tokens_per_second"])
        self.assertLess(Decimal(cheapest["usd_per_million_tokens"]), Decimal(fastest["usd_per_million_tokens"]))
