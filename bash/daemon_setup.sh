#!/bin/bash

TARGET_DIR="$1"
SERVICE_FILE="/etc/systemd/system/nodo.service"

if [ -z "$TARGET_DIR" ]; then
    echo "Usage: $0 <TARGET_DIR>"
    exit 1
fi

if [ "$(id -u)" -ne 0 ]; then
    echo "This script must be run with sudo."
    exit 1
fi

SCRIPT_USER=$(logname 2>/dev/null || echo $USER)
# If SCRIPT_USER is root, try to find the owner of the TARGET_DIR
if [ "$SCRIPT_USER" = "root" ]; then
    SCRIPT_USER=$(stat -c '%U' "$TARGET_DIR")
fi

if [ -f "$SERVICE_FILE" ]; then
    printf "Service file $SERVICE_FILE already exists. Removing it...\n"
    systemctl stop nodo.service
    systemctl disable nodo.service
    rm -f "$SERVICE_FILE"
fi

printf "Creating $SERVICE_FILE...\n"
cat <<EOF > "$SERVICE_FILE"
[Unit]
Description=Nodo Serve
After=network.target

[Service]
Type=simple
User=root
Group=sudo
WorkingDirectory=$TARGET_DIR
ExecStart=/bin/bash -c 'source $TARGET_DIR/venv/bin/activate && exec python3 $TARGET_DIR/nodo.py daemon'
Restart=on-failure
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

printf "Setting the permissions for the service file...\n"
chmod 644 "$SERVICE_FILE"

printf "Reloading systemd daemon, enabling, and starting the nodo service...\n"
systemctl daemon-reload
systemctl enable nodo.service
systemctl start nodo.service
printf "Systemd daemon reloaded and nodo service started/enabled.\n"

# Set permissions for TARGET_DIR to ensure service can access/write if needed, 
# though service runs as root, so it should be fine. 
# But we might want to ensure the user can still access it.
# install.sh did: sudo chown -R "$SCRIPT_USER:$SCRIPT_USER" "$TARGET_DIR"
# and sudo chmod -R 777 "$TARGET_DIR"

printf "Setting permissions for %s...\n" "$TARGET_DIR"
# We keep the ownership to the user who installed it (or the one we detected)
chown -R "$SCRIPT_USER:$SCRIPT_USER" "$TARGET_DIR"
chmod -R 777 "$TARGET_DIR"

if systemctl --no-pager status nodo.service >/dev/null 2>&1; then
  printf "Restarting nodo.service...\n"
  systemctl restart nodo.service
fi
