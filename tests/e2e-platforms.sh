#!/usr/bin/env bash
#
# End-to-end platform connectivity test for telechat.
#
# Validates the live integration boundary for each configured platform by
# calling its REAL API with the credentials in your .env:
#
#   Telegram   getMe                 → bot token valid, bot reachable
#   WhatsApp   getStateInstance      → Green API instance authorized
#              getSettings           → WhatsApp number linked
#   Slack      auth.test (bot)       → bot token valid
#              apps.connections.open → app-level (Socket Mode) token valid
#
# Credentials are read from the data home (~/.telechat/.env by default, or
# $TELECHAT_HOME/.env). A platform with no credentials is SKIPPED, not failed,
# so this is safe to run in CI. Secrets are never printed.
#
# Usage:   bash tests/e2e-platforms.sh
# Exit:    0 = all configured platforms passed (or all skipped)
#          1 = a configured platform failed validation

set -u

DATA_HOME="${TELECHAT_HOME:-$HOME/.telechat}"
ENV_FILE="$DATA_HOME/.env"

PASS=0
FAIL=0
SKIP=0

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; PASS=$((PASS + 1)); }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; FAIL=$((FAIL + 1)); }
skip() { printf "  \033[33m–\033[0m %s (skipped)\n" "$1"; SKIP=$((SKIP + 1)); }

# Read a key from the .env without exporting the whole file
envget() {
  [ -f "$ENV_FILE" ] || return 0
  grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '\r'
}

# jq-free JSON field check: field_true <json> <"key":value-fragment>
json_has() { printf '%s' "$1" | grep -qF -- "$2"; }

echo
echo "telechat e2e platform connectivity test"
echo "  env: $ENV_FILE"
if [ ! -f "$ENV_FILE" ]; then
  echo
  echo "  No .env found — nothing to test. Run 'telechat init' first."
  echo "  (treating as all-skipped)"
  exit 0
fi
echo

# ── Telegram ─────────────────────────────────────────────────────────────────
echo "[Telegram]"
TG_TOKEN="$(envget TELEGRAM_BOT_TOKEN)"
if [ -z "$TG_TOKEN" ]; then
  skip "Telegram not configured"
else
  resp="$(curl -s --max-time 15 "https://api.telegram.org/bot${TG_TOKEN}/getMe")"
  if json_has "$resp" '"ok":true'; then
    uname="$(printf '%s' "$resp" | sed -n 's/.*"username":"\([^"]*\)".*/\1/p')"
    ok "bot token valid (@${uname:-unknown})"
  else
    err="$(printf '%s' "$resp" | sed -n 's/.*"description":"\([^"]*\)".*/\1/p')"
    bad "getMe failed: ${err:-no/invalid response}"
  fi

  # Connectivity: getUpdates must not 409/401 (long-poll disabled, timeout 0)
  upd="$(curl -s --max-time 15 "https://api.telegram.org/bot${TG_TOKEN}/getUpdates?timeout=0&limit=1")"
  if json_has "$upd" '"ok":true'; then
    ok "getUpdates reachable (no 401/409 conflict)"
  else
    err="$(printf '%s' "$upd" | sed -n 's/.*"description":"\([^"]*\)".*/\1/p')"
    # 409 means another poller is live — that's the running bot, still OK
    if printf '%s' "$err" | grep -qi "conflict"; then
      ok "getUpdates: bot already polling (running instance) — OK"
    else
      bad "getUpdates failed: ${err:-unknown}"
    fi
  fi
fi

# ── WhatsApp (Green API) ─────────────────────────────────────────────────────
echo "[WhatsApp]"
WA_ID="$(envget GREEN_API_INSTANCE_ID)"
WA_TOKEN="$(envget GREEN_API_TOKEN)"
WA_BASE="$(envget GREEN_API_BASE_URL)"
WA_BASE="${WA_BASE:-https://api.green-api.com}"
if [ -z "$WA_ID" ] || [ -z "$WA_TOKEN" ]; then
  skip "WhatsApp not configured"
else
  state="$(curl -s --max-time 15 "${WA_BASE}/waInstance${WA_ID}/getStateInstance/${WA_TOKEN}")"
  if json_has "$state" '"stateInstance":"authorized"'; then
    ok "instance authorized"
  elif json_has "$state" '"stateInstance"'; then
    st="$(printf '%s' "$state" | sed -n 's/.*"stateInstance":"\([^"]*\)".*/\1/p')"
    bad "instance not authorized (state: ${st}) — rescan QR in Green API console"
  else
    bad "getStateInstance failed: ${state:-no response}"
  fi

  settings="$(curl -s --max-time 15 "${WA_BASE}/waInstance${WA_ID}/getSettings/${WA_TOKEN}")"
  if json_has "$settings" '"wid"'; then
    num="$(printf '%s' "$settings" | sed -n 's/.*"wid":"\([0-9]*\)@.*/\1/p')"
    ok "WhatsApp number linked (+${num:-unknown})"
  else
    bad "getSettings: no linked number (wid missing)"
  fi
fi

# ── Slack ────────────────────────────────────────────────────────────────────
echo "[Slack]"
SL_BOT="$(envget SLACK_BOT_TOKEN)"
SL_APP="$(envget SLACK_APP_TOKEN)"
if [ -z "$SL_BOT" ] && [ -z "$SL_APP" ]; then
  skip "Slack not configured"
else
  if [ -n "$SL_BOT" ]; then
    auth="$(curl -s --max-time 15 -H "Authorization: Bearer ${SL_BOT}" \
            https://slack.com/api/auth.test)"
    if json_has "$auth" '"ok":true'; then
      team="$(printf '%s' "$auth" | sed -n 's/.*"team":"\([^"]*\)".*/\1/p')"
      ok "bot token valid (team: ${team:-unknown})"
    else
      e="$(printf '%s' "$auth" | sed -n 's/.*"error":"\([^"]*\)".*/\1/p')"
      bad "auth.test failed: ${e:-unknown}"
    fi
  else
    skip "SLACK_BOT_TOKEN not set"
  fi

  if [ -n "$SL_APP" ]; then
    conn="$(curl -s --max-time 15 -X POST \
            -H "Authorization: Bearer ${SL_APP}" \
            https://slack.com/api/apps.connections.open)"
    if json_has "$conn" '"ok":true' && json_has "$conn" '"url":"wss'; then
      ok "app-level token valid (Socket Mode WebSocket negotiable)"
    else
      e="$(printf '%s' "$conn" | sed -n 's/.*"error":"\([^"]*\)".*/\1/p')"
      bad "apps.connections.open failed: ${e:-unknown} (need connections:write)"
    fi
  else
    skip "SLACK_APP_TOKEN not set"
  fi
fi

# ── Summary ──────────────────────────────────────────────────────────────────
echo
echo "────────────────────────────────────────────"
printf "  Passed: %d   Failed: %d   Skipped: %d\n" "$PASS" "$FAIL" "$SKIP"
echo "────────────────────────────────────────────"
[ "$FAIL" -eq 0 ]
