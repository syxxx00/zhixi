# No Python/model environment is needed to stop the desktop service.
# Keep the high-impact stop confirmation local to ShouldProcess below. Passing
# -Confirm at the batch entry also changes confirmation preferences in nested
# calls; confirming those calls is not authorization for the whole script.
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'High')]
param(
    [string]$ProjectRoot,
    [ValidateRange(1, 65535)][int]$Port = 8177
)

$ErrorActionPreference = 'Stop'

function Resolve-ServicePath([string]$Value, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "$Label path is empty. No process was stopped."
    }
    try {
        [System.IO.Path]::GetFullPath($Value)
    }
    catch {
        throw "Invalid $Label path '$Value': $($_.Exception.Message)"
    }
}

function Get-ServiceListeners {
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -eq $Port } |
        Select-Object -ExpandProperty OwningProcess -Unique)
}

function Assert-ServiceIdentity([string]$ExpectedDatabase) {
    $request = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/api/health")
    $request.Proxy = $null
    $request.AllowAutoRedirect = $false
    $request.Timeout = 5000
    $request.ReadWriteTimeout = 5000
    $response = $null
    $reader = $null
    try {
        $response = $request.GetResponse()
        $reader = [System.IO.StreamReader]::new($response.GetResponseStream())
        $buffer = New-Object char[] 65537
        $length = $reader.ReadBlock($buffer, 0, $buffer.Length)
        if ($length -gt 65536) { throw 'Health response is too large.' }
        $health = (-join $buffer[0..($length - 1)]) | ConvertFrom-Json
        if ($health.capabilities.input_material_contract -ne 1 -or -not $health.history_path) {
            throw 'The service does not belong to this project/data directory.'
        }
        if ((Resolve-ServicePath $health.history_path 'service history') -ne $ExpectedDatabase) {
            throw 'The service does not belong to this project/data directory.'
        }
    }
    finally {
        if ($reader) { $reader.Dispose() }
        if ($response) { $response.Dispose() }
    }
}

$serviceProcess = $null
try {
    # Resolve the default in the script body, not during parameter binding.
    # stop.bat also passes its own directory explicitly, with a trailing dot so
    # the closing quote cannot be consumed as part of a trailing backslash.
    if (-not $PSBoundParameters.ContainsKey('ProjectRoot')) {
        $ProjectRoot = $PSScriptRoot
    }
    $projectPath = Resolve-ServicePath $ProjectRoot 'project directory'
    $dataPath = if ([string]::IsNullOrWhiteSpace($env:WP_DATA_DIR)) { 'data' } else { $env:WP_DATA_DIR.Trim() }
    if (-not [System.IO.Path]::IsPathRooted($dataPath)) { $dataPath = Join-Path $projectPath $dataPath }
    $dataPath = Resolve-ServicePath $dataPath 'data directory (WP_DATA_DIR)'
    $databasePath = Join-Path $dataPath 'history.sqlite3'
    $owners = @(Get-ServiceListeners)
    if ($owners.Count -eq 0) {
        Write-Host 'The service is already stopped. Nothing was changed.'
        exit 0
    }
    if ($owners.Count -ne 1) { throw 'Multiple processes own this port; refusing to stop any of them.' }

    $serviceProcess = Get-Process -Id $owners[0] -ErrorAction Stop
    # Hold the original process handle across the confirmation prompt. This
    # prevents a recycled PID from becoming the target of taskkill.
    $null = $serviceProcess.Handle
    $processInfo = Get-CimInstance Win32_Process -Filter "ProcessId = $($serviceProcess.Id)"
    if ($processInfo.Name -notmatch '^python(?:w|\d+(?:\.\d+)?)?\.exe$' -or
        $processInfo.CommandLine -notmatch '(?i)(?:^|\s)-m\s+uvicorn\s+(?:zhixi\.web\.app|app):app(?:\s|$)' -or
        $processInfo.CommandLine -notmatch '(?i)(?:^|\s)--host\s+127\.0\.0\.1(?:\s|$)' -or
        $processInfo.CommandLine -notmatch "(?i)(?:^|\s)--port\s+$Port(?:\s|$)") {
        throw 'The listener is not the backend started by start.bat. No process was stopped.'
    }
    Assert-ServiceIdentity $databasePath
    Write-Host "Project: $projectPath"
    Write-Host 'Stopping ends this backend and its child processes, including its local model.'
    Write-Host 'Saved history/checkpoints remain. Unsaved work in the current batch may need to run again.'
    Write-Host 'This is NOT task pause or a wait-for-current-batch operation.'
    if (-not $PSCmdlet.ShouldProcess("Word Processing service PID $($serviceProcess.Id)", 'Stop service and its children')) {
        Write-Host 'No process was stopped.'
        exit 0
    }

    if ($serviceProcess.HasExited) {
        Write-Host 'The original service has already exited. No other process was stopped.'
        exit 0
    }
    # The user may leave the confirmation open. Recheck the exact listener and
    # project identity instead of trusting a stale port observation.
    $owners = @(Get-ServiceListeners)
    if ($owners.Count -ne 1 -or $owners[0] -ne $serviceProcess.Id) {
        throw 'The listener changed while waiting for confirmation. No process was stopped.'
    }
    Assert-ServiceIdentity $databasePath
    if ($serviceProcess.HasExited) { throw 'The original service exited before stopping; please check again.' }
    & "$env:SystemRoot\System32\taskkill.exe" /PID $serviceProcess.Id /T /F
    if ($LASTEXITCODE -ne 0) { throw 'Could not stop the complete service process tree. Check the message above.' }
    if (-not $serviceProcess.WaitForExit(10000)) { throw 'The service has not exited; no additional process was stopped.' }
    if (@(Get-ServiceListeners).Count -gt 0) {
        throw 'The original service exited, but the port is occupied again. The new listener was not stopped.'
    }
    Write-Host 'Service stopped. Use start.bat to start it again.'
    exit 0
}
catch {
    Write-Host "Stop failed: $($_.Exception.Message)" -ForegroundColor Red
    Write-Host 'No unverified process will be stopped. Check the service/port, or use its original terminal.'
    exit 1
}
finally {
    if ($serviceProcess) { $serviceProcess.Dispose() }
}
