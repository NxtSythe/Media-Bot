#!/usr/bin/env bash
cd "$(dirname "$0")"
while true; do
  python3 bot.py
  echo "Bot stopped, restarting in 10s..."
  sleep 10
done
