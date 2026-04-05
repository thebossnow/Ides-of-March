# =============================================================================
# github_push.ps1  —  Initialize local git repo and push to private GitHub repo
# Repo name: Ides_of_March
# Run this from PowerShell in your bot/ folder
# =============================================================================

$ErrorActionPreference = "Stop"

$REPO_NAME = "Ides_of_March"
$GIT_EMAIL = "banderson.ca@gmail.com"
$GIT_NAME = "TheBossNow"

# Get the script's directory (the bot/ folder)
$SCRIPT_DIR = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $SCRIPT_DIR
Write-Host "Working in: $SCRIPT_DIR" -ForegroundColor Green

# --- 1. Check prerequisites ---
Write-Host "`nChecking prerequisites..." -ForegroundColor Cyan

# Check git
try {
  git --version | Out-Null
  Write-Host "✓ git is available" -ForegroundColor Green
} catch {
  Write-Host "ERROR: git is not installed or not in PATH" -ForegroundColor Red
  exit 1
}

# Check gh
try {
  gh --version | Out-Null
  Write-Host "✓ gh CLI is available" -ForegroundColor Green
} catch {
  Write-Host "gh CLI not found. Visit https://cli.github.com and install it." -ForegroundColor Yellow
  Write-Host "Then run this script again." -ForegroundColor Yellow
  exit 1
}

# --- 2. Authenticate with GitHub ---
Write-Host "`nChecking GitHub authentication..." -ForegroundColor Cyan
$authStatus = gh auth status 2>&1
if ($LASTEXITCODE -ne 0) {
  Write-Host "Not authenticated. Starting browser login..." -ForegroundColor Yellow
  gh auth login --web
} else {
  Write-Host "Already authenticated:" -ForegroundColor Green
  Write-Host $authStatus
}

# --- 3. Safety check: confirm .env is gitignored ---
Write-Host "`nVerifying .env is protected..." -ForegroundColor Cyan
$gitignoreContent = Get-Content .gitignore -Raw
if ($gitignoreContent -match "^\s*\.env\s*$") {
  Write-Host ".env is properly excluded by .gitignore" -ForegroundColor Green
} else {
  Write-Host "WARNING: .env may not be in .gitignore! Aborting to protect credentials." -ForegroundColor Red
  exit 1
}

# --- 4. Initialize git repo ---
Write-Host "`nInitializing git repository..." -ForegroundColor Cyan
git init
git branch -M main
git config user.email $GIT_EMAIL
git config user.name $GIT_NAME

# --- 5. Stage and commit ---
Write-Host "`nStaging files..." -ForegroundColor Cyan
git add .

Write-Host "`nFiles that will be committed:" -ForegroundColor Yellow
git status --short

$confirm = Read-Host "`nProceed with commit? (y/N)"
if ($confirm -ne "y" -and $confirm -ne "Y") {
  Write-Host "Aborted." -ForegroundColor Yellow
  exit 0
}

git commit -m "Initial commit: Polymarket weather bot"

# --- 6. Create private GitHub repo and push ---
Write-Host "`nCreating private GitHub repo '$REPO_NAME' and pushing..." -ForegroundColor Cyan
gh repo create $REPO_NAME --private --source=. --remote=origin --push

Write-Host "`n✓ Success! Repo is live at:" -ForegroundColor Green
$repoUrl = gh repo view $REPO_NAME --json url -q '.url'
Write-Host $repoUrl -ForegroundColor Blue
