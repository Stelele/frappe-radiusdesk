# Radius Desk Developer Guide

This guide covers the internal architecture, hooks, whitelisted APIs, DocType schemas, testing, and development setup for the Radius Desk app.

---

## Architecture Overview

The app follows the Frappe framework layering pattern with these key layers:

| Layer | Responsibility |
|---|---|
| **Hooks** (`hooks.py`) | DocEvents (invoice submit auto-voucher), Scheduler events (daily POS entry, 5-min fulfillment retry), `after_migrate` |
| **Backend Python** (`radius_desk/radius_desk/utils/`) | Core business logic: voucher creation, fulfillment, payment initiation, hotspot URL validation, system user management |
| **Whitelisted APIs** (`voucher_sale.py` @whitelist) | Public and authenticated endpoints for create, status, confirmation, and retry |
| **DocTypes** (`doctype/`) | Data models: Voucher Plan, Voucher Sale, Radius Desk Settings; define fields, permissions, and validation |
| **Frontend** (`www/`, `public/js/`) | Embedded checkout page, POS receipt voucher summary, hotspot URL sanitization |
| **Installation** (`installer.py`) | Idempotent custom field sync, POS invoice mode enforcement, system user creation |

### Data Flow — POS Invoice Submit

1. Cashier submits **POS Invoice** containing a Voucher Plan item.
2. `doc_events` → `create_vouchers_for_invoice` → `frappe.enqueue` to `process_invoice_vouchers`.
3. Background job runs as **system user** (`frappe.set_user(SYSTEM_USER)`).
4. For each matching line item, a **Voucher Sale** is created in `Draft` status.
5. `vs.confirm_voucher_sale(sale.name)` triggers immediate fulfillment (cash mode).
6. Sale is promoted to `Voucher Created` → `_stamp_invoice` → invoice stamped with `radius_voucher_code` → `Completed`.
7. If PesaPay path: `initiate_voucher_payment` → customer approves → `confirm_voucher_payment` → `fulfill_voucher_sale` → voucher created → invoice stamped → `Completed`.

### Data Flow — Web Checkout

1. Customer selects plan + enters phone + payment method on `/voucher-checkout/`.
2. `create_web_checkout` creates `Voucher Sale` (`Draft`), calls `initiate_voucher_payment`, returns `checkout_token`.
3. Page polls `confirm_voucher_web_checkout` with the `poll_url` every 3s; on gateway SUCCESS it fulfills immediately.
4. The PesaPay webhook (`on_payment_authorized`) and scheduler are fallback only.
5. `fulfill_voucher_sale` creates the RadiusDesk voucher, stamps the invoice, promotes to `Completed`.
6. Voucher code delivered via `#rd-voucher=<code>` fragment.

---

## Whitelisted APIs (verified `/api/method/<module>.<method>` URLs)

All methods are in `radius_desk.radius_desk.doctype.voucher_sale.voucher_sale`. The full API path is `/api/method/radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.<method>`.

| Method | Allow Guest | Description |
|---|---|---|
| `get_voucher_plans` | Yes | List enabled plans for the default company, with price resolved from the selling price list. |
| `create_voucher_sale` | No | Create a `Draft` `Voucher Sale` for an in-progress POS invoice. Idempotent per invoice. |
| `initiate_voucher_payment` | No | Initiate PesaPay seamless payment for a `Draft` sale. Validates method/currency combo. |
| `reset_voucher_sale` | No | Reset a `Payment Pending` sale back to `Draft` for retry. Rejects confirmed/fulfilled sales. |
| `confirm_voucher_payment` | No | POS client: confirm a `Payment Pending` sale via merchant reference and fulfill. |
| `confirm_voucher_sale` | No | Cashier path for non-PesaPay modes (Cash, etc.): confirm `Draft` sale and fulfill immediately. |
| `get_voucher_codes_for_invoice` | No | Return voucher codes created for a submitted POS/Sales Invoice. Used by POS receipt screen; respects Voucher Sale read permissions. |
| `create_web_checkout` | Yes | Public guest checkout: create sale, initiate payment, return checkout token + merchant reference + poll URL. |
| `get_voucher_sale_status` | Yes | Public status polling: return sale status + voucher code (only when `Completed`). |
| `confirm_voucher_web_checkout` | Yes | Guest-safe completion: poll via `poll_url`, fulfill on SUCCESS, return terminal status. |
| `retry_voucher_sale` | No | Manual re-run fulfillment for `Fulfillment Failed` or `Voucher Created` sales. |
| `fulfill_voucher_sale` | No | Single fulfillment path: create RadiusDesk voucher (only after payment confirmed), then attach invoice. Idempotent and resumable. |

