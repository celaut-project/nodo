#Requires -RunAsAdministrator

# ====================== PARAMETERS ======================
param(
    [switch]$VerboseMode
)

# Suppress streams that ps2exe converts into popups
$ErrorActionPreference = "SilentlyContinue"
$WarningPreference = "SilentlyContinue"
$VerbosePreference = "SilentlyContinue"
$ProgressPreference = "SilentlyContinue"
$InformationPreference = "SilentlyContinue"

<#
.SYNOPSIS
    Non-interactive installation and configuration script for WSL2 + Nodo on Windows 11

.DESCRIPTION
    This script automates the full installation of WSL2 with a custom kernel
    and a WSL distribution named Nodo based on a clean Debian image. The process does not
    require user interaction and always creates the requested user/password.

.NOTES
    Author: Automated Setup
    Requirements: Windows 11, Administrator privileges

.COMPILE
    Invoke-ps2exe "C:\Users\josem\Desktop\project\install.ps1" "C:\Users\josem\Desktop\Nodo-Setup.exe" `
        -requireAdmin `
        -noConsole `
        -iconFile "C:\Users\josem\Desktop\project\favicon.ico" `
        -title "Nodo Installer" `
        -version "1.0.0" `
        -company "Celaut Project"

.RUN
    powershell -ExecutionPolicy Bypass -File "C:\Users\josem\Desktop\project\install.ps1" -VerboseMode
#>


