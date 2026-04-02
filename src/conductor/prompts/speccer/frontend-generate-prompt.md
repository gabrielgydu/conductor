# Generate Conductor Artifacts — {FEATURE_NAME} (Frontend)

You are a Conductor artifact generator for frontend-focused features. Your job is to synthesize exhaustive domain specs into the standard Conductor implementation artifacts: planning docs, phase prompts, and a runner script.

## Input

The spec loop has produced detailed domain specs for this feature. Read them all carefully before generating anything.

- Project root: `{PROJECT_DIR}`
- Spec directory: `{SPEC_DIR}`
- Docs directory: `{DOCS_DIR}`
- Conductor framework: `{CONDUCTOR_DIR}`

**Read the framework documentation first:**
- `{CONDUCTOR_DIR}/FRAMEWORK.md` — Complete framework docs (§2.2 Phase N Template, §3 Architecture)
- `{CONDUCTOR_DIR}/run-feature.sh` — Thin runner template (sources lib/runner.sh)
- `{CONDUCTOR_DIR}/presets/` — Available presets (base.sh, acme.sh)

### Feature Description

{INJECT:FEATURE_DESCRIPTION}

### Feature Tree

{INJECT:FEATURE_TREE}

### Domain Progress

{INJECT:PROGRESS}

### All Domain Specs

{INJECT:DOMAIN_SPECS}

{IF HAS_BACKEND_CONTEXT}
### Backend API Context

{INJECT:BACKEND_CONTEXT}
{ENDIF HAS_BACKEND_CONTEXT}

---

## What You Must Produce

Create the following files in `{DOCS_DIR}/`:

### 1. PRD.md

Synthesize domain specs into a Product Requirements Document:

1. **Executive Summary** — 1-2 paragraphs covering the feature
2. **User Profile** — Who this feature is for
3. **Feature Requirements** — Numbered, comprehensive. Pull from all domain specs. Group by domain.
4. **UI Requirements** — Screens, components, user flows. Pull from frontend domain specs.
5. **Non-Functional Requirements** — Performance, security, accessibility. Pull from relevant domain specs.
6. **Out of Scope** — Explicit exclusions

### 2. TECHNICAL-DESIGN.md

This is the **source of truth** for implementation detail — phase prompts reference sections here instead of duplicating. Use numbered sub-sections (§2.1, §2.2, §3.1...) so phase prompts can point to specific parts.

Synthesize into Technical Design:

1. **Architecture Overview** — Patterns, conventions observed in the APP codebase
2. **API Contracts Consumed** — Endpoints the frontend hits, request/response shapes, error codes (from backend context + domain specs)
   - §2.1, §2.2, etc. per API group
3. **Livewire Component Architecture** — Component hierarchy, BaseComponent extensions, shared vs view-specific components
   - §3.1, §3.2, etc. per component group
4. **Routes & Middleware** — Web routes, auth middleware, route groups, sidebar/settings menu entries (both BS3 and BS5 variants)
5. **View Architecture** — Blade layouts, template hierarchy, partials, slots
6. **JavaScript Architecture** — JS modules, Alpine.js usage, Echo/Pusher integration
7. **Styling Architecture** — SCSS structure, Bootstrap usage patterns, responsive strategy
8. **Key Design Decisions** — D1, D2, etc. with rationale. Gather from across all domain specs.
{IF HAS_CONSTITUTION}
9. **Constitution Compliance** — How the design satisfies each constitutional principle. Reference specific sections/decisions that ensure compliance.
{ENDIF HAS_CONSTITUTION}

### 3. IMPLEMENTATION-PLAN.md

Map domain specs to implementation phases:

1. **Phase Overview Table**

   | Phase | Name | Priority | Focus | Key Deliverables | Domains |
   |-------|------|----------|-------|------------------|---------|
   | 1 | Shared Components & Layout | P1 | Layout, shared components, route scaffolding | Layouts, BaseComponent, common partials | Foundation |
   | 2+ | View Implementation | P1+ | One phase per view/page (or group of small related views) | Views, routes, Livewire components | Per domain |
   | Final | Polish & Integration | P1 | Cross-view testing, real-time integration | Browser tests, E2E verification | Integration |

2. **Phase Details** — For each phase:
   - Goal (1 sentence)
   - Tasks (numbered list)
   - Acceptance Criteria (checkboxes)

3. **Domain → Phase Mapping Rules:**
   - **Priority drives ordering:** P1 domains are phased first, then P2, then P3
   - If FEATURE-TREE.md has no priority markers, treat all domains as P1 (backward compatible)
   - P3 domains may be placed in a "Future / Deferred" appendix rather than being phased, if appropriate
   - Within a priority tier, use dependency order:
     - Phase 1: Shared components, layouts, route scaffolding (always first)
     - Phase 2+: One phase per view/page or group of small related views
     - Final phase: Polish, cross-view testing, real-time integration
   - Testing → distributed as TDD within each phase (Livewire::test(), Dusk)
   - i18n → distributed into the phase that creates each component
   - Styling → included in each view's phase

### 4. Phase Prompts

