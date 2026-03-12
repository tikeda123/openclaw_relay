# RabbitMQ移行設計

## 結論

現行の `watch_dir + SQLite + /v1/responses + session log 補助確認` の Relay v1 は、`1 control + 少数 worker` の試作には使えるが、provider-grade の土台としては不十分です。

worker 数が増えると、次がボトルネックになります。

- worker ごとの SSH tunnel / token / host 管理
- worker ごとの remote session scrape
- transport 成否と worker 完了を同じ timeout で扱う設計
- 本文や transcript に依存した配送確認

そのため、複数 worker と将来の運用を前提にするなら、RabbitMQ を broker とする v2 へ移行するのが妥当です。

## 現在の実装状況

repo には Phase 1 の土台が入っています。

- optional な `[rabbitmq]` 設定
- topology 名の導出
- `check-rabbitmq`
- `describe-rabbitmq-topology`
- `init-rabbitmq-topology`
- `publish-rabbitmq`
- `run-rabbitmq-worker-adapter`
- `run-rabbitmq-reply-consumer`

まだ入っていないもの:

- RabbitMQ publish を使う Relay 本体の本番 transport 切替
- DLX / retry / replay の broker 移管

## 何が現行設計の問題か

現行 v1 の問題は、RabbitMQ がないこと自体よりも、次の責務分離が崩れていることです。

- transport accepted
- worker processing
- final reply completed

これらを `/v1/responses` の同期呼び出しと timeout でまとめて扱うと、次が起きます。

- 実際には worker に届いているのに `FAILED_B`
- reply が遅いだけなのに duplicate resend
- long message や transient error で状態推定が崩れる

provider 側の原則は次です。

- routing と確認は metadata で扱う
- 本文は opaque payload として扱う
- 利用者に ID / TTL / timestamp を書かせない
- 利用者の誤操作があっても broker と service 側で堅牢性を保つ

## RabbitMQ で何が良くなるか

RabbitMQ 公式ドキュメントから見て、今回の課題に直接効く機能は次です。

- queue / exchange / routing key による transport 分離
- publisher confirms による publish 確認
- consumer manual ack による処理確認
- consumer prefetch による backpressure
- dead-letter exchange による再配送/隔離
- quorum queue による durable / replicated queue
- poison message handling と delivery-limit
- vhost / user / permission による分離

RabbitMQ 自体も、複数 queue を前提に設計されており、単一 queue は anti-pattern とされています。

## 公式仕様からの設計上の含意

### 1. 確認は publisher confirms と consumer ack で分ける

RabbitMQ では publisher confirms と consumer acknowledgements は別の責務です。

- publisher confirms
  - publisher -> broker の受理確認
- consumer acknowledgements
  - broker -> consumer の処理確認

この 2 つは直交しており、reply 完了待ちの代替ではありません。  
したがって v2 では、

- Relay publish 成功 = broker が受けた
- worker adapter ack = worker adapter が受けた
- worker reply publish 成功 = reply が broker に載った

を別イベントとして扱います。

### 2. 長時間タスクに Direct Reply-To は使わない

RabbitMQ 公式 docs では、Direct Reply-To は reply queue を省略できる一方で、明示 queue 方式にも利点があり、特に long-running tasks では明示 queue が有利だとされています。

本件はまさに long-running task なので、`amq.rabbitmq.reply-to` は使わず、明示 reply queue を使います。

### 3. quorum queue を基本にする

RabbitMQ 4.x では classic queue は非 replicated で、quorum queue は replicated queue の基本選択肢です。  
また quorum queue には poison message handling と delivery-limit があり、DLX と組み合わせて安全側に寄せやすいです。

v2 では critical path は quorum queue を基本にします。

### 4. DLX は queue policy で管理する

RabbitMQ 公式 docs では、`x-arguments` をアプリに埋めるより policy を優先することを強く勧めています。  
DLX も同様で、queue 宣言コードに hardcode するより operator policy で制御する方が provider-grade です。

### 5. prefetch は小さくする

consumer prefetch は unacked 数を制御するための基本手段です。  
今回のように long-running task がある場合、prefetch を無制限にすると worker adapter が簡単に詰まります。

v2 では worker adapter ごとに小さな prefetch を使います。初期値は `1` か `4` を推奨します。

