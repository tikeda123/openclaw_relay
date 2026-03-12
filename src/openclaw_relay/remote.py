from __future__ import annotations

from dataclasses import dataclass
import json
import shlex
import socket
import subprocess
from typing import Sequence

from openclaw_relay.config import SSHConnectionConfig, TunnelConfig


class RemoteError(RuntimeError):
    """Raised when an SSH-transported operation fails."""


def _py_literal(value: object) -> str:
    return json.dumps(value, ensure_ascii=True)


@dataclass(frozen=True)
class SSHRunner:
    connection: SSHConnectionConfig

    def build_ssh_command(self) -> list[str]:
        command = [
            "ssh",
            "-i",
            str(self.connection.key_path),
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.connection.connect_timeout_seconds}",
        ]
        strict = "yes" if self.connection.strict_host_key_checking else "no"
        command.extend(["-o", f"StrictHostKeyChecking={strict}"])
        command.append(f"{self.connection.user}@{self.connection.host}")
        return command

    def run(self, remote_command: str) -> subprocess.CompletedProcess[str]:
        command = self.build_ssh_command()
        command.append(remote_command)
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RemoteError(result.stderr.strip() or result.stdout.strip() or "ssh command failed")
        return result


@dataclass(frozen=True)
class TunnelManager:
    def ensure_tunnel(self, tunnel: TunnelConfig) -> bool:
        if self.is_tunnel_open(tunnel):
            return False

        ssh_runner = SSHRunner(tunnel.ssh)
        command = ssh_runner.build_ssh_command()
        command.extend(
            [
                "-f",
                "-N",
                "-o",
                "ExitOnForwardFailure=yes",
                "-L",
                f"{tunnel.local_port}:{tunnel.remote_host}:{tunnel.remote_port}",
            ]
        )
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RemoteError(result.stderr.strip() or "failed to establish ssh tunnel")
        return True

    def is_tunnel_open(self, tunnel: TunnelConfig) -> bool:
        return _port_open("127.0.0.1", tunnel.local_port)


@dataclass(frozen=True)
class RemoteTokenResolver:
    def fetch_token(self, tunnel: TunnelConfig) -> str:
        runner = SSHRunner(tunnel.ssh)
        command = (
            "python3 -c "
            + shlex.quote(
                "import json, pathlib; "
                f"print(json.loads(pathlib.Path({str(tunnel.token_config_path)!r}).read_text())"
                "['gateway']['auth']['token'])"
            )
        )
        result = runner.run(command)
        token = result.stdout.strip()
        if not token:
            raise RemoteError("remote token command returned empty output")
        return token