---

## DocType Schemas

### Voucher Plan (`doctype/voucher_plan/voucher_plan.json`)

- **autoname**: `field:plan_name` — name is the plan_name value.
- **editable_grid**: 1 — enables inline grid editing in the list view.
- **row_format**: `Dynamic` — supports dynamic child fields.
- **Key fields**:
  - `plan_name` (Data, Required, Unique) — display/identifier name.
  - `item` (Link → Item, Required) — the item sold.
  - `company` (Link → Company, Required) — owning company.
  - `radius_realm_id` (Data, Required) — RadiusDesk realm identifier.
  - `radius_profile_id` (Data, Required) — RadiusDesk profile identifier.
  - `price` (Currency, Read-only) — synced from item's selling price list in `validate`.
  - `currency` (Link → Currency, Required) — must match company's default currency.
  - `never_expire` (Check, default "1") — if enabled, voucher never expires.
  - `sort_order` (Int, default "0") — grid ordering.

**Validation sequence** (`voucher_plan.py` `validate`):
1. `validate_currency_matches_company()` — throws if currency != company default currency.
2. `sync_price_from_item()` — calls `get_voucher_price()` to sync price from the selling price list; falls back to plan's own price if no Item Price exists.
3. `validate_price_available()` — throws if price is still None/empty after sync, with actionable message.

### Voucher Sale (`doctype/voucher_sale/voucher_sale.json`)

- **autoname**: `format:RD-VS-{YYYY}{MM}{DD}-{#####}` — date-prefixed sequential number.
- **Key fields**:
  - `plan` (Link → Voucher Plan, Required) — the associated plan.
  - `amount` (Currency, Required) — voucher amount.
  - `currency` (Link → Currency, Required) — voucher currency.
  - `phone_number` (Data) — customer phone.
  - `payment_method` (Select: EcoCash / InnBucks / Omari) — payment method.
  - `sale_source` (Select: POS / Web / Sales) — origin of the sale.
  - `status` (Select: Draft / Payment Pending / Payment Confirmed / Voucher Created / Completed / Payment Failed / Fulfillment Failed).
  - `voucher_code` (Data, Read-only) — the RadiusDesk-generated voucher code.
  - `voucher_id` (Data, Read-only) — the RadiusDesk voucher ID.
  - `radius_error` (Text, Read-only) — error message from RadiusDesk API.
  - `invoice_doctype` (Link → DocType, Dynamic Link to invoice) — e.g. "POS Invoice".
  - `invoice_name` (Dynamic Link, options = invoice_doctype) — the actual invoice name.
  - `checkout_token` (Data, Unique search index) — unguessable token for web polling.

**Status flow**: `Draft` → `Payment Pending` → (`Payment Failed` on gateway/initiation failure | `Payment Confirmed` → `Voucher Created` / `Completed`) → `Fulfillment Failed` on fulfillment error (retryable back to `Payment Confirmed`).

### Radius Desk Settings (singleton)

- **issingle**: 1 — singleton Doctype.
- **Required fields**: server_url, username, password, cloud_id, default_company, default_walkin_customer, pesepay_gateway.
- **Hotspot fields**: hotspot_login_url (exact router URL), hotspot_portal_return_prefix (pinned https return prefix).

---

## Hooks (`hooks.py`)

| Hook | Module.Path | Description |
|---|---|---|
| `doc_events` | `radius_desk.radius_desk.hooks` | Auto-create vouchers on POS Invoice / Sales Invoice submit. |
| `scheduler_events.daily` | `radius_desk.radius_desk.hooks` | `ensure_daily_pos_opening_entry` — keep the app-managed POS opening entry fresh (only when invoice_type = POS Invoice). |
| `scheduler_events.cron` | `radius_desk.radius_desk.hooks` | `retry_fulfillment_failed_sales` — every 5 min, re-run fulfillment for stale `Fulfillment Failed` sales. |
| `after_migrate` | `radius_desk.installer.after_migrate` | Idempotently sync custom fields on invoice doctypes, force POS invoice mode, ensure system user. |

---

## Tests Overview

Test suite lives in `radius_desk/tests/`. All tests extend `frappe.tests.IntegrationTestCase` and use the `guard_shared_configuration` fixture to restore mutated state after each test.

### Test Files

| File | Purpose |
|---|---|
| `test_connector.py` | Unit tests for `RadiusDeskConnector` — login, token caching, 401 refresh, base URL normalization, `find_voucher_by_extra_value`. |
| `test_voucher_sale.py` | Integration tests for `Voucher Sale` CRUD and fulfillment: web flow, POS flow, idempotency, reset, payment authorized, create_web_checkout, status endpoint, confirm paths, currency validation, auto-voucher on invoice submit. |
| `test_pos_flow_e2e.py` | End-to-end POS cashier flow: draft → sale → initiation → approval → confirmation → fulfilled → submitted Paid invoice → idempotent re-confirm. Uses real PesaPay contract stubs. |
| `test_embed_page.py` | Embed mode rendering, hotspot URL validation, injection protection, theme stylesheet, payment method cards, checkout modal structure. |
| `test_fulfillment_retry.py` | Scheduler logic: retry stale `Fulfillment Failed` sales, operator notification throttling, adopting existing vouchers from extra_value lookup, no-duplicate voucher on invoice failure. |
| `test_hotspot_embed.py` | `validate_hotspot_url` — private-IP acceptance, public-host rejection, userinfo/injection rejection, portal return prefix matching, exact-match requirement when expected URL configured, rate limiting. |
| `settings_guard.py` | Per-test fixture that snapshots and restores `Radius Desk Settings`, `_Test Company` currency, and `Account` account currency to prevent cross-test contamination. |

### Running Tests

```bash
bench --site <site-name> run-tests --app radius_desk
```

Or individually:

```bash
bench --site <site-name> run-tests --app radius_desk --module radius_desk.tests.test_hotspot_embed
bench --site <site-name> run-tests --app radius_desk --module radius_desk.tests.test_connector --test TestRadiusDeskConnector
```

---

## Development Setup

### Prerequisites

- Frappe bench installed (`pip install frappe-bench`).
- Node.js + npm for asset building (ESLint, Prettier).
- Python 3.14+.

### Install the App on a Bench Site

```bash
cd <bench-dir>
bench get-app https://github.com/stelele/frappe-radiusdesk --branch version-16
bench --site <site-name> install-app radius_desk
```

### Verify Installation

```bash
bench --site <site-name> list-apps
# Should show "Radius Desk" in the app list
```

### Common Development Commands

| Command | Description |
|---|---|
| `bench restart` | Restart bench processes after code changes. |
| `bench schedule` | Start the scheduler process. |
| `bench --site <site> enable-scheduler` | Enable scheduling on a site. |
| `bench --site <site> uninstall-app radius_desk` | Uninstall the app. |
| `bench --site <site> clear-cache` | Clear the site cache. |
| `pre-commit run --all-files` | Run ruff + eslint + prettier checks. |
| `bench get-app <url> --branch version-16` | Fetch app from GitHub at a specific branch. |

### Adding a New Whitelisted API

1. Add the method to `radius_desk/radius_desk/doctype/voucher_sale/voucher_sale.py` with `@frappe.whitelist()`.
2. Ensure the method signature and docstring follow the existing pattern.
3. Add a test in `radius_desk/tests/test_voucher_sale.py` or a new test file.
4. Run `bench --site <site> run-tests --app radius_desk` to verify.

