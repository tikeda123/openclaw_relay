# retention と cleanup

## 目的

Relay を長期運用したときに、`archive`, `deadletter`, `processing`, `replies` が増え続けないようにする。

## 方針

- 削除対象は Relay サーバ上のローカル artifact に限定する。
- `watch_dir` は cleanup 対象にしない。
  - source sync で再コピーされる可能性があるため。
- DB の `messages`, `seen_files`, `attempts`, `audit_index` は cleanup 対象にしない。
  - 重複抑止と運用履歴を壊さないため。
- `DONE` 済みメッセージの `processing` / `replies` は古くなったら削除できる。
- `archive` / `deadletter` は古くなったら削除できる。

## コマンド

dry-run:

```bash
PYTHONPATH=src python3 -m openclaw_relay \
  --config config/relay.local.toml \
  cleanup --older-than-days 7 --dry-run
```

実削除:

```bash
PYTHONPATH=src python3 -m openclaw_relay \
  --config config/relay.local.toml \
  cleanup --older-than-days 7
```

## 削除対象

- `archive_dir` の古いファイル
- `deadletter_dir` の古いファイル
- `DONE` 済みメッセージに紐づく古い `processing_path`
- `DONE` 済みメッセージに紐づく古い `reply_path`

## 削除しないもの

- `watch_dir` の `.json`
- `DEADLETTER_B` / `DEADLETTER_A` の `processing_path`
  - replay 用に残す
- SQLite state
- `a2a-audit.jsonl`

## 推奨運用

- まずは `--dry-run` で対象を確認する。
- 問題なければ手動 cleanup を行う。
- 運用が安定したら `systemd timer` で定期実行する。

## systemd timer

service / timer 定義は次に置いてある。

- [../deploy/systemd/openclaw-relay-cleanup.service](../deploy/systemd/openclaw-relay-cleanup.service)
- [../deploy/systemd/openclaw-relay-cleanup.timer](../deploy/systemd/openclaw-relay-cleanup.timer)

現在の設定:

- 毎日 `03:30`
- `RandomizedDelaySec=15m`
- 保持日数 `7日`
- `Persistent=true`
  - サーバ停止中に実行タイミングを逃しても、起動後に補完される

インストール:

```bash
sudo cp deploy/systemd/openclaw-relay-cleanup.service /etc/systemd/system/openclaw-relay-cleanup.service
sudo cp deploy/systemd/openclaw-relay-cleanup.timer /etc/systemd/system/openclaw-relay-cleanup.timer
sudo systemctl daemon-reload
sudo systemctl enable --now openclaw-relay-cleanup.timer
```

確認:

```bash
sudo systemctl status openclaw-relay-cleanup.timer --no-pager
systemctl list-timers --all | grep openclaw-relay-cleanup
```

手動実行:

```bash
sudo systemctl start openclaw-relay-cleanup.service
sudo systemctl status openclaw-relay-cleanup.service --no-pager
journalctl -u openclaw-relay-cleanup.service -n 50 --no-pager
```

停止:

```bash
sudo systemctl disable --now openclaw-relay-cleanup.timer
```