### 6. queue を 1 本にまとめない

RabbitMQ 公式 docs でも、single queue は anti-pattern です。  
そのため v2 は 1 本の global queue ではなく、worker 単位の inbox queue を持ちます。

## 推奨 v2 アーキテクチャ

### 役割

- Human
  - Telegram で `OptionABC001` に依頼
- `OptionABC001`
  - オーケストレーター
  - worker 宛て依頼を Relay API に渡す
- Relay API
  - 最小入力 `to` と `body` を受ける
  - metadata を自動補完して RabbitMQ に publish
- RabbitMQ
  - transport の正本
- worker adapter
  - worker ごとに 1 プロセス
  - queue consume
  - OpenClaw worker に投入
  - 完了後に reply queue へ publish
- Reply injector
  - reply queue consume
  - `OptionABC001` の human-facing `agent:main:main` session へ戻す

### queue / exchange

#### exchanges

- `relay.dispatch.direct`
  - durable direct exchange
  - `OptionABC001 -> worker`
- `relay.reply.direct`
  - durable direct exchange
  - `worker -> OptionABC001`
- `relay.dead.direct`
  - dead-letter exchange
- `relay.events.topic`
  - 監視・監査イベント

#### queues

- `relay.worker.optiondef002.inbox`
  - quorum, durable
  - routing key: `OptionDEF002`
- `relay.control.optionabc001.reply`
  - quorum, durable
  - routing key: `OptionABC001`
- `relay.worker.optiondef002.dlq`
  - quorum, durable
- `relay.control.optionabc001.dlq`
  - quorum, durable

worker が増えたら `relay.worker.<name>.inbox` を増やします。  
single queue にはしません。

### message contract

利用者や AI が指定するのはこれだけです。

- `to`
- `body`

Relay API が自動で付けます。

- `message_id`
- `conversation_id`
- `from_gateway`
- `created_at`
- `correlation_id`
- `reply_to`
- `delivery_mode`

重要なのは、**本文は routing や retry 判定に使わない**ことです。

### accepted / completed の分離

v2 では状態を次で分けます。

- `ACCEPTED`
  - Relay API が broker publish confirm を受けた
- `DELIVERED_TO_ADAPTER`
  - worker adapter が consume し、自ローカルへ保存後 ack
- `IN_PROGRESS`
  - worker が処理中
- `REPLIED`
  - worker adapter が reply queue へ publish confirm 済み
- `INJECTED`
  - `OptionABC001` へ戻した
- `DEAD`
  - poison / operator judgement / permanent failure

**reply timeout は transport timeout と切り離します。**  
長時間 task は `IN_PROGRESS` のまま持てばよく、`FAILED_B` にしません。

## worker adapter の責務

worker adapter は queue consumer です。Relay 本体が remote session log を scrape し続ける設計は捨てます。

worker adapter の責務:

1. inbox queue から consume
2. metadata を見て idempotency を確認
3. ローカル durable spool に保存
4. OpenClaw worker に投入
5. 完了を観測したら reply queue に publish
6. publish confirm 後にローカル state を `replied`

これで Relay 本体は worker host の session transcript を transport 確認に使わなくて済みます。

## retry / dead-letter 方針

### transport retry

broker publish が confirm されないなら、publisher 側 retry。  
consumer 側が local spool へ保存できないなら ack せず、reconnect 後に再配送させます。

### business retry

OpenClaw worker が実行中のまま長いだけなら retry しません。  
失敗が確定した場合のみ、adapter が別イベントとして failure reply を publish します。

### dead-letter

DLX は policy ベースで設定します。critical path は quorum queue + DLX を使います。  
poison message は quorum queue の delivery-limit で隔離します。

## セキュリティ設計

### 基本

- default user は使わない
- vhost を環境ごとに分ける
- user を役割ごとに分ける
- TLS 必須
- least privilege

### 推奨 role

- `relay-publisher`
  - dispatch exchange へ publish のみ
- `worker-optiondef002-consumer`
  - 自分の inbox queue consume のみ
- `worker-optiondef002-replier`
  - reply exchange へ publish のみ
- `relay-reply-consumer`
  - reply queue consume のみ
- `ops-readonly`
  - management / metrics 読み取りのみ

