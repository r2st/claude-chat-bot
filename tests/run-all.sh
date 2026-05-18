#!/usr/bin/env bash
#
# Run the full telechat e2e suite: install → platforms → features.
#
# Each layer is independent and self-skipping:
#   install   — CLI lifecycle in an isolated temp HOME (no tokens/network)
#   platforms — live Telegram/WhatsApp/Slack API checks (skips if unconfigured)
#   features  — runtime: persistence, rate limit, usage, Claude brain, health
#               (set TELECHAT_E2E_LIFECYCLE=1 to also test start/stop/restart)
#
# Usage:  bash tests/run-all.sh
# Exit:   0 only if every layer exits 0

set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
RC=0

for t in e2e-install.sh e2e-platforms.sh e2e-features.sh; do
  echo
  echo "═══════════════════════════════════════════════"
  echo "  Running: $t"
  echo "═══════════════════════════════════════════════"
  bash "$DIR/$t" || RC=1
done

echo
if [ "$RC" -eq 0 ]; then
  echo "  ✅ All e2e layers passed"
else
  echo "  ❌ One or more e2e layers failed"
fi
exit "$RC"
