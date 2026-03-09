# Alertmanager導入

## 目的

Prometheus の alert rule を、Relay 自身の UI / metrics / log に通知として戻す。

今回の構成では、Alertmanager の通知先は外部メールや Slack ではなく、Relay の local webhook。

- webhook URL: `http://127.0.0.1:18080/api/alertmanager/webhook`
- Relay UI は active alert を上部 chip に表示する
- Relay metrics は active alert 数と最終 webhook 受信時刻を出す
- Relay log にも受信サマリを残す

## 配置済みファイル

- Alertmanager 設定: [../deploy/monitoring/alertmanager.yml](../deploy/monitoring/alertmanager.yml)
- Prometheus 側設定: [../deploy/monitoring/prometheus.yml](../deploy/monitoring/prometheus.yml)
- Prometheus merge 断片: [../deploy/monitoring/prometheus-openclaw-relay-job.yml](../deploy/monitoring/prometheus-openclaw-relay-job.yml)
- rule file: [../deploy/monitoring/openclaw-relay-alerts.yml](../deploy/monitoring/openclaw-relay-alerts.yml)

## 1. Alertmanager 本体の導入

このサーバに Alertmanager が未導入なら:

```bash
sudo apt-get update
sudo apt-get install -y prometheus-alertmanager
```

## 2. 設定配置

```bash
sudo cp deploy/monitoring/alertmanager.yml /etc/prometheus/alertmanager.yml
```

Prometheus 側は、既存の `/etc/prometheus/prometheus.yml` に `alerting` ブロックが必要。

フル上書きするなら:

```bash
sudo cp deploy/monitoring/prometheus.yml /etc/prometheus/prometheus.yml
```

既存設定に追記するなら、少なくとも次を入れる。

```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets:
            - 127.0.0.1:9093
```

## 3. 反映

```bash
sudo systemctl enable --now prometheus-alertmanager
sudo systemctl restart prometheus-alertmanager
sudo systemctl restart prometheus
sudo systemctl status prometheus-alertmanager --no-pager
sudo systemctl status prometheus --no-pager
```

## 4. 動作確認

### Alertmanager API

```bash
curl -fsS http://127.0.0.1:9093/api/v2/status | python3 -m json.tool | head -n 60
```

### Prometheus rules / targets

```bash
curl -fsS 'http://127.0.0.1:9090/api/v1/rules' | python3 -m json.tool | grep -E 'OpenClawRelay|openclaw-relay'
curl -fsS 'http://127.0.0.1:9090/api/v1/targets' | python3 -m json.tool | grep -E 'openclaw-relay|127.0.0.1:18080'
```

### Relay 側で webhook が入ることの確認

Alert 発火時に、Relay 側で次が見える。

```bash
curl -fsS http://127.0.0.1:18080/api/dashboard | python3 -m json.tool | grep -E 'alerts|critical|warning|alertname'
curl -fsS http://127.0.0.1:18080/metrics | grep -E 'openclaw_relay_alertmanager_'
tail -n 50 var/log/openclaw-relay/relay.log
```

## 5. 期待される Relay 側の変化

- UI 上部に `active alerts` の件数が出る
- active alert があれば warning / critical chip が並ぶ
- `/metrics` に次が出る

```text
openclaw_relay_alertmanager_active_alerts
openclaw_relay_alertmanager_active_alerts_by_severity
openclaw_relay_alertmanager_webhook_last_received_unixtime
```

## 補足

- この構成は「Relay 内で通知を完結させる」ためのもの
- 外部通知を足す場合は、Alertmanager の `receivers` に email / Slack / Telegram / webhook を追加すればよい
- まずは local webhook で動作確認し、その後に外部通知へ広げるのが安全
