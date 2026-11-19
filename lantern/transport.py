"""Loopback, text-only Responses API gateway for the Lantern chess prototype."""

from contextlib import contextmanager
from decimal import Decimal
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import socket
from threading import Thread, Timer
import time


MODEL = "qwen/qwen3.8-27b"
MAX_OUTPUT_TOKENS = 8192
# USD per million tokens. Admission uses the same ceilings as the upstream filter.
MAX_PROMPT_PRICE = 1
MAX_COMPLETION_PRICE = 4
ROUTES = {"fastest": "throughput", "cheapest": "price"}
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
UPSTREAM_IDLE_SECONDS = 120
UPSTREAM_DEADLINE_SECONDS = 300
TERMINAL_EVENTS = {"response.completed", "response.failed", "response.incomplete"}


class ProtocolError(ValueError):
    """Only fixed local messages may be exposed in diagnostics."""


class UnregisteredCall(ProtocolError):
    def __init__(self, item):
        super().__init__("provider returned an unregistered tool")
        self.name = item.get("name")
        self.arguments = item.get("arguments")


def _check_input(items, names):
    if isinstance(items, str):
        return
    if not isinstance(items, list):
        raise ProtocolError("expected a stateless conversation")
    for item in items:
        if not isinstance(item, dict):
            raise ProtocolError("invalid conversation item")
        kind = item.get("type", "message")
        if kind in ("message", "function_call_output"):
            content = item.get("content") if kind == "message" else item.get("output")
            if isinstance(content, str):
                continue
            if not isinstance(content, list) or any(
                not isinstance(part, dict) or part.get("type") not in ("input_text", "output_text")
                or not isinstance(part.get("text"), str) for part in content
            ):
                raise ProtocolError("only text content is supported in this prototype")
        elif kind == "function_call":
            if item.get("name") not in names:
                raise ProtocolError("unknown tool in conversation")
        elif kind != "reasoning":
            raise ProtocolError("unsupported conversation item")


def _prepare(payload, route, tools, *, resuming=False):
    if not isinstance(payload, dict) or payload.get("model") != MODEL or payload.get("stream") is not True:
        raise ProtocolError("expected a streaming request for " + MODEL)
    if payload.get("previous_response_id") is not None:
        raise ProtocolError("full conversation history is required")
    names = {tool["name"] for tool in tools}
    _check_input(payload.get("input"), names)
    incoming = payload.get("tools", [])
    if not isinstance(incoming, list) or any(not isinstance(tool, dict) for tool in incoming):
        raise ProtocolError("invalid tool declarations")
    declared = {t.get("name") for t in incoming if t.get("type") == "function"}
    # Codex persists dynamic tool names at thread/start. Original chess-only
    # threads can still run their tool under the current review schema.
    if resuming and "chess_analyze" in declared and names - declared == {"consult_opus"}:
        tools = [tool for tool in tools if tool["name"] != "consult_opus"]
        names = {tool["name"] for tool in tools}
    if incoming and not names.issubset(declared):
        raise ProtocolError("Codex did not advertise all configured Lantern tools")
    choice = payload.get("tool_choice", "auto")
    if isinstance(choice, dict):
        if choice.get("type") != "function" or choice.get("name") not in names or not incoming:
            raise ProtocolError("requested an unregistered tool")
        choice = {"type": "function", "name": choice["name"]}
    elif choice not in ("auto", "required", "none"):
        raise ProtocolError("unsupported tool choice")
    forwarded = {k: payload[k] for k in ("instructions", "input", "reasoning", "include") if k in payload}
    forwarded.update(
        model=MODEL, stream=True, store=False, max_output_tokens=MAX_OUTPUT_TOKENS,
        provider={"sort": ROUTES[route], "max_price": {
            "prompt": MAX_PROMPT_PRICE, "completion": MAX_COMPLETION_PRICE, "request": 0, "image": 0,
        }},
        tools=[{"type": "function", "name": tool["name"], "description": tool["description"],
                "parameters": tool["inputSchema"], "strict": False} for tool in tools if incoming],
        tool_choice=choice, parallel_tool_calls=False,
    )
    return forwarded


@contextmanager
def _open_upstream(body, headers):
    deadline = time.monotonic() + UPSTREAM_DEADLINE_SECONDS
    connection = http.client.HTTPSConnection("openrouter.ai", timeout=UPSTREAM_IDLE_SECONDS)
    timer = None
    try:
        connection.connect()
        # Keep a reference: HTTPConnection may detach the stream's socket in
        # getresponse(). An idle timeout alone allows endless SSE keepalives.
        upstream_socket = connection.sock
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("OpenRouter connection deadline expired")
        upstream_socket.settimeout(min(UPSTREAM_IDLE_SECONDS, remaining))

        def expire():
            try:
                upstream_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = Timer(remaining, expire)
        timer.daemon = True
        timer.start()
        connection.request("POST", "/api/v1/responses", body=body, headers=headers)
        with connection.getresponse() as response:
            yield response
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        connection.close()


