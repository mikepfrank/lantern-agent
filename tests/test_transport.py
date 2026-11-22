from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timedelta
from decimal import Decimal
import base64
import hashlib
from http.client import HTTPConnection
from io import BytesIO
import json
import socket
from socketserver import _SocketWriter
from threading import Event
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from lantern.transport import MODEL, OpenRouterGateway, _open_upstream
from lantern.image_ref import image_reference
from lantern.independent_tools import PROVIDER_STATS_TOOL
from lantern.routing import ROUTE_TOOL


CHESS = {"name": "chess", "description": "Inspect a position", "inputSchema": {"type": "object"}}


class Stream(BytesIO):
    status = 200

    def getheader(self, name, default=None):
        return "text/event-stream" if name == "Content-Type" else default


def event(kind, **fields):
    return b"data: " + json.dumps({"type": kind, **fields}).encode() + b"\n\n"


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.reserve = Mock(return_value=31)
        self.settle = Mock()
        self.observe = Mock()
        self.unregistered = Mock()
        self.wire = []
        self.http_status = 200
        self.stream = event("response.completed", response={"id": "r1", "output": [],
                            "usage": {"input_tokens": 100, "output_tokens": 3, "cost": .000041}})

        @contextmanager
        def upstream(body, headers):
            self.wire.append((json.loads(body), headers))
            response = Stream(self.stream)
            response.status = self.http_status
            yield response

        self.enterContext(patch("lantern.transport._open_upstream", upstream))

    def request(self, gateway, *, token=None, **fields):
        payload = {"model": MODEL, "tools": [{"type": "function", "name": "chess"},
                   {"type": "function", "name": "exec_command"}], "input": "A move?", "stream": True, **fields}
        connection = HTTPConnection("127.0.0.1", urlsplit(gateway.base_url).port, timeout=3)
        try:
            connection.request("POST", "/v1/responses", json.dumps(payload),
                               {"Authorization": "Bearer " + (token or gateway.token)})
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def gateway(self, route, *, tools=None, **kwargs):
        return OpenRouterGateway("upstream-test-key", route=route, tools=[CHESS] if tools is None else tools,
                                 reserve=self.reserve, settle=self.settle, observe=self.observe,
                                 unregistered=self.unregistered, **kwargs)

    def test_image_references_resolve_only_for_recent_operator_messages(self):
        png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGNg+N8AAAKCAYAQWoUWAAAAAElFTkSuQmCC"
        data = base64.b64decode(png)
        sha = hashlib.sha256(data).hexdigest()
        loader = Mock(return_value=data)
        items = [{"role": "user", "content": [{"type": "input_text", "text": f"Image {n}"},
                                              {"type": "input_image", "image_url": image_reference(n, sha),
                                               "detail": "original"}]}
                 for n in range(1, 7)]
        with self.gateway("cheapest", image_loader=loader) as gate:
            self.assertEqual(200, self.request(gate, input=items)[0])
        payload, _ = self.wire[0]
        self.assertEqual([args[0] for args, _ in loader.call_args_list], [3, 4, 5, 6])
        self.assertIn("archived", payload["input"][0]["content"][1]["text"])
        for item in [i for i in payload["input"] if i.get("role") == "user"][2:]:
            self.assertEqual(item["content"][1]["image_url"], "data:image/png;base64," + png)
            self.assertEqual(item["content"][1]["detail"], "high")
        self.assertEqual(self.observe.call_args.args[0], payload)
        self.assertEqual(0, payload["provider"]["max_price"]["image"])
        self.assertNotIn(png, str(gate.evidence))

    def test_wrong_image_source_digest_or_role_never_reaches_provider(self):
        ref = image_reference(1, "0" * 64)
        loader = Mock(return_value=None)
        cases = [("user", ref), ("assistant", ref), ("user", "https://outside.invalid/image.png"),
                 ("user", "file:///example"), ("user", "data:image/png;base64,Zm9v")]
        with self.gateway("fastest", image_loader=loader) as gate:
            for role, url in cases:
                with self.subTest(role=role, scheme=url.split(":")[0]):
                    item = {"role": role, "content": [{"type": "input_image", "image_url": url}]}
                    self.assertEqual(400, self.request(gate, input=[item])[0])
            loader.assert_called_once_with(1)
            loader.return_value = b"\x89PNG\r\n\x1a\nwrong"
            self.assertEqual(400, self.request(gate, input=[{"role": "user", "content": [
                {"type": "input_image", "image_url": ref}]}])[0])
        self.observe.assert_not_called()
        self.reserve.assert_not_called()
        self.assertEqual([], self.wire)

    def test_registry_mismatch_names_only_missing_host_tools_before_reservation(self):
        chess = {**CHESS, "name": "chess_analyze"}
        tools = [chess, {**CHESS, "name": "view_pdf_page"}, {**CHESS, "name": "coding_run"}]
        private_name = "untrusted_client_declaration"
        offered = [{"type": "function", "name": name} for name in ("chess_analyze", private_name)]
        with self.gateway("fastest", tools=tools, resuming=True) as gate:
            status, body = self.request(gate, tools=offered)
        self.assertEqual(status, 400)
        self.assertEqual(gate.evidence[-1]["error"],
                         "Codex did not advertise these configured Lantern tools: coding_run, view_pdf_page")
        self.assertNotIn(private_name, str(gate.evidence))
        self.assertNotIn(private_name.encode(), body)
        self.reserve.assert_not_called()
        self.observe.assert_not_called()
        self.assertFalse(self.wire)

    def test_unavailable_historical_pixels_do_not_strand_a_later_turn(self):
        original = {"role": "user", "content": [
            {"type": "input_image", "image_url": image_reference(12, "0" * 64)}]}
        history = [original, {"type": "function_call", "name": "chess", "arguments": "{}"},
                   {"type": "function_call_output", "output": "a tool result"}]
        with self.gateway("fastest", image_loader=lambda _: None) as gate:
            self.assertEqual(400, self.request(gate, input=history)[0])
            history.append({"role": "user", "content": "Give me the text account."})
            self.assertEqual(200, self.request(gate, tools=[], input=history)[0])
        part = self.wire[-1][0]["input"][0]["content"][0]
        self.assertEqual("input_text", part["type"])
        self.assertIn("could not be verified", part["text"])
        self.reserve.assert_called_once()
        self.assertEqual([], self.wire[-1][0]["tools"])

    def test_managed_worker_requires_its_complete_active_tools(self):
        names = ["chess_analyze", "read_heritage", "consult_opus"]
        tools = [{**CHESS, "name": n} for n in names]
        offered = [{"type": "function", "name": n} for n in names]
        with self.gateway("fastest", tools=tools, resuming=True, managed_registry=True) as gate:
            self.assertEqual(400, self.request(gate, tools=offered[:1])[0])
            self.reserve.assert_not_called()
            self.assertEqual(200, self.request(gate, tools=offered)[0])
        self.assertEqual([t["name"] for t in self.wire[-1][0]["tools"]], names)
        with self.gateway("fastest", tools=tools, resuming=True) as prototype:
            self.assertEqual(200, self.request(prototype, tools=offered[:1])[0])
        self.assertEqual([t["name"] for t in self.wire[-1][0]["tools"]], names[:1])

    def test_current_room_tool_reminder_matches_the_wire_and_dispatch_evidence(self):
        tools = [CHESS, ROUTE_TOOL, PROVIDER_STATS_TOOL]
        names = [t["name"] for t in tools]
        declared = [{"type": "function", "name": n} for n in [*names, "untrusted_client_tool"]]
        history = [{"role": "user", "content": "Earlier capabilities?"},
                   {"role": "assistant", "content": "The old list included obsolete_tool."}]
        question = {"role": "user", "content": "Which tools can I use now?"}
        with self.gateway("fastest", tools=tools, managed_registry=True, resuming=True) as gate:
            self.assertEqual(200, self.request(gate, input=history + [question], tools=declared)[0])
        sent = self.wire[0][0]
        self.assertEqual(sent["input"][:len(history)], history)
        self.assertEqual(sent["input"][-1], question)
        near_question = sent["input"][len(history):-1]
        reminders = [part["text"] for item in near_question if item.get("role") == "developer"
                     for part in item["content"] if "Callable tools for this request" in part.get("text", "")]
        self.assertEqual(len(reminders), 1)
        offered = [tool["name"] for tool in sent["tools"]]
        self.assertEqual(offered, names)
        self.assertCountEqual(gate.evidence[0]["offered_tools"], offered)
        for name in names:
            self.assertIn(name, reminders[0])
        for unoffered in ["obsolete_tool", "untrusted_client_tool"]:
            self.assertNotIn(unoffered, reminders[0])
        self.assertEqual(self.observe.call_args.args[0], sent)

    def test_tool_free_compaction_and_unmanaged_call_do_not_get_a_room_tool_reminder(self):
        for managed, native_tools in ((True, []), (False, [{"type": "function", "name": "chess"}])):
            with self.subTest(managed=managed), self.gateway("fastest", managed_registry=managed) as gate:
                self.assertEqual(200, self.request(gate, tools=native_tools)[0])
                sent = self.wire[-1][0]
                self.assertNotIn("Callable tools for this request", json.dumps(sent["input"]))
                if managed:
                    self.assertEqual([], sent["tools"])
                    self.assertEqual([], gate.evidence[0]["offered_tools"])

    def test_fresh_clock_precedes_latest_input_without_rewriting_history(self):
        history = [{"role": "user", "content": "Previous turn, at noon UTC."},
                   {"role": "assistant", "content": "Acknowledged."}]
        current = {"role": "user", "content": "What time is it here?"}
        calls = [{"type": "function_call", "name": "chess", "call_id": "one"},
                 {"type": "function_call_output", "call_id": "one", "output": "result"}]
        times = ["2026-11-21 11:50 CST (UTC-06:00)", "2026-11-21 12:01 CST (UTC-06:00)"]
        with patch("lantern.transport.central_time", side_effect=times), self.gateway("fastest", resuming=True) as gate:
            self.assertEqual(200, self.request(gate, input=history + [current] + calls)[0])
            steer = {"role": "user", "content": "One follow-up from Mike."}
            self.assertEqual(200, self.request(gate, input=history + [current] + calls + [steer])[0])
        for wire, prefix, suffix, stamp in zip(self.wire, [history, history + [current] + calls],
                                               [[current] + calls, [steer]], times):
            inputs = wire[0]["input"]
            self.assertEqual(inputs[:len(prefix)], prefix)
            self.assertEqual(inputs[len(prefix)]["role"], "developer")
            self.assertIn(stamp, inputs[len(prefix)]["content"][0]["text"])
            self.assertEqual(inputs[len(prefix) + 1:], suffix)

    def test_retired_call_receipts_cover_only_matching_inert_history(self):
        history = [{"type": "function_call", "name": "workspace_read", "call_id": "earlier"},
                   {"type": "function_call_output", "call_id": "earlier", "output": "from private notes"}]
        receipts = Mock(return_value={("workspace_read", "earlier")})
        with self.gateway("fastest", resuming=True, managed_registry=True, reviewed_calls=receipts) as gate:
            self.assertEqual(200, self.request(gate, input=history)[0])
            for changes in [{"call_id": "forged"}, {"name": "unknown"}]:
                self.assertEqual(400, self.request(gate, input=[{**history[0], **changes}, history[1]])[0])
            self.assertEqual(400, self.request(gate, input=history, tool_choice={"type": "function",
                                             "name": "workspace_read"})[0])
            self.stream = event("response.output_item.done", item=history[0])
            _, response = self.request(gate, input=history)
        receipts.assert_called_once()
        self.assertEqual(self.wire[0][0]["input"][1:], history)
        self.assertEqual([t["name"] for t in self.wire[0][0]["tools"]], ["chess"])
        self.assertNotIn(b"workspace_read", response)
        self.unregistered.assert_called_once_with("workspace_read", None)

    def test_provider_media_cannot_become_a_persistent_operator_attachment(self):
        part = {"type": "input_image", "image_url": "data:image/png;base64,dGVzdA=="}
        for bad_event in (event("response.content_part.added", part=part),
                          event("response.completed", response={"output": [{"type": "message", "content": [part]}]})):
            self.stream = bad_event
            with self.gateway("fastest") as gate:
                _, received = self.request(gate)
                self.assertNotIn(b"dGVzdA==", received)
                self.assertEqual("interrupted", gate.evidence[-1]["status"])
                self.assertEqual("unsupported provider message content", gate.evidence[-1]["error"])

    def test_handoff_reminder_is_a_reviewed_instruction_on_write_capable_requests(self):
        write_tool = {**CHESS, "name": "workspace_write"}
        declared = [{"type": "function", "name": tool["name"]} for tool in [CHESS, write_tool]]
        due = Mock(return_value=True)
        with self.gateway("fastest", tools=[CHESS, write_tool], handoff_due=due, finish=Mock(),
                          operator_context="Authenticated private operator.") as gate:
            def send(tools, choice="auto"):
                self.assertEqual(self.request(gate, tools=tools, tool_choice=choice,
                                              instructions="Pinned base instructions.")[0], 200)
                sent, _ = self.wire[-1]
                self.assertEqual(self.observe.call_args.args[0], sent)
                self.assertIn("Authenticated private operator.", sent["instructions"])
                self.assertIn("Pinned base instructions.", sent["instructions"])
                self.assertEqual(sent["input"][-1], {"role": "user", "content": "A move?"})
                return sent["instructions"]

            self.assertIn("notes/handoff.md", send(declared))
            self.assertEqual(gate.evidence[-1]["handoff_event"], "reminded")
            self.assertNotIn("notes/handoff.md", send([], "none"))
            self.assertEqual(gate.evidence[-1]["handoff_event"], "tool_free")
            self.assertNotIn("notes/handoff.md", send(declared, "none"))
            self.assertNotIn("notes/handoff.md", send(declared, {"type": "function", "name": "chess"}))
            due.return_value = False
            self.assertNotIn("notes/handoff.md", send(declared))

    def test_single_durable_observation_per_dispatch(self):
        finish = Mock()
        self.stream = event("response.completed", response={"usage": {"input_tokens": 1000, "output_tokens": 51,
                            "input_tokens_details": {"cached_tokens": 20, "cache_write_tokens": 0},
                            "output_tokens_details": {"reasoning_tokens": 35}, "cost": .001}})
        with self.gateway("fastest", finish=finish) as gate:
            self.assertEqual(self.request(gate)[0], 200)
            self.assertEqual(finish.call_args.args, (31, Decimal(".001")))
            kwargs = finish.call_args.kwargs
            self.assertEqual((kwargs["status"], kwargs["charge_source"]), ("completed", "openrouter"))
            self.assertGreaterEqual(kwargs["ms"], 0)
            self.assertEqual([kwargs["evidence"].get(k) for k in
                             ("input_tokens","output_tokens","cache_reads","cache_writes","reasoning_tokens")],
                             [1000, 51, 20, 0, 35])
            self.stream = event("response.incomplete", response={"usage": {"output_tokens": 7}})
            self.assertEqual(self.request(gate)[0], 200)
            self.assertEqual(finish.call_args.args, (31, None))
            self.assertEqual(finish.call_args.kwargs["status"], "incomplete")
            self.assertIsNone(finish.call_args.kwargs["charge_source"])
            self.observe.side_effect = RuntimeError("unavailable")
            self.assertEqual(self.request(gate)[0], 503)
            self.assertEqual(finish.call_args.args, (31, Decimal(0)))
            self.assertEqual(finish.call_args.kwargs["charge_source"], "context_review_blocked")
        self.assertEqual(finish.call_count, 3)
        self.settle.assert_not_called()

    def test_routing_accounting_and_canonical_tools(self):
        for route, sort in (("cheapest", "price"), ("fastest", "throughput"), ("default", None)):
            with self.subTest(route=route), self.gateway(route) as gate:
                status, received = self.request(gate, provider={"only": ["untrusted"]},
                                                plugins=[{"id": "web"}], store=True, max_output_tokens=8192)
                self.assertEqual((status, received), (200, self.stream))
                sent, headers = self.wire[-1]
                self.assertEqual(sent["max_output_tokens"], 32_768)
                if sort is None:
                    self.assertNotIn("sort", sent["provider"])
                else:
                    self.assertEqual(sent["provider"]["sort"], sort)
                self.assertEqual(gate.evidence[-1]["route"], route)
                self.assertEqual(sent["provider"]["max_price"]["request"], 0)
                self.assertNotIn("require_parameters", sent["provider"])
                self.assertNotIn("only", sent["provider"])
                self.assertNotIn("plugins", sent)
                self.assertFalse(sent["store"])
                self.assertEqual([t["name"] for t in sent["tools"]], ["chess"])
                self.assertEqual(headers["Authorization"], "Bearer upstream-test-key")
                self.reserve.assert_called_with(sent)
                self.settle.assert_called_with(31, Decimal("0.000041"))
                self.assertEqual(gate.evidence[-1]["status"], "completed")
                self.assertEqual(gate.evidence[-1]["input_tokens"], 100)

    def test_provider_timing_excludes_review_and_skips_reasoning_and_empty_text(self):
        now = 20
        finish = Mock()
        clock = self.enterContext(patch("lantern.transport.time", wraps=time))
        clock.monotonic.side_effect = lambda: now

        def review(prepared):
            nonlocal now
            now += 15

        original_frames = [
            event("response.created", response={"id": "r"}),
            event("response.reasoning_summary_text.delta", delta="thinking"),
            event("response.output_text.delta", delta=""),
            event("response.output_text.delta", delta="visible"),
            event("response.output_text.delta", delta="again"),
            event("response.completed", response={"usage": {"cost": 0}, "openrouter_metadata": {
                "endpoints": {"available": [
                    {"provider": "Ignored", "selected": False}, {"provider": "Valid Provider", "selected": True}
                ]}}}),
        ]

        class MeasuredStream(Stream):
            def readline(self, *args):
                nonlocal now
                line = super().readline(*args)
                if line.startswith(b"data:"):
                    now += 0.125
                return line

        @contextmanager
        def upstream(body, headers):
            nonlocal now
            now += 0.25
            yield MeasuredStream(b"".join(original_frames))

        self.observe.side_effect = review
        with patch("lantern.transport._open_upstream", upstream), self.gateway("fastest", finish=finish) as gate:
            self.assertEqual(self.request(gate)[0], 200)
        call = finish.call_args
        record = call.kwargs["evidence"]
        self.assertEqual(call.args, (31, Decimal(0)))
        self.assertEqual(call.kwargs["charge_source"], "openrouter")
        self.assertEqual(call.kwargs["ms"], 16000)
        self.assertEqual(record["provider"], "Valid Provider")
        self.assertEqual(record["response_start_ms"], 250)
        self.assertEqual(record["first_token_ms"], 500)
        self.assertEqual(record["first_output_ms"], 750)
        self.assertEqual(record["response_end_ms"], 1000)
        self.assertEqual(datetime.fromisoformat(record["dispatch_at"]).utcoffset(), timedelta(0))

    def test_first_visible_text_can_arrive_complete_without_observed_token_deltas(self):
        part = {"type": "output_text", "text": "answer"}
        item = {"type": "message", "content": [part]}
        completed = event("response.completed", response={"output": []})
        cases = [
            event("response.output_text.done", text="answer") + completed,
            event("response.content_part.done", part=part) + completed,
            event("response.output_item.done", item=item) + completed,
            event("response.completed", response={"output": [item]}),
        ]
        with self.gateway("fastest") as gate:
            for self.stream in cases:
                self.assertEqual(self.request(gate), (200, self.stream))
                record = gate.evidence[-1]
                self.assertGreaterEqual(record["first_output_ms"], record["response_start_ms"])
                self.assertLessEqual(record["first_output_ms"], record["response_end_ms"])
                self.assertNotIn("first_token_ms", record)
            for kind in ("response.refusal.delta", "response.function_call_arguments.delta",
                         "response.reasoning_text.delta"):
                self.stream = event(kind, delta="content") + completed
                self.assertEqual(self.request(gate), (200, self.stream))
                self.assertIn("first_token_ms", gate.evidence[-1])
                self.assertNotIn("first_output_ms", gate.evidence[-1])
            self.stream = event("response.output_text.delta", delta="") + completed
            self.assertEqual(self.request(gate)[0], 200)
            self.assertNotIn("first_token_ms", gate.evidence[-1])
            self.assertNotIn("first_output_ms", gate.evidence[-1])

    def test_rejected_terminal_frame_is_charged_without_relaying_its_contents(self):
        finish = Mock()
        self.stream = event("response.completed", response={"usage": {"cost": .000032},
                            "output": [{"type": "function_call", "name": "unregistered"}]})
        with self.gateway("fastest", finish=finish) as gate:
            self.assertEqual(self.request(gate), (200, b""))
        finish.assert_called_once()
        self.assertEqual(finish.call_args.args, (31, Decimal(".000032")))
        self.assertEqual(finish.call_args.kwargs["status"], "interrupted")
        self.assertEqual(finish.call_args.kwargs["charge_source"], "openrouter")
        self.assertIn("response_end_ms", gate.evidence[-1])
        self.assertNotIn("first_output_ms", gate.evidence[-1])
        self.unregistered.assert_called_once_with("unregistered", None)

    def test_nonstreaming_error_uses_confirmed_cost_and_distinguishes_incomplete_body(self):
        self.http_status = 429
        finish = Mock()
        with self.gateway("fastest", finish=finish) as gate:
            self.stream = json.dumps({"usage": {"cost": .00001}, "provider": "bad\nprovider"}).encode()
            self.assertEqual(self.request(gate)[0], 429)
            record = gate.evidence[-1]
            self.assertEqual(finish.call_args.args, (31, Decimal(".00001")))
            self.assertEqual(finish.call_args.kwargs["charge_source"], "openrouter")
            self.assertGreaterEqual(record["response_end_ms"], record["response_start_ms"])
            self.assertNotIn("first_output_ms", record)
            self.assertNotIn("provider", record)
            self.stream = b"x" * 18000
            self.assertEqual(self.request(gate)[0], 429)
            self.assertIsNone(finish.call_args.args[1])
            self.assertNotIn("response_end_ms", gate.evidence[-1])

    def test_room_route_switch_is_sampled_once_per_request_and_shares_spending_checks(self):
        source = Mock(side_effect=["fastest", "cheapest", "fastest"])
        self.reserve.side_effect = [31, 32, ValueError("cap")]
        finish = Mock()
        with self.gateway("fastest", route_source=source, finish=finish) as gate:
            for expected in (200, 200, 402):
                self.assertEqual(self.request(gate, provider={"sort": "latency"})[0], expected)
        self.assertEqual(source.call_count, 3)
        self.assertEqual([r["sort"] for r in gate.evidence], ["throughput", "price", "throughput"])
        self.assertEqual([r["route"] for r in gate.evidence], ["fastest", "cheapest", "fastest"])
        self.assertEqual([p["provider"]["sort"] for p, _ in self.wire], ["throughput", "price"])
        self.assertEqual(self.wire[0][0]["provider"]["max_price"], self.wire[1][0]["provider"]["max_price"])
        self.assertEqual([call.args[0]["provider"]["sort"] for call in self.reserve.call_args_list],
                         ["throughput", "price", "throughput"])
        self.assertEqual([(call.args[0], call.kwargs["evidence"]["sort"]) for call in finish.call_args_list],
                         [(31, "throughput"), (32, "price")])

    def test_unavailable_route_does_not_send_or_reserve(self):
        source = Mock(side_effect=[RuntimeError("private details"), "invalid", "cheapest"])
        with self.gateway("fastest", route_source=source) as gate:
            for _ in range(2):
                status, body = self.request(gate)
                self.assertEqual(status, 503)
                self.assertNotIn(b"private details", body)
                self.assertNotIn("sort", gate.evidence[-1])
            self.reserve.assert_not_called()
            self.assertFalse(self.wire)
            self.assertEqual(self.request(gate)[0], 200)
            self.assertEqual(self.wire[0][0]["provider"]["sort"], "price")

    def test_rejections_do_not_reach_provider(self):
        with self.gateway("cheapest") as gate:
            self.assertEqual(self.request(gate, token="wrong")[0], 401)
            self.assertEqual(self.request(gate, previous_response_id="r1")[0], 400)
            self.assertEqual(self.request(gate, model="other")[0], 400)
            self.assertEqual(self.request(gate, input=[{"role": "user", "content": [{"type": "input_image"}]}])[0], 400)
            self.reserve.assert_not_called()
            self.reserve.side_effect = ValueError("cap")
            self.assertEqual(self.request(gate)[0], 402)
            self.assertFalse(self.wire)

    def test_unregistered_upstream_call_is_never_relayed(self):
        self.stream = event("response.output_item.done", item={"type": "function_call", "name": "exec_command"})
        with self.gateway("fastest") as gate:
            _, received = self.request(gate)
            self.assertNotIn(b"exec_command", received)
            self.settle.assert_not_called()
            self.unregistered.assert_called_once_with("exec_command", None)

    def test_context_review_failure_prevents_qwen_charge(self):
        self.observe.side_effect = RuntimeError("unavailable")
        with self.gateway("cheapest") as gate:
            self.assertEqual(self.request(gate)[0], 503)
            self.assertFalse(self.wire)
            self.assertNotIn("dispatch_at", gate.evidence[-1])
            self.settle.assert_called_once_with(31, Decimal(0))

    def test_missing_cost_conserves_reservation(self):
        self.stream = event("response.completed", response={"id": "r2", "usage": {}})
        with self.gateway("cheapest") as gate:
            self.assertEqual(self.request(gate)[1], self.stream)
            self.reserve.assert_called_once()
            self.settle.assert_not_called()
            self.assertNotIn("cost_usd", gate.evidence[-1])
            self.assertNotIn("first_output_ms", gate.evidence[-1])

    def test_disconnected_client_cannot_hide_upstream_errors(self):
        write = _SocketWriter.write

        def disconnected(writer, data):
            if data.startswith(b"data:"):
                raise BrokenPipeError()
            return write(writer, data)

        first = event("response.created", response={"id": "r3"})
        denied = event("response.output_item.added", item={"type": "function_call", "name": "outside"})
        failed = event("response.failed", response={"error": {"code": "provider_error"}})
        with patch.object(_SocketWriter, "write", disconnected), self.gateway("cheapest") as gate:
            for ending, expected_status in [(b"", "interrupted"), (denied, "interrupted"), (failed, "failed")]:
                with self.subTest(ending=ending):
                    self.stream = first + ending
                    self.assertEqual(self.request(gate), (200, b""))
                    self.assertEqual(gate.evidence[-1]["status"], expected_status)
                    self.assertTrue(gate.evidence[-1]["client_disconnected"])
        self.unregistered.assert_called_once_with("outside", None)
        self.settle.assert_not_called()
        self.assertEqual(len(self.wire), 3)

    def test_gateway_exit_waits_for_pending_cost(self):
        release, waiting, closing = Event(), Event(), Event()

        class Delayed(Stream):
            def readline(self, *args):
                waiting.set()
                if not release.wait(5):
                    raise TimeoutError("Test did not release provider")
                return super().readline(*args)

        @contextmanager
        def upstream(body, headers):
            yield Delayed(self.stream)

        with patch("lantern.transport._open_upstream", upstream), ThreadPoolExecutor() as pool:
            gate = self.gateway("cheapest").__enter__()
            client = pool.submit(self.request, gate)
            self.assertTrue(waiting.wait(3))
            close_socket = gate._server.server_close

            def close():
                closing.set()
                close_socket()

            with patch.object(gate._server, "server_close", close):
                shutdown = pool.submit(gate.__exit__, None, None, None)
                try:
                    self.assertTrue(closing.wait(3))
                    with self.assertRaises(FutureTimeout):
                        shutdown.result(timeout=.1)
                finally:
                    release.set()
                shutdown.result(timeout=3)
            self.assertEqual(client.result(), (200, self.stream))
            self.settle.assert_called_once_with(31, Decimal("0.000041"))

    def test_acknowledged_steer_can_publish_before_original_headers_arrive(self):
        waiting, release = Event(), Event()
        self.reserve.side_effect = [31, 32]

        @contextmanager
        def upstream(body, headers):
            if json.loads(body)["input"][-1]["content"] == "original":
                waiting.set()
                if not release.wait(4):
                    raise TimeoutError("Test did not release provider")
            yield Stream(self.stream)

        with patch("lantern.transport._open_upstream", upstream), ThreadPoolExecutor() as pool, \
             self.gateway("cheapest") as gate:
            self.assertIsNone(gate.capture_steer())
            first = pool.submit(self.request, gate, input="original")
            try:
                self.assertTrue(waiting.wait(2))
                marker = gate.capture_steer()
                self.assertEqual(marker, 0)
                self.assertEqual(self.request(gate, input="amended")[0], 200)
                self.assertNotIn("client_disconnected", gate.evidence[0])
                with self.assertRaises(RuntimeError):
                    gate.assert_turn_complete()
                gate.confirm_steer(marker)
                gate.assert_turn_complete()
                self.settle.assert_called_once_with(32, Decimal("0.000041"))
                self.assertFalse(first.done())
            finally:
                release.set()
            self.assertEqual(first.result()[0], 200)
            gate.assert_turn_complete()
            self.assertEqual(self.settle.call_count, 2)
            self.settle.assert_called_with(31, Decimal("0.000041"))

    def test_steer_never_exempts_final_other_requests_or_validation_failures(self):
        gate = self.gateway("fastest")
        complete = {"status": "completed"}
        for records in (
            [],
            [{"status": "unconfirmed", "steer_acknowledged": True}],
            [{"status": "completed", "client_disconnected": True}],
            [{"status": "unconfirmed"}, {"status": "unconfirmed", "steer_acknowledged": True}, complete],
            [{"status": "unconfirmed", "client_disconnected": True}, complete],
            [{"status": "rejected", "steer_acknowledged": True}, complete],
            [{"status": "invalid_upstream_response", "steer_acknowledged": True}, complete],
            [{"status": "interrupted", "error": "provider returned an unregistered tool",
              "steer_acknowledged": True}, complete],
            [{"status": "interrupted", "error": "gateway or upstream failure", "steer_acknowledged": True}, complete],
        ):
            with self.subTest(records=records), self.assertRaises(RuntimeError):
                gate.evidence = records
                gate.assert_turn_complete()
        gate.evidence = [{"status": "interrupted", "upstream_transport_interrupted": True,
                          "steer_acknowledged": True}, complete]
        gate.assert_turn_complete()

    def test_only_router_parameter_rejection_releases_hold(self):
        self.http_status = 404
        message = ("No endpoints found that can handle the requested parameters. "
                   "To learn more about provider routing, visit: https://openrouter.ai/docs/guides/routing/provider-selection")
        error = {"code": 404, "message": message}
        self.stream = json.dumps({"error": error}).encode()
        with self.gateway("fastest") as gate:
            self.assertEqual(self.request(gate)[0], 404)
            self.settle.assert_called_once_with(31, Decimal(0))
            self.assertEqual(gate.evidence[-1]["settlement_reason"], "router_no_eligible_endpoint")
            self.assertEqual(gate.evidence[-1]["error_hints"], ["no_matching_endpoint", "parameters"])
            self.settle.reset_mock()
            for changed in ({**error, "metadata": {"provider_name": "some endpoint"}},
                            {**error, "message": message + " private input"}):
                self.stream = json.dumps({"error": changed}).encode()
                self.assertEqual(self.request(gate)[0], 404)
                self.assertNotIn("private input", json.dumps(gate.evidence))
            self.settle.assert_not_called()


