# OpenClaw Relay 開発手順

## 1. 前提

- このリポジトリは Relay 実装用であり、Relay はこのサーバ上で動かす。
- OptionABC001 と OptionDEF002 はこのサーバとは別筐体にあり、同一ローカルネットワーク上で接続されている。
- OptionABC001 と OptionDEF002 の `/v1/responses` は LAN 内の private ingress のみで到達可能にし、公開インターネットには出さない。
- OptionDEF002 は read-only worker、OptionABC001 は orchestrator とする。
- v1 では `/tools/invoke` や HTTP 経由の `sessions_send` は使わず、Relay から OptionABC001 / OptionDEF002 の `/v1/responses` を呼ぶ。

## 2. 重要な設計差分

元仕様は「Relay を OptionABC001 側ホストに置き、OptionABC001 の `outbox/pending/*.json` をローカル監視する」前提だった。  
今回は Relay が第三ホストにいるため、最初に OptionABC001 の outbox を Relay がどう受け取るかを固定する必要がある。

v1 の推奨は次のどちらか。

1. A ホストの `outbox` を Relay サーバから read-only で参照する。
2. A ホスト側の OS 機能で `outbox` を Relay サーバへ同期し、Relay は同期先を監視する。

v1 では OptionABC001 のエージェント自体にネットワーク権限を持たせない。  
そのため、outbox 搬送は A の OpenClaw プロセスではなく、OS/インフラ層で解決する。

## 3. 採用トポロジ

```text
Human
  -> OptionABC001 (orchestrator, host A)
  -> OptionABC001 updates task ledger
  -> OptionABC001 writes outbox JSON
  -> outbox transport over LAN
  -> Relay (this server)
  -> POST OptionDEF002 /v1/responses
  -> OptionDEF002 (worker, host B)
  -> Relay receives OptionDEF002 response
  -> POST OptionABC001 /v1/responses
  -> OptionABC001 main session
```

## 4. フェーズ分割

### Phase 0: 接続契約とインフラ確定

目的:
OptionABC001 / OptionDEF002 / Relay が LAN 上でどうつながるかを先に固定し、後工程の不確定要素を消す。

実施項目:
- OptionABC001 と OptionDEF002 の固定 IP または名前解決方法を決める。
- Relay サーバでは `/etc/hosts` に stable alias を定義し、Relay 設定は IP ではなく alias を参照する。
  - 例: `optionabc001`, `optiondef002`, `relay-node`
- OptionABC001 / OptionDEF002 の `/v1/responses` を Relay サーバからのみ到達可能にする。
- OptionABC001 outbox の搬送方式を決める。
  - 第一候補: Relay サーバから OptionABC001 の outbox を read-only mount
  - 第二候補: OptionABC001 ホストから Relay サーバへ outbox を同期
- OptionABC001 / OptionDEF002 の Gateway token を分離管理する。
- Relay 用 state/log ディレクトリ配置を決める。

成果物:
- ネットワーク接続表
- 使用ポート表
- secret 配置方針
- outbox 搬送方式の決定
- IP 変更時の運用手順

完了条件:
- Relay サーバから OptionABC001 / OptionDEF002 の疎通確認ができる。
- Relay サーバから OptionABC001 の outbox を安定して読める。
- IP 変更時に `/etc/hosts` 更新だけで復旧できる。

### Phase 1: OpenClaw API 実機確認

目的:
`/v1/responses` の実際の request/response 形を固定し、`ResponseExtractor` の仕様を決める。

実施項目:
- OptionABC001 に対して最小の `/v1/responses` リクエストを送る。
- OptionDEF002 に対して最小の `/v1/responses` リクエストを送る。
- `x-openclaw-agent-id` と `x-openclaw-session-key` の挙動を確認する。
- OptionABC001 の `main` セッションへの再注入が意図通りに見えるか確認する。
- 実レスポンスを fixture として保存する。

成果物:
- request/response の fixture
- `ResponseExtractor` の抽出ルール
- セッション再注入の確認メモ

完了条件:
- OptionDEF002 の返答から最終テキストを安定抽出できる。
- OptionABC001 の同一セッションに返答を戻せる。

### Phase 2: Relay プロジェクト雛形

目的:
最小の実装土台を作る。

実施項目:
- Python 3.12 ベースのプロジェクト構成を作る。
- 設定モデルを定義する。
- SQLite state store を用意する。
- `relay run` の CLI を作る。
- `healthz` と `readyz` の最小エンドポイントを作る。

成果物:
- 初期ディレクトリ構成
- 設定ファイルサンプル
- DB 初期化処理
- 起動可能な空 Relay

