from __future__ import annotations

import argparse
from collections.abc import Callable
from pathlib import Path
import signal
from types import FrameType

from openclaw_relay.app import RelayApp
from openclaw_relay.cleanup import ArtifactCleaner
from openclaw_relay.config import AppConfig, ConfigError
from openclaw_relay.logging_utils import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="openclaw-relay")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config/relay.example.toml"),
        help="path to the relay TOML configuration",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="enable debug logging",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="start the relay service")
    run_parser.add_argument(
        "--once",
        action="store_true",
        help="run initialization and a single watcher polling cycle",
    )

    subparsers.add_parser("check-config", help="validate the relay configuration")
    subparsers.add_parser("init-state", help="create runtime directories and initialize SQLite state")
    inspect_parser = subparsers.add_parser("inspect", help="inspect relay message state")
    inspect_parser.add_argument("--status", action="append", help="filter by message status")
    inspect_parser.add_argument("--worker", help="filter by worker display name or worker name")
    inspect_parser.add_argument("--limit", type=int, default=20, help="maximum rows to display")
    inspect_parser.add_argument("--json", action="store_true", help="emit JSON instead of text")

    deadletters_parser = subparsers.add_parser(
        "deadletters",
        help="inspect deadlettered relay messages",
    )
    deadletters_parser.add_argument("--worker", help="filter by worker display name or worker name")
    deadletters_parser.add_argument("--limit", type=int, default=20, help="maximum rows to display")
    deadletters_parser.add_argument("--json", action="store_true", help="emit JSON instead of text")

    replay_parser = subparsers.add_parser(
        "replay-deadletter",
        help="reset a terminal deadlettered message for replay",
    )
    replay_target = replay_parser.add_mutually_exclusive_group(required=True)
    replay_target.add_argument("--message-id", type=int, help="message id to replay")
    replay_target.add_argument(
        "--latest",
        action="store_true",
        help="replay the latest deadlettered message after optional worker filtering",
    )
    replay_parser.add_argument("--worker", help="filter by worker display name or worker name")

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="remove old local artifacts that are safe to prune",
    )
    cleanup_parser.add_argument(
        "--older-than-days",
        type=int,
        default=7,
        help="delete artifacts older than this many days",
    )
    cleanup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be deleted without deleting it",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = AppConfig.from_file(args.config)
    except (OSError, ConfigError) as exc:
        parser.error(str(exc))

    logger = configure_logging(config.relay.log_dir, verbose=args.verbose)

    if args.command == "check-config":
        logger.info("config loaded successfully from %s", args.config.resolve())
        logger.info("watch_dir=%s", config.relay.watch_dir)
        logger.info(
            "%s=%s (%s)",
            config.endpoint_a.display_name,
            config.endpoint_a.base_url,
            config.endpoint_a.agent_id,
        )
        for worker in config.worker_endpoints():
            logger.info(
                "worker[%s]=%s (%s)",
                worker.name,
                worker.base_url,
                worker.agent_id,
            )
        logger.info(
            "token envs=%s,%s",
            config.endpoint_a.token_env,
            ",".join(worker.token_env for worker in config.worker_endpoints()),
        )
        if config.source_sync is not None:
            logger.info(
                "source_sync=%s@%s:%s",
                config.source_sync.ssh.user,
                config.source_sync.ssh.host,
                config.source_sync.remote_path,
            )
        if config.endpoint_a.tunnel is not None:
            logger.info(
                "tunnel_a=127.0.0.1:%s -> %s@%s:%s",
                config.endpoint_a.tunnel.local_port,
                config.endpoint_a.tunnel.ssh.user,
                config.endpoint_a.tunnel.ssh.host,
                config.endpoint_a.tunnel.remote_port,
            )
        for worker in config.worker_endpoints():
            if worker.tunnel is None:
                continue
            logger.info(
                "tunnel_worker[%s]=127.0.0.1:%s -> %s@%s:%s",
                worker.name,
                worker.tunnel.local_port,
                worker.tunnel.ssh.user,
                worker.tunnel.ssh.host,
                worker.tunnel.remote_port,
            )
        return 0

    if args.command == "init-state":
        app = RelayApp(config, logger)
        app.initialize()
        logger.info("state initialized at %s", config.relay.database_path)
        return 0

    if args.command == "inspect":
        app = RelayApp(config, logger)
        app.initialize()
        rows = _filter_message_rows(
            app,
            statuses=set(args.status) if args.status else None,
            worker=args.worker,
        )
        _print_message_items(app, rows[-args.limit :], as_json=args.json)
        return 0

    if args.command == "deadletters":
        app = RelayApp(config, logger)
        app.initialize()
        rows = _filter_message_rows(
            app,
            statuses={"DEADLETTER_A", "DEADLETTER_B"},
            worker=args.worker,
        )
        _print_message_items(app, rows[-args.limit :], as_json=args.json)
        return 0

    if args.command == "replay-deadletter":
        app = RelayApp(config, logger)
        app.initialize()
        message_id = args.message_id
        if message_id is None:
            rows = _filter_message_rows(
                app,
                statuses={"DEADLETTER_A", "DEADLETTER_B"},
                worker=args.worker,
            )
            if not rows:
                raise ConfigError("no deadlettered messages matched the requested filters")
            message_id = int(rows[-1]["id"])
        next_status = app.replay_deadletter(message_id)
        logger.info("message_id=%s reset to status=%s", message_id, next_status)
        return 0

    if args.command == "cleanup":
        app = RelayApp(config, logger)
        app.initialize()
        cleaner = ArtifactCleaner(app.store, config.relay)
        result = cleaner.cleanup(
            older_than_days=args.older_than_days,
            dry_run=args.dry_run,
        )
        logger.info(
            "cleanup %s cutoff=%s archive=%s deadletter=%s done_processing=%s done_replies=%s",
            "dry-run" if args.dry_run else "completed",
            result.cutoff.isoformat(timespec="seconds"),
            result.counts["archive"],
            result.counts["deadletter"],
            result.counts["done_processing"],
            result.counts["done_replies"],
        )
        for path in result.deleted_paths:
            logger.info("cleanup candidate: %s", path)
        return 0

    if args.command == "run":
        app = RelayApp(config, logger)
        restore_handlers = _install_signal_handlers(app, logger)
        try:
            app.run(once=args.once)
        finally:
            restore_handlers()
        return 0

    parser.error(f"unsupported command: {args.command}")
    return 2


