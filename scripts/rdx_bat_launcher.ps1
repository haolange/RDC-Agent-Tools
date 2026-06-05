[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$script:RETURN_OK = 0
$script:RETURN_ARGS_ERROR = 2
$script:RETURN_ENV_ERROR = 2
$script:RETURN_STARTUP_ERROR = 2
$script:RDX_LAST_CLI_EXIT_CODE = $script:RETURN_OK

function Resolve-ToolsRoot {
    $scriptPath = $PSCommandPath
    if (-not $scriptPath -or -not (Test-Path -LiteralPath $scriptPath)) {
        throw 'launcher script path cannot be resolved'
    }

    $scriptDir = Split-Path -Parent $scriptPath
    $fallback = Split-Path -Parent $scriptDir
    if (-not $fallback -or -not (Test-Path -LiteralPath $fallback)) {
        throw "tools root not found: $fallback"
    }

    $resolvedFallback = (Resolve-Path -LiteralPath $fallback).Path
    $envRoot = [string]$env:RDX_TOOLS_ROOT
    if (-not [string]::IsNullOrWhiteSpace($envRoot)) {
        try {
            $envResolved = (Resolve-Path -LiteralPath $envRoot).Path
            if ($envResolved -ne $resolvedFallback) {
                Write-Warning "[RDX][WARN] RDX_TOOLS_ROOT overrides launcher root and is used as effective root: $envResolved"
                return $envResolved
            }
        }
        catch {
            Write-Warning "[RDX][WARN] invalid RDX_TOOLS_ROOT='$envRoot'; fallback to: $resolvedFallback"
        }
    }

    return $resolvedFallback
}

function Resolve-CallerWorkingDirectory {
    $callerCwd = [string]$env:RDX_CALLER_CWD
    if (-not [string]::IsNullOrWhiteSpace($callerCwd) -and (Test-Path -LiteralPath $callerCwd -PathType Container)) {
        return (Resolve-Path -LiteralPath $callerCwd).Path
    }
    return [System.IO.Directory]::GetCurrentDirectory()
}

function Test-PythonCandidate {
    param([string[]]$PythonSpec)

    if ($PythonSpec.Count -eq 0) { return $false }
    $exe = [string]$PythonSpec[0]
    if ([string]::IsNullOrWhiteSpace($exe)) { return $false }

    $probeArgs = @()
    if ($PythonSpec.Count -gt 1) {
        $probeArgs += @($PythonSpec[1..($PythonSpec.Count - 1)])
    }
    $probeArgs += @('-c', 'import sys; print(sys.executable)')

    try {
        $probeOutput = @(& $exe @probeArgs 2>&1)
        $probeExitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
    }
    catch {
        return $false
    }

    return $probeExitCode -eq 0 -and -not [string]::IsNullOrWhiteSpace((($probeOutput | ForEach-Object { [string]$_ }) -join "`n").Trim())
}

function Resolve-Python {
    $envPython = [string]$env:RDX_PYTHON
    if (-not [string]::IsNullOrWhiteSpace($envPython)) {
        if (Test-PythonCandidate -PythonSpec @($envPython)) {
            return @($envPython)
        }
        throw "RDX_PYTHON is set but invalid: $envPython"
    }

    $toolsRoot = [string]$env:RDX_TOOLS_ROOT
    if ([string]::IsNullOrWhiteSpace($toolsRoot)) {
        $toolsRoot = Resolve-ToolsRoot
    }

    $bundledPython = Join-Path $toolsRoot 'binaries\windows\x64\python\python.exe'
    if ((Test-Path -LiteralPath $bundledPython -PathType Leaf) -and (Test-PythonCandidate -PythonSpec @($bundledPython))) {
        return @($bundledPython)
    }

    foreach ($candidate in @('python', 'python3')) {
        if (Test-PythonCandidate -PythonSpec @($candidate)) {
            return @($candidate)
        }
    }
    if (Test-PythonCandidate -PythonSpec @('py', '-3')) {
        return @('py', '-3')
    }

    throw 'no runnable Python found; set RDX_PYTHON or restore the bundled Python runtime'
}

function Quote-CmdArg {
    param([string]$Value)

    if ($null -eq $Value) { return '""' }
    return '"' + $Value.Replace('"', '\"') + '"'
}

function Quote-CmdFileValue {
    param([string]$Value)

    if ($null -eq $Value) { return '' }
    return $Value.Replace('"', '\"')
}

function Get-PythonInvocation {
    $pythonCmd = [string[]](Resolve-Python)
    if ($pythonCmd.Count -eq 0) {
        throw 'python executable not resolved'
    }

    $parts = [System.Collections.Generic.List[string]]::new()
    foreach ($arg in $pythonCmd) {
        $parts.Add((Quote-CmdArg $arg))
    }
    return [pscustomobject]@{
        Executable = [string]$pythonCmd[0]
        ExtraArgs = if ($pythonCmd.Count -gt 1) { @($pythonCmd[1..($pythonCmd.Count - 1)]) } else { @() }
        CommandText = ($parts -join ' ')
    }
}

function Write-JsonStatus {
    param(
        [bool]$Ok,
        [string]$ErrorCode,
        [string]$ErrorMessage,
        [string]$ContextId,
        [hashtable]$Details
    )

    if (-not $Details) { $Details = @{} }
    $payload = [ordered]@{
        ok = [bool]$Ok
        error_code = if ([string]::IsNullOrWhiteSpace($ErrorCode)) { '' } else { $ErrorCode }
        error_message = if ([string]::IsNullOrWhiteSpace($ErrorMessage)) { '' } else { $ErrorMessage }
        context_id = if ([string]::IsNullOrWhiteSpace($ContextId)) { 'default' } else { $ContextId }
        details = $Details
    }
    Write-Output ($payload | ConvertTo-Json -Depth 8 -Compress)
}

function Show-Usage {
    Write-Output 'usage: rdx.bat [--non-interactive] [--daemon-context <id>] [--json] <command> ...'
    Write-Output ''
    Write-Output 'commands:'
    Write-Output '  version'
    Write-Output '  doctor'
    Write-Output '  tools list|search'
    Write-Output '  daemon start|stop|status'
    Write-Output '  context status|update|list|clear'
    Write-Output '  session preview on|off|status'
    Write-Output '  capture open|status'
    Write-Output '  vfs ls|cat|tree|resolve'
    Write-Output '  diff pipeline|image'
    Write-Output '  assert pipeline|image'
    Write-Output '  completion powershell|bash|zsh|fish'
    Write-Output '  call <rd.*>'
    Write-Output ''
    Write-Output 'examples:'
    Write-Output '  rdx.bat --version'
    Write-Output '  rdx.bat version --json'
    Write-Output '  rdx.bat --json doctor'
    Write-Output '  rdx.bat --non-interactive --json doctor'
    Write-Output '  rdx.bat completion powershell'
    Write-Output '  rdx.bat context status --json'
    Write-Output '  rdx.bat context update --key notes --value triaged --json'
    Write-Output '  rdx.bat capture open --file D:\path\capture.rdc --frame-index 0'
    Write-Output '  rdx.bat vfs ls --path / --format tsv'
}

function Read-LauncherLine {
    param(
        [string]$Prompt,
        [bool]$AllowBlank = $false
    )

    if ($Host.Name -like '*Console*') {
        return Read-Host $Prompt
    }

    Write-Host -NoNewline $Prompt
    $line = [Console]::In.ReadLine()
    if ($null -eq $line -and -not $AllowBlank) { return '' }
    return [string]$line
}

function Select-ContextInteractive {
    while ($true) {
        Write-Host ''
        Write-Host 'Context:'
        Write-Host '1. default'
        Write-Host '2. custom'
        Write-Host '0. cancel'
        $choice = (Read-LauncherLine -Prompt 'Select: ' -AllowBlank $false).Trim()
        switch ($choice) {
            '1' { return 'default' }
            '2' {
                $ctx = (Read-LauncherLine -Prompt 'Context id: ' -AllowBlank $false).Trim()
                if (-not [string]::IsNullOrWhiteSpace($ctx) -and -not $ctx.Contains('"')) {
                    return $ctx
                }
                Write-Host '[RDX][ERR] context id cannot be blank or contain double quotes.'
            }
            '0' { return '' }
            default { Write-Host '[RDX][ERR] Invalid selection. Try again.' }
        }
    }
}

function Parse-LauncherArgs {
    $raw = [System.Collections.Generic.List[string]]::new()
    foreach ($item in @($script:Arguments)) {
        if ($null -ne $item) { $raw.Add([string]$item) }
    }

    $result = [ordered]@{
        NonInteractive = $false
        ContextId = 'default'
        CommandArgs = [System.Collections.Generic.List[string]]::new()
    }

    $i = 0
    while ($i -lt $raw.Count) {
        $arg = [string]$raw[$i]
        switch ($arg) {
            '--non-interactive' {
                $result.NonInteractive = $true
                $i += 1
            }
            '--daemon-context' {
                if ($i + 1 -ge $raw.Count) { throw 'missing --daemon-context value' }
                $result.ContextId = [string]$raw[$i + 1]
                $i += 2
            }
            '--context-id' {
                if ($i + 1 -ge $raw.Count) { throw 'missing --context-id value' }
                $result.ContextId = [string]$raw[$i + 1]
                $i += 2
            }
            default {
                $result.CommandArgs.Add($arg)
                $i += 1
            }
        }
    }

    if ([string]::IsNullOrWhiteSpace([string]$result.ContextId)) {
        $result.ContextId = 'default'
    }
    else {
        $result.ContextId = ([string]$result.ContextId).Trim()
    }

    return [pscustomobject]$result
}

function Normalize-CommandArgs {
    param(
        [string[]]$CommandArgs,
        [string]$ContextId
    )

    $normalized = [System.Collections.Generic.List[string]]::new()
    $normalized.Add('--daemon-context')
    $normalized.Add($ContextId)
    foreach ($arg in $CommandArgs) {
        $normalized.Add([string]$arg)
    }
    return [string[]]$normalized
}

function Invoke-Cli {
    param(
        [string]$ToolsRoot,
        [string]$ContextId,
        [string[]]$CommandArgs
    )

    $python = Get-PythonInvocation
    $scriptPath = Join-Path $ToolsRoot 'cli\run_cli.py'
    $extraArgs = @()
    if ($python.ExtraArgs) {
        $extraArgs = @($python.ExtraArgs)
    }
    $finalArgs = $extraArgs + @($scriptPath) + (Normalize-CommandArgs -CommandArgs $CommandArgs -ContextId $ContextId)
    $env:RDX_TOOLS_ROOT = $ToolsRoot
    $env:PYTHONIOENCODING = 'utf-8'
    $env:RDX_LAUNCHER_PROG = 'rdx.bat'
    if ([string]$env:RDX_BAT_DEBUG -eq '1') {
        Write-Host ('[RDX][DEBUG] python=' + $python.Executable)
        Write-Host ('[RDX][DEBUG] args=' + (($finalArgs | ForEach-Object { '[' + [string]$_ + ']' }) -join ' '))
    }
    $callerWorkingDirectory = Resolve-CallerWorkingDirectory
    $locationPushed = $false
    try {
        Push-Location -LiteralPath $callerWorkingDirectory
        $locationPushed = $true
        & $python.Executable @finalArgs
        $script:RDX_LAST_CLI_EXIT_CODE = $(if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE })
    }
    finally {
        if ($locationPushed) {
            Pop-Location
        }
    }
}

