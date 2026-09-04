# Generate Conductor Artifacts — {FEATURE_NAME} (Testing)

You are a Conductor artifact generator for test suite implementation. Your job is to synthesize exhaustive test domain specs into the standard Conductor implementation artifacts: planning docs, phase prompts, and a runner script.

## Input

The spec loop has produced detailed test domain specs for this feature. Read them all carefully before generating anything.

- Project root: `{PROJECT_DIR}`
- Spec directory: `{SPEC_DIR}`
- Docs directory: `{DOCS_DIR}`

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

1. **Test Strategy Overview** — Goals, coverage targets, test type rationale (why both component/unit and end-to-end tests)
2. **Scope** — What's tested, what's explicitly out of scope
3. **Test Types** — Component/unit tests (fast, isolated) and end-to-end browser tests (slow, integrated). What each type covers and why.
4. **Coverage Goals** — AC coverage matrix summary. P1 ACs must have both component/unit and end-to-end coverage. P2 ACs need at least one type.
5. **Environment Requirements** — Local test environment setup, test data, CI integration, env vars

### 2. TEST-ARCHITECTURE.md

This is the **source of truth** for test implementation detail — phase prompts reference sections here instead of duplicating. Use numbered sub-sections (§1.1, §1.2, §2.1...) so phase prompts can point to specific parts.

Synthesize into Test Architecture:

1. **End-to-End Setup** — Config, project structure, browser settings, serial execution, timeouts
   - §1.1 Config file
   - §1.2 Directory structure
   - §1.3 Browser and timeout settings
2. **Helper Library** — auth helper, framework-specific wait-for-update/console-monitor helpers, feature-specific helpers
   - §2.1 Auth helpers
   - §2.2 Framework-specific helpers
   - §2.3 Feature-specific helpers
3. **Unit Test Base Setup** — base test case extensions, stubs/fakes (auth, API clients), traits
   - §3.1 Base test class
   - §3.2 Test stubs
   - §3.3 Shared traits
4. **Test Routes** — test-only route additions needed (login routes, data setup routes)
   - §4.1 Existing test routes
   - §4.2 New test routes needed
5. **Mock & Fake Patterns** — HTTP mocking, mock response factories, test double patterns
   - §5.1 Primary API client mocks
   - §5.2 Secondary API client mocks
   - §5.3 External service mocks
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
   | 1 | Test Infrastructure | P1 | E2E config, helpers, unit test stubs, test routes | Config, helpers, stubs | Test Infra |
   | 2 | Component Tests — [View 1] | P1 | Component/unit tests (fast, isolated) | Test file, mocks | Per domain |
   | 3 | Component Tests — [View 2] | P1+ | Component/unit tests | Test file, mocks | Per domain |
   | ... | ... | ... | ... | ... | ... |
   | N | End-to-End Tests | P1 | Browser tests for all views (slow, integrated) | Spec files, plan files | All views |

2. **Phase Details** — For each phase:
   - Goal (1 sentence)
   - Tasks (numbered list)
   - Acceptance Criteria (checkboxes)

3. **Phase Ordering Rules:**
   - **Phase 1 always:** Test infrastructure (config, helpers, stubs, routes)
   - **Component/unit test phases next:** One phase per view's component tests (fast, isolated, no browser needed)
   - **End-to-end phases after:** browser tests (depend on components working correctly)
   - Priority drives ordering within each group (P1 before P2)
   - Testing → component/unit tests first because they're fast and catch component-level bugs early

### 4. Phase Prompts

Create `{DOCS_DIR}/prompts/` with one prompt file per phase.

Each prompt **MUST** follow this template:

```
# Phase {N} — {Phase Name}

## Status Tracking
- [PENDING] Step 1: {description}
- [PENDING] Step 2: {description}
...

## Context
Do NOT read files eagerly. Read only the specific file you need for the step you're currently working on. Never read TEST-PLAN.md or TEST-ARCHITECTURE.md in full — the relevant details are already included in each step below. When a step references "per TEST-ARCHITECTURE.md §X.Y", read only that section when you reach that step.

IMPORTANT: Run all test commands (the project's test runner, linter, static analyzer, and E2E framework) directly via Bash — do NOT use run_in_background or TaskOutput. Tests complete in under 2 minutes. Use timeout of 300000ms for Bash calls.

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
- Selector table for end-to-end tests
- Timing/async strategy

**Mark as [COMPLETED] when {specific criterion}.**

## Step N: ...

## Final Verification
1. Run the project's test command scoped to the specific test file/class — 0 failures (component/unit test phases)
   OR the project's E2E test command scoped to the specific spec file — 0 failures (end-to-end phases)
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
- Selectors table for end-to-end tests (from domain spec §7)
- Async timing strategy for tests involving reactive updates or polling
- Coverage mapping: which ACs each test covers

**Structure rules:**
- Reference, don't duplicate — point to TEST-PLAN.md §X and TEST-ARCHITECTURE.md §Y instead of copying
- Each phase must be self-contained and runnable autonomously
- Final phase token should be `{FEATURE_NAME}_MODULE_COMPLETE` (uppercased feature name with hyphens as underscores)
- Component/unit test quality gates: the project's test command scoped to the changed test file/class — 0 failures
- End-to-end quality gates: the project's E2E test command scoped to the changed spec file — 0 failures
- Final phase (end-to-end) quality gate: the project's full test suite + full E2E suite (see CONSTITUTION.md or the repo's CI config for exact commands)

{IF SINGLE_PR}
### 5. Runner Script (Single Sequence)

Create `{DOCS_DIR}/run.sh` as a phase manifest. Conductor parses it to build the runner config — this file is not executed directly. Use this format:

```bash
#!/bin/bash
# Phase manifest for {FEATURE_NAME}.
# Conductor parses the arrays below to build the runner config; this file is not executed directly.

FEATURE_NAME="{FEATURE_NAME}"
CONDUCTOR_MODEL="{MODEL}"
CONDUCTOR_FIX_MODEL="{MODEL}"

declare -A PHASES PHASE_TOKENS PHASE_NAMES
# One entry per prompt file in {DOCS_DIR}/prompts/:
PHASES[1]="phase-1-{name}.md";  PHASE_TOKENS[1]="PHASE_1_COMPLETE";  PHASE_NAMES[1]="{Phase 1 Name}"
# ...add more phases...
# Final phase token should use {FEATURE_NAME}_MODULE_COMPLETE (uppercased, hyphens as underscores)
```

Customize:
- Fill in all PHASES, PHASE_TOKENS, and PHASE_NAMES arrays, one entry per phase prompt file

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
│   ├── run.sh                  # Phase manifest
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

Each PR's `run.sh` is a phase manifest listing its own phases, in the same format as the single-sequence template above. Each `SCOPE.md` describes what domains are included and any dependencies on previous PRs.

Shared docs (TEST-PLAN, TEST-ARCHITECTURE, IMPLEMENTATION-PLAN) stay at `{DOCS_DIR}/` level.
{ENDIF SPLIT_PRS}

---

## Quality Checklist

Before finishing, verify ALL of these:

- [ ] TEST-PLAN.md covers every test domain with strategy and scope
- [ ] TEST-ARCHITECTURE.md has complete end-to-end setup, helper library, unit test stubs, mock patterns, test data strategy
- [ ] IMPLEMENTATION-PLAN.md maps every domain to a phase
- [ ] Phase ordering: infrastructure → component/unit test phases → end-to-end phase (final)
- [ ] Every phase prompt follows the Conductor template (status tracking, context, steps, verification, promise token)
- [ ] Every test case name from domain specs appears in a phase prompt
- [ ] Every mock/stub definition from domain specs is referenced in a phase prompt
- [ ] Every selector from domain spec §7 appears in the relevant end-to-end phase prompt
- [ ] run.sh declares PHASES, PHASE_TOKENS and PHASE_NAMES for every phase prompt
- [ ] Phase prompts reference TEST-PLAN/TEST-ARCHITECTURE sections instead of duplicating content
- [ ] Code blocks in phase prompts appear ONLY for complex test scenarios
- [ ] Final phase token uses `{FEATURE_NAME}_MODULE_COMPLETE` format (uppercased)
- [ ] Phase prompts do NOT include git add/commit instructions (runner handles this)
- [ ] Coverage matrix: every P1 AC has both component/unit and end-to-end test cases
- [ ] Component/unit test quality gates use the project's scoped test command
- [ ] End-to-end quality gates use the project's scoped E2E test command
- [ ] Final phase quality gate runs the project's full test suite plus the full E2E suite
{IF HAS_CONSTITUTION}
- [ ] TEST-ARCHITECTURE.md includes Constitution Compliance section
- [ ] No constitutional principles are violated by the test design
{ENDIF HAS_CONSTITUTION}

---

## Autonomous Execution Rules

- DO NOT ask for confirmation or present options — generate everything
- READ the domain specs thoroughly before writing anything
- WRITE all files directly — do not describe what you would write
- If a domain spec is ambiguous, make a reasonable decision and note it in TEST-ARCHITECTURE.md under Key Design Decisions
- START by reading the domain specs, then generate artifacts in order: TEST-PLAN → TEST-ARCHITECTURE → IMPLEMENTATION-PLAN → Prompts → Runner
