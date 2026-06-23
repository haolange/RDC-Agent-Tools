[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet('install', 'upgrade', 'uninstall', 'doctor')]
    [string]$Action = 'install',

    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'Programs\rdx-tools'),

    [switch]$AddToPath,

    [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-SourceRoot {
    $scriptPath = $PSCommandPath
    if (-not $scriptPath) { throw 'script path cannot be resolved' }
    $scriptDir = Split-Path -Parent $scriptPath
    $root = Split-Path -Parent $scriptDir
    if (-not (Test-Path -LiteralPath (Join-Path $root 'rdx.bat') -PathType Leaf)) {
        throw "rdx.bat not found under source root: $root"
    }
    return (Resolve-Path -LiteralPath $root).Path
}

function Resolve-InstallDir {
    param([string]$RawPath)
    $expanded = [Environment]::ExpandEnvironmentVariables($RawPath)
    $full = [System.IO.Path]::GetFullPath($expanded)
    if ([string]::IsNullOrWhiteSpace($full)) { throw 'install dir cannot be blank' }
    if ($full.Length -lt 8) { throw "install dir is unsafe: $full" }
    return $full
}

function Get-UserPathEntries {
    $raw = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ([string]::IsNullOrWhiteSpace($raw)) { return @() }
    return @($raw.Split(';') | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function Set-UserPathEntries {
    param([string[]]$Entries)
    [Environment]::SetEnvironmentVariable('Path', ($Entries -join ';'), 'User')
}

function Write-Step {
    param([string]$Message)
    Write-Output "[rdx-install] $Message"
}

function Copy-RdxTools {
    param(
        [string]$SourceRoot,
        [string]$TargetRoot
    )
    $excludeDirNames = @('intermediate', 'dist', '.git', '.venv', '__pycache__', '.agents', '.codex', '.qoder')
    $excludeDirPrefixes = @('pytest-cache-files-')
    if ($DryRun) {
        Write-Step "DRY-RUN copy $SourceRoot -> $TargetRoot"
        return
    }
    if (Test-Path -LiteralPath $TargetRoot) {
        Remove-Item -LiteralPath $TargetRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Path $TargetRoot -Force | Out-Null
    Get-ChildItem -LiteralPath $SourceRoot -Recurse -Force | ForEach-Object {
        $src = $_.FullName
        $rel = $src.Substring($SourceRoot.Length).TrimStart('\', '/')
        $parts = @($rel -split '[\\/]+' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
        foreach ($part in $parts) {
            if ($excludeDirNames -contains $part) { return }
            foreach ($prefix in $excludeDirPrefixes) {
                if ($part.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { return }
            }
        }
        $dst = Join-Path $TargetRoot $rel
        if ($_.PSIsContainer) {
            New-Item -ItemType Directory -Path $dst -Force | Out-Null
        }
        else {
            New-Item -ItemType Directory -Path (Split-Path -Parent $dst) -Force | Out-Null
            Copy-Item -LiteralPath $src -Destination $dst -Force
        }
    }
}

function Add-RdxToPath {
    param([string]$TargetRoot)
    $entries = @(Get-UserPathEntries)
    if ($entries -contains $TargetRoot) {
        Write-Step "PATH already contains $TargetRoot"
        return
    }
    if ($DryRun) {
        Write-Step "DRY-RUN add PATH entry $TargetRoot"
        return
    }
    Set-UserPathEntries -Entries @($entries + $TargetRoot)
    Write-Step "added PATH entry $TargetRoot"
}

function Remove-RdxFromPath {
    param([string]$TargetRoot)
    $entries = @(Get-UserPathEntries)
    $next = @($entries | Where-Object { $_ -ne $TargetRoot })
    if ($next.Count -eq $entries.Count) {
        Write-Step "PATH does not contain $TargetRoot"
        return
    }
    if ($DryRun) {
        Write-Step "DRY-RUN remove PATH entry $TargetRoot"
        return
    }
    Set-UserPathEntries -Entries $next
    Write-Step "removed PATH entry $TargetRoot"
}

function Invoke-RdxDoctor {
    param([string]$Root)
    $bat = Join-Path $Root 'rdx.bat'
    if ($DryRun) {
        Write-Step "DRY-RUN doctor $bat --json doctor"
        return
    }
    if (-not (Test-Path -LiteralPath $bat -PathType Leaf)) {
        throw "rdx.bat not found: $bat"
    }
    & $bat --json doctor
    if ($LASTEXITCODE -ne 0) {
        throw "rdx doctor failed with exit code $LASTEXITCODE"
    }
}

$sourceRoot = Resolve-SourceRoot
$targetRoot = Resolve-InstallDir -RawPath $InstallDir
Write-Step "action=$Action"
Write-Step "source=$sourceRoot"
Write-Step "target=$targetRoot"

switch ($Action) {
    'install' {
        Copy-RdxTools -SourceRoot $sourceRoot -TargetRoot $targetRoot
        if ($AddToPath) { Add-RdxToPath -TargetRoot $targetRoot }
        Invoke-RdxDoctor -Root $targetRoot
    }
    'upgrade' {
        Copy-RdxTools -SourceRoot $sourceRoot -TargetRoot $targetRoot
        if ($AddToPath) { Add-RdxToPath -TargetRoot $targetRoot }
        Invoke-RdxDoctor -Root $targetRoot
    }
    'uninstall' {
        Remove-RdxFromPath -TargetRoot $targetRoot
        if ($DryRun) {
            Write-Step "DRY-RUN remove $targetRoot"
        }
        elseif (Test-Path -LiteralPath $targetRoot) {
            if (-not (Test-Path -LiteralPath (Join-Path $targetRoot 'rdx.bat') -PathType Leaf)) {
                throw "refusing to remove non-rdx-tools directory: $targetRoot"
            }
            Remove-Item -LiteralPath $targetRoot -Recurse -Force
            Write-Step "removed $targetRoot"
        }
    }
    'doctor' {
        Invoke-RdxDoctor -Root $targetRoot
    }
}

Write-Step 'done'
