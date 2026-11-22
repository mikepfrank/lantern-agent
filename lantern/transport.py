"""Loopback Responses API gateway for Lantern's reviewed operator conversation."""

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from functools import cache
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
from .local_time import central_time
from .routing import ROUTES, TOOL_PROVIDER_EXCLUSIONS, safe_provider


MODEL = "qwen/qwen3.8-27b"
PRIVATE_CLI_OPERATOR = (
    "Michael P. Frank (Mike) is Lantern's developer and the system administrator of this "
    "Lightsail host. During this private deployment he is the only human user of the host "
    "and the only person with interactive access to Lantern through its SSH CLI and private "
    "browser room. The room authenticates Mike through administrator sign-in or his SSH "
    "operator link. Both interfaces show the same conversation. When a human speaks directly "
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
MAX_REQUEST_BYTES = 2 * 1024 * 1024
MAX_EVENT_BYTES = 2 * 1024 * 1024
UPSTREAM_IDLE_SECONDS = 120
UPSTREAM_DEADLINE_SECONDS = 300
TERMINAL_EVENTS = {"response.completed", "response.failed", "response.incomplete"}
GENERATED_DELTAS = {
    "response.output_text.delta", "response.reasoning_text.delta", "response.reasoning_summary_text.delta",
    "response.reasoning.delta", "response.refusal.delta", "response.function_call_arguments.delta",
}
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


def _prepare_input(items, names, image_loader, page_image_loader, reviewed_calls=None, *, image_by_digest=None):
    if isinstance(items, str):
        return items
    if not isinstance(items, list):
        raise ProtocolError("expected a stateless conversation")
    if image_by_digest is None and image_loader is not None:
        # Older single-image callers locate their only candidate by event ID.
        def image_by_digest(event_id, digest):
            return image_loader(event_id)

    images = []
    latest_user = None
    page_calls = set()
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
            page_call = item.get("call_id") if kind == "function_call_output" else None
            for position, part in enumerate(content):
                if not isinstance(part, dict):
                    raise ProtocolError("invalid content part")
                if part.get("type") in ("input_text", "output_text") and isinstance(part.get("text"), str):
                    continue
                is_page = kind == "function_call_output"
                if (part.get("type") != "input_image" or set(part) - {"type", "image_url", "detail"}
                        or (is_page and (not page_image_loader or page_call not in page_calls))
                        or (not is_page and (not image_by_digest or item.get("role") != "user"))):
                    raise ProtocolError("unsupported content part")
                try:
                    event_id, digest = parse_reference(part.get("image_url"), page=is_page)
                except ValueError:
                    raise ProtocolError("unknown private image reference") from None
                detail = part.get("detail") or "auto"
                if detail not in ("auto", "low", "high", "original"):
                    raise ProtocolError("unsupported image detail")
                images.append((index, position, event_id, digest, "high" if detail == "original" else detail, page_call))
        elif kind == "function_call":
            if item.get("name") not in names and (reviewed_calls is None or
                    (item.get("name"), item.get("call_id")) not in reviewed_calls()):
                raise ProtocolError("unknown tool in conversation")
            if item.get("name") == "view_pdf_page" and isinstance(item.get("call_id"), str):
                page_calls.add(item["call_id"])
        elif kind != "reasoning":
            raise ProtocolError("unsupported conversation item")
    if not images:
        return items
    # Codex persists only short host references in its stateless history. Expand
    # recent images after validation, without exposing a fetchable URL upstream.
    result = list(items)
    for number, (index, position, event_id, digest, detail, page_call) in enumerate(images):
        field = "output" if page_call is not None else "content"
        source = f"rendered PDF page (image #{event_id})" if page_call is not None else f"operator image, message #{event_id}"
        if result[index] is items[index]:
            result[index] = {**items[index], field: list(items[index][field])}
        if number < len(images) - MAX_TURN_IMAGES:
            part = {"type": "input_text", "text": f"[Earlier {source}. "
                    "Its pixels are archived outside the current image window.]"}
        else:
            try:
                if page_call is not None:
                    data = page_image_loader(event_id, page_call)
                else:
                    data = image_by_digest(event_id, digest)
            except Exception as exc:
                raise ProtocolError("stored image is unavailable") from exc
            if (not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES
                    or not data.startswith(SIGNATURE) or hashlib.sha256(data).hexdigest() != digest):
                if index == latest_user or (page_call is not None and (latest_user is None or index > latest_user)):
                    raise ProtocolError("stored image does not match its reference")
                # A later turn can still discuss the text. Never present unrelated
                # or corrupt pixels, or silently imply that the reviewer saw them.
                part = {"type": "input_text", "text": f"[Earlier {source} "
                        "is unavailable. Its pixels could not be verified.]"}
            else:
                url = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
                part = {"type": "input_image", "image_url": url, "detail": detail}
        result[index][field][position] = part
    return result


def _prepare(payload, route, tools, *, resuming=False, managed_registry=False, reviewed_calls=None,
             image_loader=None, page_image_loader=None, image_by_digest=None):
    if not isinstance(payload, dict) or payload.get("model") != MODEL or payload.get("stream") is not True:
        raise ProtocolError("expected a streaming request for " + MODEL)
    if payload.get("previous_response_id") is not None:
        raise ProtocolError("full conversation history is required")
    names = {tool["name"] for tool in tools}
    # A retired call can remain in native history. Its room-specific receipt
    # permits replay as input only. Outputs and tool_choice use the active list.
    prepared_input = _prepare_input(payload.get("input"), names, image_loader, page_image_loader,
                                     reviewed_calls if resuming else None, image_by_digest=image_by_digest)
    incoming = payload.get("tools", [])
    if not isinstance(incoming, list) or any(not isinstance(tool, dict) for tool in incoming):
        raise ProtocolError("invalid tool declarations")
    declared = {t.get("name") for t in incoming if t.get("type") == "function"}
    # Legacy one-shot threads can predate supervision and the heritage reader.
    # Managed rooms reconcile the full active list before resuming.
    if (not managed_registry and resuming and "chess_analyze" in declared
            and (names - declared) <= {"consult_opus", "read_heritage"}):
        tools = [tool for tool in tools if tool["name"] in declared]
        names = {tool["name"] for tool in tools}
    if incoming and (missing := names - declared):
        # Report only names from the host's registry. Client declarations can
        # contain private data and aren't needed to diagnose a stale registry.
        raise ProtocolError("Codex did not advertise these configured Lantern tools: " + ", ".join(sorted(missing)))
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
        provider={"max_price": {
            "prompt": MAX_PROMPT_PRICE, "completion": MAX_COMPLETION_PRICE, "request": 0, "image": 0,
        }},
        tools=[{"type": "function", "name": tool["name"], "description": tool["description"],
                "parameters": tool["inputSchema"], "strict": False} for tool in tools if incoming],
        tool_choice=choice, parallel_tool_calls=False,
    )
    if ROUTES[route] is not None:
        forwarded["provider"]["sort"] = ROUTES[route]
    if forwarded["tools"] and choice != "none" and TOOL_PROVIDER_EXCLUSIONS:
        forwarded["provider"]["ignore"] = list(TOOL_PROVIDER_EXCLUSIONS)
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


