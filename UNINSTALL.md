# Nodo Uninstallation Guide

This guide covers both automatic and fully manual uninstall.

It is aligned with the current runtime model:

- Python/JRE/yq/Docker can be local to `MAIN_DIR`.
- `nodo.service` exports `JAVA_HOME` and `PATH` from configured local paths.
- QEMU/binfmt are not part of the install profile.

## 1) Automatic uninstall (`uninstall.sh`)

Use this if your installation root is the default `/nodo`.

```bash
cd /nodo
sudo chmod +x uninstall.sh
sudo ./uninstall.sh
```

What it does (best effort):

- Stops/disables `nodo.service`.
- Tries to clean node-managed containers.
- Stops isolated Docker daemon under `/nodo/docker`.
- Removes `/etc/systemd/system/nodo.service`, `/usr/local/bin/nodo`, and `/nodo`.

## 2) Manual uninstall (recommended for custom `MAIN_DIR`)

Use this flow when:

- you installed manually,
- `MAIN_DIR` is not `/nodo`,
- or you want full control without trusting uninstall scripts.

### 2.1 Set the installation root

```bash
export TARGET_DIR=/nodo
```

If your install root is different, use that path.

### 2.2 Resolve effective paths from `config.yaml` (if present)

```bash
expand_main_dir() {
  printf '%s' "$1" | sed "s|\${main.MAIN_DIR}|$TARGET_DIR|g"
}

read_cfg_path_or_default() {
  local query="$1"
  local fallback="$2"
  local yq_bin="$TARGET_DIR/bin/yq"
  local cfg="$TARGET_DIR/config.yaml"
  local raw=""

  if [ -x "$yq_bin" ] && [ -f "$cfg" ]; then
    raw="$($yq_bin -r "$query // \"\"" "$cfg" 2>/dev/null || true)"
  fi

  if [ -z "$raw" ] || [ "$raw" = "null" ]; then
    raw="$fallback"
  fi

  expand_main_dir "$raw"
}

DOCKER_BIN_TARGET="$(read_cfg_path_or_default '.dependencies.docker.BIN' '${main.MAIN_DIR}/bin/docker')"
DOCKERD_BIN_TARGET="$(read_cfg_path_or_default '.dependencies.docker.DAEMON_BIN' '${main.MAIN_DIR}/bin/dockerd')"
DOCKER_SOCKET_PATH="$(read_cfg_path_or_default '.virtualizers.docker.DOCKER_SOCKET' '${main.MAIN_DIR}/docker/docker.sock')"
WRAPPER_SCRIPT="/usr/local/bin/nodo"
SERVICE_FILE="/etc/systemd/system/nodo.service"
```

### 2.3 Stop and disable service

```bash
sudo systemctl stop nodo.service 2>/dev/null || true
sudo systemctl disable nodo.service 2>/dev/null || true
```

### 2.4 Optional: remove containers from Nodo isolated daemon

If isolated Docker is still reachable, remove containers from that daemon only:

```bash
if [ -x "$DOCKER_BIN_TARGET" ] && [ -S "$DOCKER_SOCKET_PATH" ]; then
  IDS="$($DOCKER_BIN_TARGET -H "unix://$DOCKER_SOCKET_PATH" ps -aq 2>/dev/null || true)"
  if [ -n "$IDS" ]; then
    $DOCKER_BIN_TARGET -H "unix://$DOCKER_SOCKET_PATH" rm -f $IDS || true
  fi
fi
```

### 2.5 Stop isolated dockerd processes

```bash
if [ -f "$TARGET_DIR/docker/docker.pid" ]; then
  PID="$(cat "$TARGET_DIR/docker/docker.pid")"
  sudo kill "$PID" 2>/dev/null || true
  sleep 2
  sudo kill -9 "$PID" 2>/dev/null || true
fi

PIDS="$(pgrep -f "$TARGET_DIR/.*/dockerd" || true)"
if [ -n "$PIDS" ]; then
  sudo kill $PIDS 2>/dev/null || true
  sleep 2
  sudo kill -9 $PIDS 2>/dev/null || true
fi
```

### 2.6 Unmount leftover Docker netns mounts (if any)

```bash
NETNS_DIR="$TARGET_DIR/docker/exec/netns"
if [ -d "$NETNS_DIR" ] && command -v findmnt >/dev/null 2>&1; then
  while read -r mp; do
    [ -n "$mp" ] && sudo umount -l "$mp" 2>/dev/null || true
  done < <(findmnt -R -n -o TARGET "$NETNS_DIR" 2>/dev/null | sort -r)
fi
```

### 2.7 Remove service file and wrapper

```bash
sudo rm -f "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo rm -f "$WRAPPER_SCRIPT"
```

### 2.8 Remove installation directory

```bash
sudo rm -rf "$TARGET_DIR"
```

## 3) What is removed vs not removed

Removed (if inside `TARGET_DIR`):

- Local Python runtime and venv.
- Local Java runtime.
- Local yq/Docker binaries.
- Isolated Docker data/sockets.
- Cloud Hypervisor assets copied under `TARGET_DIR`.

Not removed automatically:

- Host packages installed via `apt` (`build-essential`, `curl`, etc.).
- Any custom files outside `TARGET_DIR`.

If you want to remove host packages too, do it explicitly with `apt` according to your system policy.
