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

# ====================== BACKGROUND DOWNLOADS ======================
# Invoke-WebRequest is synchronous.  Keeping it on the UI thread makes
# WinForms report "Not Responding" while a large file is being received.
# The implementation below uses a native .NET Task for the network operation
# and only updates WinForms from this (UI) thread.

Add-Type @"
using System;
using System.IO;
using System.Net;
using System.Threading;
using System.Threading.Tasks;

public sealed class NodoDownloadProgress
{
    public int Percent { get; internal set; }
    public string Status { get; internal set; }
    public string Log { get; internal set; }
    public bool Indeterminate { get; internal set; }
}

public sealed class NodoDownloadOperation
{
    private readonly object sync = new object();
    private NodoDownloadProgress progress = new NodoDownloadProgress
    {
        Percent = 0,
        Status = "Starting download...",
        Log = "",
        Indeterminate = false
    };

    public Task Completion { get; internal set; }
    public Exception Error { get; internal set; }

    public NodoDownloadProgress Progress
    {
        get
        {
            lock (sync)
            {
                return new NodoDownloadProgress
                {
                    Percent = progress.Percent,
                    Status = progress.Status,
                    Log = progress.Log,
                    Indeterminate = progress.Indeterminate
                };
            }
        }
    }

    internal void Report(int percent, string status, string log, bool indeterminate)
    {
        lock (sync)
        {
            progress = new NodoDownloadProgress
            {
                Percent = percent,
                Status = status,
                Log = log ?? "",
                Indeterminate = indeterminate
            };
        }
    }
}

public static class NodoDownloader
{
    public static NodoDownloadOperation Start(
        string uri,
        string tempPath,
        string displayName,
        int timeoutSeconds,
        int maxRetries)
    {
        var operation = new NodoDownloadOperation();
        operation.Completion = Task.Factory.StartNew(
            () =>
            {
                try
                {
                    DownloadWithRetry(operation, uri, tempPath, displayName, timeoutSeconds, maxRetries);
                }
                catch (Exception ex)
                {
                    operation.Error = ex;
                }
            },
            CancellationToken.None,
            TaskCreationOptions.LongRunning,
            TaskScheduler.Default);

        return operation;
    }