# ====================== UI: LOADING SCREEN ======================
[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null
[System.Reflection.Assembly]::LoadWithPartialName("System.Drawing") | Out-Null
[System.Windows.Forms.Application]::EnableVisualStyles()

$form = New-Object System.Windows.Forms.Form
$form.Text = "Nodo WSL Installer"
$form.Size = New-Object System.Drawing.Size(520, 320)
$form.StartPosition = "CenterScreen"
$form.FormBorderStyle = "FixedSingle"
$form.MaximizeBox = $false
$form.MinimizeBox = $false
$form.ControlBox = $false
$form.BackColor = [System.Drawing.Color]::FromArgb(18, 18, 18)
$form.ShowInTaskbar = $true
$form.TopMost       = $false

$titleLabel = New-Object System.Windows.Forms.Label
$titleLabel.Text = "Nodo WSL Installer"
$titleLabel.Font = New-Object System.Drawing.Font("Segoe UI", 16, [System.Drawing.FontStyle]::Bold)
$titleLabel.ForeColor = [System.Drawing.Color]::White
$titleLabel.Size = New-Object System.Drawing.Size(480, 36)
$titleLabel.Location = New-Object System.Drawing.Point(20, 24)

$statusLabel = New-Object System.Windows.Forms.Label
$statusLabel.Text = "Starting..."
$statusLabel.Font = New-Object System.Drawing.Font("Segoe UI", 10)
$statusLabel.ForeColor = [System.Drawing.Color]::FromArgb(180, 180, 180)
$statusLabel.Size = New-Object System.Drawing.Size(480, 24)
$statusLabel.Location = New-Object System.Drawing.Point(20, 80)

$progressBar = New-Object System.Windows.Forms.ProgressBar
$progressBar.Minimum = 0
$progressBar.Maximum = 100
$progressBar.Value = 0
$progressBar.Size = New-Object System.Drawing.Size(472, 18)
$progressBar.Location = New-Object System.Drawing.Point(20, 116)
$progressBar.Style = "Continuous"

$logBox = New-Object System.Windows.Forms.RichTextBox
$logBox.Size = New-Object System.Drawing.Size(472, 110)
$logBox.Location = New-Object System.Drawing.Point(20, 150)
$logBox.BackColor = [System.Drawing.Color]::FromArgb(30, 30, 30)
$logBox.ForeColor = [System.Drawing.Color]::FromArgb(140, 140, 140)
$logBox.Font = New-Object System.Drawing.Font("Consolas", 8)
$logBox.ReadOnly = $true
$logBox.BorderStyle = "None"
$logBox.ScrollBars = "Vertical"

Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinAPI {
    [DllImport("user32.dll")]
    public static extern int SendMessage(IntPtr hWnd, int Msg, int wParam, int lParam);
    [DllImport("user32.dll")]
    public static extern bool ReleaseCapture();
}
"@

$form.Add_MouseDown({
    if ($_.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        [WinAPI]::ReleaseCapture()
        [WinAPI]::SendMessage($form.Handle, 0xA1, 0x2, 0)
    }
})

$titleLabel.Add_MouseDown({
    if ($_.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        [WinAPI]::ReleaseCapture()
        [WinAPI]::SendMessage($form.Handle, 0xA1, 0x2, 0)
    }
})

$form.Controls.AddRange(@($titleLabel, $statusLabel, $progressBar, $logBox))
$form.Show()
[System.Windows.Forms.Application]::DoEvents()

# ====================== LOG FUNCTIONS ======================
function Update-UI {
    param(
        [string]$Status,
        [int]$Progress,
        [string]$Log = "",
        [System.Drawing.Color]$Color = [System.Drawing.Color]::FromArgb(180, 180, 180)
    )

    if ($Status) {
        $statusLabel.Text = $Status
    }

    if ($PSBoundParameters.ContainsKey('Progress')) {
        $progressBar.Value = [Math]::Min([Math]::Max($Progress, 0), 100)
    }

    if ($Log) {
        $logBox.SelectionStart = $logBox.TextLength
        $logBox.SelectionLength = 0
        $logBox.SelectionColor = $Color
        $logBox.AppendText("$Log`r`n")
        $logBox.ScrollToCaret()
    }

    [System.Windows.Forms.Application]::DoEvents()
}

function Write-Info {
    param($msg)
    Update-UI -Log $msg
}

function Write-Success {
    param($msg)
    Update-UI -Log $msg -Color ([System.Drawing.Color]::FromArgb(80, 200, 80))
}

function Write-Warning {
    param($msg)
    Update-UI -Log "[!] $msg" -Color ([System.Drawing.Color]::FromArgb(255, 200, 0))
}

function Write-Err {
    param($msg)
    Update-UI -Log "[ERROR] $msg" -Color ([System.Drawing.Color]::FromArgb(255, 80, 80))
}

# =============================================
# Single-instance guard
# =============================================
$mutexName = "Global\NodoWSL-Installer-Mutex"
$createdNew = $false

try {
    $mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
    if (-not $createdNew) {
        $form.Close()
        [System.Windows.Forms.MessageBox]::Show(
            "Nodo installer is already running.`nPlease wait for the current installation to finish.",
            "Nodo WSL Installer",
            [System.Windows.Forms.MessageBoxButtons]::OK,
            [System.Windows.Forms.MessageBoxIcon]::Warning
        ) | Out-Null
        exit 1
    }
}
catch {
    Write-Warning "Could not verify whether another instance is running."
}

$ErrorActionPreference = "Stop"
$Verbose = $VerboseMode.IsPresent

# ======================== CONSTANTS ================================

$DistroName = "Nodo"
$KernelDir = "C:\wsl-kernel"
$KernelPath = Join-Path $KernelDir "bzImage"
$KernelUrl = "https://github.com/celaut-project/nodo/releases/download/v1/bzImage"
$NodoBaseDir = "C:\WSL\Nodo"
$NodoImageDir = "C:\WSL\Images"
$NodoRootfsPath = Join-Path $NodoImageDir "debian-nodo.tar"
$NodoRootfsUrl = "https://github.com/celaut-project/nodo/releases/download/v1/debian.tar"

function Get-RegisteredDistros {
    $output = wsl --list --quiet *>&1
    if ($LASTEXITCODE -ne 0) {
        return @()
    }

    return @(
        $output |
        ForEach-Object { $_.ToString().Trim() } |
        Where-Object { $_ }
    )
}

function Remove-DistroIfExists([string]$Name) {
    $distros = Get-RegisteredDistros
    if ($distros -contains $Name) {
        Write-Warning "Distribution '$Name' already exists. It will be removed to continue in non-interactive mode..."
        wsl --shutdown *>&1 | Out-Null
        wsl --unregister $Name *>&1 | Out-Null
        Write-Success "[OK] Distribution '$Name' removed"
    }
}

function Invoke-Wsl([string]$Command) {
    # Temporarily suspend Stop preference so WSL stderr
    # (e.g. git progress messages) does not throw NativeCommandError
    $prevPref = $ErrorActionPreference
    $ErrorActionPreference = "Continue"

    $output = & wsl -d $DistroName -u root -- bash -lc $Command *>&1 | Out-String

    $ErrorActionPreference = $prevPref

    if ($LASTEXITCODE -ne 0) {
        throw "Failed to run command in WSL:`n$output"
    }

    if ($output.Trim()) {
        foreach ($line in ($output -split "`r?`n")) {
            if ($line.Trim()) {
                Write-Info $line.TrimEnd()
            }
        }
    }
}

Write-Info "======================================================================="
Write-Info "  WSL2 + Windows 11 + Nodo Setup - Automated Installation Script"
Write-Info "======================================================================="
Write-Info ""

Update-UI -Status "Checking system requirements..." -Progress 10
Write-Info "[STEP 1/8] Checking system requirements..."

$osInfo = Get-CimInstance -ClassName Win32_OperatingSystem
$buildNumber = [int]$osInfo.BuildNumber

Write-Info "Detected operating system: $($osInfo.Caption)"
Write-Info "Build number: $buildNumber"

if ($buildNumber -lt 22000) {
    Write-Warning "Windows 11 (build 22000+) is recommended. Continuing automatically..."
}

$hyperV = Get-CimInstance -ClassName Win32_ComputerSystem | Select-Object -ExpandProperty HypervisorPresent
if (-not $hyperV) {
    Write-Err "ERROR: Virtualization is not enabled in BIOS/UEFI"
    Write-Err "Enable Intel VT-x or AMD-V in BIOS/UEFI and run the script again."
    exit 1
}
Write-Success "[OK] Virtualization enabled"

try {
    wsl --version *>&1 | Out-Null
    Write-Success "[OK] WSL installed"
}
catch {
    Write-Warning "WSL is not installed. Installing WSL without a distribution..."
    wsl --install --no-distribution *>&1 | Out-Null
    Write-Info "WSL installed. Restart Windows and run this script again."
    exit 0
}

Write-Info "Setting WSL2 as the default version..."
wsl --set-default-version 2 *>&1 | Out-Null
Write-Success "[OK] WSL2 set as default"

Write-Info "Current WSL status:"
wsl --status *>&1 | Out-Null
Write-Info "[OK] WSL status queried"

Write-Info ""
Update-UI -Status "Downloading custom kernel..." -Progress 25
Write-Info "[STEP 2/8] Downloading and installing custom kernel..."

if (-not (Test-Path $KernelDir)) {
    New-Item -Path $KernelDir -ItemType Directory -Force | Out-Null
    Write-Success "[OK] Directory created: $KernelDir"
}

if (Test-Path $KernelPath) {
    $fileInfo = Get-Item $KernelPath
    Write-Info "Existing kernel detected at: $KernelPath"
    Write-Info "Current size: $([math]::Round($fileInfo.Length / 1MB, 2)) MB"
    Write-Info "Last modified: $($fileInfo.LastWriteTime)"
    Write-Info "Shutting down WSL to replace the kernel..."
    wsl --shutdown *>&1 | Out-Null
    Start-Sleep -Seconds 5
}

Write-Info "Downloading kernel from: $KernelUrl"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $tempKernelPath = Join-Path $KernelDir "bzImage.tmp"
    Invoke-WebRequest -Uri $KernelUrl -OutFile $tempKernelPath -UseBasicParsing

    if (Test-Path $KernelPath) {
        $backupPath = Join-Path $KernelDir "bzImage.backup"
        if (Test-Path $backupPath) {
            Remove-Item $backupPath -Force
        }
        Move-Item $KernelPath $backupPath -Force
        Write-Info "Previous kernel backed up at: $backupPath"
    }

    Move-Item $tempKernelPath $KernelPath -Force
    Write-Success "[OK] Kernel downloaded: $KernelPath"
}
catch {
    Write-Err "ERROR downloading kernel: $_"
    exit 1
}

