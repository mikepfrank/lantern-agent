from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from decimal import Decimal
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

    def gateway(self, route, **kwargs):
        return OpenRouterGateway("upstream-test-key", route=route, tools=[CHESS],
                                 reserve=self.reserve, settle=self.settle, observe=self.observe,
                                 unregistered=self.unregistered, **kwargs)

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
        for route, sort in (("cheapest", "price"), ("fastest", "throughput")):
            with self.subTest(route=route), self.gateway(route) as gate:
                status, received = self.request(gate, provider={"only": ["untrusted"]},
                                                plugins=[{"id": "web"}], store=True)
                self.assertEqual((status, received), (200, self.stream))
                sent, headers = self.wire[-1]
                self.assertEqual(sent["provider"]["sort"], sort)
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
            self.settle.assert_called_once_with(31, Decimal(0))

    def test_missing_cost_conserves_reservation(self):
        self.stream = event("response.completed", response={"id": "r2", "usage": {}})
        with self.gateway("cheapest") as gate:
            self.assertEqual(self.request(gate)[1], self.stream)
            self.reserve.assert_called_once()
            self.settle.assert_not_called()

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
            if json.loads(body)["input"] == "original":
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
