# 記事ネタ: OpenClaw 間メッセージ連携基盤

## 目的

この記事ネタは、`OptionABC001` と複数 worker の間をつなぐ Relay 基盤を、外部向けの記事として整理するための素材置き場である。  
実装の説明だけでなく、設計の失敗、運用で詰まった点、どのように単純化したかまで含めて書けるようにする。

2026-03-12 時点の状態を前提にしている。

## 一言で言うと

- OpenClaw 同士を直接意識させず、Relay の mailbox API を中心に疎結合でつなぐ基盤
- 人間から見える会話は `OptionABC001` の human-facing session に統一
- 内部 transport は Relay mailbox API、RabbitMQ、worker adapter、injector に分離
- `notifyHuman=true` のときだけ Telegram に direct notify する

## 記事の主題候補

### 1. Telegram Bot 同士をそのままつなぐと壊れる

- 人間向け chat session と agent 間 transport を混同すると説明責任が壊れる
- 実際には通信しているのに「通信していない」と agent が答える問題が起きた
- これは AI の能力不足ではなく、状態の正本が分裂した設計問題だった

### 2. OpenClaw 間通信は mailbox API に寄せると整理できる

- `from / to / body` の message-only 契約に削る
- outbox file、手動 receive、SSH 手順を利用者から隠す
- 利用者から見えるのは「送る」「人間に返ってくる」だけにする

### 3. AI エージェント間通信にも運用設計が必要

- queue、session、通知、trace の正本を決めないと運用不能になる
- 返信をどの session に戻すかで UX が大きく変わる
- 非同期通知を AI 会話に任せると不安定なので、event として分離した

### 4. 小さな worker 追加でも運用の癖が出る

- `OptionGHI003` 追加時に、
  - mailbox-only 参加
  - self-send loop
  - gateway bind
  - SSH/tunnel
  - direct worker adapter
  を順に解決した

## 記事タイトル案

- OpenClaw 同士をつなぐ Relay を作った: Telegram 会話と agent 間通信を分離する
- AI エージェント同士の通信基盤を mailbox API に作り直した話
- 「通信しているのに通信していないと言う」問題を Relay 設計で直した
- OpenClaw 間メッセージ連携を outbox file から mailbox API に切り替えた
- AI エージェントの会話と transport を分けると何が起きるか
- 複数 OpenClaw worker を RabbitMQ + Relay でつなぐ最小構成

## 読者想定

- 複数 AI エージェントを連携させたい開発者
- Telegram や chat UI と agent backend の境界で悩んでいる人
- queue / mailbox / session の役割分担を知りたい人
- 小規模から始める multi-agent infra に興味がある人

## この記事で伝えるべき結論

### 結論 1

agent 間通信の正本は、人間向け chat thread ではなく transport 側に置くべき。

### 結論 2

ただし人間に見せる会話は 1 本に統一しないと、AI が通信事実を説明できなくなる。

### 結論 3

公開 API は極力小さくして、`PUT /v1/messages` と内部 worker 常駐で隠すのがよい。

### 結論 4

通知は AI の会話応答に任せず、必要時だけ direct notify を出す event 処理にするべき。

## 導入で書ける問題意識

- もともとは `OptionABC001` から worker に仕事を投げたかった
- Telegram 上では会話できるのに、cross-node 通信では状態が見えなかった
- session を分けた結果、
  - Relay では届いている
  - worker は処理している
  - でも人間に聞くと「届いていない」
  という矛盾が起きた
- これを直すために、OpenClaw 間通信を mailbox API 中心に作り替えた

## Before / After

### Before

- outbox file
- source sync
- hand-made JSON
- 手動 receive
- Relay 専用 session
- Human Chat と transport がズレる
- 人間が「まだ返っていないのか」を何度も確認する

### After

