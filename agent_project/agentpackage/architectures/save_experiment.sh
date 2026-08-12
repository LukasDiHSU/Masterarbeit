#!/usr/bin/env bash
set -euo pipefail

# Finalize the active experiment session into a named directory.
#
# Usage:
#   ./agentpackage/architectures/save_experiment.sh <run_name>
#   ./agentpackage/architectures/save_experiment.sh <run_name> --stop
#   ./agentpackage/architectures/save_experiment.sh <run_name> --note "nav aborted twice"
#
# Copies tasks/experiments/runs/_active/<current>/ → tasks/experiments/runs/<run_name>/
# With --stop, also sends SIGINT/SIGTERM to recorded terminal shell PIDs.

SCRIPT_PATH="${BASH_SOURCE[0]}"
if command -v readlink >/dev/null 2>&1; then
  SCRIPT_PATH="$(readlink -f "$SCRIPT_PATH" 2>/dev/null || readlink "$SCRIPT_PATH" 2>/dev/null || echo "$SCRIPT_PATH")"
fi
SCRIPT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
REPO_ROOT="$(cd "$PROJECT_ROOT/.." && pwd)"
RUNS_ROOT="${EXPERIMENT_RUNS_ROOT:-$REPO_ROOT/tasks/experiments/runs}"
ACTIVE_ROOT="$RUNS_ROOT/_active"
CURRENT_FILE="$ACTIVE_ROOT/CURRENT"

STOP=0
NOTE=""
NAME=""

usage() {
  cat <<EOF
Usage: $(basename "$0") <run_name> [--stop] [--note TEXT]

Save the currently active experiment terminal logs into:
  $RUNS_ROOT/<run_name>/

Options:
  --stop          Also stop recorded terminal shells (best-effort)
  --note TEXT     Store a short note in meta.json / NOTES.txt
  -h, --help      Show this help
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --stop)
      STOP=1
      shift
      ;;
    --note)
      NOTE="${2:-}"
      shift 2
      ;;
    --note=*)
      NOTE="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    -*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [[ -n "$NAME" ]]; then
        echo "error: unexpected argument $1" >&2
        exit 1
      fi
      NAME="$1"
      shift
      ;;
  esac
done

if [[ -z "$NAME" ]]; then
  usage >&2
  exit 1
fi

# sanitize name (allow slashes for nested dirs like stations/easy/hmas2_r1)
NAME="${NAME#/}"
if [[ -z "$NAME" || "$NAME" == *".."* ]]; then
  echo "error: invalid run name" >&2
  exit 1
fi

if [[ ! -f "$CURRENT_FILE" ]]; then
  echo "error: no active experiment session ($CURRENT_FILE missing)." >&2
  echo "Launch an architecture first (e.g. ./launch_hmas2.sh)." >&2
  exit 1
fi

SESSION_DIR="$(tr -d '\n' <"$CURRENT_FILE")"
if [[ ! -d "$SESSION_DIR" ]]; then
  echo "error: active session dir missing: $SESSION_DIR" >&2
  exit 1
fi

DEST="$RUNS_ROOT/$NAME"
if [[ -e "$DEST" ]]; then
  echo "error: destination already exists: $DEST" >&2
  echo "Choose another name or remove it first." >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST")"

if [[ "$STOP" -eq 1 && -f "$SESSION_DIR/pids.txt" ]]; then
  echo "Stopping recorded terminal shells..."
  while read -r pid; do
    [[ -z "$pid" ]] && continue
    if kill -0 "$pid" 2>/dev/null; then
      # Signal the shell; children usually die with the pipe/tee group.
      kill -INT "$pid" 2>/dev/null || true
      sleep 0.2
      if kill -0 "$pid" 2>/dev/null; then
        # Kill process group if possible, else TERM the shell.
        kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
      fi
    fi
  done <"$SESSION_DIR/pids.txt"
  sleep 0.5
fi

# Flush a marker into each log.
if [[ -d "$SESSION_DIR/logs" ]]; then
  for f in "$SESSION_DIR/logs"/*.log; do
    [[ -e "$f" ]] || continue
    {
      echo
      echo "===== saved: $(date -Iseconds) as $NAME ====="
    } >>"$f"
  done
fi

cp -a "$SESSION_DIR" "$DEST"

python3 - "$DEST" "$NAME" "$NOTE" "$STOP" <<'PY'
import json, sys, pathlib
dest = pathlib.Path(sys.argv[1])
name = sys.argv[2]
note = sys.argv[3]
stopped = sys.argv[4] == "1"
meta_path = dest / "meta.json"
meta = {}
if meta_path.exists():
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        meta = {}
meta["saved_as"] = name
meta["saved_at"] = __import__("datetime").datetime.now().astimezone().isoformat()
meta["status"] = "stopped" if stopped else "saved"
if note:
    meta["note"] = note
meta_path.write_text(json.dumps(meta, indent=2) + "\n")
notes = dest / "NOTES.txt"
if note:
    notes.write_text(note.strip() + "\n")
elif not notes.exists():
    notes.write_text("")
# index of log files
logs = sorted((dest / "logs").glob("*.log")) if (dest / "logs").is_dir() else []
index = dest / "LOG_INDEX.txt"
lines = [f"Run: {name}", f"Logs ({len(logs)}):", ""]
for p in logs:
    lines.append(f"  - logs/{p.name}  ({p.stat().st_size} bytes)")
index.write_text("\n".join(lines) + "\n")
print(f"Saved experiment → {dest}")
if logs:
    print(f"  {len(logs)} terminal log(s) under logs/")
PY

# Clear active pointer (session copy remains under _active until cleaned).
rm -f "$CURRENT_FILE"
echo "Active session pointer cleared. You can launch a new experiment now."
if [[ "$STOP" -eq 0 ]]; then
  echo "Note: processes were left running. Re-run with --stop to signal them."
fi
