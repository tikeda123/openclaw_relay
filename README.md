# openclaw_relay

OptionABC001 と worker 群の間を中継する Relay です。  
Relay サーバ上で常駐し、mailbox API で受けた message を対象 worker へ配送し、返答を OptionABC001 の human-facing session に戻します。

## 概要

このリポジトリは、第三ホスト上の Relay で OpenClaw 制御側と worker 群をつなぐ実装です。

- mailbox API で message を受け取る
- message の `to` に応じて対象 worker へ `/v1/responses` で配送する
- worker 返答を OptionABC001 の human-facing session へ再注入する
- 会話 UI、運用監視 UI、Prometheus metrics、Alertmanager webhook を同じ Relay から提供する

## Message-Only 方針

Relay が扱う単位はすべて `message` です。

- 指示
- 依頼
- 進捗
- 完了報告
- 質問
- 返答

を同じ message として扱います。`task` は別の transport 概念ではなく、必要なら message の本文や metadata に含めるだけです。

現在の Relay は次の 2 schema を受け付けます。

- 新方式: `relay-message/v1`
- 互換方式: `relay-envelope/v1`

新規導入は `relay-message/v1` を使ってください。

## Session 方針

人間から見える会話 session は 1 本に統一します。

- 人間 -> `OptionABC001`
  - Telegram / human-facing session
- `OptionABC001` -> worker
  - Relay message transport
- worker -> `OptionABC001`
  - 同じ human-facing session
- `OptionABC001` -> 人間
  - 同じ session 上で整理して返す

`relay-message/v1` で `returnSessionKey` を省略した場合、Relay は `OptionABC001` 側の `default_session_key` を使います。現在の既定は human-facing `agent:main:main` です。互換のため `main` を指定した場合も `agent:main:main` に正規化します。

## メッセージ運用ルール

運用ルールは次の通りです。

- 人間は `OptionABC001` とだけやり取りする
- `OptionABC001 -> worker` は必ず Relay を通す
- cross-node 通信で OpenClaw 内部 subagent dispatch は使わない
- 公開 message 形式の正本は `from` / `to` / `body`
- 互換のため `fromGateway` / `toGateway` も受理する
- `messageId`、`conversationId`、`createdAt`、`ttlSeconds`、`idempotencyKey` は Relay が自動補完する
- worker の返答は `OptionABC001` の human-facing `agent:main:main` session に戻る
- 人間向け返答は `OptionABC001` が同じ session 上で worker 結果を整理して返す

重要:

- cross-node 通信の正規入口は mailbox API である
- `watch_dir` は互換入力として残っていても正本ではない
- 返信確認は workspace 配下の `inbox/processed/done/sent` ではなく、OpenClaw の human-facing `agent:main:main` session を見る

返信確認の基準:

1. `~/.openclaw/agents/main/sessions/sessions.json` で `agent:main:main` の sessionKey を確認する
2. 対応する session jsonl に `[Reply from <worker>]` が入っているか確認する

## いまの到達点

- `systemd` 常駐
- cleanup timer
- 会話 UI `/` と監視 UI `/ops`
- Prometheus scrape と alert rule
- Alertmanager local webhook 連携
- 複数 worker を見据えた `workers.<name>` 形式
- worker 別 status / retry / deadletter / latency 可視化

現在の運用状態と今後の方針は [doc/現状と今後.md](doc/現状と今後.md) にまとめています。

## 重要な判断

現行の v1 は `1 control + 少数 worker` の試作としては使えますが、provider-grade の基盤としてそのまま拡張する前提ではありません。  
worker 数が増える運用を考えるなら、RabbitMQ を broker とする v2 に移行する前提で考えてください。設計方針は [doc/RabbitMQ移行設計.md](doc/RabbitMQ移行設計.md) にまとめています。

## RabbitMQ Phase 1

v2 への全面移行はまだですが、RabbitMQ の土台は先に入っています。

