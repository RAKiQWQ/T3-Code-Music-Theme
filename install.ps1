param(
    [string]$Python = 'python',
    [string]$Destination = (Join-Path $env:USERPROFILE '.t3\tools\t3-music'),
    [switch]$SkipSkills
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'The player requires Windows.' }
$musicPython = (Get-Command $Python -ErrorAction Stop).Source
& $musicPython -c 'import numpy, soundcard; from PIL import Image'
if ($LASTEXITCODE -ne 0) {
    throw "Install dependencies first: & '$musicPython' -m pip install -r '$PSScriptRoot\requirements.txt'"
}
foreach ($binary in @('ffmpeg', 'ffprobe')) {
    $null = Get-Command $binary -ErrorAction Stop
}
$releaseFiles = @('t3music.py', 'music.ps1', 'mono-player.json', 'requirements.txt', 'README.md', 'LICENSE')
foreach ($name in $releaseFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $PSScriptRoot $name))) {
        throw "Missing release file: $name"
    }
}
if (-not $SkipSkills -and -not (Test-Path -LiteralPath (Join-Path $PSScriptRoot 'skills\music\SKILL.md'))) {
    throw 'Missing bundled music skill.'
}

$destinationPath = [IO.Path]::GetFullPath($Destination)
$installedPlayer = Join-Path $destinationPath 't3music.py'
$portFile = Join-Path $destinationPath 't3music.port'
if (Test-Path -LiteralPath $installedPlayer) {
    & $musicPython $installedPlayer stop
    for ($attempt = 0; $attempt -lt 24; $attempt++) {
        if (-not (Test-Path -LiteralPath $portFile)) { break }
        Start-Sleep -Milliseconds 250
    }
    if (Test-Path -LiteralPath $portFile) {
        $oldPlayerAlive = $false
        $client = [Net.Sockets.TcpClient]::new()
        try {
            $connect = $client.ConnectAsync('127.0.0.1', [int](Get-Content -LiteralPath $portFile -Raw))
            $oldPlayerAlive = $connect.Wait(500) -and $client.Connected
        } catch {
            $oldPlayerAlive = $false
        } finally {
            $client.Dispose()
        }
        if ($oldPlayerAlive) { throw 'The existing player did not stop; installation was left unchanged.' }
    }
}

$null = New-Item -ItemType Directory -Path $destinationPath -Force
if (-not $destinationPath.TrimEnd('\').Equals($PSScriptRoot.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) {
    foreach ($name in $releaseFiles) {
        Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $destinationPath $name) -Force
    }
}
foreach ($legacyFile in @('music.cmd', 'final_check.py', 'FINAL_CHECK.md')) {
    $legacyPath = Join-Path $destinationPath $legacyFile
    if (Test-Path -LiteralPath $legacyPath) {
        Remove-Item -LiteralPath $legacyPath -Force
    }
}
[IO.File]::WriteAllText((Join-Path $destinationPath 'python-path.txt'), $musicPython, [Text.UTF8Encoding]::new($false))
$userdataPath = if ($env:T3_MUSIC_USERDATA) { $env:T3_MUSIC_USERDATA } else { Join-Path $env:USERPROFILE '.t3\userdata' }
$themesPath = Join-Path $userdataPath 'themes'
$null = New-Item -ItemType Directory -Path $themesPath -Force
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'mono-player.json') -Destination (Join-Path $themesPath 'mono-player.json') -Force

if (-not $SkipSkills) {
    $skillSource = Join-Path $PSScriptRoot 'skills\music'
    $skillTargets = @(
        (Join-Path $env:USERPROFILE '.codex\skills\music'),
        (Join-Path $env:USERPROFILE '.claude\skills\music')
    )
    if ($env:CODEX_HOME) { $skillTargets += Join-Path $env:CODEX_HOME 'skills\music' }
    foreach ($skillTarget in ($skillTargets | Select-Object -Unique)) {
        $null = New-Item -ItemType Directory -Path $skillTarget -Force
        Get-ChildItem -LiteralPath $skillSource | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $skillTarget -Recurse -Force
        }
    }
    Write-Output 'Installed /music for Codex and Claude.'
}
Write-Output "Installed player: $destinationPath"
Write-Output "Select 'T3 Code Music Theme!' in T3 Code, then run: & '$destinationPath\music.ps1' start"