function New-CliShellCommandFile {
    param(
        [string]$ToolsRoot,
        [string]$ContextId
    )

    $python = Get-PythonInvocation
    $cliPath = Join-Path $ToolsRoot 'cli\run_cli.py'
    $cmdPath = Join-Path ([System.IO.Path]::GetTempPath()) ("rdx-cli-shell-{0}.cmd" -f ([guid]::NewGuid().ToString('N')))
    $lines = @(
        '@echo off',
        ('set "RDX_TOOLS_ROOT={0}"' -f (Quote-CmdFileValue $ToolsRoot)),
        'set "PYTHONIOENCODING=utf-8"',
        'set "RDX_LAUNCHER_PROG=rdx.bat"',
        ('title RDX CLI [{0}]' -f $ContextId),
        ('doskey rdx={0} "{1}" --daemon-context "{2}" $*' -f $python.CommandText, $cliPath, $ContextId),
        ('doskey status={0} "{1}" --daemon-context "{2}" daemon status' -f $python.CommandText, $cliPath, $ContextId),
        ('doskey stop={0} "{1}" --daemon-context "{2}" daemon stop' -f $python.CommandText, $cliPath, $ContextId),
        ('doskey clear={0} "{1}" --daemon-context "{2}" context clear' -f $python.CommandText, $cliPath, $ContextId),
        'doskey quit=exit',
        'echo.',
        ('echo [RDX] CLI shell ready. context={0}' -f $ContextId),
        'echo [RDX] Common commands: rdx --json doctor, status, stop, clear, rdx capture status',
        'echo.'
    )
    Set-Content -LiteralPath $cmdPath -Value $lines -Encoding ASCII
    return $cmdPath
}

