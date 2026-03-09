# IP変更時の対処

## 目的

OptionABC001 / OptionDEF002 / Relay サーバの IP アドレスが再起動や DHCP 更新で変わったときに、Relay を最短で復旧するための手順をまとめる。

## 現在の前提

- Relay の公開用サンプル設定は [../config/relay.example.toml](../config/relay.example.toml) にある。
- Relay は接続先を IP 直書きではなく、Relay サーバの `/etc/hosts` に定義した alias で参照する。
- 現在の alias 定義例は [../config/relay.hosts.example](../config/relay.hosts.example) にある。
  - `optionabc001`
  - `optiondef002`
  - `relay-node`

## 重要な整理

- `OptionABC001` または `OptionDEF002` の IP が変わった場合:
  - Relay サーバの `/etc/hosts` を更新する必要がある。
  - 更新後に `openclaw-relay.service` を再起動する。
- Relay サーバ自身の IP が変わった場合:
  - 現在の Relay プロセスは `127.0.0.1` のトンネルと outbound SSH を使うため、通常は Relay 設定変更は不要。
  - ただし、運用者の端末や他ノードで Relay サーバの IP を直接覚えているなら、そちらは別途更新する。
- `.local` 名や mDNS には依存しない。
  - 運用の正本は Relay サーバの `/etc/hosts` とする。

## OpenClawA / OpenClawB 側でやること

### OptionABC001 側

IP 変更が起きたとき、OptionABC001 側で最低限確認するものは次。

- 新しい LAN IP
- `Remote Login` が ON のままか
- OpenClaw gateway が動いているか
- outbox ディレクトリが残っているか

確認コマンド:

```bash
ipconfig getifaddr en0 || ipconfig getifaddr en1
nc -vz 127.0.0.1 22
openclaw status
ls -ld <control-workspace>/outbox/pending
```

補足:

- Relay は OptionABC001 へ SSH で入るので、`Remote Login` が OFF だと復旧しない。
- outbox の場所が変わっていなければ、Relay 設定変更は不要。

### OptionDEF002 側

IP 変更が起きたとき、OptionDEF002 側で最低限確認するものは次。

- 新しい LAN IP
- `Remote Login` が ON のままか
- OpenClaw gateway が動いているか
- `responses` endpoint が有効なままか

確認コマンド:

```bash
ipconfig getifaddr en0 || ipconfig getifaddr en1
nc -vz 127.0.0.1 22
openclaw status
python3 - <<'PY'
import json
from pathlib import Path
data = json.loads(Path.home().joinpath(".openclaw/openclaw.json").read_text())
print(data.get("gateway", {}).get("http", {}).get("endpoints", {}).get("responses", {}))
PY
```

補足:

- Relay は OptionDEF002 へ SSH トンネルを張るので、`Remote Login` が OFF だと復旧しない。
- `gateway.http.endpoints.responses.enabled = true` が落ちていたら再度有効化が必要。

## 変更対象ごとの対処

### 1. OptionABC001 の IP が変わったとき

OptionABC001 側で新しい IP を確認する。

```bash
ipconfig getifaddr en0 || ipconfig getifaddr en1
```

Relay サーバで `/etc/hosts` を更新する。

例:

```text
10.0.0.20 optionabc001
10.0.0.11 optiondef002
10.0.0.12 relay-node
```

確認:

```bash
getent hosts optionabc001
ssh -i ~/.ssh/id_ed25519_control controluser@optionabc001 'hostname'
```

再起動:

```bash
sudo systemctl restart openclaw-relay.service
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz
```

OptionABC001 側で最終確認:

```bash
openclaw status
ls -ld <control-workspace>/outbox/pending
```

### 2. OptionDEF002 の IP が変わったとき

OptionDEF002 側で新しい IP を確認する。

```bash
ipconfig getifaddr en0 || ipconfig getifaddr en1
```

Relay サーバで `/etc/hosts` を更新する。

例:

```text
10.0.0.10 optionabc001
10.0.0.21 optiondef002
10.0.0.12 relay-node
```

確認:

```bash
getent hosts optiondef002
ssh -i ~/.ssh/id_ed25519_worker1 workeruser@optiondef002 'hostname'
```

再起動:

```bash
sudo systemctl restart openclaw-relay.service
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz
```

OptionDEF002 側で最終確認:

```bash
openclaw status
python3 - <<'PY'
import json
from pathlib import Path
data = json.loads(Path.home().joinpath(".openclaw/openclaw.json").read_text())
print(data.get("gateway", {}).get("http", {}).get("endpoints", {}).get("responses", {}))
PY
```

### 3. Relay サーバ自身の IP が変わったとき

Relay サーバで新しい IP を確認する。

```bash
hostname -I
```

現在の構成では、Relay サーバ自身の IP は Relay の内部動作に必須ではない。  
そのため、通常は `openclaw-relay.service` の設定変更は不要。

ただし次は更新が必要になりうる。

- 運用者の手元端末の SSH 接続先メモ
- 他のサーバの `/etc/hosts`
- 監視やブックマークで Relay サーバ IP を固定している箇所

必要に応じて `relay-node` の行も更新する。

```text
10.0.0.10 optionabc001
10.0.0.11 optiondef002
10.0.0.22 relay-node
```

確認:

```bash
getent hosts relay-node
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz
```

## 標準復旧手順

IP 変更時は、基本的に次の順で戻す。

1. 変更されたホストの新 IP を確認する。
2. OpenClawA / OpenClawB 側で SSH と gateway の前提が残っているか確認する。
3. Relay サーバの `/etc/hosts` を更新する。
4. `getent hosts optionabc001 optiondef002 relay-node` で名前解決を確認する。
5. 必要なら SSH 到達を確認する。
6. `sudo systemctl restart openclaw-relay.service` を実行する。
7. `healthz` / `readyz` と `inspect` を確認する。

## 復旧確認コマンド

```bash
getent hosts optionabc001 optiondef002 relay-node
sudo systemctl status openclaw-relay.service --no-pager
curl -fsS http://127.0.0.1:18080/healthz
curl -fsS http://127.0.0.1:18080/readyz
PYTHONPATH=src python3 -m openclaw_relay \
  --config config/relay.local.toml inspect --limit 20
```

## 予防策

- 可能なら DHCP reservation で `OptionABC001` と `OptionDEF002` の IP を固定する。
- 固定化できない場合でも、Relay 設定ファイルには IP を書かず alias を維持する。
- alias の正本は Relay サーバの `/etc/hosts` とし、複数箇所に同じ IP を散らさない。
