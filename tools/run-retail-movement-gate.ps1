param(
    [string]$ServerRoot = "G:\AoSRevival\BattleSpades",
    [string]$ClientDir = "G:\AoSRevival\AceOfSpades_no_steam_new",
    [string]$ArtifactDir = "G:\AoSRevival\BattleSpades\logs\movement\release-gate",
    [string]$Python = "C:\Users\todor\AppData\Local\Programs\Python\Python312\python.exe",
    [string]$ServerExecutable = "",
    [int]$Port = 32768,
    [double]$DurationScale = 0.12,
    [string]$Segments = "walk,sprint,crouch_walk,turn_left,slope_diagonal,jump_run",
    [int]$Repeats = 1
)

$ErrorActionPreference = "Stop"
$serverRoot = (Resolve-Path -LiteralPath $ServerRoot).Path
$clientDir = (Resolve-Path -LiteralPath $ClientDir).Path
$python = (Resolve-Path -LiteralPath $Python).Path
$config = Join-Path $serverRoot "tools\protocol168-parity.toml"
$scenario = Join-Path $serverRoot "scripts\scenarios\movement_stress.py"
$artifactDir = [System.IO.Path]::GetFullPath($ArtifactDir)

if (-not (Test-Path -LiteralPath $config -PathType Leaf)) {
    throw "Retail movement profile is missing: $config"
}
if (-not (Test-Path -LiteralPath $scenario -PathType Leaf)) {
    throw "Retail movement scenario is missing: $scenario"
}
if (-not (Test-Path -LiteralPath (Join-Path $clientDir "python\python.exe") -PathType Leaf)) {
    throw "Instrumented Python 2 client is incomplete: $clientDir"
}
if (Get-NetUDPEndpoint -LocalPort $Port -ErrorAction SilentlyContinue) {
    throw "Retail movement gate port UDP $Port is already in use. Choose a free -Port."
}

New-Item -ItemType Directory -Path $artifactDir -Force | Out-Null
$stdout = Join-Path $artifactDir "server.stdout.log"
$stderr = Join-Path $artifactDir "server.stderr.log"
$serverCommand = $python
$serverWorkingDirectory = $serverRoot
$arguments = @("run_server.py", "--config", $config, "--port", $Port)
if ($ServerExecutable) {
    $serverCommand = (Resolve-Path -LiteralPath $ServerExecutable).Path
    $serverWorkingDirectory = Split-Path -Parent $serverCommand
    $arguments = @("--config", $config, "--port", $Port)
}

$server = Start-Process -FilePath $serverCommand -ArgumentList $arguments `
    -WorkingDirectory $serverWorkingDirectory -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr
$previousSmoothingOptOut = $env:AOS_DISABLE_CHARACTER_JUMP_SMOOTHING
try {
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 100
        if ($server.HasExited) {
            throw "Retail gate server exited early. stdout=$stdout stderr=$stderr"
        }
        $endpoint = Get-NetUDPEndpoint -LocalPort $Port -ErrorAction SilentlyContinue
    } while (-not $endpoint -and [DateTime]::UtcNow -lt $deadline)
    if (-not $endpoint) {
        throw "Retail gate server did not bind UDP $Port. stdout=$stdout stderr=$stderr"
    }

    # The public 0.1.2 build did not ship the experimental Character wrapper.
    # Keep this capture on the untouched retail/native movement path.
    $env:AOS_DISABLE_CHARACTER_JUMP_SMOOTHING = "1"
    & $python $scenario `
        --launch `
        --server "127.0.0.1:$Port" `
        --client-dir $clientDir `
        --class-id 0 `
        --repeats $Repeats `
        --duration-scale $DurationScale `
        --segments $Segments `
        --artifact-dir $artifactDir `
        --max-stalls 0
    if ($LASTEXITCODE -ne 0) {
        throw "Real Python 2 retail-client movement gate failed with exit code $LASTEXITCODE"
    }
}
finally {
    if ($null -eq $previousSmoothingOptOut) {
        Remove-Item Env:\AOS_DISABLE_CHARACTER_JUMP_SMOOTHING -ErrorAction SilentlyContinue
    }
    else {
        $env:AOS_DISABLE_CHARACTER_JUMP_SMOOTHING = $previousSmoothingOptOut
    }
    if (-not $server.HasExited) {
        Stop-Process -Id $server.Id -Force
        $server.WaitForExit()
    }
}

Write-Host "Retail movement artifact: $artifactDir"
Write-Host "Server stdout: $stdout"
Write-Host "Server stderr: $stderr"