- optional な `[rabbitmq]` 設定
- topology 名の固定化
- `check-rabbitmq`
- `describe-rabbitmq-topology`
- `init-rabbitmq-topology`
- `publish-rabbitmq`
- `run-rabbitmq-worker-adapter`
- `run-rabbitmq-reply-consumer`

いまは broker の接続確認、topology 初期化、最小 publish、worker adapter、reply consumer、本体の transport 切替まで入っています。RabbitMQ を有効にするには `routing.transport = "rabbitmq"` を設定し、Relay 本体に加えて対象 worker の adapter process を常駐化します。`run` と `run-rabbitmq-worker-adapter` は起動時に topology を自動で ensure するので、`init-rabbitmq-topology` は検査・事前作成用の任意コマンドです。

```bash
PYTHONPATH=src python3 -m openclaw_relay --config config/relay.example.toml describe-rabbitmq-topology --json
PYTHONPATH=src python3 -m openclaw_relay --config config/relay.example.toml check-rabbitmq --json
PYTHONPATH=src python3 -m openclaw_relay --config config/relay.example.toml init-rabbitmq-topology --json
PYTHONPATH=src python3 -m openclaw_relay --config config/relay.example.toml publish-rabbitmq --to OptionDEF002 --body "hello" --json
PYTHONPATH=src python3 -m openclaw_relay --config config/relay.example.toml run-rabbitmq-worker-adapter --worker OptionDEF002 --once
PYTHONPATH=src python3 -m openclaw_relay --config config/relay.example.toml run-rabbitmq-worker-adapter --worker OptionGHI003 --once
PYTHONPATH=src python3 -m openclaw_relay --config config/relay.example.toml run-rabbitmq-reply-consumer --once
```

補足:

- `check-rabbitmq` と `init-rabbitmq-topology` は `pika` が必要です
- この依存は [pyproject.toml](pyproject.toml) に追加済みです
- 既存 service は RabbitMQ 未設定でも従来どおり動きます

## 最初に読む順番

- 初回導入: [doc/インストールガイド.md](doc/インストールガイド.md)
- 日常運用: [doc/日常運用ガイド.md](doc/日常運用ガイド.md)
- 新規 worker 追加: [doc/新規worker追加手順.md](doc/新規worker追加手順.md)
- Control host 送信: [doc/OptionABC001送信ガイド.md](doc/OptionABC001送信ガイド.md)
- OptionABC001 に渡す恒久ルール: [doc/OptionABC001指示書.md](doc/OptionABC001指示書.md)
- worker に渡す恒久ルール: [doc/workerエージェント指示書.md](doc/workerエージェント指示書.md)
- caller 側導入: [doc/クライアント導入ガイド.md](doc/クライアント導入ガイド.md)
- 文書の入口: [doc/文書一覧.md](doc/文書一覧.md)
- 現在の到達点: [doc/現状と今後.md](doc/現状と今後.md)
- 構成の要約: [doc/アーキテクチャ概要.md](doc/アーキテクチャ概要.md)
- message-only 方針: [doc/シンプルメッセージ移行.md](doc/シンプルメッセージ移行.md)
- RabbitMQ v2 検討: [doc/RabbitMQ移行設計.md](doc/RabbitMQ移行設計.md)
- RabbitMQ Phase 1 導入: [doc/RabbitMQ導入.md](doc/RabbitMQ導入.md)
- 常駐化: [doc/systemd常駐化.md](doc/systemd常駐化.md)
- 監視: [doc/監視とアラート.md](doc/監視とアラート.md)
- GitHub 公開前: [doc/GitHub公開前チェックリスト.md](doc/GitHub公開前チェックリスト.md)

## 最短セットアップ