def _with_request_context(items, offered_tools):
    """Add current host metadata beside the latest input, leaving stored history and its cacheable prefix intact."""
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    latest_user = next((i for i in range(len(items) - 1, -1, -1) if items[i].get("role") == "user"), 0)
    text = (
        f"Current time for the operator (US Central): {central_time()}. "
        "Use this local date and time for greetings and calendar references. "
        "Codex environment timestamps describe the server.")
    if offered_tools:
        text += "\nCallable tools for this request: " + ", ".join(offered_tools) + "."
        text += " This live list supersedes earlier descriptions of your capabilities."
        if "provider_stats" in offered_tools:
            text += (" When Mike asks about provider tool availability, verify it with one read-only "
                     "provider_stats call and report its result. It leaves your route in place.")
    context = {"role": "developer", "content": [{"type": "input_text", "text": text}]}
    return items[:latest_user] + [context] + items[latest_user:]


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


def _record_provider(response, metadata, evidence):
    provider = safe_provider(response.get("provider"))
    endpoints = metadata.get("endpoints") if isinstance(metadata, dict) else None
    available = endpoints.get("available") if isinstance(endpoints, dict) else None
    if isinstance(available, list):
        for endpoint in available:
            if isinstance(endpoint, dict) and endpoint.get("selected") is True:
                provider = safe_provider(endpoint.get("provider")) or provider
                break
    if provider:
        evidence["provider"] = provider


