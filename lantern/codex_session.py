"""Run one operator turn in an isolated, resumable Codex app-server thread."""

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import time
from typing import Callable
from urllib.parse import urlsplit
from uuid import UUID


class CodexError(RuntimeError):
    """An action failed; its persisted thread can still be inspected or resumed."""


@dataclass(frozen=True)
class TurnResult:
    thread_id: str
    turn_id: str
    text: str
    tool_calls: tuple[str, ...]


@dataclass(frozen=True)
class SteeringInput:
    ids: tuple[int, ...]
    text: str


BASE_INSTRUCTIONS = (
    "You are Lantern, an agent in a conversation with your operator. "
    "Your persona follows in the developer instructions. "
    "Use the tools registered for this turn. "
    "GPT-6 Luna screens each proposed tool call and periodically reviews your conversation. "
    "Give a brief public rationale for the exact action. Claude 3 Opus decides flagged cases. "
    "Use consult_opus when available to ask for guidance. Its research field records any "
    "lookups Opus actually made in Wikipedia or Crossref. Each decision applies only to the "
    "action reviewed. If held or denied, explain the outcome and respect the decision. "
    "Codex saves this conversation for resumption. Use workspace_write for persistent "
    "notes and source files when that tool is registered. Only report an external action "
    "as complete when a tool has confirmed it. "
    "Use the supplied chess tool "
    "when the operator requests an engine analysis, then interpret its actual result. "
    "When message_operator is available, use it for a useful interim update. "
    "The operator can add a message while you work. Take it into account when it arrives. "
    "Give the operator a final answer in ordinary text."
)
DISABLED_FEATURES = (
    "shell_tool", "shell_snapshot", "unified_exec", "view_image", "computer_use",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access", "in_app_browser",
    "apps", "plugins", "remote_plugin", "hooks", "multi_agent", "multi_agent_v2",
    "image_generation", "memories", "skill_search", "workspace_dependencies",
    "code_mode", "code_mode_prewarm", "tool_suggest", "goals", "sleep_tool",
    "unbounded_connection_retries", "api_key_model_discovery",
)
MAX_FRAME = 4 * 1024 * 1024


def _isolation_args(model: str, provider: str, base_url: str | None) -> list[str]:
    settings = [
        'sandbox_mode="read-only"', 'approval_policy="never"', 'web_search="disabled"',
        'cli_auth_credentials_store="ephemeral"', "check_for_update_on_startup=false",
        "project_doc_max_bytes=0", "allow_login_shell=false",
        'shell_environment_policy.inherit="none"', "features.skip_host_skill_discovery=true",
        "features.code_mode_host={ enabled=false, disable_in_process_fallback=true }",
    ]
    settings.extend(f"features.{feature}=false" for feature in DISABLED_FEATURES)
    if base_url is not None:
        if provider != "lantern":
            raise CodexError("Lantern's local gateway requires the lantern provider ID")
        settings.extend([
            f"model={json.dumps(model)}", 'model_provider="lantern"',
            'model_providers.lantern.name="Lantern gateway"',
            f"model_providers.lantern.base_url={json.dumps(base_url)}",
            'model_providers.lantern.env_key="LANTERN_GATEWAY_TOKEN"',
            'model_providers.lantern.wire_api="responses"',
            "model_providers.lantern.request_max_retries=0",
            "model_providers.lantern.stream_max_retries=0",
            "model_providers.lantern.supports_websockets=false",
        ])
    return [arg for setting in settings for arg in ("-c", setting)]


def _child_environment(home: Path, extra: dict[str, str]) -> dict[str, str]:
    if set(extra) - {"LANTERN_GATEWAY_TOKEN"}:
        raise CodexError("Only a local Lantern gateway token can be passed to Codex")
    allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "LANG", "LC_ALL", "TZ"}
    env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
    profile = home / "profile"
    tmp = profile / "tmp"
    appdata = profile / "appdata"
    tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
    appdata.mkdir(mode=0o700, exist_ok=True)
    env.update(HOME=str(profile), USERPROFILE=str(profile), CODEX_HOME=str(home),
               APPDATA=str(appdata), LOCALAPPDATA=str(appdata),
               TEMP=str(tmp), TMP=str(tmp), TMPDIR=str(tmp), NO_COLOR="1")
    return {**env, **extra}


def _check_config(config: dict, model: str, provider: str, expected_url: str | None) -> None:
    features = config.get("features", {})
    profile = config.get("model_providers", {}).get(provider, {})
    address = urlsplit(profile.get("base_url") or "")
    if (config.get("model") != model or config.get("model_provider") != provider
            or config.get("sandbox_mode") != "read-only" or config.get("approval_policy") != "never"
            or config.get("web_search") != "disabled" or config.get("project_doc_max_bytes") != 0
            or any(features.get(feature) is not False for feature in DISABLED_FEATURES)
            or features.get("skip_host_skill_discovery") is not True
            or features.get("code_mode_host", {}).get("enabled") is not False
            or any(server.get("enabled", True) for server in (config.get("mcp_servers") or {}).values())
            or profile.get("wire_api") != "responses" or profile.get("env_key") != "LANTERN_GATEWAY_TOKEN"
            or (expected_url is not None and profile.get("base_url") != expected_url)
            or address.scheme != "http" or address.hostname not in ("127.0.0.1", "::1")
            or address.username is not None or address.password is not None):
        raise CodexError("Codex effective tool, model or local provider configuration differs from Lantern's")


