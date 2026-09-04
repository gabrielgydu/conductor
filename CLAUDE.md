# Conductor

Orchestration tool that runs multi-feature builds via speccer (spec loop) → runner (code gen) → integration merge.

## Running Conductor from Claude Code

Claude Code's bash tool has no TTY, so `conductor run` will fail with "open terminal failed: not a terminal".

**Workaround — use tmux send-keys:**

```bash
# 1. Create detached tmux session
tmux new-session -d -s conductor-<project> -n conductor

# 2. Send command with --inside-tmux flag
tmux send-keys -t conductor-<project>:conductor \
  "conductor run --inside-tmux --name <project> --project-dir <path> --overnight" Enter

# 3. Monitor
tmux capture-pane -t conductor-<project>:conductor -p 2>&1 | tail -20
tmux list-windows -t conductor-<project> 2>&1
conductor status --name <project> --project-dir <path>
```

Key flags:
- `--inside-tmux`: run the loop directly without creating a new tmux session
- `--overnight`: auto-answer speccer questions via brain (no human input needed)

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

```bash
tmux new-session -d -s conductor-go-<name> -n conductor

tmux send-keys -t conductor-go-<name>:conductor \
  "conductor go --inside-tmux --name <name> --project-dir <path> --plan <brief.md>" Enter

# Monitor
tmux capture-pane -t conductor-go-<name>:conductor -p 2>&1 | tail -20
conductor status --name <name> --project-dir <path>
```

### Phase resume logic

| Condition | Phases executed |
|---|---|
| No state | init → copy brief → plan → run |
| State exists, brief is placeholder | copy brief → plan → run |
| State exists, brief populated, no runs | plan → run |
| State exists, runs populated | run only |

### Key flags

- `--plan`: path to brief file (required on first run when brief is empty)
- `--preset`: preset name (default: base, auto-detected)
- `--base-branch`: base branch (auto-detected if omitted)
- `--quick`: enabled by default (use `--no-quick` to disable)
- `--max-parallel`: default 1 (sequential runs)
- `--worktrees-base`: base directory for worktrees (also used for integration merge worktree; defaults to `/tmp` if unset)
- All `run` flags: `--no-overnight`, `--no-quick`, `--max-parallel`, `--worktrees-base`, `--inside-tmux`

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

```bash
tmux new-session -d -s conductor-loop-<name> -n loop

tmux send-keys -t conductor-loop-<name>:loop \
  "conductor loop --inside-tmux --name <name> --project-dir <path> --plan <plan.md>" Enter

# Monitor
tmux capture-pane -t conductor-loop-<name>:loop -p 2>&1 | tail -20
conductor loop-status --name <name> --project-dir <path>
```

### Plan file format

The plan file can be **any format** — free-form markdown, structured phases, bullet lists, etc. Conductor will use Claude (Sonnet) to decompose it into a structured task checklist automatically. The generated checklist is saved to `CHECKLIST.md` in the conductor dir.

If the plan already contains `- [ ]` checklist items, those are used directly (no Claude call needed).

Each loop session receives **both** the original plan (full context) and the task checklist (progress tracking).

### How the loop works

1. On init: Claude decomposes the plan into atomic tasks (or uses existing checklist)
2. Builds prompt = task progress checklist + current task details + original plan
2. Runs Claude in a tmux window
3. Claude works until it outputs `<task-completed/>`, `<task-failed/>`, or context exhausts
4. On `<task-completed/>` → quality gate → commit → push → advance to next task
5. On context exhaustion → commit partial WIP → restart same task with fresh context
6. On `<task-failed/>` (3 attempts) → skip task, move on
7. State is persisted in `LOOP-STATE.json` so loops survive interruptions

### Key flags

- `--no-worktree`: work directly in the project dir instead of creating a worktree
- `--model`: override the Claude model (default: from preset or opus)
- `--reset`: discard previous loop state and start fresh
- `--preset`: quality gate preset (auto-detected if omitted)

## Project Structure

- `src/conductor/` — main conductor package (CLI, orchestrator, models)
  - `src/conductor/prompts/speccer/` — speccer prompt templates (spec + generate, per mode)
  - `src/conductor/prompts/` — brain/diagnosis prompt templates
- `src/speccer/` — spec generation loop
- `src/runner/` — code execution runner
  - `src/runner/steer_inbox.py` — file-based steering IPC (polls `.msg` files from orchestrator)
- `conductor`, `speccer`, `runner` — bash wrapper scripts (auto-bootstrap venv)

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
