[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$script:RETURN_OK = 0
$script:RETURN_ARGS_ERROR = 1
$script:RETURN_ENV_ERROR = 2
$script:RETURN_STARTUP_ERROR = 3
$script:RETURN_TIMEOUT = 4
$script:RETURN_TOOL_ERROR = 5
$script:DEFAULT_LEASE_TIMEOUT_SECONDS = 120

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

function Ensure-ToolsRootEnv {
    param([string]$ToolsRoot)
    $env:RDX_TOOLS_ROOT = $ToolsRoot
}

function Test-PythonCandidate {
    param([object]$PythonSpec)

    $candidate = @(Normalize-PythonCommand -PythonSpec $PythonSpec)
    if ($candidate.Count -eq 0) {
        return $false
    }

    $exe = [string]$candidate[0]
    if ([string]::IsNullOrWhiteSpace($exe)) {
        return $false
    }

    $probeArgs = @()
    if ($candidate.Count -gt 1) {
        $probeArgs += @($candidate[1..($candidate.Count - 1)])
    }
    $probeArgs += @('-c', 'import sys; print(sys.executable)')

    try {
        $probeOutput = @(& $exe @probeArgs 2>&1)
        $probeExitCode = if ($null -eq $LASTEXITCODE) { 0 } else { [int]$LASTEXITCODE }
    }
    catch {
        return $false
    }

    if ($probeExitCode -ne 0) {
        return $false
    }

    $joined = (($probeOutput | ForEach-Object { [string]$_ }) -join "`n").Trim()
    return (-not [string]::IsNullOrWhiteSpace($joined))
}

function Get-BundledPythonLayout {
    param([string]$ToolsRoot)

    $pythonRoot = Join-Path $ToolsRoot 'binaries\windows\x64\python'
    $pythonExe = Join-Path $pythonRoot 'python.exe'
    $pythonwExe = Join-Path $pythonRoot 'pythonw.exe'
    $python3Dll = Join-Path $pythonRoot 'python3.dll'
    $stdlibDir = Join-Path $pythonRoot 'Lib'
    $sitePackagesDir = Join-Path $stdlibDir 'site-packages'
    $dllDir = Join-Path $pythonRoot 'DLLs'
    $versionDll = @(Get-ChildItem -LiteralPath $pythonRoot -Filter 'python*.dll' -File -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^python\d+\.dll$' } | Sort-Object Name | Select-Object -First 1)
    $pthFile = @(Get-ChildItem -LiteralPath $pythonRoot -Filter 'python*._pth' -File -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -First 1)

    return [pscustomobject]@{
        Root = $pythonRoot
        Executable = $pythonExe
        Pythonw = $pythonwExe
        Python3Dll = $python3Dll
        VersionDll = if ($versionDll.Count -gt 0) { $versionDll[0].FullName } else { '' }
        StdlibDir = $stdlibDir
        SitePackagesDir = $sitePackagesDir
        DllDir = $dllDir
        PthFile = if ($pthFile.Count -gt 0) { $pthFile[0].FullName } else { '' }
    }
}

function Resolve-Python {
    $envPython = [string]$env:RDX_PYTHON
    if ($envPython) {
        if ((Test-Path -LiteralPath $envPython) -and (Test-PythonCandidate -PythonSpec @([string]$envPython))) {
            return @([string]$envPython)
        }
        throw "RDX_PYTHON is set but invalid: $envPython"
    }

    $toolsRoot = [string]$env:RDX_TOOLS_ROOT
    if ([string]::IsNullOrWhiteSpace($toolsRoot)) {
        $toolsRoot = Resolve-ToolsRoot
    }

    $layout = Get-BundledPythonLayout -ToolsRoot $toolsRoot
    $failures = [System.Collections.Generic.List[string]]::new()
    if (-not (Test-Path -LiteralPath $layout.Root -PathType Container)) { $failures.Add("missing directory: $($layout.Root)") }
    if (-not (Test-Path -LiteralPath $layout.Executable -PathType Leaf)) { $failures.Add("missing file: $($layout.Executable)") }
    if (-not (Test-Path -LiteralPath $layout.Pythonw -PathType Leaf)) { $failures.Add("missing file: $($layout.Pythonw)") }
    if (-not (Test-Path -LiteralPath $layout.Python3Dll -PathType Leaf)) { $failures.Add("missing file: $($layout.Python3Dll)") }
    if ([string]::IsNullOrWhiteSpace([string]$layout.VersionDll) -or -not (Test-Path -LiteralPath $layout.VersionDll -PathType Leaf)) { $failures.Add('missing versioned python dll') }
    if (-not (Test-Path -LiteralPath $layout.StdlibDir -PathType Container)) { $failures.Add("missing directory: $($layout.StdlibDir)") }
    if (-not (Test-Path -LiteralPath $layout.SitePackagesDir -PathType Container)) { $failures.Add("missing directory: $($layout.SitePackagesDir)") }
    if (-not (Test-Path -LiteralPath $layout.DllDir -PathType Container)) { $failures.Add("missing directory: $($layout.DllDir)") }
    if ([string]::IsNullOrWhiteSpace([string]$layout.PthFile) -or -not (Test-Path -LiteralPath $layout.PthFile -PathType Leaf)) { $failures.Add('missing python _pth file') }
    if (-not (Test-Path -LiteralPath (Join-Path $layout.StdlibDir 'os.py') -PathType Leaf)) { $failures.Add('missing stdlib marker: Lib\os.py') }
    if (-not (Test-Path -LiteralPath (Join-Path $layout.StdlibDir 'site.py') -PathType Leaf)) { $failures.Add('missing stdlib marker: Lib\site.py') }
    if (-not (Test-Path -LiteralPath (Join-Path $layout.StdlibDir 'encodings\__init__.py') -PathType Leaf)) { $failures.Add('missing stdlib marker: Lib\encodings\__init__.py') }

    if ($failures.Count -gt 0) {
        throw ('bundled python runtime incomplete: ' + ($failures -join '; '))
    }
    if (-not (Test-PythonCandidate -PythonSpec @([string]$layout.Executable))) {
        throw "bundled python executable is not runnable: $($layout.Executable)"
    }
    return @([string]$layout.Executable)
}

function Normalize-PythonCommand {
    param([object]$PythonSpec)

    if ($null -eq $PythonSpec) { return @() }
    if ($PythonSpec -is [string]) { return @([string]$PythonSpec) }
    if ($PythonSpec -is [System.Array]) { return [string[]]$PythonSpec }
    return @([string]$PythonSpec)
}

function Quote-CommandArg {
    param([string]$Value)

    if ($Value -match '\s|"|`"') {
        return '"' + $Value.Replace('"', '\"') + '"'
    }
    return $Value
}

function Get-PythonInvocation {
    $pythonCmd = @((Normalize-PythonCommand -PythonSpec (Resolve-Python)))
    if ($pythonCmd.Count -eq 0) {
        throw 'python executable not resolved'
    }

    $pythonExe = [string]$pythonCmd[0]
    $extraArgs = if ($pythonCmd.Count -gt 1) { @($pythonCmd[1..($pythonCmd.Count - 1)]) } else { @() }
    $parts = [System.Collections.Generic.List[string]]::new()
    $parts.Add((Quote-CommandArg $pythonExe))
    foreach ($arg in $extraArgs) {
        $parts.Add((Quote-CommandArg ([string]$arg)))
    }

    return [pscustomobject]@{
        Executable = $pythonExe
        ExtraArgs = $extraArgs
        CommandText = ($parts -join ' ')
    }
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

function Extract-JsonText {
    param([string]$Text)

    if ([string]::IsNullOrWhiteSpace($Text)) { return $null }
    $start = $Text.IndexOf('{')
    if ($start -lt 0) { return $null }
    $end = $Text.LastIndexOf('}')
    if ($end -le $start) { return $null }
    return $Text.Substring($start, $end - $start + 1)
}

function Extract-JsonPayload {
    param([string]$Text)

    $candidate = Extract-JsonText $Text
    if ([string]::IsNullOrWhiteSpace($candidate)) { return $null }
    try {
        return $candidate | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        return $null
    }
}

function Get-JsonStringProperty {
    param(
        [object]$Object,
        [string]$PropertyName
    )

    if ($null -eq $Object -or [string]::IsNullOrWhiteSpace($PropertyName)) {
        return ''
    }
    $property = $Object.PSObject.Properties[$PropertyName]
    if ($null -eq $property) {
        return ''
    }
    return [string]$property.Value
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
        [pscustomobject]$Payload
    )

    if ($TimedOut) { return $script:RETURN_TIMEOUT }
    if ($Payload -and $Payload.PSObject.Properties.Name -contains 'ok') {
        if ([bool]$Payload.ok) { return $script:RETURN_OK }
        $code = ''
        if ($Payload.PSObject.Properties.Name -contains 'error_code') {
            $code = [string]$Payload.error_code
        }
        elseif (($Payload.PSObject.Properties.Name -contains 'error') -and $Payload.error) {
            $code = Get-JsonStringProperty -Object $Payload.error -PropertyName 'code'
        }
        $codeLower = $code.ToLowerInvariant()
        if ($codeLower -in @('dependencies_missing', 'runtime_layout_missing', 'renderdoc_import_failed', 'runtime_root_invalid', 'python_runtime_incomplete')) { return $script:RETURN_ENV_ERROR }
        if ($codeLower -in @('startup_failed', 'daemon_not_ready', 'no_python_found')) { return $script:RETURN_STARTUP_ERROR }
        if ($codeLower -eq 'timeout') { return $script:RETURN_TIMEOUT }
        return $script:RETURN_TOOL_ERROR
    }
    if ($ChildExitCode -in @(0, 1, 2, 3, 4, 5)) { return $ChildExitCode }
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

    $jsonText = Extract-JsonText ($Output + "`n" + $Error)
    $payload = Extract-JsonPayload ($Output + "`n" + $Error)
    if ($payload -and $payload.PSObject.Properties.Name -contains 'ok') {
        if ($jsonText) { Write-Output $jsonText.Trim() }
        return Map-ExitCode -ChildExitCode $ExitCode -TimedOut $TimedOut -Payload $payload
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

function Invoke-CommandWithOutput {
    param([scriptblock]$CommandScript)

    $items = @(& $CommandScript)
    if ($items.Count -eq 0) {
        return [pscustomobject]@{ ExitCode = $script:RETURN_TOOL_ERROR; Output = @() }
    }

    $exit = $script:RETURN_TOOL_ERROR
    if ($items[-1] -is [int]) {
        $exit = [int]$items[-1]
        $output = if ($items.Count -gt 1) { $items[0..($items.Count - 2)] } else { @() }
    }
    else {
        $output = $items
    }

    $textOutput = @()
    foreach ($item in $output) {
        if ($null -eq $item) { continue }
        if ($item -is [string]) { $textOutput += $item } else { $textOutput += [string]$item }
    }
    return [pscustomobject]@{ ExitCode = $exit; Output = $textOutput }
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

function Invoke-PythonTool {
    param(
        [string]$ToolsRoot,
        [string]$ScriptRelativePath,
        [string[]]$ArgumentList,
        [int]$TimeoutMs = 30000,
        [string]$LauncherProg = ''
    )

    $python = Get-PythonInvocation
    $scriptPath = Join-Path $ToolsRoot $ScriptRelativePath
    $finalArgs = @($python.ExtraArgs) + @($scriptPath) + @($ArgumentList)
    $hadLauncherProg = Test-Path Env:\RDX_LAUNCHER_PROG
    $savedLauncherProg = [string]$env:RDX_LAUNCHER_PROG
    try {
        if ($LauncherProg) {
            $env:RDX_LAUNCHER_PROG = $LauncherProg
        }
        elseif ($hadLauncherProg) {
            Remove-Item Env:\RDX_LAUNCHER_PROG -ErrorAction SilentlyContinue
        }
        return Invoke-Subprocess -FilePath $python.Executable -ArgumentList $finalArgs -WorkingDirectory $ToolsRoot -TimeoutMs $TimeoutMs
    }
    finally {
        if ($hadLauncherProg) {
            $env:RDX_LAUNCHER_PROG = $savedLauncherProg
        }
        else {
            Remove-Item Env:\RDX_LAUNCHER_PROG -ErrorAction SilentlyContinue
        }
    }
}

function Invoke-CliCommand {
    param(
        [string]$ToolsRoot,
        [string]$ContextId,
        [string[]]$CommandArgs,
        [int]$TimeoutMs = 30000
    )

    $normalizedArgs = Normalize-CommandContextArgs -CommandArgs $CommandArgs -ContextId $ContextId
    return Invoke-PythonTool -ToolsRoot $ToolsRoot -ScriptRelativePath 'cli\run_cli.py' -ArgumentList $normalizedArgs -TimeoutMs $TimeoutMs -LauncherProg 'rdx.bat --non-interactive cli'
}

function Invoke-McpCommand {
    param(
        [string]$ToolsRoot,
        [string]$ContextId,
        [string[]]$CommandArgs,
        [int]$TimeoutMs = 30000
    )

    $normalizedArgs = Normalize-CommandContextArgs -CommandArgs $CommandArgs -ContextId $ContextId
    return Invoke-PythonTool -ToolsRoot $ToolsRoot -ScriptRelativePath 'mcp\run_mcp.py' -ArgumentList $normalizedArgs -TimeoutMs $TimeoutMs -LauncherProg 'rdx.bat --non-interactive mcp'
}
function Invoke-CliJson {
    param(
        [string]$ToolsRoot,
        [string]$ContextId,
        [string[]]$CommandArgs,
        [int]$TimeoutMs = 30000
    )

    $result = Invoke-CliCommand -ToolsRoot $ToolsRoot -ContextId $ContextId -CommandArgs $CommandArgs -TimeoutMs $TimeoutMs
    return [pscustomobject]@{
        ExitCode = [int]$result.ExitCode
        TimedOut = [bool]$result.TimedOut
        StdOut = [string]$result.StdOut
        StdErr = [string]$result.StdErr
        Payload = (Extract-JsonPayload ($result.StdOut + "`n" + $result.StdErr))
    }
}

function Get-TestMode {
    foreach ($value in @([string]$env:RDX_BAT_TEST_MODE, [string]$env:RDX_BAT_NO_NEW_WINDOW)) {
        if ([string]::IsNullOrWhiteSpace($value)) { continue }
        if ($value.Trim().ToLowerInvariant() -in @('1', 'true', 'yes', 'on')) { return $true }
    }
    return $false
}

function Test-ValidContextId {
    param([string]$ContextId)
    return (-not [string]::IsNullOrWhiteSpace($ContextId)) -and (-not $ContextId.Contains('"'))
}

function Read-LauncherLine {
    param(
        [string]$Prompt,
        [bool]$AllowBlank = $true
    )

    while ($true) {
        if ($Prompt) { Write-Host -NoNewline $Prompt }
        $value = [Console]::In.ReadLine()
        if ($null -eq $value) { return '' }
        if ($AllowBlank -or -not [string]::IsNullOrWhiteSpace($value)) { return [string]$value }
    }
}

function Write-TerseUsage {
    Write-Output 'rdx.bat usage:'
    Write-Output '  rdx.bat'
    Write-Output '  rdx.bat --help'
    Write-Output '  rdx.bat --non-interactive mcp --ensure-env [--daemon-context <ctx>]'
    Write-Output '  rdx.bat --non-interactive cli --help'
    Write-Output '  rdx.bat --non-interactive cli --daemon-context <ctx> daemon status'
}

function Write-HelpPage {
    Write-Host ''
    Write-Host '=== rdx-tools Launcher Help ==='
    Write-Host ''
    Write-Host 'rdx-tools is the local MCP + CLI toolset for RenderDoc.'
    Write-Host 'rdx.bat has two modes: interactive launcher and --non-interactive machine mode.'
    Write-Host ''
    Write-Host 'Main entries'
    Write-Host '- Start CLI: human shell backed by a daemon and a persistent context.'
    Write-Host '- Start MCP: MCP endpoint using the same daemon/context model.'
    Write-Host '- Help: launcher help, examples, and context notes.'
    Write-Host ''
    Write-Host 'MCP transport'
    Write-Host '- stdio: no URL; the client owns stdin/stdout.'
    Write-Host '- streamable-http: shows host:port for HTTP access.'
    Write-Host ''
    Write-Host 'CLI examples'
    Write-Host '  rdx capture open --file <capture.rdc> --frame-index 0'
    Write-Host '  rdx capture status'
    Write-Host '  rdx call rd.event.get_actions --args-json <json> --format json'
    Write-Host '  rdx call rd.session.get_context --args-file <path> --format json'
    Write-Host '  rdx vfs ls --path / --format tsv'
    Write-Host '  rdx.bat --non-interactive cli daemon status'
    Write-Host '  rdx daemon status'
    Write-Host '  rdx daemon stop'
    Write-Host '  rdx context clear'
    Write-Host '  status'
    Write-Host '  stop'
    Write-Host '  clear'
    Write-Host ''
    Write-Host 'Tool discovery'
    Write-Host '- spec/tool_catalog.json'
    Write-Host '- docs/'
    Write-Host ''
    Write-Host 'Context model'
    Write-Host '- context isolates capture, session, active event, and debug state.'
    Write-Host '- use default for one interactive flow'
    Write-Host '- use custom contexts for isolated parallel flows'
    Write-Host '- CLI and MCP share daemon mechanics but stay isolated by context.'
    Write-Host ''
}

function Pause-ForMenu {
    [void](Read-LauncherLine -Prompt 'Press Enter to return to the menu...' -AllowBlank $true)
}

function Select-ContextInteractive {
    while ($true) {
        Write-Host ''
        Write-Host 'Select context'
        Write-Host '1. default'
        Write-Host '2. Custom context'
        Write-Host '0. Back'
        $choice = (Read-LauncherLine -Prompt 'Select: ' -AllowBlank $false).Trim()
        switch ($choice) {
            '1' { return [pscustomobject]@{ Cancelled = $false; ContextId = 'default' } }
            '2' {
                while ($true) {
                    $custom = (Read-LauncherLine -Prompt 'Custom context: ' -AllowBlank $false).Trim()
                    if (Test-ValidContextId -ContextId $custom) {
                        return [pscustomobject]@{ Cancelled = $false; ContextId = $custom }
                    }
                    Write-Host '[RDX][ERR] context cannot be blank and cannot contain double quotes.'
                }
            }
            '0' { return [pscustomobject]@{ Cancelled = $true; ContextId = '' } }
            default { Write-Host '[RDX][ERR] Invalid selection. Try again.' }
        }
    }
}

function Select-McpTransportInteractive {
    while ($true) {
        Write-Host ''
        Write-Host 'Select MCP transport'
        Write-Host '1. stdio'
        Write-Host '2. streamable-http'
        Write-Host '0. Back'
        $choice = (Read-LauncherLine -Prompt 'Select: ' -AllowBlank $false).Trim()
        switch ($choice) {
            '1' { return [pscustomobject]@{ Cancelled = $false; Transport = 'stdio'; Host = ''; Port = 0 } }
            '2' {
                $listenHost = (Read-LauncherLine -Prompt 'Host (default 127.0.0.1): ' -AllowBlank $true).Trim()
                if ([string]::IsNullOrWhiteSpace($listenHost)) { $listenHost = '127.0.0.1' }
                while ($true) {
                    $portRaw = (Read-LauncherLine -Prompt 'Port (default 8765): ' -AllowBlank $true).Trim()
                    if ([string]::IsNullOrWhiteSpace($portRaw)) { $portRaw = '8765' }
                    $port = 0
                    if ([int]::TryParse($portRaw, [ref]$port) -and $port -gt 0 -and $port -le 65535) {
                        return [pscustomobject]@{ Cancelled = $false; Transport = 'streamable-http'; Host = $listenHost; Port = $port }
                    }
                    Write-Host '[RDX][ERR] Port must be an integer in the range 1-65535.'
                }
            }
            '0' { return [pscustomobject]@{ Cancelled = $true; Transport = ''; Host = ''; Port = 0 } }
            default { Write-Host '[RDX][ERR] Invalid selection. Try again.' }
        }
    }
}

function New-LauncherCmdFile {
    param(
        [string]$Prefix,
        [string[]]$Lines
    )

    $tempDir = [System.IO.Path]::GetTempPath()
    $fileName = '{0}-{1}.cmd' -f $Prefix, ([guid]::NewGuid().ToString('N'))
    $path = Join-Path $tempDir $fileName
    $content = @($Lines) -join "`r`n"
    Set-Content -LiteralPath $path -Value $content -Encoding ASCII
    return $path
}

function Build-CmdLaunchArgument {
    param([string]$ScriptPath)
    return ('call "{0}" & del /q "{0}"' -f $ScriptPath)
}

function New-CliShellCommandFile {
    param(
        [string]$ToolsRoot,
        [string]$ContextId
    )

    $cliPath = Join-Path $ToolsRoot 'cli\run_cli.py'
    $pythonText = (Get-PythonInvocation).CommandText
    $lines = @(
        '@echo off',
        ('set "RDX_TOOLS_ROOT={0}"' -f $ToolsRoot),
        ('set "RDX_CONTEXT_ID={0}"' -f $ContextId),
        'set "RDX_LAUNCHER_PROG=rdx"',
        ('title RDX CLI [{0}]' -f $ContextId),
        ('prompt [rdx:{0}] $P$G' -f $ContextId),
        ('doskey rdx={0} "{1}" --daemon-context "{2}" $*' -f $pythonText, $cliPath, $ContextId),
        ('doskey status={0} "{1}" --daemon-context "{2}" daemon status' -f $pythonText, $cliPath, $ContextId),
        ('doskey stop={0} "{1}" --daemon-context "{2}" daemon stop' -f $pythonText, $cliPath, $ContextId),
        ('doskey clear={0} "{1}" --daemon-context "{2}" context clear' -f $pythonText, $cliPath, $ContextId),
        'doskey quit=exit',
        'echo.',
        ('echo [RDX] CLI shell ready. context={0}' -f $ContextId),
        'echo [RDX] exit or quit only closes this shell. daemon and context stay alive by default.',
        'echo [RDX] Common commands: status, stop, clear, rdx capture status',
        'echo.'
    )
    return New-LauncherCmdFile -Prefix 'rdx-cli-shell' -Lines $lines
}

function New-McpCommandFile {
    param(
        [string]$ToolsRoot,
        [string]$ContextId,
        [string]$Transport,
        [string]$Host,
        [int]$Port
    )

    $mcpPath = Join-Path $ToolsRoot 'mcp\run_mcp.py'
    $pythonText = (Get-PythonInvocation).CommandText
    $lines = @(
        '@echo off',
        ('set "RDX_TOOLS_ROOT={0}"' -f $ToolsRoot),
        ('set "RDX_CONTEXT_ID={0}"' -f $ContextId),
        'set "RDX_LAUNCHER_PROG=rdx.bat --non-interactive mcp"',
        ('title RDX MCP [{0}]' -f $ContextId),
        'echo.',
        ('echo [RDX] Start MCP. context={0}' -f $ContextId),
        ('echo [RDX] transport={0}' -f $Transport)
    )
    if ($Transport -eq 'stdio') {
        $lines += 'echo [RDX] URL: no URL'
        $lines += ('{0} "{1}" --daemon-context "{2}" --transport stdio' -f $pythonText, $mcpPath, $ContextId)
    }
    else {
        $lines += ('echo [RDX] URL: http://{0}:{1}' -f $Host, $Port)
        $lines += ('{0} "{1}" --daemon-context "{2}" --transport streamable-http --host "{3}" --port {4}' -f $pythonText, $mcpPath, $ContextId, $Host, $Port)
    }
    $lines += 'echo.'
    $lines += 'echo [RDX] MCP process exited.'
    $lines += 'echo.'
    return New-LauncherCmdFile -Prefix 'rdx-mcp-shell' -Lines $lines
}

function Ensure-DaemonContext {
    param(
        [string]$ToolsRoot,
        [string]$ContextId
    )

    $result = Invoke-CliJson -ToolsRoot $ToolsRoot -ContextId $ContextId -CommandArgs @('daemon', 'start') -TimeoutMs 45000
    if ($result.TimedOut) {
        return [pscustomobject]@{ Ok = $false; Message = 'daemon start timeout'; Result = $result }
    }
    if ($result.ExitCode -ne 0) {
        $payloadError = if ($result.Payload -and ($result.Payload.PSObject.Properties.Name -contains 'error')) { $result.Payload.error } else { $null }
        $message = Get-JsonStringProperty -Object $payloadError -PropertyName 'message'
        if (-not $message) { $message = ($result.StdErr + $result.StdOut).Trim() }
        if (-not $message) { $message = 'daemon start failed' }
        return [pscustomobject]@{ Ok = $false; Message = $message; Result = $result }
    }
    $message = ''
    if ($result.Payload -and ($result.Payload.PSObject.Properties.Name -contains 'data')) {
        $message = Get-JsonStringProperty -Object $result.Payload.data -PropertyName 'message'
    }
    if (-not $message) { $message = 'daemon ready' }
    return [pscustomobject]@{ Ok = $true; Message = $message; Result = $result }
}

function Invoke-CliInternalClientCommand {
    param(
        [string]$ToolsRoot,
        [string]$ContextId,
        [string]$Action,
        [hashtable]$Parameters,
        [int]$TimeoutMs = 20000
    )

    $argsList = [System.Collections.Generic.List[string]]::new()
    $argsList.Add('daemon')
    $argsList.Add($Action)
    foreach ($entry in $Parameters.GetEnumerator()) {
        $argsList.Add("--$($entry.Key)")
        $argsList.Add([string]$entry.Value)
    }
    return Invoke-CliJson -ToolsRoot $ToolsRoot -ContextId $ContextId -CommandArgs @($argsList) -TimeoutMs $TimeoutMs
}

function Start-ClientHeartbeatHelper {
    param(
        [string]$ToolsRoot,
        [string]$ContextId,
        [string]$ClientId,
        [string]$ClientType,
        [int]$WatchPid,
        [int]$LeaseTimeoutSeconds
    )

    $launcher = $PSCommandPath
    $hostArgs = @(
        '-NoProfile', '-NoLogo', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
        '-File', $launcher,
        '--non-interactive',
        '--internal', 'client-heartbeat',
        '--daemon-context', $ContextId,
        '--client-id', $ClientId,
        '--client-type', $ClientType,
        '--watch-pid', [string]$WatchPid,
        '--lease-timeout-seconds', [string]$LeaseTimeoutSeconds
    )
    Start-Process -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -ArgumentList $hostArgs -WindowStyle Hidden | Out-Null
}

function Run-ClientHeartbeatHelper {
    param(
        [string]$ToolsRoot,
        [string]$ContextId,
        [string]$ClientId,
        [string]$ClientType,
        [int]$WatchPid,
        [int]$LeaseTimeoutSeconds
    )

    $attach = Invoke-CliInternalClientCommand -ToolsRoot $ToolsRoot -ContextId $ContextId -Action 'attach' -Parameters @{
        'client-id' = $ClientId
        'client-type' = $ClientType
        'pid' = $WatchPid
        'lease-timeout-seconds' = $LeaseTimeoutSeconds
    } -TimeoutMs 30000
    if ($attach.ExitCode -ne 0) { return $script:RETURN_TOOL_ERROR }

    $sleepSeconds = [Math]::Max(10, [Math]::Floor($LeaseTimeoutSeconds / 2))
    while ($true) {
        Start-Sleep -Seconds $sleepSeconds
        $proc = Get-Process -Id $WatchPid -ErrorAction SilentlyContinue
        if (-not $proc) { break }
        $heartbeat = Invoke-CliInternalClientCommand -ToolsRoot $ToolsRoot -ContextId $ContextId -Action 'heartbeat' -Parameters @{
            'client-id' = $ClientId
            'pid' = $WatchPid
        } -TimeoutMs 20000
        if ($heartbeat.ExitCode -ne 0) { break }
    }

    [void](Invoke-CliInternalClientCommand -ToolsRoot $ToolsRoot -ContextId $ContextId -Action 'detach' -Parameters @{
        'client-id' = $ClientId
    } -TimeoutMs 20000)
    return $script:RETURN_OK
}

function Start-InteractiveCliShell {
    param(
        [string]$ToolsRoot,
        [string]$InitialContextId
    )

    if ($InitialContextId) {
        if (-not (Test-ValidContextId -ContextId $InitialContextId)) {
            Write-Host '[RDX][ERR] legacy context cannot be blank and cannot contain double quotes.'
            return
        }
        $resolvedContext = $InitialContextId
    }
    else {
        $selection = Select-ContextInteractive
        if ($selection.Cancelled) { return }
        $resolvedContext = [string]$selection.ContextId
    }

    $daemon = Ensure-DaemonContext -ToolsRoot $ToolsRoot -ContextId $resolvedContext
    if (-not $daemon.Ok) {
        Write-Host "[RDX][ERR] $($daemon.Message)"
        return
    }

    $cmdScript = New-CliShellCommandFile -ToolsRoot $ToolsRoot -ContextId $resolvedContext
    $cmdLaunch = Build-CmdLaunchArgument -ScriptPath $cmdScript
    $clientId = 'cli-' + [guid]::NewGuid().ToString('N').Substring(0, 12)
    $leaseTimeout = $script:DEFAULT_LEASE_TIMEOUT_SECONDS

    if (Get-TestMode) {
        [void](Invoke-CliInternalClientCommand -ToolsRoot $ToolsRoot -ContextId $resolvedContext -Action 'attach' -Parameters @{
            'client-id' = $clientId
            'client-type' = 'cli'
            'pid' = 0
            'lease-timeout-seconds' = $leaseTimeout
        } -TimeoutMs 20000)
        try {
            Write-Host ''
            Write-Host "[RDX] CLI shell ready. context=$resolvedContext"
            Write-Host '[RDX] exit or quit only closes this shell. daemon and context stay alive by default.'
            Write-Host '[RDX] Common commands: status, stop, clear, rdx daemon status, rdx daemon stop, rdx context clear'
            while ($true) {
                $line = (Read-LauncherLine -Prompt "[rdx:$resolvedContext] " -AllowBlank $true).Trim()
                if ([string]::IsNullOrWhiteSpace($line)) { continue }
                if ($line -in @('exit', 'quit')) { break }

                $invokeArgs = $null
                switch -Regex ($line) {
                    '^status$' { $invokeArgs = @('daemon', 'status'); break }
                    '^stop$' { $invokeArgs = @('daemon', 'stop'); break }
                    '^clear$' { $invokeArgs = @('context', 'clear'); break }
                    '^rdx\s+daemon\s+status$' { $invokeArgs = @('daemon', 'status'); break }
                    '^rdx\s+daemon\s+stop$' { $invokeArgs = @('daemon', 'stop'); break }
                    '^rdx\s+context\s+clear$' { $invokeArgs = @('context', 'clear'); break }
                }

                if ($null -eq $invokeArgs) {
                    Write-Host '[RDX][ERR] test mode shell supports status, stop, clear, exit, and simple rdx daemon/context commands.'
                    continue
                }

                $commandResult = Invoke-CliCommand -ToolsRoot $ToolsRoot -ContextId $resolvedContext -CommandArgs $invokeArgs -TimeoutMs 45000
                if ($commandResult.StdOut) { Write-Host $commandResult.StdOut.TrimEnd() }
                if ($commandResult.StdErr) { Write-Host $commandResult.StdErr.TrimEnd() }
            }
        }
        finally {
            [void](Invoke-CliInternalClientCommand -ToolsRoot $ToolsRoot -ContextId $resolvedContext -Action 'detach' -Parameters @{
                'client-id' = $clientId
            } -TimeoutMs 20000)
        }
        return
    }

    $proc = Start-Process -FilePath $env:ComSpec -ArgumentList @('/Q', '/K', $cmdLaunch) -WorkingDirectory $ToolsRoot -PassThru
    Start-ClientHeartbeatHelper -ToolsRoot $ToolsRoot -ContextId $resolvedContext -ClientId $clientId -ClientType 'cli' -WatchPid $proc.Id -LeaseTimeoutSeconds $leaseTimeout
    Write-Host ''
    Write-Host "[RDX] CLI shell started. context=$resolvedContext"
    Write-Host '[RDX] Closing the shell does not stop the daemon. Use stop or rdx daemon stop to stop it explicitly.'
}

function Start-InteractiveMcp {
    param([string]$ToolsRoot)

    $selection = Select-ContextInteractive
    if ($selection.Cancelled) { return }
    $resolvedContext = [string]$selection.ContextId

    $transport = Select-McpTransportInteractive
    if ($transport.Cancelled) { return }

    $daemon = Ensure-DaemonContext -ToolsRoot $ToolsRoot -ContextId $resolvedContext
    if (-not $daemon.Ok) {
        Write-Host "[RDX][ERR] $($daemon.Message)"
        return
    }

    if (Get-TestMode) {
        $python = Get-PythonInvocation
        $mcpScript = Join-Path $ToolsRoot 'mcp\run_mcp.py'
        Write-Host ''
        Write-Host "[RDX] Start MCP. context=$resolvedContext"
        Write-Host "[RDX] transport=$($transport.Transport)"
        if ($transport.Transport -eq 'stdio') {
            Write-Host '[RDX] URL: no URL'
            & $python.Executable @($python.ExtraArgs) $mcpScript '--daemon-context' $resolvedContext '--transport' 'stdio'
        }
        else {
            Write-Host ("[RDX] URL: http://{0}:{1}" -f $transport.Host, $transport.Port)
            & $python.Executable @($python.ExtraArgs) $mcpScript '--daemon-context' $resolvedContext '--transport' 'streamable-http' '--host' $transport.Host '--port' ([string]$transport.Port)
        }
        return
    }

    $cmdScript = New-McpCommandFile -ToolsRoot $ToolsRoot -ContextId $resolvedContext -Transport $transport.Transport -Host $transport.Host -Port $transport.Port
    $cmdLaunch = Build-CmdLaunchArgument -ScriptPath $cmdScript
    [void](Start-Process -FilePath $env:ComSpec -ArgumentList @('/Q', '/K', $cmdLaunch) -WorkingDirectory $ToolsRoot -PassThru)
    Write-Host ''
    Write-Host "[RDX] MCP started. context=$resolvedContext transport=$($transport.Transport)"
    if ($transport.Transport -eq 'stdio') { Write-Host '[RDX] URL: no URL' } else { Write-Host ("[RDX] URL: http://{0}:{1}" -f $transport.Host, $transport.Port) }
}

function Show-MainMenu {
    param([string]$ToolsRoot)

    while ($true) {
        Write-Host ''
        Write-Host '=== rdx.bat Launcher ==='
        Write-Host '1. Start CLI'
        Write-Host '2. Start MCP'
        Write-Host '3. Help'
        Write-Host '0. Exit'
        $choice = (Read-LauncherLine -Prompt 'Select: ' -AllowBlank $false).Trim()
        switch ($choice) {
            '1' { Start-InteractiveCliShell -ToolsRoot $ToolsRoot -InitialContextId '' }
            '2' { Start-InteractiveMcp -ToolsRoot $ToolsRoot }
            '3' { Write-HelpPage; Pause-ForMenu }
            '0' { return }
            default { Write-Host '[RDX][ERR] Invalid selection. Try again.' }
        }
    }
}

function Parse-LauncherArgs {
    $argsSafe = [System.Collections.Generic.List[string]]::new()
    foreach ($item in @($script:Arguments)) {
        if ($null -eq $item) { continue }
        $argsSafe.Add([string]$item)
    }
    $result = [ordered]@{
        NonInteractive = $false
        ContextId = 'default'
        InternalCommand = ''
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
                if ($i + 1 -ge $argsSafe.Count) { throw 'missing --daemon-context value' }
                $result.ContextId = [string]$argsSafe[$i + 1]
                $i += 2
            }
            '--context-id' {
                if ($i + 1 -ge $argsSafe.Count) { throw 'missing --context-id value' }
                $result.ContextId = [string]$argsSafe[$i + 1]
                $i += 2
            }
            '--internal' {
                if ($i + 1 -ge $argsSafe.Count) { throw 'missing --internal value' }
                $result.InternalCommand = [string]$argsSafe[$i + 1]
                $i += 2
            }
            '--help' {
                if (-not $result.Command -and -not $result.InternalCommand) { $result.Command = 'help' } else { $result.CommandArgs.Add($arg) }
                $i += 1
            }
            '-h' {
                if (-not $result.Command -and -not $result.InternalCommand) { $result.Command = 'help' } else { $result.CommandArgs.Add($arg) }
                $i += 1
            }
            default {
                if (-not $result.Command -and -not $result.InternalCommand -and -not $arg.StartsWith('-')) {
                    $result.Command = $arg
                }
                else {
                    $result.CommandArgs.Add($arg)
                }
                $i += 1
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

function Parse-InternalArgs {
    param([string[]]$CommandArgs)

    $result = @{}
    $i = 0
    while ($i -lt $CommandArgs.Count) {
        $arg = [string]$CommandArgs[$i]
        if (-not $arg.StartsWith('--')) {
            $i += 1
            continue
        }
        if ($i + 1 -ge $CommandArgs.Count) { throw "missing value for $arg" }
        $result[$arg.Substring(2)] = [string]$CommandArgs[$i + 1]
        $i += 2
    }
    return $result
}

function Run-Mcp {
    param(
        [string[]]$CommandArgs,
        [string]$ToolsRoot,
        [string]$ContextId,
        [bool]$NonInteractive
    )

    $result = Invoke-McpCommand -ToolsRoot $ToolsRoot -ContextId $ContextId -CommandArgs $CommandArgs -TimeoutMs 45000
    return Emit-ChildResult -ExitCode $result.ExitCode -TimedOut $result.TimedOut -Output $result.StdOut -Error $result.StdErr -Command 'mcp' -ContextId $ContextId -AsJson:$NonInteractive
}

function Run-Cli {
    param(
        [string[]]$CommandArgs,
        [string]$ToolsRoot,
        [string]$ContextId,
        [bool]$NonInteractive
    )

    $commandArgsToRun = if ($CommandArgs.Count -eq 0) { @('--help') } else { $CommandArgs }
    $result = Invoke-CliCommand -ToolsRoot $ToolsRoot -ContextId $ContextId -CommandArgs $commandArgsToRun -TimeoutMs 45000
    return Emit-ChildResult -ExitCode $result.ExitCode -TimedOut $result.TimedOut -Output $result.StdOut -Error $result.StdErr -Command 'cli' -ContextId $ContextId -AsJson:$NonInteractive
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

$contextId = [string]$parsed.ContextId
$nonInteractive = [bool]$parsed.NonInteractive
$internalCommand = [string]$parsed.InternalCommand
$command = [string]$parsed.Command
$commandArgs = @($parsed.CommandArgs)

try {
    [void](Invoke-CliJson -ToolsRoot $toolsRoot -ContextId 'default' -CommandArgs @('daemon', 'cleanup') -TimeoutMs 30000)
}
catch {
}

if ($internalCommand) {
    try {
        $internalArgs = Parse-InternalArgs -CommandArgs $commandArgs
    }
    catch {
        exit $script:RETURN_ARGS_ERROR
    }

    switch ($internalCommand) {
        'client-heartbeat' {
            $clientId = [string]$internalArgs['client-id']
            $clientType = if ($internalArgs.ContainsKey('client-type')) { [string]$internalArgs['client-type'] } else { 'cli' }
            $watchPid = if ($internalArgs.ContainsKey('watch-pid')) { [int]$internalArgs['watch-pid'] } else { 0 }
            $leaseTimeout = if ($internalArgs.ContainsKey('lease-timeout-seconds')) { [int]$internalArgs['lease-timeout-seconds'] } else { $script:DEFAULT_LEASE_TIMEOUT_SECONDS }
            if (-not $clientId -or $watchPid -le 0) { exit $script:RETURN_ARGS_ERROR }
            $code = Run-ClientHeartbeatHelper -ToolsRoot $toolsRoot -ContextId $contextId -ClientId $clientId -ClientType $clientType -WatchPid $watchPid -LeaseTimeoutSeconds $leaseTimeout
            exit $code
        }
        default { exit $script:RETURN_ARGS_ERROR }
    }
}

if (-not $command) {
    if ($nonInteractive) {
        Write-JsonStatus -Ok $false -ErrorCode $script:RETURN_ARGS_ERROR -ErrorMessage 'missing subcommand' -ContextId $contextId
        exit $script:RETURN_ARGS_ERROR
    }
    Show-MainMenu -ToolsRoot $toolsRoot
    exit $script:RETURN_OK
}

$exitCode = $script:RETURN_OK
switch ($command) {
    'help' {
        Write-TerseUsage
        $exitCode = $script:RETURN_OK
    }
    'mcp' {
        $commandResult = Invoke-CommandWithOutput -CommandScript { Run-Mcp -CommandArgs $commandArgs -ToolsRoot $toolsRoot -ContextId $contextId -NonInteractive:$nonInteractive }
        $exitCode = $commandResult.ExitCode
        if ($commandResult.Output.Count -gt 0) { Write-Output ($commandResult.Output -join "`n") }
    }
    'cli' {
        $commandResult = Invoke-CommandWithOutput -CommandScript { Run-Cli -CommandArgs $commandArgs -ToolsRoot $toolsRoot -ContextId $contextId -NonInteractive:$nonInteractive }
        $exitCode = $commandResult.ExitCode
        if ($commandResult.Output.Count -gt 0) { Write-Output ($commandResult.Output -join "`n") }
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