def _frames(upstream):
    lines = []
    size = 0
    while True:
        line = upstream.readline(MAX_EVENT_BYTES - size + 1)
        size += len(line)
        if size > MAX_EVENT_BYTES:
            raise ProtocolError("upstream event too large")
        if line and line.rstrip(b"\r\n"):
            lines.append(line)
            continue
        if lines:
            data = b"\n".join(row[5:].lstrip(b" ").rstrip(b"\r\n") for row in lines if row.startswith(b"data:"))
            event = json.loads(data, parse_float=Decimal) if data and data != b"[DONE]" else {}
            if not isinstance(event, dict):
                raise ProtocolError("invalid upstream event")
            yield b"".join(lines) + b"\n", event
        if not line:
            return
        lines, size = [], 0


def _classify_error(error, evidence):
    """Extract known categories only. Provider messages can quote the input."""
    if not isinstance(error, dict):
        return
    metadata = error.get("metadata") or {}
    known_codes = {"invalid_prompt", "invalid_request_error", "rate_limit_exceeded", "server",
                   "insufficient_credits", "model_not_found", "not_found", "provider_error",
                   "content_filter", "context_length_exceeded", "permission_denied"}
    for value in (error.get("code"), metadata.get("error_type") if isinstance(metadata, dict) else None):
        if isinstance(value, str) and value in known_codes:
            evidence["upstream_error_type"] = value
        elif type(value) is int and 400 <= value <= 599:
            evidence["upstream_error_code"] = value
    message = error.get("message")
    if isinstance(message, str):
        hints = {"no endpoints": "no_matching_endpoint", "no available provider": "no_matching_endpoint",
                 "data policy": "provider_data_policy", "privacy": "provider_privacy",
                 "parameter": "parameters", "tool use": "tools", "pricing": "price",
                 "max_price": "price", "context length": "context_length", "guardrail": "guardrail"}
        matches = sorted({label for substring, label in hints.items() if substring in message.lower()})
        if matches:
            evidence["error_hints"] = matches


def _no_eligible_endpoint(status, error):
    """Recognize the router's pre-generation parameter-selection failure only."""
    if status != 404 or not isinstance(error, dict) or error.get("code") not in (404, "not_found"):
        return False
    metadata = error.get("metadata") or {}
    if not isinstance(metadata, dict) or any(metadata.get(k) for k in ("provider_name", "provider_code", "raw")):
        return False
    message = error.get("message")
    known = "No endpoints found that can handle the requested parameters"
    docs = ". To learn more about provider routing, visit: https://openrouter.ai/docs/guides/routing/provider-selection"
    return message in (known, known + ".", known + docs)


def _inspect(event, names, evidence):
    kind = event.get("type", "")
    response = event.get("response") or {}
    items = list(response.get("output") or [])
    if "item" in event:
        items.append(event["item"])
    for item in items:
        if item.get("type") == "function_call":
            if item.get("name") not in names or item.get("namespace"):
                raise UnregisteredCall(item)
        elif item.get("type") not in ("message", "reasoning"):
            raise ProtocolError("unsupported provider output")
    if kind.startswith("response.function_call_arguments.") and "name" in event and event["name"] not in names:
        raise UnregisteredCall(event)
    if kind in ("error", "response.failed"):
        _classify_error(response.get("error") or event.get("error") or event, evidence)
    if response.get("id"):
        evidence["request_id"] = response["id"]
    if isinstance(response.get("model"), str):
        evidence["observed_model"] = response["model"]
    metadata = event.get("openrouter_metadata") or response.get("openrouter_metadata") or {}
    for endpoint in metadata.get("endpoints", {}).get("available", []):
        if endpoint.get("selected") is True:
            evidence["provider"] = endpoint.get("provider")
    if kind not in TERMINAL_EVENTS:
        return None
    evidence["status"] = kind.partition(".")[2]
    incomplete = response.get("incomplete_details") or {}
    if isinstance(incomplete, dict) and incomplete.get("reason") == "max_output_tokens":
        evidence["output_limit_reached"] = True
    usage = response.get("usage") or {}
    for name in ("input_tokens", "output_tokens"):
        if type(usage.get(name)) is int:
            evidence[name] = usage[name]
    cost = usage.get("cost")
    if type(cost) in (Decimal, int) and Decimal(cost).is_finite() and cost >= 0:
        return Decimal(cost)
    return None


