# Nodo on WSL2 (Windows)

This guide covers two workflows:

1. **Installation** — Set up Nodo inside WSL2 on any Windows machine.
2. **Distribution** — Package the configured WSL distro as an `.appx` for one-click installation by end users.

---

# Installation

## Prerequisites

- **Windows 10 (build 19041+)** or **Windows 11**
- **WSL2** enabled. If not already installed:

```powershell
wsl --install
```

> Restart your machine if prompted. This installs WSL2 with the default Ubuntu distribution.

- **Hardware virtualization (VT-x / AMD-V)** must be enabled in BIOS/UEFI.

---

## 1️⃣ Install Ubuntu 22.04 on WSL2

If you already have Ubuntu 22.04 on WSL, skip this step.

```powershell
wsl --install -d Ubuntu-22.04
```

After installation, WSL will open the distro and ask you to create a UNIX user. Pick any username and password.

Verify it is running WSL2:

```powershell
wsl -l -v
```

If the VERSION column shows `1`, convert it:

```powershell
wsl --set-version Ubuntu-22.04 2
```

---

## 2️⃣ Enter the distro and install dependencies

```powershell
wsl -d Ubuntu-22.04
```

Inside the distro, install `curl` (needed by the installer):

```bash
sudo apt update && sudo apt install -y curl
```

---

## 3️⃣ Install Nodo

Run the official installer:

```bash
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/stable/install.sh | sudo bash
```

This will:
- Clone the Nodo repository to `/nodo`
- Install a portable Python 3.11, Java JRE 21, and yq
- Create a Python virtual environment with all dependencies
- Download Cloud Hypervisor v51 with a custom kernel (`vmlinuz`) and initramfs
- Set up the `nodo` systemd service

> ⏳ The installation takes several minutes. If it fails on the first run (e.g. network timeout), run it again — the script is idempotent.

> ⚠️ **Known issue (Ubuntu 22.04):** The Python virtual environment creation may fail with an `ensurepip` error. If this happens:
> ```bash
> sudo rm -rf /nodo/venv
> /nodo/runtime/python/current/bin/python3 -m venv /nodo/venv
> /nodo/venv/bin/pip install --upgrade pip
> /nodo/venv/bin/pip install -r /nodo/bash/requirements.txt
> ```
> Then re-run the install script.

---

## 4️⃣ Verify the installation

```bash
sudo nodo doctor
```

You should see all `[OK]` checks:

```
Virtualization checks (Cloud Hypervisor/KVM):
[OK] CPU virtualization flags detected (vmx/svm matches: ...).
[OK] KVM kernel modules appear to be loaded.
[OK] /dev/kvm exists ...
...
Cloud Hypervisor KVM smoke test:
[OK] Cloud Hypervisor vCPU is running (process alive after 2s).
```

If `/dev/kvm` is missing, make sure:
1. Hardware virtualization is enabled in BIOS.
2. You are running WSL2 (not WSL1).
3. Windows Hyper-V and Virtual Machine Platform features are enabled.

---

## 5️⃣ Start the Nodo daemon

```bash
sudo nodo daemon start
```

Check status:

```bash
sudo nodo daemon status
```

---

## 6️⃣ (Optional) Configure WSL to start as root

Nodo requires root to manage Cloud Hypervisor (networking, microVMs). You can configure the distro to default to root:

Create `/etc/wsl.conf` inside the distro:

```bash
sudo tee /etc/wsl.conf << 'EOF'
[user]
default=root
EOF
```

Then restart the distro from PowerShell:

```powershell
wsl --shutdown
wsl -d Ubuntu-22.04
```

---

# Distribution

To distribute a pre-configured Nodo WSL distro as a `.appx` / `.msixbundle` package that users can install with a double click:

---

## 1️⃣ Prepare and export the distro

After completing the installation above and verifying `nodo doctor` passes:

```powershell
wsl --export Ubuntu-22.04 nodo-distro.tar
```

This creates a portable tarball of the entire WSL filesystem.

---

## 2️⃣ Build the Appx package

Microsoft provides an open-source WSL distribution launcher template:

👉 [WSL-DistroLauncher (GitHub)](https://github.com/microsoft/WSL-DistroLauncher)

1. Clone the repository.
2. Open `DistroLauncher.sln` in **Visual Studio 2019 or 2022**.
3. Replace `install.tar.gz` in the project with your `nodo-distro.tar` (rename accordingly).
4. Edit the project configuration:
   - **Display name** → e.g. "Nodo WSL"
   - **Package identity** → unique name for your distribution
   - **Icons and metadata** as desired
5. Set build to **Release > x64**.
6. Build → **Project → Publish → Create App Packages**.
7. Select **Sideloading** (not Microsoft Store).
8. The output `.appx` or `.msixbundle` is ready for distribution.

---

## 3️⃣ User installation

End users only need to:

1. Enable WSL2 (if not already): `wsl --install` in PowerShell (admin).
2. Double-click the `.appx` / `.msixbundle` file.
3. The distro appears in the Start menu and can be launched like any app.

> **Note:** If the user has never enabled WSL, Windows will prompt them to enable it and may require a restart.

---

## 4️⃣ Post-install (for the end user)

After launching the distro for the first time:

```bash
sudo nodo doctor     # Verify everything is OK
sudo nodo daemon start  # Start the Nodo service
```

---

# Troubleshooting

| Problem | Solution |
|---------|----------|
| `ensurepip` fails during install | See the workaround in Step 3 above |
| `/dev/kvm` not found | Enable VT-x/AMD-V in BIOS, ensure WSL2 + Hyper-V enabled |
| `nodo doctor` shows kernel incompatible | Update WSL kernel: `wsl --update` from PowerShell |
| Install script fails with network errors | Re-run the install command — it is idempotent |
| WSL version is 1 instead of 2 | `wsl --set-version <distro-name> 2` |
