# systemd 常駐化

Relay の常駐は system service として行う。  
理由は、サーバ再起動後の自動復帰を user login に依存させないため。

## 前提

- Relay サーバの `/etc/hosts` に `optionabc001` と `optiondef002` が定義されている。
- systemd service は仮想環境内の Python を使う。
- root 権限がない場合は `systemctl --user` を使う。`loginctl show-user $USER -p Linger` が `yes` なら、user service でも再起動後に自動復帰できる。
- SSH 鍵は次に存在する。
  - `~/.ssh/id_ed25519_control`
  - `~/.ssh/id_ed25519_worker1`
  - 必要なら worker ごとの追加鍵
    - 例: `~/.ssh/id_ed25519_optionghi003`
- 公開用サンプル設定は [../config/relay.example.toml](../config/relay.example.toml) にある。
- RabbitMQ transport を使う場合は `routing.transport = "rabbitmq"` と `[rabbitmq]` が設定済みである。

## インストール

```bash
sudo cp deploy/systemd/openclaw-relay.service /etc/systemd/system/openclaw-relay.service
sudo cp deploy/systemd/openclaw-relay.env.example /etc/default/openclaw-relay
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-relay.service
```

`/etc/default/openclaw-relay` の `A_GATEWAY_TOKEN` / `B_GATEWAY_TOKEN` は、
SSH tunnel 経由の remote token fetch を使う場合は **設定しない**。
placeholder を残したまま有効にすると、remote token fetch より優先されて誤動作する。

RabbitMQ transport を使う場合は、Relay 本体に加えて worker adapter も常駐化する。

`openclaw-relay.service` と worker adapter は、起動時に RabbitMQ topology を自動で ensure する。  
そのため `init-rabbitmq-topology` は systemd 起動の前提条件ではなく、事前確認や手動検査用である。

```bash
sudo cp deploy/systemd/openclaw-relay-rabbitmq-worker@.service /etc/systemd/system/openclaw-relay-rabbitmq-worker@.service
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-relay-rabbitmq-worker@def002.service
sudo systemctl enable --now openclaw-relay-rabbitmq-worker@ghi003.service
```

公開用 unit は `/opt/openclaw_relay` と `User=relay` を前提にしている。  
既存の main service が別パス・別ユーザーで動いている環境では、worker adapter unit も同じ値に合わせて書き換える。

## 確認

```bash
sudo systemctl status openclaw-relay.service --no-pager
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz
curl -fsS http://127.0.0.1:18080/metrics
```

RabbitMQ transport を使う場合:

```bash
sudo systemctl status openclaw-relay-rabbitmq-worker@def002.service --no-pager
sudo systemctl status openclaw-relay-rabbitmq-worker@ghi003.service --no-pager
```

## 更新反映

コードや設定を更新したら、次で再起動する。

```bash
sudo deploy/systemd/restart-openclaw-relay.sh --worker def002 --worker ghi003
```

Relay 本体だけなら:

```bash
sudo deploy/systemd/restart-openclaw-relay.sh
```

このスクリプトは次をまとめて実行する。

- `systemctl daemon-reload`
- Relay service restart
- 指定した worker adapter service restart
- `systemctl is-active`
- `healthz` / `readyz`

必要なら、従来どおり個別に叩いてもよい。

```bash
sudo systemctl restart openclaw-relay.service
sudo systemctl status openclaw-relay.service --no-pager
```

## ログ

```bash
journalctl -u openclaw-relay.service -n 100 --no-pager
tail -n 100 var/log/openclaw-relay/relay.log
tail -n 100 var/log/openclaw-relay/a2a-audit.jsonl
```

RabbitMQ transport を使う場合:

```bash
journalctl -u openclaw-relay-rabbitmq-worker@def002.service -n 100 --no-pager
journalctl -u openclaw-relay-rabbitmq-worker@ghi003.service -n 100 --no-pager
```

## 運用コマンド

```bash
PYTHONPATH=src python3 -m openclaw_relay --config config/relay.local.toml inspect --limit 20
```

```bash
PYTHONPATH=src python3 -m openclaw_relay --config config/relay.local.toml replay-deadletter --message-id 12
```

監視の詳細手順は [監視とアラート.md](監視とアラート.md) を参照。
UI の使い方は [運用ダッシュボード.md](運用ダッシュボード.md) を参照。
cleanup の手順は [retentionとcleanup.md](retentionとcleanup.md) を参照。
cleanup timer を使う場合は `openclaw-relay-cleanup.timer` を有効化する。

## IP 変更時

`ssh_host` は alias 参照なので、Relay 設定は変更しない。  
IP が変わったら `/etc/hosts` の対応行だけ更新し、その後 Relay を再起動する。

詳細手順は [IP変更時の対処.md](IP変更時の対処.md) を参照。

```bash
sudo systemctl restart openclaw-relay.service
```