- mailbox API
- `from / to / body`
- Bearer token 認証
- worker 常駐 process
- `OptionABC001` の human-facing `agent:main:main` に統一
- `notifyHuman=true` のときだけ Telegram direct notify
- worker 追加は `workers.<name>` と常駐 process の追加で進める

## いまの最終構成

### 人間から見える流れ

1. 人間が `OptionABC001` に依頼する
2. `OptionABC001` が Relay に message を送る
3. Relay が対象 worker に配送する
4. worker が reply を Relay に返す
5. `OptionABC001` の human-facing session に reply が戻る
6. 必要なら Telegram に direct notify も出る

### 内部構成

- Relay 本体
  - mailbox API
  - RabbitMQ transport
  - health / ready / metrics / UI
- worker adapter
  - RabbitMQ inbox queue を受けて worker gateway に配送
- reply consumer / injector
  - reply を `OptionABC001` の human-facing session に戻す
- mailbox worker
  - worker 側で pull-only mailbox を常駐消費する

## 設計で重要だった判断

### 1. `from / to / body` を正本にした

- 利用者に transport metadata を意識させない
- `fromGateway / toGateway` は互換扱いに落とした

### 2. `ack` や `complete` を公開 API から消した

- 人間や agent 利用者が queue 内部状態を返す必要はない
- 返信は普通の message として返せばよい

### 3. worker ごとの session 分割を人間向け UI から隠した

- session を完全に 1 本にしたというより、
  - 人間に見せる session は 1 本
  - 内部 trace は Relay / audit / queue
  に分けた

### 4. `notifyHuman` を opt-in にした

- 全 worker reply を Telegram へ飛ばすとうるさい
- 人間が明示したときだけ direct notify する

## 実際に起きた問題と学び

### 問題 1. worker が「何も届いていない」と答える

原因:

- worker は Human Chat の記憶だけで答えていた
- Relay mailbox の事実を説明側の正本にしていなかった

学び:

- AI エージェントに transport の存在を認識させる必要がある
- 「届いたか？」は messageId / mailbox 状態で答えさせるべき

### 問題 2. mailbox が pull-only なので reply が止まる

原因:

- `OptionDEF002` や `OptionABC001` が mailbox を取りに行かなければ queue に残る

学び:

- human session の push 型 UX と mailbox queue の pull 型 UX は別物
- worker / injector は daemon 化が必須

### 問題 3. 手動 receive が誤判定を生む

原因:

- 常駐 daemon がすでに queue を消費したあと、手動 `receive` は空になる
- それを「未着」と誤解した

学び:

- 手動 receive は通常運用から外すべき

### 問題 4. 自分自身に message を送って loop した

発生例:

- `OptionGHI003` が `to=OptionGHI003` で self-send
- worker がそれに reply し続ける self-send loop が発生

対策:

- self-addressed message を破棄するガードを追加
- 新規 worker 手順書に self-send 禁止を明記

### 問題 5. direct worker adapter は token と到達性の両方が必要

- mailbox-only 参加は早い
- ただし正式 worker として health probe / adapter まで使うなら、
  - gateway 到達性
  - token 管理
  - SSH/tunnel
  が必要

## `OptionGHI003` 追加は良い題材

記事の後半では、`OptionGHI003` をどう追加したかを書くと具体性が出る。

### 流れ

1. mailbox token を作る
2. `client.env` を置く
3. `relay-mailbox-worker.py` を置く
4. LaunchAgent で常駐化する
5. `OptionABC001 -> OptionGHI003 -> OptionABC001` を mailbox-only で 1 往復通す
6. self-send loop を直す
7. gateway bind を loopback から LAN に直す
8. SSH を有効化する
9. Relay から token を取得できるようにする
10. `workers.ghi003` と `openclaw-relay-rabbitmq-worker@ghi003.service` を live に追加する

### この記事で映えるポイント

- mailbox-only から正式 worker への段階導入
- いきなり完成形を求めず、足場を作ってから health に入れた

## 記事の構成案

