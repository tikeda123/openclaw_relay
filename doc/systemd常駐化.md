# systemd 常駐化

Relay の常駐は system service として行う。  
理由は、サーバ再起動後の自動復帰を user login に依存させないため。

## 前提

- Relay サーバの `/etc/hosts` に `optionabc001` と `optiondef002` が定義されている。
- systemd service は仮想環境内の Python を使う。
- SSH 鍵は次に存在する。
  - `~/.ssh/id_ed25519_control`
  - `~/.ssh/id_ed25519_worker1`
- 公開用サンプル設定は [../config/relay.example.toml](../config/relay.example.toml) にある。

## インストール

```bash
sudo cp deploy/systemd/openclaw-relay.service /etc/systemd/system/openclaw-relay.service
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-relay.service
```

## 確認

```bash
sudo systemctl status openclaw-relay.service --no-pager
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz
curl -fsS http://127.0.0.1:18080/metrics
```

## 更新反映

コードや設定を更新したら、次で再起動する。

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
