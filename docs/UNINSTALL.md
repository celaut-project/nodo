# Nodo Uninstallation Guide

This guide covers both automatic and fully manual uninstall.

It is aligned with the current runtime model:

- Python/JRE/yq can be local to `MAIN_DIR`.
- `nodo.service` exports `JAVA_HOME` and `PATH` from configured local paths.
- No local Docker: services run under Cloud Hypervisor natively, or under QEMU when their architecture is not the host's. binfmt is not part of the install profile.

## 1) Automatic uninstall (`nodo uninstall`)

Run `nodo uninstall` (or `sudo nodo uninstall`). The command runs `uninstall.sh`
for the installation root of the `nodo` that you run. It accepts the options of
the script below except `--target-dir`. The sections below describe the script,
and you can also run it directly.

The script has two modes. It selects the mode with the user that runs it.

Options:

- `-y`, `--yes`: do not ask before removing.
- `--no-input`: ask nothing. Without sudo, print the root steps and save them.
  It removes nothing unless `--yes` is given too.
- `--delete-wallets`: also delete the wallet mnemonics (see below).
- `--target-dir <path>`: the installation root (script only).

Before it removes anything, the script prints what it removes and what it keeps,
and asks `Remove it? [y/N]`. Without a terminal to ask at, it removes nothing
unless `--yes` is given.

It removes only what belongs to the installation root:

- `nodo.service` and `/etc/systemd/system/nodo.service` only when the unit's
  `ExecStart` or `WorkingDirectory` names the root. Another unit that runs from
  the root is stopped too.
- The wrapper (`/usr/local/bin/nodo`, or `~/.local/bin/nodo` without sudo) only
  when it runs `nodo.py` from the root.
- `/etc/nodo/service-user`, `/usr/local/libexec/nodo/virtiofsd` and the AppArmor
  profile `/etc/apparmor.d/nodo-virtiofsd` only with the unit: they are the files
  of the node that the unit runs.
- The AppArmor profile `/etc/apparmor.d/nodo-rootlesskit` (the userns exemption
  for the rootlesskit in the root) only when the profile names the root, with or
  without a unit.
- Only processes that run from the root, and only mounts under it.

Anything that names another directory belongs to another install. The script
keeps it, and says so in the list it prints.

**Wallets.** `config.yaml` holds the identity mnemonic and every
`ledgers.*.WALLET_MNEMONIC`, and its backups (`config-<time>.yaml`) are beside
it, with the temporary copies a crashed write can leave (`.config-<random>.yaml`,
`.config-root.*`). The script keeps them all: before it removes the root, it moves them to
`<root>.wallets-<time>` (mode 0700) and prints that path. With
`--delete-wallets` it deletes them with the root. That option asks for a second
answer at a terminal, the words `delete wallets`, even with `--yes`. Without a
copy of the mnemonics, the funds on those wallets are lost.

### 1.1) With sudo

Use this if your installation root is the default `/nodo`.

```bash
cd /nodo
sudo chmod +x uninstall.sh
sudo ./uninstall.sh
```

What it does (best effort), for what belongs to the root (see above):

- Stops/disables `nodo.service`, and any other systemd `--type=service` unit
  whose `ExecStart` or `WorkingDirectory` names `/nodo`.
- Stops the rootless BuildKit builder (and any `dockerd` left over from the
  retired Docker toolchain) of `/nodo`, and unmounts everything under `/nodo`.
- Removes `/etc/systemd/system/nodo.service` and `/usr/local/bin/nodo`.
- Removes the files that a node with a service user or without sudo adds:
  `/etc/nodo/service-user`, `/usr/local/libexec/nodo/virtiofsd` and the AppArmor
  profile `/etc/apparmor.d/nodo-virtiofsd`. It does not delete the service user.
- Unloads and removes `/etc/apparmor.d/nodo-rootlesskit` when it names `/nodo`.
- Moves the wallet mnemonics to `/nodo.wallets-<time>`, then removes `/nodo`.

### 1.2) Without sudo

Use this if you installed with `./install.sh` without sudo. The default
installation root is `~/.local/share/nodo`. Use `--target-dir` for another root.