公開用サンプル設定は [config/relay.example.toml](config/relay.example.toml) です。  
SSH tunnel を含むフル構成の公開用サンプルは [config/relay.remote.example.toml](config/relay.remote.example.toml) です。  
実運用ではこれらを元に `config/*.local.toml` を作り、ホスト名、token、SSH 鍵、tunnel 設定を環境に合わせて埋めます。`config/*.local.toml` と `var/` は `.gitignore` 済みです。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m openclaw_relay --config config/relay.example.toml check-config
```

初回導入は [doc/インストールガイド.md](doc/インストールガイド.md) を参照してください。  
実運用化は [doc/systemd常駐化.md](doc/systemd常駐化.md) を参照してください。

## Mailbox API

Relay はサービス利用者向けに、単純な mailbox API も提供します。

- `PUT /v1/messages`
  - `Authorization: Bearer <mailbox token>`
  - `to`, `body` を送る
- `GET /v1/messages`
  - `Authorization: Bearer <mailbox token>`
  - 自分の mailbox の先頭 1 件を FIFO で受け取る

`GET` は破壊的取得で、返した message は queue から削除されます。  
`routing.transport = "rabbitmq"` のときは、この mailbox queue backend にも RabbitMQ を使います。  
`from` は認証済み mailbox から Relay が決めるため、通常は request body に入れません。`GET` でも token と異なる `for=` は `403` になります。  
remote caller から直接使う場合は、Relay の `health_host` を `0.0.0.0` または LAN IP にして bind してください。  
caller 側へ配る最小 script は [doc/クライアント導入ガイド.md](doc/クライアント導入ガイド.md) を参照してください。  
詳細は [doc/RelayサービスAPI仕様.md](doc/RelayサービスAPI仕様.md) を参照してください。

## Control host から worker へ送る

現在の標準経路は mailbox API です。  
`OptionABC001` では `~/.local/bin/relay-mailbox-send.sh` で送信し、返信受信は常駐 mailbox injector に任せます。mailbox injector の Telegram direct notify は `notifyHuman=true` の message/reply にだけ発火します。手動 `GET /v1/messages` や `relay-mailbox-receive.sh` は通常運用で使いません。

最小送信:

```bash
relay-mailbox-send.sh \
  --to OptionDEF002 \
  --body "Reply with the single word hello."
```

人間への非同期報告も欲しいときだけ `--notify-human` を付けます。

運用ルール:

- 人間が「Telegramで報告して」「返信が来たら知らせて」と明示したときだけ `--notify-human`
- それ以外は付けない

```bash
relay-mailbox-send.sh \
  --to OptionDEF002 \
  --notify-human \
  --body "Reply with the single word hello."
```

詳細は [doc/OptionABC001送信ガイド.md](doc/OptionABC001送信ガイド.md) と [doc/クライアント導入ガイド.md](doc/クライアント導入ガイド.md) を参照してください。

補足:

- `openclaw-relay emit` は削除済み
- `fromGateway` / `toGateway` も受理するが、新規の利用者向け契約では使わない

## RabbitMQ の受信と queue 削除

RabbitMQ transport では、「受け取った瞬間に queue から消す」運用ではありません。

- worker inbox queue
  - worker adapter は `auto_ack=false` で受信する
  - `OptionDEF002` に投入して reply を reply queue へ publish できたら `ack`
  - reply publish できない、または想定外例外なら `nack(requeue=true)`
  - つまり broker 上の message は、adapter が処理完了を確認するまで queue から確定削除されない
- relay reply queue
  - reply consumer も `auto_ack=false` で受信する
  - `OptionABC001` の human-facing `agent:main:main` session に inject 成功後に `ack`
  - inject 失敗時は `nack(requeue=true)`

補足:

- worker 側の処理失敗そのものは queue 処理失敗とは分けて扱う
- worker adapter が error reply を正常に publish できた場合、その original message は `ack` される
- その後の再試行や deadletter は Relay の状態機械で扱う

## リポジトリ構成

- `src/openclaw_relay/`
  - Relay 本体
- `tests/`
  - unittest
- `config/`
  - 公開用サンプル設定、フル構成サンプル、hosts 例
- `deploy/systemd/`
  - `systemd` unit と timer
- `deploy/monitoring/`
  - Prometheus / Alertmanager 設定
- `doc/`
  - 運用、監視、公開前チェックを含む文書
- `var/`
  - ローカル runtime artifact

## 現在の構成

- Relay サーバ: この Linux サーバ
- OpenClaw A: `OptionABC001`
- 現在の既定 worker: `OptionDEF002`
- 現在の live worker: `OptionDEF002`, `OptionGHI003`
- 常駐 service: `openclaw-relay.service`
- worker adapter service: `openclaw-relay-rabbitmq-worker@b.service`, `openclaw-relay-rabbitmq-worker@ghi003.service`
- cleanup timer: `openclaw-relay-cleanup.timer`

## UI

UI は Relay の health server と同じポートで公開しています。

- Conversation UI
  - `http://127.0.0.1:18080/`
  - `http://127.0.0.1:18080/ui`
