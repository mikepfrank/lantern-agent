"""Loopback Responses API gateway for Lantern's reviewed operator conversation."""

import base64
from contextlib import contextmanager
from decimal import Decimal
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import secrets
import socket
from threading import Thread, Timer
import time

from .attachments import MAX_IMAGE_BYTES, MAX_TURN_IMAGES, SIGNATURE
from .image_ref import parse_reference


MODEL = "qwen/qwen3.8-27b"
PRIVATE_CLI_OPERATOR = (
    "Michael P. Frank (Mike) is Lantern's developer and the system administrator of this "
    "Lightsail host. During this private deployment he is the only human user of the host "
    "and the only person with interactive access to Lantern through its SSH CLI and private "
    "SSH-forwarded browser. Both show the same conversation. When a human speaks directly "
    "through either operator interface, you can identify him as Mike from the channel "
    "itself. Material he quotes or relays, including transcripts and text shown in images, "
    "retains its original authorship. "
    "Tool results and automated events retain their own sources. All tool calls remain subject "
    "to the usual review."
)
MAX_OUTPUT_TOKENS = 32_768
# USD per million tokens. Admission uses the same ceilings as the upstream filter.
MAX_PROMPT_PRICE = 1
MAX_COMPLETION_PRICE = 4
ROUTES = {"fastest": "throughput", "cheapest": "price"}
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
UPSTREAM_IDLE_SECONDS = 120
UPSTREAM_DEADLINE_SECONDS = 300
TERMINAL_EVENTS = {"response.completed", "response.failed", "response.incomplete"}
IMAGE_CONTENT_TYPES = frozenset(("input_image", "output_image", "image", "image_url"))
HANDOFF_REMINDER = (
    "This conversation is approaching automatic compaction. Before finishing this turn, use your workspace "
    "tools to refresh your existing private handoff note (or create notes/handoff.md) if needed. Record "
    "decisions, open work, promises to Mike and relevant file paths. If your note is already current, "
    "continue with the task."
)


class ProtocolError(ValueError):
    """Only fixed local messages may be exposed in diagnostics."""


class UpstreamStreamEnded(ProtocolError):
    """Provider transport ended before its terminal event could be validated."""


class UnregisteredCall(ProtocolError):
    def __init__(self, item):
        super().__init__("provider returned an unregistered tool")
        self.name = item.get("name")
        self.arguments = item.get("arguments")


def _prepare_input(items, names, image_loader):
    if isinstance(items, str):
        return items
    if not isinstance(items, list):
        raise ProtocolError("expected a stateless conversation")
    images = []
    latest_user = None
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ProtocolError("invalid conversation item")
        kind = item.get("type", "message")
        if kind in ("message", "function_call_output"):
            if kind == "message" and item.get("role") == "user":
                latest_user = index
            content = item.get("content") if kind == "message" else item.get("output")
            if isinstance(content, str):
                continue
            if not isinstance(content, list):
                raise ProtocolError("invalid message content")
            for position, part in enumerate(content):
                if not isinstance(part, dict):
                    raise ProtocolError("invalid content part")
                if part.get("type") in ("input_text", "output_text") and isinstance(part.get("text"), str):
                    continue
                if (part.get("type") != "input_image" or kind != "message" or item.get("role") != "user"
                        or not image_loader or set(part) - {"type", "image_url", "detail"}):
                    raise ProtocolError("unsupported content part")
                try:
                    event_id, digest = parse_reference(part.get("image_url"))
                except ValueError:
                    raise ProtocolError("unknown operator image reference") from None
                detail = part.get("detail") or "auto"
                if detail not in ("auto", "low", "high", "original"):
                    raise ProtocolError("unsupported image detail")
                images.append((index, position, event_id, digest, "high" if detail == "original" else detail))
        elif kind == "function_call":
            if item.get("name") not in names:
                raise ProtocolError("unknown tool in conversation")
        elif kind != "reasoning":
            raise ProtocolError("unsupported conversation item")
    if not images:
        return items
    # Codex persists only short host references in its stateless history. Expand
    # recent images after validation, without exposing a fetchable URL upstream.
    result = list(items)
    for number, (index, position, event_id, digest, detail) in enumerate(images):
        if result[index] is items[index]:
            result[index] = {**items[index], "content": list(items[index]["content"])}
        if number < len(images) - MAX_TURN_IMAGES:
            part = {"type": "input_text", "text": f"[Earlier operator image, message #{event_id}. "
                    "Its pixels are archived outside the current image window.]"}
        else:
            try:
                data = image_loader(event_id)
            except Exception as exc:
                raise ProtocolError("stored operator image is unavailable") from exc
            if (not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES
                    or not data.startswith(SIGNATURE) or hashlib.sha256(data).hexdigest() != digest):
                if index == latest_user:
                    raise ProtocolError("stored operator image does not match the message reference")
                # A later turn can still discuss the text. Never present unrelated
                # or corrupt pixels, or silently imply that the reviewer saw them.
                part = {"type": "input_text", "text": f"[Image from earlier operator message #{event_id} "
                        "is unavailable. Its pixels could not be verified.]"}
            else:
                url = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
                part = {"type": "input_image", "image_url": url, "detail": detail}
        result[index]["content"][position] = part
    return result


