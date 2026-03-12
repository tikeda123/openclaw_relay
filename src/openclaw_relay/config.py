from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import secrets
import tomllib
from typing import Any


class ConfigError(ValueError):
    """Raised when the relay configuration is invalid."""


@dataclass(frozen=True)
class SSHConnectionConfig:
    host: str
    user: str
    key_path: Path
    connect_timeout_seconds: int
    strict_host_key_checking: bool


@dataclass(frozen=True)
class TunnelConfig:
    ssh: SSHConnectionConfig
    local_port: int
    remote_host: str
    remote_port: int
    token_config_path: Path


@dataclass(frozen=True)
class RelayConfig:
    node_id: str
    state_dir: Path
    log_dir: Path
    watch_dir: Path
    archive_dir: Path
    deadletter_dir: Path
    poll_interval_ms: int
    health_host: str
    health_port: int

    @property
    def database_path(self) -> Path:
        return self.state_dir / "relay.db"

    @property
    def processing_dir(self) -> Path:
        return self.state_dir / "processing"

    @property
    def replies_dir(self) -> Path:
        return self.state_dir / "replies"


@dataclass(frozen=True)
class EndpointConfig:
    name: str
    display_name: str
    base_url: str
    agent_id: str
    default_session_key: str
    token_env: str
    timeout_seconds: float
    tunnel: TunnelConfig | None = None

    def resolve_token(self) -> str:
        token = os.environ.get(self.token_env)
        if not token:
            raise ConfigError(
                f"environment variable {self.token_env!r} is required for endpoint {self.name!r}"
            )
        return token


@dataclass(frozen=True)
class RetryConfig:
    max_attempts_b: int
    max_attempts_a: int
    initial_backoff_ms: int
    max_backoff_ms: int
    jitter: bool


@dataclass(frozen=True)
class SecurityConfig:
    require_private_ingress: bool
    allow_http_only_for_localhost: bool
    mask_secrets_in_logs: bool


@dataclass(frozen=True)
class MailboxAuthConfig:
    header_name: str
    scheme: str
    token_env_by_mailbox: tuple[tuple[str, str], ...]

    def resolve_authenticated_mailbox(self, token: str) -> str | None:
        for mailbox, env_name in self.token_env_by_mailbox:
            expected = os.environ.get(env_name)
            if expected and secrets.compare_digest(expected, token):
                return mailbox
        return None


@dataclass(frozen=True)
class AuditConfig:
    mode: str
    jsonl_path: Path


@dataclass(frozen=True)
class BehaviorConfig:
    schema_version: str
    default_ttl_seconds: int
    duplicate_policy: str
    inject_notice_on_error: bool


@dataclass(frozen=True)
class RabbitMQTLSConfig:
    enabled: bool
    ca_path: Path | None
    cert_path: Path | None
    key_path: Path | None
    server_hostname: str | None


@dataclass(frozen=True)
class RabbitMQConfig:
    host: str
    port: int
    virtual_host: str
    user_env: str
    password_env: str
    heartbeat_seconds: int
    blocked_connection_timeout_seconds: int
    prefetch_count: int
    queue_type: str
    dispatch_exchange: str
    reply_exchange: str
    mailbox_exchange: str
    deadletter_exchange: str
    events_exchange: str
    mailbox_queue_prefix: str
    worker_queue_prefix: str
    control_queue_prefix: str
    deadletter_queue_prefix: str
    tls: RabbitMQTLSConfig | None = None

    def resolve_username(self) -> str:
        username = os.environ.get(self.user_env)
        if not username:
            raise ConfigError(
                f"environment variable {self.user_env!r} is required for RabbitMQ username"
            )
        return username

    def resolve_password(self) -> str:
        password = os.environ.get(self.password_env)
        if not password:
            raise ConfigError(
                f"environment variable {self.password_env!r} is required for RabbitMQ password"
            )
        return password


