# Generate Conductor Artifacts — {FEATURE_NAME} (Testing)

You are a Conductor artifact generator for test suite implementation. Your job is to synthesize exhaustive test domain specs into the standard Conductor implementation artifacts: planning docs, phase prompts, and a runner script.

## Input

The spec loop has produced detailed test domain specs for this feature. Read them all carefully before generating anything.

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

{IF HAS_SPEC_CONTEXT}
### Feature Spec Context

{INJECT:SPEC_CONTEXT}
{ENDIF HAS_SPEC_CONTEXT}

---

## What You Must Produce

Create the following files in `{DOCS_DIR}/`:

### 1. TEST-PLAN.md

Synthesize domain specs into a Test Plan:

1. **Test Strategy Overview** — Goals, coverage targets, test type rationale (why both PHPUnit and Playwright)
2. **Scope** — What's tested, what's explicitly out of scope
3. **Test Types** — PHPUnit/Livewire component tests (fast, isolated) and Playwright E2E browser tests (slow, integrated). What each type covers and why.
4. **Coverage Goals** — AC coverage matrix summary. P1 ACs must have both PHPUnit and Playwright coverage. P2 ACs need at least one type.
5. **Environment Requirements** — Docker setup, test data, CI integration, env vars

### 2. TEST-ARCHITECTURE.md

This is the **source of truth** for test implementation detail — phase prompts reference sections here instead of duplicating. Use numbered sub-sections (§1.1, §1.2, §2.1...) so phase prompts can point to specific parts.

Synthesize into Test Architecture:

1. **Playwright Setup** — Config (`playwright.config.ts`), project structure, browser settings, serial execution, timeouts
   - §1.1 Config file
   - §1.2 Directory structure
   - §1.3 Browser and timeout settings
2. **Helper Library** — auth.ts (`loginAsAirport`), livewire.ts (`waitForLivewireUpdate`, `setupConsoleMonitor`), feature-specific helpers
   - §2.1 Auth helpers
   - §2.2 Livewire helpers
   - §2.3 Feature-specific helpers
3. **PHPUnit Base Setup** — TestCase extensions, stubs (FakeUser, FakePartnerClient, FakeShopwareClient), traits
   - §3.1 Base test class
   - §3.2 Test stubs
   - §3.3 Shared traits
4. **Test Routes** — `routes/test.php` additions needed (login routes, data setup routes)
   - §4.1 Existing test routes
   - §4.2 New test routes needed
5. **Mock & Fake Patterns** — Http::fake(), mock response factories, Mockery patterns
   - §5.1 PartnerClient mocks
   - §5.2 ShopwareClient mocks
   - §5.3 ACDBApi mocks
   - §5.4 Response factories
6. **Test Data & Fixtures** — Seed data, factories, state reset patterns
   - §6.1 Database seeding
   - §6.2 Cache/state reset
   - §6.3 Test user setup
7. **Key Design Decisions** — D1, D2, etc. with rationale. Gather from across all domain specs.

{IF HAS_CONSTITUTION}
8. **Constitution Compliance** — How the test design satisfies each constitutional principle. Reference specific sections/decisions that ensure compliance.
{ENDIF HAS_CONSTITUTION}

### 3. IMPLEMENTATION-PLAN.md

Map domain specs to implementation phases:

1. **Phase Overview Table**

   | Phase | Name | Priority | Focus | Key Deliverables | Domains |
   |-------|------|----------|-------|------------------|---------|
   | 1 | Test Infrastructure | P1 | Playwright config, helpers, PHPUnit stubs, test routes | Config, helpers, stubs | Test Infra |
   | 2 | Livewire Tests — [View 1] | P1 | PHPUnit component tests (fast, isolated) | Test file, mocks | Per domain |
   | 3 | Livewire Tests — [View 2] | P1+ | PHPUnit component tests | Test file, mocks | Per domain |
   | ... | ... | ... | ... | ... | ... |
   | N | Playwright E2E Tests | P1 | Browser tests for all views (slow, integrated) | Spec files, plan files | All views |

