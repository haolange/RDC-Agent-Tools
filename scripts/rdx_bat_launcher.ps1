[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
if ($env:RDX_BAT_LAUNCHER_DEBUG) {
    $VerbosePreference = 'Continue'
}

$script:RETURN_OK = 0
$script:RETURN_ARGS_ERROR = 1
$script:RETURN_ENV_ERROR = 2
$script:RETURN_STARTUP_ERROR = 3
$script:RETURN_TIMEOUT = 4
$script:RETURN_TOOL_ERROR = 5

function Resolve-ToolsRoot {
    $scriptPath = $PSCommandPath
    if (-not $scriptPath -or -not (Test-Path -LiteralPath $scriptPath)) {
        $invocation = $MyInvocation.MyCommand
        if ($invocation -and $invocation.Path) {
            $scriptPath = [string]$invocation.Path
        }
    }
    if (-not $scriptPath -or -not (Test-Path -LiteralPath $scriptPath)) {
        if ($args.Count -gt 0) {
            $scriptPath = [string]$args[0]
        }
    }

    $scriptDir = Split-Path -Parent $scriptPath
    if (-not $scriptDir) {
        throw 'launcher script path cannot be resolved'
    }

    $fallback = Split-Path -Parent $scriptDir
    if (-not $fallback -or -not (Test-Path -LiteralPath $fallback)) {
        throw "fallback tools root not found: $fallback"
    }

    $resolvedFallback = (Resolve-Path -LiteralPath $fallback).Path
    $envRoot = [string]$env:RDX_TOOLS_ROOT
    if (-not [string]::IsNullOrWhiteSpace($envRoot)) {
        try {
            $envResolved = (Resolve-Path -LiteralPath $envRoot).Path
            if (-not (Test-Path -LiteralPath $envResolved)) {
                Write-Warning "[RDX][WARN] invalid RDX_TOOLS_ROOT='$envRoot'; fallback to: $resolvedFallback"
            }
            elseif ($envResolved -ne $resolvedFallback) {
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

function Ensure-ToolsRootEnv {
    param([string]$ToolsRoot)
    $env:RDX_TOOLS_ROOT = $ToolsRoot
}

function Resolve-Python {
    $envPython = [string]$env:RDX_PYTHON
    if ($envPython -and (Test-Path -LiteralPath $envPython)) {
        return @([string]$envPython)
    }

    $pyExe = Get-Command python -ErrorAction SilentlyContinue
    if ($pyExe) {
        return @([string]$pyExe.Source)
    }

    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        return @([string]$pyLauncher.Source, '-3')
    }

    throw 'python 3.x not found; set RDX_PYTHON or ensure python is installed.'
}

function Normalize-PythonCommand {
    param([object]$PythonSpec)

    if ($null -eq $PythonSpec) {
        return @()
    }

    if ($PythonSpec -is [string]) {
        return @([string]$PythonSpec)
    }

    if ($PythonSpec -is [System.Array]) {
        return [string[]]$PythonSpec
    }

    return @([string]$PythonSpec)
}

function Parse-LauncherArgs {
    $argsSafe = @()
    if ($Arguments) {
        $argsSafe = @($Arguments)
    }
    if (-not $argsSafe -and $args.Count -gt 0) {
        $argsSafe = @($args)
    }
    if ($env:RDX_BAT_LAUNCHER_DEBUG) { Write-Verbose ('DEBUG_ARGS=' + ($argsSafe -join '|')) }

    $result = [ordered]@{
        NonInteractive = $false
        ContextId = 'default'
        Command = ''
        CommandArgs = [System.Collections.Generic.List[string]]::new()
    }

    $i = 0
    while ($i -lt $argsSafe.Count) {
        $arg = [string]$argsSafe[$i]
        switch ($arg) {
            '--non-interactive' {
                $result.NonInteractive = $true
                $i += 1
            }
            '--daemon-context' {
                if ($i + 1 -ge $argsSafe.Count) {
                    throw 'missing --daemon-context value'
                }
                $result.ContextId = [string]$argsSafe[$i + 1]
                $i += 2
            }
            '--context-id' {
                if ($i + 1 -ge $argsSafe.Count) {
                    throw 'missing --context-id value'
                }
                $result.ContextId = [string]$argsSafe[$i + 1]
                $i += 2
            }
            '--help' {
                if (-not $result.Command) {
                    $result.Command = 'help'
                }
                else {
                    $result.CommandArgs.Add($arg)
                }
                $i += 1
            }
            '-h' {
                if (-not $result.Command) {
                    $result.Command = 'help'
                }
                else {
                    $result.CommandArgs.Add($arg)
                }
                $i += 1
            }
            default {
                if (-not $result.Command -and -not $arg.StartsWith('-')) {
                    $result.Command = $arg
                    $i += 1
                }
                else {
                    $result.CommandArgs.Add($arg)
                    $i += 1
                }
            }
        }
    }

    if ([string]::IsNullOrWhiteSpace($result.ContextId)) {
        $result.ContextId = 'default'
    }
    else {
        $result.ContextId = $result.ContextId.Trim()
    }

    return [pscustomobject]$result
}

function Extract-JsonPayload {
    param([string]$Text)
    if ([string]::IsNullOrWhiteSpace($Text)) {
        return $null
    }

    $start = $Text.IndexOf('{')
    if ($start -lt 0) {
        return $null
    }

    $end = $Text.LastIndexOf('}')
    if ($end -le $start) {
        return $null
    }

    $candidate = $Text.Substring($start, $end - $start + 1)
    try {
        return $candidate | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        return $null
    }
}

function Write-JsonStatus {
    param(
        [bool]$Ok,
        [int]$ErrorCode,
        [string]$ErrorMessage,
        [string]$ContextId,
        [hashtable]$Details
    )

    if (-not $Details) {
        $Details = @{}
    }

    $payload = [ordered]@{
        ok = [bool]$Ok
        error_code = $ErrorCode
        error_message = if ([string]::IsNullOrWhiteSpace($ErrorMessage)) { '' } else { $ErrorMessage }
        context_id = if ([string]::IsNullOrWhiteSpace($ContextId)) { 'default' } else { $ContextId }
        details = $Details
    }
    Write-Output ($payload | ConvertTo-Json -Depth 8 -Compress)
}

function Map-ExitCode {
    param(
        [int]$ChildExitCode,
        [bool]$TimedOut,
        [pscustomobject]$Payload,
        [string]$ErrorText,
        [string]$Command
    )

    if ($TimedOut) {
        return $script:RETURN_TIMEOUT
    }

    if ($Payload -and $Payload.PSObject.Properties.Name -contains 'ok') {
        if ([bool]$Payload.ok) {
            return $script:RETURN_OK
        }

        $code = [string]$Payload.error_code
        $codeLower = $code.ToLowerInvariant()
        if ($codeLower -in @('dependencies_missing', 'runtime_layout_missing', 'renderdoc_import_failed', 'runtime_root_invalid')) {
            return $script:RETURN_ENV_ERROR
        }
        if ($codeLower -in @('startup_failed', 'daemon_not_ready', 'no_python_found')) {
            return $script:RETURN_STARTUP_ERROR
        }
        if ($codeLower -eq 'timeout') {
            return $script:RETURN_TIMEOUT
        }
        return $script:RETURN_TOOL_ERROR
    }

    if ($ChildExitCode -in @(0,1,2,3,4,5)) {
        return $ChildExitCode
    }

    $err = [string]$ErrorText
    if ($Command -eq 'mcp' -and $err -match 'python|renderdoc|dependencies|missing|not found|not available') {
        return $script:RETURN_ENV_ERROR
    }
    if ($Command -eq 'daemon-shell' -and $err -match 'daemon|not ready|timeout') {
        return $script:RETURN_STARTUP_ERROR
    }

    return $script:RETURN_TOOL_ERROR
}

function Emit-ChildResult {
    param(
        [int]$ExitCode,
        [bool]$TimedOut,
        [string]$Output,
        [string]$Error,
        [string]$Command,
        [string]$ContextId,
        [bool]$AsJson
    )

    if (-not $AsJson) {
        if ($Output) { Write-Output $Output.TrimEnd() }
        if ($Error) { Write-Output $Error.TrimEnd() }
        return $ExitCode
    }

    $payload = Extract-JsonPayload ($Output + "`n" + $Error)
    if ($payload -and $payload.PSObject.Properties.Name -contains 'ok') {
        if (-not $payload.PSObject.Properties.Name -contains 'context_id') {
            $payload | Add-Member -NotePropertyName context_id -NotePropertyValue $ContextId -Force
        }
        $status = [ordered]@{
            ok = [bool]$payload.ok
            error_code = if ($payload.PSObject.Properties.Name -contains 'error_code') {
                try { [int]$payload.error_code }
                catch { 5 }
            }
            else { 5 }
            error_message = if ($payload.PSObject.Properties.Name -contains 'error_message') { [string]$payload.error_message } else { '' }
            context_id = [string]$payload.context_id
        }

        Write-Output ($status | ConvertTo-Json -Depth 8 -Compress)
        return Map-ExitCode -ChildExitCode $ExitCode -TimedOut $TimedOut -Payload $payload -ErrorText $Error -Command $Command
    }

    if ($TimedOut) {
        Write-JsonStatus -Ok $false -ErrorCode $script:RETURN_TIMEOUT -ErrorMessage 'command timeout' -ContextId $ContextId
        return $script:RETURN_TIMEOUT
    }

    if ($ExitCode -eq 0) {
        Write-JsonStatus -Ok $true -ErrorCode 0 -ErrorMessage '' -ContextId $ContextId
        return $script:RETURN_OK
    }

    $msg = ($Error | Out-String).Trim()
    if (-not $msg) { $msg = ($Output | Out-String).Trim() }
    if (-not $msg) { $msg = 'command failed' }
    Write-JsonStatus -Ok $false -ErrorCode $script:RETURN_TOOL_ERROR -ErrorMessage $msg -ContextId $ContextId
    return $script:RETURN_TOOL_ERROR
}

function Quote-CommandArg {
    param([string]$Value)
    if ($Value -match '\\s|"|`"') {
        return '"' + $Value.Replace('"', '\"') + '"'
    }
    return $Value
}

function Invoke-Subprocess {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [int]$TimeoutMs = 30000
    )

    $stdoutFile = New-TemporaryFile
    $stderrFile = New-TemporaryFile
    $argumentLine = ([string[]]$ArgumentList | ForEach-Object { Quote-CommandArg $_ }) -join ' '
    try {
        $proc = Start-Process -FilePath $FilePath -ArgumentList $argumentLine -WorkingDirectory $WorkingDirectory -NoNewWindow -PassThru -RedirectStandardOutput $stdoutFile.FullName -RedirectStandardError $stderrFile.FullName
        if (-not $proc.WaitForExit($TimeoutMs)) {
            try { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue } catch {}
            return [pscustomobject]@{
                ExitCode = $script:RETURN_TIMEOUT
                TimedOut = $true
                StdOut = ''
                StdErr = 'timeout'
            }
        }

        $outText = if (Test-Path -LiteralPath $stdoutFile.FullName) { Get-Content -Raw -Path $stdoutFile.FullName } else { '' }
        $errText = if (Test-Path -LiteralPath $stderrFile.FullName) { Get-Content -Raw -Path $stderrFile.FullName } else { '' }
        return [pscustomobject]@{
            ExitCode = [int]$proc.ExitCode
            TimedOut = $false
            StdOut = $outText
            StdErr = $errText
        }
    }
    finally {
        Remove-Item -Force $stdoutFile.FullName, $stderrFile.FullName -ErrorAction SilentlyContinue
    }
}

function Run-SubprocessCommand {
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [int]$TimeoutMs = 30000,
        [bool]$NonInteractive,
        [string]$Command,
        [string]$ContextId
    )

    Write-Verbose "[RDX][LOG][$ContextId] launch=$Command args=$($ArgumentList -join ' ')"
    $result = Invoke-Subprocess -FilePath $FilePath -ArgumentList $ArgumentList -WorkingDirectory $WorkingDirectory -TimeoutMs $TimeoutMs
    $mapped = Emit-ChildResult -ExitCode $result.ExitCode -TimedOut $result.TimedOut -Output $result.StdOut -Error $result.StdErr -Command $Command -ContextId $ContextId -AsJson:$NonInteractive
    Write-Verbose "[RDX][LOG][$ContextId] done=$Command exit=$mapped"
    return $mapped
}

function Normalize-CommandContextArgs {
    param(
        [string[]]$CommandArgs,
        [string]$ContextId
    )

    $sanitized = [System.Collections.Generic.List[string]]::new()
    $resolvedContext = [string]$ContextId
    $i = 0
    while ($i -lt $CommandArgs.Count) {
        $arg = [string]$CommandArgs[$i]
        if ($arg -eq '--daemon-context' -or $arg -eq '--context-id') {
            if ($i + 1 -ge $CommandArgs.Count) {
                throw "missing value for $arg"
            }
            $resolvedContext = [string]$CommandArgs[$i + 1]
            $i += 2
            continue
        }

        $sanitized.Add($arg)
        $i += 1
    }

    $normalized = [System.Collections.Generic.List[string]]::new()
    $normalized.Add('--daemon-context')
    $normalized.Add($resolvedContext)
    foreach ($value in $sanitized) {
        $normalized.Add($value)
    }
    return $normalized
}

function Run-Mcp {
    param(
        [string[]]$CommandArgs,
        [string]$ToolsRoot,
        [string]$ContextId,
        [bool]$NonInteractive
    )

    $pythonCmd = @((Normalize-PythonCommand -PythonSpec (Resolve-Python)))
    if ($pythonCmd.Count -eq 0) {
        Write-JsonStatus -Ok $false -ErrorCode $script:RETURN_ENV_ERROR -ErrorMessage 'python executable not resolved' -ContextId $ContextId
        return $script:RETURN_ENV_ERROR
    }

    $pythonExe = [string]$pythonCmd[0]
    if ([string]::IsNullOrWhiteSpace($pythonExe) -or -not (Test-Path -LiteralPath $pythonExe)) {
        Write-JsonStatus -Ok $false -ErrorCode $script:RETURN_ENV_ERROR -ErrorMessage 'invalid python executable path' -ContextId $ContextId
        return $script:RETURN_ENV_ERROR
    }

    $pythonArgs = if ($pythonCmd.Count -gt 1) { @($pythonCmd[1..($pythonCmd.Count - 1)]) } else { @() }
    $scriptPath = Join-Path $ToolsRoot 'mcp\run_mcp.py'

    $cmdArgs = [System.Collections.Generic.List[string]]::new()
    $cmdArgs.Add($scriptPath)
    foreach ($a in (Normalize-CommandContextArgs -CommandArgs $CommandArgs -ContextId $ContextId)) {
        $cmdArgs.Add($a)
    }

    $finalArgs = @($pythonArgs) + $cmdArgs
    return Run-SubprocessCommand -FilePath $pythonExe -ArgumentList $finalArgs -WorkingDirectory $ToolsRoot -TimeoutMs 45000 -NonInteractive:$NonInteractive -Command 'mcp' -ContextId $ContextId
}

function Run-Cli {
    param(
        [string[]]$CommandArgs,
        [string]$ToolsRoot,
        [string]$ContextId,
        [bool]$NonInteractive,
        [int]$TimeoutMs = 30000,
        [string]$CommandName = 'cli'
    )

    $pythonCmd = @((Normalize-PythonCommand -PythonSpec (Resolve-Python)))
    if ($pythonCmd.Count -eq 0) {
        Write-JsonStatus -Ok $false -ErrorCode $script:RETURN_ENV_ERROR -ErrorMessage 'python executable not resolved' -ContextId $ContextId
        return $script:RETURN_ENV_ERROR
    }

    $pythonExe = [string]$pythonCmd[0]
    if ([string]::IsNullOrWhiteSpace($pythonExe) -or -not (Test-Path -LiteralPath $pythonExe)) {
        Write-JsonStatus -Ok $false -ErrorCode $script:RETURN_ENV_ERROR -ErrorMessage 'invalid python executable path' -ContextId $ContextId
        return $script:RETURN_ENV_ERROR
    }

    $pythonArgs = if ($pythonCmd.Count -gt 1) { @($pythonCmd[1..($pythonCmd.Count - 1)]) } else { @() }
    $scriptPath = Join-Path $ToolsRoot 'cli\run_cli.py'

    $cmdArgs = [System.Collections.Generic.List[string]]::new()
    $cmdArgs.Add($scriptPath)
    foreach ($a in (Normalize-CommandContextArgs -CommandArgs $CommandArgs -ContextId $ContextId)) {
        $cmdArgs.Add($a)
    }

    $finalArgs = @($pythonArgs) + $cmdArgs
    return Run-SubprocessCommand -FilePath $pythonExe -ArgumentList $finalArgs -WorkingDirectory $ToolsRoot -TimeoutMs $TimeoutMs -NonInteractive:$NonInteractive -Command $CommandName -ContextId $ContextId
}

function Resolve-DaemonShellContext {
    param(
        [string[]]$CommandArgs,
        [string]$ContextId,
        [ref]$Steps
    )

    $resolvedContext = $ContextId
    $stepsList = [System.Collections.Generic.List[string]]::new()
    $i = 0
    while ($i -lt $CommandArgs.Count) {
        $arg = [string]$CommandArgs[$i]
        if ($arg -in @('--daemon-context', '--context-id')) {
            if ($i + 1 -ge $CommandArgs.Count) { throw "missing value for $arg" }
            $resolvedContext = [string]$CommandArgs[$i + 1]
            $i += 2
            continue
        }

        $stepsList.Add($arg)
        $i += 1
    }

    if ($stepsList.Count -eq 0) {
        $stepsList.AddRange([string[]]@('start', 'status', 'stop'))
    }

    $Steps.Value = $stepsList
    return $resolvedContext
}

function Run-CliShell {
    param(
        [string[]]$CommandArgs,
        [string]$ToolsRoot,
        [string]$ContextId,
        [bool]$NonInteractive
    )

    if ($NonInteractive) {
        $CommandArgsToRun = if ($CommandArgs.Count -eq 0) { @('--help') } else { $CommandArgs }
        return Run-Cli -CommandArgs $CommandArgsToRun -ToolsRoot $ToolsRoot -ContextId $ContextId -NonInteractive $NonInteractive -CommandName 'cli-shell'
    }

    Write-Output 'rdx.bat cli-shell: this path is designed for non-interactive automation.'
    return $script:RETURN_OK
}

function Run-DaemonShell {
    param(
        [string[]]$CommandArgs,
        [string]$ToolsRoot,
        [string]$ContextId,
        [bool]$NonInteractive
    )

    if (-not $NonInteractive) {
        Write-Output 'rdx.bat daemon-shell: use --non-interactive for deterministic smoke path.'
        Write-Output 'Usage: rdx.bat --non-interactive daemon-shell [--daemon-context <ctx>] [start|status|stop]'
        return $script:RETURN_ARGS_ERROR
    }

    $steps = [System.Collections.Generic.List[string]]::new()
    try {
        $resolvedContext = Resolve-DaemonShellContext -CommandArgs $CommandArgs -ContextId $ContextId -Steps ([ref]$steps)
    }
    catch {
        Write-JsonStatus -Ok $false -ErrorCode $script:RETURN_ARGS_ERROR -ErrorMessage $_.Exception.Message -ContextId $ContextId
        return $script:RETURN_ARGS_ERROR
    }

    $workflowDetails = @{}
    $code = $script:RETURN_OK
    foreach ($step in $steps) {
        if ($step -in @('start', 'status', 'stop')) {
            $stepResult = Invoke-CommandWithOutput -CommandScript {
                Run-Cli -CommandArgs @('daemon', $step) -ToolsRoot $ToolsRoot -ContextId $resolvedContext -NonInteractive $true -CommandName 'daemon-shell' -TimeoutMs 40000
            }
            $stepCode = [int]$stepResult.ExitCode
            $workflowDetails[$step] = $stepCode
            if (($step -ne 'stop') -and ($stepCode -ne $script:RETURN_OK)) {
                $code = $script:RETURN_TOOL_ERROR
                break
            }
            continue
        }

        if ($step -in @('help', '-h', '--help')) {
            Write-Output 'daemon-shell actions: start | status | stop'
            continue
        }

        $code = $script:RETURN_ARGS_ERROR
        $workflowDetails[$step] = 'unsupported'
        break
    }

    if ($code -eq $script:RETURN_OK) {
        $summaryCode = 0
        foreach ($value in $workflowDetails.Values) {
            if ($value -is [int] -and $value -ne $script:RETURN_OK) {
                $summaryCode = $script:RETURN_TOOL_ERROR
                break
            }
        }
        Write-JsonStatus -Ok ($summaryCode -eq 0) -ErrorCode $summaryCode -ErrorMessage $(if ($summaryCode -eq 0) { '' } else { 'daemon-shell workflow failed' }) -ContextId $resolvedContext -Details $workflowDetails
        return $summaryCode
    }

    Write-JsonStatus -Ok $false -ErrorCode $code -ErrorMessage 'daemon-shell workflow failed' -ContextId $resolvedContext -Details $workflowDetails
    return $code
}

function Invoke-CommandWithOutput {
    param([scriptblock]$CommandScript)

    $items = @(& $CommandScript)
    if ($items.Count -eq 0) {
        return [pscustomobject]@{ ExitCode = $script:RETURN_TOOL_ERROR; Output = @() }
    }

    $exit = $script:RETURN_TOOL_ERROR
    if ($items[-1] -is [int]) {
        $exit = [int]$items[-1]
        if ($items.Count -gt 1) {
            $output = $items[0..($items.Count - 2)]
        } else {
            $output = @()
        }
    }
    else {
        $output = $items
    }

    $textOutput = @()
    foreach ($item in $output) {
        if ($null -eq $item) {
            continue
        }
        if ($item -is [string]) {
            $textOutput += $item
        } else {
            $textOutput += [string]$item
        }
    }

    return [pscustomobject]@{ ExitCode = $exit; Output = $textOutput }
}

function Write-Help {
    Write-Output 'rdx.bat usage:'
    Write-Output '  rdx.bat --help'
    Write-Output '  rdx.bat --non-interactive mcp --ensure-env [--daemon-context <ctx>]'
    Write-Output '  rdx.bat --non-interactive cli-shell [--daemon-context <ctx>] [--help]'
    Write-Output '  rdx.bat --non-interactive daemon-shell [--daemon-context <ctx>] [start|status|stop]'
}

$toolsRoot = Resolve-ToolsRoot
Ensure-ToolsRootEnv -ToolsRoot $toolsRoot

try {
    $parsed = Parse-LauncherArgs
}
catch {
    Write-JsonStatus -Ok $false -ErrorCode $script:RETURN_ARGS_ERROR -ErrorMessage $_.Exception.Message -ContextId 'default'
    exit $script:RETURN_ARGS_ERROR
}

$exitCode = $script:RETURN_OK
$contextId = [string]$parsed.ContextId
$nonInteractive = [bool]$parsed.NonInteractive
$command = [string]$parsed.Command
$commandArgs = @($parsed.CommandArgs)

if (-not $command) {
    if ($nonInteractive) {
        Write-JsonStatus -Ok $false -ErrorCode $script:RETURN_ARGS_ERROR -ErrorMessage 'missing subcommand' -ContextId $contextId
        exit $script:RETURN_ARGS_ERROR
    }
    Write-Help
    exit $script:RETURN_OK
}

switch ($command) {
    'help' {
        Write-Help
        $exitCode = $script:RETURN_OK
    }
    'mcp' {
        $commandResult = Invoke-CommandWithOutput -CommandScript { Run-Mcp -CommandArgs $commandArgs -ToolsRoot $toolsRoot -ContextId $contextId -NonInteractive:$nonInteractive }
        $exitCode = $commandResult.ExitCode
        if ($commandResult.Output.Count -gt 0) {
            Write-Output ($commandResult.Output -join "`n")
        }
    }
    'cli-shell' {
        $commandResult = Invoke-CommandWithOutput -CommandScript { Run-CliShell -CommandArgs $commandArgs -ToolsRoot $toolsRoot -ContextId $contextId -NonInteractive:$nonInteractive }
        $exitCode = $commandResult.ExitCode
        if ($commandResult.Output.Count -gt 0) {
            Write-Output ($commandResult.Output -join "`n")
        }
    }
    'daemon-shell' {
        $commandResult = Invoke-CommandWithOutput -CommandScript { Run-DaemonShell -CommandArgs $commandArgs -ToolsRoot $toolsRoot -ContextId $contextId -NonInteractive:$nonInteractive }
        $exitCode = $commandResult.ExitCode
        if ($commandResult.Output.Count -gt 0) {
            Write-Output ($commandResult.Output -join "`n")
        }
    }
    default {
        if ($nonInteractive) {
            Write-JsonStatus -Ok $false -ErrorCode $script:RETURN_ARGS_ERROR -ErrorMessage "unknown command: $command" -ContextId $contextId
            $exitCode = $script:RETURN_ARGS_ERROR
        }
        else {
            Write-Output "unknown command: $command"
            Write-Output 'Run: rdx.bat --help'
            $exitCode = $script:RETURN_ARGS_ERROR
        }
    }
}

if ($exitCode -lt 0 -or $exitCode -gt 5) {
    $exitCode = $script:RETURN_TOOL_ERROR
}

exit $exitCode















