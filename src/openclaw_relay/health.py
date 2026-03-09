from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from typing import Callable


class HealthServer:
    def __init__(
        self,
        host: str,
        port: int,
        *,
        alive_probe: Callable[[], bool],
        ready_probe: Callable[[], bool],
        metrics_probe: Callable[[], str] | None = None,
        dashboard_probe: Callable[[], dict[str, object]] | None = None,
        dashboard_html: str | None = None,
        ops_html: str | None = None,
        alert_webhook: Callable[[dict[str, object]], dict[str, object] | None] | None = None,
    ) -> None:
        handler = self._build_handler(
            alive_probe=alive_probe,
            ready_probe=ready_probe,
            metrics_probe=metrics_probe,
            dashboard_probe=dashboard_probe,
            dashboard_html=dashboard_html,
            ops_html=ops_html,
            alert_webhook=alert_webhook,
        )
        self._server = ThreadingHTTPServer((host, port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="relay-health-server",
            daemon=True,
        )

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @staticmethod
    def _build_handler(
        *,
        alive_probe: Callable[[], bool],
        ready_probe: Callable[[], bool],
        metrics_probe: Callable[[], str] | None,
        dashboard_probe: Callable[[], dict[str, object]] | None,
        dashboard_html: str | None,
        ops_html: str | None,
        alert_webhook: Callable[[dict[str, object]], dict[str, object] | None] | None,
    ) -> type[BaseHTTPRequestHandler]:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path in {"/", "/ui"}:
                    if dashboard_html is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    self._write_html(dashboard_html)
                    return
                if self.path in {"/ops", "/monitor"}:
                    if ops_html is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    self._write_html(ops_html)
                    return
                if self.path == "/api/dashboard":
                    if dashboard_probe is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    try:
                        payload = dashboard_probe()
                    except Exception as exc:  # pragma: no cover - defensive
                        self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                        self.send_header("Content-Type", "application/json")
                        error_body = json.dumps({"error": str(exc)}).encode("utf-8")
                        self.send_header("Content-Length", str(len(error_body)))
                        self.end_headers()
                        self.wfile.write(error_body)
                        return
                    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path == "/healthz":
                    self._write_status(HTTPStatus.OK if alive_probe() else HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                if self.path == "/readyz":
                    self._write_status(
                        HTTPStatus.OK if ready_probe() else HTTPStatus.SERVICE_UNAVAILABLE
                    )
                    return
                if self.path == "/metrics":
                    if metrics_probe is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    try:
                        body = metrics_probe().encode("utf-8")
                    except Exception as exc:  # pragma: no cover - defensive
                        self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                        self.send_header("Content-Type", "text/plain; charset=utf-8")
                        error_body = f"metrics probe failed: {exc}\n".encode("utf-8")
                        self.send_header("Content-Length", str(len(error_body)))
                        self.end_headers()
                        self.wfile.write(error_body)
                        return
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self.send_error(HTTPStatus.NOT_FOUND)

            def do_POST(self) -> None:  # noqa: N802
                if self.path != "/api/alertmanager/webhook":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if alert_webhook is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                content_length = self.headers.get("Content-Length", "0")
                try:
                    body_length = int(content_length)
                except ValueError:
                    body_length = 0
                body = self.rfile.read(max(0, body_length))
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.send_error(HTTPStatus.BAD_REQUEST, "invalid JSON payload")
                    return
                if not isinstance(payload, dict):
                    self.send_error(HTTPStatus.BAD_REQUEST, "JSON payload must be an object")
                    return
                try:
                    response_payload = alert_webhook(payload) or {"status": "accepted"}
                except ValueError as exc:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                except Exception as exc:  # pragma: no cover - defensive
                    error_body = json.dumps({"error": str(exc)}).encode("utf-8")
                    self.send_response(HTTPStatus.INTERNAL_SERVER_ERROR)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(error_body)))
                    self.end_headers()
                    self.wfile.write(error_body)
                    return
                response_body = json.dumps(response_payload, ensure_ascii=False).encode("utf-8")
                self.send_response(HTTPStatus.ACCEPTED)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)

            def log_message(self, fmt: str, *args: object) -> None:
                return

            def _write_status(self, status: HTTPStatus) -> None:
                body = json.dumps({"status": status.phrase.lower().replace(" ", "_")}).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _write_html(self, html: str) -> None:
                body = html.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler
