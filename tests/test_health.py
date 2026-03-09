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