class _Peer:
    def __init__(self, process, tools, dispatch):
        self.process = process
        self.tools = {tool["name"] for tool in tools}
        self.dispatch = dispatch
        self.seq = 0
        self.thread_id = None
        self.turn_id = None
        self.finished = False
        self.messages = []
        self.tool_calls = []

    async def send(self, obj):
        data = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode() + b"\n"
        if len(data) > MAX_FRAME:
            raise CodexError("Codex protocol message exceeds its size limit")
        self.process.stdin.write(data)
        await self.process.stdin.drain()

    async def read(self):
        data = await self.process.stdout.readline()
        if not data:
            raise CodexError("Codex closed its output before the turn completed")
        obj = json.loads(data)
        if not isinstance(obj, dict):
            raise CodexError("Codex emitted an invalid message")
        return obj

    async def request(self, method, params):
        self.seq += 1
        await self.send({"id": self.seq, "method": method, "params": params})
        while True:
            msg = await self.read()
            if "method" in msg:
                await self.handle(msg)
            elif msg.get("id") != self.seq or not isinstance(msg.get("result"), dict):
                # Raw RPC errors sometimes include a request body or authorization details.
                raise CodexError(f"Codex rejected {method} or sent an unexpected response")
            else:
                return msg["result"]

    def set_turn(self, turn_id):
        if not isinstance(turn_id, str) or not turn_id or (self.turn_id and turn_id != self.turn_id):
            raise CodexError("Codex returned an unexpected turn ID")
        self.turn_id = turn_id

    async def handle(self, msg):
        method = msg.get("method")
        p = msg.get("params", {})
        if self.thread_id and p.get("threadId") not in (None, self.thread_id):
            raise CodexError("Codex emitted an event from a different thread")
        if self.turn_id and p.get("turnId") not in (None, self.turn_id):
            raise CodexError("Codex emitted an event from a different turn")
        if "id" in msg:
            if not self.thread_id or p.get("threadId") != self.thread_id:
                raise CodexError("Unexpected host request before thread activation")
            if method == "currentTime/read":
                result = {"currentTimeAt": int(time.time())}
            elif method == "item/tool/call":
                self.set_turn(p.get("turnId"))
                name, args = p.get("tool"), p.get("arguments")
                if name not in self.tools or p.get("namespace") or not isinstance(args, dict):
                    raise CodexError("Codex requested a tool outside this session")
                try:
                    call_id = p.get("callId")
                    if not isinstance(call_id, str) or not call_id:
                        raise CodexError("Codex omitted the tool call ID")
                    value = self.dispatch(call_id, name, args)
                    success = "error" not in value
                except ValueError as exc:
                    value, success = {"error": str(exc)}, False
                if success:
                    self.tool_calls.append(name)
                result = {"success": success, "contentItems": [
                    {"type": "inputText", "text": json.dumps(value, ensure_ascii=False, allow_nan=False)}]}
            else:
                await self.send({"id": msg["id"], "error": {"code": -32601, "message": "Capability unavailable"}})
                raise CodexError("Codex requested an unsupported host capability")
            await self.send({"id": msg["id"], "result": result})
        elif method == "turn/started":
            self.set_turn(p["turn"]["id"])
        elif method == "turn/completed":
            self.set_turn(p["turn"]["id"])
            if p["turn"].get("status") != "completed":
                raise CodexError("Codex turn ended without completing")
            self.finished = True
        elif method == "item/completed":
            item = p["item"]
            if item.get("type") == "agentMessage" and item.get("phase") in (None, "final_answer"):
                self.messages.append(item["text"])
        elif method == "model/rerouted" or (method == "error" and not p.get("willRetry", False)):
            raise CodexError("Codex reported a provider or turn error")

    async def finish_turn(self, take_input, return_input, capture_steer=None, confirm_steer=None):
        if take_input is None:
            while not self.finished:
                await self.handle(await self.read())
            return
        # One reader owns the RPC stream. Polling the inbox must never cancel a
        # partial JSON frame or lose the acknowledgement for an in-flight steer.
        reader = asyncio.create_task(self.read())
        steer = None
        displaced_request = None
        next_check = 0.0
        try:
            while not self.finished or steer is not None:
                delay = None if steer is not None else max(0.0, next_check - time.monotonic())
                ready, _ = await asyncio.wait((reader,), timeout=delay)
                if ready:
                    msg = reader.result()
                    if "method" in msg:
                        await self.handle(msg)
                    elif steer is not None and msg.get("id") == self.seq:
                        if "error" in msg:
                            # A formal rejection means the additional user input
                            # was not accepted. Leave it queued for a new turn.
                            return_input(steer.ids)
                            take_input = None
                        elif (msg.get("result") or {}).get("turnId") != self.turn_id:
                            raise CodexError("Codex did not confirm delivery to this turn")
                        elif confirm_steer is not None:
                            confirm_steer(displaced_request)
                        steer = None
                    else:
                        raise CodexError("Unexpected Codex response during an active turn")
                    if self.finished and steer is None:
                        break
                    reader = asyncio.create_task(self.read())
                if not self.finished and steer is None and time.monotonic() >= next_check:
                    next_check = time.monotonic() + .25
                    displaced_request = capture_steer() if capture_steer is not None else None
                    if capture_steer is not None and displaced_request is None:
                        # The preceding turn may already be closing. Leave the
                        # input queued until there is a specific request to steer.
                        continue
                    steer = take_input() if take_input else None
                    if steer is not None:
                        self.seq += 1
                        await self.send({"id": self.seq, "method": "turn/steer", "params": {
                            "threadId": self.thread_id, "expectedTurnId": self.turn_id,
                            "input": [{"type": "text", "text": steer.text}],
                        }})
        finally:
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


