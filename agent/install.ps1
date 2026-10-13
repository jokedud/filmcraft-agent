param(
    [string]$Apps = $(if ($env:FC_APPS) { $env:FC_APPS } else { 'film,effect,sound' })
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$Repo = if ($env:FC_REPO) { $env:FC_REPO } else { 'jokedud/filmcraft-agent' }
$Dest = if ($env:FC_DEST) { $env:FC_DEST } else { "$env:LOCALAPPDATA\filmcraft-agent\bin" }
$arch = if ($env:PROCESSOR_ARCHITEW6432 -eq 'ARM64' -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }

function Latest-Release($name) {
    Invoke-RestMethod -Uri "https://api.github.com/repos/$name/releases/latest" `
        -Headers @{ 'User-Agent' = 'fcedit-install' }
}

function Copy-ZipCli($url, $cli, $destination) {
    $zipPath = Join-Path $tmp "$cli.zip"
    Invoke-WebRequest -Uri $url -OutFile $zipPath -UseBasicParsing
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $entry = $archive.Entries | Where-Object { $_.Name -eq "$cli.exe" } | Select-Object -First 1
        if (-not $entry) { throw "$cli.exe not found in package" }
        $source = $entry.Open()
        try {
            $target = [System.IO.File]::Create($destination)
            try { $source.CopyTo($target) } finally { $target.Dispose() }
        } finally {
            $source.Dispose()
        }
    } finally {
        $archive.Dispose()
    }
}

$appsToInstall = $Apps.Split(',') | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ }
if (-not $appsToInstall) { throw 'fcedit: -Apps must include film, effect, or sound' }
$upstreams = @{
    film = 'storytold/filmcraft'
    effect = 'storytold/effectcraft'
    sound = 'storytold/soundcraft'
}
foreach ($app in $appsToInstall) {
    if (-not $upstreams.ContainsKey($app)) { throw "fcedit: unknown app '$app' in -Apps" }
}

New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$tmp = Join-Path $env:TEMP ("fcedit-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
    foreach ($app in $appsToInstall) {
        $cli = "${app}craft-cli"
        $asset = $null
        try {
            $release = Latest-Release $Repo
            $asset = ($release.assets | Where-Object {
                $_.name -match "^$cli-.*-windows-$arch\.zip$" -and $_.name -notmatch 'sha256'
            } | Select-Object -First 1).browser_download_url
        } catch { }

        if (-not $asset) {
            $release = Latest-Release $upstreams[$app]
            $engine = "${app}craft"
            $asset = ($release.assets | Where-Object {
                $_.name -match "^($engine|$cli)-.*-windows-$arch(-portable)?\.zip$"
            } | Select-Object -First 1).browser_download_url
        }
        if (-not $asset) { throw "fcedit: no release asset for $app windows-$arch" }

        Write-Host "fcedit: installing $app from $(Split-Path $asset -Leaf)"
        Copy-ZipCli $asset $cli (Join-Path $Dest "$cli.exe")
    }

    Invoke-WebRequest -Uri "https://raw.githubusercontent.com/$Repo/main/agent/fcedit.py" `
        -OutFile (Join-Path $Dest 'fcedit.py') -UseBasicParsing
    @'
@echo off
python "%~dp0fcedit.py" %*
'@ | Set-Content -Path (Join-Path $Dest 'fcedit.cmd') -Encoding ASCII
    @'
@echo off
python "%~dp0fcedit.py" serve %*
'@ | Set-Content -Path (Join-Path $Dest 'fcserve.cmd') -Encoding ASCII
    @'
@echo off
python "%~dp0fcedit.py" --app effect %*
'@ | Set-Content -Path (Join-Path $Dest 'ecedit.cmd') -Encoding ASCII
    @'
@echo off
python "%~dp0fcedit.py" --app sound %*
'@ | Set-Content -Path (Join-Path $Dest 'scedit.cmd') -Encoding ASCII

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -notlike "*$Dest*") {
        [Environment]::SetEnvironmentVariable('Path', "$userPath;$Dest", 'User')
        $env:Path = "$env:Path;$Dest"
        Write-Host "added $Dest to user PATH"
    }
    Write-Host "fcedit: installed $($appsToInstall -join ', '). Try: fcedit --app effect tools"
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
