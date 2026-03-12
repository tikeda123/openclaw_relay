from __future__ import annotations

import argparse
from collections.abc import Callable
import json
import os
from pathlib import Path
import signal
from types import FrameType

from openclaw_relay.app import RelayApp
from openclaw_relay.broker import RabbitMQBroker
from openclaw_relay.cleanup import ArtifactCleaner
from openclaw_relay.config import AppConfig, ConfigError
from openclaw_relay.logging_utils import configure_logging
from openclaw_relay.outbox import build_message_payload
from openclaw_relay.rabbitmq_runtime import RabbitMQReplyConsumer, RabbitMQWorkerAdapter


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
    rabbitmq_check_parser = subparsers.add_parser(
        "check-rabbitmq",
        help="check RabbitMQ connectivity using the configured broker settings",
    )
    rabbitmq_check_parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of text",
    )
    rabbitmq_describe_parser = subparsers.add_parser(
        "describe-rabbitmq-topology",
        help="show the derived RabbitMQ exchanges, queues, and routing keys",
    )
    rabbitmq_describe_parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of text",
    )
    rabbitmq_init_parser = subparsers.add_parser(
        "init-rabbitmq-topology",
        help="declare RabbitMQ exchanges, queues, and bindings",
    )
    rabbitmq_init_parser.add_argument(
        "--json",
        action="store_true",
        help="emit JSON instead of text",
    )
    worker_adapter_parser = subparsers.add_parser(
        "run-rabbitmq-worker-adapter",
        help="consume a worker inbox queue and publish replies back to RabbitMQ",
    )
    worker_adapter_parser.add_argument("--worker", required=True, help="worker display name or worker name")
    worker_adapter_parser.add_argument("--once", action="store_true", help="process at most one message")
    reply_consumer_parser = subparsers.add_parser(
        "run-rabbitmq-reply-consumer",
        help="consume the relay reply queue and inject replies into OptionABC001",
    )
    reply_consumer_parser.add_argument("--once", action="store_true", help="process at most one reply")
    publish_rabbitmq_parser = subparsers.add_parser(
        "publish-rabbitmq",
        help="publish a relay-message/v1 directly to RabbitMQ",
    )
    publish_rabbitmq_parser.add_argument(
        "--from",
        "--from-gateway",
        dest="from_value",
        help="override sender name; defaults to endpoint A display name",
    )
    publish_rabbitmq_parser.add_argument("--from-agent", help="optional fromAgent value")
    publish_rabbitmq_parser.add_argument(
        "--to",
        "--to-gateway",
        dest="to_value",
        required=True,
        help="target worker name",
    )
    publish_rabbitmq_parser.add_argument("--to-agent", help="optional toAgent value")
    publish_rabbitmq_parser.add_argument(
        "--conversation-id",
        help="conversationId; defaults to generated messageId",
    )
    publish_rabbitmq_parser.add_argument(
        "--message-id",
        help="messageId; defaults to a generated UUID-based id",
    )
    publish_rabbitmq_parser.add_argument(
        "--intent",
        default="message",
        help="intent metadata; defaults to 'message'",
    )
    publish_rabbitmq_parser.add_argument("--return-session-key", help="optional returnSessionKey override")
    publish_rabbitmq_parser.add_argument("--ttl-seconds", type=int, default=1800, help="message TTL in seconds")
    publish_rabbitmq_parser.add_argument("--idempotency-key", help="optional idempotency key")
    publish_rabbitmq_body = publish_rabbitmq_parser.add_mutually_exclusive_group(required=True)
    publish_rabbitmq_body.add_argument("--body", help="message body text")
    publish_rabbitmq_body.add_argument("--body-file", type=Path, help="path to a UTF-8 body text file")
    publish_rabbitmq_parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    emit_parser = subparsers.add_parser(
        "emit",
        help="removed; use relay-mailbox-send.sh or PUT /v1/messages instead",
    )
    emit_parser.add_argument("--outbox-dir", type=Path, help="control-host outbox/pending directory")
    emit_parser.add_argument(
        "--from",
        "--from-gateway",
        dest="from_value",
        help="override sender name; defaults to endpoint A display name",
    )
    emit_parser.add_argument("--from-agent", help="optional fromAgent value")
    emit_parser.add_argument(
        "--to",
        "--to-gateway",
        dest="to_value",
        required=True,
        help="target worker name",
    )
    emit_parser.add_argument("--to-agent", help="optional toAgent value")
    emit_parser.add_argument("--conversation-id", help="conversationId; defaults to generated messageId")
    emit_parser.add_argument("--message-id", help="messageId; defaults to a generated UUID-based id")
    emit_parser.add_argument("--intent", default="message", help="intent metadata; defaults to 'message'")
    emit_parser.add_argument("--return-session-key", help="optional returnSessionKey override")
    emit_parser.add_argument("--ttl-seconds", type=int, default=1800, help="message TTL in seconds")
    emit_parser.add_argument("--idempotency-key", help="optional idempotency key")
    emit_parser.add_argument("--filename", help="optional output filename; defaults to <messageId>.json")
    emit_body = emit_parser.add_mutually_exclusive_group(required=True)
    emit_body.add_argument("--body", help="message body text")
    emit_body.add_argument("--body-file", type=Path, help="path to a UTF-8 body text file")
    emit_parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
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
        logger = configure_logging(config.relay.log_dir, verbose=args.verbose)
        return _run_command(args, config, logger, parser)
    except (OSError, ConfigError) as exc:
        parser.error(str(exc))
    return 2


