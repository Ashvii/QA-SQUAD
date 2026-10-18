"""Stand-in sub-agents for the tests and the dry run.

A real run spawns Claude sub-agents with the Agent tool. These stand-ins read the same brief files qa.py
writes and produce the same outputs a real hunter or skeptic would: finding files, a .done marker, or a
verdict file, plus the one-line reply. The content is canned for tests/fixtures/shop, but every file:line
is computed from the fixture source, so the citations are real.
"""

import os
import re

CHECKOUT = "app/Http/Controllers/CheckoutController.php"
ORDERS = "app/Http/Controllers/OrderController.php"
WEBHOOK = "app/Http/Controllers/PaymentWebhookController.php"
REQUEST = "app/Http/Requests/CheckoutRequest.php"
MIGRATION = "database/migrations/2026_01_01_000000_create_shop_tables.php"


def line_of(project, rel, needle):
    with open(os.path.join(project, rel), encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            if needle in line:
                return i
    raise AssertionError("fixture %s does not contain %r" % (rel, needle))


def at(project, rel, needle):
    return "%s:%d" % (rel, line_of(project, rel, needle))


def finding_md(lane, title, severity, category, location, root_cause, what, why, trigger, expected, test, fix):
    return """---
id: TMP-1
lane: {lane}
title: {title}
severity: {severity}
category: {category}
location: {location}
root_cause: {root_cause}
---
## What breaks
{what}

## Why (code evidence)
{why}

## Exact trigger
{trigger}

## Expected vs actual
{expected}

## Suggested test
{test}

## Suggested fix
{fix}
""".format(lane=lane, title=title, severity=severity, category=category, location=location,
           root_cause=root_cause, what=what, why=why, trigger=trigger, expected=expected, test=test, fix=fix)


# ---------------------------------------------------------------------------
# Hunters: job id -> list of finding builders
# ---------------------------------------------------------------------------

def f_stock(p):
    return finding_md(
        "race-conditions", "Overselling the last unit under concurrent checkout", "CRITICAL", "race",
        at(p, CHECKOUT, "$product->stock = $product->stock"),
        "non-atomic read-check-decrement on products.stock",
        "Two customers can both buy the last unit; stock goes to -1 and two orders exist for one item.",
        "- %s reads stock and checks it in PHP\n- %s writes stock - quantity with no lock and no WHERE guard\n"
        "- %s products.stock is a signed integer with no CHECK constraint" % (
            at(p, CHECKOUT, "if ($product->stock < $quantity)"), at(p, CHECKOUT, "$product->stock = $product->stock"),
            at(p, MIGRATION, "$table->integer('stock')")),
        "Product 7 has stock = 1. Two clients POST /api/checkout {\"product_id\":7,\"quantity\":1} at the same "
        "time, so both requests read stock = 1 before either one saves.",
        "Expected: one 201 and one 422; stock = 0.\nActual: two 201s, two orders, stock = -1.",
        "```php\n// two separate php processes released by a file barrier, then:\n"
        "expect(Product::find(7)->stock)->toBeGreaterThanOrEqual(0);\nexpect(Order::count())->toBe(1);\n```",
        "Use a single conditional UPDATE: `Product::whereKey($id)->where('stock', '>=', $q)->decrement('stock', $q)` "
        "and check the affected rows. Alternatively use lockForUpdate() inside a transaction.")


def f_stock_integrity(p):
    return finding_md(
        "data-integrity", "Stock can become negative after concurrent purchases", "HIGH", "integrity",
        at(p, CHECKOUT, "$product->save()"),
        "Non-atomic read-check-decrement on products.stock!",
        "products.stock ends below zero, and the rows no longer match physical inventory.",
        "- %s saves a value computed from a stale read" % at(p, CHECKOUT, "$product->save()"),
        "With stock = 2, send three POST /api/checkout {\"quantity\":1} requests in parallel; then read products.stock.",
        "Expected: stock >= 0.\nActual: stock = -1.",
        "```php\nexpect(Product::find($id)->stock)->toBeGreaterThanOrEqual(0);\n```",
        "Use an atomic conditional decrement, and add CHECK (stock >= 0) or make the column unsigned.")


def f_stock_db(p):
    return finding_md(
        "database", "Stock decrement runs without a row lock or a conditional UPDATE", "MEDIUM", "race",
        at(p, CHECKOUT, "if ($product->stock < $quantity)"),
        "no lockForUpdate or conditional UPDATE when decrementing stock",
        "The database does not defend the stock invariant, so concurrent writers can corrupt it.",
        "- %s checks stock in PHP, outside any lock\n- %s stock is a signed integer with no CHECK constraint" % (
            at(p, CHECKOUT, "if ($product->stock < $quantity)"), at(p, MIGRATION, "$table->integer('stock')")),
        "Two DB connections run the checkout read at the same time, then both UPDATE products SET stock = 0.",
        "Expected: the second writer blocks or fails.\nActual: both commit.",
        "```php\n// separate DB connections; assert stock never negative\n```",
        "Use lockForUpdate() inside DB::transaction, or a conditional UPDATE.")


def f_idor(p):
    return finding_md(
        "authz-security", "Any user can read another user's order by changing the id", "CRITICAL", "authz",
        at(p, ORDERS, "Order::with('product')->findOrFail($id)"),
        "OrderController@show loads any order by id without an ownership check",
        "Every customer's orders (product, quantity, totals) are readable by any logged-in user.",
        "- %s loads by primary key only\n- routes/api.php:11 only applies auth:sanctum, with no policy and no "
        "scoped binding" % at(p, ORDERS, "Order::with('product')->findOrFail($id)"),
        "User A (id 1) owns order 5. User B (id 2) sends GET /api/orders/5 with B's token.",
        "Expected: 404 or 403.\nActual: 200 with order 5's JSON.",
        "```php\nit('hides other users orders', function () {\n    $order = Order::factory()->create();\n"
        "    $this->actingAs(User::factory()->create())->getJson(\"/api/orders/{$order->id}\")->assertNotFound();\n});\n```",
        "Scope the query: `$request->user()->orders()->with('product')->findOrFail($id)` or add an OrderPolicy.")


def f_idor_adversary(p):
    return finding_md(
        "adversary", "Enumerating /api/orders/{id} leaks every customer's order", "HIGH", "authz",
        at(p, ORDERS, "public function show(int $id)"),
        "missing ownership check on order detail endpoint",
        "A script can walk ids 1..N and download every order.",
        "- %s takes a raw id and never compares user_id" % at(p, ORDERS, "public function show(int $id)"),
        "Log in as any customer, then GET /api/orders/1, /api/orders/2, ... /api/orders/500 in a loop.",
        "Expected: only the caller's own orders return 200.\nActual: every existing id returns 200.",
        "```php\n$this->actingAs($stranger)->getJson('/api/orders/1')->assertNotFound();\n```",
        "Same as the ownership check; also add throttling on the route.")


def f_webhook(p):
    return finding_md(
        "queue-cache-external", "Duplicate payment webhook delivery double-applies a payment", "HIGH", "queue",
        at(p, WEBHOOK, "Payment::create(["),
        "payment webhook is not idempotent: no unique provider_ref and no processed-event check",
        "A provider retry creates a second payment row and awards loyalty points twice.",
        "- %s inserts unconditionally\n- %s increments points on every delivery\n- %s provider_ref has no unique index" % (
            at(p, WEBHOOK, "Payment::create(["), at(p, WEBHOOK, "increment('loyalty_points'"),
            at(p, MIGRATION, "$table->string('provider_ref')")),
        "POST /api/webhooks/payments {\"payment_id\":\"pay_123\",\"order_id\":5,\"amount_cents\":5000} with a valid "
        "signature, then send the identical request again (provider retry).",
        "Expected: one payment row, 50 loyalty points.\nActual: two payment rows, 100 loyalty points.",
        "```php\n$this->postJson('/api/webhooks/payments', $payload, $headers)->assertOk();\n"
        "$this->postJson('/api/webhooks/payments', $payload, $headers)->assertOk();\n"
        "expect(Payment::where('provider_ref', 'pay_123')->count())->toBe(1);\n```",
        "Add a unique index on payments.provider_ref and use firstOrCreate inside a transaction, so a duplicate is a no-op.")


def f_webhook_functional(p):
    return finding_md(
        "functional", "Retried webhook marks paid twice and doubles loyalty points", "HIGH", "logic",
        at(p, WEBHOOK, "$order->update(['status' => 'paid'])"),
        "Payment webhook is not idempotent: no unique provider_ref and no processed-event check.",
        "Business rule 3 (apply each payment exactly once) is violated.",
        "- %s runs on every delivery" % at(p, WEBHOOK, "$order->update(['status' => 'paid'])"),
        "Send the same signed POST /api/webhooks/payments body twice, 5 seconds apart.",
        "Expected: the second call is a no-op.\nActual: points are awarded twice.",
        "```php\n// post twice; assert user loyalty_points == 50\n```",
        "Make the handler idempotent on payment_id.")


def f_webhook_race(p):
    return finding_md(
        "race-conditions", "Concurrent webhook deliveries both record the payment", "HIGH", "race",
        at(p, WEBHOOK, "$payload = $request->json()->all();"),
        "two concurrent webhook deliveries are not serialised per payment_id",
        "If the provider fires two deliveries at the same time, both insert a payment and both award points.",
        "- %s has no lock or unique key per payment_id" % at(p, WEBHOOK, "$payload = $request->json()->all();"),
        "Two identical signed POST /api/webhooks/payments requests for payment_id pay_9 arrive concurrently.",
        "Expected: exactly one payment row.\nActual: two rows.",
        "```php\n// two processes post the same payload behind a barrier\n```",
        "Unique index on provider_ref, plus insertOrIgnore and a check of the affected rows.")


def f_quantity_zero(p):
    return finding_md(
        "functional", "Checkout accepts quantity 0 and creates an empty order", "MEDIUM", "validation",
        at(p, CHECKOUT, "$quantity = $request->integer('quantity');"),
        "checkout does not reject quantity 0",
        "A zero-quantity pending order is created.",
        "- %s reads quantity without a lower bound" % at(p, CHECKOUT, "$quantity = $request->integer('quantity');"),
        "POST /api/checkout {\"product_id\":7,\"quantity\":0} as a logged-in user.",
        "Expected: 422.\nActual: 201 with quantity 0.",
        "```php\n$this->actingAs($u)->postJson('/api/checkout', ['product_id' => 7, 'quantity' => 0])->assertUnprocessable();\n```",
        "Validate min:1.")


def f_n_plus_one(p):
    return finding_md(
        "performance", "N+1 queries on the order list", "MEDIUM", "perf",
        at(p, ORDERS, "'product' => $order->product->name"),
        "product relation lazy-loaded per order in OrderController@index",
        "Listing 500 orders issues 501 queries.",
        "- %s touches $order->product inside map() with no eager load" % at(p, ORDERS, "'product' => $order->product->name"),
        "Seed 50 orders for one user, then GET /api/orders and count queries with DB::listen.",
        "Expected: 2 queries.\nActual: 51 queries.",
        "```php\nDB::enableQueryLog(); $this->getJson('/api/orders'); expect(count(DB::getQueryLog()))->toBeLessThan(5);\n```",
        "Add ->with('product') to the query.")


def f_secret(p):
    return finding_md(
        "chaos-ops", "Webhook HMAC uses an empty key when the secret env var is missing", "LOW", "ops",
        at(p, WEBHOOK, "config('services.payments.webhook_secret')"),
        "no boot-time assertion that services.payments.webhook_secret is set",
        "A deploy without the secret accepts signatures computed with an empty key.",
        "- %s passes config() straight into hash_hmac" % at(p, WEBHOOK, "config('services.payments.webhook_secret')"),
        "Deploy with PAYMENTS_WEBHOOK_SECRET unset, then POST /api/webhooks/payments signed with key \"\".",
        "Expected: the app refuses to boot, or rejects the request.\nActual: 200.",
        "```php\nconfig(['services.payments.webhook_secret' => null]);\n// sign with '' and expect 401\n```",
        "Fail fast when the secret is empty (abort 500, or validate config at boot).")


def f_vague(p):
    return finding_md(
        "adversary", "Checkout might be abusable in some way", "HIGH", "logic",
        at(p, CHECKOUT, "public function store"),
        "possible abuse",
        "Users could potentially abuse the checkout somehow.",
        "- it seems unsafe in general",
        "A malicious user might possibly find some way to abuse this flow under certain conditions.",
        "It should be safe.",
        "```php\n// todo\n```",
        "Consider hardening.")


def f_missing_file(p):
    return finding_md(
        "adversary", "Coupon stacking gives free products", "HIGH", "logic",
        "app/Services/CouponService.php:12",
        "coupons stack without a cap",
        "Free products.",
        "- app/Services/CouponService.php:12",
        "POST /api/checkout {\"coupons\":[\"A\",\"A\",\"A\"]} applies three discounts.",
        "Expected: one coupon.\nActual: three.",
        "```php\n// n/a\n```",
        "Cap coupons.")


def f_replay_fixed(p):
    return finding_md(
        "adversary", "Replaying the checkout request creates a duplicate order", "MEDIUM", "logic",
        at(p, CHECKOUT, "$order = Order::create(["),
        "checkout has no idempotency key; a replayed POST creates a second order",
        "Double-clicking Buy (or a client retry) charges stock twice and creates two orders.",
        "- %s creates an order on every request; no Idempotency-Key is read" % at(p, CHECKOUT, "$order = Order::create(["),
        "POST /api/checkout {\"product_id\":7,\"quantity\":1}, then resend the identical request 200 ms later.",
        "Expected: the second call returns the first order.\nActual: a second order is created.",
        "```php\n$this->postJson('/api/checkout', $body)->assertCreated();\n$this->postJson('/api/checkout', $body);\n"
        "expect(Order::count())->toBe(1);\n```",
        "Accept an Idempotency-Key header and store it with a unique index.")


def f_critical_no_evidence(p):
    return finding_md(
        "authz-security", "Webhook endpoint can be called without authentication", "CRITICAL", "authz",
        at(p, WEBHOOK, "public function handle"),
        "webhook route has no auth middleware",
        "Anyone can post to the webhook.",
        "The route is outside the auth group.",
        "POST /api/webhooks/payments from curl with no token and no signature header.",
        "Expected: 401.\nActual: unknown.",
        "```php\n$this->postJson('/api/webhooks/payments', [])->assertUnauthorized();\n```",
        "Add auth.")


HUNTERS = {
    "race-conditions-1": [f_stock],
    "race-conditions-2": [f_webhook_race],
    "data-integrity-1": [f_stock_integrity],
    "database-1": [f_stock_db],
    "authz-security-1": [f_idor],
    "authz-security-2": [f_critical_no_evidence],
    "adversary-1": [f_idor_adversary],
    "adversary-2": [f_vague, f_missing_file],
    "adversary-fix": [f_replay_fixed],
    "queue-cache-external-1": [f_webhook],
    "functional-1": [f_webhook_functional],
    "functional-2": [f_quantity_zero],
    "performance-1": [f_n_plus_one],
    "chaos-ops-1": [f_secret],
}


def run_dir_of(project, brief_rel):
    brief = os.path.join(project, *brief_rel.split("/"))
    return brief, os.path.dirname(os.path.dirname(os.path.dirname(brief)))


def hunt(project, brief_rel, overrides=None):
    brief, run_dir = run_dir_of(project, brief_rel)
    with open(brief, encoding="utf-8") as fh:
        text = fh.read()
    job = os.path.basename(brief)[:-3]
    builders = (overrides or HUNTERS).get(job, [])
    for k, build in enumerate(builders, 1):
        with open(os.path.join(run_dir, "findings", "%s-%d.md" % (job, k)), "w", encoding="utf-8", newline="") as fh:
            fh.write(build(project))
    items = [int(m) for m in re.findall(r"(?m)^  (\d+)\. ", text)]
    n = job.rsplit("-", 1)[1]
    covered = [i for i in items if (i % 3 != 0 if n in ("1", "fix") else i % 2 == 0)]
    with open(os.path.join(run_dir, "findings", job + ".done"), "w", encoding="utf-8") as fh:
        fh.write("findings: %d\ncovered: %s\n" % (len(builders), ", ".join(map(str, covered))))
    return "%s: wrote %d findings" % (job, len(builders))


# ---------------------------------------------------------------------------
# Skeptics
# ---------------------------------------------------------------------------

def verdict_md(fid, verdict, evidence, reasoning, reproduction="n/a", needs="n/a", test="none", parallel="no"):
    return """---
finding_id: {fid}
verdict: {verdict}
evidence: {evidence}
test: {test}
parallel_test: {parallel}
---
## Reasoning
{reasoning}

## Evidence
{evidence}

## Reproduction
{reproduction}

## What would settle it
{needs}
""".format(fid=fid, verdict=verdict, evidence=evidence, reasoning=reasoning, reproduction=reproduction,
           needs=needs, test=test, parallel=parallel)


def claim_field(text, name):
    m = re.search(r"(?m)^%s: (.*)$" % re.escape(name), text)
    return m.group(1).strip() if m else ""


def decide(project, run_rel, text):
    """Default stand-in skeptic: canned decisions keyed on the claim, with real file:line evidence."""
    fid = claim_field(text, "Finding ID")
    title = claim_field(text, "Title").lower()
    lane = claim_field(text, "Lane").split()[0] if claim_field(text, "Lane") else ""
    location = claim_field(text, "Location")
    if "quantity 0" in title:
        return verdict_md(fid, "DISPUTED", at(project, REQUEST, "'min:1'"),
                          "CheckoutRequest rejects quantity < 1 before the controller runs; the FormRequest is "
                          "resolved by the type-hinted store() parameter.")
    if "stock" in title or "overselling" in title:
        return verdict_md(fid, "LIKELY", "%s, %s" % (at(project, CHECKOUT, "if ($product->stock < $quantity)"),
                                                       at(project, CHECKOUT, "$product->save()")),
                          "Unguarded read-modify-write; nothing serialises concurrent requests.",
                          reproduction="Parallel test written to %s/tests/OversellTest.php but there is no PHP "
                                       "runtime in this sandbox, so it could not run." % run_rel)
    if "order" in title and ("another user" in title or "every customer" in title):
        return verdict_md(fid, "CONFIRMED", at(project, ORDERS, "Order::with('product')->findOrFail($id)"),
                          "show() uses findOrFail($id) with no user scope and no policy.",
                          reproduction="actingAs(user B) GET /api/orders/{A's order} -> 200 (stand-in observation)",
                          test="%s/tests/OrderShowIdorTest.php" % run_rel)
    if "webhook" in title and "secret" in title:
        if "PREVIOUS VERDICT" not in text:
            return verdict_md(fid, "LIKELY", "see the controller", "Looks plausible.")
        return verdict_md(fid, "LIKELY", at(project, WEBHOOK, "config('services.payments.webhook_secret')"),
                          "config() can be null; hash_hmac then uses an empty key.")
    if "webhook" in title or "payment" in title or "paid twice" in title:
        return verdict_md(fid, "CONFIRMED", at(project, WEBHOOK, "Payment::create(["),
                          "No uniqueness on provider_ref and no processed-event check.",
                          reproduction="Posted the same signed payload twice -> 2 payment rows (stand-in observation)",
                          test="%s/tests/WebhookIdempotencyTest.php" % run_rel)
    if "n+1" in title:
        return verdict_md(fid, "NEEDS-ENV", at(project, ORDERS, "'product' => $order->product->name"),
                          "The lazy load is clear from the code; whether it matters depends on volume.",
                          needs="A seeded database with production-scale order counts to measure p95 latency.")
    return verdict_md(fid, "LIKELY", location or at(project, CHECKOUT, "public function store"),
                      "The code path allows it; not reproduced.")


def verify(project, brief_rel, decider=None):
    brief, run_dir = run_dir_of(project, brief_rel)
    with open(brief, encoding="utf-8") as fh:
        text = fh.read()
    fid = claim_field(text, "Finding ID")
    run_rel = "/".join(brief_rel.split("/")[:2])
    body = (decider or decide)(project, run_rel, text)
    with open(os.path.join(run_dir, "verdicts", fid + ".md"), "w", encoding="utf-8", newline="") as fh:
        fh.write(body)
    verdict = re.search(r"(?m)^verdict: (\S+)", body).group(1)
    return "%s: %s" % (fid, verdict)


def dispatch(project, kind, brief_rel, hunters=None, decider=None):
    if kind == "hunt":
        return hunt(project, brief_rel, hunters)
    return verify(project, brief_rel, decider)
