#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any
from urllib import error, request


DEFAULT_RELAY_ENV = Path.home() / ".config" / "openclaw-relay" / "client.env"
DEFAULT_OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
DEFAULT_AGENT_ID = "main"
DEFAULT_POLL_INTERVAL_SECONDS = 2.0


class WorkerError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll Relay mailbox, dispatch work to local OpenClaw, and send replies."
    )
    parser.add_argument("--once", action="store_true", help="process at most one mailbox message")
    parser.add_argument(
        "--poll-interval-seconds",
        type=float,
        default=DEFAULT_POLL_INTERVAL_SECONDS,
        help="sleep interval while mailbox is empty",
    )
    parser.add_argument(
        "--agent-id",
        default=DEFAULT_AGENT_ID,
        help="local OpenClaw agent id for /v1/responses",
    )
    parser.add_argument(
        "--session-key",
        default=None,
        help="local OpenClaw session key used for mailbox work; defaults to agent:<agent-id>:<agent-id>",
    )
    parser.add_argument(
        "--relay-client-env",
        type=Path,
        default=Path(os.environ.get("RELAY_CLIENT_ENV", DEFAULT_RELAY_ENV)),
        help="path to Relay client env file",
    )
    parser.add_argument(
        "--openclaw-config",
        type=Path,
        default=DEFAULT_OPENCLAW_CONFIG,
        help="path to local openclaw.json",
    )
    parser.add_argument("--verbose", action="store_true", help="emit verbose logs to stderr")
    return parser.parse_args()


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise WorkerError(f"environment variable {name} is required")
    return value


def load_openclaw_gateway(config_path: Path) -> tuple[str, str]:
    if not config_path.exists():
        raise WorkerError(f"OpenClaw config not found: {config_path}")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    gateway = payload.get("gateway")
    if not isinstance(gateway, dict):
        raise WorkerError("openclaw.json is missing gateway config")
    port = gateway.get("port")
    if not isinstance(port, int) or port <= 0:
        raise WorkerError("openclaw.json has invalid gateway.port")
    auth = gateway.get("auth")
    if not isinstance(auth, dict):
        raise WorkerError("openclaw.json is missing gateway.auth")
    token = auth.get("token")
    if not isinstance(token, str) or not token:
        raise WorkerError("openclaw.json is missing gateway.auth.token")
    return (f"http://127.0.0.1:{port}", token)


def default_human_session_key(agent_id: str) -> str:
    normalized = (agent_id or "main").strip() or "main"
    return f"agent:{normalized}:{normalized}"