def _inspect(event, names, evidence):
    kind = event.get("type", "")
    response = event.get("response") or {}
    _record_provider(response, event.get("openrouter_metadata") or response.get("openrouter_metadata"), evidence)
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
    if kind not in TERMINAL_EVENTS:
        return
    incomplete = response.get("incomplete_details") or {}
    if isinstance(incomplete, dict) and incomplete.get("reason") == "max_output_tokens":
        evidence["output_limit_reached"] = True


def _has_visible_text(event):
    kind = event.get("type")
    if kind in ("response.output_text.delta", "response.output_text.done"):
        text = event.get("delta" if kind.endswith(".delta") else "text")
        return isinstance(text, str) and bool(text)
    if kind in ("response.content_part.added", "response.content_part.done"):
        parts = [event.get("part")]
    else:
        if kind in ("response.output_item.added", "response.output_item.done"):
            items = [event.get("item")]
        elif kind in TERMINAL_EVENTS:
            items = (event.get("response") or {}).get("output") or []
        else:
            return False
        parts = (part for item in items if isinstance(item, dict) and item.get("type") == "message"
                 for part in item.get("content") or [])
    return any(isinstance(part, dict) and part.get("type") == "output_text" and
               isinstance(part.get("text"), str) and part["text"] for part in parts)


def _record_usage(usage, evidence):
    if not isinstance(usage, dict):
        return None
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


def _inspect_nonstream_response(upstream, evidence, elapsed_ms):
    # Error bodies can contain billable usage. Read bounded JSON, retaining only evidence.
    limit = MAX_EVENT_BYTES if upstream.status == 200 else 16384
    try:
        raw = upstream.read(limit + 1)
        if len(raw) > limit or getattr(upstream, "length", None) not in (None, 0):
            return None, None
        evidence["response_end_ms"] = elapsed_ms()
        detail = json.loads(raw, parse_float=Decimal)
    except (ValueError, OSError, http.client.HTTPException):
        return None, None
    if not isinstance(detail, dict):
        return None, None
    response = detail.get("response", detail)
    if not isinstance(response, dict):
        return detail.get("error"), None
    metadata = detail.get("openrouter_metadata") or response.get("openrouter_metadata")
    _record_provider(response, metadata, evidence)
    return detail.get("error"), _record_usage(response.get("usage"), evidence)


