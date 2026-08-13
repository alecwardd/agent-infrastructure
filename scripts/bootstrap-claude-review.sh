#!/usr/bin/env bash
# Roll the Claude Opus review caller out to alecwardd repositories.
#
# Safety properties:
#   * Never prints, stores, or transmits the OAuth token. It only reports
#     whether the secret NAME exists.
#   * Never force-overwrites an existing opus-review.yml.
#   * Installs the caller through a pull request, never a push to the default branch.
#   * --status and --labels are the only non-PR actions; both are idempotent.
#
# Usage:
#   ./bootstrap-claude-review.sh --status                       # audit all repos
#   ./bootstrap-claude-review.sh --repo alecwardd/x --labels    # create labels
#   ./bootstrap-claude-review.sh --repo alecwardd/x --install   # open caller PR
#   ./bootstrap-claude-review.sh --repo alecwardd/x --labels --install
#
# The CLAUDE_CODE_OAUTH_TOKEN secret is intentionally NOT handled here. Set it
# interactively so the value never reaches disk or shell history:
#   gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo alecwardd/<repo>

set -euo pipefail

OWNER="alecwardd"
SHARED_REF="alecwardd/agent-infrastructure/.github/workflows/reusable-opus-pr-review.yml@v1"
WORKFLOW_PATH=".github/workflows/opus-review.yml"
SECRET_NAME="CLAUDE_CODE_OAUTH_TOKEN"

REPO=""
DO_STATUS=0
DO_LABELS=0
DO_INSTALL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --repo)    REPO="$2"; shift 2 ;;
    --status)  DO_STATUS=1; shift ;;
    --labels)  DO_LABELS=1; shift ;;
    --install) DO_INSTALL=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

if [ "$DO_STATUS" -eq 0 ] && [ "$DO_LABELS" -eq 0 ] && [ "$DO_INSTALL" -eq 0 ]; then
  echo "Nothing to do. Pass --status, --labels, or --install (see --help)." >&2
  exit 2
fi

# name|color|description
LABELS=(
  "review:opus|5319e7|Request a Claude Opus deep review"
  "review:passed|0e8a16|Opus review found no P0/P1/P2 findings"
  "review:changes-requested|d93f0b|Opus review found blocking findings"
  "review:opus-error|b60205|Opus review failed to complete — not a pass"
)

has_secret() {
  # Lists secret NAMES only. The value is never retrievable via the API.
  gh secret list --repo "$1" --json name --jq '.[].name' 2>/dev/null | grep -Fxq "$SECRET_NAME"
}

has_caller() {
  gh api "repos/$1/contents/$WORKFLOW_PATH" --silent >/dev/null 2>&1
}

status_one() {
  local repo="$1" secret="MISSING" caller="missing" labels=0
  has_secret "$repo" && secret="present"
  has_caller "$repo" && caller="PRESENT"
  for entry in "${LABELS[@]}"; do
    gh label list --repo "$repo" --limit 200 --json name --jq '.[].name' 2>/dev/null \
      | grep -Fxq "${entry%%|*}" && labels=$((labels + 1))
  done
  printf '%-34s secret=%-8s caller=%-8s labels=%d/4\n' "$repo" "$secret" "$caller" "$labels"
}

ensure_labels() {
  local repo="$1"
  for entry in "${LABELS[@]}"; do
    IFS='|' read -r name color desc <<< "$entry"
    # --force makes this idempotent: creates, or updates colour/description.
    gh label create "$name" --repo "$repo" --color "$color" --description "$desc" --force >/dev/null
    echo "  label ok: $name"
  done
}

install_caller() {
  local repo="$1"

  if has_caller "$repo"; then
    echo "  SKIP: $WORKFLOW_PATH already exists in $repo. Not overwriting; update it manually." >&2
    return 0
  fi
  if ! has_secret "$repo"; then
    echo "  WARNING: $SECRET_NAME is not set in $repo. The caller will install but reviews will fail until you run:"
    echo "           gh secret set $SECRET_NAME --repo $repo"
  fi

  local default_branch branch tmp
  default_branch=$(gh repo view "$repo" --json defaultBranchRef --jq '.defaultBranchRef.name')
  branch="chore/opus-review-caller"
  tmp=$(mktemp -d)

  git clone --quiet --depth 1 "https://github.com/$repo.git" "$tmp/repo"
  (
    cd "$tmp/repo"
    git checkout --quiet -b "$branch"
    mkdir -p "$(dirname "$WORKFLOW_PATH")"
    cat > "$WORKFLOW_PATH" <<YAML
# Claude Opus deep PR review. Canonical implementation lives in
# alecwardd/agent-infrastructure — do not fork the reviewer logic here.
#
# Trigger: apply the \`review:opus\` label to a non-draft, same-repo pull request.

name: Opus Review

on:
  pull_request:
    types: [labeled]

jobs:
  opus-review:
    if: >-
      github.event.label.name == 'review:opus' &&
      github.event.pull_request.draft == false &&
      github.event.pull_request.head.repo.full_name == github.repository

    permissions:
      contents: read
      pull-requests: write
      issues: read
      actions: read

    uses: $SHARED_REF
    with:
      pr_number: \${{ github.event.pull_request.number }}
    secrets:
      CLAUDE_CODE_OAUTH_TOKEN: \${{ secrets.CLAUDE_CODE_OAUTH_TOKEN }}
YAML
    git add "$WORKFLOW_PATH"
    git commit --quiet -m "chore: add Claude Opus deep review caller

Delegates to the canonical reusable workflow in alecwardd/agent-infrastructure.
Runs only when the review:opus label is applied to a non-draft, same-repo PR.
Read-only: no contents:write, no commits, no merges."
    git push --quiet -u origin "$branch"
  )

  gh pr create --repo "$repo" --base "$default_branch" --head "$branch" \
    --title "chore: add Claude Opus deep review caller" \
    --body "Adds the standard caller for the shared Opus reviewer in \`alecwardd/agent-infrastructure\`.

**Trigger:** apply the \`review:opus\` label to a non-draft, same-repo PR. Never runs on push.

**Permissions:** \`contents: read\` only — this reviewer cannot commit, push, or merge.

CodeRabbit continues to run independently. Human merge authority is unchanged.

See the [system documentation](https://github.com/alecwardd/agent-infrastructure/blob/main/docs/claude-opus-review.md)."

  rm -rf "$tmp"
}

if [ "$DO_STATUS" -eq 1 ]; then
  echo "=== Opus review rollout status ==="
  if [ -n "$REPO" ]; then
    status_one "$REPO"
  else
    while read -r r; do status_one "$OWNER/$r"; done < <(
      gh repo list "$OWNER" --limit 200 --no-archived --json name --jq '.[].name' | sort
    )
  fi
  echo
  echo "secret=present means the NAME exists; values are never readable via the API."
fi

if [ "$DO_LABELS" -eq 1 ] || [ "$DO_INSTALL" -eq 1 ]; then
  if [ -z "$REPO" ]; then
    echo "--labels/--install require an explicit --repo. Mass rollout is intentionally opt-in per repo." >&2
    exit 2
  fi
  echo "=== $REPO ==="
  [ "$DO_LABELS" -eq 1 ]  && ensure_labels "$REPO"
  [ "$DO_INSTALL" -eq 1 ] && install_caller "$REPO"
fi
