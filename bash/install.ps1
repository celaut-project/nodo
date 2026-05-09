#Requires -RunAsAdministrator

# =============================================
# Prevención de ejecución múltiple
# =============================================
$mutexName = "Global\NodoWSL-Installer-Mutex"
$createdNew = $false

try {
    $mutex = New-Object System.Threading.Mutex($true, $mutexName, [ref]$createdNew)
    
    if (-not $createdNew) {
        # Usamos Write-Host directamente porque las funciones aún no están definidas
        Write-Host "================================================================" -ForegroundColor Yellow
        Write-Host "El instalador de Nodo ya se está ejecutando." -ForegroundColor Yellow
        Write-Host "Espera a que termine la instalación actual." -ForegroundColor Yellow
        Write-Host "================================================================" -ForegroundColor Yellow
        Start-Sleep -Seconds 4
        exit 1
    }
}
catch {
    Write-Host "No se pudo verificar si ya hay otra instancia ejecutándose." -ForegroundColor Yellow
}

<#
.SYNOPSIS
    Script de instalacion y configuracion no interactiva de WSL2 + Nodo en Windows 11

.DESCRIPTION
    Este script automatiza la instalacion completa de WSL2 con kernel personalizado
    y una distro WSL llamada Nodo basada en un Debian limpio. El proceso no requiere
    interaccion del usuario y siempre crea el usuario/password solicitados.

.NOTES
    Autor: Setup Automatizado
    Requisitos: Windows 11, Permisos de Administrador

.COMPILE
    Invoke-ps2exe "C:\Users\josem\Desktop\project\install.ps1" "C:\Users\josem\Desktop\Nodo-Setup.exe" `
        -requireAdmin `
        -noConsole `
        -iconFile "C:\Users\josem\Desktop\nodo.ico" `
        -title "Nodo WSL Installer" `
        -version "1.0.0" `
        -company "Celaut Project"
#>

$ErrorActionPreference = "Stop"

# ====================== PARÁMETROS ======================
param(
    [switch]$VerboseMode
)

$Verbose = $VerboseMode.IsPresent

# ====================== FUNCIONES DE LOG ======================
function Write-Info     { param($msg) if ($Verbose) { Write-Host "[INFO] $msg" -ForegroundColor Cyan } }
function Write-Success  { param($msg) if ($Verbose) { Write-Host "[OK]  $msg" -ForegroundColor Green } }
function Write-Warning  { param($msg) Write-Host "[!]  $msg" -ForegroundColor Yellow }
function Write-Err      { param($msg) Write-Host "[ERROR] $msg" -ForegroundColor Red }

# ======================== CONSTANTES ================================

$DistroName = "Nodo"
$DistroUser = "user"
$DistroPassword = "password"
$KernelDir = "C:\wsl-kernel"
$KernelPath = Join-Path $KernelDir "bzImage"
$KernelUrl = "https://github.com/celaut-project/nodo/releases/download/v1/bzImage"
$NodoBaseDir = "C:\WSL\Nodo"
$NodoImageDir = "C:\WSL\Images"
$NodoRootfsPath = Join-Path $NodoImageDir "debian-nodo.tar"
$NodoRootfsUrl = "https://github.com/celaut-project/nodo/releases/download/v1/debian.tar"

function Get-RegisteredDistros {
    $output = wsl --list --quiet 2>&1
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
        Write-Warning "La distribucion '$Name' ya existe. Se eliminara para continuar en modo no interactivo..."
        wsl --shutdown
        wsl --unregister $Name
        Write-Success "[OK] Distribucion '$Name' eliminada"
    }
}

function Invoke-Wsl([string]$Command) {
    & wsl -d $DistroName -u root -- bash -lc $Command
    if ($LASTEXITCODE -ne 0) {
        throw "Fallo al ejecutar comando en WSL: $Command"
    }
}

