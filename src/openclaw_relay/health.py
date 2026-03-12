from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from typing import Callable
from urllib.parse import parse_qs, urlsplit


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
        mailbox_send: Callable[[dict[str, object]], dict[str, object]] | None = None,
        mailbox_receive: Callable[[str], dict[str, object] | None] | None = None,
        mailbox_authenticate: Callable[[str, dict[str, str]], str | None] | None = None,
    ) -> None:
        handler = self._build_handler(
            alive_probe=alive_probe,
            ready_probe=ready_probe,
            metrics_probe=metrics_probe,
            dashboard_probe=dashboard_probe,
            dashboard_html=dashboard_html,
            ops_html=ops_html,
            alert_webhook=alert_webhook,
            mailbox_send=mailbox_send,
            mailbox_receive=mailbox_receive,
            mailbox_authenticate=mailbox_authenticate,
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
        mailbox_send: Callable[[dict[str, object]], dict[str, object]] | None,
        mailbox_receive: Callable[[str], dict[str, object] | None] | None,
        mailbox_authenticate: Callable[[str, dict[str, str]], str | None] | None,
    ) -> type[BaseHTTPRequestHandler]:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlsplit(self.path)
                route = parsed.path
                query = parse_qs(parsed.query, keep_blank_values=False)

                if route in {"/", "/ui"}:
                    if dashboard_html is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    self._write_html(dashboard_html)
                    return
                if route in {"/ops", "/monitor"}:
                    if ops_html is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    self._write_html(ops_html)
                    return
                if route == "/api/dashboard":
                    if dashboard_probe is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    try:
                        payload = dashboard_probe()
                    except Exception as exc:  # pragma: no cover - defensive
                        self._write_json(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"error": str(exc)},
                        )
                        return
                    self._write_json(HTTPStatus.OK, payload, cache_control="no-store")
                    return
                if route == "/v1/messages":
                    if mailbox_receive is None:
                        self.send_error(HTTPStatus.NOT_FOUND)
                        return
                    try:
                        authenticated_mailbox = self._authenticate_mailbox("GET")
                    except PermissionError as exc:
                        self._write_json(
                            HTTPStatus.UNAUTHORIZED,
                            {"error": str(exc)},
                            headers={"WWW-Authenticate": "Bearer"},
                        )
                        return
                    requested_mailbox = self._mailbox_name(query)
                    if authenticated_mailbox is not None:
                        if (
                            requested_mailbox is not None
                            and requested_mailbox != authenticated_mailbox
                        ):
                            self._write_json(
                                HTTPStatus.FORBIDDEN,
                                {
                                    "error": "requested mailbox does not match authenticated mailbox"
                                },
                            )
                            return
                        mailbox = authenticated_mailbox
                    else:
                        mailbox = requested_mailbox
                        if mailbox is None:
                            self.send_error(
                                HTTPStatus.BAD_REQUEST,
                                "query parameter 'for' or X-Relay-Mailbox header is required",
                            )
                            return
                    try:
                        payload = mailbox_receive(mailbox)
                    except ValueError as exc:
                        self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                        return
                    except Exception as exc:  # pragma: no cover - defensive
                        self._write_json(
                            HTTPStatus.INTERNAL_SERVER_ERROR,
                            {"error": str(exc)},
                        )
                        return
                    if payload is None:
                        self.send_response(HTTPStatus.NO_CONTENT)
                        self.end_headers()
                        return
                    self._write_json(HTTPStatus.OK, payload, cache_control="no-store")
                    return
                if route == "/healthz":
                    self._write_status(HTTPStatus.OK if alive_probe() else HTTPStatus.SERVICE_UNAVAILABLE)
                    return
                if route == "/readyz":
                    self._write_status(
                        HTTPStatus.OK if ready_probe() else HTTPStatus.SERVICE_UNAVAILABLE
                    )
                    return
                if route == "/metrics":
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
                route = urlsplit(self.path).path
                if route != "/api/alertmanager/webhook":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if alert_webhook is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                try:
                    payload = self._read_json_object()
                except ValueError as exc:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                try:
                    response_payload = alert_webhook(payload) or {"status": "accepted"}
                except ValueError as exc:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                except Exception as exc:  # pragma: no cover - defensive
                    self._write_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": str(exc)},
                    )
                    return
                self._write_json(HTTPStatus.ACCEPTED, response_payload)

            def do_PUT(self) -> None:  # noqa: N802
                route = urlsplit(self.path).path
                if route != "/v1/messages":
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                if mailbox_send is None:
                    self.send_error(HTTPStatus.NOT_FOUND)
                    return
                try:
                    payload = self._read_json_object()
                except ValueError as exc:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                try:
                    authenticated_mailbox = self._authenticate_mailbox("PUT")
                except PermissionError as exc:
                    self._write_json(
                        HTTPStatus.UNAUTHORIZED,
                        {"error": str(exc)},
                        headers={"WWW-Authenticate": "Bearer"},
                    )
                    return
                if authenticated_mailbox is not None:
                    payload["from"] = authenticated_mailbox
                    payload["fromGateway"] = authenticated_mailbox
                try:
                    response_payload = mailbox_send(payload)
                except ValueError as exc:
                    self.send_error(HTTPStatus.BAD_REQUEST, str(exc))
                    return
                except Exception as exc:  # pragma: no cover - defensive
                    self._write_json(
                        HTTPStatus.INTERNAL_SERVER_ERROR,
                        {"error": str(exc)},
                    )
                    return
                self._write_json(HTTPStatus.ACCEPTED, response_payload)

            def log_message(self, fmt: str, *args: object) -> None:
                return

            def _read_json_object(self) -> dict[str, object]:
                content_length = self.headers.get("Content-Length", "0")
                try:
                    body_length = int(content_length)
                except ValueError:
                    body_length = 0
                body = self.rfile.read(max(0, body_length))
                try:
                    payload = json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("invalid JSON payload") from exc
                if not isinstance(payload, dict):
                    raise ValueError("JSON payload must be an object")
                return payload

            def _mailbox_name(self, query: dict[str, list[str]]) -> str | None:
                header_value = self.headers.get("X-Relay-Mailbox")
                if isinstance(header_value, str) and header_value.strip():
                    return header_value.strip()
                values = query.get("for") or []
                if not values:
                    return None
                mailbox = values[0].strip()
                return mailbox or None

            def _authenticate_mailbox(self, method: str) -> str | None:
                if mailbox_authenticate is None:
                    return None
                return mailbox_authenticate(method, dict(self.headers.items()))

            def _write_status(self, status: HTTPStatus) -> None:
                body = json.dumps({"status": status.phrase.lower().replace(" ", "_")}).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _write_json(
                self,
                status: HTTPStatus,
                payload: dict[str, object],
                *,
                cache_control: str | None = None,
                headers: dict[str, str] | None = None,
            ) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                if cache_control is not None:
                    self.send_header("Cache-Control", cache_control)
                if headers is not None:
                    for key, value in headers.items():
                        self.send_header(key, value)
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
