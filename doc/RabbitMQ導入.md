# RabbitMQ導入

## 目的

RabbitMQ v2 の検証用に、Relay ホスト上で broker を起動し、`check-rabbitmq` と `init-rabbitmq-topology` を通すための手順です。

現時点では RabbitMQ は **Phase 1** です。

- broker 接続確認
- topology 導出
- exchange / queue / binding 初期化
- 最小 publish
- worker adapter
- reply consumer

までが対象です。Relay 本体で RabbitMQ transport を使うときは `routing.transport = "rabbitmq"` を設定します。

## 1. 依存

Python 側は `pika` が必要です。

```bash
/home/tikeda/anaconda3/envs/ntrader/bin/python3 -m pip install 'pika>=1.3,<2'
```

## 2. ローカル RabbitMQ を起動

repo には開発用 compose を置いています。

```bash
docker compose -f deploy/docker-compose.rabbitmq.yml up -d
docker compose -f deploy/docker-compose.rabbitmq.yml ps
```

管理 UI:

```text
http://127.0.0.1:15672
```

初期値:

- user: `relay`
- password: `relay-dev-pass`
- vhost: `/openclaw-relay`

## 3. Relay 設定

既存の sample config をベースに、`[rabbitmq]` のみローカル向けに設定します。

最低限必要なのは次です。

```toml
[rabbitmq]
host = "127.0.0.1"
port = 5672
virtual_host = "/openclaw-relay"
user_env = "RABBITMQ_USER"
password_env = "RABBITMQ_PASSWORD"
heartbeat_seconds = 30
blocked_connection_timeout_seconds = 30
prefetch_count = 4
queue_type = "quorum"
dispatch_exchange = "relay.dispatch.direct"
reply_exchange = "relay.reply.direct"
deadletter_exchange = "relay.dead.direct"
events_exchange = "relay.events.topic"
worker_queue_prefix = "relay.worker"
control_queue_prefix = "relay.control"
deadletter_queue_prefix = "relay.deadletter"

[rabbitmq.tls]
enabled = false
```

資格情報は環境変数で渡します。

```bash
export RABBITMQ_USER=relay
export RABBITMQ_PASSWORD=relay-dev-pass
```

systemd で常駐させる場合は、`deploy/systemd/openclaw-relay.env.example` を
`/etc/default/openclaw-relay` にコピーして値を設定します。

## 4. 接続確認

```bash
PYTHONPATH=src python3 -m openclaw_relay \
  --config config/relay.example.toml \
  check-rabbitmq --json
```

## 5. Topology 確認

```bash
PYTHONPATH=src python3 -m openclaw_relay \
  --config config/relay.example.toml \
  describe-rabbitmq-topology --json
```

## 6. Topology 初期化

`run` と `run-rabbitmq-worker-adapter` は起動時に topology を自動で ensure する。  
ここでの `init-rabbitmq-topology` は、事前に queue / exchange を確認したい場合の任意手順である。

```bash
PYTHONPATH=src python3 -m openclaw_relay \
  --config config/relay.example.toml \
  init-rabbitmq-topology --json
```

## 7. publish 疎通確認

Topology 作成後、broker へ 1 message を publish できます。

```bash
PYTHONPATH=src python3 -m openclaw_relay \
  --config config/relay.example.toml \
  publish-rabbitmq \
  --to OptionDEF002 \
  --body "Reply with the single word ACK." \
  --json
```

この段階では worker adapter はまだないため、queue に載るところまでが確認対象です。

## 8. worker adapter / reply consumer

worker inbox queue と relay reply queue を処理する独立プロセスも実装済みです。

worker adapter:

```bash
PYTHONPATH=src python3 -m openclaw_relay \
  --config config/relay.example.toml \
  run-rabbitmq-worker-adapter \
  --worker OptionDEF002 \
  --once

PYTHONPATH=src python3 -m openclaw_relay \
  --config config/relay.example.toml \
  run-rabbitmq-worker-adapter \
  --worker OptionGHI003 \
  --once
```

reply consumer:

```bash
PYTHONPATH=src python3 -m openclaw_relay \
  --config config/relay.example.toml \
  run-rabbitmq-reply-consumer \
  --once
```

## 8-1. ack / nack のルール

この構成では、queue message は「受け取った瞬間」には消えません。

- worker inbox queue
  - `auto_ack=false`
  - worker adapter が message を受け取る
  - `OptionDEF002` へ投入し、reply queue への publish が成功したら `ack`
  - reply publish に失敗したら `nack(requeue=true)`
  - 想定外例外でも `nack(requeue=true)`
- relay reply queue
  - `auto_ack=false`
  - reply consumer が reply を受け取る
  - `OptionABC001` の human-facing `agent:main:main` session へ inject 成功後に `ack`
  - inject 失敗時は `nack(requeue=true)`

重要:

- queue ack は transport の完了確認
- worker の意味的な成功/失敗は別で、error reply として返す
- worker adapter が error reply を正常 publish できた場合、original message は `ack` される
- その後の retry / deadletter は Relay 側の state machine で管理する

## 9. 期待される topology

- exchange
  - `relay.dispatch.direct`
  - `relay.reply.direct`
  - `relay.dead.direct`
  - `relay.events.topic`
- queue
  - `relay.control.optionabc001.reply`
  - `relay.deadletter.optionabc001.control`
  - `relay.worker.optiondef002.inbox`
  - `relay.worker.optionghi003.inbox`
  - `relay.deadletter.optiondef002.worker`
  - `relay.deadletter.optionghi003.worker`

## 10. 確認

次が通れば Phase 1 の broker 検証は完了です。

1. `check-rabbitmq` が成功する
2. `init-rabbitmq-topology` が成功する
3. `publish-rabbitmq` が成功する
4. `run-rabbitmq-worker-adapter --once` が 1 message を処理できる
5. `run-rabbitmq-reply-consumer --once` が 1 reply を処理できる
6. RabbitMQ management UI に exchange / queue が見える

## 11. 停止

```bash
docker compose -f deploy/docker-compose.rabbitmq.yml down
```
