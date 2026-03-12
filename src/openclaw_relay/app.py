from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import json
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
from collections import deque
from urllib import error as urllib_error
from urllib import request as urllib_request

from openclaw_relay.audit import AuditLogger
from openclaw_relay.alerts import AlertStore
from openclaw_relay.broker import RabbitMQBroker, RabbitMQError
from openclaw_relay.config import AppConfig, ConfigError
from openclaw_relay.dashboard import render_dashboard_html, render_ops_html
from openclaw_relay.envelope import Envelope, EnvelopeError, resolve_return_session_key
from openclaw_relay.health import HealthServer
from openclaw_relay.rabbitmq_runtime import RabbitMQReply
from openclaw_relay.remote import (
    RemoteError,
    RemoteSessionFetcher,
    RemoteTokenResolver,
    TunnelManager,
)
from openclaw_relay.response_extractor import ResponseExtractor, ResponseExtractorError
from openclaw_relay.responses_client import ResponsesClient, ResponsesClientError
from openclaw_relay.spool import SpoolWriter
from openclaw_relay.state import StateStore
from openclaw_relay.watcher import Watcher


class RelayApp:
    EXTERNAL_HEALTH_INTERVAL_SECONDS = 5.0
    TOKEN_CACHE_TTL_SECONDS = 300.0

    def __init__(
        self,
        config: AppConfig,
        logger: logging.Logger,
        *,
        responses_client: ResponsesClient | None = None,
        response_extractor: ResponseExtractor | None = None,
        tunnel_manager: TunnelManager | None = None,
        remote_token_resolver: RemoteTokenResolver | None = None,
        remote_session_fetcher: RemoteSessionFetcher | None = None,
        rabbitmq_broker: RabbitMQBroker | None = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self.store = StateStore(config.relay.database_path)
        self.watcher = Watcher(config.relay.watch_dir)
        self.responses_client = responses_client or ResponsesClient()
        self.response_extractor = response_extractor or ResponseExtractor()
        self.tunnel_manager = tunnel_manager or TunnelManager()
        self.remote_token_resolver = remote_token_resolver or RemoteTokenResolver()
        self.remote_session_fetcher = remote_session_fetcher or RemoteSessionFetcher()
        self.spool = SpoolWriter(
            processing_dir=config.relay.processing_dir,
            archive_dir=config.relay.archive_dir,
            deadletter_dir=config.relay.deadletter_dir,
        )
        self.audit = AuditLogger(
            jsonl_path=config.audit.jsonl_path,
            mode=config.audit.mode,
            store=self.store,
        )
        self._stop_event = threading.Event()
        self._health_server: HealthServer | None = None
        self._token_cache: dict[str, tuple[str, float]] = {}
        self._started_at_monotonic = time.monotonic()
        self._last_poll_completed_at: float | None = None
        self._last_poll_processed_count = 0
        self._session_cache_generated_at = 0.0
        self._session_cache_entries: list[dict[str, object]] = []
        self._last_external_health_check_at_monotonic = 0.0
        self._endpoint_tunnel_health = {
            endpoint.name: endpoint.tunnel is None
            for endpoint in self._all_endpoints()
        }
        self._endpoint_http_health = {
            endpoint.name: False
            for endpoint in self._all_endpoints()
        }
        self._endpoint_last_error: dict[str, str | None] = {
            endpoint.name: None
            for endpoint in self._all_endpoints()
        }
        self._alert_store = AlertStore(self.config.relay.state_dir / "alertmanager-alerts.json")
        self._rabbitmq_broker = rabbitmq_broker
        if self._rabbitmq_broker is None:
            self._rabbitmq_broker = (
                RabbitMQBroker(config)
                if self.config.transport_mode == "rabbitmq" and self.config.rabbitmq is not None
                else None
            )

    def initialize(self) -> None:
        if self.config.transport_mode == "rabbitmq" and self._rabbitmq_broker is None:
            raise ConfigError("routing.transport='rabbitmq' requires a [rabbitmq] configuration")
        _validate_mailbox_auth_environment(self.config)
        self._ensure_runtime_dirs()
        self.store.initialize()
        _ensure_rabbitmq_topology_if_available(self._rabbitmq_broker, self.logger)
        self.logger.info("initialized relay state at %s", self.config.relay.state_dir)

    def run(self, *, once: bool = False) -> None:
        self.initialize()
        self._ensure_endpoint_tunnels()
        self._refresh_external_health(force=True)
        self._health_server = HealthServer(
            self.config.relay.health_host,
            self.config.relay.health_port,
            alive_probe=lambda: True,
            ready_probe=self.is_ready,
            metrics_probe=self.metrics_text,
            dashboard_probe=self.dashboard_payload,
            dashboard_html=render_dashboard_html(),
            ops_html=render_ops_html(),
            alert_webhook=self.receive_alertmanager_webhook,
            mailbox_send=self.put_mailbox_message,
            mailbox_receive=self.get_mailbox_message,
            mailbox_authenticate=self.authenticate_mailbox_request,
        )
        self._health_server.start()
        self.logger.info(
            "health endpoints listening on http://%s:%s",
            self.config.relay.health_host,
            self.config.relay.health_port,
        )

        try:
            if once:
                pending_count = self.poll_once()
                self.logger.info("single polling cycle completed with %s pending files", pending_count)
                return

            self.logger.info(
                "relay run loop started with poll_interval_ms=%s",
                self.config.relay.poll_interval_ms,
            )
            while not self._stop_event.is_set():
                self.poll_once()
                time.sleep(self.config.relay.poll_interval_ms / 1000.0)
        finally:
            if self._health_server is not None:
                self._health_server.stop()
                self._health_server = None

    def stop(self) -> None:
        self._stop_event.set()

    def is_ready(self) -> bool:
        if not self.config.relay.watch_dir.exists():
            return False
        if not self.config.relay.watch_dir.is_dir():
            return False
        if not os_access_readable(self.config.relay.watch_dir):
            return False
        if not os_access_writable(self.config.relay.state_dir):
            return False
        if not os_access_writable(self.config.relay.log_dir):
            return False
        for path in self.spool.local_dirs():
            if not os_access_writable(path):
                return False
        for endpoint in self._all_endpoints():
            if endpoint.tunnel is not None and not self._endpoint_tunnel_health.get(endpoint.name, False):
                return False
            if not self._endpoint_http_health.get(endpoint.name, False):
                return False
        return self.store.healthcheck()

    def poll_once(self) -> int:
        processed_count = 0
        self._refresh_external_health()
        pending_files = self.watcher.list_pending_files()
        for path in pending_files:
            if self._process_pending_file(path):
                processed_count += 1
        processed_count += self._dispatch_reserved_messages()
        processed_count += self._consume_rabbitmq_replies()
        processed_count += self._collect_queued_worker_replies()
        processed_count += self._inject_replied_messages()
        if pending_files:
            self.logger.info(
                "scan complete: seen=%s newly_processed=%s",
                len(pending_files),
                processed_count,
            )
        self._last_poll_processed_count = processed_count
        self._last_poll_completed_at = time.time()
        return processed_count

    def metrics_text(self) -> str:
        snapshot = self.metrics_snapshot()
        lines = [
            "# HELP openclaw_relay_up Relay process is running.",
            "# TYPE openclaw_relay_up gauge",
            "openclaw_relay_up 1",
            "# HELP openclaw_relay_ready Relay readiness state.",
            "# TYPE openclaw_relay_ready gauge",
            f"openclaw_relay_ready {1 if snapshot['ready'] else 0}",
            "# HELP openclaw_relay_uptime_seconds Relay process uptime in seconds.",
            "# TYPE openclaw_relay_uptime_seconds gauge",
            f"openclaw_relay_uptime_seconds {snapshot['uptimeSeconds']:.3f}",
            "# HELP openclaw_relay_watch_present_files JSON files currently visible in watch_dir.",
            "# TYPE openclaw_relay_watch_present_files gauge",
            f"openclaw_relay_watch_present_files {snapshot['watchPresentFiles']}",
            "# HELP openclaw_relay_watch_pending_files JSON files in watch_dir that are not yet terminally handled.",
            "# TYPE openclaw_relay_watch_pending_files gauge",
            f"openclaw_relay_watch_pending_files {snapshot['watchPendingFiles']}",
            "# HELP openclaw_relay_last_poll_processed_count Files and messages processed in last poll.",
            "# TYPE openclaw_relay_last_poll_processed_count gauge",
            f"openclaw_relay_last_poll_processed_count {self._last_poll_processed_count}",
        ]
        if self._last_poll_completed_at is not None:
            lines.extend(
                [
                    "# HELP openclaw_relay_last_poll_completed_unixtime Last completed poll time.",
                    "# TYPE openclaw_relay_last_poll_completed_unixtime gauge",
                    f"openclaw_relay_last_poll_completed_unixtime {self._last_poll_completed_at:.3f}",
                ]
            )
        for endpoint_name, enabled in snapshot["endpointTunnels"].items():
            lines.append(
                f'openclaw_relay_endpoint_tunnel_configured{{endpoint="{endpoint_name}"}} '
                f'{1 if enabled else 0}'
            )
        for endpoint_name, healthy in snapshot["endpointTunnelHealth"].items():
            lines.append(
                f'openclaw_relay_endpoint_tunnel_healthy{{endpoint="{endpoint_name}"}} '
                f'{1 if healthy else 0}'
            )
        for endpoint_name, healthy in snapshot["endpointHttpHealth"].items():
            lines.append(
                f'openclaw_relay_endpoint_http_healthy{{endpoint="{endpoint_name}"}} '
                f'{1 if healthy else 0}'
            )
        for status, count in snapshot["messageStatusCounts"].items():
            lines.append(
                f'openclaw_relay_message_status_total{{status="{_prom_label(status)}"}} {count}'
            )
        worker_status_counts = snapshot.get("workerStatusCounts", {})
        if isinstance(worker_status_counts, dict):
            for worker_name, status_counts in worker_status_counts.items():
                if not isinstance(status_counts, dict):
                    continue
                for status, count in status_counts.items():
                    lines.append(
                        "openclaw_relay_worker_message_status_total"
                        f'{{worker="{_prom_label(str(worker_name))}",status="{_prom_label(str(status))}"}} {int(count)}'
                    )
        worker_attempt_counts = snapshot.get("workerAttemptCounts", {})
        if isinstance(worker_attempt_counts, dict):
            for worker_name, target_counts in worker_attempt_counts.items():
                if not isinstance(target_counts, dict):
                    continue
                for target, result_counts in target_counts.items():
                    if not isinstance(result_counts, dict):
                        continue
                    for result, count in result_counts.items():
                        lines.append(
                            "openclaw_relay_worker_attempt_total"
                            f'{{worker="{_prom_label(str(worker_name))}",target="{_prom_label(str(target))}",result="{_prom_label(str(result))}"}} {int(count)}'
                        )
        worker_deadletter_counts = snapshot.get("workerDeadletterCounts", {})
        if isinstance(worker_deadletter_counts, dict):
            for worker_name, target_counts in worker_deadletter_counts.items():
                if not isinstance(target_counts, dict):
                    continue
                for target, count in target_counts.items():
                    lines.append(
                        "openclaw_relay_worker_deadletter_total"
                        f'{{worker="{_prom_label(str(worker_name))}",target="{_prom_label(str(target))}"}} {int(count)}'
                    )
        worker_latency = snapshot.get("workerLatency", {})
        if isinstance(worker_latency, dict):
            for worker_name, stage_stats in worker_latency.items():
                if not isinstance(stage_stats, dict):
                    continue
                for stage, stats in stage_stats.items():
                    if not isinstance(stats, dict):
                        continue
                    sample_count = int(stats.get("sampleCount", 0))
                    lines.append(
                        "openclaw_relay_worker_latency_sample_count"
                        f'{{worker="{_prom_label(str(worker_name))}",stage="{_prom_label(str(stage))}"}} {sample_count}'
                    )
                    for stat_name in ("avgSeconds", "latestSeconds", "maxSeconds"):
                        value = float(stats.get(stat_name, 0.0))
                        prom_stat = {
                            "avgSeconds": "avg",
                            "latestSeconds": "latest",
                            "maxSeconds": "max",
                        }[stat_name]
                        lines.append(
                            "openclaw_relay_worker_latency_seconds"
                            f'{{worker="{_prom_label(str(worker_name))}",stage="{_prom_label(str(stage))}",stat="{prom_stat}"}} {value:.3f}'
                        )
        alert_summary = snapshot.get("alertSummary", {})
        if isinstance(alert_summary, dict):
            lines.append(
                "# HELP openclaw_relay_alertmanager_active_alerts Current active Alertmanager alerts."
            )
            lines.append("# TYPE openclaw_relay_alertmanager_active_alerts gauge")
            lines.append(
                "# HELP openclaw_relay_alertmanager_active_alerts_by_severity Current active Alertmanager alerts by severity."
            )
            lines.append(
                "# TYPE openclaw_relay_alertmanager_active_alerts_by_severity gauge"
            )
            total_alerts = int(alert_summary.get("total", 0))
            lines.append(f"openclaw_relay_alertmanager_active_alerts {total_alerts}")
            for severity, count in alert_summary.items():
                if severity == "total":
                    continue
                lines.append(
                    "openclaw_relay_alertmanager_active_alerts_by_severity"
                    f'{{severity="{_prom_label(str(severity))}"}} {int(count)}'
                )
        last_alertmanager_webhook_at = snapshot.get("lastAlertmanagerWebhookAt")
        if isinstance(last_alertmanager_webhook_at, str):
            parsed_webhook_at = _parse_timestamp(last_alertmanager_webhook_at)
            if parsed_webhook_at is not None:
                lines.append(
                    "# HELP openclaw_relay_alertmanager_webhook_last_received_unixtime Last received Alertmanager webhook time."
                )
                lines.append(
                    "# TYPE openclaw_relay_alertmanager_webhook_last_received_unixtime gauge"
                )
                lines.append(
                    "openclaw_relay_alertmanager_webhook_last_received_unixtime "
                    f"{parsed_webhook_at.timestamp():.3f}"
                )
        for status, count in snapshot["seenStatusCounts"].items():
            lines.append(
                f'openclaw_relay_seen_file_status_total{{status="{_prom_label(status)}"}} {count}'
            )
        for (target, result), count in snapshot["attemptCounts"].items():
            lines.append(
                "openclaw_relay_attempt_total"
                f'{{target="{_prom_label(target)}",result="{_prom_label(result)}"}} {count}'
            )
        for spool_kind, count in snapshot["spoolFiles"].items():
            lines.append(
                f'openclaw_relay_spool_files{{kind="{_prom_label(spool_kind)}"}} {count}'
            )
        mailbox_status_counts = snapshot.get("mailboxMessageStatusCounts", {})
        if isinstance(mailbox_status_counts, dict):
            for status, count in mailbox_status_counts.items():
                lines.append(
                    "openclaw_relay_mailbox_message_status_total"
                    f'{{status="{_prom_label(str(status))}"}} {int(count)}'
                )
        mailbox_queue_depths = snapshot.get("mailboxQueueDepths", {})
        if isinstance(mailbox_queue_depths, dict):
            for mailbox, count in mailbox_queue_depths.items():
                lines.append(
                    "openclaw_relay_mailbox_queue_depth"
                    f'{{mailbox="{_prom_label(str(mailbox))}"}} {int(count)}'
                )
        return "\n".join(lines) + "\n"

    def metrics_snapshot(self) -> dict[str, object]:
        attempt_counts = self.store.count_attempts_by_target_result()
        worker_attempt_counts = self._worker_attempt_counts()
        worker_status_counts = self._worker_status_counts()
        worker_latency = self._worker_latency_stats()
        alert_snapshot = self._alert_store.snapshot()
        return {
            "up": True,
            "ready": self.is_ready(),
            "uptimeSeconds": max(0.0, time.monotonic() - self._started_at_monotonic),
            "watchPresentFiles": len(self.watcher.list_pending_files()),
            "watchPendingFiles": self._count_pending_watch_files(),
            "endpointTunnels": {
                endpoint.name: endpoint.tunnel is not None
                for endpoint in self._all_endpoints()
            },
            "endpointTunnelHealth": dict(self._endpoint_tunnel_health),
            "endpointHttpHealth": dict(self._endpoint_http_health),
            "messageStatusCounts": self.store.count_messages_by_status(),
            "seenStatusCounts": self.store.count_seen_files_by_status(),
            "attemptCounts": attempt_counts,
            "workerAttemptCounts": worker_attempt_counts,
            "spoolFiles": {
                "processing": _count_json_files(self.config.relay.processing_dir),
                "archive": _count_json_files(self.config.relay.archive_dir),
                "deadletter": _count_json_files(self.config.relay.deadletter_dir),
                "replies": _count_json_files(self.config.relay.replies_dir),
            },
            "attemptSummary": {
                "aSuccess": attempt_counts.get(("A", "SUCCESS"), 0),
                "aFailed": attempt_counts.get(("A", "FAILED"), 0),
                "bSuccess": attempt_counts.get(("B", "SUCCESS"), 0),
                "bFailed": attempt_counts.get(("B", "FAILED"), 0),
                "total": sum(attempt_counts.values()),
            },
            "workerRetrySummary": self._worker_retry_summary(worker_attempt_counts),
            "workerStatusCounts": worker_status_counts,
            "workerDeadletterCounts": self._worker_deadletter_counts(worker_status_counts),
            "workerLatency": worker_latency,
            "alertSummary": alert_snapshot["summary"],
            "lastAlertmanagerWebhookAt": alert_snapshot["lastReceivedAt"],
            "mailboxMessageStatusCounts": self.store.count_mailbox_messages_by_status(),
            "mailboxQueueDepths": self.store.count_mailbox_queue_depths(),
        }

    def dashboard_payload(self) -> dict[str, object]:
        metrics = self.metrics_snapshot()
        dashboard_metrics = dict(metrics)
        dashboard_metrics["attemptCounts"] = _json_safe_attempt_counts(metrics["attemptCounts"])
        dashboard_metrics["workerAttemptCounts"] = _json_safe_worker_attempt_counts(
            metrics["workerAttemptCounts"]
        )
        return {
            "nodeId": self.config.relay.node_id,
            "timestamp": time.time(),
            "lastPollCompletedAt": self._last_poll_completed_at,
            "lastPollProcessedCount": self._last_poll_processed_count,
            "metrics": dashboard_metrics,
            "alerts": self._alert_store.snapshot(),
            "workers": self._dashboard_workers(metrics),
            "timeline": self._relay_timeline_entries(limit=80),
            "sessionTimeline": self._session_timeline_entries(limit=80),
            "messages": self._recent_messages_payload(limit=12),
            "auditEvents": self._recent_audit_events(limit=24),
            "logTail": self._recent_log_lines(limit=36),
        }

    def receive_alertmanager_webhook(self, payload: dict[str, object]) -> dict[str, object]:
        result = self._alert_store.record_webhook(payload)
        snapshot = self._alert_store.snapshot()
        self.logger.warning(
            "received alertmanager webhook: alerts=%s active=%s critical=%s warning=%s",
            result["received"],
            snapshot["summary"].get("total", 0),
            snapshot["summary"].get("critical", 0),
            snapshot["summary"].get("warning", 0),
        )
        return {
            "status": "accepted",
            "received": result["received"],
            "active": snapshot["summary"].get("total", 0),
            "critical": snapshot["summary"].get("critical", 0),
            "warning": snapshot["summary"].get("warning", 0),
        }

    def put_mailbox_message(self, payload: dict[str, object]) -> dict[str, object]:
        normalized_payload = _normalized_mailbox_payload(payload)
        message_id = str(normalized_payload["messageId"])
        try:
            row = self.store.enqueue_mailbox_message(
                message_id=message_id,
                conversation_id=str(normalized_payload["conversationId"]),
                from_mailbox=str(normalized_payload["from"]),
                to_mailbox=str(normalized_payload["to"]),
                body=str(normalized_payload["body"]),
                notify_human=bool(normalized_payload.get("notifyHuman")),
                in_reply_to=_optional_mailbox_string(normalized_payload, "inReplyTo"),
                queued_at=str(normalized_payload["queuedAt"]),
                status="publishing" if self._uses_rabbitmq_transport() else "queued",
            )
        except sqlite3.IntegrityError as exc:
            self.logger.error(
                "failed to queue mailbox message messageId=%s from=%s to=%s: %s",
                message_id,
                normalized_payload["from"],
                normalized_payload["to"],
                exc,
            )
            raise ValueError(f"messageId {message_id!r} already exists") from exc
        if self._uses_rabbitmq_transport():
            if self._rabbitmq_broker is None:
                raise ConfigError("RabbitMQ transport is enabled but broker is not configured")
            try:
                self._rabbitmq_broker.publish_mailbox_message(normalized_payload)
            except (RabbitMQError, OSError) as exc:
                self.store.update_mailbox_message_status(
                    message_id=message_id,
                    status="failed",
                )
                self.logger.error(
                    "failed to publish mailbox message messageId=%s from=%s to=%s: %s",
                    message_id,
                    normalized_payload["from"],
                    normalized_payload["to"],
                    exc,
                )
                raise
            updated_row = self.store.update_mailbox_message_status(
                message_id=message_id,
                status="queued",
            )
            if updated_row is not None:
                row = updated_row
        self.audit.append(
            "mailbox_message_queued",
            payload={
                "messageId": row["message_id"],
                "conversationId": row["conversation_id"],
                "from": row["from_mailbox"],
                "to": row["to_mailbox"],
                "inReplyTo": row["in_reply_to"],
            },
        )
        return _mailbox_row_payload(row)

    def get_mailbox_message(self, mailbox: str) -> dict[str, object] | None:
        normalized_mailbox = mailbox.strip()
        if not normalized_mailbox:
            raise ValueError("mailbox name must be a non-empty string")
        if self._uses_rabbitmq_transport():
            if self._rabbitmq_broker is None:
                raise ConfigError("RabbitMQ transport is enabled but broker is not configured")
            delivery = self._rabbitmq_broker.consume_mailbox_message(normalized_mailbox)
            if delivery is None:
                return None
            try:
                message_payload = _mailbox_delivery_payload(
                    json.loads(delivery.body.decode("utf-8")),
                    dequeued_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                )
                row = self.store.update_mailbox_message_status(
                    message_id=str(message_payload["messageId"]),
                    status="delivered",
                    dequeued_at=str(message_payload["dequeuedAt"]),
                )
                delivery.ack()
            except ValueError as exc:
                delivery.nack(requeue=False)
                raise RuntimeError(f"invalid mailbox message payload: {exc}") from exc
            except Exception:
                delivery.nack(requeue=True)
                raise
            payload = _mailbox_row_payload(row) if row is not None else message_payload
        else:
            row = self.store.dequeue_mailbox_message(mailbox=normalized_mailbox)
            if row is None:
                return None
            payload = _mailbox_row_payload(row)
        self.audit.append(
            "mailbox_message_dequeued",
            payload={
                "messageId": payload["messageId"],
                "conversationId": payload["conversationId"],
                "from": payload["from"],
                "to": payload["to"],
                "mailbox": normalized_mailbox,
            },
        )
        return payload

    def authenticate_mailbox_request(
        self,
        method: str,
        headers: dict[str, str],
    ) -> str | None:
        del method
        mailbox_auth = self.config.mailbox_auth
        if mailbox_auth is None:
            return None
        header_value = _header_value(headers, mailbox_auth.header_name)
        if header_value is None:
            raise PermissionError(f"missing {mailbox_auth.header_name} header")
        token = _extract_mailbox_auth_token(
            header_value=header_value,
            expected_scheme=mailbox_auth.scheme,
        )
        mailbox = mailbox_auth.resolve_authenticated_mailbox(token)
        if mailbox is None:
            raise PermissionError("invalid mailbox credentials")
        return mailbox

    def _dashboard_workers(self, metrics: dict[str, object]) -> list[dict[str, object]]:
        worker_counts = metrics.get("workerStatusCounts", {})
        worker_attempt_counts = metrics.get("workerAttemptCounts", {})
        worker_retry_summary = metrics.get("workerRetrySummary", {})
        worker_deadletter_counts = metrics.get("workerDeadletterCounts", {})
        worker_latency = metrics.get("workerLatency", {})
        result: list[dict[str, object]] = []
        for worker in self.config.worker_endpoints():
            status_counts = {}
            if isinstance(worker_counts, dict):
                status_counts = worker_counts.get(worker.display_name, {})
            attempt_counts = {}
            if isinstance(worker_attempt_counts, dict):
                attempt_counts = worker_attempt_counts.get(worker.display_name, {})
            retry_summary = {}
            if isinstance(worker_retry_summary, dict):
                retry_summary = worker_retry_summary.get(worker.display_name, {})
            deadletter_counts = {}
            if isinstance(worker_deadletter_counts, dict):
                deadletter_counts = worker_deadletter_counts.get(worker.display_name, {})
            latency = {}
            if isinstance(worker_latency, dict):
                latency = worker_latency.get(worker.display_name, {})
            result.append(
                {
                    "name": worker.name,
                    "displayName": worker.display_name,
                    "tunnelHealthy": self._endpoint_tunnel_health.get(worker.name, False),
                    "httpHealthy": self._endpoint_http_health.get(worker.name, False),
                    "attemptCounts": attempt_counts,
                    "retrySummary": retry_summary,
                    "deadletterCounts": deadletter_counts,
                    "latency": latency,
                    "messageStatusCounts": status_counts,
                }
            )
        return result

    def _count_pending_watch_files(self) -> int:
        pending = 0
        for path in self.watcher.list_pending_files():
            try:
                source_key = self._build_source_key(path)
            except OSError:
                pending += 1
                continue

            seen_row = self.store.get_seen_file(source_key)
            if seen_row is None or seen_row["status"] in {"DISCOVERED", "FILE_COPY_FAILED"}:
                pending += 1
        return pending

    def _recent_messages_payload(self, *, limit: int) -> list[dict[str, object]]:
        rows = self.store.list_messages()[-limit:]
        messages: list[dict[str, object]] = []
        for row in reversed(rows):
            request_meta = _load_request_metadata(row["processing_path"])
            messages.append(
                {
                    "id": row["id"],
                    "conversationId": request_meta.get("conversationId", row["task_id"]),
                    "messageId": request_meta.get("messageId", row["turn_id"]),
                    "taskId": row["task_id"],
                    "turnId": row["turn_id"],
                    "idempotencyKey": row["idempotency_key"],
                    "filename": row["filename"],
                    "status": row["status"],
                    "from": request_meta.get("from", self.config.endpoint_a.display_name),
                    "to": self._message_worker_display_name(row, request_meta=request_meta),
                    "fromGateway": request_meta.get("fromGateway", self.config.endpoint_a.display_name),
                    "toGateway": self._message_worker_display_name(row, request_meta=request_meta),
                    "workerName": row["worker_name"] or self._message_worker_name(row),
                    "intent": request_meta.get("intent"),
                    "requestBody": request_meta.get("body"),
                    "replyText": row["reply_text"],
                    "returnSessionKey": request_meta.get("returnSessionKey", self.config.endpoint_a.default_session_key),
                    "attempts": {
                        "a": self.store.count_attempts(message_id=row["id"], target="A"),
                        "b": self.store.count_attempts(message_id=row["id"], target="B"),
                    },
                    "lastError": row["last_error"],
                    "createdAt": _normalize_timestamp(row["created_at"]),
                    "updatedAt": _normalize_timestamp(row["updated_at"]),
                }
            )
        return messages

    def _relay_timeline_entries(self, *, limit: int) -> list[dict[str, object]]:
        timeline: list[dict[str, object]] = []
        for message in reversed(self._recent_messages_payload(limit=limit)):
            timeline.append(
                {
                    "source": "relay",
                    "kind": "a",
                    "from": message["from"],
                    "to": message["to"],
                    "sender": message["fromGateway"],
                    "conversationId": message["conversationId"],
                    "messageId": message["messageId"],
                    "taskId": message["taskId"],
                    "status": message["status"],
                    "at": message["createdAt"] or message["updatedAt"],
                    "body": message["requestBody"] or "(request body unavailable)",
                    "worker": message["toGateway"],
                }
            )
            if message["replyText"]:
                timeline.append(
                    {
                        "source": "relay",
                        "kind": "b",
                        "from": message["to"],
                        "to": message["from"],
                        "sender": message["toGateway"],
                        "conversationId": message["conversationId"],
                        "messageId": message["messageId"],
                        "taskId": message["taskId"],
                        "status": message["status"],
                        "at": message["updatedAt"],
                        "body": message["replyText"],
                        "worker": message["toGateway"],
                    }
                )
            else:
                timeline.append(
                    {
                        "source": "relay",
                        "kind": "relay",
                        "sender": "Relay",
                        "conversationId": message["conversationId"],
                        "messageId": message["messageId"],
                        "taskId": message["taskId"],
                        "status": message["status"],
                        "at": message["updatedAt"],
                        "body": message["lastError"] or f"{message['toGateway']} からの返答待ちです。",
                        "worker": message["toGateway"],
                    }
                )
        return timeline[-limit:]

    def _session_timeline_entries(self, *, limit: int) -> list[dict[str, object]]:
        now = time.time()
        if now - self._session_cache_generated_at <= 10 and self._session_cache_entries:
            return self._session_cache_entries[-limit:]

        connection = self._session_monitor_connection()
        if connection is None:
            self._session_cache_generated_at = now
            self._session_cache_entries = []
            return []

        include_terms = (
            *self.config.worker_display_names(),
            *(f"[Reply from {name}]" for name in self.config.worker_display_names()),
            "childSessionKey",
            "runId",
            "Subagent Task",
        )
        try:
            entries = self.remote_session_fetcher.fetch_recent_messages(
                connection,
                node_label=self.config.endpoint_a.display_name,
                subagent_labels=self.config.worker_display_names(),
                include_terms=include_terms,
                limit_messages=limit,
            )
        except RemoteError as exc:
            self.logger.debug("remote session fetch skipped: %s", exc)
            entries = []

        session_label_by_key: dict[str, str] = {}
        for entry in entries:
            text = entry.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            session_key = _extract_session_key(text)
            label = _extract_session_label(text)
            if session_key and label:
                session_label_by_key[session_key] = label

        cleaned_entries: list[dict[str, object]] = []
        for entry in entries:
            text = entry.get("text")
            at = entry.get("at")
            role = entry.get("role")
            if not isinstance(text, str) or not text.strip():
                continue
            if not isinstance(at, str) or not at.strip():
                continue
            normalized_at = _normalize_timestamp(at)
            if not normalized_at:
                continue
            internal_child_completion = (
                isinstance(role, str)
                and role == "user"
                and _is_internal_child_completion(text)
            )
            if role != "assistant" and not internal_child_completion:
                continue
            sender = entry.get("sender")
            if internal_child_completion:
                sender = _extract_internal_child_task_name(text)
            if not isinstance(sender, str) or not sender.strip():
                if internal_child_completion:
                    sender = "Subagent"
                else:
                    sender = self.config.endpoint_a.display_name if role == "assistant" else "Human"
            kind = entry.get("kind")
            if internal_child_completion:
                kind = "b"
            elif kind not in {"a", "b", "relay"}:
                kind = "a" if role == "assistant" else "relay"
            label = entry.get("label")
            worker = self._infer_session_worker(
                entry=entry,
                sender=sender,
                kind=kind,
                text=text,
            )
            fallback_task_id = entry.get("sessionFile")
            if not isinstance(fallback_task_id, str) or not fallback_task_id.strip():
                fallback_task_id = "session"
            if internal_child_completion:
                session_key = _extract_session_key(text)
                task_id = session_label_by_key.get(session_key or "")
                if not task_id:
                    task_id = _extract_session_task_id(
                        text=text,
                        fallback=worker if isinstance(worker, str) and worker.strip() else fallback_task_id,
                        include_task_patterns=True,
                    )
            else:
                task_id = _extract_session_task_id(
                    text=text,
                    fallback=label if isinstance(label, str) and label.strip() else fallback_task_id,
                )
            body = (
                _extract_internal_child_result_text(text)
                if internal_child_completion
                else _clean_session_text(text)
            )
            if not body:
                continue
            cleaned_entries.append(
                {
                    "source": "session",
                    "kind": kind,
                    "sender": sender,
                    "worker": worker,
                    "taskId": task_id,
                    "status": "SESSION",
                    "at": normalized_at,
                    "body": body,
                }
            )

        self._session_cache_generated_at = now
        self._session_cache_entries = cleaned_entries
        return cleaned_entries[-limit:]

    def _session_monitor_connection(self):
        if self.config.endpoint_a.tunnel is not None:
            return self.config.endpoint_a.tunnel.ssh
        return None

    def _all_endpoints(self) -> tuple:
        return (self.config.endpoint_a, *self.config.worker_endpoints())

    def _infer_session_worker(
        self,
        *,
        entry: dict[str, object],
        sender: str,
        kind: str,
        text: str,
    ) -> str | None:
        worker = entry.get("worker")
        if isinstance(worker, str) and worker.strip():
            return worker
        label = entry.get("label")
        candidates = [text, sender]
        if isinstance(label, str):
            candidates.append(label)
        for candidate in candidates:
            if not isinstance(candidate, str):
                continue
            for worker_name in self.config.worker_display_names():
                if worker_name in candidate:
                    return worker_name
        if kind == "b" and sender != self.config.endpoint_a.display_name:
            return sender
        return None

    def _worker_status_counts(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {}
        for row in self.store.list_messages():
            worker_display_name = self._message_worker_display_name(row)
            counts.setdefault(worker_display_name, {})
            counts[worker_display_name][row["status"]] = (
                counts[worker_display_name].get(row["status"], 0) + 1
            )
        return counts

    def _worker_attempt_counts(self) -> dict[str, dict[str, dict[str, int]]]:
        message_rows = {
            int(row["id"]): row
            for row in self.store.list_messages()
        }
        counts: dict[str, dict[str, dict[str, int]]] = {}
        for key, count in self.store.count_attempts_by_message_target_result().items():
            message_id, target, result = key
            row = message_rows.get(message_id)
            if row is None:
                worker_display_name = "unknown"
            else:
                worker_display_name = self._message_worker_display_name(row)
            worker_counts = counts.setdefault(worker_display_name, {})
            target_counts = worker_counts.setdefault(target, {})
            target_counts[result] = target_counts.get(result, 0) + int(count)
        return counts

    def _worker_retry_summary(
        self,
        worker_attempt_counts: dict[str, dict[str, dict[str, int]]],
    ) -> dict[str, dict[str, int]]:
        summary: dict[str, dict[str, int]] = {}
        for worker_name, target_counts in worker_attempt_counts.items():
            a_success = int(target_counts.get("A", {}).get("SUCCESS", 0))
            a_failed = int(target_counts.get("A", {}).get("FAILED", 0))
            b_success = int(target_counts.get("B", {}).get("SUCCESS", 0))
            b_failed = int(target_counts.get("B", {}).get("FAILED", 0))
            summary[worker_name] = {
                "aSuccess": a_success,
                "aFailed": a_failed,
                "bSuccess": b_success,
                "bFailed": b_failed,
                "total": a_success + a_failed + b_success + b_failed,
            }
        return summary

    def _worker_deadletter_counts(
        self,
        worker_status_counts: dict[str, dict[str, int]],
    ) -> dict[str, dict[str, int]]:
        summary: dict[str, dict[str, int]] = {}
        for worker_name, status_counts in worker_status_counts.items():
            summary[worker_name] = {
                "A": int(status_counts.get("DEADLETTER_A", 0)),
                "B": int(status_counts.get("DEADLETTER_B", 0)),
            }
        return summary

    def _worker_latency_stats(self) -> dict[str, dict[str, dict[str, float | int]]]:
        message_rows = {
            int(row["id"]): row
            for row in self.store.list_messages()
        }
        attempts_by_message: dict[int, list] = {}
        for attempt in self.store.list_all_attempts():
            attempts_by_message.setdefault(int(attempt["message_id"]), []).append(attempt)

        stages: dict[str, dict[str, list[float]]] = {}
        for message_id, row in message_rows.items():
            created_at = _parse_timestamp(row["created_at"])
            if created_at is None:
                continue
            attempts = attempts_by_message.get(message_id, [])
            first_b_success = None
            first_a_success = None
            for attempt in attempts:
                parsed_at = _parse_timestamp(attempt["created_at"])
                if parsed_at is None:
                    continue
                if attempt["target"] == "B" and attempt["result"] == "SUCCESS" and first_b_success is None:
                    first_b_success = parsed_at
                if attempt["target"] == "A" and attempt["result"] == "SUCCESS" and first_a_success is None:
                    first_a_success = parsed_at
            worker_name = self._message_worker_display_name(row)
            worker_stages = stages.setdefault(worker_name, {"dispatch": [], "inject": []})
            if first_b_success is not None:
                worker_stages["dispatch"].append(
                    max(0.0, (first_b_success - created_at).total_seconds())
                )
            if first_b_success is not None and first_a_success is not None:
                worker_stages["inject"].append(
                    max(0.0, (first_a_success - first_b_success).total_seconds())
                )

        result: dict[str, dict[str, dict[str, float | int]]] = {}
        for worker_name, stage_values in stages.items():
            result[worker_name] = {}
            for stage, values in stage_values.items():
                if values:
                    avg_seconds = sum(values) / len(values)
                    latest_seconds = values[-1]
                    max_seconds = max(values)
                    sample_count = len(values)
                else:
                    avg_seconds = 0.0
                    latest_seconds = 0.0
                    max_seconds = 0.0
                    sample_count = 0
                result[worker_name][stage] = {
                    "sampleCount": sample_count,
                    "avgSeconds": round(avg_seconds, 3),
                    "latestSeconds": round(latest_seconds, 3),
                    "maxSeconds": round(max_seconds, 3),
                }
        return result

    def _message_worker_name(self, row) -> str:
        worker_name = row["worker_name"]
        if isinstance(worker_name, str) and worker_name.strip():
            return worker_name
        request_meta = _load_request_metadata(row["processing_path"])
        to_gateway = request_meta.get("toGateway")
        if isinstance(to_gateway, str) and to_gateway.strip():
            return self.config.resolve_worker(to_gateway).name
        return self.config.default_worker.name

    def _message_worker_display_name(
        self,
        row,
        *,
        request_meta: dict[str, object] | None = None,
    ) -> str:
        worker_display_name = row["worker_display_name"]
        if isinstance(worker_display_name, str) and worker_display_name.strip():
            return worker_display_name
        payload = request_meta if request_meta is not None else _load_request_metadata(row["processing_path"])
        to_gateway = payload.get("toGateway") if isinstance(payload, dict) else None
        if isinstance(to_gateway, str) and to_gateway.strip():
            return self.config.resolve_worker(to_gateway).display_name
        return self.config.default_worker.display_name

    def _recent_audit_events(self, *, limit: int) -> list[dict[str, object]]:
        if not self.config.audit.jsonl_path.exists():
            return []
        lines = deque(maxlen=limit)
        with self.config.audit.jsonl_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.strip()
                if stripped:
                    lines.append(stripped)
        events: list[dict[str, object]] = []
        for line in reversed(lines):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(parsed, dict):
                continue
            events.append(parsed)
        return events

    def _recent_log_lines(self, *, limit: int) -> list[str]:
        log_path = self.config.relay.log_dir / "relay.log"
        if not log_path.exists():
            return []
        lines = deque(maxlen=limit)
        with log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                stripped = line.rstrip("\n")
                if stripped:
                    lines.append(stripped)
        return list(reversed(lines))

    def _ensure_endpoint_tunnels(self) -> None:
        for endpoint in self._all_endpoints():
            if endpoint.tunnel is None:
                continue
            try:
                opened = self.tunnel_manager.ensure_tunnel(endpoint.tunnel)
            except RemoteError as exc:
                raise ConfigError(
                    f"failed to establish tunnel for {endpoint.display_name}: {exc}"
                ) from exc
            if opened:
                self.logger.info(
                    "opened tunnel for %s on local port %s",
                    endpoint.display_name,
                    endpoint.tunnel.local_port,
                )

    def _refresh_external_health(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (
            now - self._last_external_health_check_at_monotonic
            < self.EXTERNAL_HEALTH_INTERVAL_SECONDS
        ):
            return

        for endpoint in self._all_endpoints():
            tunnel_healthy = self._ensure_endpoint_tunnel(endpoint)
            http_healthy = tunnel_healthy and self._probe_endpoint_http(endpoint)
            self._endpoint_http_health[endpoint.name] = http_healthy

        self._last_external_health_check_at_monotonic = now

    def _ensure_endpoint_tunnel(self, endpoint) -> bool:
        tunnel = endpoint.tunnel
        if tunnel is None:
            self._endpoint_tunnel_health[endpoint.name] = True
            return True
        try:
            self.tunnel_manager.ensure_tunnel(tunnel)
            healthy = self.tunnel_manager.is_tunnel_open(tunnel)
        except RemoteError as exc:
            self._endpoint_tunnel_health[endpoint.name] = False
            self._endpoint_last_error[endpoint.name] = str(exc)
            self.logger.warning("tunnel health check failed for %s: %s", endpoint.display_name, exc)
            return False

        self._endpoint_tunnel_health[endpoint.name] = healthy
        self._endpoint_last_error[endpoint.name] = None if healthy else "local tunnel port is closed"
        return healthy

    def _probe_endpoint_http(self, endpoint) -> bool:
        url = endpoint.base_url.rstrip("/") + "/v1/responses"
        request = urllib_request.Request(url, method="GET")
        timeout = min(max(endpoint.timeout_seconds, 0.5), 5.0)
        try:
            with urllib_request.urlopen(request, timeout=timeout):
                pass
        except urllib_error.HTTPError:
            self._endpoint_last_error[endpoint.name] = None
            return True
        except (urllib_error.URLError, OSError) as exc:
            self._endpoint_last_error[endpoint.name] = str(exc)
            self.logger.warning("HTTP health check failed for %s: %s", endpoint.display_name, exc)
            return False

        self._endpoint_last_error[endpoint.name] = None
        return True

    def _ensure_runtime_dirs(self) -> None:
        for path in (
            self.config.relay.state_dir,
            self.config.relay.processing_dir,
            self.config.relay.replies_dir,
            self.config.relay.log_dir,
            self.config.relay.archive_dir,
            self.config.relay.deadletter_dir,
            self.config.audit.jsonl_path.parent,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _process_pending_file(self, path: Path) -> bool:
        stat = path.stat()
        source_key = self._build_source_key(path)

        claimed = self.store.claim_source_file(
            source_key=source_key,
            filename=path.name,
            watch_path=str(path.resolve()),
            source_size=stat.st_size,
            source_mtime_ns=stat.st_mtime_ns,
        )
        seen_row = None
        is_resume = False
        if not claimed:
            seen_row = self.store.get_seen_file(source_key)
            if seen_row is None:
                self.logger.debug("skipping already-seen source file %s", path)
                return False
            if seen_row["status"] not in {"DISCOVERED", "FILE_COPY_FAILED"}:
                self.logger.debug(
                    "skipping source file %s with terminal seen status=%s",
                    path,
                    seen_row["status"],
                )
                return False
            is_resume = True
            self.logger.info(
                "resuming incomplete ingestion for %s with seen status=%s",
                path.name,
                seen_row["status"],
            )

        try:
            envelope = Envelope.from_file(
                path,
                expected_schema_version=self.config.behavior.schema_version,
                default_ttl_seconds=self.config.behavior.default_ttl_seconds,
                default_from_gateway=self.config.endpoint_a.display_name,
            )
        except EnvelopeError as exc:
            deadletter_path = self.spool.copy_to_deadletter(path, reason=exc.code)
            self.store.finalize_source_file(
                source_key=source_key,
                status=exc.code.upper(),
                error_text=str(exc),
                local_copy_path=str(deadletter_path),
            )
            self.audit.append(
                "source_deadlettered",
                payload={
                    "filename": path.name,
                    "reason": exc.code,
                    "deadletterPath": str(deadletter_path),
                },
            )
            self.logger.warning("%s -> deadletter (%s)", path.name, exc)
            return True

        reservation = self.store.reserve_message(
            envelope,
            filename=path.name,
            watch_path=str(path.resolve()),
        )
        if not reservation.reserved:
            if is_resume:
                existing_message = self.store.get_message(reservation.message_id)
                if existing_message is None:
                    self.logger.error(
                        "resume failed for %s: message_id=%s not found",
                        path.name,
                        reservation.message_id,
                    )
                    return False
                processing_path = existing_message["processing_path"]
                if processing_path:
                    self.store.finalize_source_file(
                        source_key=source_key,
                        status=existing_message["status"],
                        message_id=reservation.message_id,
                        local_copy_path=processing_path,
                    )
                    self.logger.info(
                        "%s resumed with existing processing artifact %s",
                        path.name,
                        processing_path,
                    )
                    return True
                processing_path = self._materialize_processing_copy(path, envelope=envelope)
                self.store.update_message_artifact(
                    message_id=reservation.message_id,
                    processing_path=str(processing_path),
                )
                self.store.finalize_source_file(
                    source_key=source_key,
                    status="RESERVED",
                    message_id=reservation.message_id,
                    local_copy_path=str(processing_path),
                )
                self.logger.info(
                    "%s resumed and reserved as task=%s idempotencyKey=%s",
                    path.name,
                    envelope.task_id,
                    envelope.idempotency_key,
                )
                self.audit.append(
                    "message_resumed",
                    message_id=reservation.message_id,
                    payload={
                        "filename": path.name,
                        "taskId": envelope.task_id,
                        "idempotencyKey": envelope.idempotency_key,
                    },
                )
                return True

            archive_path = self.spool.copy_to_archive(path, reason="duplicate_suppressed")
            self.store.finalize_source_file(
                source_key=source_key,
                status="DUPLICATE_SUPPRESSED",
                message_id=reservation.message_id,
                local_copy_path=str(archive_path),
            )
            self.logger.info(
                "%s suppressed as duplicate idempotencyKey=%s",
                path.name,
                envelope.idempotency_key,
            )
            self.audit.append(
                "duplicate_suppressed",
                message_id=reservation.message_id,
                payload={
                    "filename": path.name,
                    "idempotencyKey": envelope.idempotency_key,
                    "archivePath": str(archive_path),
                },
            )
            return True

        try:
            processing_path = self._materialize_processing_copy(path, envelope=envelope)
        except OSError as exc:
            self.store.update_message_status(
                message_id=reservation.message_id,
                status="FILE_COPY_FAILED",
                error_text=str(exc),
            )
            self.store.finalize_source_file(
                source_key=source_key,
                status="FILE_COPY_FAILED",
                message_id=reservation.message_id,
                error_text=str(exc),
            )
            self.logger.error("failed to copy %s into processing spool: %s", path.name, exc)
            return True

        self.store.update_message_artifact(
            message_id=reservation.message_id,
            processing_path=str(processing_path),
        )
        self.store.finalize_source_file(
            source_key=source_key,
            status="RESERVED",
            message_id=reservation.message_id,
            local_copy_path=str(processing_path),
        )
        self.logger.info(
            "%s reserved as task=%s idempotencyKey=%s",
            path.name,
            envelope.task_id,
            envelope.idempotency_key,
        )
        self.audit.append(
            "message_reserved",
            message_id=reservation.message_id,
            payload={
                "filename": path.name,
                "taskId": envelope.task_id,
                "idempotencyKey": envelope.idempotency_key,
                "processingPath": str(processing_path),
            },
        )
        return True

    def _materialize_processing_copy(self, path: Path, *, envelope: Envelope | None = None) -> Path:
        existing = self.config.relay.processing_dir / path.name
        if existing.exists():
            return existing
        processing_path = self.spool.copy_to_processing(path)
        if envelope is not None:
            processing_path.write_text(
                json.dumps(envelope.to_payload(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return processing_path

    def _build_source_key(self, path: Path) -> str:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        stat = path.stat()
        return f"{path.resolve()}:{stat.st_size}:{digest}"

    def _dispatch_reserved_messages(self) -> int:
        dispatched = 0
        for row in self.store.list_messages_by_status(("RESERVED", "FAILED_B")):
            processing_path = row["processing_path"]
            if not processing_path:
                continue
            if not self._message_due_for_retry(row):
                continue

            if self._retry_exhausted(message_id=row["id"], target="B"):
                self._deadletter_message(
                    row=row,
                    source_path=Path(processing_path),
                    status="DEADLETTER_B",
                    reason="failed_b",
                    target="B",
                    error_text=row["last_error"] or "dispatch retries exhausted",
                )
                continue

            try:
                envelope = Envelope.from_file(
                    Path(processing_path),
                    expected_schema_version=self.config.behavior.schema_version,
                    default_ttl_seconds=self.config.behavior.default_ttl_seconds,
                )
                worker_endpoint = self.config.resolve_worker(envelope.to_gateway)
                request_content = self._build_b_request_content(envelope)
                self.store.assign_message_worker(
                    message_id=row["id"],
                    worker_name=worker_endpoint.name,
                    worker_display_name=worker_endpoint.display_name,
                )
            except EnvelopeError as exc:
                attempt_no = self.store.record_attempt(
                    message_id=row["id"],
                    target="B",
                    result="FAILED",
                    error_text=str(exc),
                )
                if attempt_no >= self.config.retry.max_attempts_b:
                    self._deadletter_message(
                        row=row,
                        source_path=Path(processing_path),
                        status="DEADLETTER_B",
                        reason="failed_b",
                        target="B",
                        error_text=str(exc),
                    )
                    self.logger.error("failed dispatch for %s: %s", row["filename"], exc)
                    continue

                delay = self._retry_delay_seconds(
                    message_id=row["id"],
                    target="B",
                    attempt_no=attempt_no,
                )
                next_attempt_at = self._future_retry_timestamp(delay)
                self.store.update_message_status(
                    message_id=row["id"],
                    status="FAILED_B",
                    error_text=str(exc),
                    next_attempt_at=next_attempt_at,
                )
                self.audit.append(
                    "dispatch_retry_scheduled",
                    message_id=row["id"],
                    payload={
                        "attemptNo": attempt_no,
                        "delaySeconds": delay,
                        "nextAttemptAt": next_attempt_at,
                        "error": str(exc),
                    },
                )
                self.logger.warning(
                    "dispatch attempt %s for %s failed; next retry at %s: %s",
                    attempt_no,
                    row["filename"],
                    next_attempt_at,
                    exc,
                )
                continue

            if self._uses_rabbitmq_transport():
                try:
                    publish_summary = self._publish_broker_message(
                        row=row,
                        envelope=envelope,
                        worker_endpoint=worker_endpoint,
                    )
                    attempt_no = self.store.record_attempt(
                        message_id=row["id"],
                        target="B",
                        result="SUCCESS",
                    )
                    self.store.update_message_status(
                        message_id=row["id"],
                        status="QUEUED_B",
                        error_text=None,
                        next_attempt_at=None,
                    )
                    self.audit.append(
                        "dispatch_queued",
                        message_id=row["id"],
                        payload={
                            "attemptNo": attempt_no,
                            "worker": worker_endpoint.display_name,
                            "exchange": publish_summary["exchange"],
                            "routingKey": publish_summary["routingKey"],
                            "replyTo": publish_summary["replyTo"],
                            "messageId": publish_summary["messageId"],
                            "conversationId": publish_summary["conversationId"],
                        },
                    )
                    self.logger.info(
                        "%s queued to broker for %s message=%s",
                        row["filename"],
                        worker_endpoint.display_name,
                        envelope.message_id,
                    )
                    dispatched += 1
                except (RabbitMQError, ConfigError) as exc:
                    self._schedule_b_retry(
                        row=row,
                        error_text=str(exc),
                    )
                continue

            prior_attempts = self.store.count_attempts(message_id=row["id"], target="B")
            try:
                token = self._resolve_endpoint_token(worker_endpoint)
            except ConfigError as exc:
                self.logger.debug(
                    "dispatch skipped for %s: %s",
                    worker_endpoint.display_name,
                    exc,
                )
                continue

            if prior_attempts > 0:
                observed = self._confirm_b_dispatch_reply(
                    worker_endpoint=worker_endpoint,
                    request_content=request_content,
                )
                if observed is not None:
                    reply_text = observed.get("replyText")
                    if isinstance(reply_text, str) and reply_text.strip():
                        attempt_no = self.store.record_attempt(
                            message_id=row["id"],
                            target="B",
                            result="SUCCESS",
                            error_text="confirmed via worker session after prior timeout",
                        )
                        response = self._build_session_reply_artifact(
                            worker_endpoint_name=worker_endpoint.display_name,
                            envelope=envelope,
                            reply_text=reply_text,
                            observed=observed,
                        )
                        reply_path = self._write_reply_artifact(
                            idempotency_key=row["idempotency_key"],
                            response=response,
                        )
                        self.store.update_message_reply(
                            message_id=row["id"],
                            reply_path=str(reply_path),
                            reply_text=reply_text,
                        )
                        self.audit.append(
                            "dispatch_succeeded",
                            message_id=row["id"],
                            payload={
                                "attemptNo": attempt_no,
                                "worker": worker_endpoint.display_name,
                                "replyPath": str(reply_path),
                                "replyTextPreview": reply_text[:200],
                                "confirmedAt": observed.get("replyAt") or observed.get("requestAt"),
                                "sessionFile": observed.get("sessionFile"),
                                "via": "worker-session",
                            },
                        )
                        self.logger.info(
                            "%s already replied in %s session; marking B_REPLIED",
                            row["filename"],
                            worker_endpoint.display_name,
                        )
                        dispatched += 1
                        continue

                    delay = min(5.0, max(self.config.retry.initial_backoff_ms / 1000.0, 1.0))
                    next_attempt_at = self._future_retry_timestamp(delay)
                    self.store.update_message_status(
                        message_id=row["id"],
                        status="FAILED_B",
                        error_text="worker accepted request; awaiting reply",
                        next_attempt_at=next_attempt_at,
                    )
                    self.audit.append(
                        "dispatch_waiting_for_reply",
                        message_id=row["id"],
                        payload={
                            "worker": worker_endpoint.display_name,
                            "nextAttemptAt": next_attempt_at,
                            "sessionFile": observed.get("sessionFile"),
                            "requestAt": observed.get("requestAt"),
                        },
                    )
                    self.logger.info(
                        "%s already reached %s; waiting for reply before rechecking",
                        row["filename"],
                        worker_endpoint.display_name,
                    )
                    continue

            try:
                response = self.responses_client.send_user_message(
                    endpoint=worker_endpoint,
                    session_key=worker_endpoint.default_session_key,
                    content=request_content,
                    token=token,
                )
                attempt_no = self.store.record_attempt(
                    message_id=row["id"],
                    target="B",
                    result="SUCCESS",
                )
                reply_text = self.response_extractor.extract_text(response)
                reply_path = self._write_reply_artifact(
                    idempotency_key=row["idempotency_key"],
                    response=response,
                )
                self.store.update_message_reply(
                    message_id=row["id"],
                    reply_path=str(reply_path),
                    reply_text=reply_text,
                )
                self.audit.append(
                    "dispatch_succeeded",
                    message_id=row["id"],
                    payload={
                        "attemptNo": attempt_no,
                        "worker": worker_endpoint.display_name,
                        "replyPath": str(reply_path),
                        "replyTextPreview": reply_text[:200],
                    },
                )
                self.logger.info(
                    "%s dispatched to %s and replied",
                    row["filename"],
                    worker_endpoint.display_name,
                )
                dispatched += 1
            except (ResponsesClientError, ResponseExtractorError, OSError) as exc:
                if self._is_timeout_error(exc):
                    observed = self._confirm_b_dispatch_reply(
                        worker_endpoint=worker_endpoint,
                        request_content=request_content,
                    )
                    if observed is not None:
                        reply_text = observed.get("replyText")
                        if isinstance(reply_text, str) and reply_text.strip():
                            attempt_no = self.store.record_attempt(
                                message_id=row["id"],
                                target="B",
                                result="SUCCESS",
                                error_text="timed out but confirmed via worker session",
                            )
                            response = self._build_session_reply_artifact(
                                worker_endpoint_name=worker_endpoint.display_name,
                                envelope=envelope,
                                reply_text=reply_text,
                                observed=observed,
                            )
                            reply_path = self._write_reply_artifact(
                                idempotency_key=row["idempotency_key"],
                                response=response,
                            )
                            self.store.update_message_reply(
                                message_id=row["id"],
                                reply_path=str(reply_path),
                                reply_text=reply_text,
                            )
                            self.audit.append(
                                "dispatch_succeeded",
                                message_id=row["id"],
                                payload={
                                    "attemptNo": attempt_no,
                                    "worker": worker_endpoint.display_name,
                                    "replyPath": str(reply_path),
                                    "replyTextPreview": reply_text[:200],
                                    "confirmedAt": observed.get("replyAt") or observed.get("requestAt"),
                                    "sessionFile": observed.get("sessionFile"),
                                    "via": "worker-session",
                                    "transportError": str(exc),
                                },
                            )
                            self.logger.warning(
                                "%s dispatch timed out but reply was confirmed in %s session",
                                row["filename"],
                                worker_endpoint.display_name,
                            )
                            dispatched += 1
                            continue

                        delay = min(5.0, max(self.config.retry.initial_backoff_ms / 1000.0, 1.0))
                        next_attempt_at = self._future_retry_timestamp(delay)
                        attempt_no = self.store.record_attempt(
                            message_id=row["id"],
                            target="B",
                            result="FAILED",
                            error_text=str(exc),
                        )
                        self.store.update_message_status(
                            message_id=row["id"],
                            status="FAILED_B",
                            error_text="worker accepted request; awaiting reply",
                            next_attempt_at=next_attempt_at,
                        )
                        self.audit.append(
                            "dispatch_waiting_for_reply",
                            message_id=row["id"],
                            payload={
                                "attemptNo": attempt_no,
                                "worker": worker_endpoint.display_name,
                                "nextAttemptAt": next_attempt_at,
                                "sessionFile": observed.get("sessionFile"),
                                "requestAt": observed.get("requestAt"),
                                "transportError": str(exc),
                            },
                        )
                        self.logger.warning(
                            "%s dispatch timed out after delivery to %s; waiting for reply",
                            row["filename"],
                            worker_endpoint.display_name,
                        )
                        continue
                if self._should_refresh_token(exc):
                    self._invalidate_endpoint_token(worker_endpoint.name)
                    self.logger.warning(
                        "invalidated cached token for %s after auth failure",
                        worker_endpoint.display_name,
                    )
                self._schedule_b_retry(row=row, error_text=str(exc))
        return dispatched

    def _inject_replied_messages(self) -> int:
        try:
            token = self._resolve_endpoint_token(self.config.endpoint_a)
        except ConfigError as exc:
            self.logger.debug("inject skipped: %s", exc)
            return 0

        injected = 0
        for row in self.store.list_messages_by_status(("B_REPLIED", "FAILED_A_INJECTION")):
            processing_path = row["processing_path"]
            reply_text = row["reply_text"]
            if not processing_path or not reply_text:
                continue
            if not self._message_due_for_retry(row):
                continue

            try:
                envelope = Envelope.from_file(
                    Path(processing_path),
                    expected_schema_version=self.config.behavior.schema_version,
                    default_ttl_seconds=self.config.behavior.default_ttl_seconds,
                )
                worker_endpoint = self.config.resolve_worker(envelope.to_gateway)
                inject_content = self._build_a_inject_content(
                    envelope,
                    reply_text,
                    worker_endpoint.display_name,
                )
            except (EnvelopeError, ConfigError) as exc:
                source_path = Path(row["reply_path"] or processing_path)
                self._deadletter_message(
                    row=row,
                    source_path=source_path,
                    status="DEADLETTER_A",
                    reason="failed_a_injection",
                    target="A",
                    error_text=str(exc),
                )
                self.logger.error("failed inject for %s: %s", row["filename"], exc)
                continue

            prior_attempts = self.store.count_attempts(message_id=row["id"], target="A")
            if prior_attempts > 0:
                delivered = self._confirm_a_injection_delivery(
                    envelope=envelope,
                    inject_content=inject_content,
                )
                if delivered is not None:
                    attempt_no = self.store.record_attempt(
                        message_id=row["id"],
                        target="A",
                        result="SUCCESS",
                        error_text="confirmed via session log after prior timeout",
                    )
                    self.store.update_message_status(message_id=row["id"], status="DONE")
                    self.audit.append(
                        "inject_timeout_confirmed",
                        message_id=row["id"],
                        payload={
                            "attemptNo": attempt_no,
                            "sessionKey": envelope.return_session_key,
                            "confirmedAt": delivered.get("at"),
                            "sessionFile": delivered.get("sessionFile"),
                        },
                    )
                    self.logger.info(
                        "%s already present in %s session=%s; marking DONE",
                        row["filename"],
                        self.config.endpoint_a.display_name,
                        envelope.return_session_key,
                    )
                    injected += 1
                    continue

            if prior_attempts >= self.config.retry.max_attempts_a:
                source_path = Path(row["reply_path"] or processing_path)
                self._deadletter_message(
                    row=row,
                    source_path=source_path,
                    status="DEADLETTER_A",
                    reason="failed_a_injection",
                    target="A",
                    error_text=row["last_error"] or "inject retries exhausted",
                )
                continue

            try:
                self.responses_client.send_user_message(
                    endpoint=self.config.endpoint_a,
                    session_key=envelope.return_session_key,
                    content=inject_content,
                    token=token,
                )
                attempt_no = self.store.record_attempt(
                    message_id=row["id"],
                    target="A",
                    result="SUCCESS",
                )
                self.store.update_message_status(message_id=row["id"], status="DONE")
                self.audit.append(
                    "inject_succeeded",
                    message_id=row["id"],
                    payload={
                        "attemptNo": attempt_no,
                        "sessionKey": envelope.return_session_key,
                    },
                )
                self.logger.info(
                    "%s injected into %s session=%s",
                    row["filename"],
                    self.config.endpoint_a.display_name,
                    envelope.return_session_key,
                )
                injected += 1
            except (ResponsesClientError, OSError) as exc:
                if self._is_timeout_error(exc):
                    delivered = self._confirm_a_injection_delivery(
                        envelope=envelope,
                        inject_content=inject_content,
                    )
                    if delivered is not None:
                        attempt_no = self.store.record_attempt(
                            message_id=row["id"],
                            target="A",
                            result="SUCCESS",
                            error_text="timed out but confirmed via session log",
                        )
                        self.store.update_message_status(message_id=row["id"], status="DONE")
                        self.audit.append(
                            "inject_timeout_confirmed",
                            message_id=row["id"],
                            payload={
                                "attemptNo": attempt_no,
                                "sessionKey": envelope.return_session_key,
                                "confirmedAt": delivered.get("at"),
                                "sessionFile": delivered.get("sessionFile"),
                                "transportError": str(exc),
                            },
                        )
                        self.logger.warning(
                            "%s inject timed out but delivery was confirmed in session=%s",
                            row["filename"],
                            envelope.return_session_key,
                        )
                        injected += 1
                        continue
                if self._should_refresh_token(exc):
                    self._invalidate_endpoint_token(self.config.endpoint_a.name)
                    self.logger.warning(
                        "invalidated cached token for %s after auth failure",
                        self.config.endpoint_a.display_name,
                    )
                attempt_no = self.store.record_attempt(
                    message_id=row["id"],
                    target="A",
                    result="FAILED",
                    error_text=str(exc),
                )
                if attempt_no >= self.config.retry.max_attempts_a:
                    source_path = Path(row["reply_path"] or processing_path)
                    self._deadletter_message(
                        row=row,
                        source_path=source_path,
                        status="DEADLETTER_A",
                        reason="failed_a_injection",
                        target="A",
                        error_text=str(exc),
                    )
                    self.logger.error("failed inject for %s: %s", row["filename"], exc)
                    continue

                delay = self._retry_delay_seconds(
                    message_id=row["id"],
                    target="A",
                    attempt_no=attempt_no,
                )
                next_attempt_at = self._future_retry_timestamp(delay)
                self.store.update_message_status(
                    message_id=row["id"],
                    status="FAILED_A_INJECTION",
                    error_text=str(exc),
                    next_attempt_at=next_attempt_at,
                )
                self.audit.append(
                    "inject_retry_scheduled",
                    message_id=row["id"],
                    payload={
                        "attemptNo": attempt_no,
                        "delaySeconds": delay,
                        "nextAttemptAt": next_attempt_at,
                        "error": str(exc),
                    },
                )
                self.logger.warning(
                    "inject attempt %s for %s failed; next retry at %s: %s",
                    attempt_no,
                    row["filename"],
                    next_attempt_at,
                    exc,
                )
        return injected

    def _consume_rabbitmq_replies(self) -> int:
        if not self._uses_rabbitmq_transport() or self._rabbitmq_broker is None:
            return 0

        consumed = 0
        reply_binding = self._rabbitmq_broker.relay_reply_binding()
        while True:
            delivery = self._rabbitmq_broker.consume_one(reply_binding.queue)
            if delivery is None:
                return consumed
            try:
                reply_payload = json.loads(delivery.body.decode("utf-8"))
                reply = RabbitMQReply.from_payload(reply_payload)
                message_row = self.store.get_message_by_turn_id(reply.message_id)
                if message_row is None:
                    message_row = self.store.get_message_by_idempotency_key(reply.message_id)
                if message_row is None:
                    self.logger.warning(
                        "dropping RabbitMQ reply for unknown messageId=%s conversationId=%s",
                        reply.message_id,
                        reply.conversation_id,
                    )
                    delivery.ack()
                    consumed += 1
                    continue
                if message_row["status"] in {"B_REPLIED", "DONE", "DEADLETTER_A", "DEADLETTER_B"}:
                    delivery.ack()
                    consumed += 1
                    continue
                if reply.status == "ok" and isinstance(reply.reply_text, str) and reply.reply_text.strip():
                    reply_artifact = self._build_rabbitmq_reply_artifact(reply)
                    reply_path = self._write_reply_artifact(
                        idempotency_key=message_row["idempotency_key"],
                        response=reply_artifact,
                    )
                    self.store.update_message_reply(
                        message_id=message_row["id"],
                        reply_path=str(reply_path),
                        reply_text=reply.reply_text,
                    )
                    self.audit.append(
                        "reply_received",
                        message_id=message_row["id"],
                        payload={
                            "worker": reply.from_gateway,
                            "replyPath": str(reply_path),
                            "messageId": reply.message_id,
                            "conversationId": reply.conversation_id,
                            "replyTextPreview": reply.reply_text[:200],
                        },
                    )
                    self.logger.info(
                        "reply received from %s for message=%s",
                        reply.from_gateway,
                        reply.message_id,
                    )
                    delivery.ack()
                    consumed += 1
                    continue

                error_text = reply.error_text or "worker returned an error reply"
                attempt_no = self.store.record_attempt(
                    message_id=message_row["id"],
                    target="B",
                    result="FAILED",
                    error_text=error_text,
                )
                if self._retry_exhausted(message_id=message_row["id"], target="B"):
                    self._deadletter_message(
                        row=message_row,
                        source_path=Path(message_row["processing_path"]),
                        status="DEADLETTER_B",
                        reason="failed_b",
                        target="B",
                        error_text=error_text,
                    )
                    self.logger.error(
                        "worker reply marked deadletter for %s: %s",
                        message_row["filename"],
                        error_text,
                    )
                else:
                    delay = self._retry_delay_seconds(
                        message_id=message_row["id"],
                        target="B",
                        attempt_no=attempt_no,
                    )
                    next_attempt_at = self._future_retry_timestamp(delay)
                    self.store.update_message_status(
                        message_id=message_row["id"],
                        status="FAILED_B",
                        error_text=error_text,
                        next_attempt_at=next_attempt_at,
                    )
                    self.audit.append(
                        "reply_error_retry_scheduled",
                        message_id=message_row["id"],
                        payload={
                            "attemptNo": attempt_no,
                            "delaySeconds": delay,
                            "nextAttemptAt": next_attempt_at,
                            "error": error_text,
                            "worker": reply.from_gateway,
                        },
                    )
                    self.logger.warning(
                        "worker error reply for %s; next retry at %s: %s",
                        message_row["filename"],
                        next_attempt_at,
                        error_text,
                    )
                delivery.ack()
                consumed += 1
            except (ValueError, RabbitMQError, OSError) as exc:
                self.logger.error("failed to process RabbitMQ reply: %s", exc)
                delivery.nack(requeue=True)
                return consumed

    def _collect_queued_worker_replies(self) -> int:
        if not self._uses_rabbitmq_transport():
            return 0

        collected = 0
        for row in self.store.list_messages_by_status(("QUEUED_B",)):
            processing_path = row["processing_path"]
            if not processing_path:
                continue
            try:
                envelope = Envelope.from_file(
                    Path(processing_path),
                    expected_schema_version=self.config.behavior.schema_version,
                    default_ttl_seconds=self.config.behavior.default_ttl_seconds,
                )
                worker_endpoint = self.config.resolve_worker(
                    row["worker_display_name"] or row["worker_name"] or envelope.to_gateway
                )
            except (EnvelopeError, ConfigError, OSError) as exc:
                self.logger.debug(
                    "queued reply collection skipped for %s: %s",
                    row["filename"],
                    exc,
                )
                continue

            observed = self._confirm_b_dispatch_reply(
                worker_endpoint=worker_endpoint,
                request_content=envelope.body,
            )
            if observed is None:
                continue

            reply_text = observed.get("replyText")
            if not isinstance(reply_text, str) or not reply_text.strip():
                continue

            reply_artifact = self._build_session_reply_artifact(
                worker_endpoint_name=worker_endpoint.display_name,
                envelope=envelope,
                reply_text=reply_text,
                observed=observed,
            )
            reply_path = self._write_reply_artifact(
                idempotency_key=row["idempotency_key"],
                response=reply_artifact,
            )
            self.store.update_message_reply(
                message_id=row["id"],
                reply_path=str(reply_path),
                reply_text=reply_text,
            )
            self.audit.append(
                "reply_received",
                message_id=row["id"],
                payload={
                    "worker": worker_endpoint.display_name,
                    "replyPath": str(reply_path),
                    "messageId": envelope.message_id,
                    "conversationId": envelope.conversation_id,
                    "replyTextPreview": reply_text[:200],
                    "confirmedAt": observed.get("replyAt") or observed.get("requestAt"),
                    "sessionFile": observed.get("sessionFile"),
                    "via": "worker-session",
                },
            )
            self.logger.info(
                "reply collected from %s session for message=%s",
                worker_endpoint.display_name,
                envelope.message_id,
            )
            collected += 1
        return collected

    def _uses_rabbitmq_transport(self) -> bool:
        return self.config.transport_mode == "rabbitmq"

    def _publish_broker_message(
        self,
        *,
        row,
        envelope: Envelope,
        worker_endpoint,
    ) -> dict[str, object]:
        if self._rabbitmq_broker is None:
            raise ConfigError("RabbitMQ transport is enabled but broker is not configured")
        payload = envelope.to_payload()
        payload["toGateway"] = worker_endpoint.display_name
        payload["fromGateway"] = self.config.endpoint_a.display_name
        return self._rabbitmq_broker.publish_message(payload)

    def _schedule_b_retry(self, *, row, error_text: str) -> None:
        attempt_no = self.store.record_attempt(
            message_id=row["id"],
            target="B",
            result="FAILED",
            error_text=error_text,
        )
        if self._retry_exhausted(message_id=row["id"], target="B"):
            self._deadletter_message(
                row=row,
                source_path=Path(row["processing_path"]),
                status="DEADLETTER_B",
                reason="failed_b",
                target="B",
                error_text=error_text,
            )
            self.logger.error("failed dispatch for %s: %s", row["filename"], error_text)
            return

        delay = self._retry_delay_seconds(
            message_id=row["id"],
            target="B",
            attempt_no=attempt_no,
        )
        next_attempt_at = self._future_retry_timestamp(delay)
        self.store.update_message_status(
            message_id=row["id"],
            status="FAILED_B",
            error_text=error_text,
            next_attempt_at=next_attempt_at,
        )
        self.audit.append(
            "dispatch_retry_scheduled",
            message_id=row["id"],
            payload={
                "attemptNo": attempt_no,
                "delaySeconds": delay,
                "nextAttemptAt": next_attempt_at,
                "error": error_text,
            },
        )
        self.logger.warning(
            "dispatch attempt %s for %s failed; next retry at %s: %s",
            attempt_no,
            row["filename"],
            next_attempt_at,
            error_text,
        )

    def _retry_exhausted(self, *, message_id: int, target: str) -> bool:
        failed_attempts = self.store.count_attempts_by_result(
            message_id=message_id,
            target=target,
            result="FAILED",
        )
        limit = (
            self.config.retry.max_attempts_b
            if target == "B"
            else self.config.retry.max_attempts_a
        )
        return failed_attempts >= limit

    def _build_rabbitmq_reply_artifact(self, reply: RabbitMQReply) -> dict[str, object]:
        return {
            "id": f"rabbitmq:{reply.message_id}",
            "object": "response",
            "status": "completed" if reply.status == "ok" else "failed",
            "model": f"openclaw:{reply.from_gateway}",
            "source": "rabbitmq-reply-queue",
            "replyStatus": reply.status,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": reply.reply_text or reply.error_text or "",
                        }
                    ],
                    "status": "completed" if reply.status == "ok" else "failed",
                }
            ],
        }

    def _build_b_request_content(self, envelope: Envelope) -> str:
        return (
            f"[FROM {envelope.from_gateway}]"
            f"[MESSAGE {envelope.message_id}]"
            f"[CONVERSATION {envelope.conversation_id}]\n"
            f"{envelope.body}"
        )

    def _build_session_reply_artifact(
        self,
        *,
        worker_endpoint_name: str,
        envelope: Envelope,
        reply_text: str,
        observed: dict[str, object],
    ) -> dict[str, object]:
        return {
            "id": f"session-log:{envelope.message_id}",
            "object": "response",
            "status": "completed",
            "model": f"openclaw:{worker_endpoint_name}",
            "source": "worker-session-log",
            "sessionFile": observed.get("sessionFile"),
            "requestAt": observed.get("requestAt"),
            "replyAt": observed.get("replyAt"),
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": reply_text,
                        }
                    ],
                    "status": "completed",
                }
            ],
        }

    def _build_a_inject_content(
        self,
        envelope: Envelope,
        reply_text: str,
        worker_display_name: str,
    ) -> str:
        return (
            f"[Reply from {worker_display_name}]"
            f"[MESSAGE {envelope.message_id}]"
            f"[CONVERSATION {envelope.conversation_id}]\n"
            f"{reply_text}"
        )

    def _confirm_a_injection_delivery(
        self,
        *,
        envelope: Envelope,
        inject_content: str,
    ) -> dict[str, object] | None:
        connection = self._session_monitor_connection()
        if connection is None:
            return None
        try:
            return self.remote_session_fetcher.find_message(
                connection,
                session_key=envelope.return_session_key,
                marker_text=inject_content,
            )
        except RemoteError as exc:
            self.logger.debug(
                "remote inject confirmation skipped for session=%s: %s",
                envelope.return_session_key,
                exc,
            )
            return None

    def _confirm_b_dispatch_reply(
        self,
        *,
        worker_endpoint,
        request_content: str,
    ) -> dict[str, object] | None:
        if worker_endpoint.tunnel is None:
            return None
        try:
            return self.remote_session_fetcher.find_message_reply(
                worker_endpoint.tunnel.ssh,
                marker_text=request_content,
            )
        except RemoteError as exc:
            self.logger.debug(
                "remote worker reply confirmation skipped for %s: %s",
                worker_endpoint.display_name,
                exc,
            )
            return None

    def _is_timeout_error(self, exc: BaseException) -> bool:
        return "timed out" in str(exc).lower()

    def _write_reply_artifact(self, *, idempotency_key: str, response: dict[str, object]) -> Path:
        path = self.config.relay.replies_dir / f"{idempotency_key}.json"
        path.write_text(json.dumps(response, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def _resolve_endpoint_token(self, endpoint) -> str:
        token = os.environ.get(endpoint.token_env)
        if token:
            return token

        cached = self._token_cache.get(endpoint.name)
        if cached is not None:
            cached_token, cached_at = cached
            if time.monotonic() - cached_at <= self.TOKEN_CACHE_TTL_SECONDS:
                return cached_token

        if endpoint.tunnel is None:
            raise ConfigError(
                f"environment variable {endpoint.token_env!r} is required for endpoint {endpoint.name!r}"
            )

        try:
            token = self.remote_token_resolver.fetch_token(endpoint.tunnel)
        except RemoteError as exc:
            raise ConfigError(
                f"failed to fetch token for {endpoint.display_name}: {exc}"
            ) from exc

        self._token_cache[endpoint.name] = (token, time.monotonic())
        return token

    def _invalidate_endpoint_token(self, endpoint_name: str) -> None:
        self._token_cache.pop(endpoint_name, None)

    def _should_refresh_token(self, exc: BaseException) -> bool:
        return isinstance(exc, ResponsesClientError) and exc.http_status in {401, 403}

    def replay_deadletter(self, message_id: int) -> str:
        row = self.store.get_message(message_id)
        if row is None:
            raise ConfigError(f"message_id={message_id} was not found")

        if row["status"] == "DEADLETTER_B":
            self.store.clear_attempts(message_id=message_id, target="B")
            self.store.update_message_status(message_id=message_id, status="RESERVED", error_text=None)
            self.audit.append(
                "deadletter_replayed",
                message_id=message_id,
                payload={"target": "B", "nextStatus": "RESERVED"},
            )
            return "RESERVED"

        if row["status"] == "DEADLETTER_A":
            self.store.clear_attempts(message_id=message_id, target="A")
            self.store.update_message_status(message_id=message_id, status="B_REPLIED", error_text=None)
            self.audit.append(
                "deadletter_replayed",
                message_id=message_id,
                payload={"target": "A", "nextStatus": "B_REPLIED"},
            )
            return "B_REPLIED"

        raise ConfigError(
            f"message_id={message_id} has status={row['status']!r}; only DEADLETTER_B/A can be replayed"
        )

    def _retry_delay_seconds(self, *, message_id: int, target: str, attempt_no: int) -> float:
        delay_ms = min(
            self.config.retry.initial_backoff_ms * (2 ** max(0, attempt_no - 1)),
            self.config.retry.max_backoff_ms,
        )
        if self.config.retry.jitter:
            digest = hashlib.sha256(f"{message_id}:{target}:{attempt_no}".encode("utf-8")).digest()
            fraction = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF
            delay_ms *= 0.75 + (0.5 * fraction)
        return delay_ms / 1000.0

    def _message_due_for_retry(self, row) -> bool:
        next_attempt_at = row["next_attempt_at"]
        if not next_attempt_at:
            return True
        scheduled_for = _parse_timestamp(next_attempt_at)
        if scheduled_for is None:
            return True
        return scheduled_for <= datetime.now(timezone.utc)

    def _future_retry_timestamp(self, delay_seconds: float) -> str:
        return (datetime.now(timezone.utc) + timedelta(seconds=delay_seconds)).isoformat(
            timespec="seconds"
        ).replace("+00:00", "Z")

    def _deadletter_message(
        self,
        *,
        row,
        source_path: Path,
        status: str,
        reason: str,
        target: str,
        error_text: str,
    ) -> None:
        deadletter_path = self.spool.copy_to_deadletter(source_path, reason=reason)
        self.store.update_message_status(
            message_id=row["id"],
            status=status,
            error_text=error_text,
        )
        self.audit.append(
            "message_deadlettered",
            message_id=row["id"],
            payload={
                "target": target,
                "status": status,
                "deadletterPath": str(deadletter_path),
                "error": error_text,
            },
        )


def os_access_readable(path: Path) -> bool:
    try:
        return path.exists() and path.is_dir()
    except OSError:
        return False


def os_access_writable(path: Path) -> bool:
    try:
        if not path.exists():
            return False
        probe = path / ".write-check"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _count_json_files(directory: Path) -> int:
    if not directory.exists():
        return 0
    return sum(1 for path in directory.iterdir() if path.is_file() and path.suffix == ".json")


def _load_request_metadata(processing_path: str | None) -> dict[str, object]:
    if not processing_path:
        return {}
    try:
        raw = json.loads(Path(processing_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[str, object] = {}
    for key in (
        "from",
        "fromGateway",
        "to",
        "toGateway",
        "intent",
        "body",
        "conversationId",
        "messageId",
    ):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            result[key] = value
    if "conversationId" not in result:
        fallback = raw.get("taskId")
        if isinstance(fallback, str) and fallback.strip():
            result["conversationId"] = fallback
    if "from" not in result and isinstance(result.get("fromGateway"), str):
        result["from"] = result["fromGateway"]
    if "fromGateway" not in result and isinstance(result.get("from"), str):
        result["fromGateway"] = result["from"]
    if "to" not in result and isinstance(result.get("toGateway"), str):
        result["to"] = result["toGateway"]
    if "toGateway" not in result and isinstance(result.get("to"), str):
        result["toGateway"] = result["to"]
    if "messageId" not in result:
        fallback = raw.get("turnId")
        if isinstance(fallback, str) and fallback.strip():
            result["messageId"] = fallback
    requested_return_session_key = raw.get("returnSessionKey")
    if isinstance(requested_return_session_key, str) and requested_return_session_key.strip():
        result["requestedReturnSessionKey"] = requested_return_session_key.strip()
    to_gateway = result.get("toGateway")
    conversation_id = result.get("conversationId") or result.get("messageId")
    if isinstance(to_gateway, str) and isinstance(conversation_id, str):
        result["returnSessionKey"] = resolve_return_session_key(
            requested_return_session_key if isinstance(requested_return_session_key, str) else None,
            to_gateway=to_gateway,
            conversation_id=conversation_id,
        )
    return result


def _required_mailbox_field(
    payload: dict[str, object],
    key: str,
    *,
    fallback_key: str | None = None,
) -> str:
    candidates = [key]
    if fallback_key is not None:
        candidates.append(fallback_key)
    for candidate in candidates:
        value = payload.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
    raise ValueError(f"missing or invalid string field '{key}'")


def _optional_mailbox_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_mailbox_bool(payload: dict[str, object], key: str) -> bool | None:
    value = payload.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    return None


def _header_value(headers: dict[str, str], header_name: str) -> str | None:
    expected = header_name.casefold()
    for key, value in headers.items():
        if key.casefold() == expected and value.strip():
            return value.strip()
    return None


def _extract_mailbox_auth_token(*, header_value: str, expected_scheme: str) -> str:
    parts = header_value.strip().split(None, 1)
    if len(parts) != 2:
        raise PermissionError("invalid authorization header")
    scheme, token = parts
    if scheme.casefold() != expected_scheme.casefold():
        raise PermissionError("invalid authorization scheme")
    token = token.strip()
    if not token:
        raise PermissionError("missing authorization token")
    return token


def _normalized_mailbox_payload(payload: dict[str, object]) -> dict[str, object]:
    sender = _required_mailbox_field(payload, "from", fallback_key="fromGateway")
    recipient = _required_mailbox_field(payload, "to", fallback_key="toGateway")
    body = _required_mailbox_field(payload, "body")
    raw_message_id = payload.get("messageId")
    message_id = (
        str(raw_message_id).strip()
        if isinstance(raw_message_id, str) and raw_message_id.strip()
        else _generate_mailbox_message_id(
            sender=sender,
            recipient=recipient,
            body=body,
        )
    )
    raw_conversation_id = payload.get("conversationId")
    conversation_id = (
        str(raw_conversation_id).strip()
        if isinstance(raw_conversation_id, str) and raw_conversation_id.strip()
        else message_id
    )
    raw_queued_at = payload.get("queuedAt")
    queued_at = (
        str(raw_queued_at).strip()
        if isinstance(raw_queued_at, str) and raw_queued_at.strip()
        else datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    result: dict[str, object] = {
        "messageId": message_id,
        "conversationId": conversation_id,
        "from": sender,
        "to": recipient,
        "body": body,
        "queuedAt": queued_at,
        "notifyHuman": _optional_mailbox_bool(payload, "notifyHuman") is True,
    }
    in_reply_to = _optional_mailbox_string(payload, "inReplyTo")
    if in_reply_to is not None:
        result["inReplyTo"] = in_reply_to
    return result


def _generate_mailbox_message_id(*, sender: str, recipient: str, body: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    entropy = f"{time.time_ns()}:{sender}:{recipient}:{body}"
    digest = hashlib.sha1(entropy.encode("utf-8")).hexdigest()[:10]
    return f"MSG-mailbox-{timestamp}-{digest}"


def _mailbox_delivery_payload(raw_payload: object, *, dequeued_at: str) -> dict[str, object]:
    if not isinstance(raw_payload, dict):
        raise ValueError("mailbox message must be a JSON object")
    payload = _normalized_mailbox_payload(raw_payload)
    payload["status"] = "delivered"
    payload["dequeuedAt"] = dequeued_at
    return payload


def _mailbox_row_payload(row) -> dict[str, object]:
    payload: dict[str, object] = {
        "messageId": row["message_id"],
        "conversationId": row["conversation_id"],
        "from": row["from_mailbox"],
        "to": row["to_mailbox"],
        "body": row["body"],
        "queuedAt": row["queued_at"],
        "status": row["status"],
        "notifyHuman": bool(row["notify_human"]),
    }
    if row["in_reply_to"]:
        payload["inReplyTo"] = row["in_reply_to"]
    if row["dequeued_at"]:
        payload["dequeuedAt"] = row["dequeued_at"]
    return payload


def _ensure_rabbitmq_topology_if_available(
    broker: object | None,
    logger: logging.Logger,
) -> None:
    if broker is None:
        return
    ensure_topology = getattr(broker, "ensure_topology", None)
    if not callable(ensure_topology):
        return
    ensure_topology()
    logger.info("rabbitmq topology ensured")


def _validate_mailbox_auth_environment(config: AppConfig) -> None:
    mailbox_auth = config.mailbox_auth
    if mailbox_auth is None:
        return
    missing = [
        f"{mailbox}:{env_name}"
        for mailbox, env_name in mailbox_auth.token_env_by_mailbox
        if not os.environ.get(env_name)
    ]
    if missing:
        raise ConfigError(
            "mailbox auth token environment variables are required: " + ", ".join(missing)
        )


def _json_safe_attempt_counts(
    attempt_counts: dict[tuple[str, str], int] | object,
) -> dict[str, dict[str, int]]:
    if not isinstance(attempt_counts, dict):
        return {}
    result: dict[str, dict[str, int]] = {}
    for key, count in attempt_counts.items():
        if not isinstance(key, tuple) or len(key) != 2:
            continue
        target, outcome = key
        if not isinstance(target, str) or not isinstance(outcome, str):
            continue
        result.setdefault(target, {})[outcome] = int(count)
    return result


def _json_safe_worker_attempt_counts(
    worker_attempt_counts: object,
) -> dict[str, dict[str, dict[str, int]]]:
    if not isinstance(worker_attempt_counts, dict):
        return {}
    result: dict[str, dict[str, dict[str, int]]] = {}
    for worker_name, target_counts in worker_attempt_counts.items():
        if not isinstance(worker_name, str) or not isinstance(target_counts, dict):
            continue
        result[worker_name] = {}
        for target, result_counts in target_counts.items():
            if not isinstance(target, str) or not isinstance(result_counts, dict):
                continue
            result[worker_name][target] = {}
            for outcome, count in result_counts.items():
                if not isinstance(outcome, str):
                    continue
                result[worker_name][target][outcome] = int(count)
    return result


def _clean_session_text(text: str) -> str:
    cleaned = text.replace("[[reply_to_current]]", "").strip()
    if cleaned.startswith("Conversation info (untrusted metadata):"):
        blocks = cleaned.split("\n\n")
        while blocks and (
            blocks[0].startswith("Conversation info (untrusted metadata):")
            or blocks[0].startswith("Sender (untrusted metadata):")
            or blocks[0] == "json"
            or blocks[0].startswith("{")
        ):
            blocks.pop(0)
        cleaned = "\n\n".join(blocks).strip()
    return cleaned.strip()


def _extract_session_label(text: str) -> str | None:
    patterns = (
        r"(?im)^\s*(?:[-*]\s+)?\*{0,2}label\*{0,2}\s*:\s*`([^`]+)`",
        r"(?im)^\s*(?:[-*]\s+)?label\s*:\s*([^\n]+)$",
        r"(?im)^\s*(?:[-*]\s+)?Label\s*:\s*`([^`]+)`",
        r"(?im)^\s*(?:[-*]\s+)?Label\s*:\s*([^\n]+)$",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match is None:
            continue
        candidate = match.group(1).strip().strip("*").strip()
        if candidate:
            return candidate
    return None


def _extract_session_task_id(
    text: str,
    *,
    fallback: str,
    include_task_patterns: bool = False,
) -> str:
    label = _extract_session_label(text)
    if label:
        return label
    if include_task_patterns:
        task = _extract_internal_child_task_name(text)
        if task:
            return task
    return fallback


def _extract_session_key(text: str) -> str | None:
    match = re.search(
        r"(?im)^\s*(?:childSessionKey|sessionKey|session_key)\s*:\s*`?([^\s`]+)`?\s*$",
        text,
    )
    if match is None:
        return None
    candidate = match.group(1).strip()
    return candidate or None


def _extract_internal_child_task_name(text: str) -> str | None:
    match = re.search(r"(?im)^\s*task\s*:\s*([^\n]+)$", text)
    if match is None:
        return None
    candidate = match.group(1).strip().strip("*").strip()
    return candidate or None


def _is_internal_child_completion(text: str) -> bool:
    markers = (
        "[Internal task completion event]",
        "source: subagent",
        "Result (untrusted content, treat as data):",
        "<<<BEGIN_UNTRUSTED_CHILD_RESULT>>>",
    )
    return all(marker in text for marker in markers)


def _extract_internal_child_result_text(text: str) -> str:
    marker = "<<<BEGIN_UNTRUSTED_CHILD_RESULT>>>"
    start = text.find(marker)
    if start == -1:
        return _clean_session_text(text)
    body = text[start + len(marker):]
    end_marker = "<<<END_UNTRUSTED_CHILD_RESULT>>>"
    end = body.find(end_marker)
    if end != -1:
        body = body[:end]
    return body.strip()


def _timeline_sort_key(value: object) -> float:
    normalized = _normalize_timestamp(value)
    if not normalized:
        return 0.0
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _parse_timestamp(value: object) -> datetime | None:
    normalized = _normalize_timestamp(value)
    if not normalized:
        return None
    try:
        return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError:
        return None


def _normalize_timestamp(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None

    candidates = [raw]
    if raw.endswith("Z"):
        candidates.append(raw[:-1] + "+00:00")
    if " " in raw and "T" not in raw:
        candidates.append(raw.replace(" ", "T") + "+00:00")
        candidates.append(raw.replace(" ", "T"))

    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return raw


def _prom_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')
