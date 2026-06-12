#!/bin/bash
# Independent health watchdog for the packed campaign.
# Runs continuously (every 60s), independent of the orchestrator.
# Logs RAM / disk / container count / hung-runner count / orchestrator status.
# Self-exits when the orchestrator pid is gone (campaign finished) to avoid a zombie.
LOG=/path/to/magma_split/monitor_watchdog/health.log
PIDFILE=/tmp/packed_pid.txt
while true; do
  ts=$(TZ='UTC' date '+%Y-%m-%d %H:%M:%S %Z')
  ram=$(free -g | awk 'NR==2{print $7}')
  disk=$(df -BG / | awk 'NR==2{gsub("G","",$4); print $4}')
  cnt=$(docker ps -q 2>/dev/null | wc -l)
  over=$(ps -eo etimes,cmd | grep magma_online_split.py | grep -v grep | awk '$1>44400{c++} END{print c+0}')
  if pgrep -F "$PIDFILE" >/dev/null 2>&1; then alive=yes; else alive=no; fi
  flag=""
  [ "${ram:-999}" -lt 80 ] && flag="$flag !RAM_LOW"
  [ "${disk:-9999}" -lt 150 ] && flag="$flag !DISK_LOW"
  [ "${over:-0}" -gt 0 ] && flag="$flag !HUNG_RUNNERS=$over"
  echo "$ts ram=${ram}G disk=${disk}G containers=$cnt orch=$alive$flag" >> "$LOG"
  if [ "$alive" = no ]; then
    echo "$ts orchestrator gone -> watchdog exiting" >> "$LOG"
    break
  fi
  sleep 60
done