完了条件:
- Relay が設定を読んで起動できる。
- state dir/log dir を正しく初期化できる。

### Phase 3: A outbox 受信

目的:
OptionABC001 が書いた envelope を Relay が安全に取り込めるようにする。

実施項目:
- `outbox` 監視処理を実装する。
- `.tmp` 無視、`.json` のみ受理を実装する。
- schema 検証を実装する。
- TTL 検証を実装する。
- `idempotencyKey` の予約と重複抑止を実装する。
- invalid/expired/duplicate の振り分けを実装する。

成果物:
- Watcher
- Validator
- State store の messages/attempts テーブル
- deadletter/archive 処理

完了条件:
- 正常な envelope を 1 秒程度で検出できる。
- 同一 `idempotencyKey` を二重に処理しない。

### Phase 4: A -> B 配送

目的:
Relay が envelope を OptionDEF002 へ配送し、応答を durable に保持できるようにする。

実施項目:
- OptionDEF002 への HTTP client を実装する。
- timeout/retry/backoff を実装する。
- raw response の保存を実装する。
- `ResponseExtractor` を実装する。
- `DISPATCHING_TO_B`、`B_REPLIED` までの状態遷移を実装する。

成果物:
- Dispatcher
- Response extractor
- B 応答保存領域

完了条件:
- 正常系で OptionDEF002 の返答本文を保存できる。
- OptionDEF002 停止時に retry 後 deadletter へ落とせる。

### Phase 5: B -> A 再注入

目的:
OptionDEF002 の返答を OptionABC001 の `main` セッションへ戻す。

実施項目:
- OptionABC001 への injector を実装する。
- OptionABC001 注入前に reply body を durable 保存する。
- OptionABC001 注入失敗時は OptionDEF002 再送をせず、OptionABC001 注入だけ再試行する。
- notice 注入方針を実装する。

成果物:
- Injector
- 注入済みフラグ管理
- OptionABC001 失敗時の retry 処理

完了条件:
- 正常系で OptionABC001 の同一セッションへ返答が戻る。
- OptionABC001 注入失敗時に reply body を失わない。

### Phase 6: 監査・復旧・運用

目的:
Relay を常駐運用できる品質まで上げる。

実施項目:
- append-only JSONL 監査ログを実装する。
- secret masking を実装する。
- 再起動時の復旧処理を実装する。
- `relay inspect`、`relay replay-deadletter` などの運用コマンドを実装する。
- systemd unit を用意する。
- メトリクスを実装する。

成果物:
- `a2a-audit.jsonl`
- 復旧ロジック
- 運用手順書
- systemd サービス定義

完了条件:
- `B_REPLIED` や `INJECTING_TO_A` の途中状態から再開できる。
- token がログに出ない。

### Phase 7: 実機受け入れ試験

目的:
LAN 上の 3 台構成で v1 を成立させる。

実施項目:
- OptionABC001 実機で outbox 作成から Relay 受信まで確認する。
- OptionDEF002 実機で応答生成から OptionABC001 再注入まで確認する。
- OptionABC001 / OptionDEF002 停止、ネットワーク断、TTL 切れ、重複投入を試験する。
- ログ、deadletter、archive の動作を確認する。

成果物:
- 受け入れ試験結果
- 既知制約一覧
- 初期運用チェックリスト

完了条件:
- 仕様書の受け入れ基準を満たす。
- 3 台構成での運用手順が通る。

## 5. 実装順の優先度

最初に着手すべき順序は次の通り。

1. Phase 0 で outbox 搬送方式を決める。
2. Phase 1 で `/v1/responses` の実レスポンスを固定する。
3. Phase 2 から Phase 5 までで往復 MVP を成立させる。
4. Phase 6 で復旧と監査を固める。
5. Phase 7 で 3 台構成の実機検証を行う。

今回の構成では、最大の先行リスクは `ResponseExtractor` ではなく、`OptionABC001 の outbox を第三ホストの Relay がどう受け取るか` である。  
そのため、開発開始前に Phase 0 を必須ゲートとして扱う。

## 6. v1 の判断

- outbox 搬送方式はまず単純なものを選ぶ。
- Relay は単一プロセスで始める。
- watcher は polling で始め、必要なら後で event-driven に変える。
- OptionDEF002 の返答は v1 では opaque text として扱う。
- 人間承認ロジックの主責務は A に持たせ、Relay は監査と防御線に徹する。

## 7. 直近の着手項目

直近で実施する作業は以下。

1. OptionABC001 / OptionDEF002 / Relay 間の到達性確認
2. OptionABC001 outbox 搬送方式の決定
3. `/v1/responses` の fixture 採取
4. Relay の雛形作成
