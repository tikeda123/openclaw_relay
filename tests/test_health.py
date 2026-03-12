from __future__ import annotations

import json
import tempfile
import unittest
from urllib import request
from urllib.error import HTTPError

from openclaw_relay.health import HealthServer


class HealthServerTests(unittest.TestCase):
    def test_metrics_endpoint_returns_prometheus_payload(self) -> None:
        server = HealthServer(
            "127.0.0.1",
            0,
            alive_probe=lambda: True,
            ready_probe=lambda: True,
            metrics_probe=lambda: "openclaw_relay_up 1\n",
        )
        server.start()
        try:
            with request.urlopen(f"http://127.0.0.1:{server.port}/healthz", timeout=3) as response:
                health = json.loads(response.read().decode("utf-8"))
            with request.urlopen(f"http://127.0.0.1:{server.port}/metrics", timeout=3) as response:
                metrics = response.read().decode("utf-8")
                content_type = response.headers.get_content_type()
        finally:
            server.stop()

        self.assertEqual(health["status"], "ok")
        self.assertEqual(content_type, "text/plain")
        self.assertIn("openclaw_relay_up 1", metrics)

    def test_alertmanager_webhook_is_accepted(self) -> None:
        received: list[dict[str, object]] = []
        server = HealthServer(
            "127.0.0.1",
            0,
            alive_probe=lambda: True,
            ready_probe=lambda: True,
            alert_webhook=lambda payload: received.append(payload) or {"status": "accepted"},
        )
        server.start()
        try:
            req = request.Request(
                f"http://127.0.0.1:{server.port}/api/alertmanager/webhook",
                data=json.dumps({"alerts": []}).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=3) as response:
                body = json.loads(response.read().decode("utf-8"))
                status_code = response.status
        finally:
            server.stop()

        self.assertEqual(status_code, 202)
        self.assertEqual(body["status"], "accepted")
        self.assertEqual(received, [{"alerts": []}])

    def test_alertmanager_webhook_rejects_invalid_json(self) -> None:
        server = HealthServer(
            "127.0.0.1",
            0,
            alive_probe=lambda: True,
            ready_probe=lambda: True,
            alert_webhook=lambda payload: {"status": "accepted"},
        )
        server.start()
        try:
            req = request.Request(
                f"http://127.0.0.1:{server.port}/api/alertmanager/webhook",
                data=b"{not-json",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with self.assertRaises(HTTPError) as ctx:
                request.urlopen(req, timeout=3)
        finally:
            server.stop()

        self.assertEqual(ctx.exception.code, 400)

    def test_mailbox_put_and_get_endpoints_work(self) -> None:
        queued: list[dict[str, object]] = []

        def mailbox_send(payload: dict[str, object]) -> dict[str, object]:
            message = {
                "messageId": "msg-1",
                "conversationId": "conv-1",
                "from": str(payload["from"]),
                "to": str(payload["to"]),
                "body": str(payload["body"]),
                "queuedAt": "2026-03-11T13:40:00Z",
                "status": "queued",
            }
            queued.append(message)
            return message

        def mailbox_receive(mailbox: str) -> dict[str, object] | None:
            for index, message in enumerate(queued):
                if message["to"] != mailbox:
                    continue
                delivered = dict(message)
                delivered["status"] = "delivered"
                delivered["dequeuedAt"] = "2026-03-11T13:40:05Z"
                queued.pop(index)
                return delivered
            return None

        server = HealthServer(
            "127.0.0.1",
            0,
            alive_probe=lambda: True,
            ready_probe=lambda: True,
            mailbox_send=mailbox_send,
            mailbox_receive=mailbox_receive,
        )
        server.start()
        try:
            put_request = request.Request(
                f"http://127.0.0.1:{server.port}/v1/messages",
                data=json.dumps(
                    {
                        "from": "OptionABC001",
                        "to": "OptionDEF002",
                        "body": "hello",
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="PUT",
            )
            with request.urlopen(put_request, timeout=3) as response:
                put_body = json.loads(response.read().decode("utf-8"))
                put_status = response.status

            with request.urlopen(
                f"http://127.0.0.1:{server.port}/v1/messages?for=OptionDEF002",
                timeout=3,
            ) as response:
                get_body = json.loads(response.read().decode("utf-8"))
                get_status = response.status

            with request.urlopen(
                f"http://127.0.0.1:{server.port}/v1/messages?for=OptionDEF002",
                timeout=3,
            ) as response:
                empty_status = response.status
        finally:
            server.stop()

        self.assertEqual(put_status, 202)
        self.assertEqual(get_status, 200)
        self.assertEqual(put_body["status"], "queued")
        self.assertEqual(get_body["status"], "delivered")
        self.assertEqual(get_body["body"], "hello")
        self.assertEqual(empty_status, 204)

    def test_mailbox_endpoints_require_auth_and_bind_authenticated_mailbox(self) -> None:
        queued: list[dict[str, object]] = []

        def mailbox_authenticate(_method: str, headers: dict[str, str]) -> str | None:
            authorization = headers.get("Authorization")
            if authorization == "Bearer token-abc":
                return "OptionABC001"
            raise PermissionError("invalid mailbox credentials")

        def mailbox_send(payload: dict[str, object]) -> dict[str, object]:
            message = {
                "messageId": "msg-auth-1",
                "conversationId": "conv-auth-1",
                "from": str(payload["from"]),
                "to": str(payload["to"]),
                "body": str(payload["body"]),
                "queuedAt": "2026-03-12T00:40:00Z",
                "status": "queued",
            }
            queued.append(message)
            return message

        def mailbox_receive(mailbox: str) -> dict[str, object] | None:
            if mailbox != "OptionABC001":
                return None
            return {
                "messageId": "msg-auth-2",
                "conversationId": "conv-auth-2",
                "from": "OptionDEF002",
                "to": mailbox,
                "body": "reply",
                "queuedAt": "2026-03-12T00:40:05Z",
                "status": "delivered",
                "dequeuedAt": "2026-03-12T00:40:06Z",
            }

        server = HealthServer(
            "127.0.0.1",
            0,
            alive_probe=lambda: True,
            ready_probe=lambda: True,
            mailbox_send=mailbox_send,
            mailbox_receive=mailbox_receive,
            mailbox_authenticate=mailbox_authenticate,
        )
        server.start()
        try:
            put_request = request.Request(
                f"http://127.0.0.1:{server.port}/v1/messages",
                data=json.dumps(
                    {
                        "from": "SpoofedMailbox",
                        "to": "OptionDEF002",
                        "body": "hello",
                    }
                ).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer token-abc",
                },
                method="PUT",
            )
            with request.urlopen(put_request, timeout=3) as response:
                put_body = json.loads(response.read().decode("utf-8"))
                put_status = response.status

            with request.urlopen(
                request.Request(
                    f"http://127.0.0.1:{server.port}/v1/messages",
                    headers={"Authorization": "Bearer token-abc"},
                    method="GET",
                ),
                timeout=3,
            ) as response:
                get_body = json.loads(response.read().decode("utf-8"))
                get_status = response.status

            with self.assertRaises(HTTPError) as ctx:
                request.urlopen(
                    request.Request(
                        f"http://127.0.0.1:{server.port}/v1/messages?for=OptionDEF002",
                        headers={"Authorization": "Bearer token-abc"},
                        method="GET",
                    ),
                    timeout=3,
                )
        finally:
            server.stop()

        self.assertEqual(put_status, 202)
        self.assertEqual(get_status, 200)
        self.assertEqual(put_body["from"], "OptionABC001")
        self.assertEqual(queued[0]["from"], "OptionABC001")
        self.assertEqual(get_body["to"], "OptionABC001")
        self.assertEqual(ctx.exception.code, 403)

    def test_mailbox_endpoints_reject_missing_auth_when_enabled(self) -> None:
        server = HealthServer(
            "127.0.0.1",
            0,
            alive_probe=lambda: True,
            ready_probe=lambda: True,
            mailbox_send=lambda payload: payload,
            mailbox_receive=lambda mailbox: None,
            mailbox_authenticate=lambda _method, _headers: (_ for _ in ()).throw(
                PermissionError("missing Authorization header")
            ),
        )
        server.start()
        try:
            with self.assertRaises(HTTPError) as ctx:
                request.urlopen(
                    request.Request(
                        f"http://127.0.0.1:{server.port}/v1/messages",
                        data=json.dumps({"to": "OptionDEF002", "body": "hello"}).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="PUT",
                    ),
                    timeout=3,
                )
        finally:
            server.stop()

        self.assertEqual(ctx.exception.code, 401)