### 構成案 A: 失敗から入る

1. AI が「通信していない」と嘘をつく問題
2. 原因は session と transport の分裂
3. mailbox API への単純化
4. daemon 化と通知の分離
5. 新規 worker `OptionGHI003` を追加して確かめた
6. 学び

### 構成案 B: 実装から入る

1. やりたかったこと
2. 最終構成図
3. mailbox API の設計
4. RabbitMQ と worker adapter
5. human-facing session 統一
6. `notifyHuman` ルール
7. 運用で詰まったところ

## 本文に入れやすい図

### 図 1. 全体構成

- Human
- Telegram
- `OptionABC001`
- Relay
- RabbitMQ
- `OptionDEF002`
- `OptionGHI003`

### 図 2. 1 message の流れ

- `PUT /v1/messages`
- relay dispatch
- worker `/v1/responses`
- reply queue
- inject
- Telegram notify

### 図 3. Before / After

- Before: outbox file, session split, manual receive
- After: mailbox API, daemon, opt-in notify

## 本文に入れやすいコード断片候補

### 最小送信

```bash
relay-mailbox-send.sh \
  --to OptionGHI003 \
  --notify-human \
  --body "PING / 受信後に PONG 1行で返してください。"
```

### worker 追加の config 断片

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
local_port = 31902
remote_host = "127.0.0.1"
remote_port = 18789
```

## 書くと強い論点

### 1. 「AI が分かっていない」のではなく「基盤が分からせていない」

- エージェントが通信事実を説明できるかは prompt だけでは決まらない
- transport 設計の問題である

### 2. 人間向け会話と agent transport は分けるべきだが、見え方は統一すべき

- 実装の分離
- UX の統一
- trace の分離

### 3. シンプル API は強い

- `PUT /v1/messages`
- 認証は Bearer token
- message は `from / to / body`

### 4. daemon と通知は運用の要

- queue は pull-only なら必ず daemon がいる
- notify も明示条件が必要

## 公開時にぼかすべき情報

- 実 token 値
- 実 IP
- 実 username
- 実 chat id
- 実 host 名
- internal directory path の一部

必要なら次だけを anonymize する。

- `OptionABC001`, `OptionDEF002`, `OptionGHI003`
- `10.0.1.x`
- `tikeda`

## 公開時に残してよい情報

- mailbox API を中心にした設計
- session 分離で起きた失敗
- `notifyHuman` の opt-in
- self-send loop の再発防止
- worker 追加の段階導入
- SSH tunnel と RabbitMQ adapter の役割分担

## 記事を書くときに参照する文書

- [../README.md](../README.md)
- [アーキテクチャ概要.md](アーキテクチャ概要.md)
- [現状と今後.md](現状と今後.md)
- [RelayサービスAPI仕様.md](RelayサービスAPI仕様.md)
- [新規worker追加手順.md](新規worker追加手順.md)
- [クライアント導入ガイド.md](クライアント導入ガイド.md)
- [workerエージェント指示書.md](workerエージェント指示書.md)
- [シンプルメッセージ移行.md](シンプルメッセージ移行.md)

## 1 段落要約の下書き

OpenClaw 同士を Telegram の会話文脈のままつなごうとすると、実際には通信しているのに「通信していない」と agent が答える。そこで、agent 間 transport を Relay mailbox API に寄せ、人間から見える会話は `OptionABC001` の human-facing session に統一し、内部では RabbitMQ、worker adapter、injector に分離した。結果として、利用者には単純な message 送信に見せながら、worker の追加や通知条件の制御ができる基盤になった。

## 記事の締めに使える一文

- AI エージェント同士をつなぐときは、モデルより先に transport の正本を決めるべき
- 会話 UX を壊す原因は、だいたい session ではなく状態の正本が複数あること
- Multi-agent の最小実装は、賢い orchestration より先に雑音の少ない message 契約から始めるのがよい
