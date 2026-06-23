#!/usr/bin/env bash
set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
RDC_PATH=""
CTX="cli-smoke-$(date +%Y%m%d%H%M%S)"
STEP_TIMEOUT="${RDX_SMOKE_TIMEOUT:-120}"
OPEN_TIMEOUT="${RDX_SMOKE_OPEN_TIMEOUT:-600}"
LOG_FILE=""
FINDINGS_FILE=""
TIMEOUT_CMD=""
RDC_SIZE_BYTES=""
RDC_SHA256=""
COMMAND_LOG=()
RESULT_LOG=()

usage() {
  cat <<'EOF'
Usage: bash scripts/smoke_cli.sh [options]

Options:
  --tools-root <path>  rdx-tools root. Defaults to this script's parent directory.
  --rdc <path>         optional .rdc capture used for daemon-backed smoke.
  --context <id>       daemon context id. Defaults to cli-smoke-<timestamp>.
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
FINDINGS_FILE="$TOOLS_ROOT/intermediate/logs/tool_smoke_findings.md"
STATE_FILE="$TOOLS_ROOT/intermediate/runtime/rdx_cli/daemon_state_${CTX}.json"
mkdir -p "$(dirname "$LOG_FILE")"
: > "$LOG_FILE"
: > "$FINDINGS_FILE"

export MSYS_NO_PATHCONV="${MSYS_NO_PATHCONV:-1}"
export MSYS2_ARG_CONV_EXCL="${MSYS2_ARG_CONV_EXCL:-*}"

if [[ ! -f "$RDX" ]]; then
  echo "[smoke] ERROR: missing bash launcher: $RDX" | tee -a "$LOG_FILE"
  exit 2
fi

if [[ -n "$RDC_PATH" && ! -f "$RDC_PATH" ]]; then
  echo "[smoke] ERROR: .rdc capture not found: $RDC_PATH" | tee -a "$LOG_FILE"
  exit 2
fi

if [[ -n "$RDC_PATH" ]]; then
  if RDC_SIZE_BYTES="$(stat -c '%s' "$RDC_PATH" 2>/dev/null)"; then
    :
  else
    RDC_SIZE_BYTES="$(wc -c < "$RDC_PATH" | tr -d '[:space:]')"
  fi
  if command -v sha256sum >/dev/null 2>&1; then
    RDC_SHA256="$(sha256sum "$RDC_PATH" | awk '{print toupper($1)}')"
  else
    RDC_SHA256="unavailable"
  fi
fi

format_command() {
  local rendered=""
  local display_arg=""
  for arg in "$@"; do
    if [[ "$arg" == "$RDX" ]]; then
      display_arg="bin/rdx"
    elif [[ -n "$RDC_PATH" && "$arg" == "$RDC_PATH" ]]; then
      display_arg="<rdc_path>"
    else
      display_arg="$arg"
    fi
    rendered+=" $display_arg"
  done
  echo "${rendered# }"
}

write_findings() {
  local status="$1"
  {
    echo '# rdx-tools Local Smoke Findings'
    echo ''
    echo "- status: $status"
    echo "- context: $CTX"
    if [[ -n "$RDC_PATH" ]]; then
      echo "- rdc_path: \`$RDC_PATH\`"
      echo "- rdc_size_bytes: $RDC_SIZE_BYTES"
      echo "- rdc_sha256: $RDC_SHA256"
    else
      echo '- rdc_path: not provided'
    fi
    echo "- log: \`intermediate/logs/smoke_cli.log\`"
    echo ''
    echo '## Commands'
    local i
    for ((i = 0; i < ${#COMMAND_LOG[@]}; i++)); do
      echo "- ${RESULT_LOG[$i]}: \`${COMMAND_LOG[$i]}\`"
    done
    echo ''
  } > "$FINDINGS_FILE"
}

if [[ -x /usr/bin/timeout ]]; then
  TIMEOUT_CMD="/usr/bin/timeout"
else
  candidate_timeout="$(command -v timeout 2>/dev/null || true)"
  case "$candidate_timeout" in
    *[Ww]indows*|*[Ss]ystem32*|"")
      TIMEOUT_CMD=""
      ;;
    *)
      TIMEOUT_CMD="$candidate_timeout"
      ;;
  esac
fi

print_command() {
  printf '[smoke] command: %s\n' "$(format_command "$@")"
}

run_raw() {
  local timeout_seconds="$1"
  shift
  if [[ -n "$TIMEOUT_CMD" ]]; then
    "$TIMEOUT_CMD" "$timeout_seconds" "$@" 2>&1 | tee -a "$LOG_FILE"
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
  if [[ -z "$RDC_PATH" ]]; then
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
  local command_text
  command_text="$(format_command "$@")"
  COMMAND_LOG+=("$name: $command_text")
  run_raw "$timeout_seconds" "$@"
  local rc=$?
  if [[ "$rc" -eq 124 || "$rc" -eq 137 ]]; then
    echo "[smoke] TIMEOUT after ${timeout_seconds}s: $name" | tee -a "$LOG_FILE"
    RESULT_LOG+=("TIMEOUT after ${timeout_seconds}s")
    print_context_state
    write_findings "FAIL"
    cleanup_context
    exit "$rc"
  fi
  if [[ "$rc" -ne 0 ]]; then
    echo "[smoke] FAIL rc=$rc: $name" | tee -a "$LOG_FILE"
    RESULT_LOG+=("FAIL rc=$rc")
    write_findings "FAIL"
    cleanup_context
    exit "$rc"
  fi
  RESULT_LOG+=("PASS")
}

echo "[smoke] tools root: $TOOLS_ROOT" | tee -a "$LOG_FILE"
echo "[smoke] launcher: $RDX" | tee -a "$LOG_FILE"
echo "[smoke] context: $CTX" | tee -a "$LOG_FILE"
if [[ -n "$TIMEOUT_CMD" ]]; then
  echo "[smoke] timeout: $TIMEOUT_CMD" | tee -a "$LOG_FILE"
else
  echo "[smoke] timeout: unavailable" | tee -a "$LOG_FILE"
fi

run_step "doctor JSON" "$STEP_TIMEOUT" "$RDX" --json doctor
run_step "tools list" "$STEP_TIMEOUT" "$RDX" tools list --json --limit 5
run_step "tools search" "$STEP_TIMEOUT" "$RDX" tools search pipeline --json

if [[ -z "$RDC_PATH" ]]; then
  echo "" | tee -a "$LOG_FILE"
  echo "[smoke] PASS: entry-only CLI smoke completed" | tee -a "$LOG_FILE"
  write_findings "PASS"
  exit 0
fi

run_step "context clear" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" context clear
run_step "context status empty" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" context status --json
run_step "capture open" "$OPEN_TIMEOUT" "$RDX" --daemon-context "$CTX" capture open --file "$RDC_PATH" --frame-index 0
run_step "capture status" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" capture status
run_step "context status" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" context status --json
run_step "context update notes" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" context update --key notes --value "smoke-triaged" --json
run_step "vfs root tsv" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" vfs ls --path / --format tsv
run_step "vfs context tree json" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" vfs tree --path /context --depth 1 --format json
run_step "daemon tools list" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" tools list --json --limit 5
run_step "cleanup context clear" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" context clear
run_step "cleanup daemon stop" "$STEP_TIMEOUT" "$RDX" --daemon-context "$CTX" daemon stop

echo "" | tee -a "$LOG_FILE"
echo "[smoke] PASS: CLI smoke completed" | tee -a "$LOG_FILE"
write_findings "PASS"
