# Answer key: download-centre fixture

Kept OUTSIDE `download-centre/` on purpose, so agents running on a copy of the fixture never see it.

## Planted bugs

| # | Bug | Where | Expected severity / lane |
|---|---|---|---|
| B1 | **Cross-tenant data leak.** The queue worker never runs `SetTenant`, so `currentTenantId` is not bound and the `BelongsToTenant` scope is skipped. The export then contains every tenant's invoices in the date range | `app/Jobs/GenerateExportJob.php` `handle()` invoice query, combined with `BelongsToTenant.php` `if (app()->bound(...))` | CRITICAL, authz-security / data-integrity |
| B2 | **`?file=` override on download.** The policy check is done on `$export`, but the file served comes from the query string, so a user can fetch other files on the `exports` disk (e.g. `?file=2/export-55.csv`, another tenant's export). Flysystem blocks `../` outside the disk root, but not other files inside it | `ExportController::download` | CRITICAL, authz-security |
| B3 | **Retry overlap duplicates rows.** `$timeout = 120` exceeds `retry_after = 90`, so a slow job is retried while still running. `fopen(..., 'a')` appends, so retries and overlaps produce a CSV with duplicated rows and headers | `GenerateExportJob` + `config/queue.php` | HIGH, queue-cache-external / race-conditions / data-integrity |
| B4 | **Stuck in "processing".** No `failed()` method, so after 3 failures the export stays `processing` forever and the user is never told | `GenerateExportJob` | MEDIUM/HIGH, queue-cache-external / functional |
| B5 | **Memory blow-up.** `->get()` loads every invoice in the range into memory instead of `lazy()`/`cursor()`/`chunk()` | `GenerateExportJob::handle` | MEDIUM, performance |
| B6 | **Duplicate exports on double submit.** No idempotency, so two clicks create two exports and two jobs (the rate limit of 5/min does not prevent this) | `ExportController::store` | LOW/MEDIUM, functional / adversary |

## Decoys (claims that SHOULD be DISPUTED)

| # | Tempting claim | Why it is wrong |
|---|---|---|
| D1 | "POST /exports has no rate limit" | `throttle:exports` in routes/api.php + `RateLimiter::for('exports')` (5/min per user) in AppServiceProvider |
| D2 | "Any user can view or delete another user's export (IDOR on show/destroy)" | `authorize('view'/'delete')` + `ExportPolicy` checks `user_id`; Export also has the tenant scope at request time |
| D3 | "Mass assignment on store" | store builds the attribute array explicitly from `validated()` |
| D4 | "`?file=../../.env` reads arbitrary server files" | the local Flysystem adapter throws on paths outside the disk root. The *real* bug is B2 (other files inside the root) |

Also worth noting: the scheduled prune runs without a tenant (scope skipped). That is intended, since it must prune all tenants.
