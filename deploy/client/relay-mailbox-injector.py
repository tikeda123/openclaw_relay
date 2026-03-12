#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
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
DEFAULT_DEADLETTER_PATH = (
    Path.home() / ".local" / "state" / "openclaw-relay" / "mailbox-injector-deadletter.jsonl"
)
DEFAULT_TELEGRAM_NOTICE_PREFIX = "[AUTO] Relay reply received"


class InjectorError(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Poll Relay mailbox and inject replies into the local human-facing OpenClaw session."
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
        help="local OpenClaw session key used for injected replies; defaults to agent:<agent-id>:<agent-id>",
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
    parser.add_argument(
        "--sessions-registry",
        type=Path,
        default=None,
        help="path to the local OpenClaw sessions.json; defaults to ~/.openclaw/agents/<agent-id>/sessions/sessions.json",
    )
    parser.add_argument(
        "--deadletter-path",
        type=Path,
        default=DEFAULT_DEADLETTER_PATH,
        help="where to persist messages that were dequeued but could not be injected",
    )
    parser.add_argument(
        "--notify-telegram",
        action="store_true",
        help="after successful inject, send a fixed-format Telegram notice to the human-facing chat",
    )
    parser.add_argument(
        "--telegram-notice-prefix",
        default=DEFAULT_TELEGRAM_NOTICE_PREFIX,
        help="first line used for direct Telegram notices",
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
        raise InjectorError(f"environment variable {name} is required")
    return value


def load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    if not path.exists():
        raise InjectorError(f"{label} not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InjectorError(f"{label} is invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise InjectorError(f"{label} is not a JSON object: {path}")
    return payload


def extract_openclaw_gateway(payload: dict[str, Any]) -> tuple[str, str]:
    gateway = payload.get("gateway")
    if not isinstance(gateway, dict):
        raise InjectorError("openclaw.json is missing gateway config")
    port = gateway.get("port")
    if not isinstance(port, int) or port <= 0:
        raise InjectorError("openclaw.json has invalid gateway.port")
    auth = gateway.get("auth")
    if not isinstance(auth, dict):
        raise InjectorError("openclaw.json is missing gateway.auth")
    token = auth.get("token")
    if not isinstance(token, str) or not token:
        raise InjectorError("openclaw.json is missing gateway.auth.token")
    return (f"http://127.0.0.1:{port}", token)


def load_openclaw_gateway(config_path: Path) -> tuple[str, str]:
    return extract_openclaw_gateway(load_json_object(config_path, label="OpenClaw config"))


def default_human_session_key(agent_id: str) -> str:
    normalized = (agent_id or "main").strip() or "main"
    return f"agent:{normalized}:{normalized}"


def default_sessions_registry(agent_id: str) -> Path:
    normalized = (agent_id or "main").strip() or "main"
    return Path.home() / ".openclaw" / "agents" / normalized / "sessions" / "sessions.json"


def resolve_telegram_delivery(
    *,
    openclaw_config_path: Path,
    sessions_registry_path: Path,
    session_key: str,
) -> tuple[str, str]:
    openclaw_payload = load_json_object(openclaw_config_path, label="OpenClaw config")
    channels = openclaw_payload.get("channels")
    if not isinstance(channels, dict):
        raise InjectorError("openclaw.json is missing channels config")
    telegram = channels.get("telegram")
    if not isinstance(telegram, dict):
        raise InjectorError("openclaw.json is missing channels.telegram config")
    token = telegram.get("botToken")
    if not isinstance(token, str) or not token:
        raise InjectorError("openclaw.json is missing channels.telegram.botToken")

    sessions_payload = load_json_object(sessions_registry_path, label="OpenClaw sessions registry")
    session_payload = sessions_payload.get(session_key)
    if not isinstance(session_payload, dict):
        raise InjectorError(f"sessions.json does not contain session key: {session_key}")
    delivery_context = session_payload.get("deliveryContext")
    if not isinstance(delivery_context, dict):
        raise InjectorError(f"sessions.json is missing deliveryContext for session: {session_key}")
    destination = delivery_context.get("to")
    if not isinstance(destination, str) or not destination:
        raise InjectorError(f"sessions.json is missing deliveryContext.to for session: {session_key}")
    if not destination.startswith("telegram:"):
        raise InjectorError(
            f"session {session_key} is not backed by Telegram delivery context: {destination}"
        )
    chat_id = destination.split(":", 1)[1].strip()
    if not chat_id:
        raise InjectorError(f"session {session_key} has empty Telegram chat id")
    return token, chat_id


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
        raise InjectorError(f"relay receive failed with HTTP {exc.code}: {detail[:400]}") from exc
    except error.URLError as exc:
        raise InjectorError(f"relay receive failed: {exc.reason}") from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise InjectorError(f"relay receive returned invalid JSON: {body[:400]}") from exc
    if not isinstance(payload, dict):
        raise InjectorError("relay receive returned non-object JSON")
    return payload


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
        raise InjectorError(f"local gateway returned HTTP {exc.code}: {detail[:400]}") from exc
    except error.URLError as exc:
        raise InjectorError(f"local gateway request failed: {exc.reason}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise InjectorError(f"local gateway returned invalid JSON: {body[:400]}") from exc
    if not isinstance(parsed, dict):
        raise InjectorError("local gateway returned non-object JSON")
    return parsed


def send_telegram_message(*, bot_token: str, chat_id: str, text: str) -> dict[str, Any]:
    raw_payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
        }
    ).encode("utf-8")
    http_request = request.Request(
        url=f"https://api.telegram.org/bot{bot_token}/sendMessage",
        data=raw_payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with request.urlopen(http_request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise InjectorError(f"telegram send failed with HTTP {exc.code}: {detail[:400]}") from exc
    except error.URLError as exc:
        raise InjectorError(f"telegram send failed: {exc.reason}") from exc
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise InjectorError(f"telegram send returned invalid JSON: {body[:400]}") from exc
    if not isinstance(parsed, dict):
        raise InjectorError("telegram send returned non-object JSON")
    if parsed.get("ok") is not True:
        raise InjectorError(f"telegram send returned non-ok payload: {body[:400]}")
    return parsed


def build_inject_content(message: dict[str, Any]) -> str:
    from_mailbox = str(message.get("from") or "unknown")
    message_id = str(message.get("messageId") or "unknown")
    conversation_id = str(message.get("conversationId") or message_id)
    body = str(message.get("body") or "")
    return (
        f"[Reply from {from_mailbox}]"
        f"[MESSAGE {message_id}]"
        f"[CONVERSATION {conversation_id}]\n"
        f"{body}"
    )


def build_telegram_notice(message: dict[str, Any], *, prefix: str) -> str:
    from_mailbox = str(message.get("from") or "unknown")
    message_id = str(message.get("messageId") or "unknown")
    conversation_id = str(message.get("conversationId") or message_id)
    in_reply_to = str(message.get("inReplyTo") or "").strip()
    body = str(message.get("body") or "").strip()
    lines = [prefix, f"from: {from_mailbox}", f"messageId: {message_id}"]
    if conversation_id:
        lines.append(f"conversationId: {conversation_id}")
    if in_reply_to:
        lines.append(f"inReplyTo: {in_reply_to}")
    if body:
        lines.append(f"body: {body}")
    return "\n".join(lines)


def append_deadletter(*, path: Path, message: dict[str, Any], error_text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "at": datetime.now(timezone.utc).isoformat(),
        "error": error_text,
        "message": message,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def log(message: str, *, verbose: bool) -> None:
    if verbose:
        print(message, file=sys.stderr, flush=True)


class MailboxInjector:
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
        deadletter_path: Path,
        telegram_bot_token: str | None,
        telegram_chat_id: str | None,
        telegram_notice_prefix: str,
        verbose: bool,
    ) -> None:
        self.relay_base_url = relay_base_url
        self.relay_token = relay_token
        self.local_base_url = local_base_url
        self.local_token = local_token
        self.agent_id = agent_id
        self.session_key = session_key
        self.poll_interval_seconds = max(0.2, poll_interval_seconds)
        self.deadletter_path = deadletter_path
        self.telegram_bot_token = telegram_bot_token
        self.telegram_chat_id = telegram_chat_id
        self.telegram_notice_prefix = telegram_notice_prefix
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
        inject_content = build_inject_content(message)
        try:
            call_local_gateway(
                base_url=self.local_base_url,
                token=self.local_token,
                agent_id=self.agent_id,
                session_key=self.session_key,
                content=inject_content,
            )
            log(
                f"processed mailbox reply={message_id} from={message.get('from')} session={self.session_key}",
                verbose=self.verbose,
            )
            if (
                self.telegram_bot_token
                and self.telegram_chat_id
                and message.get("notifyHuman") is True
            ):
                telegram_response = send_telegram_message(
                    bot_token=self.telegram_bot_token,
                    chat_id=self.telegram_chat_id,
                    text=build_telegram_notice(message, prefix=self.telegram_notice_prefix),
                )
                result = telegram_response.get("result")
                telegram_message_id = None
                if isinstance(result, dict):
                    telegram_message_id = result.get("message_id")
                log(
                    "sent telegram notice "
                    f"reply={message_id} chat={self.telegram_chat_id} telegram_message_id={telegram_message_id}",
                    verbose=self.verbose,
                )
        except Exception as exc:
            append_deadletter(path=self.deadletter_path, message=message, error_text=str(exc))
            log(
                f"deadlettered mailbox reply={message_id} session={self.session_key} error={exc}",
                verbose=self.verbose,
            )
        return True


def main() -> int:
    args = parse_args()
    load_env_file(args.relay_client_env)
    relay_base_url = require_env("RELAY_BASE_URL")
    relay_token = require_env("RELAY_MAILBOX_TOKEN")
    local_base_url, local_token = load_openclaw_gateway(args.openclaw_config)
    session_key = args.session_key or default_human_session_key(args.agent_id)
    telegram_bot_token = None
    telegram_chat_id = None
    if args.notify_telegram:
        telegram_bot_token, telegram_chat_id = resolve_telegram_delivery(
            openclaw_config_path=args.openclaw_config,
            sessions_registry_path=args.sessions_registry or default_sessions_registry(args.agent_id),
            session_key=session_key,
        )
    injector = MailboxInjector(
        relay_base_url=relay_base_url,
        relay_token=relay_token,
        local_base_url=local_base_url,
        local_token=local_token,
        agent_id=args.agent_id,
        session_key=session_key,
        poll_interval_seconds=args.poll_interval_seconds,
        deadletter_path=args.deadletter_path,
        telegram_bot_token=telegram_bot_token,
        telegram_chat_id=telegram_chat_id,
        telegram_notice_prefix=args.telegram_notice_prefix,
        verbose=args.verbose,
    )
    signal.signal(signal.SIGINT, injector.stop)
    signal.signal(signal.SIGTERM, injector.stop)
    processed = injector.run(once=args.once)
    log(f"injector exit processed={processed}", verbose=args.verbose)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InjectorError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