Write-Info ""
Update-UI -Status "Configuring WSL2..." -Progress 35
Write-Info "[STEP 3/8] Configuring WSL2 settings..."

$wslConfigPath = Join-Path $env:USERPROFILE ".wslconfig"
$wslConfigContent = @"
[wsl2]
nestedVirtualization=true
kernel=C:\\wsl-kernel\\bzImage
"@

Set-Content -Path $wslConfigPath -Value $wslConfigContent -Force
Write-Success "[OK] .wslconfig created/updated at: $wslConfigPath"
Write-Info "Applied content:"
Write-Info $wslConfigContent

Write-Info "Shutting down WSL to apply configuration..."
wsl --shutdown *>&1 | Out-Null
Start-Sleep -Seconds 3
Write-Success "[OK] Configuration applied"

Write-Info ""
Update-UI -Status "Creating Nodo distribution..." -Progress 50
Write-Info "[STEP 4/8] Creating the $DistroName distribution from the published Debian image..."

Remove-DistroIfExists -Name $DistroName

New-Item -ItemType Directory -Path $NodoBaseDir -Force | Out-Null
New-Item -ItemType Directory -Path $NodoImageDir -Force | Out-Null

if (Test-Path $NodoRootfsPath) {
    Remove-Item -Path $NodoRootfsPath -Force
}

    Write-Info "Downloading Debian rootfs from: $NodoRootfsUrl"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $NodoRootfsUrl -OutFile $NodoRootfsPath -UseBasicParsing
    Write-Success "[OK] Rootfs downloaded to: $NodoRootfsPath"
}
catch {
    Write-Err "ERROR downloading Debian rootfs: $_"
    exit 1
}

