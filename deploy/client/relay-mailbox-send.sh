#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  relay-mailbox-send.sh --to MAILBOX [--body TEXT | --body-file PATH] [--conversation-id ID] [--in-reply-to ID] [--notify-human]

Environment:
  RELAY_BASE_URL       Base URL of the Relay mailbox API, for example http://127.0.0.1:18080
  RELAY_MAILBOX_TOKEN  Bearer token for the caller mailbox
  RELAY_CLIENT_ENV     Optional path to a client env file. Default: ~/.config/openclaw-relay/client.env
EOF
}

load_default_env() {
  local env_file=${RELAY_CLIENT_ENV:-"$HOME/.config/openclaw-relay/client.env"}
  if [[ -f "$env_file" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$env_file"
    set +a
  fi
}

require_env() {
  local name=$1
  if [[ -z "${!name:-}" ]]; then
    printf '%s\n' "error: environment variable ${name} is required" >&2
    exit 1
  fi
}

to_value=""
body_value=""
body_file=""
conversation_id=""
in_reply_to=""
notify_human="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --to)
      to_value=${2:-}
      shift 2
      ;;
    --body)
      body_value=${2:-}
      shift 2
      ;;
    --body-file)
      body_file=${2:-}
      shift 2
      ;;
    --conversation-id)
      conversation_id=${2:-}
      shift 2
      ;;
    --in-reply-to)
      in_reply_to=${2:-}
      shift 2
      ;;
    --notify-human)
      notify_human="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf '%s\n' "error: unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ -z "$to_value" ]]; then
  printf '%s\n' "error: --to is required" >&2
  usage >&2
  exit 1
fi

if [[ -n "$body_value" && -n "$body_file" ]]; then
  printf '%s\n' "error: --body and --body-file are mutually exclusive" >&2
  exit 1
fi

if [[ -z "$body_value" && -z "$body_file" ]]; then
  printf '%s\n' "error: one of --body or --body-file is required" >&2
  exit 1
fi

load_default_env

require_env RELAY_BASE_URL
require_env RELAY_MAILBOX_TOKEN

if [[ -n "$body_file" ]]; then
  if [[ ! -f "$body_file" ]]; then
    printf '%s\n' "error: body file not found: $body_file" >&2
    exit 1
  fi
fi

payload=$(
  python3 - "$to_value" "$body_value" "$body_file" "$conversation_id" "$in_reply_to" "$notify_human" <<'PY'
import json
import sys
from pathlib import Path

to_value, body_value, body_file, conversation_id, in_reply_to, notify_human = sys.argv[1:]
if body_file:
    body_text = Path(body_file).read_text(encoding="utf-8")
else:
    body_text = body_value

payload = {
    "to": to_value,
    "body": body_text,
}
if conversation_id:
    payload["conversationId"] = conversation_id
if in_reply_to:
    payload["inReplyTo"] = in_reply_to
if notify_human == "true":
    payload["notifyHuman"] = True
print(json.dumps(payload, ensure_ascii=False))
PY
)

exec curl -fsS \
  -X PUT \
  -H "Authorization: Bearer ${RELAY_MAILBOX_TOKEN}" \
  -H 'Content-Type: application/json' \
  --data-binary "$payload" \
  "${RELAY_BASE_URL%/}/v1/messages"