```bash
cd ~/.local/share/nodo
./uninstall.sh
```

The script removes the files that you own:

- The BuildKit builder processes of the installation root.
- The wrapper `~/.local/bin/nodo`, when it runs this root.
- The shell completion in `~/.local/share/bash-completion/completions/nodo`
  and `~/.zsh/completions/_nodo`, unless the wrapper runs another root.
- The installation root, after it moves the wallet mnemonics out of it.

The script cannot do these steps without root, and lists each one only when it
belongs to the installation root (see above):

- Stop and disable `nodo.service`. This stops every VM that the service runs.
- Remove the unit `/etc/systemd/system/nodo.service`.
- Remove `/etc/nodo/service-user`, `/usr/local/libexec/nodo/virtiofsd` and the
  AppArmor profile `/etc/apparmor.d/nodo-virtiofsd`.
- Unload and remove `/etc/apparmor.d/nodo-rootlesskit`, when it names the
  installation root.
- Unmount a mount point under the installation root.
- Remove a file in the installation root that belongs to root, for example a
  `.pyc` file that `sudo nodo doctor` wrote.

The script prints each of these steps as an exact `sudo` command. It saves them
to `<target-dir>/uninstall-privileged.sh`. It runs them only if you answer yes.
It removes nothing from the installation root until the root steps are done.

With `--no-input --yes`, the script only prints and saves the steps, and stops.
Run the saved script, then run `./uninstall.sh` again. The second run asks for no root
steps and removes your files. A step is only listed when it is still necessary.

> **Note:** the script does **not** clean up running Cloud Hypervisor microVMs.
> CH instances are subprocess children of the daemon (not systemd units), so
> their bridge/tap/iptables state is left orphaned on the host. Run
> `nodo kill <instance>` for each active microVM (or reboot) **before**
> uninstalling to clear residual microVMs and bridge/tap devices.

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

WRAPPER_SCRIPT="/usr/local/bin/nodo"
SERVICE_FILE="/etc/systemd/system/nodo.service"
```

### 2.3 Stop and disable service

Check first that the unit runs `$TARGET_DIR` (`systemctl cat nodo.service`): if
it runs another install, skip 2.3 and the unit, wrapper and root files of 2.4.

```bash
sudo systemctl stop nodo.service 2>/dev/null || true
sudo systemctl disable nodo.service 2>/dev/null || true
```

### 2.4 Remove service file, wrapper, and shell completions

```bash
sudo rm -f "$SERVICE_FILE"
sudo systemctl daemon-reload
sudo rm -f "$WRAPPER_SCRIPT"
# System-level shell completions installed by install.sh (uninstall.sh does not remove these):
sudo rm -f /etc/bash_completion.d/nodo /usr/local/share/zsh/site-functions/_nodo
```

### 2.5 Remove installation directory

`config.yaml`, its backups and any `.config-*.yaml` temporary copy hold the
wallet mnemonics. Move them out first,
unless you have a copy:

```bash
sudo install -d -m 0700 "$TARGET_DIR.wallets"
sudo mv "$TARGET_DIR"/config.yaml "$TARGET_DIR"/config-*.yaml "$TARGET_DIR"/.config-*.yaml "$TARGET_DIR.wallets/"
sudo rm -rf "$TARGET_DIR"
```

## 3) What is removed vs not removed

Removed (if inside `TARGET_DIR`):

- Local Python runtime and venv.
- Local Java runtime.
- Local yq binary.
- Cloud Hypervisor assets copied under `TARGET_DIR`.

Not removed automatically:

- Host packages installed via `apt` (`build-essential`, `curl`, etc.).
- System-level shell completions installed by `install.sh`:
  `/etc/bash_completion.d/nodo` and `/usr/local/share/zsh/site-functions/_nodo`
  (remove with the command in §2.4).
- The four `git config --global` keys `install.sh` writes to root's gitconfig —
  these are never reverted; remove them manually with `sudo git config --global --unset <key>` if desired.
- Running Cloud Hypervisor microVMs and their bridge/tap/iptables state (see the note in §1).
- Any custom files outside `TARGET_DIR`.

If you want to remove host packages too, do it explicitly with `apt` according to your system policy.