Write-Info "Importing distribution $DistroName..."
wsl --import $DistroName $NodoBaseDir $NodoRootfsPath --version 2 *>&1 | Out-Null
Write-Success "[OK] Distribution $DistroName created from the published Debian image"

Write-Info ""
Update-UI -Status "Configuring internal environment..." -Progress 65
Write-Info "[STEP 5/8] Configuring user, password, and internal environment..."

$wslSetupScript = @'
#!/bin/bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

WSL_USER="user"
WSL_PASSWORD="password"

echo -e "${CYAN}=======================================================================${NC}"
echo -e "${CYAN}  WSL internal configuration - Nodo Setup${NC}"
echo -e "${CYAN}=======================================================================${NC}"

echo -e "\n${CYAN}[STEP 5.1] Creating default user...${NC}"
if ! id -u "${WSL_USER}" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "${WSL_USER}"
    echo -e "${GREEN}[OK] User ${WSL_USER} created${NC}"
else
    echo -e "${YELLOW}User ${WSL_USER} already exists${NC}"
fi

echo "${WSL_USER}:${WSL_PASSWORD}" | chpasswd

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq git curl sudo iptables bc
usermod -aG sudo "${WSL_USER}"

mkdir -p /etc
cat > /etc/wsl.conf <<EOF
[boot]
systemd=true

[user]
default=root
EOF
echo -e "${GREEN}[OK] Default user configured as '${WSL_USER}'${NC}"

echo "nodo" >> /root/.bashrc

echo -e "\n${CYAN}[STEP 5.2] Configuring hostname...${NC}"
echo "Nodo" > /etc/hostname
if grep -q '^127\.0\.1\.1' /etc/hosts 2>/dev/null; then
    sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tNodo/' /etc/hosts
else
    echo -e "127.0.1.1\tNodo" >> /etc/hosts
fi
echo -e "${GREEN}[OK] Hostname configured as 'Nodo'${NC}"

echo -e "\n${CYAN}[STEP 5.3] Downloading internal kernel...${NC}"
mkdir -p /boot
curl -sSL https://github.com/celaut-project/nodo/releases/download/v1/vmlinuz -o /boot/vmlinuz
curl -sSL https://github.com/celaut-project/nodo/releases/download/v1/initramfs -o /boot/initramfs
echo -e "${GREEN}[OK] vmlinuz and initramfs installed${NC}"

echo -e "\n${CYAN}[STEP 5.4] Installing Nodo system...${NC}"
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/stable/install.sh | bash
echo -e "${GREEN}[OK] Nodo system installed${NC}"

echo -e "\n${CYAN}[STEP 5.5] Adjusting Nodo permissions...${NC}"
mkdir -p /nodo/storage
touch /nodo/storage/app.log
chown -R "${WSL_USER}:${WSL_USER}" /nodo
find /nodo -type d -exec chmod 777 {} \;
find /nodo -type f -exec chmod 777 {} \;
chmod +x /nodo/nodo.py 2>/dev/null || true
echo -e "${GREEN}[OK] /nodo permissions configured for ${WSL_USER}${NC}"

echo -e "\n${CYAN}[STEP 5.6] Configuring networking for the microVM...${NC}"
iptables -C FORWARD -d 192.168.200.0/24 -j ACCEPT 2>/dev/null || iptables -A FORWARD -d 192.168.200.0/24 -j ACCEPT
iptables -C FORWARD -s 192.168.200.0/24 -j ACCEPT 2>/dev/null || iptables -A FORWARD -s 192.168.200.0/24 -j ACCEPT
mkdir -p /etc/iptables
iptables-save > /etc/iptables/rules.v4

