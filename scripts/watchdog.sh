#!/usr/bin/env bash
# Bot watchdog — keeps the copy-trading bot alive through the night.
#
# Behaviour:
#   * Every WATCHDOG_INTERVAL seconds it checks two health signals:
#       1. A python main.py process is running.
#       2. bot_live.log has been written to within the last STALE_SECONDS
#          (heartbeat is emitted every ~15s, so anything >90s is a hang).
#   * If either check fails, the bot is respawned inside screen session
#     "botlive". Port 8080 is freed first to avoid the web dashboard
#     failing to bind.
#   * Restarts are throttled to at most MAX_RESTARTS_PER_HOUR.
#
# Designed to run itself inside a second screen session so it survives
# closing the terminal.

set -euo pipefail

BOT_DIR="/Users/emilioranucoli/Desktop/Oficina_Ranuk/Bot-Copy-Trading-Ranuk"
# Pick the right log file based on MODE in .env (defaults to paper).
MODE=$(grep -E '^MODE=' "$BOT_DIR/.env" 2>/dev/null | tail -n1 | cut -d= -f2 | tr -d ' "' || true)
MODE="${MODE:-paper}"
if [[ "$MODE" == "live" ]]; then
  LOG_FILE="$BOT_DIR/bot_live.log"
  SCREEN_NAME="botlive"
else
  LOG_FILE="$BOT_DIR/bot_paper.log"
  SCREEN_NAME="botpaper"
fi
WATCHDOG_LOG="$BOT_DIR/logs/watchdog.log"
PYTHON_CMD="source .venv/bin/activate && python main.py --dashboard web --web-port 8080 >> $(basename "$LOG_FILE") 2>&1"
WATCHDOG_INTERVAL="${WATCHDOG_INTERVAL:-60}"
STALE_SECONDS="${STALE_SECONDS:-300}"
MAX_RESTARTS_PER_HOUR="${MAX_RESTARTS_PER_HOUR:-6}"

mkdir -p "$BOT_DIR/logs"

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" | tee -a "$WATCHDOG_LOG"
}

is_bot_running() {
  # Any python process whose command-line still contains "main.py"
  pgrep -f "python main.py" >/dev/null 2>&1
}

is_log_fresh() {
  [[ -f "$LOG_FILE" ]] || return 1
  local mtime now age
  mtime=$(stat -f %m "$LOG_FILE")
  now=$(date +%s)
  age=$(( now - mtime ))
  if (( age <= STALE_SECONDS )); then
    return 0
  fi
  log "stale log: last write was ${age}s ago"
  return 1
}

start_bot() {
  log "starting bot in screen session '$SCREEN_NAME'"
  # Kill any orphan python main.py that may still be hanging
  pkill -f "python main.py" 2>/dev/null || true
  # Free the web-dashboard port
  lsof -ti:8080 2>/dev/null | xargs kill -9 2>/dev/null || true
  # Wipe any dead screen sessions first
  screen -wipe >/dev/null 2>&1 || true
  cd "$BOT_DIR"
  ulimit -n 10240 2>/dev/null || true
  screen -dmS "$SCREEN_NAME" bash -c "$PYTHON_CMD"
  sleep 3
  if is_bot_running; then
    log "bot started OK (pid=$(pgrep -f 'python main.py' | head -n1))"
  else
    log "bot failed to start; see $LOG_FILE tail"
    tail -n 30 "$LOG_FILE" 2>/dev/null | tee -a "$WATCHDOG_LOG"
  fi
}

# --- rolling restart counter -----------------------------------------------
# Keep the last N restart epochs in a file and prune anything older than 1h.
RESTART_FILE="$BOT_DIR/logs/watchdog_restarts.tsv"
touch "$RESTART_FILE"

can_restart_now() {
  local now cutoff count
  now=$(date +%s)
  cutoff=$(( now - 3600 ))
  awk -v c="$cutoff" '$1 >= c' "$RESTART_FILE" > "$RESTART_FILE.tmp"
  mv "$RESTART_FILE.tmp" "$RESTART_FILE"
  count=$(wc -l < "$RESTART_FILE" | tr -d ' ')
  if (( count >= MAX_RESTARTS_PER_HOUR )); then
    log "restart rate-limited: ${count}/${MAX_RESTARTS_PER_HOUR} in last hour"
    return 1
  fi
  echo "$now" >> "$RESTART_FILE"
  return 0
}

trap 'log "watchdog stopping"; exit 0' INT TERM

log "watchdog online (interval=${WATCHDOG_INTERVAL}s stale=${STALE_SECONDS}s)"
while true; do
  if ! is_bot_running; then
    log "bot process missing"
    if can_restart_now; then
      start_bot
    fi
  elif ! is_log_fresh; then
    log "heartbeat missing; forcing restart"
    if can_restart_now; then
      pkill -f "python main.py" 2>/dev/null || true
      sleep 2
      start_bot
    fi
  fi
  sleep "$WATCHDOG_INTERVAL"
done
