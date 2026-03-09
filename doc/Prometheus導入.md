# Prometheus導入

## 前提

このリポジトリには Prometheus 本体は含めていない。  
このサーバに Prometheus が導入済みか、別途導入する前提で、Relay 監視を Prometheus に取り込むための設定を用意している。

配置済みファイル:

- フルサンプル設定: [../deploy/monitoring/prometheus.yml](../deploy/monitoring/prometheus.yml)
- 既存 config へ足す用の断片: [../deploy/monitoring/prometheus-openclaw-relay-job.yml](../deploy/monitoring/prometheus-openclaw-relay-job.yml)
- alert rule: [../deploy/monitoring/openclaw-relay-alerts.yml](../deploy/monitoring/openclaw-relay-alerts.yml)
- Alertmanager 導入: [Alertmanager導入.md](Alertmanager導入.md)

## 導入パターン

### 1. 既存の Prometheus がある場合

既存の `/etc/prometheus/prometheus.yml` に job を追記し、rule file を読み込ませる。

追加する内容:

- `alerting` に `127.0.0.1:9093`
- `scrape_configs` に `openclaw-relay` job
- `rule_files` に `/etc/prometheus/rules/openclaw-relay-alerts.yml`

使う断片:

- [../deploy/monitoring/prometheus-openclaw-relay-job.yml](../deploy/monitoring/prometheus-openclaw-relay-job.yml)

### 2. Prometheus をこの用途で新規に立てる場合

フルサンプルをそのまま使う。

- [../deploy/monitoring/prometheus.yml](../deploy/monitoring/prometheus.yml)

## 配置手順

以下は一般的な Linux の `/etc/prometheus` レイアウトを想定している。

`sudo systemctl restart prometheus` で `Unit prometheus.service not found` になる場合は、Prometheus 本体が未導入。
このサーバでも最初はその状態だった。

その場合は先に:

```bash
sudo apt-get update
sudo apt-get install -y prometheus
```

を実行し、その後にこの手順へ戻る。

```bash
sudo mkdir -p /etc/prometheus/rules
sudo cp deploy/monitoring/openclaw-relay-alerts.yml /etc/prometheus/rules/openclaw-relay-alerts.yml
```

### フルサンプルを使う場合

```bash
sudo cp deploy/monitoring/prometheus.yml /etc/prometheus/prometheus.yml
```

### 既存 config に追記する場合

次を参考に `/etc/prometheus/prometheus.yml` へ手で反映する。

```yaml
rule_files:
  - /etc/prometheus/rules/openclaw-relay-alerts.yml

alerting:
  alertmanagers:
    - static_configs:
        - targets:
            - 127.0.0.1:9093

scrape_configs:
  - job_name: openclaw-relay
    metrics_path: /metrics
    scheme: http
    static_configs:
      - targets:
          - 127.0.0.1:18080
        labels:
          service: openclaw-relay
          relay_node: relay-a
```

## 反映確認

`promtool` がある場合:

```bash
promtool check config /etc/prometheus/prometheus.yml
promtool check rules /etc/prometheus/rules/openclaw-relay-alerts.yml
```

Prometheus service を使っている場合:

```bash
sudo systemctl enable --now prometheus
sudo systemctl restart prometheus
sudo systemctl status prometheus --no-pager
```

## 動作確認

Relay 側:

```bash
curl -fsS http://127.0.0.1:18080/metrics | head -n 80
```

Prometheus 側:

- `Status -> Targets` で `openclaw-relay` が `UP`
- `Rules` で `OpenClawRelay*` 系 rule が読まれている

CLI で確認する場合:

```bash
curl -fsS 'http://127.0.0.1:9090/api/v1/targets' | python3 -m json.tool | head -n 80
```

## 最低限見るべき query

```promql
openclaw_relay_ready
openclaw_relay_source_sync_healthy
openclaw_relay_worker_deadletter_total
openclaw_relay_worker_attempt_total{result="FAILED"}
openclaw_relay_worker_latency_seconds{stage="dispatch",stat="latest"}
openclaw_relay_worker_latency_seconds{stage="inject",stat="latest"}
```

## 補足

- このサーバの現時点では Prometheus / promtool コマンドは未導入だった
- そのため、このリポジトリでは「投入する設定一式」までを整備している
- 実際の `/etc/prometheus` 反映と `prometheus.service` 再起動は root 権限で行う