    private static void DownloadWithRetry(
        NodoDownloadOperation operation,
        string uri,
        string tempPath,
        string displayName,
        int timeoutSeconds,
        int maxRetries)
    {
        for (int attempt = 1; attempt <= maxRetries; attempt++)
        {
            try
            {
                if (File.Exists(tempPath)) File.Delete(tempPath);

                operation.Report(
                    0,
                    string.Format("Downloading {0} (attempt {1}/{2})...", displayName, attempt, maxRetries),
                    "",
                    false);

                var request = (HttpWebRequest)WebRequest.Create(uri);
                request.Method = "GET";
                request.AllowAutoRedirect = true;
                request.UserAgent = "Nodo-Installer/1.0";
                request.Timeout = Math.Max(1000, timeoutSeconds * 1000);
                request.ReadWriteTimeout = Math.Max(1000, timeoutSeconds * 1000);

                using (var response = (HttpWebResponse)request.GetResponse())
                {
                    int statusCode = (int)response.StatusCode;
                    if (statusCode < 200 || statusCode >= 300)
                    {
                        throw new InvalidOperationException(
                            string.Format("HTTP status {0} ({1}).", statusCode, response.StatusDescription));
                    }

                    long totalBytes = response.ContentLength;
                    long downloadedBytes = 0;
                    DateTime lastReport = DateTime.UtcNow;

                    using (Stream input = response.GetResponseStream())
                    using (FileStream output = new FileStream(
                        tempPath,
                        FileMode.Create,
                        FileAccess.Write,
                        FileShare.None,
                        1024 * 1024,
                        FileOptions.SequentialScan))
                    {
                        byte[] buffer = new byte[1024 * 1024];
                        int bytesRead;
                        int lastPercent = -1;

                        while ((bytesRead = input.Read(buffer, 0, buffer.Length)) > 0)
                        {
                            output.Write(buffer, 0, bytesRead);
                            downloadedBytes += bytesRead;

                            if (totalBytes > 0)
                            {
                                int percent = (int)Math.Min(99, Math.Floor(downloadedBytes * 100.0 / totalBytes));
                                DateTime now = DateTime.UtcNow;
                                if (percent != lastPercent &&
                                    ((now - lastReport).TotalMilliseconds >= 150 || percent >= 99))
                                {
                                    lastPercent = percent;
                                    lastReport = now;
                                    operation.Report(
                                        percent,
                                        string.Format(
                                            "Downloading {0} - {1}% ({2:0.0} / {3:0.0} MB)",
                                            displayName,
                                            percent,
                                            downloadedBytes / 1048576.0,
                                            totalBytes / 1048576.0),
                                        "",
                                        false);
                                }
                            }
                            else
                            {
                                DateTime now = DateTime.UtcNow;
                                if ((now - lastReport).TotalMilliseconds >= 250)
                                {
                                    lastReport = now;
                                    operation.Report(
                                        0,
                                        string.Format(
                                            "Downloading {0} ({1:0.0} MB received)",
                                            displayName,
                                            downloadedBytes / 1048576.0),
                                        "",
                                        true);
                                }
                            }
                        }

                        output.Flush();
                    }

                    long actualLength = new FileInfo(tempPath).Length;
                    if (totalBytes > 0 && actualLength != totalBytes)
                    {
                        throw new InvalidDataException(
                            string.Format(
                                "The download is incomplete (expected {0} bytes, received {1}).",
                                totalBytes,
                                actualLength));
                    }
                }

                operation.Report(100, "Downloaded " + displayName + " - 100%", "", false);
                return;
            }
            catch (Exception ex)
            {
                if (attempt >= maxRetries)
                {
                    throw new InvalidOperationException(
                        string.Format(
                            "Download of {0} failed after {1} attempts: {2}",
                            displayName,
                            maxRetries,
                            ex.Message),
                        ex);
                }

                int delaySeconds = Math.Min(30, (int)Math.Pow(2, attempt));
                operation.Report(
                    0,
                    string.Format("Retrying {0} in {1} seconds...", displayName, delaySeconds),
                    string.Format("Download attempt {0} failed: {1}", attempt, ex.Message),
                    false);
                Thread.Sleep(delaySeconds * 1000);
            }
        }
    }
}
"@

function ConvertFrom-TarOctal {
    param(
        [byte[]]$Buffer,
        [int]$Offset,
        [int]$Length
    )

    [long]$value = 0
    $foundDigit = $false

    for ($i = $Offset; $i -lt ($Offset + $Length); $i++) {
        $byte = $Buffer[$i]
        if ($byte -eq 0 -or $byte -eq 32) {
            if ($foundDigit) { break }
            continue
        }

        if ($byte -lt 48 -or $byte -gt 55) {
            throw "Invalid TAR octal field."
        }

        $foundDigit = $true
        $value = ($value * 8) + ($byte - 48)
    }

    return $value
}

