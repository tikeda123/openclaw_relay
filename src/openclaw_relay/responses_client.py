from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib import error, request

from openclaw_relay.config import EndpointConfig


class ResponsesClientError(RuntimeError):
    """Raised when an OpenClaw responses API call fails."""

    def __init__(self, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.http_status = http_status


@dataclass(frozen=True)
class ResponsesClient:
    def send_user_message(
        self,
        *,
        endpoint: EndpointConfig,
        session_key: str,
        content: str,
        token: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": f"openclaw:{endpoint.agent_id}",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": content,
                }
            ],
        }
        raw_payload = json.dumps(payload).encode("utf-8")
        http_request = request.Request(
            url=f"{endpoint.base_url}/v1/responses",
            data=raw_payload,
            headers={
                "Authorization": f"Bearer {token or endpoint.resolve_token()}",
                "x-openclaw-agent-id": endpoint.agent_id,
                "x-openclaw-session-key": session_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with request.urlopen(http_request, timeout=endpoint.timeout_seconds) as response:
                body = response.read().decode("utf-8")
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ResponsesClientError(
                f"{endpoint.display_name} returned HTTP {exc.code}: {body[:400]}",
                http_status=exc.code,
            ) from exc
        except error.URLError as exc:
            raise ResponsesClientError(f"{endpoint.display_name} request failed: {exc.reason}") from exc

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ResponsesClientError(
                f"{endpoint.display_name} returned non-JSON response: {body[:400]}"
            ) from exc

        if not isinstance(parsed, dict):
            raise ResponsesClientError(f"{endpoint.display_name} returned non-object JSON")
        return parsed
