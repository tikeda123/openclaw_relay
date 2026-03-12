# 新規worker追加手順

## 目的

新しい worker ノードを Relay mailbox API に参加させるための最短手順をまとめる。  
ここでは新しい worker 名を `OptionGHI003` とする。

前提:

- `OptionGHI003` には OpenClaw がインストール済み
- `OptionGHI003` では Telegram で人間とやり取りできる
- `OptionGHI003` の local OpenClaw gateway (`/v1/responses`) が使える
- Relay は mailbox API と Bearer token 認証で動いている

2026-03-12 時点の確認済み値:

- `OptionGHI003` の active NIC は `en1`
- `OptionGHI003` の local IP は `10.0.1.80`
- `en0` は inactive
- `10.0.1.80:22` と `10.0.1.80:18789` は Relay host から到達可能
- `OptionGHI003` は現在、mailbox worker 常駐と SSH tunnel の両方で参加できる

重要:

- cross-node の正規経路は Relay mailbox API
- `OptionGHI003` 側で手動 `GET /v1/messages` はしない
- `OptionGHI003` 側に必要なのは mailbox worker 常駐 process
- SSH tunnel や outbox file を新規 worker 追加の前提にしない
- ただし現行 Relay は `workers.<name>.base_url` を readiness / health probe する
- そのため live config へ正式追加するには、Relay host から `OptionGHI003` の gateway へ到達できる必要がある
- 到達方法は次のどちらか:
  - `OptionGHI003` の gateway を Relay host から見える private IP / port で公開する
  - SSH を有効化して tunnel を張る

## 必要な値

追加前に次を決める。

- mailbox 名: `OptionGHI003`
- Relay config の worker key: `ghi003`
- Relay token env 名: `RELAY_MAILBOX_TOKEN_OPTIONGHI003`
- `OptionGHI003` から見た Relay URL
  - 例: `http://10.0.1.69:18080`
- `OptionGHI003` の local OpenClaw gateway URL
  - 第一候補: `http://10.0.1.80:18789`
  - 最終的には `~/.openclaw/openclaw.json` の `gateway.port` を正とする

## 1. Relay 側設定

現時点の推奨は 2 段階です。

- 第1段階: mailbox-only で参加させる
  - `mailbox_auth.tokens.OptionGHI003` と env token だけ追加する
  - この段階では `workers.ghi003` は live config に入れない
- 第2段階: Relay host から gateway 到達性ができた後に正式 worker 登録する
  - `workers.ghi003` を live config に追加する
  - dashboard / health probe / 将来の transport 拡張を有効化する

注意:

- `workers.ghi003` を live config に入れた瞬間から、その worker は readiness / health check の対象になる
- `OptionGHI003` のように gateway 到達性と SSH が両方できた時点で正式 worker 登録へ進める
- gateway か SSH のどちらかが未整備な段階では mailbox-only に止める

### 1-1. まず mailbox token だけ追加する

```toml
[mailbox_auth.tokens]
OptionABC001 = "RELAY_MAILBOX_TOKEN_OPTIONABC001"
OptionDEF002 = "RELAY_MAILBOX_TOKEN_OPTIONDEF002"
OptionGHI003 = "RELAY_MAILBOX_TOKEN_OPTIONGHI003"
```

Relay host の env / secrets に実 token を追加する。

```bash
RELAY_MAILBOX_TOKEN_OPTIONGHI003=<actual-token>
```

この段階では `OptionABC001 -> Relay -> OptionGHI003 -> Relay -> OptionABC001` の mailbox 通信だけは動かせる。

### 1-2. 後で worker entry を追加する

gateway 到達性か SSH を作った後で、live config に worker entry を追加する。

```toml
[workers.ghi003]
display_name = "OptionGHI003"
base_url = "http://127.0.0.1:31902"
agent_id = "main"
default_session_key = "agent:main:main"
token_env = "GHI003_GATEWAY_TOKEN"
timeout_seconds = 30.0

[workers.ghi003.tunnel]
ssh_host = "10.0.1.80"
ssh_user = "tikeda"
ssh_key_path = "/home/tikeda/.ssh/id_ed25519_optionghi003"
ssh_connect_timeout_seconds = 10
strict_host_key_checking = true
local_port = 31902
remote_host = "127.0.0.1"
remote_port = 18789
token_config_path = "/Users/tikeda/.openclaw/openclaw.json"
```

補足:

- `workers.<name>` は dashboard、worker 一覧、将来の transport 拡張で使う
- `base_url` と `token_env` は worker の local OpenClaw gateway を表す
- tunnel を付ける場合、Relay 本体が local port を開き、worker adapter はその port を使う
- gateway 到達性ができるまでは live config に入れない方が安全

反映:

