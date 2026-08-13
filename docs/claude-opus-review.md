# Claude Opus deep PR review

Our specific system. Anthropic's and GitHub's own documentation is linked rather
than duplicated.

- [Claude Code Action](https://github.com/anthropics/claude-code-action) · [security notes](https://github.com/anthropics/claude-code-action/blob/main/docs/security.md)
- [GitHub reusable workflows](https://docs.github.com/en/actions/reference/workflows-and-actions/reusing-workflow-configurations)

---

## 1. Architecture

One canonical reviewer here; a ~40-line caller in each consuming repository.

```
alecwardd/agent-infrastructure          (PUBLIC — no secrets)
  .github/workflows/reusable-opus-pr-review.yml
        ▲ workflow_call
        │
   ┌────┴─────────────────────────────────────────┐
   │ repo A .github/workflows/opus-review.yml     │  + CLAUDE_CODE_OAUTH_TOKEN secret
   │ repo B .github/workflows/opus-review.yml     │  + review:* labels
   │ ...                                          │
   └──────────────────────────────────────────────┘
```

The caller owns the *event and the guards*; the shared workflow owns *everything
canonical*: action version, model, effort and turn limits, permissions, the
reviewer prompt, the severity taxonomy, the result contract, and the label state
transitions.

### Why public

GitHub allows a private repository to share reusable workflows only with **other
private repositories owned by the same account** (`access_level: user`). A public
caller can only consume workflows from a public repository. Because participating
repositories span both visibilities, only a public home serves all of them without
per-repo access configuration. The workflow source holds no credentials, so this
costs nothing in security.

---

## 2. Division of responsibility

| Actor | Role |
| --- | --- |
| Cursor Cloud | implementation |
| CodeRabbit | continuous, routine PR review |
| Claude Opus | heavyweight independent review checkpoint (this system) |
| Human | final approval and merge |

**Deterministic automation performs deterministic state changes; the model only
reasons.** Claude never mutates GitHub. Workflow steps post the comment and move
the labels, driven by the model's structured verdict.

---

## 3. Trigger

`pull_request: types: [labeled]`, guarded in the caller:

```yaml
if: >-
  github.event.label.name == 'review:opus' &&
  github.event.pull_request.draft == false &&
  github.event.pull_request.head.repo.full_name == github.repository
```

Opus does **not** run on push, on every commit, or on comments. It runs when the
`review:opus` label is applied and at no other time. The shared workflow
re-verifies open/non-draft/non-fork itself, so a misconfigured caller cannot
bypass the guards.

### Duplicate-run protection

Two independent mechanisms:

1. **The trigger label is consumed at the start of the run.** One label
   application produces exactly one review. Re-requesting means re-applying it.
2. **Concurrency group** `opus-review-<repo>-<pr>` with `cancel-in-progress: true`.
   A newer request supersedes an in-flight one rather than running two
   simultaneous Opus reviews, and the surviving run reviews the newer head.

---

## 4. Labels

| Label | Meaning | Set by |
| --- | --- | --- |
| `review:opus` | Deep review requested | You or Cursor. Removed when the run starts |
| `review:passed` | No P0/P1/P2 findings | Workflow |
| `review:changes-requested` | Blocking findings remain | Workflow |
| `review:opus-error` | Run failed/timed out — **not** a pass | Workflow |

Stale verdict labels are cleared at the start of each run, so a PR never carries
contradictory states. `review:passed` means *an independent reviewer found no
blocking issues* — it is not an approval and never triggers a merge.

---

## 5. Authentication

### Claude

Repository Actions secret `CLAUDE_CODE_OAUTH_TOKEN`, generated locally:

```bash
claude setup-token
```

Available to Claude Pro and Max subscribers. The token is passed to the reusable
workflow through an explicitly named `secrets:` block — never `secrets: inherit`,
so the shared workflow receives that one credential and no other.

> **GitHub has no personal-account equivalent of organization-level Actions
> secrets.** Only organizations have shared secrets; personal accounts have
> repository and environment secrets only. The token must therefore be installed
> once per participating repository. `scripts/bootstrap-claude-review.sh` reports
> which repositories are missing it, without ever reading its value.

Never commit the token, echo it, or paste it into an issue, PR, or log.

### GitHub — and why the Claude GitHub App is not required

The workflow passes `github_token: ${{ github.token }}`. In the action's source,
supplying that input short-circuits the OIDC-exchange path entirely, so no Claude
GitHub App installation token is ever minted. Consequences:

- **The [Claude GitHub App](https://github.com/apps/claude) does not need to be
  installed** on participating repositories.
- `id-token: write` is not needed.
- Claude's GitHub capability is exactly the job's `permissions:` block — nothing
  more. This is the enforcement mechanism behind "Claude cannot push code",
  rather than an instruction in a prompt.

Trade-off: the review comment is authored by `github-actions[bot]` rather than
`claude[bot]`. Worth it — an App token carries `contents: write` by design, which
would defeat the read-only guarantee.

---

## 6. Permissions

Declared in both the caller and the shared workflow. A called workflow can only
*reduce* the caller's grant, never escalate, which is why the caller must declare
the ceiling.

| Permission | Why |
| --- | --- |
| `contents: read` | Check out and read source and git history |
| `pull-requests: write` | Post the review comment; add/remove `review:*` labels on the PR |
| `issues: read` | Read the linked specification issue |
| `actions: read` | Inspect CI runs and job logs (`additional_permissions: actions: read`) |

`contents: write` is **never** granted. `issues: write` is not granted: label
operations on a pull request are covered by `pull-requests: write`. If a future
GitHub change makes PR label writes require `issues: write`, the failure surfaces
loudly in the "Publish review and apply verdict" step rather than silently
passing.

---

## 7. Claude configuration

| Setting | Value | Rationale |
| --- | --- | --- |
| Action | `anthropics/claude-code-action@v1` | Current major; v1 inputs only, no deprecated ones |
| Model | `--model opus` | Alias tracks the latest Opus rather than pinning a stale dated id |
| Effort | `--effort high` | Deep reasoning without the unbounded cost of `max` |
| Turns | `--max-turns 40` | Enough for a real multi-file investigation; bounded |
| Step timeout | 30 min (configurable) | Job carries a hard 60-minute ceiling regardless |
| Mode | agent (implied by `prompt`) | Tag mode would auto-grant `git commit`/`git push` tools |

**Agent mode matters.** Tag mode's default tool set includes `Bash(git add:*)`,
`Bash(git commit:*)` and a git-push wrapper. Providing `prompt` selects agent
mode, which grants nothing implicitly.

### Tool policy

Allowed: `Read`, `Glob`, `Grep`, `LS`, read-only git
(`git log/show/diff/blame/status/rev-parse`), and the three `mcp__github_ci__*`
tools for CI inspection.

Denied: `Edit`, `MultiEdit`, `Write`, `NotebookEdit`, `WebFetch`, `WebSearch`,
and `Bash(git push|commit|add|rm)`, `Bash(gh:*)`, `Bash(curl:*)`, `Bash(rm:*)`.
`--disallowedTools` takes precedence over any allow rule.

Claude needs no network and no GitHub token because all GitHub context is
pre-staged into `/tmp/review-context` by deterministic steps (see §8).

---

## 8. What the reviewer reads

Workflow steps stage everything before Claude starts, so the model never
constructs an API call and there is no injection path through command arguments:

| File | Contents |
| --- | --- |
| `pr.json` | PR metadata and changed-file list |
| `pr.diff` | Complete diff (capped at 1.5 MB, truncation disclosed) |
| `spec.md` | Linked issue(s), or an explicit "no linked issue" notice |
| `discussion.md` | Existing PR discussion, including CodeRabbit |
| `ci.txt` | Deterministic CI check results |

Claude then reads repository-specific instructions in priority order — `AGENTS.md`,
`CLAUDE.md`, `README.md` — followed by only the architecture docs the diff
actually touches. Repository conventions override the generic reviewer's
assumptions. The prompt is domain-neutral: no repository's architecture or
vocabulary is baked in.

### Specification retrieval

The authoritative mechanism is GitHub's own closing-reference graph
(`closingIssuesReferences` via GraphQL), which resolves `Closes #123` and
manually linked issues alike. If nothing resolves, the reviewer is instructed to
review the code anyway and state plainly that spec-compliance review could not be
performed. It never guesses an issue.

### CI

CI status is staged deterministically, and Claude can pull failing job logs via
the CI tools. The prompt states that passing CI is evidence rather than proof,
that failing CI should normally prevent PASS, and that missing required CI must
be called out. Claude cannot modify CI results.

---

## 9. Result contract

Claude returns a validated JSON object via `--json-schema`, surfaced on the
action's supported `structured_output` output — no console-log scraping:

```json
{ "result": "PASS | CHANGES_REQUESTED",
  "review_markdown": "...",
  "spec_issue": "#10",
  "counts": { "p0": 0, "p1": 0, "p2": 0, "p3": 2 } }
```

`review_markdown` also ends with the human-readable marker
`CLAUDE_REVIEW_RESULT: PASS` / `CHANGES_REQUESTED`, so the verdict is visible in
the comment as well as machine-readable.

The deterministic layer cross-checks the model against itself: a `PASS` reported
alongside any P0/P1/P2 count is downgraded to `CHANGES_REQUESTED` with a warning.
Severity taxonomy and the six-field finding format live in the shared prompt.

### Output shape

One top-level review comment per run. Inline comments are deliberately not used
in v1 — one finding should not become five notifications, and a clean top-level
review avoids complicating authentication. The comment carries a
`<!-- claude-opus-review -->` marker for future de-duplication.

---

## 10. Security model

- **No secrets in this repository.** The workflow source is public and holds none.
- **Fork PRs are excluded**, in the caller and again in the shared workflow.
  `pull_request` from a fork receives no secrets, so the review simply cannot run
  for forks — and `pull_request_target` is **deliberately not used**. Using it to
  reach secrets while checking out PR-authored code is the classic pwn-request
  pattern; excluding forks is the honest trade in v1.
- **Untrusted input.** The diff, PR body, discussion, and repository files are
  treated as data, not instruction. The prompt states this explicitly and
  instructs Claude to report embedded instructions as a P0 prompt-injection
  finding rather than obey them. The action independently strips HTML comments,
  invisible characters, and hidden attributes, and restores `.claude/`,
  `CLAUDE.md`, and `.mcp.json` from the **base** branch so a PR cannot rewrite the
  reviewer's own configuration.
- **Log hygiene.** `show_full_output` and `display_report` stay at their secure
  defaults (`false`). Public-repository Actions logs are world-readable, so full
  model and tool output is never enabled. Anthropic's subprocess secret-scrubbing
  behavior is left intact. Do not set the `ACTIONS_STEP_DEBUG` secret on these
  repositories — it force-enables full output.
- **No static PAT.** Only the short-lived per-job workflow token is used.
- **Bot triggers.** The action requires a human actor in agent mode. See §12.

---

## 11. Onboarding a repository

1. **Add the secret** (per repository — no personal-account shared secrets exist):
   ```bash
   gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo alecwardd/<repo>
   ```
   Paste at the prompt. Nothing is written to disk or shell history.
2. **Create the labels:**
   ```bash
   ./scripts/bootstrap-claude-review.sh --repo alecwardd/<repo> --labels
   ```
3. **Add the caller** — copy `examples/opus-review-caller.yml` to
   `.github/workflows/opus-review.yml`. No customization required.
4. **Verify Actions are enabled** for the repository. No Actions *access* setting
   is needed on `agent-infrastructure` because it is public.
5. **Test** on one safe PR: apply `review:opus`, confirm the review posts.

The Claude GitHub App is **not** required (§5).

---

## 12. Interaction with Cursor

GitHub is the coordination bus; the two agents share no container and run in
isolated environments against the same PR state. Cursor implements, opens the
draft PR, responds to CI, and — when implementation is substantially complete —
applies `review:opus`. It then reads the review comment, addresses valid
findings, pushes, and may re-request review by re-applying the label.

> **One constraint to know:** the action refuses to run for non-human actors. If
> Cursor applies the label via a GitHub App identity (e.g. `cursor[bot]`), the run
> fails the actor check. Either have Cursor act through a user-authenticated
> token (PAT), or add its bot name to the action's `allowed_bots` input in the
> shared workflow. Do not set `allowed_bots: '*'` on public repositories.

Re-review is always explicit. Opus never re-runs automatically on a Cursor fix
push — that is the entire point of the label trigger.

---

## 13. Versioning and pinning

Callers use `@v1`, a moving major tag here. Prompt and policy improvements
propagate to every repository without touching them; breaking changes go to `v2`.

The trade-off: a moving tag means a change here alters behavior everywhere at
once. That is desirable for a reviewer whose whole value is one canonical policy,
and the blast radius is bounded — a bad prompt produces a bad *review*, never a
bad commit, because the workflow has no write authority.

Pin a commit SHA (`@<sha>`) instead if a repository ever needs a frozen reviewer.
The upstream action is separately pinned at `@v1`; move it to an exact tag if you
want fully reproducible runs.

---

## 14. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Workflow never starts | Label name must be exactly `review:opus`. Check the PR is not a draft and not from a fork. |
| `Workflow initiated by non-human actor` | The label was applied by a bot. See §12. |
| Authentication failure | `CLAUDE_CODE_OAUTH_TOKEN` missing or expired. Re-run `claude setup-token` and re-set the secret. |
| `review:opus-error` applied | The run failed or timed out. **Not a pass.** Check the run log, then re-apply `review:opus`. |
| Label step 403s | Confirm the caller declares `pull-requests: write`; a called workflow cannot escalate beyond it. |
| CI tools unavailable | Both `actions: read` in `permissions:` and `additional_permissions: actions: read` are required. |
| Review truncated | `review_markdown` is capped at 60000 chars when posted (GitHub's limit is 65536). |
| Two reviews on one PR | The label was applied twice. It is consumed at run start; re-applying is a new request. |

---

## 15. Rollout

`scripts/bootstrap-claude-review.sh` enumerates repositories, reports secret
presence **without revealing the value**, creates labels, and installs the caller
**via a pull request** — never a direct push to a default branch. It refuses to
overwrite an existing `opus-review.yml`.

Do not mass-roll out before the pilot has produced a real review. Order:
pilot repository → confirm end to end → then batch the rest.