function Start-InteractiveCliShell {
    param([string]$ToolsRoot)

    $ctx = Select-ContextInteractive
    if ([string]::IsNullOrWhiteSpace($ctx)) { return }

    Invoke-Cli -ToolsRoot $ToolsRoot -ContextId $ctx -CommandArgs @('daemon', 'start')
    $startCode = $script:RDX_LAST_CLI_EXIT_CODE
    if ($startCode -ne 0) {
        Write-Host "[RDX][ERR] daemon start failed: exit=$startCode"
        return
    }

    if (Get-TestMode) {
        Write-Host ''
        Write-Host "[RDX] CLI shell ready. context=$ctx"
        Write-Host '[RDX] Common commands: status, stop, clear, rdx --json doctor, exit'
        while ($true) {
            $line = (Read-LauncherLine -Prompt "[rdx:$ctx] " -AllowBlank $true).Trim()
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            if ($line -in @('exit', 'quit')) { break }

            $invokeArgs = $null
            switch -Regex ($line) {
                '^status$' { $invokeArgs = @('daemon', 'status'); break }
                '^stop$' { $invokeArgs = @('daemon', 'stop'); break }
                '^clear$' { $invokeArgs = @('context', 'clear'); break }
                '^rdx\s+--json\s+doctor$' { $invokeArgs = @('--json', 'doctor'); break }
                '^rdx\s+daemon\s+status$' { $invokeArgs = @('daemon', 'status'); break }
                '^rdx\s+daemon\s+stop$' { $invokeArgs = @('daemon', 'stop'); break }
                '^rdx\s+context\s+clear$' { $invokeArgs = @('context', 'clear'); break }
            }
            if ($null -eq $invokeArgs) {
                Write-Host '[RDX][ERR] test mode shell supports status, stop, clear, rdx --json doctor, and exit.'
                continue
            }
            Invoke-Cli -ToolsRoot $ToolsRoot -ContextId $ctx -CommandArgs $invokeArgs
        }
        return
    }

    $cmdFile = New-CliShellCommandFile -ToolsRoot $ToolsRoot -ContextId $ctx
    [void](Start-Process -FilePath $env:ComSpec -ArgumentList @('/Q', '/K', $cmdFile) -WorkingDirectory $ToolsRoot -PassThru)
    Write-Host ''
    Write-Host "[RDX] CLI shell started. context=$ctx"
    Write-Host '[RDX] Closing the shell does not stop the daemon. Use stop or rdx daemon stop to stop it explicitly.'
}

