from contextlib import contextmanager
from decimal import Decimal
from http.client import HTTPConnection
from io import BytesIO
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from lantern.transport import MODEL, OpenRouterGateway


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
                                 reserve=self.reserve, settle=self.settle)

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

    def test_missing_cost_conserves_reservation(self):
        self.stream = event("response.completed", response={"id": "r2", "usage": {}})
        with self.gateway("cheapest") as gate:
            self.assertEqual(self.request(gate)[1], self.stream)
            self.reserve.assert_called_once()
            self.settle.assert_not_called()

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