def _run_command(args, config: AppConfig, logger, parser: argparse.ArgumentParser) -> int:
    if args.command == "check-config":
        _validate_mailbox_auth_environment(config)
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
        logger.info("transport=%s", config.transport_mode)
        logger.info(
            "token envs=%s,%s",
            config.endpoint_a.token_env,
            ",".join(worker.token_env for worker in config.worker_endpoints()),
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
        if config.rabbitmq is not None:
            logger.info(
                "rabbitmq=%s:%s vhost=%s queue_type=%s",
                config.rabbitmq.host,
                config.rabbitmq.port,
                config.rabbitmq.virtual_host,
                config.rabbitmq.queue_type,
            )
        return 0

    if args.command == "init-state":
        app = RelayApp(config, logger)
        app.initialize()
        logger.info("state initialized at %s", config.relay.database_path)
        return 0

    if args.command == "check-rabbitmq":
        broker = RabbitMQBroker(config)
        summary = broker.check_connection()
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            logger.info(
                "rabbitmq reachable %s:%s vhost=%s queue_type=%s",
                summary["host"],
                summary["port"],
                summary["virtualHost"],
                summary["queueType"],
            )
        return 0

    if args.command == "describe-rabbitmq-topology":
        broker = RabbitMQBroker(config)
        plan = broker.describe_topology().as_dict()
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            _print_rabbitmq_topology(plan)
        return 0

    if args.command == "init-rabbitmq-topology":
        broker = RabbitMQBroker(config)
        plan = broker.ensure_topology().as_dict()
        if args.json:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
        else:
            logger.info("rabbitmq topology initialized")
            _print_rabbitmq_topology(plan)
        return 0

    if args.command == "publish-rabbitmq":
        body = args.body
        if body is None:
            body = args.body_file.read_text(encoding="utf-8")
        payload = build_message_payload(
            from_gateway=args.from_value or config.endpoint_a.display_name,
            to_gateway=args.to_value,
            body=body,
            conversation_id=args.conversation_id,
            message_id=args.message_id,
            from_agent=args.from_agent,
            to_agent=args.to_agent,
            intent=args.intent,
            return_session_key=args.return_session_key,
            ttl_seconds=args.ttl_seconds,
            idempotency_key=args.idempotency_key,
        )
        broker_summary = RabbitMQBroker(config).publish_message(payload)
        summary = {
            "exchange": broker_summary["exchange"],
            "routingKey": broker_summary["routingKey"],
            "replyTo": broker_summary["replyTo"],
            "messageId": broker_summary["messageId"],
            "conversationId": broker_summary["conversationId"],
            "from": payload["from"],
            "to": payload["to"],
            "fromGateway": payload["fromGateway"],
            "toGateway": broker_summary["toGateway"],
        }
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(f"published exchange={summary['exchange']}")
            print(f"routingKey={summary['routingKey']}")
            print(f"replyTo={summary['replyTo']}")
            print(f"messageId={summary['messageId']}")
            print(f"conversationId={summary['conversationId']}")
            print(f"to={summary['to']}")
        return 0

    if args.command == "run-rabbitmq-worker-adapter":
        runtime = RabbitMQWorkerAdapter(
            config,
            logger,
            worker_name=args.worker,
        )
        restore_handlers = _install_signal_handlers(runtime, logger)
        try:
            runtime.run(once=args.once)
        finally:
            restore_handlers()
        return 0

    if args.command == "run-rabbitmq-reply-consumer":
        runtime = RabbitMQReplyConsumer(config, logger)
        restore_handlers = _install_signal_handlers(runtime, logger)
        try:
            runtime.run(once=args.once)
        finally:
            restore_handlers()
        return 0

    if args.command == "emit":
        raise ConfigError("emit command was removed; use relay-mailbox-send.sh or PUT /v1/messages")

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


def _install_signal_handlers(stoppable, logger) -> Callable[[], None]:
    previous_sigint = signal.getsignal(signal.SIGINT)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def _handle(signum: int, _frame: FrameType | None) -> None:
        signal_name = signal.Signals(signum).name
        logger.info("received %s; shutting down relay loop", signal_name)
        stoppable.stop()

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
                "conversationId": row["task_id"],
                "messageId": row["turn_id"],
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
                    f"conversation={item['conversationId']}",
                    f"message={item['messageId']}",
                    f"idem={item['idempotencyKey']}",
                    f"worker={item['worker'] or '-'}",
                    f"b_attempts={item['bAttempts']}",
                    f"a_attempts={item['aAttempts']}",
                    f"updated_at={item['updatedAt']}",
                    f"error={item['lastError'] or '-'}",
                ]
            )
        )


def _print_rabbitmq_topology(plan: dict[str, object]) -> None:
    print(f"dispatch_exchange={plan['dispatchExchange']}")
    print(f"reply_exchange={plan['replyExchange']}")
    print(f"mailbox_exchange={plan['mailboxExchange']}")
    print(f"deadletter_exchange={plan['deadletterExchange']}")
    print(f"events_exchange={plan['eventsExchange']}")
    relay_reply_queue = plan["relayReplyQueue"]
    relay_deadletter_queue = plan["relayDeadletterQueue"]
    print(
        "relay_reply_queue="
        f"{relay_reply_queue['queue']} routing_key={relay_reply_queue['routingKey']}"
    )
    print(
        "relay_deadletter_queue="
        f"{relay_deadletter_queue['queue']} routing_key={relay_deadletter_queue['routingKey']}"
    )
    for binding in plan["workerInboxQueues"]:
        print(
            "worker_inbox_queue="
            f"{binding['queue']} routing_key={binding['routingKey']}"
        )
    for binding in plan["workerDeadletterQueues"]:
        print(
            "worker_deadletter_queue="
            f"{binding['queue']} routing_key={binding['routingKey']}"
        )


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
