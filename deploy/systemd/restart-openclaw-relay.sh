#!/usr/bin/env bash
set -euo pipefail

relay_service="openclaw-relay.service"
health_url="http://127.0.0.1:18080/healthz"
ready_url="http://127.0.0.1:18080/readyz"
wait_seconds=20
reload_daemon=1
show_status=1
declare -a worker_instances=()

usage() {
  cat <<'EOF'
Usage:
  restart-openclaw-relay.sh [options]

Options:
  --worker <instance>       Restart openclaw-relay-rabbitmq-worker@<instance>.service too.
                            Repeatable. Example: --worker def002 --worker xyz003
  --relay-service <name>    Relay service unit name. Default: openclaw-relay.service
  --health-url <url>        healthz URL. Default: http://127.0.0.1:18080/healthz
  --ready-url <url>         readyz URL. Default: http://127.0.0.1:18080/readyz
  --wait-seconds <seconds>  Max wait for health/ready. Default: 20
  --no-daemon-reload        Skip systemctl daemon-reload
  --no-status               Skip final systemctl status output
  -h, --help                Show this help

Notes:
  - Run this script as root or via sudo.
  - The worker unit template is fixed to:
      openclaw-relay-rabbitmq-worker@<instance>.service
EOF
}

require_arg() {
  local option="$1"
  local value="${2:-}"
  if [[ -z "$value" ]]; then
    printf 'missing value for %s\n' "$option" >&2
    exit 2
  fi
}

require_command() {
  local command_name="$1"
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'required command not found: %s\n' "$command_name" >&2
    exit 1
  fi
}

wait_for_url() {
  local label="$1"
  local url="$2"
  local deadline=$((SECONDS + wait_seconds))

  while true; do
    if curl -fsS --max-time 2 "$url" >/dev/null; then
      printf '%s ok: %s\n' "$label" "$url"
      return 0
    fi
    if (( SECONDS >= deadline )); then
      printf '%s failed after %ss: %s\n' "$label" "$wait_seconds" "$url" >&2
      return 1
    fi
    sleep 1
  done
}

service_name_for_worker() {
  local instance="$1"
  printf 'openclaw-relay-rabbitmq-worker@%s.service' "$instance"
}

print_status() {
  local service_name="$1"
  systemctl --no-pager --full --lines=20 status "$service_name"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --worker)
      require_arg "$1" "${2:-}"
      worker_instances+=("$2")
      shift 2
      ;;
    --relay-service)
      require_arg "$1" "${2:-}"
      relay_service="$2"
      shift 2
      ;;
    --health-url)
      require_arg "$1" "${2:-}"
      health_url="$2"
      shift 2
      ;;
    --ready-url)
      require_arg "$1" "${2:-}"
      ready_url="$2"
      shift 2
      ;;
    --wait-seconds)
      require_arg "$1" "${2:-}"
      wait_seconds="$2"
      shift 2
      ;;
    --no-daemon-reload)
      reload_daemon=0
      shift
      ;;
    --no-status)
      show_status=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

require_command systemctl
require_command curl

if ! [[ "$wait_seconds" =~ ^[0-9]+$ ]] || (( wait_seconds < 1 )); then
  printf 'invalid --wait-seconds: %s\n' "$wait_seconds" >&2
  exit 2
fi

if (( reload_daemon )); then
  printf 'systemctl daemon-reload\n'
  systemctl daemon-reload
fi

printf 'restarting %s\n' "$relay_service"
systemctl restart "$relay_service"

declare -a services=("$relay_service")
for instance in "${worker_instances[@]}"; do
  worker_service="$(service_name_for_worker "$instance")"
  printf 'restarting %s\n' "$worker_service"
  systemctl restart "$worker_service"
  services+=("$worker_service")
done

for service_name in "${services[@]}"; do
  if ! systemctl is-active --quiet "$service_name"; then
    print_status "$service_name" || true
    printf 'service is not active: %s\n' "$service_name" >&2
    exit 1
  fi
  printf 'service active: %s\n' "$service_name"
done

wait_for_url "healthz" "$health_url"
wait_for_url "readyz" "$ready_url"

if (( show_status )); then
  for service_name in "${services[@]}"; do
    print_status "$service_name"
  done
fi

printf 'restart and health checks completed\n'