- Operations UI
  - `http://127.0.0.1:18080/ops`
  - `http://127.0.0.1:18080/monitor`
- JSON API
  - `http://127.0.0.1:18080/api/dashboard`
- Health / Ready / Metrics
  - `http://127.0.0.1:18080/healthz`
  - `http://127.0.0.1:18080/readyz`
  - `http://127.0.0.1:18080/metrics`

### UI で見えるもの

- `OptionABC001 -> worker` の message dispatch
- Relay transport の request / reply
- Relay の最小ステータス
- worker 別の状態サマリ
- worker filter
- 会話画面では古い `DEADLETTER` は既定で隠し、直近の失敗は表示する
- 古い `DEADLETTER` は必要なときだけ `Show archived deadletters` で表示
- operations 画面では会話を出さず、worker health / retry / deadletter / latency / active alerts に絞る

補足:

- 左側の bubble は `OptionABC001`
- 右側の bubble は worker
- `Relay` bubble は pending / error / 中継状態
- 会話画面 `/` は Telegram 会話や session 補助ログを出さない
- cross-node 通信の正規経路は Relay message transport
- 人間向けの見え方は `agent:main:main` に統一し、Relay transport は内部実装として分離する
- 画面上部の chip には worker ごとの `done / in-flight / retry-a / retry-b / dead-a / dead-b / dispatch / inject / tunnel / http` が出る
- `All workers` / 各 worker ボタンで会話を絞り込める

関連文書:

- 会話画面: [doc/運用ダッシュボード.md](doc/運用ダッシュボード.md)
- 監視画面: [doc/運用監視画面.md](doc/運用監視画面.md)

## UI の参照方法

### Relay サーバ上で直接見る

ブラウザで次を開きます。

```text
http://127.0.0.1:18080/
```

監視専用画面は:

```text
http://127.0.0.1:18080/ops
```

### 別端末から見る

SSH port-forward を張ってから、手元ブラウザで開きます。

```bash
ssh -L 18080:127.0.0.1:18080 relayuser@relay-node
```

その後:

```text
http://127.0.0.1:18080/
```

監視専用画面は:

```text
http://127.0.0.1:18080/ops
```

## 運用コマンド

### service 状態確認

```bash
sudo systemctl status openclaw-relay.service --no-pager
```

root 権限がない環境では `systemctl --user` でも常駐化できます。  
`loginctl show-user $USER -p Linger` が `yes` なら、user service も再起動後に自動復帰します。

### 更新反映

コードや設定を変更したら再起動します。

```bash
sudo deploy/systemd/restart-openclaw-relay.sh --worker def002 --worker ghi003
```

Relay 本体だけなら:

```bash
sudo deploy/systemd/restart-openclaw-relay.sh
```

### 基本確認

```bash
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz
curl -fsS http://127.0.0.1:18080/metrics
```

`readyz` は現在、次をまとめて見ます。

- local spool / DB が読めること
- `OptionABC001` / worker 向け SSH tunnel が生きていること
- `OptionABC001` / worker の HTTP endpoint に到達できること

### ダッシュボード API の確認

```bash
curl -fsS http://127.0.0.1:18080/api/dashboard | python3 -m json.tool | head -n 60
```

### worker 別 deadletter の確認と replay

一覧:

```bash
PYTHONPATH=src python3 -m openclaw_relay \
  --config config/relay.local.toml \
  deadletters --worker OptionDEF002 --limit 20
```

最新 1 件を replay:

```bash
PYTHONPATH=src python3 -m openclaw_relay \
  --config config/relay.local.toml \
  replay-deadletter --worker OptionDEF002 --latest
```

### worker 別 retry / deadletter / latency 集計の確認

Prometheus 形式:

```bash
curl -fsS http://127.0.0.1:18080/metrics | grep -E 'openclaw_relay_worker_(attempt|deadletter|message_status)_total|openclaw_relay_worker_latency_'
```

Prometheus alert rule 雛形:

- [deploy/monitoring/openclaw-relay-alerts.yml](deploy/monitoring/openclaw-relay-alerts.yml)
- scrape config: [deploy/monitoring/prometheus.yml](deploy/monitoring/prometheus.yml)
- merge 用断片: [deploy/monitoring/prometheus-openclaw-relay-job.yml](deploy/monitoring/prometheus-openclaw-relay-job.yml)
- 導入手順: [doc/Prometheus導入.md](doc/Prometheus導入.md)
- Alertmanager 設定: [deploy/monitoring/alertmanager.yml](deploy/monitoring/alertmanager.yml)
- Alertmanager 導入手順: [doc/Alertmanager導入.md](doc/Alertmanager導入.md)
- 詳細手順: [doc/監視とアラート.md](doc/監視とアラート.md)

CLI で worker 別状況を絞る:

```bash
PYTHONPATH=src python3 -m openclaw_relay \
  --config config/relay.local.toml \
  inspect --worker OptionDEF002 --limit 20
```

## 主要設定

- 公開用サンプル設定: [config/relay.example.toml](config/relay.example.toml)
- フル構成サンプル: [config/relay.remote.example.toml](config/relay.remote.example.toml)
- hosts 例: [config/relay.hosts.example](config/relay.hosts.example)
- systemd unit: [deploy/systemd/openclaw-relay.service](deploy/systemd/openclaw-relay.service)

重要な現在値:

- `transport = "rabbitmq"`
  - mailbox queue backend は RabbitMQ
- `mailbox_auth`
  - `Authorization: Bearer <token>` で caller を識別する
- `strict_host_key_checking = true`
  - `optionabc001` / `optiondef002` の host key が `~/.ssh/known_hosts` に必要
- 実機設定は `endpoints.b` ではなく `workers.b` を使う
  - `routing.default_worker = "b"`

## 現行の制約

現在の実装は、**制御側 1台 + 複数 worker** を前提にできます。  
ただし、まだ「複数 worker 完全版」ではありません。

- 制御側 endpoint は `endpoints.a` 固定
- worker は `workers.<name>` を複数定義できる
- message の `to` に一致する worker へ配送する
- 一致しない場合は `routing.default_worker` へフォールバックする

未実装または弱いところ:

- UI の session transcript 監視は、Relay DB ベース表示が中心

## 追加ワーカーエージェント対応

### 1. worker を1台追加または差し替える場合

次を更新します。

- `/etc/hosts`
  - 新 worker の hostname alias
- `config/*.local.toml`
  - 既存 worker 差し替えでも追加 worker でも `workers.<name>.*`
  - 必要なら `routing.default_worker`
- Relay サーバの SSH 鍵
  - 新 worker に対する専用鍵
- environment
  - worker ごとの `*_GATEWAY_TOKEN`

新 worker 側の前提:

- `Remote Login` が有効
- OpenClaw gateway が起動している
- `gateway.http.endpoints.responses.enabled = true`
- Relay サーバの公開鍵が `authorized_keys` に入っている
- `~/.openclaw/openclaw.json` の `gateway.auth.token` が有効

反映:

```bash
sudo systemctl restart openclaw-relay.service
```

確認:

```bash
curl -fsS http://127.0.0.1:18080/readyz
curl -fsS http://127.0.0.1:18080/api/dashboard | python3 -m json.tool | head -n 80
```