cat > /etc/systemd/system/iptables-restore.service <<EOF
[Unit]
Description=Restore iptables rules
Before=network-pre.target

[Service]
Type=oneshot
ExecStart=/sbin/iptables-restore /etc/iptables/rules.v4

[Install]
WantedBy=multi-user.target
EOF

systemctl enable iptables-restore.service 2>/dev/null || true

echo -e "\n${GREEN}=======================================================================${NC}"
echo -e "${GREEN}  [OK] Configuration completed successfully${NC}"
echo -e "${GREEN}=======================================================================${NC}"
'@

$tempScriptPath = Join-Path $env:TEMP "wsl-nodo-setup.sh"
Set-Content -Path $tempScriptPath -Value $wslSetupScript -Force -Encoding UTF8
Write-Info "Temporary configuration script created at: $tempScriptPath"

Write-Info "Copying and running configuration inside WSL..."
$setupScriptBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($wslSetupScript))
Invoke-Wsl "echo $setupScriptBase64 | base64 -d > /tmp/setup.sh && chmod +x /tmp/setup.sh && /tmp/setup.sh"
Write-Success "[OK] Internal configuration completed"

Write-Info ""
Update-UI -Status "Configuring network..." -Progress 80
Write-Info "[STEP 6/8] Configuring Windows-to-WSL network routing..."

try {
    $wslIP = & wsl -d $DistroName -- hostname -I *>&1 | Out-String
    $wslIP = ($wslIP -split " ")[0].Trim()

    if ($wslIP -and $wslIP -match '^\d+\.\d+\.\d+\.\d+$') {
        Write-Info "Detected WSL IP: $wslIP"

        try {
            route delete 192.168.200.0 *>&1 | Out-Null
            route add 192.168.200.0 mask 255.255.255.0 $wslIP *>&1 | Out-Null
            Write-Success "[OK] Route added: 192.168.200.0/24 -> $wslIP"
        }
        catch {
            Write-Warning "Could not configure the Windows route: $_"
        }
    }
    else {
        Write-Warning "Could not obtain a valid IP from distribution $DistroName"
    }
}
catch {
    Write-Warning "Error getting WSL IP: $_"
}

Write-Info ""
Update-UI -Status "Finalizing installation..." -Progress 95
Write-Info "[STEP 7/8] Shutting down WSL to apply the default user and systemd..."
wsl --shutdown *>&1 | Out-Null
Write-Success "[OK] WSL shut down"


# === CREATE DESKTOP SHORTCUT ===
Write-Info ""
Write-Info "[FINAL] Creating desktop shortcut..."

$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "Nodo Terminal.lnk"

try {
    $WScriptShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WScriptShell.CreateShortcut($ShortcutPath)

    $Shortcut.TargetPath      = "wsl.exe"
    $Shortcut.Arguments       = "-d Nodo --cd ~"
    $Shortcut.WorkingDirectory = "%USERPROFILE%"
    $Shortcut.Description     = "Open Nodo Terminal (WSL2)"
    $Shortcut.IconLocation    = "C:\Windows\System32\wsl.exe,0"  # "$PSScriptRoot\Nodo-Setup.exe,0" (does not work reliably)
    $Shortcut.Save()

    Write-Success "Desktop shortcut created: Nodo Terminal.lnk"
}
catch {
    Write-Warning "Could not create the desktop shortcut."
}

# ====================== FINAL MESSAGE ======================
Update-UI -Status "Installation completed" -Progress 100
Write-Success "[OK] Installation completed successfully"
Start-Sleep -Seconds 1
$form.Close()

[System.Windows.Forms.MessageBox]::Show(
    "Nodo installation completed successfully.`n`nDesktop shortcut created.",
    "Nodo WSL Installer - Success",
    [System.Windows.Forms.MessageBoxButtons]::OK,
    [System.Windows.Forms.MessageBoxIcon]::Information
) | Out-Null

# Launch Nodo terminal  TODO Se abre, pero al presionar q se cierra ¿?
#Start-Process "wsl.exe" -ArgumentList "-d Nodo --cd ~ -- nodo"

# Release mutex
if ($mutex) { $mutex.ReleaseMutex() }