class UpstreamDeadlineTests(unittest.TestCase):
    def test_keepalives_cannot_extend_absolute_deadline(self):
        upstream, peer = socket.socketpair()
        stop = Event()

        def keepalive():
            # More frequent than either timeout. Cap the fixture's lifetime
            # even if the transport were to ignore its deadline.
            for _ in range(100):
                if stop.wait(.025):
                    break
                try:
                    peer.sendall(b": keepalive\n\n")
                except OSError:
                    break

        with upstream, peer, ThreadPoolExecutor() as pool:
            upstream.settimeout(3)
            peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n: keepalive\n\n")
            connection = HTTPConnection("offline")
            with patch("lantern.transport.http.client.HTTPSConnection", return_value=connection), \
                 patch.object(connection, "connect", lambda: setattr(connection, "sock", upstream)), \
                 patch("lantern.transport.UPSTREAM_DEADLINE_SECONDS", .15):
                with _open_upstream(b"{}", {}) as response:
                    self.assertEqual(response.readline(), b": keepalive\n")
                    self.assertEqual(response.readline(), b"\n")
                    start = time.monotonic()
                    pool.submit(keepalive)
                    try:
                        while response.readline():
                            pass
                    except OSError:
                        pass  # Windows may report the timeout directly.
                    finally:
                        stop.set()
                    self.assertLess(time.monotonic() - start, 2)
