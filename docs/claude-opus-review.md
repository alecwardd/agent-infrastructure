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

## 6. Permissions and privilege separation

**Claude never executes in a job that holds GitHub mutation authority.** The run
is split into four jobs with disjoint permissions:

| Job | Permissions | Runs Claude? | Does |
| --- | --- | --- | --- |
| `claim` | `pull-requests: write` | no | Preflight guards; consume the trigger label; clear stale verdicts |
| `prepare` | `contents/pull-requests/issues/checks/statuses: read` | no | Stage PR + base-revision context, upload as artifact |
| `review` | `contents/pull-requests/issues/actions: read` | **yes** | Reason only; emit result artifact |
| `publish` | `pull-requests: write` | no | Post the review; apply verdict labels; handle errors |

The `review` job holds **no write permission of any kind**. Even a fully
compromised model in that job cannot comment, label, push, or merge — not
because it is told not to, but because the token it holds cannot.

The caller declares the *union* as a ceiling; each job downgrades from there. A
called workflow can only reduce the caller's grant, never escalate, which is why
the caller must declare the ceiling rather than the minimum.

| Permission | Why |
| --- | --- |
| `contents: read` | Check out and read source and git history |
| `pull-requests: write` | `claim` and `publish` only: review comment and `review:*` labels |
| `issues: read` | Read the linked specification issue |
| `actions: read` | Inspect CI runs and job logs (`additional_permissions: actions: read`) |
| `checks: read`, `statuses: read` | `gh pr checks` resolves `statusCheckRollup`, which spans check runs *and* commit statuses. Both are needed to see third-party reviewers (CodeRabbit, Cursor Bugbot), not just Actions runs |

`contents: write` is **never** granted anywhere in the pipeline. `issues: write`
is not granted: label operations on a pull request are covered by
`pull-requests: write`.

### Result transfer between jobs

The review payload moves from `review` to `publish` as an **artifact**, not a job
output. A review can approach 40 KB of arbitrary markdown; artifacts impose no
escaping, expression-interpolation, or size constraints on that content, and
`download-artifact` is scoped to the current run by default so it needs no extra
permission. The publisher re-validates the JSON before acting on it.

### A completed review is never discarded

An Opus review is expensive — several dollars and up to ten minutes — so the
pipeline treats a finished review as something to be preserved even when the
action step around it fails. The action fails its step for conditions that say
nothing about the quality of the review, most notably its guard on a successful
result whose turn count exceeded `--max-turns`. Losing the review in that case
means paying full price for no feedback.

So the Claude step runs `continue-on-error: true`, and the next step decides the
outcome itself. It prefers the action's `structured_output` output and, when the
failed step never exported one, recovers the same payload from the execution log
(`execution_file`, a JSON array of SDK messages whose result message carries
`structured_output`).

**This does not weaken fail-closed behaviour.** The verdict is still gated on a
structurally valid review — `result`, `review_markdown`, and `counts` must all be
present — and a run without one fails the job, applies `review:opus-error`, and
posts the "did not complete" comment exactly as before. What changed is that the
*deterministic* layer decides, rather than an action-side error the pipeline
cannot interpret.

### Failure diagnostics

The same step writes `diagnostics.json` into the result artifact: a strict
whitelist of scalar result metadata — `subtype`, `is_error`, `terminal_reason`,
`api_error_status`, `stop_reason`, durations, `num_turns`, `total_cost_usd`,
input/output token counts, a permission-denial *count*, and a 300-character
error excerpt with credential-shaped strings redacted.

Never included: the prompt, the transcript, assistant messages, tool results, or
any repository content. Both the Actions log and the artifact are effectively
public on a public repository, so the whitelist is deliberately narrow and the
raw execution file is never uploaded — it contains the entire review transcript.
`show_full_output` remains off for the same reason.

`api_error_status` and `terminal_reason` are the two fields that identify an
authentication or quota rejection immediately, which the action's own error
message does not.

---

## 7. Claude configuration

| Setting | Value | Rationale |
| --- | --- | --- |
| Action | `anthropics/claude-code-action@v1` | Current major; v1 inputs only, no deprecated ones |
| Model | `--model opus` | Alias tracks the latest Opus rather than pinning a stale dated id |
| Effort | `--effort high` | Deep reasoning without the unbounded cost of `max` |
| Turns | `--max-turns 80` | A deep multi-file review routinely needs 40-60; the action fails a run whose turn count exceeds this bound, so headroom matters. Lower per-caller to cap spend |
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
| `base-instructions/` | **Authoritative** repository instructions, from the PR base revision |
| `pr.json` | PR metadata and changed-file list |
| `pr.diff` | Complete diff (capped at 1.5 MB, truncation disclosed) |
| `spec.md` | Linked issue(s), or an explicit "no linked issue" notice |
| `discussion.md` | Existing PR discussion, including CodeRabbit |
| `ci.txt` | Deterministic CI check results |

### Authoritative instructions vs. reviewable content

A pull request must not be able to rewrite the rules that govern its own review.
`AGENTS.md`, `CLAUDE.md`, and `README.md` are therefore fetched **at the PR base
revision** by a deterministic step and staged into `base-instructions/`. Only
those copies carry authority, alongside the prompt itself.