### Adding a New DocType

1. Create the DocType JSON in `radius_desk/radius_desk/doctype/<doctype_name>/<doctype_name>.json`.
2. Create the Python controller in `radius_desk/radius_desk/doctype/<doctype_name>/<doctype_name>.py` (can be empty if no custom behavior).
3. Register the DocType in `hooks.py` if it needs special controller hooks.
4. Run `bench --site <site> migrate` to create the table.
5. Add test fixtures in `radius_desk/tests/` if needed.

### Debugging Tips

- **Token cache**: `frappe.cache.delete_value("radius_desk:token:{base_url}:{username}")` to force re-login.
- **Scheduler not running**: Start the bench scheduler: `bench --site <site> start-scheduler`.
- **Background job not running**: Check `frappe.enqueue` queue= settings; ensure the worker process is alive.
- **Permission errors**: The system user (`radius-desk-system@example.com`) must have the `Radius Desk System` role and the `Custom DocPerm` records granted via `ensure_system_user()`.
- **Hotspot URL rejected**: Run `validate_hotspot_url()` in the console to test a URL; check that it has no userinfo and a private/loopback/link-local IP hostname.

---

## Credential Storage

- **Radius Desk credentials** (server_url, username, password, cloud_id) are stored in the `Radius Desk Settings` singleton. The `password` field is a Frappe `Password` type, encrypted at rest using Frappe's built-in encryption.
- **PesaPay credentials** (integration_key, encryption_key) are stored in `Pesepay Settings` DocType, also encrypted.
- **Do not hard-code credentials** in Python code or commit them to version control. Use bench environment variables or site-specific configuration for different deployments.

---

## Pitfalls to Avoid

| Pitfall | Why It Happens | Prevention |
|---|---|---|
| Documenting non-existent methods | Copying from old docs or guessing | Verify every method exists in the actual Python source; check the `/api/method/` URL resolves. |
| Endpoint URL module mismatch | Referring to `radius_desk.api` when the module is `radius_desk.radius_desk.doctype.voucher_sale.voucher_sale` | Always trace the import path from the file where the method is defined. |
| Bench commands that don't exist | Using `bench install-app` without `--site` or `bench run-tests` without a site | Use `bench --site <site> install-app radius_desk` and `bench --site <site> run-tests --app radius_desk`. |
| TODO/TBD/[year] placeholders | Leaving placeholders from a scaffold | Remove all placeholders before committing; this doc has none. |
| Invented credential key names | Making up field names that don't exist in the Doctype | Reference the exact Doctype JSON field names (e.g. `server_url`, `username`, `password`, `cloud_id`). |
| Missing `check_permission` or `allow_guest` | Forgetting the permission flag for a public API | Review each whitelisted method: if it's called from the guest checkout, add `allow_guest=True`; if it's POS-only, keep `check_permission=True`. |
| Off-by-one in status flow | Confusing `Payment Confirmed` vs `Payment Pending` | The status flow is: `Draft` → `Payment Pending` → `Payment Confirmed` → (`Voucher Created` / `Completed`) / `Payment Failed` → `Fulfillment Failed`. Read the code carefully. |
| Forgetting to commit after `after_migrate` | Custom fields and system user not persisted | `after_migrate` in `installer.py` calls `frappe.db.commit()` at the end. |

---

## Release Notes (observed since version-16 branch)

- **v16.0.0**: Initial release on version-16 branch.
- Auto-voucher on POS/Sales Invoice submit (replaces old POS "Buy Voucher" button).
- PesaPay seamless payment integration (EcoCash, InnBucks, Omari).
- Hotspot captive-portal URL validation with exact-pin matching and portal return prefix.
- Guest web checkout with status polling and voucher code fragment delivery.
- Fulfillment retry scheduler with operator notification throttling.
- POS receipt voucher code summary with polling fallback.
- System user with minimal permissions for privileged background work.
- Idempotent voucher creation via `find_voucher_by_extra_value` duplicate guard.
- Rate limiting per-phone and per-IP for abuse prevention.