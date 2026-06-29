#!/bin/bash
# Keeps auto_trader.py alive — auto-restarts on crash/code-change.
# Halt trading without killing the process: /stopauto in Telegram (or touch STOP_AUTO).
#   bash run_auto.sh            # foreground
#   nohup bash run_auto.sh &    # background
cd "$(dirname "$0")"
while true; do
  echo "[run_auto] launching auto_trader $(date '+%H:%M:%S')"
  python3 auto_trader.py
  echo "[run_auto] exited — relaunching in 3s"
  sleep 3
done