function Get-TestMode {
    foreach ($value in @([string]$env:RDX_BAT_TEST_MODE, [string]$env:RDX_BAT_NO_NEW_WINDOW)) {
        if ([string]::IsNullOrWhiteSpace($value)) { continue }
        if ($value.Trim().ToLowerInvariant() -in @('1', 'true', 'yes', 'on')) { return $true }
    }
    return $false
}

function Show-MainMenu {
    param([string]$ToolsRoot)

    while ($true) {
        Write-Host ''
        Write-Host '=== rdx.bat Launcher ==='
        Write-Host '1. Start CLI'
        Write-Host '2. Help'
        Write-Host '0. Exit'
        $choice = (Read-LauncherLine -Prompt 'Select: ' -AllowBlank $false).Trim()
        switch ($choice) {
            '1' { Start-InteractiveCliShell -ToolsRoot $ToolsRoot }
            '2' { Show-Usage }
            '0' { return }
            default { Write-Host '[RDX][ERR] Invalid selection. Try again.' }
        }
    }
}

function First-CommandToken {
    param([string[]]$CommandArgs)

    $skipNext = $false
    foreach ($arg in $CommandArgs) {
        if ($skipNext) {
            $skipNext = $false
            continue
        }
        if ($arg -in @('--daemon-context', '--context-id')) {
            $skipNext = $true
            continue
        }
        if ($arg.StartsWith('-')) { continue }
        return $arg
    }
    return ''
}

