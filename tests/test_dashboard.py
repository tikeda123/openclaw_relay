from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json
import logging
import os
import tempfile
import unittest
from unittest import mock
from urllib import request

from openclaw_relay.app import RelayApp
from openclaw_relay.config import (
    AppConfig,
    AuditConfig,
    BehaviorConfig,
    EndpointConfig,
    RelayConfig,
    RetryConfig,
    SecurityConfig,
)
from openclaw_relay.health import HealthServer
from openclaw_relay.response_extractor import ResponseExtractor


def build_config(root: Path) -> AppConfig:
    return AppConfig(
        relay=RelayConfig(
            node_id="relay-a",
            state_dir=root / "state",
            log_dir=root / "log",
            watch_dir=root / "watch",
            archive_dir=root / "archive",
            deadletter_dir=root / "deadletter",
            poll_interval_ms=100,
            health_host="127.0.0.1",
            health_port=18080,
        ),
        endpoint_a=EndpointConfig(
            name="a",
            display_name="OptionABC001",
            base_url="http://127.0.0.1:31879",
            agent_id="main",
            default_session_key="main",
            token_env="A_GATEWAY_TOKEN",
            timeout_seconds=30.0,
        ),
        endpoint_b=EndpointConfig(
            name="b",
            display_name="OptionDEF002",
            base_url="http://127.0.0.1:31901",
            agent_id="main",
            default_session_key="main",
            token_env="B_GATEWAY_TOKEN",
            timeout_seconds=30.0,
        ),
        source_sync=None,
        retry=RetryConfig(
            max_attempts_b=5,
            max_attempts_a=10,
            initial_backoff_ms=1000,
            max_backoff_ms=30000,
            jitter=True,
        ),
        security=SecurityConfig(
            require_private_ingress=True,
            allow_http_only_for_localhost=True,
            mask_secrets_in_logs=True,
        ),
        audit=AuditConfig(
            mode="preview",
            jsonl_path=root / "log" / "a2a-audit.jsonl",
        ),
        behavior=BehaviorConfig(
            schema_version="relay-envelope/v1",
            default_ttl_seconds=300,
            duplicate_policy="suppress",
            inject_notice_on_error=True,
        ),
    )


def build_logger() -> logging.Logger:
    logger = logging.getLogger("openclaw_relay_dashboard_test")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger


class FakeResponsesClient:
    def send_user_message(self, *, endpoint, session_key: str, content: str, token: str | None = None):
        return {
            "id": "resp-dashboard",
            "object": "response",
            "created_at": 1772896430,
            "status": "completed",
            "model": f"openclaw:{endpoint.agent_id}",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "dashboard-ok"}],
                    "status": "completed",
                }
            ],
            "usage": {},
        }


class FakeSessionFetcher:
    def fetch_recent_messages(
        self,
        connection,
        *,
        node_label: str,
        subagent_labels=None,
        include_terms,
        limit_files: int = 8,
        limit_messages: int = 24,
    ):
        return [
            {
                "source": "session",
                "node": node_label,
                "role": "user",
                "at": "2026-03-08T02:11:44Z",
                "text": "次の施策をOptionDEF002と協調しながら考えてください。",
                "sessionFile": "recent.jsonl",
            },
            {
                "source": "session",
                "node": node_label,
                "role": "assistant",
                "kind": "a",
                "sender": node_label,
                "label": "OptionDEF002_next_measures",
                "at": "2026-03-08T02:11:53Z",
                "text": "[Dispatch to OptionDEF002]\nlabel: OptionDEF002_next_measures\n\nTask body",
                "sessionFile": "recent.jsonl",
            },
            {
                "source": "session",
                "node": node_label,
                "role": "assistant",
                "at": "2026-03-08T02:12:05Z",
                "text": "了解です。\n- Label: `OptionDEF002_next_measures`\n- runId: `747abf89-b97b-494a-93d2-26946d61db3f`",
                "sessionFile": "recent.jsonl",
            },
            {
                "source": "session",
                "node": "OptionDEF002",
                "role": "assistant",
                "kind": "b",
                "sender": "OptionDEF002",
                "label": "OptionDEF002_next_measures",
                "at": "2026-03-08T02:12:51Z",
                "text": "以下、Phase A NO-GO後の次手（最大3トラック）を提案します。",
                "sessionFile": "subagent.jsonl",
            },
        ]