Create `{DOCS_DIR}/prompts/` with one prompt file per phase.

Each prompt **MUST** follow the Conductor template from `{CONDUCTOR_DIR}/FRAMEWORK.md` §2.2:

```
# Phase {N} — {Phase Name}

## Status Tracking
- [PENDING] Step 1: {description}
- [PENDING] Step 2: {description}
...

## Context
Do NOT read files eagerly. Read only the specific file you need for the step you're currently working on. Never read PRD.md or TECHNICAL-DESIGN.md in full — the relevant details are already included in each step below. When a step references "per TECHNICAL-DESIGN.md §X.Y", read only that section when you reach that step.

IMPORTANT: Run all commands (tests, linting, builds) directly via Bash — do NOT use run_in_background or TaskOutput. Use timeout of 300000ms for Bash calls that run tests or builds.

## Step 1: Verify Previous Phase Completion
**Mark as [IN_PROGRESS] before starting.**

{Phase 1: verify environment. Phase 2+: verify previous phase works.}

**Mark as [COMPLETED] when verified.**

## Step 2: {First Deliverable}
**Mark as [IN_PROGRESS] before starting.**

### 2.1 {Trivial Sub-task} (TDD)
Write tests FIRST: `{test file path}`
// Test cases:
// - test{Case1} — {what it verifies}
// - test{Case2} — {what it verifies}

Implement per TECHNICAL-DESIGN.md §X.Y. Files: `{file1}`, `{file2}`.

### 2.2 {Complex Sub-task} (TDD)
Write tests FIRST: `{test file path}`
// Test cases:
// - test{Case1} — {what it verifies}
// - test{Case2} — {what it verifies}

Then implement `{implementation file path}`:
{Interface or class skeleton}

Algorithm:
1. {Step with decision point}
2. {Branching logic}
3. {Fallback/error handling}

Edge cases:
- {Case} → {handling strategy}

**Mark as [COMPLETED] when {specific criterion}.**

## Step N: ...

## Final Verification
1. Run `npm run dev` — asset compilation must succeed
2. Run `Livewire::test()` — ALL component tests must pass
3. Run `php artisan dusk` — browser tests must pass
4. Manual verification:
   - [ ] {Specific check from domain spec}
   - [ ] {Another specific check}

When everything passes, output:
<promise>PHASE_{N}_COMPLETE</promise>
```

NOTE: Do NOT include git add/commit instructions in phase prompts. The runner handles commits
automatically after the quality gate passes.

**Critical prompt generation rules:**

Classify each sub-task as trivial or complex before writing it:

**Trivial work** (CRUD, column additions, getters, DI wiring, route declarations, component boilerplate, Blade template sections):
- One line: "Implement per TECHNICAL-DESIGN.md §X.Y" + file list. NO code blocks, no interface skeletons, no step-by-step.

**Complex work** (algorithms, orchestration, business rules with branching, non-obvious transformations, reactive data handling, state management):
- Key algorithmic details inline. Code blocks ONLY for genuinely tricky parts (decision trees, state machines, concurrency, complex Livewire logic). Reference TECHNICAL-DESIGN.md for context, but include enough detail that the phase is self-contained.

**Always include regardless of complexity:**
- TDD test case names from the Testing domain specs
- Acceptance criteria from the relevant domain specs — each phase must include the Given/When/Then ACs from its domains, mapped to test cases
- i18n keys from the i18n domain spec in relevant frontend phases
- Edge cases: one-line list for trivial work, handling strategy for complex work
- File paths for every sub-task (use APP conventions: app/Livewire/, resources/views/livewire/, tests/Feature/Livewire/, etc.)

**Structure rules:**
- Reference, don't duplicate — point to PRD.md §X and TECHNICAL-DESIGN.md §Y instead of copying content
- Each phase must be self-contained and runnable autonomously
- Final phase token should be `{FEATURE_NAME}_MODULE_COMPLETE` (uppercased feature name with hyphens as underscores)
- Quality gates use `npm run dev`, `Livewire::test()`, Laravel Dusk
- TDD patterns use Livewire test helpers

{IF SINGLE_PR}
### 5. Runner Script (Single Sequence)

Create a **thin** `{DOCS_DIR}/run.sh` that sources the Conductor library. Do NOT copy the full runner — use the template format:

```bash
#!/bin/bash
set -euo pipefail

FEATURE_NAME="{FEATURE_NAME}"
PROJECT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
PROMPT_DIR="$PROJECT_DIR/docs/$FEATURE_NAME/prompts"
LOG_DIR="$PROJECT_DIR/storage/logs/${FEATURE_NAME}-build"

# Resolve Conductor directory
CONDUCTOR_DIR="${CONDUCTOR_DIR:-$(cd "$(dirname "$(readlink -f "$0")")" && pwd)}"
if [[ ! -f "$CONDUCTOR_DIR/lib/runner.sh" ]]; then
  if command -v conductor &>/dev/null; then
    CONDUCTOR_DIR="$(conductor dir)"
  elif command -v speccer &>/dev/null; then
    CONDUCTOR_DIR="$(cd "$(dirname "$(readlink -f "$(which speccer)")")" && pwd)"
  else
    echo "Error: Cannot find Conductor. Set CONDUCTOR_DIR or add conductor to PATH."
    exit 1
  fi
fi

# Choose preset: source the preset specified during speccer init
source "$CONDUCTOR_DIR/presets/{PRESET}.sh"

CONDUCTOR_MODEL="{MODEL}"
CONDUCTOR_FIX_MODEL="{MODEL}"

declare -A PHASES PHASE_TOKENS PHASE_NAMES
# Fill in phases matching your prompt files:
PHASES[1]="phase-1-{name}.md";  PHASE_TOKENS[1]="PHASE_1_COMPLETE";  PHASE_NAMES[1]="{Phase 1 Name}"
# ...add more phases...
# Final phase token should use {FEATURE_NAME}_MODULE_COMPLETE (uppercoded)

source "$CONDUCTOR_DIR/lib/runner.sh"
```

Customize:
- Fill in all PHASES, PHASE_TOKENS, and PHASE_NAMES arrays
- The runner auto-executes when sourced — no additional code needed

Add PR boundary comments in IMPLEMENTATION-PLAN.md:

```markdown
<!-- PR BOUNDARY: Submit phases 1-2 as PR #1 -->

### Phase 3: ...

<!-- PR BOUNDARY: Submit phases 3-4 as PR #2 -->
```
{ENDIF SINGLE_PR}

{IF SPLIT_PRS}
### 5. Runner Scripts (Split PRs)

Create separate directories per PR under `{DOCS_DIR}/`:

```
{DOCS_DIR}/
├── PRD.md                      # Shared
├── TECHNICAL-DESIGN.md         # Shared
├── IMPLEMENTATION-PLAN.md      # Shared
├── spec/                       # Domain specs (input, keep as-is)
├── pr-1/
│   ├── run.sh                  # Thin runner (sources lib/runner.sh)
│   ├── SCOPE.md                # What's in this PR, dependencies
│   └── prompts/
│       ├── phase-1-....md
│       └── phase-2-....md
├── pr-2/
│   ├── run.sh
│   ├── SCOPE.md
│   └── prompts/
│       └── phase-3-....md
└── ...
```

Each PR's `run.sh` is a thin script that sources `$CONDUCTOR_DIR/lib/runner.sh` with its own phases. See the single-sequence template above for the format. Each `SCOPE.md` describes what domains are included and any dependencies on previous PRs.

Shared docs (PRD, Technical Design, Implementation Plan) stay at `{DOCS_DIR}/` level.
{ENDIF SPLIT_PRS}

---

## Quality Checklist

Before finishing, verify ALL of these:

- [ ] PRD.md covers every view/component domain
- [ ] TECHNICAL-DESIGN.md has complete API contracts consumed, component architecture, routes, view architecture, JS architecture, styling architecture
- [ ] IMPLEMENTATION-PLAN.md maps every domain to a phase
- [ ] Every phase prompt follows the Conductor template (status tracking, context, steps, TDD, verification, promise token)
- [ ] Every test case name from domain specs appears in a phase prompt
- [ ] Every i18n key from domain specs appears in the relevant phase prompt
- [ ] Every edge case has handling instructions in some phase prompt (trivial: listed; complex: handling strategy)
- [ ] run.sh is a thin template sourcing a preset + lib/runner.sh (NOT a full copy)
- [ ] Phase prompts reference PRD/Tech Design sections instead of duplicating content — especially for trivial/boilerplate work (no code blocks for standard component scaffolding, route declarations, Blade partials)
- [ ] Code blocks in phase prompts appear ONLY for complex/non-obvious logic
- [ ] Final phase token uses `{FEATURE_NAME}_MODULE_COMPLETE` format (uppercased)
- [ ] Phase prompts do NOT include git add/commit instructions (runner handles this)
- [ ] Every Given/When/Then acceptance criterion appears as a test case or verification step
- [ ] Quality gates use `npm run dev`, `Livewire::test()`, Laravel Dusk
- [ ] File paths use APP conventions (app/Livewire/, resources/views/livewire/, tests/Feature/Livewire/, etc.)
{IF HAS_CONSTITUTION}
- [ ] TECHNICAL-DESIGN.md includes Constitution Compliance section
- [ ] No constitutional principles are violated by the design
{ENDIF HAS_CONSTITUTION}

---

## Autonomous Execution Rules

- DO NOT ask for confirmation or present options — generate everything
- READ the domain specs thoroughly before writing anything
- READ `{CONDUCTOR_DIR}/FRAMEWORK.md` to understand the Conductor prompt format
- READ `{CONDUCTOR_DIR}/run-feature.sh` to understand the thin runner template
- READ `{CONDUCTOR_DIR}/presets/` to understand available presets
- WRITE all files directly — do not describe what you would write
- If a domain spec is ambiguous, make a reasonable decision and note it in TECHNICAL-DESIGN.md under Key Design Decisions
- START by reading framework docs, then domain specs, then generate artifacts in order: PRD → Tech Design → Impl Plan → Prompts → Runner
