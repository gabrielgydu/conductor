# Conductor

Multi-run orchestrator for parallel AI-driven feature builds. Plans work, manages dependencies, spawns speccer and runner processes, handles failures, and produces audit reports.

## Quick Start

```bash
# Clone and run — venv is auto-created on first invocation
git clone git@github.com:gabrielgydu/conductor.git
cd conductor

# Initialize a project (from your target repo)
cd /path/to/your/project
/path/to/conductor/conductor init --name my-feature --project-dir .

# Fill in the feature brief
# (opens at ~/.conductor/projects/<key>/conductor/my-feature/FEATURE-BRIEF.md)

# Generate a plan (uses Claude Opus)
/path/to/conductor/conductor plan --name my-feature --project-dir .

# Execute the plan
/path/to/conductor/conductor run --name my-feature --project-dir .

# Or run overnight (auto-answers speccer questions)
/path/to/conductor/conductor run --name my-feature --project-dir . --overnight
```

No manual venv setup needed — the `./conductor` and `./speccer` wrapper scripts auto-create a venv and install dependencies on first run.

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
| `conductor plan --name <name>` | Generate run plan via Claude Opus |
| `conductor run --name <name> [--overnight]` | Execute the orchestration loop |
| `conductor status --name <name>` | Show run/stage status table |
| `conductor log --name <name> [--tail N]` | Tail the conductor log |
| `conductor cleanup --name <name>` | Remove worktrees and branches |

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
- `run.sh` — runner entry point
- `prompts/` — per-phase prompt files

### Runner

Phase execution engine. Drives Claude through implementation phases with quality gates.

**Commands:**

| Command | Description |
|---------|-------------|
| `runner run --feature <name> [--steerable] [--preset <name>]` | Execute phase loop |
| `runner fixer --feature <name> --branch <branch> --pr <num>` | Fix PR review comments |

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
- Merges all completed run branches in DAG topological order
- On conflict: tries `git merge -X theirs`, then Claude-assisted resolution
- Creates a non-draft PR on the integration branch

**Integration E2E Testing:**
- Detects test framework (Playwright, Cypress)
- Uses Claude to generate cross-feature E2E tests
- Runs tests and captures results (failures don't block processing)

## Storage

All metadata lives outside the target repo at `~/.conductor/projects/<project-key>/`:

```
~/.conductor/projects/<project-key>/
├── conductor/
│   └── <project-name>/
│       ├── CONDUCTOR-STATE.json      # DAG state, run/stage status
│       ├── CONDUCTOR-LOG.md          # Human-readable event log
│       ├── CONDUCTOR-AUDIT.jsonl     # Machine-readable audit trail
│       ├── STATS.json                # Token usage and cost tracking
│       ├── FEATURE-BRIEF.md          # Project description (user-written)
│       └── brain-calls/              # Preserved brain call responses
├── features/
│   └── <feature-name>/
│       ├── spec/                     # Speccer output (domains, progress)
│       ├── prompts/                  # Per-phase prompt files
│       ├── run.sh                    # Runner entry point
│       ├── activity.log              # Runner activity log (read by orchestrator)
│       ├── steer_inbox/              # Steering message queue (.msg files)
│       ├── LEARNINGS.md              # Phase learnings
│       └── STATS.json               # Per-feature cost tracking
└── logs/
    └── <feature>-build/              # Build logs per phase
```

The project key is derived from the repo root path: `/home/user/dev/repo` → `-home-user-dev-repo`. Worktrees resolve to the main repo's key via `git rev-parse --git-common-dir`.

## Presets

Presets customize quality gates, preflight checks, and teardown per project type.

| Preset | Quality Gate | Teardown | Push/Fixer |
|--------|-------------|----------|------------|
| `base` | No-op (always passes) | No-op | Disabled |
| `acme` | PHPStan via pre-commit hook | `docker compose down` | Enabled |
| `nodeapp` | tsc + eslint + pnpm test per package | No-op | Enabled |

Custom presets: subclass `Preset` in `src/conductor/core/presets.py`.

```python
class MyPreset(BasePreset):
    def quality_gate(self, cwd: Path) -> GateResult:
        result = subprocess.run(["make", "check"], cwd=cwd, capture_output=True)
        if result.returncode != 0:
            return GateResult(passed=False, failures=[result.stderr])
        return GateResult(passed=True)
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
# Set up dev environment
python -m venv .venv
.venv/bin/pip install -e ".[dev]"

# Run tests
.venv/bin/python -m pytest

# Run specific test
.venv/bin/python -m pytest tests/test_models.py -v
```

Tests: 238 passing across unit, integration, and E2E suites.