async def run_turn(*, codex_bin: str, codex_home: Path, workspace: Path, model: str,
                   provider: str, persona: str, prompt: str, tools: list[dict],
                   dispatch: Callable[[str, str, dict], dict], thread_id: str | None = None,
                   on_thread: Callable[[str], None] | None = None,
                   take_input: Callable[[], SteeringInput | None] | None = None,
                   return_input: Callable[[tuple[int, ...]], None] | None = None,
                   capture_steer: Callable[[], int | None] | None = None,
                   confirm_steer: Callable[[int | None], None] | None = None,
                   env: dict[str, str] | None = None, base_url: str | None = None,
                   timeout: float = 180.0) -> TurnResult:
    """Start or resume with a local gateway URL or the caller's isolated config.toml.

    on_thread runs before inference so the caller can durably record a new thread.
    Tool dispatch is synchronous; handlers must keep their own work bounded.
    """
    if take_input is not None and return_input is None:
        raise ValueError("Steering requires a way to return undelivered input")
    workspace = workspace.resolve(strict=True)
    home = codex_home.resolve(strict=True)
    if thread_id is not None:
        UUID(thread_id)
    process = await asyncio.create_subprocess_exec(
        codex_bin, "app-server", "--stdio", "--strict-config", *_isolation_args(model, provider, base_url),
        cwd=workspace, env=_child_environment(home, env or {}),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, limit=MAX_FRAME,
    )
    peer = _Peer(process, tools, dispatch)
    try:
        async with asyncio.timeout(timeout):
            init = await peer.request("initialize", {
                "clientInfo": {"name": "lantern", "title": "Lantern", "version": "0.1"},
                "capabilities": {"experimentalApi": True}})
            if Path(init.get("codexHome") or "").resolve() != home:
                raise CodexError("Codex did not use its isolated home")
            await peer.send({"method": "initialized", "params": {}})
            effective = await peer.request("config/read", {"includeLayers": False})
            _check_config(effective["config"], model, provider, base_url)
            params = {"model": model, "modelProvider": provider, "cwd": str(workspace),
                      "approvalPolicy": "never", "approvalsReviewer": "user", "sandbox": "read-only",
                      "runtimeWorkspaceRoots": [], "baseInstructions": BASE_INSTRUCTIONS,
                      "developerInstructions": persona}
            if thread_id:
                resp = await peer.request("thread/resume", {**params, "threadId": thread_id, "excludeTurns": True})
            else:
                resp = await peer.request("thread/start", {
                    **params, "dynamicTools": tools, "allowProviderModelFallback": False,
                    "environments": [], "ephemeral": False})
            tid = resp["thread"]["id"]
            UUID(tid)
            if ((thread_id and tid != thread_id) or resp.get("model") != model
                    or resp.get("modelProvider") != provider or resp.get("approvalPolicy") != "never"
                    or resp.get("sandbox", {}).get("type") != "readOnly"
                    or resp["sandbox"].get("networkAccess", False) or resp.get("runtimeWorkspaceRoots")
                    or resp.get("instructionSources") or resp.get("activePermissionProfile")):
                raise CodexError("Codex thread identity, permissions or instruction sources changed")
            peer.thread_id = tid
            if on_thread:
                on_thread(tid)
            resp = await peer.request("turn/start", {
                "threadId": tid, "model": model, "approvalPolicy": "never",
                "environments": [], "runtimeWorkspaceRoots": [],
                "sandboxPolicy": {"type": "readOnly", "networkAccess": False},
                "input": [{"type": "text", "text": prompt}]})
            peer.set_turn(resp["turn"]["id"])
            await peer.finish_turn(take_input, return_input, capture_steer, confirm_steer)
            return TurnResult(tid, peer.turn_id, "\n\n".join(peer.messages), tuple(peer.tool_calls))
    except TimeoutError as exc:
        raise CodexError("Codex timed out; do not replay a turn automatically") from exc
    finally:
        # EOF allows Codex to flush the final conversation event before the next resume.
        process.stdin.close()
        try:
            await asyncio.wait_for(process.wait(), 4)
        except TimeoutError:
            if process.returncode is None:
                process.kill()
            await process.wait()
