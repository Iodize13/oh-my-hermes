# Native Hermes Subagent Wrapper Notes

This README is intentionally limited to the documentation changes from the native Hermes subagent wrapper work.

## Moving This Patch to Another Machine

If you want the same docs patch on another machine, transfer the patch or git commit, not a Hermes profile.

Common ways to do that:

```bash
# Save the patch from the current repo
git diff > native-hermes-subagents-wrapper-docs.patch

# Copy the patch file to another machine and apply it there
git apply native-hermes-subagents-wrapper-docs.patch
```

If you already committed the change, you can also move it with normal git workflows such as pushing the branch, fetching it on the other machine, or cherry-picking the commit.

This is about moving the patch itself across machines, not moving Hermes profile state.

## Native Wrapper Config Example

The native wrapper uses a separate `subagents:` block. `delegate_task` still uses the `delegation:` settings; the wrapper config controls named types, default background behavior, and whether selected types use git worktrees.

```yaml
subagents:
  default_type: general-purpose
  default_max_iterations: 50
  max_concurrent_background: 4

  types:
    general-purpose:
      description: "General-purpose coding and research subagent."
      model: openai-codex/gpt-5.4-mini
      toolsets: [terminal, file, web]
      prompt_mode: append
      default_background: false
      readonly: false
      worktree: false
      role: leaf

    explore:
      description: "Read-only exploration subagent for codebase inspection and research."
      model: openai-codex/gpt-5.4-mini
      toolsets: [terminal, file, web]
      prompt_mode: append
      default_background: false
      readonly: true
      worktree: false
      role: leaf

    plan:
      description: "Read-only planning subagent that writes recommendations instead of changing files."
      model: openai-codex/gpt-5.5
      toolsets: [terminal, file, web]
      prompt_mode: append
      default_background: false
      readonly: true
      worktree: false
      role: leaf

    code-review:
      description: "Review-only type that runs in an isolated worktree."
      model: openai-codex/gpt-5.5
      toolsets: [terminal, file, web]
      prompt_mode: append
      default_background: false
      readonly: true
      worktree: true
      role: leaf

  worktrees:
    enabled: true
    branch_prefix: hermes-subagent-
    cleanup_orphans_on_start: false
    base_dir: ""
```

Notes:

- `default_type` controls what `subagent_spawn` uses when you do not specify a type.
- `readonly: true` is ideal for `explore` and `plan` types; those types should avoid writing files.
- `worktree: true` is an explicit opt-in per type. Keep it off unless the child needs an isolated checkout.
- `worktrees.enabled: true` is the global switch that allows wrapper-managed worktrees at all.

## Troubleshooting Worktree Setup Failures

If `git worktree add` fails, the most common causes are:

- Not in a git repository — the directory has no `.git/` metadata, so git cannot create a worktree.
- Repository has no `HEAD` yet — usually a brand-new repo with no commit. Create the first commit, or `git checkout` a branch with a valid commit before retrying.
- Target path already exists — remove or rename the destination directory before creating the worktree.
- Branch/path conflicts — the branch may already be checked out in another worktree, or the branch name may already be in use.

A reliable recovery sequence is:

```bash
git status

git branch --show-current

git worktree list

# If this repo is brand new, make an initial commit first.
# If the target directory already exists, choose a different path.
```

If Hermes itself is running in `hermes -w` or a wrapper worktree mode and startup fails, first verify the repo is valid and has a commit, then retry from a clean checkout. When the repo is valid, `hermes -w` creates and owns the disposable worktree automatically.

## Background Child Lifecycle Limitations

The native subagent wrapper (`subagent_spawn`, `subagent_result`, `subagent_list`, `subagent_steer`) has a different lifecycle from `delegate_task`:

- Background children get a stable child ID immediately, then finish later.
- Use `subagent_result` to poll for completion and fetch the final result.
- Use `subagent_list` to inspect active and recently-completed wrapper children.
- Use `subagent_steer` only while a child is running; it is rejected once the child is terminal.
- Background children are still not a general job queue: if the parent process exits, restarts, or is otherwise torn down, the running child may stop receiving supervision and its state should not be treated as durable automation.

Practical rule: use wrapper background mode for interactive or human-supervised work, and use `cronjob` or `terminal(background=True, notify_on_complete=True)` when the work must survive the parent turn and deliver a completion signal later.

## Choosing the Right Surface

Hermes now has four related but distinct ways to run work in isolation. Pick the one that matches the lifecycle you need:

| Surface                                                                                             | Best for                                                                                                             | Lifecycle                                              | Notes                                                                                                                                                     |
| --------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `delegate_task`                                                                                     | Fresh-context reasoning, research, refactors, and code review                                                        | Synchronous inside the current turn                    | The classic Hermes subagent primitive. Great when you want the parent to wait for one or more children and then synthesize their results.                 |
| Native subagent wrapper (`subagent_spawn` / `subagent_result` / `subagent_list` / `subagent_steer`) | Long-lived or interactive child work, named subagent types, background polling, steering, or wrapper-specific policy | Foreground or background, with explicit result polling | Use this when you want lifecycle control. It reuses Hermes child execution but gives you a stable child ID, background status, and list/steer operations. |
| MCP bridge for the wrapper                                                                          | External MCP clients, IDE integrations, or automation that already speaks MCP                                        | Same wrapper service, exposed over MCP                 | Use this when the caller is not the Hermes agent itself but still needs the same native wrapper behavior and limits.                                      |
| `hermes -w`                                                                                         | A whole Hermes session that should edit inside an isolated git checkout                                              | Session-wide, disposable worktree                      | Use this when you want the entire CLI session isolated from the main repo. It is broader than the wrapper’s opt-in per-type worktree mode.                |

Rule of thumb:

- Use `delegate_task` for most reasoning-heavy subtasks.
- Use the native wrapper when you need to control or observe the child lifecycle explicitly.
- Use the MCP bridge only when an external MCP client needs the same wrapper behavior.
- Use `hermes -w` when you want the whole session to live in its own git worktree.

## Sharing a Sanitized Config

When you want to share setup details without secrets, keep the sensitive values out of the file and provide a redacted example instead:

```yaml
model:
  default: anthropic/claude-sonnet-4
  provider: anthropic
terminal:
  backend: local
  cwd: /absolute/path/to/project
delegation:
  model: google/gemini-3-flash-preview
  provider: openrouter
```

Use placeholders for anything credential-like:

```yaml
# ~/.hermes/profiles/coder/.env.example
OPENAI_API_KEY=***
DISCORD_BOT_TOKEN=***
```

If you need to share a full working agent, prefer a profile distribution repo instead of handing out raw profile files.

## Updating a Source Checkout

Before updating a git install, do this in order:

1. Commit or stash local source changes first so you have a clean baseline.
2. Rebase or merge your fork onto upstream `main` before pulling the next release.
3. Run `hermes config migrate` after the update to add any new config options.
4. Rerun targeted subagent tests after the upgrade, especially delegation and worktree-related coverage.

If you maintain a fork, keep the branch in sync with upstream regularly so `hermes update` does not have to reconcile a long-lived drift.

For update validation, rerun the relevant docs or wrapper tests after changes land.
