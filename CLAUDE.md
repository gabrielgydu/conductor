# Conductor

Orchestration tool that runs multi-feature builds via speccer (spec loop) → runner (code gen) → integration merge.

## Running Conductor from Claude Code

Claude Code's bash tool has no TTY.

`conductor run`, `go` and `loop` notice that and start their tmux session detached instead of attaching to it: they print `started in tmux session <s>; attach with: tmux attach -t '=<s>:'` and exit 0.

**Simplest recipe — run the plain command, then monitor:**

```bash
conductor run --name <project> --project-dir <path>

# Monitor (`go` uses the same session; `loop` uses conductor-loop-<name>, window `loop`)
tmux capture-pane -t '=conductor-<project>:=conductor' -p 2>&1 | tail -20
tmux list-windows -t '=conductor-<project>:' 2>&1
conductor status --name <project> --project-dir <path>
```

Run it once: a second `conductor run` for the same project is refused while the first holds the project lock.

Re-running the same command after the orchestrator died resumes the run and adopts runners that are still working in the session.

**Alternative — drive the session yourself with `--inside-tmux`:**

Use this when you want to choose the session name or window.

Do not run the plain command first: it already created `conductor-<project>`, and `tmux new-session` would fail with "duplicate session".

```bash
# 1. Create detached tmux session
tmux new-session -d -s conductor-<project> -n conductor

# 2. Send command with --inside-tmux flag
tmux send-keys -t conductor-<project>:conductor \
  "conductor run --inside-tmux --name <project> --project-dir <path>" Enter

# 3. Monitor
tmux capture-pane -t conductor-<project>:conductor -p 2>&1 | tail -20
tmux list-windows -t conductor-<project> 2>&1
conductor status --name <project> --project-dir <path>
```

For `go` and `loop` replace the command (and add `--plan`); `loop` uses session `conductor-loop-<name>` and window `loop`.

Key flags:
- `--inside-tmux`: run the loop directly without creating a new tmux session
- `--no-overnight`: answer speccer questions yourself (by default, overnight mode, the brain auto-answers them)

## Go Mode (One-Shot)

`conductor go` combines init → plan → run into a single resumable command. On resume after kill/crash, it detects completed phases and skips them.

### Usage

```bash
# First run — provide a brief file
conductor go \
  --name my-proj \
  --project-dir ~/repo \
  --plan ~/.claude/plans/my-brief.md

# Resume after kill/crash — same command, skips completed phases
conductor go \
  --name my-proj \
  --project-dir ~/repo
```

### Running from Claude Code (no TTY)

Plain `conductor go ...` starts the detached session `conductor-<name>` (see the recipe above); monitor it with `tmux capture-pane -t '=conductor-<name>:=conductor' -p | tail -20` and `conductor status --name <name> --project-dir <path>`.

With `--inside-tmux` you create the session and send the command yourself, as in the alternative recipe above.

### Phase resume logic

| Condition | Phases executed |
|---|---|
| No state | init → copy brief → plan → run |
| State exists, no runs, `--plan` given | copy brief (replacing the old one) → plan → run |
| State exists, no runs, brief populated | plan → run |
| State exists, runs populated | run only (a different `--plan` is ignored with a warning) |

### Key flags

- `--plan`: path to brief file (required on first run when brief is empty)
- `--preset`: preset name (auto-detected if omitted; `--preset base` opts out of a detected preset)
- `--base-branch`: base branch (auto-detected if omitted)
- `--quick` / `--no-quick`: quick mode is turned on when `go` plans the project; on resume the saved setting is kept unless one of these flags is given
- `--max-parallel`: the saved setting, 1 (sequential runs) for a new project
- `--worktrees-base`: base directory for worktrees, the integration worktree included (`<base>/integration-<name>`); defaults to the preset's `worktrees_base`, else `<project>/../worktrees`
- `--reintegrate`: attempt the integration again when all runs are finished, even if it is not due (a failure that is not retried automatically)
- All `run` flags: `--no-overnight`, `--quick`/`--no-quick`, `--max-parallel`, `--worktrees-base`, `--reintegrate`, `--inside-tmux`
- Settings saved by an earlier run (quick, max parallel, worktrees base) are kept when the flag is left out; `--no-overnight` applies only to the invocation that passes it

## Loop Mode

`conductor loop` is a persistent prompt loop for fix/improvement tasks on existing branches. Unlike `conductor run` (which goes through speccer → runner → integration), loop mode takes a plan file with a task checklist and runs Claude repeatedly until all tasks are done.

