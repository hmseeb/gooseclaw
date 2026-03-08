#!/bin/bash
# persist.sh — commit and push identity state changes back to GitHub
#
# Usage:
#   ./scripts/persist.sh              # commit and push if there are changes
#   ./scripts/persist.sh --dry-run    # show what would be committed
#
# Required env vars:
#   GITHUB_PAT    — fine-grained PAT with Contents: read/write
#   GITHUB_REPO   — e.g. "username/nix-agent"
#
# Optional env vars:
#   GIT_USER_NAME    — commit author name  (default: "nix-agent")
#   GIT_USER_EMAIL   — commit author email (default: "nix-agent@users.noreply.github.com")
#   PERSIST_BRANCH   — branch to push to   (default: "main")

set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

DRY_RUN=false
for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
    esac
done

GIT_USER_NAME="${GIT_USER_NAME:-nix-agent}"
GIT_USER_EMAIL="${GIT_USER_EMAIL:-nix-agent@users.noreply.github.com}"
PERSIST_BRANCH="${PERSIST_BRANCH:-main}"

# files to persist (identity state only)
PERSIST_PATHS=(
    "identity/memory.md"
    "identity/journal/"
    "identity/soul.md"
    "identity/user.md"
)

log()  { echo "[persist $(date '+%H:%M:%S')] $*"; }
warn() { echo "[persist $(date '+%H:%M:%S')] WARN: $*" >&2; }

cd "$REPO_DIR" || { warn "cannot cd to $REPO_DIR"; exit 0; }

if ! command -v git &>/dev/null; then
    warn "git is not installed"
    exit 0
fi

git config user.name "$GIT_USER_NAME"
git config user.email "$GIT_USER_EMAIL"

# configure remote with PAT
if [ -n "${GITHUB_PAT:-}" ] && [ -n "${GITHUB_REPO:-}" ]; then
    AUTHENTICATED_URL="https://x-access-token:${GITHUB_PAT}@github.com/${GITHUB_REPO}.git"
    CURRENT_URL=$(git remote get-url origin 2>/dev/null || echo "")
    if [ "$CURRENT_URL" != "$AUTHENTICATED_URL" ]; then
        if git remote | grep -q '^origin$'; then
            git remote set-url origin "$AUTHENTICATED_URL"
        else
            git remote add origin "$AUTHENTICATED_URL"
        fi
    fi
elif [ -z "$(git remote get-url origin 2>/dev/null)" ]; then
    warn "no GITHUB_PAT/GITHUB_REPO set and no origin remote"
    exit 0
fi

# check for changes
CHANGES=()
for path in "${PERSIST_PATHS[@]}"; do
    if [ -e "$REPO_DIR/$path" ]; then
        status_output=$(git status --porcelain -- "$path" 2>/dev/null)
        if [ -n "$status_output" ]; then
            CHANGES+=("$path")
        fi
    fi
done

if [ ${#CHANGES[@]} -eq 0 ]; then
    log "no changes to persist"
    exit 0
fi

log "changes detected in: ${CHANGES[*]}"

if $DRY_RUN; then
    log "dry run — would commit:"
    git status --short -- "${PERSIST_PATHS[@]}"
    exit 0
fi

# stage identity files only
for path in "${PERSIST_PATHS[@]}"; do
    git add -- "$path" 2>/dev/null || true
done

CHANGED_FILES=$(git diff --cached --name-only 2>/dev/null)
if [ -z "$CHANGED_FILES" ]; then
    log "nothing staged — aborting"
    exit 0
fi

TIMESTAMP=$(date -u '+%Y-%m-%d %H:%M UTC')
FILE_COUNT=$(echo "$CHANGED_FILES" | wc -l | tr -d ' ')

COMMIT_MSG="chore(identity): auto-persist state — ${TIMESTAMP}

Auto-persisted by nix-agent.
Files: ${FILE_COUNT} changed
${CHANGED_FILES}"

if ! git commit -m "$COMMIT_MSG"; then
    warn "git commit failed"
    exit 0
fi

log "committed: ${FILE_COUNT} files"

# push with rebase
if ! git fetch origin "$PERSIST_BRANCH" 2>/dev/null; then
    warn "git fetch failed"
fi

if git rebase "origin/$PERSIST_BRANCH" 2>/dev/null; then
    log "rebase succeeded"
else
    warn "rebase conflict — aborting, trying merge"
    git rebase --abort 2>/dev/null || true
    if ! git merge "origin/$PERSIST_BRANCH" --strategy-option ours \
        -m "chore: merge remote (auto-persist)" 2>/dev/null; then
        git merge --abort 2>/dev/null || true
        warn "merge failed — will retry next run"
        exit 0
    fi
fi

if git push origin "HEAD:$PERSIST_BRANCH" 2>/dev/null; then
    log "pushed to origin/$PERSIST_BRANCH"
else
    warn "push failed — committed locally, will retry"
fi

exit 0
