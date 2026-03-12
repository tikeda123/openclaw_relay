# RelayサービスAPI仕様

## 目的

`OptionABC001` と `OptionDEF002` を含む各サービス利用者は、通信相手のホスト、SSH、session、RabbitMQ topology を意識しない。  
各サービスは Relay だけを相手にし、Relay が背後で message box を管理する。

この仕様の公開面は次の 2 操作だけに絞る。

- message を送る
- 自分宛ての message を受け取る

`ack`、`complete`、再試行指示、broker 操作は公開 API に含めない。

## 基本モデル

- 各サービスは自分専用の mailbox を持つ
- mailbox は Relay が提供する
- 送信時、Relay は受信側 mailbox に message を queue する
- 受信時、Relay は自分の mailbox から最古 message を 1 件返す
- 返した message は mailbox queue から削除する
- 返信は別の message として送る

見え方としては mailbox API だが、queue backend は公開しない。

- 公開 API
  - Relay mailbox API
- 内部実装
  - queue backend
  - Relay state
  - audit log
  - retry / deadletter

RabbitMQ を使う場合も、Relay の内部実装であり、サービス利用者は意識しない。

## サービス境界

- `OptionABC001` は Relay に送る
- `OptionDEF002` も Relay に送る
- `OptionABC001` は Relay から受ける
- `OptionDEF002` も Relay から受ける
- `OptionABC001` と `OptionDEF002` は直接通信しない

利用者から見える通信相手は常に Relay である。

## 公開 API

### 1. 送信 API

- `PUT /v1/messages`

役割:

- 自分から相手へ message を送る
- Relay は受理後、相手の inbox queue に追加する

request:

```json
{
  "to": "OptionDEF002",
  "body": "現在の稼働状況を1行で返答してください。",
  "conversationId": "conv-20260311-001",
  "inReplyTo": null,
  "notifyHuman": false
}
```

最小必須:

- `to`
- `body`

補足:

- `Authorization: Bearer <mailbox token>` を必須とする
- `from` は認証情報から Relay が補完するのが原則
- body に `from` を入れても Relay は認証済み mailbox で上書きする
- `conversationId` は会話継続時のみ指定する
- 返信時は `inReplyTo` に元 message の `messageId` を入れてよい
- `notifyHuman=true` を付けた場合だけ、OptionABC001 の mailbox injector は fixed-format Telegram direct notify を送る
- `notifyHuman` を省略した場合は `false`

response:

```json
{
  "messageId": "msg-20260311-0001",
  "conversationId": "conv-20260311-001",
  "from": "OptionABC001",
  "to": "OptionDEF002",
  "notifyHuman": false,
  "queuedAt": "2026-03-11T13:40:00Z",
  "status": "queued"
}
```

### 2. 受信 API

- `GET /v1/messages?for=<mailbox>`

役割:

- 自分宛て inbox queue の先頭を 1 件取り出す
- 返した message は queue から削除する

認証:

- `Authorization: Bearer <mailbox token>` を必須とする
- `for=` は省略可
- `for=` を指定した場合でも、認証済み mailbox と一致しなければ `403 Forbidden`

response 例:

```json
{
  "messageId": "msg-20260311-0001",
  "conversationId": "conv-20260311-001",
  "from": "OptionABC001",
  "to": "OptionDEF002",
  "queuedAt": "2026-03-11T13:40:00Z",
  "body": "現在の稼働状況を1行で返答してください。"
}
```

queue が空なら:

- `204 No Content`

## FIFO と削除ルール

- inbox queue は宛先ごとに独立する
- 取り出し順は FIFO とする
- 順序基準は Relay の受理時刻 `queuedAt`
- `GET /v1/messages?for=<mailbox>` は破壊的取得とする
- 返却成功時点で、その message は inbox queue から削除される

重要:

- 公開 API 上は `ack` と `complete` を持たない
- そのため配送保証は `at-most-once` である
- 受信後に利用者側が処理失敗しても、同じ message は自動再取得されない

この制約は、利用者向け API を単純に保つための意図的な設計である。

## 返信ルール

返信は新しい message として送る。  
専用の `reply` API は設けない。

例:

1. `OptionABC001` が `OptionDEF002` 宛てに送る
2. `OptionDEF002` が inbox から取得する
3. `OptionDEF002` が `OptionABC001` 宛てに新しい message を送る

reply request 例:

```json
{
  "to": "OptionABC001",
  "from": "OptionDEF002",
  "body": "稼働中です。応答正常です。",
  "conversationId": "conv-20260311-001",
  "inReplyTo": "msg-20260311-0001"
}
```

## RabbitMQ の位置づけ

この仕様では、RabbitMQ は message box の背後で queue 処理を行う内部基盤の候補とする。

役割:

- Relay の outbox 受理後、内部 queue に積む
- 宛先 mailbox ごとの FIFO を支える
- Relay プロセス間の非同期配送を支える
- retry / deadletter の内部基盤になる

ただし外部仕様には出さない。

サービス利用者が知る必要があるのは次だけである。

- 送ると queue される
- 受けると FIFO で 1 件返る
- 返ったら inbox queue から消える

## 現在の実装

現在の v1 実装では、公開 mailbox API は transport mode に応じて backend を切り替える。

- `routing.transport = "responses"`
  - mailbox queue は Relay の SQLite state を使う
- `routing.transport = "rabbitmq"`
  - mailbox queue は RabbitMQ を使う
  - SQLite state には監査用の message history と status を残す

重要なのは公開契約であり、backend が SQLite でも RabbitMQ でも API は変えない。

## Relay 内部で持つもの

公開 API に見せないが、Relay 内部では次を持ってよい。

- RabbitMQ exchange / queue / routing key
- delivery retry
- deadletter
- immutable audit log
- message history
- metrics

特に `GET` 後に queue から消えた message も、監査用コピーは Relay 内部に保持してよい。  
削除対象は「公開 inbox queue 上の未受信 entry」であり、監査記録全体ではない。

## 非目標

この v1 仕様では次を公開しない。

- `ack`
- `complete`
- visibility timeout
- lease / lock 延長
- consumer group
- push webhook
- SSE / WebSocket 通知
- 利用者向け RabbitMQ 操作

必要になっても、まずは Relay 内部で吸収し、公開 API を増やさない方針を優先する。

## 運用上の意味

この仕様にすると、サービス利用者は次だけ考えればよい。

- どこへ送るか
- 自分宛てを取り出すか
- 返信を返すか

考えなくてよいもの:

- 相手ホスト
- SSH
- session transcript
- worker adapter
- RabbitMQ topology
- `ack` / `complete`
- Relay 内部状態遷移