@dataclass(frozen=True)
class RemoteSessionFetcher:
    def fetch_recent_messages(
        self,
        connection: SSHConnectionConfig,
        *,
        node_label: str,
        subagent_labels: Sequence[str] | None = None,
        include_terms: Sequence[str],
        limit_files: int = 8,
        limit_messages: int = 24,
    ) -> list[dict[str, object]]:
        runner = SSHRunner(connection)
        remote_script = f"""
import json
from heapq import nlargest
from pathlib import Path

base = Path.home() / ".openclaw" / "agents" / "main" / "sessions"
sessions_index_path = base / "sessions.json"
terms = [term.lower() for term in {_py_literal(list(include_terms))}]
limit_files = {int(limit_files)}
limit_messages = {int(limit_messages)}
node_label = {_py_literal(node_label)}
subagent_labels = {_py_literal(list(subagent_labels or []))}
spawn_calls = {{}}
tracked_sessions = {{}}

def should_include(*values):
    haystack = "\\n".join(
        value for value in values
        if isinstance(value, str) and value.strip()
    ).lower()
    if not haystack:
        return False
    return any(term in haystack for term in terms)

def detect_subagent_label(*values):
    haystack = "\\n".join(
        value for value in values
        if isinstance(value, str) and value.strip()
    ).lower()
    if not haystack:
        return subagent_labels[0] if subagent_labels else None
    for candidate in subagent_labels:
        if candidate.lower() in haystack:
            return candidate
    return subagent_labels[0] if subagent_labels else None

def iter_text_parts(content):
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            yield text.strip()

def load_sessions_index():
    if not sessions_index_path.exists():
        return {{}}
    try:
        payload = json.loads(sessions_index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {{}}
    if not isinstance(payload, dict):
        return {{}}
    return payload

def build_subagent_transcript_map(index):
    result = {{}}
    for session_key, details in index.items():
        if not isinstance(session_key, str) or not session_key.startswith("agent:main:subagent:"):
            continue
        if not isinstance(details, dict):
            continue
        transcript_path = details.get("transcriptPath") or details.get("sessionFile")
        if not isinstance(transcript_path, str) or not transcript_path.strip():
            continue
        result[Path(transcript_path).name] = {{
            "sessionKey": session_key,
            "label": details.get("label"),
            "transcriptPath": transcript_path,
        }}
    return result

def iter_assistant_text_messages(path, label, sender_label):
    try:
        handle = path.open("r", encoding="utf-8", errors="ignore")
    except OSError:
        return
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("type") != "message":
                continue
            message = payload.get("message")
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            text_parts = list(iter_text_parts(message.get("content")))
            if not text_parts:
                continue
            yield {{
                "source": "session",
                "node": sender_label or node_label,
                "role": "assistant",
                "kind": "b",
                "sender": sender_label or node_label,
                "worker": sender_label or node_label,
                "label": label,
                "at": payload.get("timestamp"),
                "text": "\\n\\n".join(text_parts).strip(),
                "sessionFile": path.name,
            }}

entries = []
sessions_index = load_sessions_index()
subagent_transcripts = build_subagent_transcript_map(sessions_index)
if base.exists():
    files = [
        path for path in base.iterdir()
        if path.is_file() and (path.name.endswith(".jsonl") or ".jsonl.deleted." in path.name)
    ]
    files = nlargest(limit_files, files, key=lambda path: path.stat().st_mtime)
    for path in files:
        try:
            handle = path.open("r", encoding="utf-8", errors="ignore")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict) or payload.get("type") != "message":
                    continue
                message = payload.get("message")
                if not isinstance(message, dict):
                    continue
                if path.name in subagent_transcripts:
                    continue
                role = message.get("role")
                content = message.get("content")
                if role in {{"user", "assistant"}}:
                    text_parts = list(iter_text_parts(content))
                    if text_parts:
                        text = "\\n\\n".join(text_parts).strip()
                        if should_include(text):
                            entries.append(
                                {{
                                    "source": "session",
                                    "node": node_label,
                                    "role": role,
                                    "at": payload.get("timestamp"),
                                    "text": text,
                                    "sessionFile": path.name,
                                }}
                            )

                if role == "assistant" and isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") != "toolCall" or item.get("name") != "sessions_spawn":
                            continue
                        arguments = item.get("arguments")
                        if not isinstance(arguments, dict):
                            continue
                        label = arguments.get("label")
                        task = arguments.get("task")
                        if not should_include(label, task):
                            continue
                        target_label = detect_subagent_label(label, task)
                        spawn_calls[item.get("id")] = {{
                            "label": label,
                            "task": task,
                            "subagentLabel": target_label,
                        }}
                        formatted = "\\n".join(
                            part for part in (
                                f"[Dispatch to {{target_label or 'worker'}}]",
                                f"label: {{label}}" if isinstance(label, str) and label else None,
                                "",
                                task if isinstance(task, str) else None,
                            )
                            if part is not None
                        ).strip()
                        entries.append(
                            {{
                                "source": "session",
                                "node": node_label,
                                "role": "assistant",
                                "kind": "a",
                                "sender": node_label,
                                "worker": target_label,
                                "label": label,
                                "at": payload.get("timestamp"),
                                "text": formatted,
                                "sessionFile": path.name,
                            }}
                        )

                if role == "toolResult" and message.get("toolName") == "sessions_spawn":
                    merged = {{}}
                    details = message.get("details")
                    if isinstance(details, dict):
                        merged.update(details)
                    if isinstance(content, list):
                        for item in content:
                            if not isinstance(item, dict) or item.get("type") != "text":
                                continue
                            text = item.get("text")
                            if not isinstance(text, str):
                                continue
                            try:
                                parsed_text = json.loads(text)
                            except json.JSONDecodeError:
                                continue
                            if isinstance(parsed_text, dict):
                                merged.update(parsed_text)
                    context = spawn_calls.get(message.get("toolCallId"), {{}})
                    label = context.get("label")
                    task = context.get("task")
                    target_label = context.get("subagentLabel")
                    run_id = merged.get("runId")
                    session_key = merged.get("childSessionKey")
                    status = merged.get("status")
                    if not should_include(label, task, run_id, session_key):
                        continue
                    if isinstance(session_key, str) and session_key:
                        tracked_sessions[session_key] = {{
                            "label": label,
                            "runId": run_id,
                            "subagentLabel": target_label,
                        }}
                    formatted = "\\n".join(
                        part for part in (
                            "[Dispatch accepted]",
                            f"label: {{label}}" if isinstance(label, str) and label else None,
                            f"runId: {{run_id}}" if isinstance(run_id, str) and run_id else None,
                            f"sessionKey: {{session_key}}" if isinstance(session_key, str) and session_key else None,
                            f"status: {{status}}" if isinstance(status, str) and status else None,
                        )
                        if part is not None
                    ).strip()
                    entries.append(
                        {{
                            "source": "session",
                            "node": node_label,
                            "role": "assistant",
                            "kind": "a",
                            "sender": node_label,
                            "worker": target_label,
                            "label": label,
                            "at": payload.get("timestamp"),
                            "text": formatted,
                            "sessionFile": path.name,
                        }}
                    )

for session_key, context in tracked_sessions.items():
    details = sessions_index.get(session_key)
    if not isinstance(details, dict):
        continue
    transcript_path = details.get("transcriptPath") or details.get("sessionFile")
    if not isinstance(transcript_path, str) or not transcript_path.strip():
        continue
    label = context.get("label")
    sender_label = context.get("subagentLabel")
    for item in iter_assistant_text_messages(Path(transcript_path), label, sender_label):
        entries.append(item)

entries.sort(key=lambda item: item.get("at") or "")
print(json.dumps(entries[-limit_messages:], ensure_ascii=False))
"""
        result = runner.run("python3 - <<'PY'\n" + remote_script + "\nPY")
        output = result.stdout.strip()
        if not output:
            return []
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RemoteError("remote session fetch returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise RemoteError("remote session fetch returned unexpected payload")
        return [item for item in payload if isinstance(item, dict)]

    def find_message(
        self,
        connection: SSHConnectionConfig,
        *,
        session_key: str,
        marker_text: str,
        limit_files: int = 32,
    ) -> dict[str, object] | None:
        runner = SSHRunner(connection)
        remote_script = f"""
import json
from heapq import nlargest
from pathlib import Path

base = Path.home() / ".openclaw" / "agents" / "main" / "sessions"
sessions_index_path = base / "sessions.json"
session_key = {_py_literal(session_key)}
marker_text = {_py_literal(marker_text)}
limit_files = {int(limit_files)}

def iter_text_parts(content):
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            yield text.strip()

def load_sessions_index():
    if not sessions_index_path.exists():
        return {{}}
    try:
        payload = json.loads(sessions_index_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {{}}
    return payload if isinstance(payload, dict) else {{}}

def candidate_paths():
    index = load_sessions_index()
    details = index.get(session_key)
    preferred = []
    if isinstance(details, dict):
        transcript_path = details.get("transcriptPath") or details.get("sessionFile")
        if isinstance(transcript_path, str) and transcript_path.strip():
            preferred_path = Path(transcript_path)
            if preferred_path.exists() and preferred_path.is_file():
                preferred.append(preferred_path)
    if preferred:
        return preferred
    if not base.exists():
        return []
    files = [
        path for path in base.iterdir()
        if path.is_file() and (path.name.endswith(".jsonl") or ".jsonl.deleted." in path.name)
    ]
    return nlargest(limit_files, files, key=lambda path: path.stat().st_mtime)

for path in candidate_paths():
    try:
        handle = path.open("r", encoding="utf-8", errors="ignore")
    except OSError:
        continue
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("type") != "message":
                continue
            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            text_parts = list(iter_text_parts(message.get("content")))
            if not text_parts:
                continue
            text = "\\n\\n".join(text_parts).strip()
            if marker_text in text:
                print(
                    json.dumps(
                        {{
                            "found": True,
                            "sessionFile": path.name,
                            "at": payload.get("timestamp"),
                            "role": message.get("role"),
                            "body": text,
                        }},
                        ensure_ascii=False,
                    )
                )
                raise SystemExit(0)

print(json.dumps({{"found": False}}, ensure_ascii=False))
"""
        result = runner.run("python3 - <<'PY'\n" + remote_script + "\nPY")
        output = result.stdout.strip()
        if not output:
            return None
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RemoteError("remote session search returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RemoteError("remote session search returned unexpected payload")
        if payload.get("found") is True:
            return payload
        return None

    def find_message_reply(
        self,
        connection: SSHConnectionConfig,
        *,
        marker_text: str,
        limit_files: int = 16,
    ) -> dict[str, object] | None:
        runner = SSHRunner(connection)
        remote_script = f"""
import json
from heapq import nlargest
from pathlib import Path

base = Path.home() / ".openclaw" / "agents" / "main" / "sessions"
marker_text = {_py_literal(marker_text)}
limit_files = {int(limit_files)}

def iter_text_parts(content):
    if not isinstance(content, list):
        return
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "text":
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            yield text.strip()

if not base.exists():
    print(json.dumps({{"found": False}}, ensure_ascii=False))
    raise SystemExit(0)

files = [
    path for path in base.iterdir()
    if path.is_file() and (path.name.endswith(".jsonl") or ".jsonl.deleted." in path.name)
]
files = nlargest(limit_files, files, key=lambda path: path.stat().st_mtime)

best_match = None
for path in files:
    try:
        handle = path.open("r", encoding="utf-8", errors="ignore")
    except OSError:
        continue
    matched_request = None
    with handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("type") != "message":
                continue
            message = payload.get("message")
            if not isinstance(message, dict):
                continue
            text_parts = list(iter_text_parts(message.get("content")))
            if not text_parts:
                continue
            text = "\\n\\n".join(text_parts).strip()
            role = message.get("role")
            if matched_request is None and role == "user" and marker_text in text:
                matched_request = {{
                    "found": True,
                    "sessionFile": path.name,
                    "requestAt": payload.get("timestamp"),
                    "requestBody": text,
                }}
                continue
            if matched_request is not None and role == "assistant":
                matched_request["replyAt"] = payload.get("timestamp")
                matched_request["replyText"] = text
                print(json.dumps(matched_request, ensure_ascii=False))
                raise SystemExit(0)
    if matched_request is not None and best_match is None:
        best_match = matched_request

print(json.dumps(best_match or {{"found": False}}, ensure_ascii=False))
"""
        result = runner.run("python3 - <<'PY'\n" + remote_script + "\nPY")
        output = result.stdout.strip()
        if not output:
            return None
        try:
            payload = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RemoteError("remote reply search returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RemoteError("remote reply search returned unexpected payload")
        if payload.get("found") is True:
            return payload
        return None


def _port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0