The PR's own versions of those same files stay in the working tree and are
classified as **data to review, not instructions to obey**. The prompt states
this explicitly and tells Claude that where the two disagree, the base copy wins
and the difference is itself something to evaluate. Legitimate improvements to an
instruction file are reviewed normally and are not findings; changes that weaken
review obligations, broaden permissions, disable checks, or address an automated
reviewer are called out.

This complements a protection the action already provides: on pull requests it
restores `.claude/`, `.mcp.json`, and `CLAUDE.md` from the base branch. That list
does not include `AGENTS.md` or `README.md`, so staging all three explicitly makes
the rule uniform rather than dependent on the action's internal list.

Claude then reads only the architecture docs the diff actually touches.
Repository conventions override the generic reviewer's assumptions. The prompt is
domain-neutral: no repository's architecture or vocabulary is baked in.

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
- **Claude holds no write authority.** The job Claude runs in has read-only
  permissions; a separate job with no Claude execution performs every state
  change. See §6.
- **Untrusted input.** The diff, PR body, discussion, and repository files are
  treated as data, not instruction. Instruction files from the PR are explicitly
  demoted to data, with authoritative copies staged from the base revision (§8).
  The prompt instructs Claude to report embedded instructions as a P0
  prompt-injection finding rather than obey them. The action independently strips
  HTML comments, invisible characters, and hidden attributes, and restores
  `.claude/`, `CLAUDE.md`, and `.mcp.json` from the **base** branch.
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
   If the caller was added by merging a PR, prefer a PR opened *after* that
   merge. Any PR that was already open predates the workflow and needs
   `gh pr update-branch` first — see
   [PRs that predate onboarding](#prs-that-predate-onboarding).

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
| **No run at all on a pre-existing PR** | The PR branched before the caller landed on the default branch. See "PRs that predate onboarding" below — this is the most likely surprise on the first repo you onboard. |
| Workflow never starts | Label name must be exactly `review:opus`. Check the PR is not a draft and not from a fork. |
| `Workflow initiated by non-human actor` | The label was applied by a bot. See §12. |
| Authentication failure | `CLAUDE_CODE_OAUTH_TOKEN` missing, expired, or **invalidated by a later `claude setup-token` run**. Issuing a new token can revoke the stored one, so the reviewer keeps working until the next PR and then fails. Diagnose from the `diagnostics.json` in the `opus-review-result` artifact: `api_error_status: 401` with `terminal_reason: api_error`, `num_turns: 1`, and `total_cost_usd: 0` is exactly this. Re-run `claude setup-token`, verify it locally, then re-set the secret. |
| `did not return structured_output` | A symptom, never the cause. The action emits this whenever the result is flagged as an error, regardless of whether structured output existed. Read `diagnostics.json` for the real reason. |
| `exceeding the configured maximum of N` turns | The SDK can overshoot `--max-turns`, and the action fails a successful result whose turn count exceeds it. The review itself is fine and is recovered from the execution log rather than discarded; raise `max_turns` for that caller. |
| `review:opus-error` applied | The run failed or timed out. **Not a pass.** Check the run log, then re-apply `review:opus`. |
| Label step 403s | Confirm the caller declares `pull-requests: write`; a called workflow cannot escalate beyond it. |
| CI tools unavailable | Both `actions: read` in `permissions:` and `additional_permissions: actions: read` are required. |
| Review truncated | `review_markdown` is capped at 60000 chars when posted (GitHub's limit is 65536). |
| Two reviews on one PR | The label was applied twice. It is consumed at run start; re-applying is a new request. |

### PRs that predate onboarding

**Symptom:** you apply `review:opus` to an open PR and *nothing happens* — no run,
no failure, no check. The Actions tab shows no Opus Review run for that PR.

**Cause.** GitHub resolves `pull_request` workflows from the PR's **merge
commit**, not from the default branch. A PR branched before the caller landed
has a merge ref computed against the older base, and `opus-review.yml` does not
exist in that tree. The `labeled` event fires, GitHub finds no matching workflow,
and discards it silently. Nothing is broken and nothing is logged.

Confirm it in one command — if the listing does not include `opus-review.yml`,
this is your cause:

```bash
gh api "repos/OWNER/REPO/contents/.github/workflows?ref=refs/pull/PR/merge" --jq '.[].name'
```

**Fix.** Refresh the branch so its merge ref is recomputed against current base:

```bash
gh pr update-branch PR -R OWNER/REPO
```

Then **remove and re-apply** `review:opus`. The original `labeled` event was
already consumed; a label sitting on the PR does not re-fire on its own.

> The GitHub UI's "Update branch" button does the same thing, but it is only
> rendered when the branch is behind *and* either branch protection requires
> up-to-date branches or the repo has **Settings → General → Pull Requests →
> "Always suggest updating pull request branches"** enabled. That setting is off
> by default, so on a fresh repo the button will not be there. The CLI command
> above works regardless.

**Scope.** This is a one-time wrinkle per repository, affecting only PRs that
were already open when the caller merged. Any PR branched afterwards picks the
workflow up automatically. It is worth knowing about before onboarding a repo
with active PRs, because the failure mode looks like a broken reviewer rather
than a stale merge ref.

---

## 15. Rollout

`scripts/bootstrap-claude-review.sh` enumerates repositories, reports secret
presence **without revealing the value**, creates labels, and installs the caller
**via a pull request** — never a direct push to a default branch. It refuses to
overwrite an existing `opus-review.yml`.

Do not mass-roll out before the pilot has produced a real review. Order:
pilot repository → confirm end to end → then batch the rest.