@dataclass(frozen=True)
class AppConfig:
    relay: RelayConfig
    endpoint_a: EndpointConfig
    endpoint_b: EndpointConfig
    retry: RetryConfig
    security: SecurityConfig
    audit: AuditConfig
    behavior: BehaviorConfig
    mailbox_auth: MailboxAuthConfig | None = None
    rabbitmq: RabbitMQConfig | None = None
    workers: tuple[EndpointConfig, ...] = ()
    default_worker_name: str | None = None
    transport_mode: str = "responses"

    @property
    def default_worker(self) -> EndpointConfig:
        if not self.workers:
            return self.endpoint_b
        if self.default_worker_name:
            for worker in self.workers:
                if worker.name == self.default_worker_name:
                    return worker
        return self.workers[0]

    def worker_endpoints(self) -> tuple[EndpointConfig, ...]:
        if self.workers:
            return self.workers
        return (self.endpoint_b,)

    def worker_display_names(self) -> tuple[str, ...]:
        return tuple(worker.display_name for worker in self.worker_endpoints())

    def resolve_worker(self, gateway_name: str | None) -> EndpointConfig:
        if gateway_name:
            normalized = gateway_name.strip()
            for worker in self.worker_endpoints():
                if normalized in {worker.name, worker.display_name}:
                    return worker
        return self.default_worker

    @classmethod
    def from_file(cls, path: Path) -> "AppConfig":
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        base_dir = path.parent

        relay_table = _require_table(data, "relay")
        endpoints_table = _require_table(data, "endpoints")
        routing_table = _optional_table(data, "routing")
        retry_table = _require_table(data, "retry")
        security_table = _require_table(data, "security")
        mailbox_auth = _load_mailbox_auth(data)
        audit_table = _require_table(data, "audit")
        behavior_table = _require_table(data, "behavior")
        rabbitmq = _load_rabbitmq(data, base_dir)

        relay = RelayConfig(
            node_id=_require_str(relay_table, "node_id"),
            state_dir=_resolve_path(base_dir, _require_str(relay_table, "state_dir")),
            log_dir=_resolve_path(base_dir, _require_str(relay_table, "log_dir")),
            watch_dir=_resolve_path(base_dir, _require_str(relay_table, "watch_dir")),
            archive_dir=_resolve_path(base_dir, _require_str(relay_table, "archive_dir")),
            deadletter_dir=_resolve_path(base_dir, _require_str(relay_table, "deadletter_dir")),
            poll_interval_ms=_require_positive_int(relay_table, "poll_interval_ms"),
            health_host=_require_str(relay_table, "health_host"),
            health_port=_require_port(relay_table, "health_port"),
        )

        endpoint_a = _load_endpoint("a", endpoints_table, base_dir)
        workers, default_worker_name = _load_workers(
            data,
            endpoints_table,
            routing_table,
            base_dir,
        )
        endpoint_b = _select_default_worker(workers, default_worker_name) or _load_endpoint(
            "b", endpoints_table, base_dir
        )

        return cls(
            relay=relay,
            endpoint_a=endpoint_a,
            endpoint_b=endpoint_b,
            workers=workers,
            default_worker_name=default_worker_name,
            transport_mode=_load_transport_mode(routing_table),
            retry=RetryConfig(
                max_attempts_b=_require_positive_int(retry_table, "max_attempts_b"),
                max_attempts_a=_require_positive_int(retry_table, "max_attempts_a"),
                initial_backoff_ms=_require_positive_int(retry_table, "initial_backoff_ms"),
                max_backoff_ms=_require_positive_int(retry_table, "max_backoff_ms"),
                jitter=_require_bool(retry_table, "jitter"),
            ),
            security=SecurityConfig(
                require_private_ingress=_require_bool(security_table, "require_private_ingress"),
                allow_http_only_for_localhost=_require_bool(
                    security_table, "allow_http_only_for_localhost"
                ),
                mask_secrets_in_logs=_require_bool(security_table, "mask_secrets_in_logs"),
            ),
            mailbox_auth=mailbox_auth,
            audit=AuditConfig(
                mode=_require_str(audit_table, "mode"),
                jsonl_path=_resolve_path(base_dir, _require_str(audit_table, "jsonl_path")),
            ),
            behavior=BehaviorConfig(
                schema_version=_require_str(behavior_table, "schema_version"),
                default_ttl_seconds=_require_positive_int(behavior_table, "default_ttl_seconds"),
                duplicate_policy=_require_str(behavior_table, "duplicate_policy"),
                inject_notice_on_error=_require_bool(
                    behavior_table, "inject_notice_on_error"
                ),
            ),
            rabbitmq=rabbitmq,
        )


def _load_endpoint(name: str, endpoints_table: dict[str, Any], base_dir: Path) -> EndpointConfig:
    endpoint_table = _require_table(endpoints_table, name)
    tunnel = _load_tunnel(endpoint_table, base_dir)
    return EndpointConfig(
        name=name,
        display_name=endpoint_table.get("display_name", name),
        base_url=_require_str(endpoint_table, "base_url").rstrip("/"),
        agent_id=_require_str(endpoint_table, "agent_id"),
        default_session_key=_require_str(endpoint_table, "default_session_key"),
        token_env=_require_str(endpoint_table, "token_env"),
        timeout_seconds=_require_positive_float(endpoint_table, "timeout_seconds"),
        tunnel=tunnel,
    )