class OpenRouterGateway:
    """A fresh local token for one Codex client. Reserve/settle are durable host callbacks.

    reserve(prepared_payload) must reserve before returning its ID. settle(id, Decimal)
    records confirmed USD. A provider failure, lost stream, or missing cost leaves the
    reservation intact. The gateway never forwards client headers to OpenRouter.
    """

    def __init__(self, api_key, *, route, tools, reserve, settle, observe, unregistered, resuming=False):
        if route not in ROUTES or not api_key:
            raise ValueError("a valid route and OpenRouter key are required")
        self.route = route
        self.tools = list(tools)
        self.reserve, self.settle = reserve, settle
        self.observe, self.resuming = observe, resuming
        self.unregistered = unregistered
        self._api_key = api_key
        self.token = secrets.token_urlsafe(32)
        self.evidence = []

    def __enter__(self):
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 15

            def log_message(self, *args):
                pass

            def fail(self, status, message):
                data = json.dumps({"error": {"message": message}}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                if self.path != "/v1/responses":
                    return self.fail(404, "unknown route")
                expected = "Bearer " + gateway.token
                if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
                    return self.fail(401, "unauthorized")
                evidence = {"model": MODEL, "sort": ROUTES[gateway.route], "status": "rejected"}
                gateway.evidence.append(evidence)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_REQUEST_BYTES:
                        evidence["error"] = "invalid request size"
                        return self.fail(413, "invalid request size")
                    prepared = _prepare(json.loads(self.rfile.read(length)), gateway.route, gateway.tools,
                                        resuming=gateway.resuming)
                    body = json.dumps(prepared, allow_nan=False, separators=(",", ":")).encode()
                except (KeyError, ValueError, TypeError, RecursionError, OSError) as exc:
                    evidence["error"] = str(exc) if isinstance(exc, ProtocolError) else "malformed request"
                    return self.fail(400, evidence["error"])
                try:
                    reservation = gateway.reserve(prepared)
                except Exception:
                    evidence["error"] = "budget reservation refused"
                    return self.fail(402, "budget reservation refused")
                try:
                    gateway.observe(prepared)
                except Exception:
                    gateway.settle(reservation, Decimal(0))
                    evidence["error"] = "required context review unavailable"
                    return self.fail(503, evidence["error"])
                evidence["status"] = "unconfirmed"
                headers = {"Authorization": "Bearer " + gateway._api_key, "Content-Type": "application/json",
                           "Accept": "text/event-stream", "Accept-Encoding": "identity",
                           "X-OpenRouter-Metadata": "enabled"}
                started = False

                def deliver(write, *args):
                    # Steering can cancel Codex's local HTTP read while the
                    # provider is still generating (and charging) this request.
                    if evidence.get("client_disconnected"):
                        return
                    try:
                        write(*args)
                    except OSError:
                        evidence["client_disconnected"] = True
                        self.close_connection = True

                try:
                    with _open_upstream(body, headers) as upstream:
                        evidence["http_status"] = upstream.status
                        if upstream.status != 200:
                            evidence["status"] = "upstream_http_error"
                            try:
                                detail = json.loads(upstream.read(16384))
                            except (ValueError, OSError):
                                detail = None
                            if isinstance(detail, dict):
                                error = detail.get("error")
                                _classify_error(error, evidence)
                                if _no_eligible_endpoint(upstream.status, error):
                                    gateway.settle(reservation, Decimal(0))
                                    evidence["cost_usd"] = "0"
                                    evidence["settlement_reason"] = "router_no_eligible_endpoint"
                            return deliver(self.fail, upstream.status if 400 <= upstream.status < 600 else 502,
                                           "OpenRouter declined the request")
                        if upstream.getheader("Content-Type", "").split(";")[0].strip().lower() != "text/event-stream":
                            return deliver(self.fail, 502, "expected OpenRouter event stream")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        deliver(self.end_headers)
                        started = True
                        names = {tool["name"] for tool in prepared["tools"]}
                        for frame, event in _frames(upstream):
                            cost = _inspect(event, names, evidence)
                            if cost is not None and "cost_usd" not in evidence:
                                gateway.settle(reservation, cost)
                                evidence["cost_usd"] = str(cost)
                            deliver(self.wfile.write, frame)
                            if event.get("type") in TERMINAL_EVENTS:
                                break
                        else:
                            raise ProtocolError("upstream stream ended before a terminal response")
                except Exception as exc:
                    evidence["error"] = str(exc) if isinstance(exc, ProtocolError) else "gateway or upstream failure"
                    if isinstance(exc, UnregisteredCall):
                        try:
                            gateway.unregistered(exc.name, exc.arguments)
                        except Exception:
                            evidence["review_error"] = "unregistered tool review unavailable"
                    if not started:
                        deliver(self.fail, 502, "OpenRouter connection failed")
                    else:
                        evidence["status"] = "interrupted"
                        self.close_connection = True

        # server_close() joins these handlers before callers inspect evidence.
        # A canceled client may already have returned a replacement response.
        class GatewayServer(ThreadingHTTPServer):
            daemon_threads = False

        self._server = GatewayServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_port}/v1"
        self._thread = Thread(target=self._server.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join()
