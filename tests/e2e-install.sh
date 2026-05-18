#!/usr/bin/env bash
#
# End-to-end installation test for telechat.
#
# Runs the full install/lifecycle surface in an ISOLATED home directory so it
# never touches the real ~/.telechat. Requires no real tokens and no network
# (the bot is never actually started — only the CLI orchestration and the
# Python entry point's pre-flight/guidance paths are exercised).
#
# Usage:  bash tests/e2e-install.sh
# Exit:   0 = all passed, 1 = a check failed

set -u

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CLI="$REPO_ROOT/npm/bin/telechat.js"

# Isolated environment — override HOME so ~/.telechat resolves to a temp dir.
TEST_HOME="$(mktemp -d)"
export HOME="$TEST_HOME"
export TELECHAT_HOME="$TEST_HOME/.telechat"
DATA_HOME="$TEST_HOME/.telechat"

PASS=0
FAIL=0

cleanup() { rm -rf "$TEST_HOME"; }
trap cleanup EXIT

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; PASS=$((PASS + 1)); }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; FAIL=$((FAIL + 1)); }

# assert_contains <description> <expected substring> <actual output>
assert_contains() {
  if printf '%s' "$3" | grep -qF -- "$2"; then ok "$1"; else
    bad "$1"
    printf "      expected to contain: %s\n" "$2"
    printf "      got: %s\n" "$(printf '%s' "$3" | head -3 | tr '\n' '|')"
  fi
}

assert_file() {
  if [ -f "$2" ]; then ok "$1"; else bad "$1 (missing: $2)"; fi
}

assert_no_file() {
  if [ ! -f "$2" ]; then ok "$1"; else bad "$1 (should not exist: $2)"; fi
}

run() { node "$CLI" "$@" 2>&1; }

echo
echo "telechat e2e installation test"
echo "  HOME=$TEST_HOME"
echo

# ── 1. Basics ────────────────────────────────────────────────────────────────
echo "[1] CLI basics"
out="$(run --version)"
assert_contains "--version prints version" "telechat " "$out"

out="$(run --help)"
assert_contains "--help lists start"  "telechat [start]" "$out"
assert_contains "--help lists init"   "telechat init"    "$out"
assert_contains "--help shows data home note" ".telechat/" "$out"

out="$(run bogus-command)"
assert_contains "unknown command rejected" "Unknown command" "$out"

# ── 2. Status / workdir before any setup ─────────────────────────────────────
echo "[2] Pre-setup status"
out="$(run status)"
assert_contains "status shows data home" ".telechat" "$out"
assert_contains "status shows not running" "not running" "$out"

out="$(run workdir)"
assert_contains "workdir shows data home" "Data home" "$out"

# ── 3. start with no .env → guidance, not a crash ────────────────────────────
echo "[3] start with no .env"
out="$(run start)"
rc=$?
assert_contains "no .env shows setup guidance" "No .env configuration found" "$out"
assert_contains "guidance mentions telechat init" "telechat init" "$out"
if [ $rc -ne 0 ]; then ok "start exits non-zero when unconfigured"; else bad "start should exit non-zero when unconfigured"; fi

# ── 4. start with .env but no platform tokens ────────────────────────────────
echo "[4] start with empty .env"
mkdir -p "$DATA_HOME"
printf 'BOT_MODE=telegram\nCLAUDE_MODE=cli\n' > "$DATA_HOME/.env"
out="$(run start)"
assert_contains "empty-token .env detected" "no platform credentials found" "$out"

# ── 5. env display masks secrets ─────────────────────────────────────────────
echo "[5] env display"
cat > "$DATA_HOME/.env" <<'EOF'
BOT_MODE=telegram
TELEGRAM_BOT_TOKEN=123456789:AAExampleTokenValueForTestingABCDEFGHIJ
CLAUDE_MODE=cli
EOF
out="$(run env)"
assert_contains "env shows BOT_MODE" "BOT_MODE=telegram" "$out"
if printf '%s' "$out" | grep -q "AAExampleTokenValueForTesting"; then
  bad "env must mask the token"
else
  ok "env masks the token"
fi

# ── 6. workdir set/get ───────────────────────────────────────────────────────
echo "[6] workdir set/get"
WD="$TEST_HOME/claude-space"
out="$(run workdir "$WD")"
assert_contains "workdir set echoes path" "$WD" "$out"
if [ -d "$WD" ]; then ok "workdir directory created"; else bad "workdir directory not created: $WD"; fi
out="$(run workdir)"
assert_contains "workdir get shows configured dir" "$WD" "$out"
assert_contains "config.json persisted claudeWorkdir" "claude-space" "$(cat "$DATA_HOME/config.json")"

# ── 7. clean removes .env ────────────────────────────────────────────────────
echo "[7] clean"
out="$(printf 'y\n' | run clean)"
assert_contains "clean confirms deletion" ".env deleted" "$out"
assert_no_file "clean removed .env" "$DATA_HOME/.env"

out="$(run clean)"
assert_contains "clean with no .env is graceful" "nothing to clean" "$out"

# ── 8. Python entry point: clean guidance, no traceback ──────────────────────
echo "[8] Python entry point"
PY="$(command -v python3 || command -v python)"
if [ -n "$PY" ] && "$PY" -c "import sys;sys.path.insert(0,'$REPO_ROOT');import telechat_pkg" 2>/dev/null; then
  out="$(cd "$TEST_HOME" && PYTHONPATH="$REPO_ROOT" "$PY" -m telechat_pkg.main 2>&1)"
  if printf '%s' "$out" | grep -q "Traceback"; then
    bad "python entry must not print a traceback when unconfigured"
    printf '%s\n' "$out" | tail -3 | sed 's/^/      /'
  else
    ok "python entry: no traceback when unconfigured"
  fi
  assert_contains "python entry shows setup guidance" "not configured yet" "$out"

  # _find_env_file must resolve the data home, not the package dir
  res="$(cd "$TEST_HOME" && PYTHONPATH="$REPO_ROOT" "$PY" -c "
import telechat_pkg.main as m
m._resolve_workdir()
print(m._find_env_file())
" 2>&1)"
  assert_contains "_find_env_file uses data home" ".telechat/.env" "$res"
else
  echo "  (skipped — telechat_pkg not importable with $PY)"
fi

# ── 9. npm pack produces a usable tarball ────────────────────────────────────
echo "[9] npm pack"
PACK_DIR="$(mktemp -d)"
( cd "$REPO_ROOT/npm" && npm pack --pack-destination "$PACK_DIR" >/dev/null 2>&1 )
TARBALL="$(ls "$PACK_DIR"/telechat-*.tgz 2>/dev/null | head -1)"
if [ -n "$TARBALL" ]; then
  ok "npm pack created tarball"
  tar tzf "$TARBALL" | grep -q "package/bin/telechat.js" \
    && ok "tarball contains bin/telechat.js" \
    || bad "tarball missing bin/telechat.js"
else
  bad "npm pack did not produce a tarball"
fi
rm -rf "$PACK_DIR"

# ── Summary ──────────────────────────────────────────────────────────────────
echo
echo "────────────────────────────────────"
printf "  Passed: %d   Failed: %d\n" "$PASS" "$FAIL"
echo "────────────────────────────────────"
[ "$FAIL" -eq 0 ]
