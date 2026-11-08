# Conductor

Multi-run orchestrator for parallel AI-driven feature builds. Plans work, manages dependencies, spawns speccer and runner processes, handles failures, and produces audit reports.

## Quick Start

```bash
# Clone and run — venv is auto-created on first invocation
git clone https://github.com/gabrielgydu/conductor.git
cd conductor

# Initialize a project (from your target repo)
cd /path/to/your/project
/path/to/conductor/conductor init --name my-feature --project-dir .

# Fill in the feature brief
# (opens at ~/.conductor/projects/<key>/conductor/my-feature/FEATURE-BRIEF.md)

# Generate a plan (uses Claude Opus)
/path/to/conductor/conductor plan --name my-feature --project-dir .

# Execute the plan (overnight mode: speccer questions are auto-answered)
/path/to/conductor/conductor run --name my-feature --project-dir .

# Or answer speccer questions yourself
/path/to/conductor/conductor run --name my-feature --project-dir . --no-overnight
```

No manual venv setup needed: the `./conductor`, `./speccer` and `./runner` wrappers create `.venv` and install conductor into it on first run.

They also repair it: a failed or interrupted install is retried on the next call, a changed `pyproject.toml` is reinstalled, and the venv is rebuilt when a Python upgrade breaks it.

The venv is built with the first `python3` or `python` that is at least 3.11; set `CONDUCTOR_PYTHON` to choose another.

## Requirements

- Python >= 3.11
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-cli) installed and authenticated
- Git
- tmux (for conductor run)

## Architecture

```
conductor (orchestrator)
  │
  ├─ plan    → Claude Opus generates a DAG of runs with stages
  ├─ run     → main event loop, advances runs through state machine
  │   │
  │   ├─ speccer (per stage)
  │   │   ├─ init      → create spec directory
  │   │   ├─ run       → Claude explores codebase, writes domain specs
  │   │   └─ generate  → Claude produces run.sh, PRD, tech design, prompts
  │   │
  │   ├─ runner (per stage)
  │   │   ├─ phase loop   → iterate phases with quality gates
  │   │   ├─ steerable    → interactive Claude sessions via subprocess.PIPE
  │   │   └─ fixer        → auto-fix PR review comments
  │   │
  │   └─ brain (diagnosis)
  │       ├─ diagnose-stall   → analyze stuck runners
  │       ├─ diagnose-failure → determine retry vs block
  │       └─ answer-questions → auto-answer speccer questions (overnight)
  │
  ├─ post-run
  │   ├─ learnings review → update CLAUDE.md
  │   ├─ integration merge → merge all branches, AI conflict resolution
  │   ├─ integration E2E   → generate cross-feature tests
  │   └─ audit report     → overnight summary
  │
  └─ status/log/cleanup
```

## Components

### Conductor

The top-level orchestrator. Manages a DAG of runs, each with one or more stages.

**Commands:**