try {
    $toolsRoot = Resolve-ToolsRoot
    $env:RDX_TOOLS_ROOT = $toolsRoot
    $parsed = Parse-LauncherArgs
}
catch {
    Write-JsonStatus -Ok $false -ErrorCode 'launcher_error' -ErrorMessage $_.Exception.Message -ContextId 'default' -Details @{}
    exit $script:RETURN_ENV_ERROR
}

$contextId = [string]$parsed.ContextId
$nonInteractive = [bool]$parsed.NonInteractive
$commandArgs = [string[]]@($parsed.CommandArgs)

if ($commandArgs.Count -eq 0) {
    if ($nonInteractive) {
        Write-JsonStatus -Ok $false -ErrorCode 'missing_command' -ErrorMessage 'missing command' -ContextId $contextId -Details @{}
        exit $script:RETURN_ARGS_ERROR
    }
    Show-MainMenu -ToolsRoot $toolsRoot
    exit $script:RETURN_OK
}

if ($commandArgs.Count -eq 1 -and $commandArgs[0] -in @('--help', '-h', 'help')) {
    Show-Usage
    exit $script:RETURN_OK
}

$firstCommand = First-CommandToken -CommandArgs $commandArgs
if ($firstCommand -eq 'mcp') {
    Write-JsonStatus `
        -Ok $false `
        -ErrorCode 'unsupported_command' `
        -ErrorMessage 'rdx-tools no longer exposes an MCP server; use CLI commands such as `rdx.bat --json doctor` or `rdx.bat call <rd.*>`.' `
        -ContextId $contextId `
        -Details @{ unsupported_command = 'mcp'; supported_entrypoints = @('rdx.bat', 'bin/rdx', 'python cli/run_cli.py') }
    exit $script:RETURN_ARGS_ERROR
}

try {
    Invoke-Cli -ToolsRoot $toolsRoot -ContextId $contextId -CommandArgs $commandArgs
    $exitCode = $script:RDX_LAST_CLI_EXIT_CODE
}
catch {
    Write-JsonStatus -Ok $false -ErrorCode 'startup_failed' -ErrorMessage $_.Exception.Message -ContextId $contextId -Details @{}
    exit $script:RETURN_STARTUP_ERROR
}

if ($exitCode -lt 0 -or $exitCode -gt 255) {
    $exitCode = $script:RETURN_STARTUP_ERROR
}
exit $exitCode
