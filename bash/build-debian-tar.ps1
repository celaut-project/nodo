<#
.SYNOPSIS
    Exports a clean Ubuntu 22.04 rootfs as debian.tar for Nodo WSL2.

.DESCRIPTION
    debian.tar just needs to be an importable Ubuntu 22.04 rootfs. Everything
    else -- the default user, host packages (build-essential, git, curl,
    iptables, ...), systemd, Python, Cloud Hypervisor, the guest kernel, and
    the Nodo code itself -- is installed at install time by install.ps1 and
    bash/setup_linux_x86.sh, which are idempotent and self-sufficient. Baking
    any of that into this image only duplicates work and risks going stale
    (see the Nodo-code case this replaced: a pre-cloned repo that diverged
    from origin and broke `git pull --ff-only`).

.PARAMETER OutputPath
    Path to save debian.tar (default: ./debian.tar)

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\bash\build-debian-tar.ps1
    powershell -ExecutionPolicy Bypass -File .\bash\build-debian-tar.ps1 -OutputPath "C:\nodo-release\debian.tar"
#>

param(
    [string]$OutputPath = "debian.tar"
)

$ErrorActionPreference = "Stop"
$ContainerName = "nodo-debian-build-$(Get-Random)"

# Fail fast with a clear message instead of a wall of Docker CLI errors.
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    Write-Host "Docker is not installed." -ForegroundColor Red
    Write-Host "Install Docker Desktop from https://www.docker.com/products/docker-desktop/ and re-run this script." -ForegroundColor Yellow
    exit 1
}

docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Docker is installed but not running." -ForegroundColor Red
    Write-Host "Start Docker Desktop, wait for it to finish starting, and re-run this script." -ForegroundColor Yellow
    exit 1
}

Write-Host "Pulling ubuntu:22.04..." -ForegroundColor Cyan
docker pull ubuntu:22.04
if ($LASTEXITCODE -ne 0) { throw "docker pull failed" }

try {
    Write-Host "Creating container..." -ForegroundColor Green
    docker create --name $ContainerName ubuntu:22.04 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "docker create failed" }

    Write-Host "Exporting rootfs..." -ForegroundColor Green
    # -o writes the tar directly to disk. Piping docker export's binary stdout
    # through PowerShell's Out-File/redirection is unreliable across PS
    # versions (Windows PowerShell 5.1 has no -Encoding Byte, and even where it
    # exists PowerShell can mangle binary bytes going through its pipeline).
    docker export $ContainerName -o $OutputPath
    if ($LASTEXITCODE -ne 0) { throw "docker export failed" }

    $SizeMB = [Math]::Round((Get-Item $OutputPath).Length / 1MB, 1)
    Write-Host "$OutputPath ($SizeMB MB)" -ForegroundColor Cyan

    Write-Host "`nNext: Upload to wsl-exe release:" -ForegroundColor Yellow
    Write-Host "  gh release upload wsl-exe --repo celaut-project/nodo --clobber $OutputPath"

} finally {
    Write-Host "Cleaning up..." -ForegroundColor Gray
    docker rm -f $ContainerName 2>$null | Out-Null
}

Write-Host "Done!" -ForegroundColor Green