class OpenRouterGateway:
    """A fresh local token for one Codex client. Reserve/settle are durable host callbacks.

    reserve(prepared_payload) must reserve before returning its ID. settle(id, Decimal)
    records confirmed USD. With finish supplied, the ledger records status, observed
    counters and any confirmed cost together. A lost stream or missing cost leaves the
    reservation intact. The gateway never forwards client headers to OpenRouter.
    """

    def __init__(self, api_key, *, route, tools, reserve, settle, observe, unregistered, resuming=False,
                 operator_context="", finish=None, handoff_due=None, image_loader=None, page_image_loader=None,
                 managed_registry=False, reviewed_calls=None, image_by_digest=None, route_source=None):
        if route not in ROUTES or not api_key:
            raise ValueError("a valid route and OpenRouter key are required")
        self.route = route
        self.route_source = route_source
        self.tools = list(tools)
        self.reserve, self.settle = reserve, settle
        self.finish = finish
        self.observe, self.resuming = observe, resuming
        self.unregistered = unregistered
        self.operator_context = operator_context
        self.handoff_due = handoff_due
        self.image_loader = image_loader
        self.image_by_digest = image_by_digest
        self.page_image_loader = page_image_loader
        self.managed_registry = managed_registry
        # Historical lookup is needed only when replaying a retired tool.
        # The configured tools stay fixed for this one gateway/turn.
        self.reviewed_calls = cache(reviewed_calls) if reviewed_calls is not None else None
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
                evidence = {"model": MODEL, "status": "rejected"}
                gateway.evidence.append(evidence)
                # One snapshot belongs to this request, including its billing
                # evidence when a concurrent tool call changes the room setting.
                try:
                    route = gateway.route_source() if gateway.route_source is not None else gateway.route
                    evidence.update(route=route, sort=ROUTES[route])
                except Exception:
                    evidence["error"] = "provider routing preference unavailable"
                    return self.reject_unread(503, evidence["error"])
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= MAX_REQUEST_BYTES:
                        evidence["error"] = "invalid request size"
                        return self.reject_unread(413, "invalid request size")
                    prepared = _prepare(json.loads(self.rfile.read(length)), route, gateway.tools,
                                        resuming=gateway.resuming, image_loader=gateway.image_loader,
                                        image_by_digest=gateway.image_by_digest,
                                        page_image_loader=gateway.page_image_loader,
                                        managed_registry=gateway.managed_registry, reviewed_calls=gateway.reviewed_calls)
                    if gateway.operator_context:
                        # Apply current channel facts even when Codex resumes an older thread.
                        instructions = prepared.get("instructions") or ""
                        prepared["instructions"] = (gateway.operator_context + "\n\n" + instructions).rstrip()
                    if gateway.handoff_due is not None:
                        event = _handoff_event(prepared, gateway.handoff_due)
                        if event:
                            evidence["handoff_event"] = event
                    # Only names from the host-built wire schemas are logged or repeated to the model.
                    # A tool-free compaction has its own empty registry.
                    offered = [tool["name"] for tool in prepared["tools"]]
                    choice = prepared["tool_choice"]
                    if choice == "none":
                        callable_now = []
                    elif isinstance(choice, dict):
                        callable_now = [choice["name"]]
                    else:
                        callable_now = offered
                    prepared["input"] = _with_request_context(prepared["input"],
                                                              callable_now if gateway.managed_registry else [])
                    evidence["offered_tools"] = offered
                    evidence["tool_choice"] = choice
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
                    if cost is not None:
                        evidence["cost_usd"] = str(cost)
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

                def elapsed_ms():
                    return round((time.monotonic() - dispatched) * 1000)

                terminal_cost = None
                try:
                    evidence["dispatch_at"] = datetime.now(timezone.utc).isoformat()
                    dispatched = time.monotonic()
                    with _open_upstream(body, headers) as upstream:
                        evidence["response_start_ms"] = elapsed_ms()
                        evidence["http_status"] = upstream.status
                        media_type = upstream.getheader("Content-Type", "").split(";")[0].strip().lower()
                        if upstream.status != 200 or media_type != "text/event-stream":
                            error, cost = _inspect_nonstream_response(upstream, evidence, elapsed_ms)
                            source = "openrouter" if cost is not None else None
                            if upstream.status != 200:
                                evidence["status"] = "upstream_http_error"
                                _classify_error(error, evidence)
                                if cost is None and _no_eligible_endpoint(upstream.status, error):
                                    cost, source = Decimal(0), "router_no_eligible_endpoint"
                                    evidence["settlement_reason"] = source
                                status = upstream.status if 400 <= upstream.status < 600 else 502
                                message = "OpenRouter declined the request"
                            else:
                                evidence["status"] = "invalid_upstream_response"
                                status, message = 502, "expected OpenRouter event stream"
                                evidence["error"] = message
                            meter(evidence["status"], cost, source)
                            return deliver(self.fail, status, message)
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        deliver(self.end_headers)
                        started = True
                        names = {tool["name"] for tool in prepared["tools"]}
                        for frame, event in _frames(upstream):
                            kind = event.get("type")
                            terminal = kind in TERMINAL_EVENTS
                            received_ms = elapsed_ms()
                            if terminal:
                                evidence["response_end_ms"] = received_ms
                                response = event.get("response")
                                if isinstance(response, dict):
                                    # A refused output can still carry a genuine billable charge.
                                    terminal_cost = _record_usage(response.get("usage"), evidence)
                            _inspect(event, names, evidence)
                            if kind in GENERATED_DELTAS and "first_token_ms" not in evidence:
                                if isinstance(event.get("delta"), str) and event["delta"]:
                                    evidence["first_token_ms"] = received_ms
                            if "first_output_ms" not in evidence and _has_visible_text(event):
                                evidence["first_output_ms"] = received_ms
                            if terminal:
                                # Publish the audited status only after any
                                # confirmed cost has been durably recorded.
                                status = event["type"].partition(".")[2]
                                meter(status, terminal_cost, "openrouter" if terminal_cost is not None else None)
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
                    meter("interrupted", terminal_cost, "openrouter" if terminal_cost is not None else None)
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
