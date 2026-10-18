# qa-squad rubric

The rules qa.py enforces, written down so hunters, skeptics and humans read the same thing.

## Severity

| Severity | Meaning | Examples |
|---|---|---|
| CRITICAL | Data loss or corruption, money or stock wrong, someone sees or does what they must not | double charge, overselling, tenant data leak, auth bypass, IDOR on personal data |
| HIGH | Wrong business outcome, or an unhandled failure users will notice, or a privilege issue | order stuck in the wrong status, job fails with no retry or alert, user can edit a field reserved for admins |
| MEDIUM | Misbehaviour on an edge case, missing validation, a performance risk at scale | 500 on an emoji name, N+1 on a list page, missing index on a filter column |
| LOW | Cosmetic, a minor inconsistency, or a hardening suggestion | wrong error message text, inconsistent date format |

Rate the impact if the trigger happens, not how likely the trigger is. The skeptic's verdict covers likelihood.

## Finding validity (`qa.py validate`)

A finding is rejected, moved to `findings/_rejected/` and given a recorded reason if:

1. it is missing a frontmatter field (`id`, `lane`, `title`, `severity`, `category`, `location`, `root_cause`) or one of the six sections (What breaks, Why, Exact trigger, Expected vs actual, Suggested test, Suggested fix);
2. `severity` is not CRITICAL, HIGH, MEDIUM or LOW;
3. `location` has no `file:line`, or that file does not exist in the repo;
4. "Exact trigger" has fewer than 40 characters, or contains no concrete value, no HTTP verb or route, no sequence ("then", "while", "after") and no timing statement;
5. the title or trigger only hedges ("may", "might", "could potentially", "consider") and gives no concrete trigger;
6. "Suggested test" is empty;
7. it is CRITICAL or HIGH and "Why" contains no `file:line` citation;
8. it is beyond the hunter's first 8 findings. Extras are rejected as `over-cap`, starting with the lowest severity.

If more than 50% of a lane's findings are rejected, one fix-pass hunter is spawned for that lane. This happens once per lane.

## Verdicts (skeptics)

| Verdict | Meaning | Required evidence |
|---|---|---|
| CONFIRMED | Reproduced by a test, or observed directly | a file:line citation, plus the test path and its output |
| LIKELY | The code path clearly allows it, but it was not reproduced | file:line of the unguarded path + what blocked reproduction |
| DISPUTED | The code prevents it | file:line of the guard (middleware, policy, constraint, lock, validation rule) |
| NEEDS-ENV | Needs infrastructure the sandbox lacks | file:line + what infrastructure would settle it |
| UNVERIFIED | `--no-verify` was used, so no skeptic ran | (none) |

- A verdict without any `file:line` is rejected. The finding is re-queued once. If the second verdict is also invalid, the finding becomes NEEDS-ENV.
- **Race rule:** a `race-conditions` finding (or any finding with `category: race`) can only be CONFIRMED by a test that uses real parallelism: separate processes or separate DB connections released together by a barrier. The verdict must set `parallel_test: yes`. Otherwise qa.py downgrades CONFIRMED to LIKELY. Calling an endpoint twice in sequence does not reproduce a race.

Verdict strength, from strongest to weakest: CONFIRMED > LIKELY > NEEDS-ENV > UNVERIFIED > DISPUTED.

## Dedupe (`qa.py dedupe`, deterministic)

Two findings describe the same issue if any of these holds:

1. their `root_cause` values are identical after normalising case, whitespace and punctuation;
2. they are in the same file, at most 10 lines apart, with the same `category`;
3. they are in the same file and `difflib` gives a similarity of at least 0.75 for title + root_cause.

When findings merge, the survivor is the one with the strongest verdict, and after that the highest severity. It takes the highest severity in the group, the union of the lanes that found it (`found_by_lanes`), and a `merged_from` list. If two or more lanes found it independently, it gets the **multi-lane** badge. DISPUTED findings never merge with non-disputed ones, so a skeptic's disproof stays visible in the report.

## Release gate (`qa.py gate`)

Disputed findings are ignored. Every other final finding is checked:

**BLOCK RELEASE** if any finding that is CONFIRMED or LIKELY is
- CRITICAL, in any lane; or
- HIGH, and in `race-conditions`, `authz-security` or `data-integrity` (by lane or category), or about payments, billing, inventory/stock or tenant isolation.

**WARN** if no finding blocks but MEDIUM findings remain, a non-sensitive HIGH is CONFIRMED or LIKELY, or any HIGH or CRITICAL is only NEEDS-ENV or UNVERIFIED.

**PASS** if there are no findings, or only LOW findings.

The gate always lists its reasons and the coverage gaps: lanes that were skipped, checklist items no hunter examined, NEEDS-ENV findings, and hunters that failed to report.

## Test-case status mapping

| Verdict | Test-case status |
|---|---|
| CONFIRMED | Fail |
| LIKELY | Blocked |
| NEEDS-ENV / UNVERIFIED | Not tested |
| DISPUTED | Pass |