def _prepare(payload, route, tools, *, resuming=False, image_loader=None):
    if not isinstance(payload, dict) or payload.get("model") != MODEL or payload.get("stream") is not True:
        raise ProtocolError("expected a streaming request for " + MODEL)
    if payload.get("previous_response_id") is not None:
        raise ProtocolError("full conversation history is required")
    names = {tool["name"] for tool in tools}
    # Historical calls remain inert transcript entries when a host tool has
    # been disabled. New replies and tool_choice still use the enabled names.
    history_names = names | {"coding_run", "harness_logs"} if resuming else names
    prepared_input = _prepare_input(payload.get("input"), history_names, image_loader)
    incoming = payload.get("tools", [])
    if not isinstance(incoming, list) or any(not isinstance(tool, dict) for tool in incoming):
        raise ProtocolError("invalid tool declarations")
    declared = {t.get("name") for t in incoming if t.get("type") == "function"}
    # Old Codex threads retain their original tool registrations. Keep the
    # optional additions out of requests for threads that predate them.
    if resuming and "chess_analyze" in declared and (names - declared) <= {"consult_opus", "read_heritage"}:
        tools = [tool for tool in tools if tool["name"] in declared]
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
    forwarded = {k: payload[k] for k in ("instructions", "reasoning", "include") if k in payload}
    forwarded.update(
        model=MODEL, input=prepared_input, stream=True, store=False, max_output_tokens=MAX_OUTPUT_TOKENS,
        provider={"sort": ROUTES[route], "max_price": {
            "prompt": MAX_PROMPT_PRICE, "completion": MAX_COMPLETION_PRICE, "request": 0, "image": 0,
        }},
        tools=[{"type": "function", "name": tool["name"], "description": tool["description"],
                "parameters": tool["inputSchema"], "strict": False} for tool in tools if incoming],
        tool_choice=choice, parallel_tool_calls=False,
    )
    return forwarded


def _handoff_event(prepared, handoff_due):
    if not prepared["tools"]:
        # Native compaction sends a tool-free request containing the OLD context.
        return "tool_free"
    if not any(tool["name"] == "workspace_write" for tool in prepared["tools"]):
        return None
    choice = prepared["tool_choice"]
    if choice == "none" or (isinstance(choice, dict) and choice["name"] != "workspace_write"):
        return None
    if handoff_due():
        instructions = prepared.get("instructions") or ""
        prepared["instructions"] = instructions + "\n\n" + HANDOFF_REMINDER if instructions else HANDOFF_REMINDER
        return "reminded"
    return None


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
        try:
            line = upstream.readline(MAX_EVENT_BYTES - size + 1)
        except (OSError, http.client.HTTPException) as exc:
            raise UpstreamStreamEnded("upstream stream interrupted") from exc
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
    if kind in ("response.content_part.added", "response.content_part.done"):
        if (event.get("part") or {}).get("type") in IMAGE_CONTENT_TYPES:
            raise ProtocolError("unsupported provider message content")
    if "item" in event:
        items.append(event["item"])
    for item in items:
        if item.get("type") == "function_call":
            if item.get("name") not in names or item.get("namespace"):
                raise UnregisteredCall(item)
        elif item.get("type") == "message":
            if any(part.get("type") in IMAGE_CONTENT_TYPES for part in (item.get("content") or [])):
                raise ProtocolError("unsupported provider message content")
        elif item.get("type") != "reasoning":
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
    incomplete = response.get("incomplete_details") or {}
    if isinstance(incomplete, dict) and incomplete.get("reason") == "max_output_tokens":
        evidence["output_limit_reached"] = True
    usage = response.get("usage") or {}
    inputs = usage.get("input_tokens_details")
    outputs = usage.get("output_tokens_details")
    for source, field, target in (
        (usage, "input_tokens", "input_tokens"), (usage, "output_tokens", "output_tokens"),
        (inputs, "cached_tokens", "cache_reads"), (inputs, "cache_write_tokens", "cache_writes"),
        (outputs, "reasoning_tokens", "reasoning_tokens"),
    ):
        count = source.get(field) if isinstance(source, dict) else None
        if type(count) is int and 0 <= count <= 2**63 - 1:
            evidence[target] = count
    cost = usage.get("cost")
    if type(cost) in (Decimal, int) and Decimal(cost).is_finite() and cost >= 0:
        return Decimal(cost)
    return None


