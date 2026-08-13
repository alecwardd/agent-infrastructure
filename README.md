# agent-infrastructure

Shared development automation for `alecwardd` repositories. This repository owns
infrastructure only — it contains no application code, no secrets, and no
repository-specific private context.

## What lives here

| Path | Purpose |
| --- | --- |
| [`.github/workflows/reusable-opus-pr-review.yml`](.github/workflows/reusable-opus-pr-review.yml) | Canonical Claude Opus deep PR reviewer (reusable workflow) |
| [`examples/opus-review-caller.yml`](examples/opus-review-caller.yml) | The tiny per-repo caller to copy into consuming repositories |
| [`docs/claude-opus-review.md`](docs/claude-opus-review.md) | Architecture, setup, security model, troubleshooting |
| [`scripts/bootstrap-claude-review.sh`](scripts/bootstrap-claude-review.sh) | Safe, PR-based rollout helper |

## Why this repository is public

A **public caller repository cannot call a reusable workflow stored in a private
repository** — GitHub's access rules only allow private→private sharing (via the
"Accessible from repositories owned by the user" setting). Since participating
repositories include both public (`the-desk`) and private ones, a public shared
repository is the only home that serves all of them with no per-repo access
configuration.

This is safe because **the workflow source contains no credentials**. The Claude
OAuth token lives exclusively in each consuming repository's Actions secrets and
is passed in through `workflow_call`.

## The review loop

```
GitHub issue / spec
      ↓
Cursor implementation agent  →  draft PR  →  deterministic CI
      ↓
implementation substantially complete
      ↓
apply label `review:opus`
      ↓
Claude Opus deep review  ──→  PASS               → review:passed        → human merges
                         └──→ CHANGES_REQUESTED  → review:changes-requested → Cursor fixes → re-label
```

CodeRabbit continues to run as the ordinary continuous reviewer, untouched.
Opus is the expensive, explicitly-requested checkpoint. Humans retain sole merge
authority — a PASS is an independent reviewer's opinion, not an approval.

## Adding a repository

See [docs/claude-opus-review.md](docs/claude-opus-review.md#onboarding-a-repository).
Short version: add the `CLAUDE_CODE_OAUTH_TOKEN` secret, create four labels, copy
one 40-line caller workflow. Nothing in the caller needs customizing.

## Versioning

Callers reference `@v1`, a moving major tag on this repository. Prompt and policy
improvements reach every consuming repository without touching them; breaking
changes go to `v2`. See the
[versioning notes](docs/claude-opus-review.md#versioning-and-pinning) for when to
pin a commit SHA instead.
