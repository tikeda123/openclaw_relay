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
                    [source_sync]
                    enabled = true
                    ssh_host = "control-node"
                    ssh_user = "controluser"
                    ssh_key_path = "./keys/control"
                    ssh_connect_timeout_seconds = 10
                    strict_host_key_checking = true
                    remote_path = "/home/controluser/.openclaw/workspace/outbox/pending"
                    sync_interval_ms = 5000

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
                    default_session_key = "main"
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
                    default_session_key = "main"
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
                    schema_version = "relay-envelope/v1"
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
            self.assertIsNotNone(config.source_sync)
            self.assertEqual(config.source_sync.ssh.host, "control-node")
            self.assertEqual(config.source_sync.ssh.key_path, (root / "keys" / "control").resolve())
            self.assertEqual(config.source_sync.sync_interval_ms, 5000)
            self.assertTrue(config.source_sync.ssh.strict_host_key_checking)
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
                    default_session_key = "main"
                    token_env = "A_GATEWAY_TOKEN"
                    timeout_seconds = 30.0

                    [workers.def002]
                    display_name = "OptionDEF002"
                    base_url = "http://127.0.0.1:31901"
                    agent_id = "main"
                    default_session_key = "main"
                    token_env = "B_GATEWAY_TOKEN"
                    timeout_seconds = 30.0

                    [workers.xyz003]
                    display_name = "OptionXYZ003"
                    base_url = "http://127.0.0.1:31902"
                    agent_id = "main"
                    default_session_key = "main"
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
                    schema_version = "relay-envelope/v1"
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


if __name__ == "__main__":
    unittest.main()