Write-Info "======================================================================="
Write-Info "  WSL2 + Windows 11 + Nodo Setup - Script de Instalacion Automatizado"
Write-Info "======================================================================="
Write-Info ""

Write-Info "[PASO 1/8] Verificando requisitos del sistema..."

$osInfo = Get-CimInstance -ClassName Win32_OperatingSystem
$buildNumber = [int]$osInfo.BuildNumber

Write-Info "Sistema operativo detectado: $($osInfo.Caption)"
Write-Info "Numero de build: $buildNumber"

if ($buildNumber -lt 22000) {
    Write-Warning "Se recomienda Windows 11 (build 22000+). Continuando automaticamente..."
}

$hyperV = Get-CimInstance -ClassName Win32_ComputerSystem | Select-Object -ExpandProperty HypervisorPresent
if (-not $hyperV) {
    Write-Err "ERROR: La virtualizacion no esta habilitada en BIOS/UEFI"
    Write-Err "Habilite Intel VT-x o AMD-V en BIOS/UEFI y vuelva a ejecutar el script."
    exit 1
}
Write-Success "[OK] Virtualizacion habilitada"

try {
    $null = wsl --version 2>&1
    Write-Success "[OK] WSL instalado"
}
catch {
    Write-Warning "WSL no esta instalado. Instalando WSL sin distribucion..."
    wsl --install --no-distribution
    Write-Info "WSL instalado. Reinicie Windows y ejecute este script de nuevo."
    exit 0
}

Write-Info "Configurando WSL2 como version predeterminada..."
wsl --set-default-version 2
Write-Success "[OK] WSL2 configurado como predeterminado"

Write-Info "Estado actual de WSL:"
wsl --status

Write-Info ""
Write-Info "[PASO 2/8] Descargando e instalando kernel personalizado..."

if (-not (Test-Path $KernelDir)) {
    New-Item -Path $KernelDir -ItemType Directory -Force | Out-Null
    Write-Success "[OK] Directorio creado: $KernelDir"
}

if (Test-Path $KernelPath) {
    $fileInfo = Get-Item $KernelPath
    Write-Info "Kernel existente detectado en: $KernelPath"
    Write-Info "Tamano actual: $([math]::Round($fileInfo.Length / 1MB, 2)) MB"
    Write-Info "Fecha actual: $($fileInfo.LastWriteTime)"
    Write-Info "Apagando WSL para reemplazar el kernel..."
    wsl --shutdown
    Start-Sleep -Seconds 5
}

Write-Info "Descargando kernel desde: $KernelUrl"
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
        Write-Info "Kernel anterior respaldado en: $backupPath"
    }

    Move-Item $tempKernelPath $KernelPath -Force
    Write-Success "[OK] Kernel descargado: $KernelPath"
}
catch {
    Write-Err "ERROR al descargar el kernel: $_"
    exit 1
}

Write-Info ""
Write-Info "[PASO 3/8] Configurando settings de WSL2..."

$wslConfigPath = Join-Path $env:USERPROFILE ".wslconfig"
$wslConfigContent = @"
[wsl2]
nestedVirtualization=true
kernel=C:\\wsl-kernel\\bzImage
"@

Set-Content -Path $wslConfigPath -Value $wslConfigContent -Force
Write-Success "[OK] Archivo .wslconfig creado/actualizado en: $wslConfigPath"
Write-Info "Contenido aplicado:"
Write-Info $wslConfigContent

Write-Info "Apagando WSL para aplicar configuracion..."
wsl --shutdown
Start-Sleep -Seconds 3
Write-Success "[OK] Configuracion aplicada"

Write-Info ""
Write-Info "[PASO 4/8] Creando la distro $DistroName desde la imagen Debian publicada..."

Remove-DistroIfExists -Name $DistroName

New-Item -ItemType Directory -Path $NodoBaseDir -Force | Out-Null
New-Item -ItemType Directory -Path $NodoImageDir -Force | Out-Null

