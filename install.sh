#!/usr/bin/env bash
# LunaMC Media Bot - one-command install / update / move to a new server.
# Run it from inside the bot folder:   sudo bash install.sh
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE="lunabot"

if [ "$EUID" -ne 0 ]; then
  echo "Please run with sudo:  sudo bash install.sh"
  exit 1
fi

if [ ! -f "$DIR/config.yml" ]; then
  echo "config.yml was not found in $DIR"
  exit 1
fi

echo "[1/5] Installing system packages..."
apt-get update -y >/dev/null
apt-get install -y python3 python3-venv python3-pip >/dev/null

# Folders inside /root can only be used by root.
if [[ "$DIR" == /root/* ]]; then
  RUN_USER="root"
else
  RUN_USER="lunabot"
  echo "[2/5] Creating the '$RUN_USER' user..."
  id -u "$RUN_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$RUN_USER"
fi

echo "[3/5] Setting up the Python environment..."
# A venv copied from another server usually doesn't work - rebuild it if needed.
if ! "$DIR/venv/bin/python" -c "import sys" >/dev/null 2>&1 || ! "$DIR/venv/bin/pip" --version >/dev/null 2>&1; then
  rm -rf "$DIR/venv"
  python3 -m venv "$DIR/venv"
fi
"$DIR/venv/bin/pip" install -q --upgrade pip
"$DIR/venv/bin/pip" install -q -r "$DIR/requirements.txt"

echo "[4/5] Setting file permissions..."
mkdir -p "$DIR/backups"
chown -R "$RUN_USER":"$RUN_USER" "$DIR"

echo "[5/5] Installing the system service..."
cat > /etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=LunaMC Media Bot
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python bot.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1

if grep -q "توکن_بات_رو_اینجا_بذار" "$DIR/config.yml"; then
  echo
  echo "Installed, but config.yml still needs your bot token and IDs."
  echo "Edit it with:  nano $DIR/config.yml"
  echo "Then start:    systemctl restart $SERVICE"
  exit 0
fi

systemctl restart "$SERVICE"
sleep 3
echo
systemctl --no-pager --lines=0 status "$SERVICE" | head -n 5
echo
echo "Done. Live logs:  journalctl -u $SERVICE -f"