2. **Phase Details** — For each phase:
   - Goal (1 sentence)
   - Tasks (numbered list)
   - Acceptance Criteria (checkboxes)

3. **Phase Ordering Rules:**
   - **Phase 1 always:** Test infrastructure (config, helpers, stubs, routes)
   - **PHPUnit phases next:** One phase per view's Livewire component tests (fast, isolated, no browser needed)
   - **Playwright phases after:** E2E browser tests (depend on components working correctly)
   - Priority drives ordering within each group (P1 before P2)
   - Testing → PHPUnit first because they're fast and catch component-level bugs early

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
Do NOT read files eagerly. Read only the specific file you need for the step you're currently working on. Never read TEST-PLAN.md or TEST-ARCHITECTURE.md in full — the relevant details are already included in each step below. When a step references "per TEST-ARCHITECTURE.md §X.Y", read only that section when you reach that step.

IMPORTANT: Run all test commands (playwright, phpunit, phpstan) directly via Bash — do NOT use run_in_background or TaskOutput. Tests complete in under 2 minutes. Use timeout of 300000ms for Bash calls.

## Step 1: Verify Previous Phase Completion
**Mark as [IN_PROGRESS] before starting.**

{Phase 1: verify test environment. Phase 2+: verify previous tests pass.}

**Mark as [COMPLETED] when verified.**

## Step 2: {First Test Group}
**Mark as [IN_PROGRESS] before starting.**

### 2.1 {Test file/group}
Create test file: `{test file path}`

Test cases:
- `test{Case1}` — {what it verifies} — {key assertions}
- `test{Case2}` — {what it verifies} — {key assertions}

Mock setup per TEST-ARCHITECTURE.md §X.Y.

### 2.2 {Complex test scenario}
{For complex tests, include:}
- Mock response chain
- Step-by-step interaction sequence
- Selector table for Playwright
- Timing/async strategy

**Mark as [COMPLETED] when {specific criterion}.**

## Step N: ...

## Final Verification
1. Run `php artisan test --filter={TestClass}` — 0 failures (PHPUnit phases)
   OR `npx playwright test tests/{file}.spec.ts` — 0 failures (Playwright phases)
2. Verify coverage:
   - [ ] {AC from domain spec} → {test case that covers it}
   - [ ] {Another AC} → {test case}

