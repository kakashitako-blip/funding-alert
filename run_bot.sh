#!/bin/bash
# Keeps tgbot.py alive — auto-restarts on crash AND when a code change is
# deployed (Claude kills the python process; this loop relaunches it with the
# new code). Run this ONCE and leave it; you never restart manually again.
#
#   bash run_bot.sh            # foreground (leave terminal open)
#   nohup bash run_bot.sh &    # background (survives closing the terminal)
cd "$(dirname "$0")"
while true; do
  echo "[run_bot] launching tgbot $(date '+%H:%M:%S')"
  python3 tgbot.py
  echo "[run_bot] tgbot exited — relaunching in 2s"
  sleep 2
done
