# qa-squad

**A QA team of sub-agents for Claude Code.** Run `/qa-squad` on a feature, endpoint, PR, diff or module. Specialist sub-agents each hunt a different class of bug: functional, race conditions, data integrity, authorization/security, database, queue/cache/external services, performance, UI, ops, and a hostile "adversary". A separate **skeptic** agent then tries to disprove every finding. You get one deduplicated, severity-ranked report with reproducible test cases and a release gate verdict: **PASS / WARN / BLOCK**.

- Free, MIT licensed. No API key, no signup.
- `qa.py` is Python 3.8+ standard library only. Nothing to `pip install`.
- Laravel-first (PHP, Pest/PHPUnit, MySQL/Postgres, Redis queues). It detects Node, Django/FastAPI/Flask, Rails and Go from `package.json` / `pyproject.toml` / `Gemfile` / `go.mod` and translates the techniques for that stack.

## Install

**Option 1: paste the URL into Claude Code**

> Install the Claude Code skill from https://github.com/Ashvii/QA-SQUAD into ~/.claude/skills/qa-squad

**Option 2: git clone + copy**

```bash
git clone https://github.com/Ashvii/QA-SQUAD.git
mkdir -p ~/.claude/skills
cp -r QA-SQUAD/skills/qa-squad ~/.claude/skills/qa-squad      # personal, all projects
# or, per project:  cp -r QA-SQUAD/skills/qa-squad .claude/skills/qa-squad
```

**Option 3: plugin**

```
/plugin marketplace add Ashvii/QA-SQUAD
/plugin install qa-squad@qa-squad
```

(As a plugin, the command is namespaced: `/qa-squad:qa-squad`.)

Recommended: switch to accept-edits mode (Shift+Tab) before a run, because every agent writes files under `.qa-squad/`.

## Usage

```
/qa-squad                                  # scope = what you just asked about, or your uncommitted diff
/qa-squad the checkout endpoint            # explicit target
/qa-squad --diff main                      # everything changed vs main
/qa-squad --quick                          # 5 core lanes, depth 1 (~20 agent calls)
/qa-squad --lanes race,authz app/Jobs/     # only these lanes
/qa-squad --skip ui-ux,perf                # everything except these
/qa-squad --depth 3                        # 3 hunters per lane, each from a different angle
/qa-squad --no-verify                      # skip skeptics: cheaper, noisier, everything UNVERIFIED
/qa-squad --wave 5                         # at most 5 parallel sub-agents
/qa-squad --seed 42                        # reproducible angle dealing
/qa-squad --rerun race                     # after a fix: re-run only the race lane on the same scope
```

Or just ask: *"is this production ready?"*, *"break this"*, *"find bugs in the refund flow"*. When Claude triggers the skill itself, it shows the cost plan and asks before spending.

## How it works

```
 you: /qa-squad checkout
   │
   ▼
 SCOPE ─────── orchestrator reads routes, controllers, models, policies, migrations, jobs, tests
   │           writes .qa-squad/<run>/scope.md  (the ONLY context agents get; SHA-256 recorded)
   ▼
 PLAN ──────── qa.py plan → cost table; asks first if auto-triggered or > 60 calls
   ▼
 HUNT ──────── lanes × depth hunters, in waves of 10        ┌ functional        ┌ angle 1 code-reading
   │           each writes findings/*.md + a .done marker   ├ race-conditions   ├ angle 2 test-first
   │           and replies with ONE line                    ├ data-integrity    ├ angle 3 data-driven
   │                                                        ├ authz-security    ├ angle 4 attacker
   │                                                        ├ database          └ angle 5 historical
   │                                                        ├ queue-cache-external
   │                                                        ├ performance · ui-ux · chaos-ops
   │                                                        └ adversary
   ▼
 VALIDATE ──── pure Python: rejects vague or malformed findings (no file:line, hedging, no trigger,
   │           no test, over the 8-per-hunter cap). >50% of a lane rejected → one fix-pass hunter
   ▼
 VERIFY ────── one skeptic per finding. Sees scope + claim only, never the hunter's reasoning.
   │           CONFIRMED / LIKELY / DISPUTED / NEEDS-ENV, each with file:line evidence.
   │           Race claims need a real-parallelism test to be CONFIRMED.
   ▼
 DEDUPE ────── pure Python: same root cause / same file ±10 lines / fuzzy title → merge,
   │           keep the best evidence + highest severity, mark "multi-lane"
   ▼
 REPORT ────── REPORT.md + testcases.csv + release gate (PASS / WARN / BLOCK)
```

The architecture is borrowed from "arena"-style skills. A thin orchestrator prompt drives a standard-library Python state machine. Sub-agents write to disk and reply with one line, so the main conversation stays small even with 100 agents. The orchestrator never opens a finding or verdict file; only `qa.py` reads them. Every step is recorded in `.qa-squad/<run>/state.json` with atomic writes. If the conversation is compacted or interrupted, `qa.py next` says exactly what to do next.

## Lanes

| Lane | Hunts for | Skipped when |
|---|---|---|
| `functional` | business rules, validation, boundaries, state transitions, error responses | never |
| `race-conditions` | lost updates, double processing, overselling, duplicate callbacks; must prove with real parallelism | no shared mutable state |
| `data-integrity` | orphans, partial writes, money as floats, timezones, side effects before commit, soft deletes | no writes |
| `authz-security` | 401/403, IDOR, tenant isolation, mass assignment, leaks, injection, XSS, rate limits | never for reachable code |
| `database` | missing constraints/indexes, N+1, migrations on populated tables, deadlock/retry safety | no DB access |
| `queue-cache-external` | retries, duplicate side effects, failed_jobs, Redis down, stale cache, webhooks, 429s | no async/integration |
| `performance` | what falls over at 10x; static analysis and bounded measurement only, no load tests | trivial changes |
| `ui-ux` | loading/empty/error states, double submit, back button, a11y, timezones | no UI |
| `chaos-ops` | env/secrets, debug mode, deploy order, `queue:restart`, logging, alerts, rollback | pure refactor |
| `adversary` | replay, reorder, stale tabs, direct API calls, absurd sizes, account switching | never |

Lane cards live in [`skills/qa-squad/lanes.json`](skills/qa-squad/lanes.json) and are meant to be edited. You can add checklist items, add a lane, or change `default_depth`.

## Cost

Numbers from `qa.py plan`, assuming about 3 valid findings per hunter (`--est-findings` changes the estimate). "Calls" means sub-agent invocations.

| Run | Lanes × depth | Hunters | Skeptics (est.) | Calls | Waves of 10 | Asks first? |
|---|---|---|---|---|---|---|
| `--quick --no-verify` | 5 × 1 | 5 | 0 | **5** | 1 | no |
| `--quick` | 5 × 1 | 5 | 15 | **20** | 3 | no |
| default, API-only (`--skip ui-ux`) | 9 × 2 | 18 | 54 | **72** | 8 | yes (> 60) |
| full (all 10 lanes) | 10 × 2 | 20 | 60 | **80** | 8 | yes |
| full `--depth 3` | 10 × 3 | 30 | 90 | **120** | 12 | yes |

In practice the orchestrator usually skips 2-4 lanes based on the scope, so a typical default run lands around 40-60 calls. Retries (fix passes and re-queued verdicts) are bounded; `plan` prints the upper bound.

## Output

Everything goes in `.qa-squad/<YYYYMMDD-HHMMSS-slug>/` in your project. The folder ignores itself in git.

| Path | What |
|---|---|
| `REPORT.md` | header, release gate + reasons, summary table, findings by severity (trigger, evidence, failing test, fix, which lanes), disputed findings with the evidence that killed them, needs-env items, coverage matrix, test-case table |
| `testcases.csv` | `Test Case ID, Feature, Scenario, Preconditions, Test data, Steps, Expected result, Actual result, Priority/Severity, Status`, ready for Jira / TestRail / Sheets import. CONFIRMED → Fail, LIKELY → Blocked, NEEDS-ENV → Not tested, DISPUTED → Pass |
| `tests/` | tests written by hunters and skeptics. They are never copied into your suite unless you say so |
| `scope.md`, `state.json` | the exact context agents saw (with its SHA-256) and the run's state |
| `findings/`, `verdicts/`, `briefs/` | raw agent output (rejects in `_rejected/` with a reason) and the exact brief each agent got |

## Safety

These hard rules are repeated in the skill and in every sub-agent brief:

1. Never edit application source, config, migrations or existing tests. Agents write only inside `.qa-squad/`. Applying generated tests is your decision, and the skill asks first.
2. Nothing that writes data runs unless `APP_ENV` is local/testing **and** the DB name is clearly a test database. Otherwise agents do static analysis only.
3. No load or stress testing. No calls to real external services or payment APIs: agents use fakes (`Http::fake`, `Queue::fake`, `Event::fake`) or stop at LIKELY.
4. No exploit tooling, and no scanning outside the local test app.
5. No destructive commands (`migrate:fresh` on a non-test DB, `rm -rf`, `DROP`, …).
6. `.env` values never appear in findings, logs or the report.

## Development

```bash
python3 -m unittest discover -s tests -v      # 42 tests, stdlib only
python3 tests/dry_run.py --keep                # full pipeline on tests/fixtures/shop with stand-in agents
python3 skills/qa-squad/qa.py plan --quick     # cost table
```

`tests/fixtures/shop` is a tiny Laravel shop with three planted bugs: a non-atomic stock decrement, an IDOR on order show, and a non-idempotent payment webhook. `tests/standin.py` plays the sub-agents. It reads the same briefs a real agent would and writes canned findings and verdicts with real file:line citations. The dry run flags all three bugs and the gate says BLOCK.

The release gate, the severity definitions and the validation rules are in [`skills/qa-squad/rubric.md`](skills/qa-squad/rubric.md).

## Fine print

- The agents are the same model looking through different lenses. Lanes and angles diversify the search, but they share blind spots.
- A verdict is evidence, not proof. CONFIRMED means a test or observation backed it up. LIKELY means the code path allows it.
- **Skeptics can be wrong.** A DISPUTED finding is shown with the guard the skeptic cited, so you can check the skeptic too.
- A vague scope produces vague findings. The scope.md the orchestrator writes is the biggest single factor in quality.
- It does not replace a real load test, a penetration test, or human exploratory testing.

## License

MIT. See [LICENSE](LICENSE).