def relay_receive(*, base_url: str, token: str) -> dict[str, Any] | None:
    http_request = request.Request(
        url=f"{base_url.rstrip('/')}/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    try:
        with request.urlopen(http_request, timeout=30) as response:
            if response.status == 204:
                return None
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        if exc.code == 204:
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise WorkerError(f"relay receive failed with HTTP {exc.code}: {detail[:400]}") from exc
    except error.URLError as exc:
        raise WorkerError(f"relay receive failed: {exc.reason}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise WorkerError(f"relay receive returned invalid JSON: {body[:400]}") from exc
    if not isinstance(payload, dict):
        raise WorkerError("relay receive returned non-object JSON")
    return payload


def relay_send(*, base_url: str, token: str, payload: dict[str, Any]) -> dict[str, Any]:
    raw_payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    http_request = request.Request(
        url=f"{base_url.rstrip('/')}/v1/messages",
        data=raw_payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="PUT",
    )
    try:
        with request.urlopen(http_request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise WorkerError(f"relay send failed with HTTP {exc.code}: {detail[:400]}") from exc
    except error.URLError as exc:
        raise WorkerError(f"relay send failed: {exc.reason}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise WorkerError(f"relay send returned invalid JSON: {body[:400]}") from exc
    if not isinstance(parsed, dict):
        raise WorkerError("relay send returned non-object JSON")
    return parsed


def call_local_gateway(
    *,
    base_url: str,
    token: str,
    agent_id: str,
    session_key: str,
    content: str,
) -> dict[str, Any]:
    raw_payload = json.dumps(
        {
            "model": f"openclaw:{agent_id}",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": content,
                }
            ],
        }
    ).encode("utf-8")
    http_request = request.Request(
        url=f"{base_url.rstrip('/')}/v1/responses",
        data=raw_payload,
        headers={
            "Authorization": f"Bearer {token}",
            "x-openclaw-agent-id": agent_id,
            "x-openclaw-session-key": session_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(http_request, timeout=120) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise WorkerError(f"local gateway returned HTTP {exc.code}: {detail[:400]}") from exc
    except error.URLError as exc:
        raise WorkerError(f"local gateway request failed: {exc.reason}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise WorkerError(f"local gateway returned invalid JSON: {body[:400]}") from exc
    if not isinstance(parsed, dict):
        raise WorkerError("local gateway returned non-object JSON")
    return parsed


def extract_output_text(payload: dict[str, Any]) -> str:
    output = payload.get("output")
    if not isinstance(output, list):
        raise WorkerError("gateway response is missing output[]")
    texts: list[str] = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "output_text":
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text.strip())
    if not texts:
        raise WorkerError("gateway response does not contain output_text")
    return "\n".join(texts)


def build_reply_payload(message: dict[str, Any], body: str) -> dict[str, Any]:
    payload = {
        "to": str(message["from"]),
        "body": body,
        "conversationId": str(message.get("conversationId") or message.get("messageId") or ""),
        "inReplyTo": str(message.get("messageId") or ""),
    }
    if message.get("notifyHuman") is True:
        payload["notifyHuman"] = True
    return payload


def is_self_addressed_message(message: dict[str, Any]) -> bool:
    sender = str(message.get("from") or "").strip()
    recipient = str(message.get("to") or "").strip()
    return bool(sender and recipient and sender == recipient)


def log(message: str, *, verbose: bool) -> None:
    if verbose:
        print(message, file=sys.stderr, flush=True)


class MailboxWorker:
    def __init__(
        self,
        *,
        relay_base_url: str,
        relay_token: str,
        local_base_url: str,
        local_token: str,
        agent_id: str,
        session_key: str,
        poll_interval_seconds: float,
        verbose: bool,
    ) -> None:
        self.relay_base_url = relay_base_url
        self.relay_token = relay_token
        self.local_base_url = local_base_url
        self.local_token = local_token
        self.agent_id = agent_id
        self.session_key = session_key
        self.poll_interval_seconds = max(0.2, poll_interval_seconds)
        self.verbose = verbose
        self.running = True

    def stop(self, *_args: object) -> None:
        self.running = False

    def run(self, *, once: bool) -> int:
        processed = 0
        while self.running:
            handled = self.process_one()
            if handled:
                processed += 1
            if once:
                return processed
            if not handled:
                time.sleep(self.poll_interval_seconds)
        return processed

    def process_one(self) -> bool:
        message = relay_receive(base_url=self.relay_base_url, token=self.relay_token)
        if message is None:
            return False
        message_id = str(message.get("messageId") or "unknown")
        if is_self_addressed_message(message):
            log(
                f"discarded self-addressed mailbox message={message_id}",
                verbose=self.verbose,
            )
            return True
        body = str(message.get("body") or "")
        if not body.strip():
            reply_text = "worker_error: mailbox message body is empty"
        else:
            try:
                response = call_local_gateway(
                    base_url=self.local_base_url,
                    token=self.local_token,
                    agent_id=self.agent_id,
                    session_key=self.session_key,
                    content=body,
                )
                reply_text = extract_output_text(response)
            except Exception as exc:
                reply_text = f"worker_error: {exc}"
        reply_payload = build_reply_payload(message, reply_text)
        relay_send(base_url=self.relay_base_url, token=self.relay_token, payload=reply_payload)
        log(
            f"processed mailbox message={message_id} reply_to={reply_payload['to']} session={self.session_key}",
            verbose=self.verbose,
        )
        return True


def main() -> int:
    args = parse_args()
    load_env_file(args.relay_client_env)
    relay_base_url = require_env("RELAY_BASE_URL")
    relay_token = require_env("RELAY_MAILBOX_TOKEN")
    local_base_url, local_token = load_openclaw_gateway(args.openclaw_config)
    worker = MailboxWorker(
        relay_base_url=relay_base_url,
        relay_token=relay_token,
        local_base_url=local_base_url,
        local_token=local_token,
        agent_id=args.agent_id,
        session_key=args.session_key or default_human_session_key(args.agent_id),
        poll_interval_seconds=args.poll_interval_seconds,
        verbose=args.verbose,
    )
    signal.signal(signal.SIGINT, worker.stop)
    signal.signal(signal.SIGTERM, worker.stop)
    processed = worker.run(once=args.once)
    log(f"worker exit processed={processed}", verbose=args.verbose)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except WorkerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