def _load_workers(
    data: dict[str, Any],
    endpoints_table: dict[str, Any],
    routing_table: dict[str, Any] | None,
    base_dir: Path,
) -> tuple[tuple[EndpointConfig, ...], str | None]:
    workers_table = _optional_table(data, "workers")
    if workers_table:
        workers: list[EndpointConfig] = []
        for worker_name, worker_table in workers_table.items():
            if not isinstance(worker_table, dict):
                raise ConfigError(f"worker entry {worker_name!r} must be a table")
            workers.append(_load_endpoint(worker_name, workers_table, base_dir))
        if not workers:
            raise ConfigError("configuration key 'workers' must contain at least one worker table")
        default_worker_name = None
        if routing_table and "default_worker" in routing_table:
            default_worker_name = _require_str(routing_table, "default_worker")
            if default_worker_name not in {worker.name for worker in workers}:
                raise ConfigError(
                    f"routing.default_worker {default_worker_name!r} is not defined in workers"
                )
        else:
            default_worker_name = workers[0].name
        return tuple(workers), default_worker_name

    endpoint_b_table = _optional_table(endpoints_table, "b")
    if endpoint_b_table is None:
        raise ConfigError("configuration key 'endpoints.b' or top-level 'workers' is required")
    return (), None


def _load_transport_mode(routing_table: dict[str, Any] | None) -> str:
    if routing_table is None or "transport" not in routing_table:
        return "responses"
    transport_mode = _require_str(routing_table, "transport").lower()
    if transport_mode not in {"responses", "rabbitmq"}:
        raise ConfigError("routing.transport must be 'responses' or 'rabbitmq'")
    return transport_mode


def _select_default_worker(
    workers: tuple[EndpointConfig, ...],
    default_worker_name: str | None,
) -> EndpointConfig | None:
    if not workers:
        return None
    if default_worker_name:
        for worker in workers:
            if worker.name == default_worker_name:
                return worker
    return workers[0]


def _load_rabbitmq(data: dict[str, Any], base_dir: Path) -> RabbitMQConfig | None:
    table = _optional_table(data, "rabbitmq")
    if table is None:
        return None
    tls = _load_rabbitmq_tls(table, base_dir)
    queue_type = _require_str(table, "queue_type") if "queue_type" in table else "quorum"
    if queue_type not in {"quorum", "classic"}:
        raise ConfigError("configuration key 'rabbitmq.queue_type' must be 'quorum' or 'classic'")
    return RabbitMQConfig(
        host=_require_str(table, "host"),
        port=_require_port(table, "port") if "port" in table else 5672,
        virtual_host=_require_str(table, "virtual_host") if "virtual_host" in table else "/",
        user_env=_require_str(table, "user_env"),
        password_env=_require_str(table, "password_env"),
        heartbeat_seconds=_require_positive_int(table, "heartbeat_seconds")
        if "heartbeat_seconds" in table
        else 30,
        blocked_connection_timeout_seconds=_require_positive_int(
            table, "blocked_connection_timeout_seconds"
        )
        if "blocked_connection_timeout_seconds" in table
        else 30,
        prefetch_count=_require_positive_int(table, "prefetch_count")
        if "prefetch_count" in table
        else 4,
        queue_type=queue_type,
        dispatch_exchange=_require_str(table, "dispatch_exchange")
        if "dispatch_exchange" in table
        else "relay.dispatch.direct",
        reply_exchange=_require_str(table, "reply_exchange")
        if "reply_exchange" in table
        else "relay.reply.direct",
        mailbox_exchange=_require_str(table, "mailbox_exchange")
        if "mailbox_exchange" in table
        else "relay.mailbox.direct",
        deadletter_exchange=_require_str(table, "deadletter_exchange")
        if "deadletter_exchange" in table
        else "relay.dead.direct",
        events_exchange=_require_str(table, "events_exchange")
        if "events_exchange" in table
        else "relay.events.topic",
        mailbox_queue_prefix=_require_str(table, "mailbox_queue_prefix")
        if "mailbox_queue_prefix" in table
        else "relay.mailbox",
        worker_queue_prefix=_require_str(table, "worker_queue_prefix")
        if "worker_queue_prefix" in table
        else "relay.worker",
        control_queue_prefix=_require_str(table, "control_queue_prefix")
        if "control_queue_prefix" in table
        else "relay.control",
        deadletter_queue_prefix=_require_str(table, "deadletter_queue_prefix")
        if "deadletter_queue_prefix" in table
        else "relay.deadletter",
        tls=tls,
    )


