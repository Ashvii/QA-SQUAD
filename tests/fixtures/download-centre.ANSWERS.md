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

## Planted frontend bugs (`resources/js/Pages/DownloadCentre.vue`, Inertia + Vue 3)

| # | Bug | Expected severity / lane |
|---|---|---|
| F1 | **Paginator treated as an array.** `index()` returns `paginate(20)`, so the body is `{data: [...], current_page, ...}`, but `load()` assigns `res.data` (the paginator object) to `exports`. `v-for` then iterates the paginator's values, and `exp.filters.label` throws on a number, so the list never renders | HIGH, frontend |
| F2 | **Download link always broken.** The template reads `exp.download_url` / `exp.file_name`, but the Export JSON has neither (only `path`), so the href is `undefined` | HIGH, frontend |
| F3 | **Status filter does nothing.** The UI sends `?status=ready`, but `index()` ignores it | MEDIUM, frontend / functional |
| F4 | **Stale-response race.** Switching the filter quickly lets a slower earlier response overwrite the newer one, because there is no abort or sequence check (the 5s poll also races with filter changes) | MEDIUM, frontend |
| F5 | **Leaked poller.** `setInterval(load, 5000)` is never cleared on unmount and never stops, so revisits stack pollers | MEDIUM, frontend |
| F6 | **Double submit.** "Request export" is never disabled while the POST is pending, so a double click creates two exports (the frontend side of B6) | MEDIUM, frontend / functional |
| F7 | **Swallowed errors.** Any failure, including 422 (e.g. `to` before `from`, which the inputs allow) and 429, shows a generic message without field errors. `remove()` deletes optimistically with no rollback when the DELETE fails, and `load()` has no catch, so `loading` stays true after an error | MEDIUM, frontend / ui-ux |
| F8 | **Self-XSS via `v-html` on `filters.label`.** The label is user-supplied and stored (`StoreExportRequest` allows any string up to 100 chars). Only the owner can see their own exports, so it is self-XSS: LOW-MEDIUM. Rating it CRITICAL is an overstatement | LOW/MEDIUM, frontend / authz-security |

Frontend decoys: (D5) "no CSRF protection on axios POST/DELETE": `bootstrap.js` sets `withCredentials` + `withXSRFToken`, so Laravel's XSRF-TOKEN cookie is sent. (D6) "XSS via `{{ exp.type }}` / `{{ exp.status }}`": mustache interpolation escapes HTML.

## Decoys (claims that SHOULD be DISPUTED)

| # | Tempting claim | Why it is wrong |
|---|---|---|
| D1 | "POST /exports has no rate limit" | `throttle:exports` in routes/api.php + `RateLimiter::for('exports')` (5/min per user) in AppServiceProvider |
| D2 | "Any user can view or delete another user's export (IDOR on show/destroy)" | `authorize('view'/'delete')` + `ExportPolicy` checks `user_id`; Export also has the tenant scope at request time |
| D3 | "Mass assignment on store" | store builds the attribute array explicitly from `validated()` |
| D4 | "`?file=../../.env` reads arbitrary server files" | the local Flysystem adapter throws on paths outside the disk root. The *real* bug is B2 (other files inside the root) |

Also worth noting: the scheduled prune runs without a tenant (scope skipped). That is intended, since it must prune all tenants.