if (Test-Path $NodoRootfsPath) {
    Remove-Item -Path $NodoRootfsPath -Force
}

Write-Info "Descargando rootfs Debian desde: $NodoRootfsUrl"
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $NodoRootfsUrl -OutFile $NodoRootfsPath -UseBasicParsing
    Write-Success "[OK] Rootfs descargado en: $NodoRootfsPath"
}
catch {
    Write-Err "ERROR al descargar el rootfs Debian: $_"
    exit 1
}

Write-Info "Importando distro $DistroName..."
wsl --import $DistroName $NodoBaseDir $NodoRootfsPath --version 2
Write-Success "[OK] Distro $DistroName creada desde la imagen Debian publicada"

Write-Info ""
Write-Info "[PASO 5/8] Configurando usuario, password y entorno interno..."

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
echo -e "${CYAN}  Configuracion interna de WSL - Nodo Setup${NC}"
echo -e "${CYAN}=======================================================================${NC}"

echo -e "\n${CYAN}[PASO 5.1] Creando usuario por defecto...${NC}"
if ! id -u "${WSL_USER}" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "${WSL_USER}"
    echo -e "${GREEN}[OK] Usuario ${WSL_USER} creado${NC}"
else
    echo -e "${YELLOW}El usuario ${WSL_USER} ya existe${NC}"
fi

echo "${WSL_USER}:${WSL_PASSWORD}" | chpasswd

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y git curl sudo iptables bc
usermod -aG sudo "${WSL_USER}"

mkdir -p /etc
cat > /etc/wsl.conf <<EOF
[boot]
systemd=true

[user]
default=${WSL_USER}
EOF
echo -e "${GREEN}[OK] Usuario por defecto configurado como '${WSL_USER}'${NC}"

echo -e "\n${CYAN}[PASO 5.2] Configurando hostname...${NC}"
echo "Nodo" > /etc/hostname
if grep -q '^127\.0\.1\.1' /etc/hosts 2>/dev/null; then
    sed -i 's/^127\.0\.1\.1.*/127.0.1.1\tNodo/' /etc/hosts
else
    echo -e "127.0.1.1\tNodo" >> /etc/hosts
fi
echo -e "${GREEN}[OK] Hostname configurado como 'Nodo'${NC}"

echo -e "\n${CYAN}[PASO 5.3] Descargando kernel interno...${NC}"
mkdir -p /boot
curl -L https://github.com/celaut-project/nodo/releases/download/v1/vmlinuz -o /boot/vmlinuz
curl -L https://github.com/celaut-project/nodo/releases/download/v1/initramfs -o /boot/initramfs
echo -e "${GREEN}[OK] vmlinuz e initramfs instalados${NC}"

echo -e "\n${CYAN}[PASO 5.4] Instalando sistema Nodo...${NC}"
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/stable/install.sh | bash
echo -e "${GREEN}[OK] Sistema Nodo instalado${NC}"

echo -e "\n${CYAN}[PASO 5.5] Ajustando permisos de Nodo...${NC}"
mkdir -p /nodo/storage
touch /nodo/storage/app.log
chown -R "${WSL_USER}:${WSL_USER}" /nodo
find /nodo -type d -exec chmod 777 {} \;
find /nodo -type f -exec chmod 777 {} \;
chmod +x /nodo/nodo.py 2>/dev/null || true
echo -e "${GREEN}[OK] Permisos de /nodo configurados para ${WSL_USER}${NC}"

echo -e "\n${CYAN}[PASO 5.6] Configurando networking para microVM...${NC}"
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
echo -e "${GREEN}  [OK] Configuracion completada exitosamente${NC}"
echo -e "${GREEN}=======================================================================${NC}"
'@

$tempScriptPath = Join-Path $env:TEMP "wsl-nodo-setup.sh"
Set-Content -Path $tempScriptPath -Value $wslSetupScript -Force -Encoding UTF8
Write-Info "Script temporal de configuracion creado en: $tempScriptPath"