function Test-TarArchive {
    param([string]$Path)

    $stream = $null
    try {
        $fileInfo = Get-Item -LiteralPath $Path -ErrorAction Stop
        if ($fileInfo.Length -lt 1024 -or ($fileInfo.Length % 512) -ne 0) {
            throw "The downloaded TAR file is empty or has an invalid size."
        }

        $stream = New-Object System.IO.FileStream(
            $Path,
            [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read,
            [System.IO.FileShare]::Read,
            512,
            [System.IO.FileOptions]::SequentialScan
        )

        $header = New-Object byte[] 512
        $hasHeader = $false
        $zeroBlocks = 0

        while ($stream.Position -lt $stream.Length) {
            $read = 0
            while ($read -lt 512) {
                $count = $stream.Read($header, $read, 512 - $read)
                if ($count -le 0) { throw "The downloaded TAR file is truncated." }
                $read += $count
            }

            $allZero = $true
            for ($i = 0; $i -lt 512; $i++) {
                if ($header[$i] -ne 0) {
                    $allZero = $false
                    break
                }
            }

            if ($allZero) {
                $zeroBlocks++
                if ($zeroBlocks -ge 2) {
                    if (-not $hasHeader) { throw "The downloaded TAR file contains no entries." }
                    return $true
                }
                continue
            }

            $zeroBlocks = 0
            $hasHeader = $true

            $storedChecksum = ConvertFrom-TarOctal -Buffer $header -Offset 148 -Length 8
            [long]$calculatedChecksum = 0
            for ($i = 0; $i -lt 512; $i++) {
                if ($i -ge 148 -and $i -lt 156) {
                    $calculatedChecksum += 32
                } else {
                    $calculatedChecksum += $header[$i]
                }
            }

            if ($storedChecksum -ne $calculatedChecksum) {
                throw "The downloaded TAR file has an invalid header checksum."
            }

            $entrySize = ConvertFrom-TarOctal -Buffer $header -Offset 124 -Length 12
            $dataBlocks = [long][Math]::Ceiling($entrySize / 512.0)
            $nextHeader = $stream.Position + ($dataBlocks * 512)
            if ($nextHeader -gt $stream.Length) {
                throw "The downloaded TAR file contains a truncated entry."
            }

            if ($dataBlocks -gt 0) {
                $stream.Seek($dataBlocks * 512, [System.IO.SeekOrigin]::Current) | Out-Null
            }
        }

        throw "The downloaded TAR file has no valid end-of-archive marker."
    }
    finally {
        if ($stream) { $stream.Dispose() }
    }
}

function Test-DownloadedFile {
    param(
        [string]$Path,
        [string]$ValidationType,
        [long]$ExpectedLength
    )

    $fileInfo = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($fileInfo.Length -le 0) {
        throw "The downloaded file is empty."
    }

    if ($ExpectedLength -gt 0 -and $fileInfo.Length -ne $ExpectedLength) {
        throw "The downloaded file is incomplete (expected $ExpectedLength bytes, received $($fileInfo.Length))."
    }

    # A proxy/error page saved as a successful response must never replace an
    # installer artifact.
    $prefixLength = [Math]::Min([long]$fileInfo.Length, 16)
    $prefix = New-Object byte[] $prefixLength
    $prefixStream = $null
    try {
        $prefixStream = [System.IO.File]::OpenRead($Path)
        [void]$prefixStream.Read($prefix, 0, $prefix.Length)
    }
    finally {
        if ($prefixStream) { $prefixStream.Dispose() }
    }

    $prefixText = [Text.Encoding]::ASCII.GetString($prefix).TrimStart()
    if ($prefixText -match '^(<html|<!doctype|<\?xml)') {
        throw "The server returned an HTML/XML error page instead of the requested file."
    }

    switch ($ValidationType) {
        "Tar" {
            Test-TarArchive -Path $Path | Out-Null
        }
        "Kernel" {
            if ($fileInfo.Length -lt 1MB) {
                throw "The downloaded kernel is unexpectedly small."
            }

            $kernelStream = $null
            try {
                $kernelStream = [System.IO.File]::OpenRead($Path)
                if ($kernelStream.Length -lt 0x20A) {
                    throw "The downloaded kernel is too small to contain a valid Linux boot header."
                }
                $kernelStream.Seek(0x202, [System.IO.SeekOrigin]::Begin) | Out-Null
                $signature = New-Object byte[] 4
                [void]$kernelStream.Read($signature, 0, 4)
                if ([Text.Encoding]::ASCII.GetString($signature) -ne "HdrS") {
                    throw "The downloaded file does not contain a valid bzImage boot header."
                }
            }
            finally {
                if ($kernelStream) { $kernelStream.Dispose() }
            }
        }
        default {
            throw "Unknown download validation type: $ValidationType"
        }
    }
}

function Invoke-BackgroundDownload {
    param(
        [string]$Uri,
        [string]$Destination,
        [string]$DisplayName,
        [string]$ValidationType
    )

    $tempPath = "$Destination.download"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $operation = [NodoDownloader]::Start($Uri, $tempPath, $DisplayName, 300, 3)
    $lastPercent = -1
    $lastStatus = ""
    $lastLog = ""

    while (-not $operation.Completion.IsCompleted) {
        $update = $operation.Progress
        if ($update.Status -ne $lastStatus -or $update.Percent -ne $lastPercent -or $update.Log -ne $lastLog) {
            if ($update.Indeterminate) {
                $progressBar.Style = [System.Windows.Forms.ProgressBarStyle]::Marquee
                Update-UI -Status $update.Status
            } else {
                if ($progressBar.Style -ne [System.Windows.Forms.ProgressBarStyle]::Continuous) {
                    $progressBar.Style = [System.Windows.Forms.ProgressBarStyle]::Continuous
                }
                Update-UI -Status $update.Status -Progress $update.Percent
            }

            if ($update.Log) {
                Write-Warning $update.Log
            }

            $lastPercent = $update.Percent
            $lastStatus = $update.Status
            $lastLog = $update.Log
        }

        [System.Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 50
    }

    $update = $operation.Progress
    if ($update.Indeterminate) {
        $progressBar.Style = [System.Windows.Forms.ProgressBarStyle]::Continuous
    } else {
        Update-UI -Status $update.Status -Progress $update.Percent
    }
    if ($operation.Error) {
        throw $operation.Error
    }

    # The worker has finished writing the temporary file.  Validate it before
    # making it visible at the destination path.
    Test-DownloadedFile -Path $tempPath -ValidationType $ValidationType -ExpectedLength -1

    if (Test-Path -LiteralPath $Destination) {
        Remove-Item -LiteralPath $Destination -Force -ErrorAction Stop
    }
    Move-Item -LiteralPath $tempPath -Destination $Destination -Force -ErrorAction Stop
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
$KernelUrl = "https://github.com/celaut-project/nodo/releases/download/v2/bzImage"
$NodoBaseDir = "C:\WSL\Nodo"
$NodoImageDir = "C:\WSL\Images"
$NodoRootfsPath = Join-Path $NodoImageDir "debian-nodo.tar"
$NodoRootfsUrl = "https://github.com/celaut-project/nodo/releases/download/v2/debian.tar"

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

# ====================== NETWORKING HELPERS ======================

function Merge-WslConfig {
    # Return the merged .wslconfig text: existing content is preserved verbatim
    # (comments, other sections, unrelated keys) while the required [wsl2] keys are
    # set/updated in place. Missing keys are appended to [wsl2]; the section is
    # created if absent.
    param(
        [string]$Path,
        [System.Collections.Specialized.OrderedDictionary]$Wsl2
    )

    if (Test-Path $Path) {
        $raw = Get-Content -Path $Path -Raw -ErrorAction SilentlyContinue
        if ($null -eq $raw) { $raw = "" }
        $lines = $raw -split "`r?`n"
    }
    else {
        $lines = @()
    }

    $result   = New-Object System.Collections.Generic.List[string]
    $inWsl2   = $false
    $seenWsl2 = $false
    $applied  = @{}

    foreach ($line in $lines) {
        $trim = $line.Trim()

        if ($trim -match '^\[(.+)\]$') {
            # Leaving a section: flush any required keys not yet written into [wsl2].
            if ($inWsl2) {
                foreach ($k in $Wsl2.Keys) {
                    if (-not $applied.ContainsKey($k)) {
                        $result.Add("$k=$($Wsl2[$k])")
                        $applied[$k] = $true
                    }
                }
            }
            $inWsl2 = ($matches[1].Trim() -ieq 'wsl2')
            if ($inWsl2) { $seenWsl2 = $true }
            $result.Add($line)
            continue
        }

        if ($inWsl2 -and $trim -match '^([^#;=]+)=(.*)$') {
            $key = $matches[1].Trim()
            if ($Wsl2.Contains($key)) {
                if (-not $applied.ContainsKey($key)) {
                    $result.Add("$key=$($Wsl2[$key])")   # override in place
                    $applied[$key] = $true
                }
                continue   # drop duplicate/old lines for this key
            }
        }

        $result.Add($line)
    }

    if ($inWsl2) {
        foreach ($k in $Wsl2.Keys) {
            if (-not $applied.ContainsKey($k)) {
                $result.Add("$k=$($Wsl2[$k])")
                $applied[$k] = $true
            }
        }
    }

    if (-not $seenWsl2) {
        if ($result.Count -gt 0 -and $result[$result.Count - 1].Trim() -ne "") {
            $result.Add("")
        }
        $result.Add("[wsl2]")
        foreach ($k in $Wsl2.Keys) {
            $result.Add("$k=$($Wsl2[$k])")
        }
    }

    return ($result -join "`r`n").TrimEnd() + "`r`n"
}

function Set-WslHyperVFirewall {
    # Allow inbound traffic to the WSL virtual machine through the Hyper-V firewall
    # so that services on 0.0.0.0 inside WSL are reachable from the LAN when
    # networkingMode=mirrored is in effect. The VMCreatorId is discovered
    # dynamically and the rule is created only once.
    if (-not (Get-Command Get-NetFirewallHyperVVMCreator -ErrorAction SilentlyContinue) -or
        -not (Get-Command New-NetFirewallHyperVRule -ErrorAction SilentlyContinue)) {
        Write-Warning "Hyper-V firewall cmdlets are unavailable on this Windows build; skipping the WSL inbound rule."
        return
    }

    try {
        $creator = Get-NetFirewallHyperVVMCreator -ErrorAction Stop |
            Where-Object { $_.FriendlyName -eq "WSL" } |
            Select-Object -First 1

        if (-not $creator) {
            Write-Warning "No WSL Hyper-V VM creator found; skipping the inbound firewall rule."
            return
        }

        $vmCreator = $creator.VMCreatorId

        $existing = Get-NetFirewallHyperVRule -VMCreatorId $vmCreator -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -eq "WSL-Allow-All" -or $_.DisplayName -eq "WSL Allow All Inbound" }

        if ($existing) {
            Write-Info "Hyper-V firewall rule 'WSL-Allow-All' already exists; reusing it."
            return
        }

        New-NetFirewallHyperVRule `
            -Name "WSL-Allow-All" `
            -DisplayName "WSL Allow All Inbound" `
            -VMCreatorId $vmCreator `
            -Direction Inbound `
            -Action Allow -ErrorAction Stop | Out-Null

        Write-Success "[OK] Hyper-V inbound firewall rule created (WSL reachable from the LAN)"
    }
    catch {
        Write-Warning "Could not configure the Hyper-V firewall rule: $_"
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
    $tempKernelPath = Join-Path $KernelDir "bzImage.tmp"
    Invoke-BackgroundDownload `
        -Uri $KernelUrl `
        -Destination $tempKernelPath `
        -DisplayName "custom kernel" `
        -ValidationType "Kernel"

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

# Keys this installer must ensure under [wsl2]. networkingMode=mirrored lets WSL
# share the Windows network stack so services are reachable via the host IP.
# Any other existing keys/sections in the user's .wslconfig are preserved.
$requiredWsl2 = [ordered]@{
    'nestedVirtualization' = 'true'
    'kernel'               = 'C:\\wsl-kernel\\bzImage'
    'networkingMode'       = 'mirrored'
}

$wslConfigContent = Merge-WslConfig -Path $wslConfigPath -Wsl2 $requiredWsl2

# Only touch the file (and back it up) when the merged content actually differs.
$configChanged = $true
if (Test-Path $wslConfigPath) {
    $existingRaw = Get-Content -Path $wslConfigPath -Raw
    if ($null -eq $existingRaw) { $existingRaw = "" }
    if ($existingRaw.Trim() -eq $wslConfigContent.Trim()) { $configChanged = $false }
}

if ($configChanged) {
    if (Test-Path $wslConfigPath) {
        $wslConfigOldPath = Join-Path $env:USERPROFILE ".wslconfig.old"
        Copy-Item -Path $wslConfigPath -Destination $wslConfigOldPath -Force
        Write-Info "Existing .wslconfig backed up to: $wslConfigOldPath"
    }
    Set-Content -Path $wslConfigPath -Value $wslConfigContent -Force
    Write-Success "[OK] .wslconfig updated at: $wslConfigPath"
} else {
    Write-Info ".wslconfig already contains the required settings - no changes needed"
}
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
    Invoke-BackgroundDownload `
        -Uri $NodoRootfsUrl `
        -Destination $NodoRootfsPath `
        -DisplayName "Debian rootfs" `
        -ValidationType "Tar"
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
curl -sSL https://github.com/celaut-project/nodo/releases/download/v2/vmlinuz -o /boot/vmlinuz
curl -sSL https://github.com/celaut-project/nodo/releases/download/v2/initramfs -o /boot/initramfs
echo -e "${GREEN}[OK] vmlinuz and initramfs installed${NC}"

echo -e "\n${CYAN}[STEP 5.4] Installing Nodo system...${NC}"
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/stable/install.sh | sudo bash
echo -e "${GREEN}[OK] Nodo system installed${NC}"

if [ -f /etc/systemd/system/nodo.service ]; then
    sed -i 's/{{PYTHON_VENV_BIN}}/python/g' /etc/systemd/system/nodo.service
    systemctl daemon-reload 2>/dev/null || true
fi

echo -e "\n${CYAN}[STEP 5.5a] Adjusting Nodo permissions...${NC}"
mkdir -p /nodo/storage
touch /nodo/storage/app.log
chown -R "${WSL_USER}:${WSL_USER}" /nodo
find /nodo -type d -exec chmod 777 {} \;
find /nodo -type f -exec chmod 777 {} \;
chmod +x /nodo/nodo.py 2>/dev/null || true
echo -e "${GREEN}[OK] /nodo permissions configured for ${WSL_USER}${NC}"

echo -e "\n${CYAN}[STEP 5.5b] Configuring Nodo network for Windows access...${NC}"
NODO_CONFIG="/nodo/config.yaml"
YQ="/nodo/bin/yq"

# Detect the WSL2 outbound interface (carries the 172.x.x.x IP reachable from Windows)
WSL_IFACE=$(ip route | awk '/^default/{print $5; exit}')
echo "Detected WSL2 outbound interface: $WSL_IFACE"

# Patch config.yaml so Nodo exposes services on the WSL2 interface
$YQ -i "
  .network.EXTERNAL_INTERFACE = \"$WSL_IFACE\" |
  .network.ISOLATE_INTERNAL_CHILDREN = true |
  .network.DEFAULT_EXECUTE_REMOTE = true
" "$NODO_CONFIG"
echo -e "${GREEN}[OK] Nodo will expose services on $WSL_IFACE (reachable from Windows)${NC}"

echo -e "\n${CYAN}[STEP 5.6] Configuring iptables forwarding for microVMs...${NC}"

# Enable IP forwarding
echo "net.ipv4.ip_forward=1" >> /etc/sysctl.conf
sysctl -w net.ipv4.ip_forward=1

# Accept forwarded traffic to/from microVM subnets
for SUBNET in 192.168.200.0/24 192.168.0.0/24; do
    iptables -C FORWARD -d $SUBNET -j ACCEPT 2>/dev/null || iptables -A FORWARD -d $SUBNET -j ACCEPT
    iptables -C FORWARD -s $SUBNET -j ACCEPT 2>/dev/null || iptables -A FORWARD -s $SUBNET -j ACCEPT
done

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
echo -e "${GREEN}[OK] iptables forwarding configured and persisted${NC}"

echo -e "\n${GREEN}=======================================================================${NC}"
echo -e "${GREEN}  [OK] Configuration completed successfully${NC}"
echo -e "${GREEN}=======================================================================${NC}"
'@

$tempScriptPath = Join-Path $env:TEMP "wsl-nodo-setup.sh"
Set-Content -Path $tempScriptPath -Value $wslSetupScript -Force -Encoding UTF8
Write-Info "Temporary configuration script created at: $tempScriptPath"

Write-Info "Copying and running configuration inside WSL..."
# Normalize to Unix line endings (LF only) before encoding — prevents \r breaking the shebang
$wslSetupScriptUnix = $wslSetupScript -replace "`r`n", "`n" -replace "`r", "`n"
$setupScriptBase64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($wslSetupScriptUnix))
Invoke-Wsl "echo $setupScriptBase64 | base64 -d > /tmp/setup.sh && chmod +x /tmp/setup.sh && /tmp/setup.sh"
Write-Success "[OK] Internal configuration completed"

Write-Info ""
Update-UI -Status "Configuring network..." -Progress 80
Write-Info "[STEP 6/8] Configuring Windows-to-WSL network routing..."

Write-Info "Configuring the Hyper-V firewall so WSL services are reachable from the LAN..."
Set-WslHyperVFirewall


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
    $Shortcut.IconLocation    = "C:\Windows\System32\wsl.exe,0"
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