### operator policy

operator policy で最低限これを固定します。

- queue type = quorum
- delivery-limit
- message TTL / queue length limit の上限
- DLX

アプリが勝手に危険な queue を宣言できないようにします。

## パフォーマンス設計

### 初期推奨

- worker ごとに 1 inbox queue
- adapter prefetch = 1 から開始
- persistent message
- durable exchange / durable queue
- publish confirm 使用

### 注意

- high throughput で 1 queue に集約しない
- backlog が多い worker は queue を分ける
- monitoring は queue depth / unacked / redelivery / confirm latency / consumer lag を見る

## 監視項目

Prometheus / management API では最低限これを監視します。

- queue ready messages
- queue unacked messages
- publish confirm latency
- redelivery count
- dead-letter rate
- consumer connection count
- consumer prefetch / outstanding deliveries
- worker ごとの in-progress 件数

## 現行 v1 から何を捨てるか

v2 で transport 正本から外すもの:

- `watch_dir` を transport queue とみなす考え方
- worker host の session transcript scrape を配送確認に使うこと
- `/v1/responses` timeout を reply 完了 timeout として使うこと
- `FAILED_B` に「未達」と「返答待ち」を混ぜること
- provider 側で本文一致検索をして状態推定すること

残してよいもの:

- UI
- metrics
- audit
- `OptionABC001` への最終 inject

## 段階移行案

### Phase 0: 仕様確定

- accepted / completed 分離
- queue 名、routing key、message contract 固定
- current Relay v1 は bug fix のみ

検証:

- status 遷移図を作り、`ACCEPTED -> DELIVERED_TO_ADAPTER -> IN_PROGRESS -> REPLIED -> INJECTED` の各遷移に trigger と owner が 1 つずつ割り当たっていること
- `FAILED_B` や `DEADLETTER_B` に「未達」と「返答待ち」が混在していないこと
- message contract に、利用者入力・provider 自動生成・broker metadata が分離されていること
- `to` と `body` だけで publish 可能な API 仕様になっていること

完了条件:

- 仕様書レビューで、「本文一致検索」や「reply timeout を transport timeout で代用する」設計が残っていない
- v1 と v2 の責務境界が README / 設計書で矛盾なく説明できる

### Phase 1: broker 導入

- RabbitMQ 導入
- vhost / user / TLS / policy / quorum queue 定義
- Relay 側に publish path を追加

検証:

- `relay.dispatch.direct` / `relay.reply.direct` / `relay.dead.direct` が durable に作成されること
- `relay.worker.optiondef002.inbox` と reply queue が quorum queue で作成されること
- publisher confirms を有効にした publish テストで confirm を取得できること
- wrong credential / wrong vhost / TLS mismatch の各失敗が明示的エラーになること
- operator policy により queue type / DLX / delivery-limit が期待値で適用されること

テスト:

- unit
  - config loader
  - publish payload builder
  - broker connection factory
- integration
  - disposable RabbitMQ コンテナを使った exchange / queue / binding 作成確認
  - publish confirm 成功 / 失敗確認
  - DLX policy 適用確認

完了条件:

- Relay から broker へ 1 message を durable publish できる
- broker 側で queue depth と confirm 成功を確認できる
- TLS / user / vhost 設定が least privilege で動く

### Phase 2: worker adapter

- `OptionDEF002` 用 adapter を実装
- queue consume -> local durable spool -> OpenClaw投入
- reply publish 実装

検証:

- adapter が inbox queue から consume し、ローカル spool 保存後にのみ ack すること
- OpenClaw worker 投入前に adapter プロセスが落ちた場合、message が再配送されること
- 同一 `message_id` が再配送されても idempotency により二重投入しないこと
- long-running task で `IN_PROGRESS` を維持し、duplicate resend が発生しないこと
- worker 完了後、reply queue に publish confirm を得ること

テスト:

- unit
  - idempotency store
  - local spool recovery
  - worker invocation wrapper
- integration
  - fake worker を使った consume -> process -> reply の往復
  - adapter crash before ack / after ack / before reply publish の故障注入
  - Japanese long message / multiline / markdown / large payload の通過確認

完了条件:

- 1 worker で `publish -> consume -> process -> reply publish` が end-to-end で通る
- timeout ではなく broker ack / state machine で進捗管理できる
- 同じ long message を複数回投入しない

### Phase 3: reply path 移行

- reply queue -> `OptionABC001` inject
- human-facing `agent:main:main` session へ戻す
- current session scrape は補助監視へ格下げ

検証:

- reply queue から受けた結果が `OptionABC001` の human-facing `agent:main:main` session に入ること
- 人間との会話から cross-node 通信事実を直接確認できること
- `OptionABC001` inject timeout 時も、重複 inject せず delivery confirmation だけで `INJECTED` へ進めること
- reply publish 済みで inject 未完了の状態から Relay 再起動後に復旧できること

テスト:

- unit
  - inject payload builder
  - human session key resolver
  - reply consumer state transition
- integration
  - fake `OptionABC001` endpoint を使った reply inject
  - inject failure / recovery / replay
  - Telegram session 分離確認

完了条件:

- worker reply が `OptionABC001` Relay session に確実に戻る
- human session 汚染が起きない
- `REPLIED -> INJECTED` の途中再起動から復旧できる

### Phase 4: v1 transport 廃止

- `watch_dir` transport 廃止
- worker session scrape に依存する retry 廃止
- `FAILED_B` / `FAILED_A` 状態整理

検証:

- outbox file 監視を止めても v2 transport が単独で成立すること
- session transcript scrape を無効化しても配送確認ロジックが成立すること
- old v1 message が混入しても v2 に影響を与えないこと
- `FAILED_B` が純粋な transport failure のみを意味すること

テスト:

- migration
  - v1 DB / spool が残った状態で v2 が起動すること
  - old artifact cleanup が安全に動くこと
- integration
  - broker only で full roundtrip
  - worker stop / broker restart / relay restart / adapter restart の故障注入

完了条件:

- v1 の session scrape と watch_dir transport を off にできる
- broker / adapter / injector のみで end-to-end が成立する
- provider 視点で配送確認根拠が metadata と broker state に限定される

## フェーズ横断の非機能テスト

各フェーズで最低限次を継続確認します。

- 信頼性
  - relay restart / broker restart / adapter restart 後に message が消えない
- 冪等性
  - 同一 `message_id` 再配送で二重実行しない
- 文字種
  - 日本語、長文、改行、Markdown、ASCII 以外を含んでも壊れない
- セキュリティ
  - 認証失敗、権限不足、wrong vhost、expired cert が安全に失敗する
- 可観測性
  - queue depth、unacked、dead-letter、publish confirm latency、consumer lag が取得できる
- 負荷
  - prefetch 1/4/8、message size、worker 数増加時の backlog を測定する

## フェーズゲート

実装を前に進める条件を固定します。

- Phase 1 -> 2
  - broker へ durable publish と confirm が通る
- Phase 2 -> 3
  - duplicate resend なしで 1 worker の long-running task が処理できる
- Phase 3 -> 4
  - Telegram session 汚染なしで reply を戻せる
- Phase 4 完了
  - v1 transport を off にしても、監視・dead-letter・replay を含めた日常運用が成立する

## この repo に対する判断

現状 repo は v1 としてはかなり進んでいますが、provider-grade の基盤としては v2 へ切り直すべきです。

優先順位は次です。

1. v1 の大きな bug fix は最小限
2. RabbitMQ v2 の設計確定
3. `OptionDEF002` 1 台で adapter PoC
4. その後に複数 worker へ展開

## 参考にした RabbitMQ 公式 docs

- Queues: https://www.rabbitmq.com/docs/queues
- Exchanges: https://www.rabbitmq.com/docs/exchanges
- Consumer Acknowledgements and Publisher Confirms: https://www.rabbitmq.com/docs/confirms
- Consumer Prefetch: https://www.rabbitmq.com/docs/consumer-prefetch
- Dead Letter Exchanges: https://www.rabbitmq.com/docs/dlx
- Time-To-Live and Expiration: https://www.rabbitmq.com/docs/ttl
- Quorum Queues: https://www.rabbitmq.com/docs/quorum-queues
- Authentication, Authorisation, Access Control: https://www.rabbitmq.com/docs/access-control
- Direct Reply-To: https://www.rabbitmq.com/docs/4.1/direct-reply-to