Write-Info "Copiando y ejecutando configuracion dentro de WSL..."
$setupScriptBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($wslSetupScript))
Invoke-Wsl "echo $setupScriptBase64 | base64 -d > /tmp/setup.sh && chmod +x /tmp/setup.sh && /tmp/setup.sh"
Write-Success "[OK] Configuracion interna completada"

Write-Info ""
Write-Info "[PASO 6/8] Configurando enrutamiento de red de Windows a WSL..."

try {
    $wslIP = & wsl -d $DistroName -- hostname -I 2>&1
    $wslIP = ($wslIP -split " ")[0].Trim()

    if ($wslIP -and $wslIP -match '^\d+\.\d+\.\d+\.\d+$') {
        Write-Info "IP de WSL detectada: $wslIP"

        try {
            route delete 192.168.200.0 2>$null | Out-Null
            route add 192.168.200.0 mask 255.255.255.0 $wslIP
            Write-Success "[OK] Ruta agregada: 192.168.200.0/24 -> $wslIP"
        }
        catch {
            Write-Warning "No se pudo configurar la ruta de Windows: $_"
        }
    }
    else {
        Write-Warning "No se pudo obtener una IP valida de la distro $DistroName"
    }
}
catch {
    Write-Warning "Error al obtener IP de WSL: $_"
}

Write-Info ""
Write-Info "[PASO 7/8] Apagando WSL para aplicar el usuario por defecto y systemd..."
wsl --shutdown
Write-Success "[OK] WSL apagado"


# === CREAR SHORTCUT EN ESCRITORIO ===
$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "Nodo Terminal.lnk"

try {
    $WScriptShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WScriptShell.CreateShortcut($ShortcutPath)

    $Shortcut.TargetPath      = "wsl.exe"
    $Shortcut.Arguments       = "-d Nodo --cd ~"
    $Shortcut.WorkingDirectory = "%USERPROFILE%"
    $Shortcut.Description     = "Terminal Nodo - WSL2"
    $Shortcut.IconLocation    = "C:\Windows\System32\wsl.exe,0"
    
    $Shortcut.Save()

    Write-Success "[OK] Acceso directo creado en el Escritorio"
}
catch {
    Write-Warning "No se pudo crear el acceso directo en el Escritorio."
}

# === CREAR SHORTCUT EN ESCRITORIO ===
Write-Info ""
Write-Info "[FINAL] Creando acceso directo en el Escritorio..."

$DesktopPath = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $DesktopPath "Nodo Terminal.lnk"

try {
    $WScriptShell = New-Object -ComObject WScript.Shell
    $Shortcut = $WScriptShell.CreateShortcut($ShortcutPath)

    $Shortcut.TargetPath      = "wsl.exe"
    $Shortcut.Arguments       = "-d Nodo --cd ~"
    $Shortcut.WorkingDirectory = "%USERPROFILE%"
    $Shortcut.Description     = "Abrir Terminal Nodo (WSL2)"
    $Shortcut.IconLocation    = "C:\Windows\System32\wsl.exe,0"
    $Shortcut.Save()

    Write-Success "Acceso directo creado: Nodo Terminal.lnk"
}
catch {
    Write-Warning "No se pudo crear el acceso directo en el Escritorio."
}

# ====================== MENSAJE FINAL ======================
[System.Reflection.Assembly]::LoadWithPartialName("System.Windows.Forms") | Out-Null

[System.Windows.Forms.MessageBox]::Show(
    "¡Instalación de Nodo completada exitosamente!`n`nAcceso directo creado en el Escritorio.`n`nUsuario: user`nPassword: password", 
    "Nodo WSL Installer - Éxito",
    [System.Windows.Forms.MessageBoxButtons]::OK,
    [System.Windows.Forms.MessageBoxIcon]::Information
) | Out-Null

# Liberar Mutex
if ($mutex) { $mutex.ReleaseMutex() }