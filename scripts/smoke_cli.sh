#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RDC_PATH=""
CTX="cli-smoke-$(date +%Y%m%d%H%M%S)"
SKIP_RDC=0
STEP_TIMEOUT="${RDX_SMOKE_TIMEOUT:-120}"
OPEN_TIMEOUT="${RDX_SMOKE_OPEN_TIMEOUT:-600}"
LOG_FILE=""

usage() {
  cat <<'EOF'
Usage: bash scripts/smoke_cli.sh [options]

Options:
  --tools-root <path>  rdx-tools root. Defaults to this script's parent directory.
  --rdc <path>         .rdc capture used for daemon-backed smoke.
  --context <id>       daemon context id. Defaults to cli-smoke-<timestamp>.
  --skip-rdc           run only doctor/tools/search/negative MCP checks.
  --timeout <seconds>  default timeout for daemon-backed CLI commands.
  --open-timeout <s>   timeout for capture open.
  -h, --help           show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tools-root)
      [[ $# -ge 2 ]] || { echo "[smoke] ERROR: --tools-root needs a value" >&2; exit 2; }
      TOOLS_ROOT="$2"
      shift 2
      ;;
    --rdc)
      [[ $# -ge 2 ]] || { echo "[smoke] ERROR: --rdc needs a value" >&2; exit 2; }
      RDC_PATH="$2"
      shift 2
      ;;
    --context)
      [[ $# -ge 2 ]] || { echo "[smoke] ERROR: --context needs a value" >&2; exit 2; }
      CTX="$2"
      shift 2
      ;;
    --skip-rdc)
      SKIP_RDC=1
      shift
      ;;
    --timeout)
      [[ $# -ge 2 ]] || { echo "[smoke] ERROR: --timeout needs a value" >&2; exit 2; }
      STEP_TIMEOUT="$2"
      shift 2
      ;;
    --open-timeout)
      [[ $# -ge 2 ]] || { echo "[smoke] ERROR: --open-timeout needs a value" >&2; exit 2; }
      OPEN_TIMEOUT="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "[smoke] ERROR: unknown argument: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

TOOLS_ROOT="$(cd "$TOOLS_ROOT" && pwd)"
RDX="$TOOLS_ROOT/bin/rdx"
LOG_FILE="$TOOLS_ROOT/intermediate/logs/smoke_cli.log"
STATE_FILE="$TOOLS_ROOT/intermediate/runtime/rdx_cli/daemon_state_${CTX}.json"
FIXTURE_ROOT="$TOOLS_ROOT/tests/fixtures"
mkdir -p "$(dirname "$LOG_FILE")"
: > "$LOG_FILE"

if [[ ! -f "$RDX" ]]; then
  echo "[smoke] ERROR: missing bash launcher: $RDX" | tee -a "$LOG_FILE"
  exit 2
fi

if [[ "$SKIP_RDC" -eq 0 && -z "$RDC_PATH" && -d "$FIXTURE_ROOT" ]]; then
  while IFS= read -r candidate; do
    RDC_PATH="$candidate"
    break
  done < <(find "$FIXTURE_ROOT" -type f -name '*.rdc' | sort)
  if [[ -n "$RDC_PATH" ]]; then
    echo "[smoke] first-party fixture: $RDC_PATH" | tee -a "$LOG_FILE"
  fi
fi

if [[ "$SKIP_RDC" -eq 0 && -z "$RDC_PATH" ]]; then
  echo "[smoke] ERROR: pass --rdc <path>, add a first-party tests/fixtures/*.rdc, or use --skip-rdc for entry-only smoke" | tee -a "$LOG_FILE"
  exit 2
fi

if [[ "$SKIP_RDC" -eq 0 && ! -f "$RDC_PATH" ]]; then
  echo "[smoke] ERROR: .rdc fixture not found: $RDC_PATH" | tee -a "$LOG_FILE"
  exit 2
fi

print_command() {
  printf '[smoke] command:'
  for arg in "$@"; do
    printf ' %q' "$arg"
  done
  printf '\n'
}

run_raw() {
  local timeout_seconds="$1"
  shift
  if command -v timeout >/dev/null 2>&1; then
    timeout "$timeout_seconds" "$@" 2>&1 | tee -a "$LOG_FILE"
    return "${PIPESTATUS[0]}"
  fi
  echo "[smoke] WARN: timeout command is not available; running without shell timeout" | tee -a "$LOG_FILE"
  "$@" 2>&1 | tee -a "$LOG_FILE"
  return "${PIPESTATUS[0]}"
}

print_context_state() {
  echo "[smoke] daemon status for context: $CTX" | tee -a "$LOG_FILE"
  "$RDX" --daemon-context "$CTX" daemon status 2>&1 | tee -a "$LOG_FILE" || true
  if [[ -f "$STATE_FILE" ]]; then
    echo "[smoke] state file summary: $STATE_FILE" | tee -a "$LOG_FILE"
    grep -E '"(session_id|capture_file_id|capture_path|active_event_id|recovery_status)"' "$STATE_FILE" 2>/dev/null | tee -a "$LOG_FILE" || true
  else
    echo "[smoke] state file not found: $STATE_FILE" | tee -a "$LOG_FILE"
  fi
}

cleanup_context() {
  if [[ "$SKIP_RDC" -eq 1 ]]; then
    return 0
  fi
  echo "[smoke] cleanup: context clear" | tee -a "$LOG_FILE"
  "$RDX" --daemon-context "$CTX" context clear 2>&1 | tee -a "$LOG_FILE" || true
  echo "[smoke] cleanup: daemon stop" | tee -a "$LOG_FILE"
  "$RDX" --daemon-context "$CTX" daemon stop 2>&1 | tee -a "$LOG_FILE" || true
}

run_step() {
  local name="$1"
  local timeout_seconds="$2"
  shift 2
  echo "" | tee -a "$LOG_FILE"
  echo "[smoke] STEP: $name" | tee -a "$LOG_FILE"
  print_command "$@" | tee -a "$LOG_FILE"
  run_raw "$timeout_seconds" "$@"
  local rc=$?
  if [[ "$rc" -eq 124 || "$rc" -eq 137 ]]; then
    echo "[smoke] TIMEOUT after ${timeout_seconds}s: $name" | tee -a "$LOG_FILE"
    print_context_state
    cleanup_context
    exit "$rc"
  fi
  if [[ "$rc" -ne 0 ]]; then
    echo "[smoke] FAIL rc=$rc: $name" | tee -a "$LOG_FILE"
    cleanup_context
    exit "$rc"
  fi
}

run_negative_mcp() {
  local tmp
  tmp="$(mktemp)"
  echo "" | tee -a "$LOG_FILE"
  echo "[smoke] STEP: negative MCP route must be unsupported" | tee -a "$LOG_FILE"
  print_command "$RDX" mcp --ensure-env | tee -a "$LOG_FILE"
  if command -v timeout >/dev/null 2>&1; then
    timeout "$STEP_TIMEOUT" "$RDX" mcp --ensure-env 2>&1 | tee "$tmp" | tee -a "$LOG_FILE"
    rc="${PIPESTATUS[0]}"
  else
    "$RDX" mcp --ensure-env 2>&1 | tee "$tmp" | tee -a "$LOG_FILE"
    rc="${PIPESTATUS[0]}"
  fi
  if [[ "$rc" -eq 0 ]]; then
    echo "[smoke] FAIL: mcp route unexpectedly succeeded" | tee -a "$LOG_FILE"
    rm -f "$tmp"
    exit 1
  fi
  if ! grep -q "unsupported_command" "$tmp"; then
    echo "[smoke] FAIL: mcp route did not print unsupported_command" | tee -a "$LOG_FILE"
    rm -f "$tmp"
    exit 1
  fi
  rm -f "$tmp"
}

echo "[smoke] tools root: $TOOLS_ROOT" | tee -a "$LOG_FILE"
echo "[smoke] launcher: $RDX" | tee -a "$LOG_FILE"
echo "[smoke] context: $CTX" | tee -a "$LOG_FILE"

run_step "doctor JSON" "$STEP_TIMEOUT" "$RDX" --json doctor
run_step "tools list" "$STEP_TIMEOUT" "$RDX" tools list --json --limit 5
run_step "tools search" "$STEP_TIMEOUT" "$RDX" tools search pipeline --json
run_negative_mcp

if [[ "$SKIP_RDC" -eq 1 ]]; then
  echo "" | tee -a "$LOG_FILE"
  echo "[smoke] PASS: entry-only CLI smoke completed" | tee -a "$LOG_FILE"
  exit 0
fi

run_step "context clear" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" context clear
run_step "context status empty" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" context status --json
run_step "capture open" "$OPEN_TIMEOUT" "$RDX" --daemon-context "$CTX" capture open --file "$RDC_PATH" --frame-index 0
run_step "capture status" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" capture status
run_step "context status" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" context status --json
run_step "context update notes" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" context update --key notes --value "smoke-triaged" --json
run_step "vfs root tsv" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" vfs ls --path / --format tsv
run_step "vfs tree json" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" vfs tree --path / --depth 2 --format json
run_step "daemon tools list" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" tools list --json --limit 5
run_step "cleanup context clear" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" context clear
run_step "cleanup daemon stop" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" daemon stop

echo "" | tee -a "$LOG_FILE"
echo "[smoke] PASS: CLI smoke completed" | tee -a "$LOG_FILE"
