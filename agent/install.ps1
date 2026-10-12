# Install filmcraft-cli + fcedit on Windows.
#
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/jokedud/filmcraft-agent/main/agent/install.ps1 | iex"
#
# Downloads the latest release binary for this machine (this repo's slim
# filmcraft-cli package first, upstream storytold/filmcraft as a fallback),
# installs fcedit, and adds the folder to the user PATH.

$ErrorActionPreference = 'Stop'
$Repo = $env:FC_REPO ?? 'jokedud/filmcraft-agent'
$Upstream = 'storytold/filmcraft'
$Dest = $env:FC_DEST ?? "$env:LOCALAPPDATA\filmcraft-agent\bin"

$arch = if ($env:PROCESSOR_ARCHITEW6432 -eq 'ARM64' -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x64' }

function Latest-Release($name) {
    Invoke-RestMethod -Uri "https://api.github.com/repos/$name/releases/latest" -Headers @{ 'User-Agent' = 'fcedit-install' }
}

$asset = $null
$version = $null
try {
    $rel = Latest-Release $Repo
    $version = $rel.tag_name
    $asset = ($rel.assets | Where-Object { $_.name -match "filmcraft-cli-.*-windows-$arch\.zip" -and $_.name -notmatch 'sha256' } | Select-Object -First 1).browser_download_url
} catch { }

if (-not $asset) {
    $rel = Latest-Release $Upstream
    $version = $version ?? $rel.tag_name
    $asset = ($rel.assets | Where-Object { $_.name -match "filmcraft-.*-windows-$arch-portable\.zip" } | Select-Object -First 1).browser_download_url
}
if (-not $asset) { throw "fcedit: no release asset for windows-$arch" }

Write-Host "fcedit: installing filmcraft $version for windows-$arch"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$tmp = Join-Path $env:TEMP ("fcedit-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
    $zip = Join-Path $tmp 'pkg.zip'
    Invoke-WebRequest -Uri $asset -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $cli = Get-ChildItem -Path $tmp -Recurse -Filter 'filmcraft-cli.exe' | Select-Object -First 1
    if (-not $cli) { throw 'filmcraft-cli.exe not found in package' }
    Copy-Item $cli.FullName (Join-Path $Dest 'filmcraft-cli.exe') -Force

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

    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    if ($userPath -notlike "*$Dest*") {
        [Environment]::SetEnvironmentVariable('Path', "$userPath;$Dest", 'User')
        $env:Path = "$env:Path;$Dest"
        Write-Host "added $Dest to user PATH"
    }

    & (Join-Path $Dest 'filmcraft-cli.exe') --version
    Write-Host "fcedit: done. Open a new shell and try: fcedit commands"
} finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