| Command | Description |
|---------|-------------|
| `conductor init --name <name>` | Create a project with feature brief template |
| `conductor plan --name <name> [--force]` | Generate run plan via Claude Opus (`--force` re-plans a project whose runs have started) |
| `conductor run --name <name> [--no-overnight] [--quick\|--no-quick] [--max-parallel N] [--effort LEVEL] [--reintegrate]` | Execute the orchestration loop (`--reintegrate` attempts the integration again even when it is not due; `--effort` sets the effort of every Claude session, default the preset's `effort`, else `xhigh`) |
| `conductor status --name <name>` | Show run/stage status table, the integration PR and how a failed integration is retried |
| `conductor log --name <name> [--tail N]` | Tail the conductor log |
| `conductor cleanup --name <name> [--force]` | Remove worktrees and branches; keeps uncommitted, unpushed or unmerged work unless `--force`; branch names stay in state, so later runs still find work saved on origin |

**Stage State Machine:**

```
pending → spec_init → spec_running → spec_needs_input → spec_complete
  → generated → executing → done
  (failures → failed → retry or → blocked)
```

**DAG Execution:** Runs declare dependencies via `depends_on`. A run only starts when all its dependencies are done. Independent runs execute in parallel.

### Speccer

Iterative specification generator. Drives Claude through codebase exploration, question-asking, and domain decomposition before any code is written.

**Commands:**

| Command | Description |
|---------|-------------|
| `speccer init --feature <name> --spec-dir <path>` | Create spec directory structure |
| `speccer run --spec-dir <path>` | Run spec loop (explore → ask questions → spec) |
| `speccer run --continue --spec-dir <path>` | Resume after answering questions |
| `speccer generate --spec-dir <path>` | Generate implementation artifacts |
| `speccer status --spec-dir <path>` | Show current spec status |
| `speccer tree --spec-dir <path>` | Display feature tree |

**State Machine:**

```
INIT → EXPLORING → NEEDS_INPUT ⇄ SPECCING → COMPLETE → GENERATED
```

**Promise Tokens:** Speccer detects `<promise>SPEC_COMPLETE</promise>` and `<promise>SPEC_NEEDS_INPUT</promise>` in Claude's output to drive state transitions.

**Generated Artifacts:**
- `PRD.md` — product requirements
- `TECHNICAL-DESIGN.md` — architecture and design
- `API-CONTRACTS.md` — interface definitions
- `IMPLEMENTATION-PLAN.md` — phased implementation plan
- `run.sh` — phase manifest (parsed by conductor for its `PHASES`/`PHASE_TOKENS`/`PHASE_NAMES` arrays; the Python runner is launched directly, not by executing this file)
- `prompts/` — per-phase prompt files

### Runner

Phase execution engine. Drives Claude through implementation phases with quality gates.

**Commands:**

| Command | Description |
|---------|-------------|
| `runner run --feature <name> [--steerable] [--preset <name>]` | Execute phase loop |
| `runner fixer --feature <name> --branch <branch> --pr <num> --phase <n> [--project-dir <path>] [--base-branch <branch>]` | Fix PR review comments |

**Phase Loop:**
1. Build prompt from phase file
2. Invoke Claude (steerable or one-shot)
3. Detect promise token for phase completion
4. Run quality gate (from preset)
5. If gate fails: feed errors back to Claude, retry
6. If gate passes: commit, optionally push
7. Repeat for each phase

**Steerable Sessions:** Uses `subprocess.PIPE` for bidirectional communication with Claude CLI — no named FIFOs, no fd inheritance issues. Completion detected via idle timeout after `end_turn`, not by polling for a result event.

**Steering Queue:** The orchestrator steers running sessions via file-based IPC — writes atomic `.msg` files to `{feature_dir}/steer_inbox/`, which the runner polls every 2s and forwards to the active session. No external CLI dependencies needed.

### Integration

Post-run features for merging and testing across runs.

**Integration Merge:**
- One finished run gets a PR from its own branch; several are merged into `integration/<name>` in DAG topological order
- The integration worktree is `<base>/integration-<name>`, where `<base>` is `--worktrees-base`, else the preset's `worktrees_base`, else `<project>/../worktrees`
- Incremental and never destructive: an attempt starts from the existing integration branch (the newer of the local one and origin's), merges only the runs it does not contain yet, and moves and pushes the branch only by fast-forward, so commits pushed to the PR branch and the PR itself stay
- A run that finishes in a later session is merged on top of the branch
- A run branch that is gone locally is taken from origin, else from the commit recorded when the run finished
- On conflict: Claude resolves it first; `git merge -X theirs` is a last resort that is recorded as unresolved and makes the merge PARTIAL; a run neither can merge is left out
- The learnings from all runs are committed to CLAUDE.md once, on the integration branch, before the push
- DONE means validated, pushed and with a PR; the PR is a draft when the merge is partial
- A failed push or PR is retried as push and PR only, and a failed setup (e.g. the integration branch checked out somewhere) is retried as a whole, each in up to 3 sessions
- Anything else, such as diverged local and origin integration branches, waits for `--reintegrate`

**Integration E2E Testing:**
- Detects test framework (Playwright, Cypress)
- Uses Claude to generate cross-feature E2E tests
- Runs tests and captures results (failures don't block processing)

## Storage

Project state lives outside the target repo at `~/.conductor/projects/<project-key>/conductor/<project-name>/`:

```
~/.conductor/projects/<project-key>/conductor/<project-name>/
├── CONDUCTOR-STATE.json      # DAG state, run/stage status
├── conductor.lock            # Single-instance lock (run/go/plan/learnings/loop/manual-test/cleanup)
├── CONDUCTOR-LOG.md          # Human-readable event log
├── CONDUCTOR-AUDIT.jsonl     # Machine-readable audit trail
├── STATS.json                # Token usage and cost tracking
├── FEATURE-BRIEF.md          # Project description (user-written)
├── brain-calls/              # Preserved brain call responses
├── ipc/                      # Exit and fail files written by the tmux window wrappers
└── logs/                     # tmux pane logs (orchestrator and runner windows)
```

The per-feature files are not stored there.

Each stage works in its own git worktree, and the speccer and runner keep their files in `<worktree>/docs/<feature>/`: `spec/`, `prompts/`, `run.sh`, `RUN-CONFIG.json`, `activity.log`, `steer_inbox/`, `LEARNINGS.md` and `STATS.json`.

The project key is derived from the repo root path: `/srv/dev/repo` → `-srv-dev-repo`. Worktrees resolve to the main repo's key via `git rev-parse --git-common-dir`.

Set `CONDUCTOR_HOME` to relocate the whole `~/.conductor` tree (project state and user-level presets); the test suite points it at a temp dir.

## Presets

Presets customize quality gates, preflight checks, teardown, post-run validation, and manual-test policy per project. They are TOML files that live outside this repository, so project-specific tooling never has to be committed here.

| Location | Purpose |
|----------|---------|
| `<project>/.conductor/preset.toml` | Project-local preset, auto-detected when present |
| `~/.conductor/presets/<name>.toml` | User-level presets, selected with `--preset <name>` or auto-detected via `[detect] paths` |
| `--preset /path/to/file.toml` | Explicit preset file |

`CONDUCTOR_PRESETS_DIR` overrides the user-level directory (default `$CONDUCTOR_HOME/presets`, i.e. `~/.conductor/presets`). Without any preset file the built-in `base` preset is used: no-op quality gate, no teardown, generated smoke tests.

See [`examples/preset.example.toml`](examples/preset.example.toml) for the full schema. A minimal preset:

```toml
name = "myproject"

[detect]
paths = ["backend", "frontend"]

[config]
fixer_enabled = true
local_ci_enabled = true
local_ci_command = "make ci"

[[quality_gate.checks]]
name = "lint"
command = "make lint"

[teardown]
stage = ["docker compose down"]
```

## State Models

All state is managed via Pydantic models with enum status fields. No jq, no string matching, no shell variable substitution.

Key models:
- `ConductorState` — project-level: runs, integration, base branch
- `RunState` — per-run: stages, dependencies, constitution, monitor
- `StageState` — per-stage: status, worktree, branch, pid
- `IntegrationState` — merge status, conflicts, E2E results
- `MonitorState` — stall count, progress hash, retry count

State mutations are atomic: write to temp file, then `os.rename`.

## Design Decisions

Conductor is fully standalone — no external runtime dependencies beyond Python, Claude CLI, git, and tmux. Prompt templates, steering, and activity log detection are all self-contained.

This is a Python rewrite of the original bash implementation. Key improvements:

| Problem (bash) | Solution (Python) |
|----------------|-------------------|
| FIFO deadlocks (5 separate bugs) | `subprocess.PIPE` — no named FIFOs |
| Dead process detection via exit files | `process.poll()` as primary liveness check |
| Infinite stall loops (31 consecutive brain calls) | Hard cap at 5 stall checks |
| String-based state matching | Pydantic models with enum fields |
| jq + sed + mv for state mutation | Pydantic serialization with atomic writes |
| fd 7 inheritance causing hangs | `close_fds=True` on all subprocesses |
| Background SIGTSTP freezes | `stdin=DEVNULL` for background processes |
| Variable substitution bugs in jq filters | Python f-strings and `json.dumps()` |

## Development

```bash
# Set up dev environment: the first call creates .venv and installs
# conductor with the dev extras (see scripts/venv-run)
./conductor --help

# Run tests
.venv/bin/python -m pytest

# Run specific test
.venv/bin/python -m pytest tests/test_models.py -v
```

Tests are split into three suites: unit tests directly under `tests/`, integration tests under `tests/integration/`, and end-to-end tests under `tests/e2e/`.

## Contributing

Fork the repo, make your change on a branch, and open a pull request.

Run the test suite (see [Development](#development) above) before submitting, and keep new code covered by tests.

## License

MIT, see [LICENSE](LICENSE).