### 2. 複数 worker を同時運用する設定形式

複数 worker を使う場合は、`endpoints.b` の代わりに top-level `workers` を使います。

```toml
[routing]
default_worker = "def002"

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
```

このとき routing は次のルールです。

- message の `to` が `display_name` または worker 名に一致すれば、その worker へ配送
- 一致しなければ `routing.default_worker` へ配送

今後の深掘り候補:

- worker ごとの token refresh / health 表示
- worker ごとの dispatch / inject latency
- mailbox queue の retry/backoff 可視化

## 保守メンテナンスで知っておくこと

### 日常確認

最低限これを見ます。

```bash
sudo systemctl status openclaw-relay.service --no-pager
sudo systemctl status openclaw-relay-cleanup.timer --no-pager
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz
curl -fsS http://127.0.0.1:18080/metrics
```

見るべき項目:

- service が `active (running)` か
- timer が `active (waiting)` か
- `readyz` が `{"status": "ok"}` か
- `/metrics` で `openclaw_relay_ready 1` か
- `/metrics` で `openclaw_relay_endpoint_tunnel_healthy{endpoint="a"} 1` と `...{endpoint="b"} 1` か
- `/metrics` で `openclaw_relay_endpoint_http_healthy{endpoint="a"} 1` と `...{endpoint="b"} 1` か
- `/metrics` で `openclaw_relay_mailbox_queue_depth{mailbox="..."}` が溜まり続けていないか
- `/metrics` で `openclaw_relay_worker_message_status_total{worker="...",status="..."}` が想定どおりか
- `/metrics` で `openclaw_relay_worker_attempt_total{worker="...",target="...",result="..."}` が想定どおりか
- `/metrics` で `openclaw_relay_worker_deadletter_total{worker="...",target="..."}` が想定どおりか
- `/metrics` で `openclaw_relay_worker_latency_seconds{worker="...",stage="dispatch|inject",stat="avg|latest|max"}` が想定どおりか
- `/metrics` で `openclaw_relay_worker_latency_sample_count{worker="...",stage="dispatch|inject"}` が想定どおりか
- `deadletter` や `FAILED` が増えていないか

推奨の alert 条件:

- `ready == 0` が 2 分継続
- tunnel / HTTP health が 3 分継続で `0`
- `worker_deadletter_total` の 15 分増分が `> 0`
- dispatch failed の 15 分増分が `>= 3`
- dispatch latest latency が `> 120s`
- inject latest latency が `> 30s`

### token の扱い

- `A_GATEWAY_TOKEN` など環境変数で与えた token は、そのまま毎回参照する
- remote から取得する worker token は process 内で最大 5 分 cache する
- `401` / `403` が返った場合、その endpoint の cached token は即時破棄する
- 次回 retry で remote config から再取得する

つまり、worker 側で `gateway.auth.token` を更新しても、通常は Relay 再起動なしで追従できます。

### 変更時の基本手順

コード、設定、hosts、SSH 鍵、token を更新したら、次を実行します。

```bash
sudo systemctl restart openclaw-relay.service
sudo systemctl status openclaw-relay.service --no-pager
```

その後:

```bash
curl -fsS http://127.0.0.1:18080/api/dashboard | python3 -m json.tool | head -n 80
```

### ログ確認

```bash
journalctl -u openclaw-relay.service -n 100 --no-pager
tail -n 100 var/log/openclaw-relay/relay.log
tail -n 100 var/log/openclaw-relay/a2a-audit.jsonl
```

見るべきエラー:

- `source sync failed`
- `source sync health check failed`
- `failed to establish tunnel`
- `HTTP health check failed`
- `invalidated cached token`
- `dispatch failed`
- `injection failed`
- `remote session fetch skipped`

### retry の動き

現在の retry は main loop を止めません。

- 失敗した message は `FAILED_B` または `FAILED_A_INJECTION` へ一度戻る
- 次回試行時刻は DB の `next_attempt_at` に保存される
- backoff 中でも、他の message の同期・配送・再注入は継続する