### Usage

```bash
# Start a new loop
conductor loop \
  --name my-fix \
  --project-dir ~/my-project \
  --plan ./fix-plan.md \
  --preset myproject

# Check progress
conductor loop-status \
  --name my-fix \
  --project-dir ~/my-project

# Resume after interruption (no --plan needed)
conductor loop \
  --name my-fix \
  --project-dir ~/my-project
```

### Running from Claude Code (no TTY)

Plain `conductor loop ...` starts the detached session `conductor-loop-<name>` (window `loop`); monitor it with `tmux capture-pane -t '=conductor-loop-<name>:=loop' -p | tail -20` and `conductor loop-status --name <name> --project-dir <path>`.

With `--inside-tmux` you create the session and send the command yourself, as in the alternative recipe at the top.

A relative `--plan` is resolved against the current directory, as for `go`; if nothing exists there, `--project-dir` is tried next, with a printed note.

### Plan file format

The plan file can be **any format** — free-form markdown, structured phases, bullet lists, etc. Conductor will use Claude (Sonnet) to decompose it into a structured task checklist automatically. The generated checklist is saved to `CHECKLIST.md` in the conductor dir.

If the plan already contains `- [ ]` checklist items, those are used directly (no Claude call needed).

Each loop session receives **both** the original plan (full context) and the task checklist (progress tracking).

### How the loop works

1. On init: Claude decomposes the plan into atomic tasks (or uses existing checklist)
2. Builds prompt = task progress checklist + current task details + original plan
3. Runs one headless Claude session (`claude -p`, 200 turns max) in a window of the `conductor-loop-<name>` tmux session
4. Claude works until it outputs `<task-completed/>` or `<task-failed/>` on a line of its own, or the session ends without either (turn limit, crash)
5. On `<task-completed/>` → commit all changes in the worktree → advance to the next task
6. On a session without a signal → commit partial work as `WIP: <task>` → restart the same task with fresh context
7. If a pre-commit hook rejects a commit, the changes stay uncommitted and the next session's prompt quotes the hook output
8. Each task gets 3 sessions; if it is still not committed after the third (`<task-failed/>`, no signal or rejected commits), it is marked failed and the loop moves on
9. Sessions in which Claude never answered (expired login, unknown model, usage limit) don't count against the task; two in a row stop the loop, with the CLI's error in `LOOP-LOG.md`
10. The loop ends `completed` only when every task is completed, otherwise `failed`
11. State is persisted in `LOOP-STATE.json`, so resuming continues with the current task

The loop runs no quality gate and pushes nothing: review the `loop-<name>` branch (or the project dir's branch with `--no-worktree`) and push it yourself.

Pane logs are in the conductor dir's `logs/`; the per-session prompt and output files in `session/` are removed after each session.

### Key flags

- `--no-worktree`: work directly in the project dir instead of creating a worktree
- `--model`: override the Claude model (default: from preset or opus)
- `--reset`: discard previous loop state and start fresh
- `--preset`: preset supplying the default `model` and `worktrees_base` (auto-detected if omitted)

## Project Structure

- `src/conductor/` — main conductor package (CLI, orchestrator, models)
  - `src/conductor/prompts/speccer/` — speccer prompt templates (spec + generate, per mode)
  - `src/conductor/prompts/` — brain/diagnosis prompt templates
- `src/speccer/` — spec generation loop
- `src/runner/` — code execution runner
  - `src/runner/steer_inbox.py` — file-based steering IPC (polls `.msg` files from orchestrator)
- `conductor`, `speccer`, `runner` — bash wrapper scripts; `scripts/venv-run` creates, repairs and runs `.venv`

## Steering

The orchestrator steers running Claude sessions via file-based IPC:
- Orchestrator writes `.msg` files to `{feature_dir}/steer_inbox/` (atomic write via `.tmp` + rename)
- Runner polls the inbox every 2s and forwards messages to the steerable session
- No external CLI dependencies — fully self-contained

## Environment Variables

Templates generate `run.sh` files that reference:
- `CONDUCTOR_MODEL` — primary Claude model for phases
- `CONDUCTOR_FIX_MODEL` — model used for fix/retry phases

## Symlinks

Symlink `conductor`, `speccer`, and `runner` from this repo into a directory on your PATH (for example `~/.local/bin`) so the commands above work without a leading path.