When everything passes, output:
<promise>PHASE_{N}_COMPLETE</promise>
```

NOTE: Do NOT include git add/commit instructions in phase prompts. The runner handles commits
automatically after the quality gate passes.

**Bug handling rules:**

When a test failure reveals an actual application bug (not a selector/timing/test issue), do NOT attempt to fix the application code in that phase. Instead:
1. Mark the test with `test.fixme('BUG: <description of the bug>')` so it is skipped but tracked
2. Log the bug in LEARNINGS.md with reproduction steps
3. Move on to the next test

The **final phase** must include a **Bug Fix step** at the end:
1. Search for all `test.fixme('BUG:` markers across all test files
2. For each one: fix the application bug, then remove the `test.fixme` wrapper so the test runs normally
3. Run the affected test files to confirm fixes work
4. Verify that zero `test.fixme('BUG:` markers remain

**Critical prompt generation rules:**

Classify each test as trivial or complex before writing it:

**Trivial tests** (simple renders, basic navigation, standard CRUD assertions):
- One line: test case name + description + key assertion. Reference TEST-ARCHITECTURE.md for mock setup.

**Complex tests** (multi-step workflows, async operations, modal interactions, error cascades):
- Full test scenario inline: mock setup, interaction sequence, selectors, timing strategy, assertions.

**Always include regardless of complexity:**
- Test case names from the domain specs
- Mock/stub requirements from the domain specs
- Selectors table for Playwright tests (from domain spec §7)
- Async timing strategy for tests involving Livewire updates or PARTNER polling
- Coverage mapping: which ACs each test covers

**Structure rules:**
- Reference, don't duplicate — point to TEST-PLAN.md §X and TEST-ARCHITECTURE.md §Y instead of copying
- Each phase must be self-contained and runnable autonomously
- Final phase token should be `{FEATURE_NAME}_MODULE_COMPLETE` (uppercased feature name with hyphens as underscores)
- PHPUnit quality gates: `php artisan test --filter={TestClass}` — 0 failures
- Playwright quality gates: `npx playwright test tests/{file}.spec.ts` — 0 failures
- Final phase (Playwright E2E) quality gate: full `php artisan test` + `npx playwright test`

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

TESTING_MODE=true

CONDUCTOR_MODEL="{MODEL}"
CONDUCTOR_FIX_MODEL="{MODEL}"

declare -A PHASES PHASE_TOKENS PHASE_NAMES
# Fill in phases matching your prompt files:
PHASES[1]="phase-1-{name}.md";  PHASE_TOKENS[1]="PHASE_1_COMPLETE";  PHASE_NAMES[1]="{Phase 1 Name}"
# ...add more phases...
# Final phase token should use {FEATURE_NAME}_MODULE_COMPLETE (uppercased)

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
├── TEST-PLAN.md                # Shared
├── TEST-ARCHITECTURE.md        # Shared
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

Shared docs (TEST-PLAN, TEST-ARCHITECTURE, IMPLEMENTATION-PLAN) stay at `{DOCS_DIR}/` level.
{ENDIF SPLIT_PRS}

---

## Quality Checklist

Before finishing, verify ALL of these:

- [ ] TEST-PLAN.md covers every test domain with strategy and scope
- [ ] TEST-ARCHITECTURE.md has complete Playwright setup, helper library, PHPUnit stubs, mock patterns, test data strategy
- [ ] IMPLEMENTATION-PLAN.md maps every domain to a phase
- [ ] Phase ordering: infrastructure → PHPUnit phases → Playwright E2E phase (final)
- [ ] Every phase prompt follows the Conductor template (status tracking, context, steps, verification, promise token)
- [ ] Every test case name from domain specs appears in a phase prompt
- [ ] Every mock/stub definition from domain specs is referenced in a phase prompt
- [ ] Every selector from domain spec §7 appears in the relevant Playwright phase prompt
- [ ] run.sh is a thin template sourcing a preset + lib/runner.sh (NOT a full copy)
- [ ] Phase prompts reference TEST-PLAN/TEST-ARCHITECTURE sections instead of duplicating content
- [ ] Code blocks in phase prompts appear ONLY for complex test scenarios
- [ ] Final phase token uses `{FEATURE_NAME}_MODULE_COMPLETE` format (uppercased)
- [ ] Phase prompts do NOT include git add/commit instructions (runner handles this)
- [ ] Coverage matrix: every P1 AC has both PHPUnit and Playwright test cases
- [ ] PHPUnit quality gates: `php artisan test --filter={TestClass}`
- [ ] Playwright quality gates: `npx playwright test tests/{file}.spec.ts`
- [ ] Final phase quality gate runs full `php artisan test` + `npx playwright test`
{IF HAS_CONSTITUTION}
- [ ] TEST-ARCHITECTURE.md includes Constitution Compliance section
- [ ] No constitutional principles are violated by the test design
{ENDIF HAS_CONSTITUTION}

---

## Autonomous Execution Rules

- DO NOT ask for confirmation or present options — generate everything
- READ the domain specs thoroughly before writing anything
- READ `{CONDUCTOR_DIR}/FRAMEWORK.md` to understand the Conductor prompt format
- READ `{CONDUCTOR_DIR}/run-feature.sh` to understand the thin runner template
- READ `{CONDUCTOR_DIR}/presets/` to understand available presets
- WRITE all files directly — do not describe what you would write
- If a domain spec is ambiguous, make a reasonable decision and note it in TEST-ARCHITECTURE.md under Key Design Decisions
- START by reading framework docs, then domain specs, then generate artifacts in order: TEST-PLAN → TEST-ARCHITECTURE → IMPLEMENTATION-PLAN → Prompts → Runner
