#!/usr/bin/env bash
# Push the TikTok video editor code from GitHub into the HF Space.
# Run this on YOUR laptop, not in Hermes's VM.

set -euo pipefail

REPO_GH="arkydarmalik-coder/tiktok-video-editor-asisten"
SPACE_ID="Seratih/tiktok-video-editor-assisten"

# 1. Install hf CLI if missing
if ! command -v hf >/dev/null 2>&1; then
  echo ">> installing hf CLI"
  curl -LsSf https://hf.co/cli/install.sh | bash
  export PATH="$PATH:$HOME/.local/bin"
fi

# 2. Auth (only prompts if not already logged in)
if ! hf auth whoami >/dev/null 2>&1; then
  echo ">> login to Hugging Face (paste write-token)"
  hf auth login
fi

# 3. Clone the GitHub repo (with code we already pushed)
WORK=$(mktemp -d)
echo ">> cloning $REPO_GH into $WORK/repo"
gh repo clone "$REPO_GH" "$WORK/repo" 2>/dev/null \
  || git clone "https://github.com/$REPO_GH.git" "$WORK/repo"

# 4. Download the (empty) HF Space
echo ">> downloading $SPACE_ID into $WORK/space"
hf download "$SPACE_ID" --repo-type=space --local-dir "$WORK/space"

# 5. Copy code over
echo ">> copying files"
shopt -s dotglob nullglob
cp -r "$WORK/repo"/* "$WORK/space/" 2>/dev/null || true
shopt -u dotglob nullglob

# 6. Init git in the Space dir and push
cd "$WORK/space"
if [ ! -d .git ]; then
  git init -q
  git checkout -q -b main 2>/dev/null || git checkout -b main
fi
git add -A
if git diff --cached --quiet; then
  echo ">> no changes to commit"
else
  git -c user.email=tvea@local -c user.name=tvea commit -q -m "Import: TikTok video editor assistant"
fi

# 7. Push using the token already saved by `hf auth login`
TOKEN=$(hf auth token)
git remote remove origin 2>/dev/null || true
git remote add origin "https://Seratih:${TOKEN}@huggingface.co/spaces/$SPACE_ID"
echo ">> pushing to HF Space"
git push -u origin main --force

echo
echo "DONE. Space: https://huggingface.co/spaces/$SPACE_ID"
echo "Next: Settings -> Variables and secrets di Space lo (tambah 4 secrets)"