def _install_signal_handlers(app: RelayApp, logger) -> Callable[[], None]:
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def _handle(signum: int, _frame: FrameType | None) -> None:
        signal_name = signal.Signals(signum).name
        logger.info("received %s; shutting down relay loop", signal_name)
        app.stop()

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    def _restore() -> None:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)

    return _restore


def _filter_message_rows(app: RelayApp, *, statuses: set[str] | None, worker: str | None):
    rows = app.store.list_messages()
    if statuses:
        rows = [row for row in rows if row["status"] in statuses]
    if worker:
        worker_filter = worker.strip()
        rows = [
            row
            for row in rows
            if worker_filter in {
                app._message_worker_name(row),
                app._message_worker_display_name(row),
            }
        ]
    return rows


def _message_items(app: RelayApp, rows) -> list[dict[str, object]]:
    items = []
    for row in rows:
        items.append(
            {
                "id": row["id"],
                "status": row["status"],
                "taskId": row["task_id"],
                "idempotencyKey": row["idempotency_key"],
                "filename": row["filename"],
                "worker": app._message_worker_display_name(row),
                "bAttempts": app.store.count_attempts(message_id=row["id"], target="B"),
                "aAttempts": app.store.count_attempts(message_id=row["id"], target="A"),
                "lastError": row["last_error"],
                "updatedAt": row["updated_at"],
            }
        )
    return items


def _print_message_items(app: RelayApp, rows, *, as_json: bool) -> None:
    items = _message_items(app, rows)
    if as_json:
        import json

        print(json.dumps(items, ensure_ascii=False, indent=2))
        return

    for item in items:
        print(
            " ".join(
                [
                    f"id={item['id']}",
                    f"status={item['status']}",
                    f"task={item['taskId']}",
                    f"idem={item['idempotencyKey']}",
                    f"worker={item['worker'] or '-'}",
                    f"b_attempts={item['bAttempts']}",
                    f"a_attempts={item['aAttempts']}",
                    f"updated_at={item['updatedAt']}",
                    f"error={item['lastError'] or '-'}",
                ]
            )
        )