def _load_mailbox_auth(data: dict[str, Any]) -> MailboxAuthConfig | None:
    table = _optional_table(data, "mailbox_auth")
    if table is None:
        return None
    tokens_table = _require_table(table, "tokens")
    pairs: list[tuple[str, str]] = []
    for mailbox in sorted(tokens_table):
        env_name = _require_str(tokens_table, mailbox)
        pairs.append((mailbox.strip(), env_name))
    if not pairs:
        raise ConfigError("mailbox_auth.tokens must define at least one mailbox token")
    return MailboxAuthConfig(
        header_name=_require_str(table, "header_name") if "header_name" in table else "Authorization",
        scheme=_require_str(table, "scheme") if "scheme" in table else "Bearer",
        token_env_by_mailbox=tuple(pairs),
    )


def _load_rabbitmq_tls(table: dict[str, Any], base_dir: Path) -> RabbitMQTLSConfig | None:
    tls_table = _optional_table(table, "tls")
    if tls_table is None:
        return None
    enabled = _require_bool(tls_table, "enabled") if "enabled" in tls_table else True
    if not enabled:
        return RabbitMQTLSConfig(
            enabled=False,
            ca_path=None,
            cert_path=None,
            key_path=None,
            server_hostname=None,
        )
    return RabbitMQTLSConfig(
        enabled=True,
        ca_path=_resolve_path(base_dir, _require_str(tls_table, "ca_path"))
        if "ca_path" in tls_table
        else None,
        cert_path=_resolve_path(base_dir, _require_str(tls_table, "cert_path"))
        if "cert_path" in tls_table
        else None,
        key_path=_resolve_path(base_dir, _require_str(tls_table, "key_path"))
        if "key_path" in tls_table
        else None,
        server_hostname=_require_str(tls_table, "server_hostname")
        if "server_hostname" in tls_table
        else None,
    )


def _load_tunnel(endpoint_table: dict[str, Any], base_dir: Path | None) -> TunnelConfig | None:
    tunnel_table = endpoint_table.get("tunnel")
    if tunnel_table is None:
        return None
    if not isinstance(tunnel_table, dict):
        raise ConfigError("endpoint tunnel must be a table")
    base = Path.cwd() if base_dir is None else base_dir
    ssh = _load_ssh_connection(tunnel_table, base)
    token_config_path = tunnel_table.get("token_config_path", "~/.openclaw/openclaw.json")
    if not isinstance(token_config_path, str) or not token_config_path.strip():
        raise ConfigError("endpoint tunnel token_config_path must be a non-empty string")
    return TunnelConfig(
        ssh=ssh,
        local_port=_require_port(tunnel_table, "local_port"),
        remote_host=_require_str(tunnel_table, "remote_host"),
        remote_port=_require_port(tunnel_table, "remote_port"),
        token_config_path=Path(token_config_path).expanduser(),
    )


def _load_ssh_connection(table: dict[str, Any], base_dir: Path) -> SSHConnectionConfig:
    return SSHConnectionConfig(
        host=_require_str(table, "ssh_host"),
        user=_require_str(table, "ssh_user"),
        key_path=_resolve_path(base_dir, _require_str(table, "ssh_key_path")),
        connect_timeout_seconds=_require_positive_int(
            table,
            "ssh_connect_timeout_seconds",
        )
        if "ssh_connect_timeout_seconds" in table
        else 10,
        strict_host_key_checking=_require_bool(table, "strict_host_key_checking")
        if "strict_host_key_checking" in table
        else True,
    )


def _resolve_path(base_dir: Path, raw_path: str) -> Path:
    return (base_dir / Path(raw_path).expanduser()).resolve()


def _require_table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"missing table {key!r}")
    return value


def _optional_table(data: dict[str, Any], key: str) -> dict[str, Any] | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConfigError(f"configuration key {key!r} must be a table")
    return value


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"configuration key {key!r} must be a non-empty string")
    return value


def _require_bool(data: dict[str, Any], key: str) -> bool:
    value = data.get(key)
    if not isinstance(value, bool):
        raise ConfigError(f"configuration key {key!r} must be a boolean")
    return value


def _require_positive_int(data: dict[str, Any], key: str) -> int:
    value = data.get(key)
    if not isinstance(value, int) or value <= 0:
        raise ConfigError(f"configuration key {key!r} must be a positive integer")
    return value


def _require_positive_float(data: dict[str, Any], key: str) -> float:
    value = data.get(key)
    if not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"configuration key {key!r} must be a positive number")
    return float(value)


def _require_port(data: dict[str, Any], key: str) -> int:
    value = _require_positive_int(data, key)
    if value > 65535:
        raise ConfigError(f"configuration key {key!r} must be a valid TCP port")
    return value