```bash
systemctl --user restart openclaw-relay.service openclaw-relay-rabbitmq-worker@b.service
curl -fsS http://127.0.0.1:18080/readyz
```

## 2. OptionGHI003 側配布

現時点の注意:

- `OptionGHI003` には SSH できるので、この Relay ホストからも配布可能
- ただし OpenClaw セッション上で本人に作業させる方が手戻りは少ない

### env

```bash
mkdir -p ~/.config/openclaw-relay
cat > ~/.config/openclaw-relay/client.env <<'EOF'
RELAY_BASE_URL=http://10.0.1.69:18080
RELAY_MAILBOX_TOKEN=<OptionGHI003 mailbox token>
EOF
```

### script

```bash
mkdir -p ~/.local/bin
cp deploy/client/relay-mailbox-send.sh ~/.local/bin/
cp deploy/client/relay-mailbox-worker.py ~/.local/bin/
chmod +x ~/.local/bin/relay-mailbox-send.sh ~/.local/bin/relay-mailbox-worker.py
```

## 3. OptionGHI003 側 worker 常駐化

単発確認:

```bash
relay-mailbox-worker.py --once --verbose
```

macOS LaunchAgent:

```bash
mkdir -p ~/Library/LaunchAgents ~/Library/Logs
sed "s#__HOME__#$HOME#g" \
  deploy/client/com.openclaw.relay-mailbox-worker.plist.template \
  > ~/Library/LaunchAgents/com.openclaw.relay-mailbox-worker.plist
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.relay-mailbox-worker.plist 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.openclaw.relay-mailbox-worker.plist
launchctl kickstart -k "gui/$(id -u)/com.openclaw.relay-mailbox-worker"
launchctl print "gui/$(id -u)/com.openclaw.relay-mailbox-worker" | head -n 40
tail -f ~/Library/Logs/openclaw-relay-mailbox-worker.log
```

## 4. OptionGHI003 へ渡す指示

- [workerエージェント指示書.md](workerエージェント指示書.md) をそのまま渡す
- `worker エージェント = OptionGHI003` と読み替える

要点:

- Relay mailbox message を正規入力にする
- Human Chat の記憶だけで未受信判定しない
- direct chat を cross-node 正規経路にしない

## 5. 疎通試験

### 最小往復

`OptionABC001` から:

```bash
relay-mailbox-send.sh \
  --to OptionGHI003 \
  --body "PING OptionGHI003 / 受信後に PONG 1行で返信してください。"
```

重要:

- 疎通試験の開始点は `OptionABC001` にする
- `OptionGHI003` が自分自身 (`to=OptionGHI003`) に test message を送ってはいけない
- self-send すると worker reply loop の原因になる

### 人間通知込み

```bash
relay-mailbox-send.sh \
  --to OptionGHI003 \
  --notify-human \
  --body "PING OptionGHI003 / 受信後に PONG 1行で返信してください。"
```

確認点:

- Relay DB / audit に `OptionABC001 -> OptionGHI003`
- `OptionGHI003` worker log に `processed mailbox message=...`
- Relay DB / audit に `OptionGHI003 -> OptionABC001`
- `OptionABC001` の `agent:main:main` に reply
- `--notify-human` を付けた場合だけ Telegram direct notify

## 6. 完了条件

次を満たしたら導入完了とする。

- `OptionGHI003` 宛て message が `delivered`
- `OptionGHI003` reply も `delivered`
- `OptionABC001` が human-facing session で reply を確認できる
- `notifyHuman=true` のときだけ Telegram 通知が出る

## 7. よくある詰まりどころ

- `OptionGHI003` 側で `client.env` の token が違う
- mailbox worker が常駐しておらず queue に溜まる
- local OpenClaw gateway token / port が `openclaw.json` と一致していない
- Relay 側に `mailbox_auth.tokens.OptionGHI003` を追加していない
- `OptionABC001` が `OptionGHI003` ではなく別 mailbox 名へ送っている
- `OptionGHI003` 側の NIC は `en1` なのに、`en0` の想定で IP を見てしまっている

## 8. OptionGHI003 追加時の最小チェックリスト

- Relay config に `mailbox_auth.tokens.OptionGHI003` を追加した
- Relay env に `RELAY_MAILBOX_TOKEN_OPTIONGHI003` を追加した
- `OptionGHI003` に `client.env` を置いた
- `OptionGHI003` に `relay-mailbox-worker.py` を置いた
- `OptionGHI003` の mailbox worker を常駐化した
- `OptionABC001 -> OptionGHI003 -> OptionABC001` を 1 往復確認した
- gateway 到達性か SSH を作った後で `workers.ghi003` を追加した
- `openclaw-relay-rabbitmq-worker@ghi003.service` を起動した
