from __future__ import annotations

from pathlib import Path
import tempfile
import textwrap
import unittest

from openclaw_relay.config import AppConfig


class ConfigTests(unittest.TestCase):
    def test_load_config_resolves_relative_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "relay.toml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    [relay]
                    node_id = "relay-a"
                    state_dir = "./state"
                    log_dir = "./log"
                    watch_dir = "./watch"
                    archive_dir = "./archive"
                    deadletter_dir = "./deadletter"
                    poll_interval_ms = 500
                    health_host = "127.0.0.1"
                    health_port = 18080

                    [endpoints.a]
                    display_name = "OptionABC001"
                    base_url = "http://10.0.0.10:18789"
                    agent_id = "orchestrator"
                    default_session_key = "agent:main:main"
                    token_env = "A_GATEWAY_TOKEN"
                    timeout_seconds = 30.0

                    [endpoints.a.tunnel]
                    ssh_host = "control-node"
                    ssh_user = "controluser"
                    ssh_key_path = "./keys/control"
                    ssh_connect_timeout_seconds = 10
                    strict_host_key_checking = true
                    local_port = 31879
                    remote_host = "127.0.0.1"
                    remote_port = 18789
                    token_config_path = "/home/controluser/.openclaw/openclaw.json"

                    [endpoints.b]
                    display_name = "OptionDEF002"
                    base_url = "http://10.0.0.11:19001"
                    agent_id = "worker"
                    default_session_key = "agent:main:main"
                    token_env = "B_GATEWAY_TOKEN"
                    timeout_seconds = 30.0

                    [retry]
                    max_attempts_b = 5
                    max_attempts_a = 10
                    initial_backoff_ms = 1000
                    max_backoff_ms = 30000
                    jitter = true

                    [security]
                    require_private_ingress = true
                    allow_http_only_for_localhost = false
                    mask_secrets_in_logs = true

                    [audit]
                    mode = "preview"
                    jsonl_path = "./log/a2a-audit.jsonl"

                    [behavior]
                    schema_version = "relay-message/v1"
                    default_ttl_seconds = 300
                    duplicate_policy = "suppress"
                    inject_notice_on_error = true
                    """
                ).strip(),
                encoding="utf-8",
            )

            config = AppConfig.from_file(config_path)

            self.assertEqual(config.relay.node_id, "relay-a")
            self.assertEqual(config.relay.state_dir, (root / "state").resolve())
            self.assertEqual(config.endpoint_b.agent_id, "worker")
            self.assertEqual(config.endpoint_a.display_name, "OptionABC001")
            self.assertEqual(config.endpoint_b.display_name, "OptionDEF002")
            self.assertIsNotNone(config.endpoint_a.tunnel)
            self.assertEqual(config.endpoint_a.tunnel.local_port, 31879)
            self.assertTrue(config.endpoint_a.tunnel.ssh.strict_host_key_checking)
            self.assertEqual(config.audit.jsonl_path, (root / "log" / "a2a-audit.jsonl").resolve())

    def test_load_config_supports_multiple_workers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "relay.toml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    [relay]
                    node_id = "relay-a"
                    state_dir = "./state"
                    log_dir = "./log"
                    watch_dir = "./watch"
                    archive_dir = "./archive"
                    deadletter_dir = "./deadletter"
                    poll_interval_ms = 500
                    health_host = "127.0.0.1"
                    health_port = 18080

                    [endpoints.a]
                    display_name = "OptionABC001"
                    base_url = "http://127.0.0.1:31879"
                    agent_id = "main"
                    default_session_key = "agent:main:main"
                    token_env = "A_GATEWAY_TOKEN"
                    timeout_seconds = 30.0

                    [workers.def002]
                    display_name = "OptionDEF002"
                    base_url = "http://127.0.0.1:31901"
                    agent_id = "main"
                    default_session_key = "agent:main:main"
                    token_env = "B_GATEWAY_TOKEN"
                    timeout_seconds = 30.0

                    [workers.xyz003]
                    display_name = "OptionXYZ003"
                    base_url = "http://127.0.0.1:31902"
                    agent_id = "main"
                    default_session_key = "agent:main:main"
                    token_env = "C_GATEWAY_TOKEN"
                    timeout_seconds = 30.0

                    [routing]
                    default_worker = "xyz003"

                    [retry]
                    max_attempts_b = 5
                    max_attempts_a = 10
                    initial_backoff_ms = 1000
                    max_backoff_ms = 30000
                    jitter = true

                    [security]
                    require_private_ingress = true
                    allow_http_only_for_localhost = false
                    mask_secrets_in_logs = true

                    [audit]
                    mode = "preview"
                    jsonl_path = "./log/a2a-audit.jsonl"

                    [behavior]
                    schema_version = "relay-message/v1"
                    default_ttl_seconds = 300
                    duplicate_policy = "suppress"
                    inject_notice_on_error = true
                    """
                ).strip(),
                encoding="utf-8",
            )

            config = AppConfig.from_file(config_path)

            self.assertEqual([worker.name for worker in config.worker_endpoints()], ["def002", "xyz003"])
            self.assertEqual(config.default_worker.name, "xyz003")
            self.assertEqual(config.resolve_worker("OptionDEF002").name, "def002")
            self.assertEqual(config.resolve_worker("xyz003").display_name, "OptionXYZ003")

    def test_load_config_supports_rabbitmq(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "relay.toml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    [relay]
                    node_id = "relay-a"
                    state_dir = "./state"
                    log_dir = "./log"
                    watch_dir = "./watch"
                    archive_dir = "./archive"
                    deadletter_dir = "./deadletter"
                    poll_interval_ms = 500
                    health_host = "127.0.0.1"
                    health_port = 18080

                    [endpoints.a]
                    display_name = "OptionABC001"
                    base_url = "http://127.0.0.1:31879"
                    agent_id = "main"
                    default_session_key = "agent:main:main"
                    token_env = "A_GATEWAY_TOKEN"
                    timeout_seconds = 30.0

                    [workers.def002]
                    display_name = "OptionDEF002"
                    base_url = "http://127.0.0.1:31901"
                    agent_id = "main"
                    default_session_key = "agent:main:main"
                    token_env = "B_GATEWAY_TOKEN"
                    timeout_seconds = 30.0

                    [retry]
                    max_attempts_b = 5
                    max_attempts_a = 10
                    initial_backoff_ms = 1000
                    max_backoff_ms = 30000
                    jitter = true

                    [security]
                    require_private_ingress = true
                    allow_http_only_for_localhost = false
                    mask_secrets_in_logs = true

                    [audit]
                    mode = "preview"
                    jsonl_path = "./log/a2a-audit.jsonl"

                    [behavior]
                    schema_version = "relay-message/v1"
                    default_ttl_seconds = 300
                    duplicate_policy = "suppress"
                    inject_notice_on_error = true

                    [rabbitmq]
                    host = "rabbitmq.internal"
                    port = 5671
                    virtual_host = "/openclaw-relay"
                    user_env = "RABBITMQ_USER"
                    password_env = "RABBITMQ_PASSWORD"
                    heartbeat_seconds = 45
                    blocked_connection_timeout_seconds = 20
                    prefetch_count = 2
                    queue_type = "quorum"
                    dispatch_exchange = "relay.dispatch.direct"
                    reply_exchange = "relay.reply.direct"
                    deadletter_exchange = "relay.dead.direct"
                    events_exchange = "relay.events.topic"
                    worker_queue_prefix = "relay.worker"
                    control_queue_prefix = "relay.control"
                    deadletter_queue_prefix = "relay.deadletter"

                    [rabbitmq.tls]
                    enabled = true
                    ca_path = "./certs/ca.pem"
                    cert_path = "./certs/client.pem"
                    key_path = "./certs/client.key"
                    server_hostname = "rabbitmq.internal"
                    """
                ).strip(),
                encoding="utf-8",
            )

            config = AppConfig.from_file(config_path)

            self.assertIsNotNone(config.rabbitmq)
            assert config.rabbitmq is not None
            self.assertEqual(config.transport_mode, "responses")
            self.assertEqual(config.rabbitmq.host, "rabbitmq.internal")
            self.assertEqual(config.rabbitmq.port, 5671)
            self.assertEqual(config.rabbitmq.virtual_host, "/openclaw-relay")
            self.assertEqual(config.rabbitmq.prefetch_count, 2)
            self.assertEqual(config.rabbitmq.queue_type, "quorum")
            self.assertEqual(config.rabbitmq.mailbox_exchange, "relay.mailbox.direct")
            self.assertEqual(config.rabbitmq.mailbox_queue_prefix, "relay.mailbox")
            self.assertIsNotNone(config.rabbitmq.tls)
            assert config.rabbitmq.tls is not None
            self.assertEqual(config.rabbitmq.tls.ca_path, (root / "certs" / "ca.pem").resolve())
            self.assertEqual(
                config.rabbitmq.tls.cert_path,
                (root / "certs" / "client.pem").resolve(),
            )
            self.assertEqual(
                config.rabbitmq.tls.key_path,
                (root / "certs" / "client.key").resolve(),
            )

    def test_load_config_supports_rabbitmq_transport_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "relay.toml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    [relay]
                    node_id = "relay-a"
                    state_dir = "./state"
                    log_dir = "./log"
                    watch_dir = "./watch"
                    archive_dir = "./archive"
                    deadletter_dir = "./deadletter"
                    poll_interval_ms = 500
                    health_host = "127.0.0.1"
                    health_port = 18080

                    [endpoints.a]
                    display_name = "OptionABC001"
                    base_url = "http://127.0.0.1:31879"
                    agent_id = "main"
                    default_session_key = "agent:main:main"
                    token_env = "A_GATEWAY_TOKEN"
                    timeout_seconds = 30.0

                    [workers.def002]
                    display_name = "OptionDEF002"
                    base_url = "http://127.0.0.1:31901"
                    agent_id = "main"
                    default_session_key = "agent:main:main"
                    token_env = "B_GATEWAY_TOKEN"
                    timeout_seconds = 30.0

                    [routing]
                    default_worker = "def002"
                    transport = "rabbitmq"

                    [retry]
                    max_attempts_b = 5
                    max_attempts_a = 10
                    initial_backoff_ms = 1000
                    max_backoff_ms = 30000
                    jitter = true

                    [security]
                    require_private_ingress = true
                    allow_http_only_for_localhost = false
                    mask_secrets_in_logs = true

                    [audit]
                    mode = "preview"
                    jsonl_path = "./log/a2a-audit.jsonl"

                    [behavior]
                    schema_version = "relay-message/v1"
                    default_ttl_seconds = 300
                    duplicate_policy = "suppress"
                    inject_notice_on_error = true

                    [rabbitmq]
                    host = "rabbitmq.internal"
                    user_env = "RABBITMQ_USER"
                    password_env = "RABBITMQ_PASSWORD"
                    """
                ).strip(),
                encoding="utf-8",
            )

            config = AppConfig.from_file(config_path)

            self.assertEqual(config.transport_mode, "rabbitmq")

    def test_load_config_supports_mailbox_auth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "relay.toml"
            config_path.write_text(
                textwrap.dedent(
                    """
                    [relay]
                    node_id = "relay-a"
                    state_dir = "./state"
                    log_dir = "./log"
                    watch_dir = "./watch"
                    archive_dir = "./archive"
                    deadletter_dir = "./deadletter"
                    poll_interval_ms = 500
                    health_host = "127.0.0.1"
                    health_port = 18080

                    [endpoints.a]
                    display_name = "OptionABC001"
                    base_url = "http://127.0.0.1:31879"
                    agent_id = "main"
                    default_session_key = "agent:main:main"
                    token_env = "A_GATEWAY_TOKEN"
                    timeout_seconds = 30.0

                    [workers.def002]
                    display_name = "OptionDEF002"
                    base_url = "http://127.0.0.1:31901"
                    agent_id = "main"
                    default_session_key = "agent:main:main"
                    token_env = "B_GATEWAY_TOKEN"
                    timeout_seconds = 30.0

                    [retry]
                    max_attempts_b = 5
                    max_attempts_a = 10
                    initial_backoff_ms = 1000
                    max_backoff_ms = 30000
                    jitter = true

                    [security]
                    require_private_ingress = true
                    allow_http_only_for_localhost = true
                    mask_secrets_in_logs = true

                    [mailbox_auth]
                    header_name = "Authorization"
                    scheme = "Bearer"

                    [mailbox_auth.tokens]
                    OptionABC001 = "RELAY_MAILBOX_TOKEN_OPTIONABC001"
                    OptionDEF002 = "RELAY_MAILBOX_TOKEN_OPTIONDEF002"

                    [audit]
                    mode = "preview"
                    jsonl_path = "./log/a2a-audit.jsonl"

                    [behavior]
                    schema_version = "relay-message/v1"
                    default_ttl_seconds = 300
                    duplicate_policy = "suppress"
                    inject_notice_on_error = true
                    """
                ).strip(),
                encoding="utf-8",
            )

            config = AppConfig.from_file(config_path)

            self.assertIsNotNone(config.mailbox_auth)
            assert config.mailbox_auth is not None
            self.assertEqual(config.mailbox_auth.header_name, "Authorization")
            self.assertEqual(config.mailbox_auth.scheme, "Bearer")
            self.assertEqual(
                config.mailbox_auth.token_env_by_mailbox,
                (
                    ("OptionABC001", "RELAY_MAILBOX_TOKEN_OPTIONABC001"),
                    ("OptionDEF002", "RELAY_MAILBOX_TOKEN_OPTIONDEF002"),
                ),
            )


if __name__ == "__main__":
    unittest.main()
