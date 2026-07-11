#!/usr/bin/env bash
# Reusable Slack watchdog for long-running jobs. Posts START, then SUCCESS or CRASH/ANOMALY.
# Attaches an nvidia-smi snapshot when GPUs are present. NO heartbeat.
#
# Two modes:
#   1) RUN  — wrap + launch a command (recommended for new jobs):
#        tmux new -s job 'bash /path/to/slack-connector/watch.sh run "my label" <command...>'
#      (wrap in tmux so it survives disconnects; watch.sh runs the command as its child.)
#
#   2) ATTACH — monitor an ALREADY-running tmux session by reading its pane:
#        bash /path/to/slack-connector/watch.sh attach "my label" <tmux-session> "<DONE regex>" ["<FAIL regex>"] &
#
# Anomaly patterns (always flagged, either mode): traceback, CUDA/OOM errors, NaN grad/loss,
# RuntimeError, NCCL error, "FAILED", "core dumped", "Killed".
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/.venv/bin/python"
NOTIFY="$HERE/notify.py"
POLL=60

ANOM='[Tt]raceback|CUDA error|CUDA out of memory|[Oo]ut of memory|OutOfMemory|RuntimeError|NCCL.*(error|timeout)|core dumped|Killed|'"'"'grad_norm'"'"': '"'"'nan|'"'"'loss/total'"'"': '"'"'nan|DIVERGED|FAILED \(rc='
notify() { "$PY" "$NOTIFY" "$1" >/dev/null 2>&1 || true; }
gpu() {
  command -v nvidia-smi >/dev/null 2>&1 || { echo "  (no GPUs)"; return; }
  nvidia-smi --query-gpu=index,name,memory.used,memory.total,utilization.gpu --format=csv,noheader 2>&1 | sed 's/^/  /'
}
now() { date '+%F %T'; }

mode="${1:-}"; shift || true

case "$mode" in
  run)
    LABEL="${1:-job}"; shift
    LOG=$(mktemp "/tmp/watch-$(echo "$LABEL" | tr -c 'A-Za-z0-9' '_').XXXX.log")
    notify "▶️ STARTED: ${LABEL}
$(now)
cmd: $*
GPUs:
$(gpu)"
    # run the command, mirror output to pane + log; capture the command's real exit code
    "$@" 2>&1 | tee "$LOG"; rc=${PIPESTATUS[0]}
    anom=$(grep -oniE "$ANOM" "$LOG" 2>/dev/null | head -5)
    if [ "$rc" -eq 0 ] && [ -z "$anom" ]; then
      notify "✅ COMPLETED: ${LABEL}
$(now)  (exit 0, no anomalies)
GPUs:
$(gpu)"
    else
      notify "🚨 PROBLEM: ${LABEL}
$(now)  exit=${rc}
$([ -n "$anom" ] && printf 'flags:\n%s\n' "$anom")
last log lines:
$(tail -8 "$LOG" | sed 's/^/  /')
GPUs:
$(gpu)"
    fi
    ;;

  attach)
    LABEL="${1:?label}"; SESSION="${2:?tmux session}"; DONE_RE="${3:?DONE regex}"; FAIL_RE="${4:-}"
    if ! tmux has-session -t "$SESSION" 2>/dev/null; then
      notify "⚠️ watch attach: tmux session '${SESSION}' not found — nothing to monitor."; exit 1
    fi
    notify "👀 MONITORING: ${LABEL} (tmux '${SESSION}', already running)
$(now)
GPUs:
$(gpu)"
    while true; do
      pane=$(tmux capture-pane -t "$SESSION" -p -S -4000 2>/dev/null || true)
      if printf '%s' "$pane" | grep -qE "$DONE_RE"; then
        notify "✅ COMPLETED: ${LABEL}
$(now)  (done marker seen)
tail:
$(printf '%s' "$pane" | grep -vE '^\s*$' | tail -6 | sed 's/^/  /')
GPUs:
$(gpu)"
        exit 0
      fi
      hit=$(printf '%s' "$pane" | grep -oniE "${FAIL_RE:+$FAIL_RE|}$ANOM" | head -5 || true)
      if [ -n "$hit" ]; then
        notify "🚨 ANOMALY: ${LABEL}
$(now)
flags:
$(printf '%s' "$hit" | sed 's/^/  /')
tail:
$(printf '%s' "$pane" | grep -vE '^\s*$' | tail -6 | sed 's/^/  /')
GPUs:
$(gpu)"
        exit 2
      fi
      if ! tmux has-session -t "$SESSION" 2>/dev/null; then
        notify "🚨 ENDED without done-marker: ${LABEL}
$(now)  (tmux '${SESSION}' gone — likely crashed)
last pane:
$(printf '%s' "$pane" | grep -vE '^\s*$' | tail -8 | sed 's/^/  /')
GPUs:
$(gpu)"
        exit 3
      fi
      sleep "$POLL"
    done
    ;;

  *)
    echo "usage:"
    echo "  watch.sh run    \"<label>\" <command...>"
    echo "  watch.sh attach \"<label>\" <tmux-session> \"<DONE regex>\" [\"<FAIL regex>\"]"
    exit 64
    ;;
esac
