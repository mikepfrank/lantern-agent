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

    def gateway(self, route):
        return OpenRouterGateway("upstream-test-key", route=route, tools=[CHESS],
                                 reserve=self.reserve, settle=self.settle, observe=self.observe,
                                 unregistered=self.unregistered)

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
    def test_stalled_stream_is_closed_at_absolute_deadline(self):
        upstream, peer = socket.socketpair()
        with upstream, peer:
            upstream.settimeout(3)
            # Begin a valid, indefinite stream on a real socket. The peer
            # deliberately leaves it open even after the initial event.
            peer.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\n: keepalive\n\n")
            connection = HTTPConnection("offline")
            with patch("lantern.transport.http.client.HTTPSConnection", return_value=connection), \
                 patch.object(connection, "connect", lambda: setattr(connection, "sock", upstream)), \
                 patch("lantern.transport.UPSTREAM_DEADLINE_SECONDS", .15):
                with _open_upstream(b"{}", {}) as response:
                    self.assertEqual(response.readline(), b": keepalive\n")
                    self.assertEqual(response.readline(), b"\n")
                    start = time.monotonic()
                    try:
                        self.assertEqual(response.readline(), b"")
                    except OSError:
                        pass  # Windows may report the timeout directly.
                    self.assertLess(time.monotonic() - start, 2)
