$ErrorActionPreference = "Stop"

$RepoUrl = if ($env:GIT_SYNAPSE_REPO_URL) { $env:GIT_SYNAPSE_REPO_URL } else { "https://github.com/kirankn8/git-synapse.git" }
$TargetDir = if ($args.Count -gt 0) { $args[0] } elseif ($env:GIT_SYNAPSE_DIR) { $env:GIT_SYNAPSE_DIR } else { Join-Path $HOME "git-synapse" }
$TargetDir = [Environment]::ExpandEnvironmentVariables($TargetDir)
$InstallRef = if ($env:GIT_SYNAPSE_REF) { $env:GIT_SYNAPSE_REF } else { "main" }
if (-not $env:GIT_SYNAPSE_REF -and $RepoUrl -eq "https://github.com/kirankn8/git-synapse.git") {
  try {
    $release = Invoke-RestMethod "https://api.github.com/repos/kirankn8/git-synapse/releases/latest" -Headers @{ Accept = "application/vnd.github+json" }
    if ($release.tag_name -match '^v\d+\.\d+\.\d+$') { $InstallRef = $release.tag_name }
  } catch { $InstallRef = "main" }
}

function Fail([string]$Message) { Write-Host "`nError: $Message" -ForegroundColor Red; exit 1 }

try { docker compose version | Out-Null; docker info | Out-Null }
catch {
  $answer = Read-Host "Docker Desktop is required. Install it with winget now? [Y/n]"
  if ($answer -and $answer -notmatch '^[Yy]$') { Fail "Install Docker Desktop, then run this command again: https://www.docker.com/products/docker-desktop/" }
  if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install --id Docker.DockerDesktop --exact --accept-source-agreements --accept-package-agreements
    Start-Process "Docker Desktop" -ErrorAction SilentlyContinue
  } else { Fail "winget is unavailable. Install Docker Desktop manually: https://www.docker.com/products/docker-desktop/" }
  for ($i = 0; $i -lt 30; $i++) {
    try { docker compose version | Out-Null; docker info | Out-Null; break } catch { Start-Sleep -Seconds 2 }
    if ($i -eq 29) { Fail "Docker Desktop is not running yet. Start it and run this command again." }
  }
}

if (Test-Path (Join-Path $TargetDir ".git")) {
  Write-Host "Updating Git Synapse in $TargetDir" -ForegroundColor Cyan
  if ((git -C $TargetDir status --porcelain)) {
    Write-Host "Local changes found; leaving the existing checkout untouched." -ForegroundColor Yellow
  } else {
    git -C $TargetDir fetch --tags origin
    git -C $TargetDir checkout --detach $InstallRef
    git -C $TargetDir pull --ff-only origin $InstallRef 2>$null
  }
} elseif (Test-Path $TargetDir) {
  if ((Get-ChildItem -Force $TargetDir | Select-Object -First 1)) { Fail "$TargetDir is not empty and is not a Git Synapse checkout." }
  git clone --branch $InstallRef $RepoUrl $TargetDir
} else {
  New-Item -ItemType Directory -Force -Path (Split-Path $TargetDir) | Out-Null
  git clone --branch $InstallRef $RepoUrl $TargetDir
}

Set-Location $TargetDir
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
docker compose up -d --build

$Port = "8080"
$PortLine = Select-String -Path .env -Pattern '^API_PUBLISHED_PORT=' | Select-Object -Last 1
if ($PortLine) { $Port = ($PortLine.Line -split '=', 2)[1] }
$Url = "http://localhost:$Port"
Write-Host "Waiting for Git Synapse at $Url" -NoNewline
for ($i = 0; $i -lt 60; $i++) {
  try { Invoke-WebRequest "$Url/api/health" -UseBasicParsing | Out-Null; Write-Host " ready."; break }
  catch { Write-Host "." -NoNewline; Start-Sleep -Seconds 2 }
  if ($i -eq 59) { docker compose ps; Fail "Git Synapse did not become healthy. Run: docker compose logs --tail=100 api" }
}
Write-Host "`nGit Synapse is ready" -ForegroundColor Cyan
Write-Host "Open: $Url"
Write-Host "`nFor a new installation, get the one-time setup token with:" -ForegroundColor Yellow
Write-Host '  docker compose run --rm cli admin setup-token'
Write-Host "Open the URL above, paste the token into the first-run page, and choose your email, name, and password."
Write-Host "The first account becomes the administrator and the token expires after use."