class OpenRouterGateway:
    """A fresh local token for one Codex client. Reserve/settle are durable host callbacks.

    reserve(prepared_payload) must reserve before returning its ID. settle(id, Decimal)
    records confirmed USD. With finish supplied, the ledger records status, observed
    counters and any confirmed cost together. A lost stream or missing cost leaves the
    reservation intact. The gateway never forwards client headers to OpenRouter.
    """

    def __init__(self, api_key, *, route, tools, reserve, settle, observe, unregistered, resuming=False,
                 operator_context="", finish=None, handoff_due=None, image_loader=None):
        if route not in ROUTES or not api_key:
            raise ValueError("a valid route and OpenRouter key are required")
        self.route = route
        self.tools = list(tools)
        self.reserve, self.settle = reserve, settle
        self.finish = finish
        self.observe, self.resuming = observe, resuming
        self.unregistered = unregistered
        self.operator_context = operator_context
        self.handoff_due = handoff_due
        self.image_loader = image_loader
        self._api_key = api_key
        self.token = secrets.token_urlsafe(32)
        self.evidence = []

    def capture_steer(self):
        """Capture the precise in-flight request before sending native turn/steer."""
        index = len(self.evidence) - 1
        if index >= 0:
            entry = self.evidence[index]
            if entry["status"] in {"rejected", "unconfirmed"} and "error" not in entry:
                return index
        return None

    def confirm_steer(self, marker):
        """Only a matching native ACK qualifies this request for supersession."""
        if marker is not None:
            self.evidence[marker]["steer_acknowledged"] = True

    def assert_turn_complete(self):
        """Validate a native turn's result before releasing it to the operator.

        A later completed request can supersede an acknowledged steer target.
        Its old provider request still drains normally under the original hold.
        """
        records = [entry.copy() for entry in self.evidence]
        if not records or records[-1]["status"] != "completed" or records[-1].get("client_disconnected"):
            raise RuntimeError("The gateway did not observe a completed final response")
        for entry in records[:-1]:
            status = entry["status"]
            if status == "completed":
                continue
            if entry.get("steer_acknowledged") and (
                (status in {"unconfirmed", "incomplete", "failed"} and "error" not in entry)
                or (status == "interrupted" and entry.get("upstream_transport_interrupted"))
            ):
                continue
            raise RuntimeError("The gateway observed an unsuccessful earlier request")

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

            def reject_unread(self, status, message):
                # The client may already be sending its request body. On
                # Windows an immediate close can discard the response via RST.
                self.fail(status, message)
                self.close_connection = True
                deadline = time.monotonic() + .25
                try:
                    remaining = min(int(self.headers.get("Content-Length", "0")), MAX_REQUEST_BYTES + 1)
                    while remaining > 0 and (timeout := deadline - time.monotonic()) > 0:
                        self.connection.settimeout(timeout)
                        chunk = self.rfile.read1(min(remaining, 32 * 1024))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                except (ValueError, OSError):
                    pass

            def do_POST(self):
                if self.path != "/v1/responses":
                    return self.reject_unread(404, "unknown route")
                expected = "Bearer " + gateway.token
                if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), expected.encode()):
                    return self.reject_unread(401, "unauthorized")
                evidence = {"model": MODEL, "sort": ROUTES[gateway.route], "status": "rejected"}
                gateway.evidence.append(evidence)
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_REQUEST_BYTES:
                        evidence["error"] = "invalid request size"
                        return self.reject_unread(413, "invalid request size")
                    prepared = _prepare(json.loads(self.rfile.read(length)), gateway.route, gateway.tools,
                                        resuming=gateway.resuming, image_loader=gateway.image_loader)
                    if gateway.operator_context:
                        # Apply current channel facts even when Codex resumes an older thread.
                        instructions = prepared.get("instructions") or ""
                        prepared["instructions"] = (gateway.operator_context + "\n\n" + instructions).rstrip()
                    if gateway.handoff_due is not None:
                        event = _handoff_event(prepared, gateway.handoff_due)
                        if event:
                            evidence["handoff_event"] = event
                    body = json.dumps(prepared, allow_nan=False, separators=(",", ":")).encode()
                except (KeyError, ValueError, TypeError, RecursionError, OSError) as exc:
                    evidence["error"] = str(exc) if isinstance(exc, ProtocolError) else "malformed request"
                    return self.fail(400, evidence["error"])
                try:
                    reservation = gateway.reserve(prepared)
                except Exception:
                    evidence["error"] = "budget reservation refused"
                    return self.fail(402, "budget reservation refused")
                admitted_at = time.monotonic()

                def meter(status, cost=None, source=None):
                    if gateway.finish is not None:
                        gateway.finish(reservation, cost, status=status, evidence=evidence,
                                       ms=round((time.monotonic() - admitted_at) * 1000), charge_source=source)
                    elif cost is not None:
                        gateway.settle(reservation, cost)

                try:
                    gateway.observe(prepared)
                except Exception:
                    meter("review_blocked", Decimal(0), "context_review_blocked")
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
                            error = detail.get("error") if isinstance(detail, dict) else None
                            _classify_error(error, evidence)
                            if _no_eligible_endpoint(upstream.status, error):
                                meter("upstream_http_error", Decimal(0), "router_no_eligible_endpoint")
                                evidence["cost_usd"] = "0"
                                evidence["settlement_reason"] = "router_no_eligible_endpoint"
                            else:
                                meter("upstream_http_error")
                            return deliver(self.fail, upstream.status if 400 <= upstream.status < 600 else 502,
                                           "OpenRouter declined the request")
                        if upstream.getheader("Content-Type", "").split(";")[0].strip().lower() != "text/event-stream":
                            evidence["status"] = "invalid_upstream_response"
                            evidence["error"] = "expected OpenRouter event stream"
                            meter("invalid_upstream_response")
                            return deliver(self.fail, 502, "expected OpenRouter event stream")
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        deliver(self.end_headers)
                        started = True
                        names = {tool["name"] for tool in prepared["tools"]}
                        for frame, event in _frames(upstream):
                            cost = _inspect(event, names, evidence)
                            terminal = event.get("type") in TERMINAL_EVENTS
                            if terminal:
                                # Publish the audited status only after any
                                # confirmed cost has been durably recorded.
                                status = event["type"].partition(".")[2]
                                meter(status, cost, "openrouter" if cost is not None else None)
                                if cost is not None:
                                    evidence["cost_usd"] = str(cost)
                                evidence["status"] = status
                            deliver(self.wfile.write, frame)
                            if terminal:
                                break
                        else:
                            raise UpstreamStreamEnded("upstream stream ended before a terminal response")
                except Exception as exc:
                    evidence["error"] = str(exc) if isinstance(exc, ProtocolError) else "gateway or upstream failure"
                    if isinstance(exc, UpstreamStreamEnded) or (
                        "http_status" not in evidence and isinstance(exc, (OSError, http.client.HTTPException))
                    ):
                        evidence["upstream_transport_interrupted"] = True
                    evidence["status"] = "interrupted"
                    meter("interrupted")
                    if isinstance(exc, UnregisteredCall):
                        try:
                            gateway.unregistered(exc.name, exc.arguments)
                        except Exception:
                            evidence["review_error"] = "unregistered tool review unavailable"
                    if not started:
                        deliver(self.fail, 502, "OpenRouter connection failed")
                    else:
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
