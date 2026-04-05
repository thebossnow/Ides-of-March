#!/bin/bash
# =============================================================================
# github_push.sh  —  Initialize local git repo and push to private GitHub repo
# Repo name: Ides_of_March
# Run this from your terminal (NOT from Claude's sandbox)
# =============================================================================

set -e

REPO_NAME="Ides_of_March"
GIT_EMAIL="banderson.ca@gmail.com"
GIT_NAME="TheBossNow"

# Move to script's own directory (the bot/ folder)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
echo "Working in: $SCRIPT_DIR"

# --- 1. Check prerequisites ---
echo ""
echo "Checking prerequisites..."

if ! command -v git &>/dev/null; then
  echo "ERROR: git is not installed. Install it first."
  exit 1
fi

if ! command -v gh &>/dev/null; then
  echo "gh CLI not found. Installing via Homebrew..."
  if command -v brew &>/dev/null; then
    brew install gh
  else
    echo "ERROR: Homebrew not found either. Install gh manually:"
    echo "  https://cli.github.com"
    exit 1
  fi
fi

echo "git and gh are available."

# --- 2. Authenticate with GitHub ---
echo ""
echo "Checking GitHub auth..."
if ! gh auth status &>/dev/null; then
  echo "Not logged in. Starting browser login..."
  gh auth login --web --git-protocol https
else
  echo "Already authenticated."
  gh auth status
fi

# --- 3. Safety check: confirm .env is gitignored ---
echo ""
echo "Verifying .env is not tracked..."
if git check-ignore -q .env 2>/dev/null || grep -q "^\.env$" .gitignore; then
  echo ".env is properly excluded by .gitignore."
else
  echo "WARNING: .env may not be ignored! Aborting to protect your credentials."
  exit 1
fi

# --- 4. Initialize git repo ---
echo ""
echo "Initializing git repo..."
git init
git branch -M main
git config user.email "$GIT_EMAIL"
git config user.name "$GIT_NAME"

# --- 5. Stage and commit ---
echo ""
echo "Staging files..."
git add .

echo ""
echo "Files that will be committed:"
git status --short

echo ""
read -p "Proceed with commit? (y/N): " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "Aborted."
  exit 0
fi

git commit -m "Initial commit: Polymarket weather bot"

# --- 6. Create private GitHub repo and push ---
echo ""
echo "Creating private GitHub repo '$REPO_NAME' and pushing..."
gh repo create "$REPO_NAME" --private --source=. --remote=origin --push

echo ""
echo "Done! Repo is live at:"
gh repo view "$REPO_NAME" --json url -q '.url'
