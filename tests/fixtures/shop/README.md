# Fixture: deliberately buggy Laravel shop

A tiny project for qa-squad's tests and dry run (`python tests/dry_run.py`). It is not a working app: there is no `vendor/`, and nothing in it is ever executed.

The three planted bugs:

| Bug | Where | Expected lanes |
|---|---|---|
| Non-atomic stock decrement (overselling) | `app/Http/Controllers/CheckoutController.php` `store()` | race-conditions, data-integrity, database |
| IDOR: any user can read any order | `app/Http/Controllers/OrderController.php` `show()` | authz-security, adversary |
| Non-idempotent payment webhook (duplicate delivery double-applies) | `app/Http/Controllers/PaymentWebhookController.php` `handle()` | queue-cache-external, functional, race-conditions |

`CheckoutRequest` does validate `quantity` (`min:1`), so a claim that quantity 0 gets through should end up DISPUTED.