つまり、1件の一時失敗で Relay 全体が詰まらない構成です。

### 容量と保持

定期的に次を見ます。

```bash
du -sh var/*
```

特に増える場所:

- `var/lib/openclaw-relay/processing`
- `var/lib/openclaw-relay/replies`
- `var/archive`
- `var/deadletter`
- `var/log/openclaw-relay`

cleanup は timer で回りますが、詰まったときは dry-run で先に確認します。

```bash
PYTHONPATH=src python3 -m openclaw_relay \
  --config config/relay.local.toml \
  cleanup --older-than-days 7 --dry-run
```

### SSH host key を更新する場面

OpenClaw 側を再構築したり SSH host key が変わった場合は、Relay サーバで `known_hosts` を更新しないと接続できません。

確認:

```bash
ssh-keygen -F optionabc001 -f ~/.ssh/known_hosts
ssh-keygen -F optiondef002 -f ~/.ssh/known_hosts
```

更新例:

```bash
ssh-keygen -R optionabc001
ssh-keyscan -H optionabc001 >> ~/.ssh/known_hosts

ssh-keygen -R optiondef002
ssh-keyscan -H optiondef002 >> ~/.ssh/known_hosts
```

更新後:

```bash
sudo systemctl restart openclaw-relay.service
curl -fsS http://127.0.0.1:18080/readyz
```

### バックアップ対象

障害時に残したいのは次です。

- `config/*.local.toml`
- `/etc/hosts`
- `/etc/systemd/system/openclaw-relay.service`
- `/etc/systemd/system/openclaw-relay-cleanup.timer`
- `var/lib/openclaw-relay/relay.db`
- `var/log/openclaw-relay/a2a-audit.jsonl`

### IP / ホスト変更時

SSH 接続先は hostname alias 前提です。IP が変わったら Relay 設定本体ではなく、まず `/etc/hosts` を更新します。

更新後:

```bash
getent hosts optionabc001 optiondef002 relay-node
sudo systemctl restart openclaw-relay.service
```

詳細は [doc/IP変更時の対処.md](doc/IP変更時の対処.md) を参照。

## GitHub 公開メモ

公開時に注意するもの:

- `var/`
  - runtime artifact。コミットしない
- `config/*.local.toml`
  - 実機向け local 設定。コミットしない
- `~/.ssh/`、`/etc/hosts`、`/etc/prometheus/*`
  - 当然このリポジトリには入れない
- 実ログや session transcript の抜粋
  - token、IP、host 名、ユーザー名が混じっていないか見直す

公開前の確認項目は [doc/GitHub公開前チェックリスト.md](doc/GitHub公開前チェックリスト.md) を参照。

## 参照文書

- 文書一覧: [doc/文書一覧.md](doc/文書一覧.md)
- 初回導入: [doc/インストールガイド.md](doc/インストールガイド.md)
- 日常運用: [doc/日常運用ガイド.md](doc/日常運用ガイド.md)
- アーキテクチャ概要: [doc/アーキテクチャ概要.md](doc/アーキテクチャ概要.md)
- 開発計画: [doc/Relay開発手順.md](doc/Relay開発手順.md)
- 常駐化: [doc/systemd常駐化.md](doc/systemd常駐化.md)
- 会話 UI: [doc/運用ダッシュボード.md](doc/運用ダッシュボード.md)
- 監視 UI: [doc/運用監視画面.md](doc/運用監視画面.md)
- 監視: [doc/監視とアラート.md](doc/監視とアラート.md)
- Prometheus: [doc/Prometheus導入.md](doc/Prometheus導入.md)
- Alertmanager: [doc/Alertmanager導入.md](doc/Alertmanager導入.md)
- cleanup: [doc/retentionとcleanup.md](doc/retentionとcleanup.md)
- IP 変更時: [doc/IP変更時の対処.md](doc/IP変更時の対処.md)
- GitHub 公開前: [doc/GitHub公開前チェックリスト.md](doc/GitHub公開前チェックリスト.md)
