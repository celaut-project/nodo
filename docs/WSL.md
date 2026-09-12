# Nodo on WSL2 (Windows)

Windows cannot run a node directly: services execute as Cloud Hypervisor microVMs,
which need `/dev/kvm`. WSL2 is the supported way to get one, and it is the only
Windows path there is.

There are two ways in, and they are not equivalent:

| | [The installer](#the-installer) | [By hand](#installing-by-hand) |
|---|---|---|
| What runs it | `bash/install.ps1`, or `Nodo-Setup.exe` (the same script compiled) | you, step by step |
| Distro | a purpose-built Debian rootfs named `Nodo` | whatever you pick |
| Host kernel | a custom `bzImage` pinned in `.wslconfig` | whatever WSL ships |
| Networking | mirrored + a Hyper-V inbound rule, configured for you | **yours to set up** — see [Reaching the node](#reaching-the-node-from-outside) |
| systemd | enabled for you | **yours to enable** |
| Who it is for | anyone running a node on Windows | development, auditing, or a distro you already keep |

**Use the installer unless you have a reason not to.** The manual path is written
out below because the installer is a script someone has to be able to read, repair
and disagree with — not because it is the recommended route.

> **Both paths install from the `stable` branch, not from `dev`.** That is what
> pins them: a change merged to `dev` does not reach a Windows node until a
> release moves `stable`. See [What is not in a Windows node yet](#what-is-not-in-a-windows-node-yet)
> for what is currently waiting there.

---

# The installer

## Prerequisites

- **Windows 11** (build 22000+). The script warns and continues below that; the
  Hyper-V firewall rule it writes needs 22H2 or newer, and mirrored networking
  needs the same.
- **Hardware virtualization (VT-x / AMD-V)** enabled in BIOS/UEFI. This is the one
  check the script refuses to continue past.
- **Administrator privileges** (`#Requires -RunAsAdministrator`).

If WSL itself is missing, the script installs it with `wsl --install --no-distribution`
and stops, asking you to restart Windows and run it again.

## Running it

```powershell
powershell -ExecutionPolicy Bypass -File .\bash\install.ps1 -VerboseMode
```

`Nodo-Setup.exe` is the same script wrapped by PS2EXE with a GUI progress window.
It may lag `install.ps1` — it is rebuilt by hand, so when the two disagree the
script is the current one ([`RELEASING.md`](RELEASING.md)).

## What it actually does

Worth reading before running something as Administrator, and worth having written
down when a node misbehaves and the question is what state the host is in.

**On Windows:**

1. Downloads a custom WSL2 host kernel to `C:\wsl-kernel\bzImage`.
2. Merges three keys into `%USERPROFILE%\.wslconfig` under `[wsl2]`, preserving
   everything else in the file and backing it up to `.wslconfig.old` first:
   ```ini
   nestedVirtualization=true
   kernel=C:\\wsl-kernel\\bzImage
   networkingMode=mirrored
   ```
   `nestedVirtualization` is what gives the distro `/dev/kvm`. `networkingMode=mirrored`
   is what makes a port opened inside the distro reachable on the host's own address.
3. **Unregisters any existing distro named `Nodo`** — it is non-interactive, and
   that is how it stays so. A distro of that name is destroyed without a prompt.
4. Imports the `Nodo` distro from a published Debian rootfs into `C:\WSL\Nodo`.
5. Adds a Hyper-V firewall rule `WSL-Allow-All` (inbound, allow) for the WSL VM
   creator. Skipped with a warning on builds whose PowerShell has no
   `New-NetFirewallHyperVRule`.
6. Adds a Windows route for `192.168.200.0/24` — the microVM subnet — via the
   distro's IP, so the host can reach the guests a node launches.
7. Creates a `Nodo Terminal` desktop shortcut (`wsl.exe -d Nodo --cd ~`).

**Inside the distro:**

8. Writes `/etc/wsl.conf` with `systemd=true` and `default=root`. **The node needs
   both**: `install.sh` installs a systemd unit, and Cloud Hypervisor networking
   needs root.
9. Installs `git curl sudo iptables bc`, sets the hostname to `Nodo`.
10. Downloads the paired `vmlinuz` and `initramfs` to `/boot`. These belong to the
    WSL2 host kernel and are **not** the Cloud Hypervisor guest kernel, which
    `install.sh` fetches separately into `/nodo/cloud_hypervisor/`.
11. Runs the standard installer, from `stable`:
    ```bash
    curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/stable/install.sh | sudo bash
    ```
12. Points `network.EXTERNAL_INTERFACE` in `/nodo/config.yaml` at the distro's
    default-route interface, and enables IP forwarding plus `FORWARD` accepts for
    the microVM subnets, persisted through an `iptables-restore` unit.

> ⚠️ **It ends by making `/nodo` world-writable** (`chmod 777` over every file and
> directory). `config.yaml` holds the wallet mnemonic. On a single-user Windows
> laptop that is mostly theoretical; on a shared machine it is not, and it is worth
> tightening by hand afterwards.

## After it finishes

Open the `Nodo Terminal` shortcut, then:

```bash
sudo nodo doctor        # every check should be [OK]
sudo nodo daemon start
sudo nodo daemon status
```

---

# Installing by hand

For development, for auditing what the installer does, or to use a distro you
already keep. You are responsible for the two things the installer would have
done for you: **systemd** and **networking**.

## 1. A WSL2 distro

```powershell
wsl --install -d Ubuntu-22.04
wsl -l -v                              # VERSION must be 2
wsl --set-version Ubuntu-22.04 2       # if it says 1
```

## 2. Enable systemd and root — before installing

`install.sh` writes and starts a systemd unit unconditionally: it calls
`systemctl daemon-reload`, `enable` and `start` with no fallback and no `set -e`.
Without systemd the install limps to the end and prints
`Error: nodo.service does not exist or cannot be restarted`, leaving a node that
never comes up on its own.

```bash
sudo tee /etc/wsl.conf << 'EOF'
[boot]
systemd=true

[user]
default=root
EOF
```

Then, from PowerShell:

```powershell
wsl --shutdown
wsl -d Ubuntu-22.04
```

Confirm before going further — `systemctl is-system-running` must not answer
`offline`:

```bash
systemctl is-system-running    # "running" or "degraded" are both fine
```

## 3. Give the distro KVM

In `%USERPROFILE%\.wslconfig`:

```ini
[wsl2]
nestedVirtualization=true
```

`wsl --shutdown` to apply. Without this there is no `/dev/kvm` and no service will
ever execute.

## 4. Install

```bash
sudo apt update && sudo apt install -y curl
```

Then **clone and run** rather than piping:

```bash
git clone https://github.com/celaut-project/nodo.git /tmp/nodo
sudo bash /tmp/nodo/install.sh
```

Piping the installer into `bash` works, but stdin is then the pipe rather than a
terminal, and the installer skips every question it would otherwise ask — silently.
Today that costs nothing. It will not stay that way; see
[What is not in a Windows node yet](#what-is-not-in-a-windows-node-yet).

To pipe anyway, answer the questions with the environment instead:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/stable/install.sh \
  | sudo NODO_DONATION_PERCENTAGE=0.02 bash
```

The install downloads a portable Python 3.11 and JRE 21, builds a venv, fetches
Cloud Hypervisor `v51.1` with its guest kernel and initramfs, and installs the
`nodo` systemd unit. It takes several minutes and is idempotent — re-run it if it
fails on a network timeout.

> ⚠️ **Known issue (Ubuntu 22.04):** venv creation can fail with an `ensurepip`
> error. If it does:
> ```bash
> sudo rm -rf /nodo/venv
> /nodo/runtime/python/current/bin/python3 -m venv /nodo/venv
> /nodo/venv/bin/pip install --upgrade pip
> /nodo/venv/bin/pip install -r /nodo/bash/requirements.txt
> ```
> Then re-run the install script.

## 5. Verify

```bash
sudo nodo doctor
```

Expect all `[OK]`, including:

```
Virtualization checks (Cloud Hypervisor/KVM):
[OK] CPU virtualization flags detected (vmx/svm matches: ...).
[OK] KVM kernel modules appear to be loaded.
[OK] /dev/kvm exists ...
...
Cloud Hypervisor KVM smoke test:
[OK] Cloud Hypervisor vCPU is running (process alive after 2s).
```

If `/dev/kvm` is missing: `nestedVirtualization=true` (step 3), virtualization
enabled in BIOS, WSL2 rather than WSL1, and Hyper-V + Virtual Machine Platform
enabled in Windows features.

## 6. Start

```bash
sudo nodo daemon start
sudo nodo daemon status
```

---

# Reaching the node from outside

**This is the part WSL2 does not give you for free, and the part a manual install
has to finish by hand.** A node that only talks to itself does not need it; a node
that peers, or that publishes a service port, does.

`install.sh` assigns a gateway port and writes a firewall rule for it *inside* the
distro. Under WSL2's default NAT that rule is real and still unreachable: the
distro sits behind a virtual switch with its own address, so nothing on the LAN
can open that port. Two ways out.

## Mirrored networking (what the installer uses)

Windows 11 22H2 and newer. In `%USERPROFILE%\.wslconfig`:

```ini
[wsl2]
networkingMode=mirrored
```

The distro then shares the host's network interfaces, and a port bound to
`0.0.0.0` inside it is reachable at the host's own address — no forwarding to
maintain and no address that changes on reboot.

Mirrored mode puts WSL traffic behind the **Hyper-V firewall**, which is a
separate stack from both the Windows Defender firewall and the distro's own
nftables. Inbound is filtered there and has to be allowed explicitly:

```powershell
$vmCreator = (Get-NetFirewallHyperVVMCreator | Where-Object { $_.FriendlyName -eq 'WSL' }).VMCreatorId

New-NetFirewallHyperVRule -Name "WSL-Allow-Nodo" -DisplayName "Nodo gateway (WSL)" `
  -VMCreatorId $vmCreator -Direction Inbound -Action Allow -Protocol TCP -LocalPorts <GATEWAY_PORT>
```

The installer writes the same rule without `-Protocol`/`-LocalPorts`, i.e. allow
all inbound to the VM. Naming the port is the narrower version and is what to
prefer on a machine that is not solely a node.

`wsl --shutdown` to apply.

## Port forwarding (NAT mode, or Windows 10)

Keep the default NAT and forward each port from Windows into the distro:

```powershell
$wslIp = (wsl -d <distro> -- hostname -I).Trim().Split(" ")[0]
netsh interface portproxy add v4tov4 listenport=<GATEWAY_PORT> listenaddress=0.0.0.0 `
  connectport=<GATEWAY_PORT> connectaddress=$wslIp
netsh advfirewall firewall add rule name="Nodo gateway" dir=in action=allow `
  protocol=TCP localport=<GATEWAY_PORT>
```

**The distro's IP changes on most restarts**, so this has to be re-run — a startup
task, or mirrored mode instead. That impermanence is the reason the installer does
not use this route.

## Which port

Whatever `network.GATEWAY_PORT` resolved to in `/nodo/config.yaml`. `install.sh`
assigns it at install time and prints the result as its last line; afterwards:

```bash
/nodo/bin/yq '.network.GATEWAY_PORT' /nodo/config.yaml
```

Published service ports come out of `network.FREE_PORTS_RANGE` and need the same
treatment — narrow the range first, or the rule you have to write is enormous. See
[`FIREWALL.md`](FIREWALL.md) and [`TUNNELING.md`](TUNNELING.md), the latter being
how to reach a service **without** opening anything.

---

# What is not in a Windows node yet

Both installation paths pull `install.sh` from **`stable`**. `dev` has since gained
two things that change what an install does, and neither is live on Windows until
the next release moves `stable`. Written down here because on the day it moves,
they land on Windows without either path changing a line:

- **A donation prompt.** `install.sh` on `dev` asks what share of earnings to
  donate, defaulting to 2 %. It asks only on a terminal (`[ -t 0 ]`), and **both
  Windows paths install through a pipe** — the installer's in-distro script pipes
  `curl` into `bash`, and under `Nodo-Setup.exe` there is no console at all. The
  question will be skipped and the default kept, silently. The commit that added it
  argues "a default nobody is told about is not consent"; a Windows user is exactly
  that nobody. Passing `NODO_DONATION_PERCENTAGE` names the share without a prompt
  and is what the installer should do — until then, check
  `ledgers.ergo.payments.DONATION_PERCENTAGE` in `config.yaml` after installing.
- **Gateway port assignment.** `install.sh` on `dev` picks the port during the
  install and prints an operator notice as its final line, saying what still has to
  be opened. Piped and GUI-wrapped, that notice has nowhere to go. It is the same
  instruction as [Reaching the node](#reaching-the-node-from-outside).

Also on `dev`: `builder.ARM_SUPPORT` and `builder.X86_SUPPORT` were removed and are
now **refused** at startup. A `config.yaml` kept from an older node — or baked into
an exported distro image — stops that node from booting until those keys are
deleted. Architecture support is derived from the host now, not declared.

---

# Distributing a configured distro

`Nodo-Setup.exe` is built by compiling `bash/install.ps1` with PS2EXE, and the
rootfs, kernel and initramfs it downloads are release assets. The whole process —
building the rootfs, publishing the assets, rebuilding the `.exe` — is in
[`RELEASING.md`](RELEASING.md).

To hand someone a distro you configured yourself rather than the published one:

```powershell
wsl --export Nodo nodo-distro.tar
```

They import it with `wsl --import <name> <install-dir> nodo-distro.tar`. Two things
travel with that tarball and are worth checking first: the `config.yaml` inside it
(**including the wallet mnemonic** — regenerate it, do not ship yours), and whether
it predates the removal of `ARM_SUPPORT`/`X86_SUPPORT` above.

---

# Troubleshooting

| Problem | Solution |
|---------|----------|
| `System has not been booted with systemd` | `[boot] systemd=true` in `/etc/wsl.conf`, then `wsl --shutdown` |
| `nodo.service does not exist or cannot be restarted` | Same cause: systemd was off during the install. Enable it, then re-run `install.sh` |
| `ensurepip` fails during install | See the workaround in step 4 above |
| `/dev/kvm` not found | `nestedVirtualization=true` in `.wslconfig`; VT-x/AMD-V in BIOS; WSL2 not WSL1; Hyper-V enabled |
| `nodo doctor` shows kernel incompatible | `wsl --update` from PowerShell |
| Install fails with network errors | Re-run it — the script is idempotent |
| WSL version is 1 instead of 2 | `wsl --set-version <distro> 2` |
| Peers cannot reach the node | [Reaching the node](#reaching-the-node-from-outside) — under NAT the port is open inside the distro and unreachable from the LAN |
| Reachable from Windows but not from the LAN | Mirrored mode is on but the Hyper-V firewall is still filtering inbound; add the rule above |
| Port forwarding stopped working after a reboot | The distro's IP changed. Re-run the `netsh` commands, or switch to mirrored |
| Node refuses to start naming `ARM_SUPPORT` / `X86_SUPPORT` | Delete both keys from `config.yaml`; support is derived from the host now |
