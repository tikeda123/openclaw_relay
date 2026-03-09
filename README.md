# openclaw_relay

OptionABC001 と worker 群の間を中継する Relay です。  
Relay サーバ上で常駐し、OptionABC001 の outbox を同期して対象 worker へ配送し、返答を OptionABC001 に戻します。

## 概要

このリポジトリは、第三ホスト上の Relay で OpenClaw 制御側と worker 群をつなぐ実装です。

- OptionABC001 の outbox を pull して取り込む
- `toGateway` に応じて対象 worker へ `/v1/responses` を配送する
- worker 返答を OptionABC001 の session へ再注入する
- 会話 UI、運用監視 UI、Prometheus metrics、Alertmanager webhook を同じ Relay から提供する

## いまの到達点

- `systemd` 常駐
- cleanup timer
- 会話 UI `/` と監視 UI `/ops`
- Prometheus scrape と alert rule
- Alertmanager local webhook 連携
- 複数 worker を見据えた `workers.<name>` 形式
- worker 別 status / retry / deadletter / latency 可視化

## 最初に読む順番

- 初回導入: [doc/インストールガイド.md](doc/インストールガイド.md)
- 日常運用: [doc/日常運用ガイド.md](doc/日常運用ガイド.md)
- 文書の入口: [doc/文書一覧.md](doc/文書一覧.md)
- 構成の要約: [doc/アーキテクチャ概要.md](doc/アーキテクチャ概要.md)
- 常駐化: [doc/systemd常駐化.md](doc/systemd常駐化.md)
- 監視: [doc/監視とアラート.md](doc/監視とアラート.md)
- GitHub 公開前: [doc/GitHub公開前チェックリスト.md](doc/GitHub公開前チェックリスト.md)

## 最短セットアップ

公開用サンプル設定は [config/relay.example.toml](config/relay.example.toml) です。  
SSH tunnel と `source_sync` を含むフル構成の公開用サンプルは [config/relay.remote.example.toml](config/relay.remote.example.toml) です。  
実運用ではこれらを元に `config/*.local.toml` を作り、ホスト名、token、SSH 鍵、tunnel 設定を環境に合わせて埋めます。`config/*.local.toml` と `var/` は `.gitignore` 済みです。

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m openclaw_relay --config config/relay.example.toml check-config
```

初回導入は [doc/インストールガイド.md](doc/インストールガイド.md) を参照してください。  
実運用化は [doc/systemd常駐化.md](doc/systemd常駐化.md) を参照してください。

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
- 常駐 service: `openclaw-relay.service`
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

- `OptionABC001 -> worker` の dispatch
- worker subagent transcript から拾った返答
- `OptionABC001` 側の進行中メモ、runId、sessionKey
- Relay の最小ステータス
- worker 別の状態サマリ
- worker filter
- operations 画面では会話を出さず、worker health / retry / deadletter / latency / active alerts に絞る

補足:

- 左側の bubble は `OptionABC001`
- 右側の bubble は worker
- `Relay` bubble は pending / error / 中継状態
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

### 更新反映

コードや設定を変更したら再起動します。

```bash
sudo systemctl restart openclaw-relay.service
```

### 基本確認

```bash
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz
curl -fsS http://127.0.0.1:18080/metrics
```

`readyz` は現在、次をまとめて見ます。

- local spool / DB が読めること
- `source_sync` の疎通が通っていること
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

- `source_sync.sync_interval_ms = 5000`
  - outbox 同期は 5 秒間隔
- `strict_host_key_checking = true`
  - `optionabc001` / `optiondef002` の host key が `~/.ssh/known_hosts` に必要
- 実機設定は `endpoints.b` ではなく `workers.b` を使う
  - `routing.default_worker = "b"`

## 現行の制約

現在の実装は、**制御側 1台 + 複数 worker** を前提にできます。  
ただし、まだ「複数 worker 完全版」ではありません。

- `source_sync` は 1 系統
- 制御側 endpoint は `endpoints.a` 固定
- worker は `workers.<name>` を複数定義できる
- envelope の `toGateway` に一致する worker へ配送する
- 一致しない場合は `routing.default_worker` へフォールバックする

未実装または弱いところ:

- UI の session transcript 監視は、Relay DB ベース表示が中心
- source_sync は OptionABC001 1 系統のみ

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
```

このとき routing は次のルールです。

- envelope の `toGateway` が `display_name` または worker 名に一致すれば、その worker へ配送
- 一致しなければ `routing.default_worker` へ配送

今後の深掘り候補:

- worker ごとの token refresh / health 表示
- worker ごとの dispatch / inject latency
- source_sync の複線化または push 化

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
- `/metrics` で `openclaw_relay_source_sync_healthy 1` か
- `/metrics` で `openclaw_relay_endpoint_tunnel_healthy{endpoint="a"} 1` と `...{endpoint="b"} 1` か
- `/metrics` で `openclaw_relay_endpoint_http_healthy{endpoint="a"} 1` と `...{endpoint="b"} 1` か
- `/metrics` で `openclaw_relay_worker_message_status_total{worker="...",status="..."}` が想定どおりか
- `/metrics` で `openclaw_relay_worker_attempt_total{worker="...",target="...",result="..."}` が想定どおりか
- `/metrics` で `openclaw_relay_worker_deadletter_total{worker="...",target="..."}` が想定どおりか
- `/metrics` で `openclaw_relay_worker_latency_seconds{worker="...",stage="dispatch|inject",stat="avg|latest|max"}` が想定どおりか
- `/metrics` で `openclaw_relay_worker_latency_sample_count{worker="...",stage="dispatch|inject"}` が想定どおりか
- `deadletter` や `FAILED` が増えていないか

推奨の alert 条件:

- `ready == 0` が 2 分継続
- `source_sync_healthy == 0` が 3 分継続
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