def write_envelope(path: Path) -> None:
    payload = {
        "schemaVersion": "relay-envelope/v1",
        "taskId": "TASK-UI-001",
        "turnId": "TURN-UI-001",
        "fromGateway": "OptionABC001",
        "fromAgent": "main",
        "toGateway": "OptionDEF002",
        "toAgent": "main",
        "intent": "request",
        "body": "show the dashboard flow",
        "stateRef": "TASK-UI-001#rev1",
        "returnSessionKey": "main",
        "approvalRequired": False,
        "ttlSeconds": 1800,
        "idempotencyKey": "idem-ui-001",
        "createdAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


class DashboardTests(unittest.TestCase):
    def test_dashboard_payload_contains_message_and_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            config.relay.watch_dir.mkdir(parents=True, exist_ok=True)
            source = config.relay.watch_dir / "message.json"
            write_envelope(source)
            (config.relay.log_dir / "relay.log").parent.mkdir(parents=True, exist_ok=True)
            (config.relay.log_dir / "relay.log").write_text(
                "2026-03-08T01:50:00+0900 INFO dashboard test log\n",
                encoding="utf-8",
            )
            app = RelayApp(
                config,
                build_logger(),
                responses_client=FakeResponsesClient(),
                response_extractor=ResponseExtractor(),
            )
            app.initialize()

            with mock.patch.dict(
                os.environ,
                {"A_GATEWAY_TOKEN": "a", "B_GATEWAY_TOKEN": "b"},
                clear=False,
            ):
                app.poll_once()
            app.receive_alertmanager_webhook(
                {
                    "alerts": [
                        {
                            "status": "firing",
                            "labels": {
                                "alertname": "OpenClawRelayDispatchLatencyHigh",
                                "severity": "warning",
                                "worker": "OptionDEF002",
                            },
                            "annotations": {
                                "summary": "dispatch latency is high",
                            },
                            "startsAt": "2026-03-08T02:20:00Z",
                            "generatorURL": "http://127.0.0.1:9090/graph",
                            "fingerprint": "fp-warning-1",
                        }
                    ]
                }
            )

            payload = app.dashboard_payload()
            json.dumps(payload)

            self.assertEqual(payload["nodeId"], "relay-a")
            self.assertEqual(payload["messages"][0]["taskId"], "TASK-UI-001")
            self.assertEqual(payload["messages"][0]["replyText"], "dashboard-ok")
            self.assertTrue(str(payload["messages"][0]["createdAt"]).endswith("Z"))
            self.assertTrue(str(payload["messages"][0]["updatedAt"]).endswith("Z"))
            self.assertTrue(payload["auditEvents"])
            self.assertTrue(payload["logTail"])
            self.assertEqual(payload["alerts"]["summary"]["total"], 1)
            self.assertEqual(payload["alerts"]["summary"]["warning"], 1)
            self.assertEqual(payload["alerts"]["active"][0]["alertname"], "OpenClawRelayDispatchLatencyHigh")
            self.assertEqual(payload["metrics"]["attemptCounts"]["A"]["SUCCESS"], 1)
            self.assertEqual(payload["metrics"]["attemptCounts"]["B"]["SUCCESS"], 1)
            self.assertEqual(payload["metrics"]["alertSummary"]["warning"], 1)
            self.assertEqual(payload["workers"][0]["displayName"], "OptionDEF002")
            self.assertEqual(
                payload["metrics"]["workerAttemptCounts"]["OptionDEF002"]["A"]["SUCCESS"],
                1,
            )
            self.assertEqual(
                payload["metrics"]["workerAttemptCounts"]["OptionDEF002"]["B"]["SUCCESS"],
                1,
            )
            self.assertEqual(
                payload["metrics"]["workerStatusCounts"]["OptionDEF002"]["DONE"],
                1,
            )
            self.assertEqual(
                payload["metrics"]["workerLatency"]["OptionDEF002"]["dispatch"]["sampleCount"],
                1,
            )
            self.assertEqual(
                payload["metrics"]["workerLatency"]["OptionDEF002"]["inject"]["sampleCount"],
                1,
            )
            self.assertEqual(payload["workers"][0]["retrySummary"]["aSuccess"], 1)
            self.assertEqual(payload["workers"][0]["retrySummary"]["bSuccess"], 1)
            self.assertEqual(payload["workers"][0]["deadletterCounts"]["A"], 0)
            self.assertEqual(payload["workers"][0]["deadletterCounts"]["B"], 0)
            self.assertIn("dispatch", payload["workers"][0]["latency"])
            self.assertIn("inject", payload["workers"][0]["latency"])

            metrics_text = app.metrics_text()
            self.assertIn(
                'openclaw_relay_worker_attempt_total{worker="OptionDEF002",target="A",result="SUCCESS"} 1',
                metrics_text,
            )
            self.assertIn(
                'openclaw_relay_worker_attempt_total{worker="OptionDEF002",target="B",result="SUCCESS"} 1',
                metrics_text,
            )
            self.assertIn(
                'openclaw_relay_worker_deadletter_total{worker="OptionDEF002",target="A"} 0',
                metrics_text,
            )
            self.assertIn(
                'openclaw_relay_worker_deadletter_total{worker="OptionDEF002",target="B"} 0',
                metrics_text,
            )
            self.assertIn(
                'openclaw_relay_worker_latency_sample_count{worker="OptionDEF002",stage="dispatch"} 1',
                metrics_text,
            )
            self.assertIn(
                'openclaw_relay_worker_latency_sample_count{worker="OptionDEF002",stage="inject"} 1',
                metrics_text,
            )
            self.assertIn(
                'openclaw_relay_worker_latency_seconds{worker="OptionDEF002",stage="dispatch",stat="avg"}',
                metrics_text,
            )
            self.assertIn(
                'openclaw_relay_worker_latency_seconds{worker="OptionDEF002",stage="inject",stat="avg"}',
                metrics_text,
            )
            self.assertIn("openclaw_relay_alertmanager_active_alerts 1", metrics_text)
            self.assertIn(
                'openclaw_relay_alertmanager_active_alerts_by_severity{severity="warning"} 1',
                metrics_text,
            )

    def test_health_server_serves_ui_and_dashboard_api(self) -> None:
        server = HealthServer(
            "127.0.0.1",
            0,
            alive_probe=lambda: True,
            ready_probe=lambda: True,
            dashboard_probe=lambda: {"ok": True},
            dashboard_html="<html><body>ui</body></html>",
            ops_html="<html><body>ops</body></html>",
        )
        server.start()
        try:
            with request.urlopen(f"http://127.0.0.1:{server.port}/", timeout=3) as response:
                html = response.read().decode("utf-8")
            with request.urlopen(f"http://127.0.0.1:{server.port}/ops", timeout=3) as response:
                ops_html = response.read().decode("utf-8")
            with request.urlopen(f"http://127.0.0.1:{server.port}/api/dashboard", timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            server.stop()

        self.assertIn("ui", html)
        self.assertIn("ops", ops_html)
        self.assertTrue(payload["ok"])

    def test_dashboard_payload_prefers_assistant_session_entries_and_extracts_label(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = build_config(root)
            app = RelayApp(
                config,
                build_logger(),
                remote_session_fetcher=FakeSessionFetcher(),
            )
            app.initialize()

            with mock.patch.object(app, "_session_monitor_connection", return_value=object()):
                timeline = app.dashboard_payload()["timeline"]

            session_entries = [item for item in timeline if item["source"] == "session"]
            self.assertEqual(len(session_entries), 3)
            self.assertEqual(
                [item["sender"] for item in session_entries],
                ["OptionABC001", "OptionABC001", "OptionDEF002"],
            )
            self.assertEqual(
                [item["worker"] for item in session_entries],
                ["OptionDEF002", "OptionDEF002", "OptionDEF002"],
            )
            self.assertTrue(all(item["taskId"] == "OptionDEF002_next_measures" for item in session_entries))
            self.assertEqual(session_entries[-1]["kind"], "b")
