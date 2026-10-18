# Shop checkout, order view and payment webhook

## Feature summary
Customers buy a product through `POST /api/checkout`. The endpoint checks stock, decrements it and creates a
`pending` order. Customers view their orders through `GET /api/orders` and `GET /api/orders/{id}`. The payment
provider calls `POST /api/webhooks/payments` when a payment succeeds. That call records a payment, marks the order
`paid` and awards loyalty points (1 per 100 cents).

## Files in scope
- `app/Http/Controllers/CheckoutController.php` - POST /api/checkout: stock check, decrement, order creation
- `app/Http/Controllers/OrderController.php` - GET /api/orders (list) and GET /api/orders/{id} (detail)
- `app/Http/Controllers/PaymentWebhookController.php` - POST /api/webhooks/payments: HMAC check, payment row, status, loyalty points
- `app/Http/Requests/CheckoutRequest.php` - validation for checkout (quantity 1..100, product exists)
- `app/Models/Product.php`, `app/Models/Order.php`, `app/Models/Payment.php` - Eloquent models
- `database/migrations/2026_01_01_000000_create_shop_tables.php` - products, orders, payments tables
- `routes/api.php` - route definitions and middleware

## Routes and HTTP verbs
| Verb | URI | Controller@method | Middleware |
|---|---|---|---|
| POST | /api/checkout | CheckoutController@store | auth:sanctum |
| GET | /api/orders | OrderController@index | auth:sanctum |
| GET | /api/orders/{id} | OrderController@show | auth:sanctum |
| POST | /api/webhooks/payments | PaymentWebhookController@handle | none (HMAC header X-Signature) |

## Models and relationships
- Order belongsTo User, belongsTo Product, hasMany Payment. Fillable: user_id, product_id, quantity, total_cents, status.
- Product: fillable name, price_cents, stock. Payment: fillable order_id, provider_ref, amount_cents.
- No global scopes, no observers, no policies registered.

## Database tables, constraints and indexes
- products: id, name, price_cents unsigned int, stock signed int default 0. No CHECK (stock >= 0).
- orders: id, user_id FK, product_id FK, quantity, total_cents, status string default 'pending'.
- payments: id, order_id FK, provider_ref string (NO unique index), amount_cents.

## Jobs, queues, events, listeners, scheduled tasks
None. Everything runs inside the request.

## External services
The payment provider sends webhooks signed with HMAC-SHA256 (`services.payments.webhook_secret`). The provider
retries a delivery until it gets a 2xx, and it can deliver the same event more than once.

## Auth model
Sanctum tokens. Customers may only see their own orders. There are no admin roles in scope. Not multi-tenant.

## Business rules and state machines
1. Stock never goes below 0. You cannot buy more units than are in stock.
2. An order belongs to the user who created it, and only that user can read it.
3. Each provider payment (`payment_id`) is applied exactly once: one payment row, one status change, one loyalty award.
4. Order status: pending -> paid. No other transitions in scope.

## How to run tests
- `php artisan test`
- A generated test from a run: `php artisan test .qa-squad/<run>/tests/<File>Test.php`

## Environment facts
APP_ENV=testing, DB_CONNECTION=sqlite, DB_DATABASE=:memory:, QUEUE_CONNECTION=sync. PHP 8.2, Laravel 11.
(Fixture note: no PHP runtime in the dry-run sandbox, so nothing can actually be executed.)

## Known gaps
No tests for the webhook or order endpoints yet.

## Out of scope
Authentication and registration, admin panel, refunds, shipping.
