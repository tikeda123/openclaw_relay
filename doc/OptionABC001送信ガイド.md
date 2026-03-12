# OptionABC001送信ガイド

`OptionABC001` にそのまま渡す恒久ルール文は [OptionABC001指示書.md](OptionABC001指示書.md) を参照。
caller 側への配布手順は [クライアント導入ガイド.md](クライアント導入ガイド.md) を参照。

## 目的

`OptionABC001` から worker への cross-node 通信を、OpenClaw 内部 subagent dispatch や outbox file ではなく、Relay mailbox API に統一する。

この文書の狙いは 2 つ。

- サービス利用者としての操作を実質 `PUT` だけに絞る
- `OptionABC001 -> worker -> OptionABC001` の全経路を Relay で監査・可視化する

## 原則

- 人間 -> `OptionABC001`
  - Telegram
- `OptionABC001` -> Relay
  - `PUT /v1/messages`
- Relay -> `OptionABC001`
  - mailbox injector が `GET /v1/messages`
- `OptionABC001` -> 人間
  - Telegram に整理して返す

OpenClaw 内部 subagent dispatch は cross-node 通信に使わない。

## 標準 API

### 1. 送信

Relay に送る最小形:

```bash
curl -fsS -X PUT http://127.0.0.1:18080/v1/messages \
  -H 'Authorization: Bearer '"$RELAY_MAILBOX_TOKEN_OPTIONABC001" \
  -H 'Content-Type: application/json' \
  -d '{
    "to": "OptionDEF002",
    "body": "Reply with the single word hello."
  }'
```

最小 request body:

```json
{
  "to": "OptionDEF002",
  "body": "最新の market regime を要約してください。"
}
```

補足:

- `Authorization: Bearer <mailbox token>` を必ず付ける
- `from` は token から Relay が決める
- `messageId`、`conversationId`、`createdAt`、`ttlSeconds` は Relay が自動補完する
- 人間への非同期報告が必要なときだけ `notifyHuman: true` を付ける
- 人間が「Telegramで報告して」「返信が来たら知らせて」と明示したときだけ `notifyHuman: true`
- それ以外は `notifyHuman` を付けない
- `fromGateway` / `toGateway` は互換扱いで、新規利用では使わない

### 2. 受信

返答受信は `OptionABC001` 自身が手動で行わず、常駐 mailbox injector に任せる。

重要:

- `GET /v1/messages` は破壊的取得
- 手動 `GET` や `relay-mailbox-receive.sh` は queue を先食いするので通常運用で使わない
- 返答確認は human-facing `agent:main:main` session、injector log、Relay audit を見る

## よく使うパターン

### 1. 単発 message

```bash
curl -fsS -X PUT http://127.0.0.1:18080/v1/messages \
  -H 'Authorization: Bearer '"$RELAY_MAILBOX_TOKEN_OPTIONABC001" \
  -H 'Content-Type: application/json' \
  -d '{
    "to": "OptionDEF002",
    "body": "最新の market regime を要約してください。"
  }'
```

### 2. 長文依頼

```bash
cat > request.json <<'EOF'
{
  "to": "OptionDEF002",
  "body": "EURUSD 戦略の改善案を 3 本だけ提案してください。各案について、仮説、実装手順、リスク、評価指標を簡潔に示してください。"
}
EOF

curl -fsS -X PUT http://127.0.0.1:18080/v1/messages \
  -H 'Authorization: Bearer '"$RELAY_MAILBOX_TOKEN_OPTIONABC001" \
  -H 'Content-Type: application/json' \
  --data-binary @request.json
```

### 3. 同じ会話を継続する

`conversationId` を固定する。

```bash
curl -fsS -X PUT http://127.0.0.1:18080/v1/messages \
  -H 'Authorization: Bearer '"$RELAY_MAILBOX_TOKEN_OPTIONABC001" \
  -H 'Content-Type: application/json' \
  -d '{
    "to": "OptionDEF002",
    "conversationId": "CONV-EURUSD-001",
    "body": "前回提案の Track 1 をもっと保守的に詰めてください。"
  }'
```

### 4. worker からの返答を受ける

worker からの返答は mailbox injector が自動で受け、human-facing `agent:main:main` session に注入する。injector の Telegram direct notify は `notifyHuman=true` の reply にだけ発火する。人が manual receive を実行しない。

### 5. 人間に非同期報告も欲しい

`notifyHuman` を明示する。

```bash
curl -fsS -X PUT http://127.0.0.1:18080/v1/messages \
  -H 'Authorization: Bearer '"$RELAY_MAILBOX_TOKEN_OPTIONABC001" \
  -H 'Content-Type: application/json' \
  -d '{
    "to": "OptionDEF002",
    "notifyHuman": true,
    "body": "受信後、必ず 1 行で返信してください。"
  }'
```

## OptionABC001 側の運用ルール

- worker へ依頼するときは Relay mailbox API を使う
- worker の返答も human-facing `agent:main:main` session に統一する
- Relay inbox で返ってきた内容は mailbox injector が `agent:main:main` に取り込む
- 返信の Telegram 通知は mailbox injector の固定フォーマット direct notify を正とする
- direct notify は `notifyHuman=true` を付けた依頼にだけ使う
- `notifyHuman=true` の発火条件は、人間からの明示依頼だけとする
- Relay inbox は pull-only なので、`OptionABC001` 側でも常駐 injector を動かす
- `relay-mailbox-receive.sh` や手動 `GET /v1/messages` は使わない
- outbox file や session transcript を配送確認の正本にしない
- 「届いたか」の判断は `messageId` / `conversationId` / human-facing session / Relay audit を基準にする

## AGENTS.md に書くべき方針

`OptionABC001` 側の AGENTS.md には、少なくとも次を入れる。

```text
Use Relay mailbox API for all cross-node communication.
Send requests with PUT /v1/messages using Authorization, to, and body.
Let the mailbox injector receive worker replies automatically; do not run relay-mailbox-receive.sh.
Do not use internal subagent dispatch, outbox files, or Telegram session history as the source of truth for cross-node delivery.
```

## 確認

Relay host で:

```bash
curl -fsS http://127.0.0.1:18080/api/dashboard | python3 -m json.tool | head -n 80
```

見るもの:

- `OptionABC001 -> OptionDEF002` の dispatch
- worker 返答
- mailbox queue の動き

### reply が届いたかの判断

- 自分の会話記憶だけで判定しない
- manual `GET /v1/messages` を呼ばない
- human-facing `agent:main:main` session と injector log を見る
- Telegram 側の通知確認は AI の会話返答ではなく injector の direct notify を正とする
- 必要なら `messageId` / `conversationId` を人間に返して追跡する

## トラブル時

### message が見えない

- Relay の `readyz` が `ok` か
- `PUT /v1/messages` が `202` を返したか
- injector log に該当 `messageId` があるか
- `api/dashboard` や audit に該当 `messageId` があるか

### 返答が Telegram に混ざる

- internal subagent dispatch や direct chat を使っていないか
- `OptionABC001` が Relay inbox ではなく Human Chat 文脈だけで判断していないか
- Relay が最新コードで再起動済みか
