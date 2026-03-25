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
  "/home/user/development/conductor/conductor run --inside-tmux --name <project> --project-dir <path> --overnight" Enter

# 3. Monitor
tmux capture-pane -t conductor-<project>:conductor -p 2>&1 | tail -20
tmux list-windows -t conductor-<project> 2>&1
conductor status --name <project> --project-dir <path>
```

Key flags:
- `--inside-tmux`: run the loop directly without creating a new tmux session
- `--overnight`: auto-answer speccer questions via brain (no human input needed)

## Loop Mode

`conductor loop` is a persistent prompt loop for fix/improvement tasks on existing branches. Unlike `conductor run` (which goes through speccer → runner → integration), loop mode takes a plan file with a task checklist and runs Claude repeatedly until all tasks are done.

### Usage

```bash
# Start a new loop
conductor loop \
  --name app-tests-fix \
  --project-dir ~/acme-worktrees/app-tests-integration \
  --plan ./fix-plan.md \
  --preset acme

# Check progress
conductor loop-status \
  --name app-tests-fix \
  --project-dir ~/acme-worktrees/app-tests-integration

# Resume after interruption (no --plan needed)
conductor loop \
  --name app-tests-fix \
  --project-dir ~/acme-worktrees/app-tests-integration
```

### Running from Claude Code (no TTY)

```bash
tmux new-session -d -s conductor-loop-<name> -n loop

tmux send-keys -t conductor-loop-<name>:loop \
  "/home/user/development/conductor/conductor loop --inside-tmux --name <name> --project-dir <path> --plan <plan.md>" Enter

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
- `src/speccer/` — spec generation loop
- `src/runner/` — code execution runner
- `conductor`, `speccer`, `runner` — bash wrapper scripts (auto-bootstrap venv)

## Symlinks

`~/.local/bin/conductor`, `speccer`, `runner` all symlink to the wrapper scripts in this repo.
