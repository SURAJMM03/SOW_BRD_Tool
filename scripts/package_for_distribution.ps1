<#
.SYNOPSIS
  Builds a clean zip of this repo to hand to another user, excluding venvs,
  real .env files, the user database, generated output, and logs - so the
  recipient's own "set up the application" run starts from a blank slate
  instead of inheriting your local secrets and account data.

.USAGE
  powershell -ExecutionPolicy Bypass -File scripts\package_for_distribution.ps1
  # Produces ..\docgenplatform-dist.zip next to the repo root.
#>

$ErrorActionPreference = "Stop"

$RepoRoot = Split-Path -Parent $PSScriptRoot
$StageDir = Join-Path $env:TEMP "docgenplatform-dist-stage"
$ZipPath  = Join-Path (Split-Path -Parent $RepoRoot) "docgenplatform-dist.zip"

# Directories/files to leave out of the zip entirely.
$ExcludeDirs = @(
    "venv", ".venv", "env", "__pycache__", ".pytest_cache",
    "node_modules",
    "output", "outputs", "uploads", "document_versions", "document_imports",
    "extracted_images", "debug_llm_mapping",
    "exports"
)
$ExcludeFiles = @(
    ".env", "*.env",           # real secrets - .env.example files are kept
    "*.db", "*.sqlite", "*.sqlite3",   # users.db etc - recipient gets a fresh one
    "*.log",
    "settings.json", "settings.local.json"   # your machine-specific Claude Code permission allowlist
)

Write-Host "Staging a clean copy at $StageDir ..."
if (Test-Path $StageDir) { Remove-Item $StageDir -Recurse -Force }
New-Item -ItemType Directory -Path $StageDir | Out-Null

$xd = $ExcludeDirs | ForEach-Object { "/XD"; $_ }
$xf = $ExcludeFiles | ForEach-Object { "/XF"; $_ }

# robocopy exit codes 0-7 are all "success" (see /?); only >=8 is a real error.
robocopy $RepoRoot $StageDir /E /NFL /NDL /NJH /NJS @xd @xf | Out-Null
if ($LASTEXITCODE -ge 8) {
    throw "robocopy failed with exit code $LASTEXITCODE"
}

Write-Host "Zipping to $ZipPath ..."
if (Test-Path $ZipPath) { Remove-Item $ZipPath -Force }
Compress-Archive -Path (Join-Path $StageDir "*") -DestinationPath $ZipPath

Remove-Item $StageDir -Recurse -Force

Write-Host ""
Write-Host "Done: $ZipPath"
Write-Host "This zip has NO real .env files, no users.db, and no venvs -"
Write-Host "the recipient runs 'set up the application' in Claude Code to"
Write-Host "provision their own copy from scratch."
