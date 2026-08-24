# Radius Desk Voucher Sales — Design

Date: 2026-08-18
App: `radius_desk` (Frappe v16)
Author: Gift Mugweni

## Summary

A Frappe app that lets a hotspot operator sell RadiusDesk internet vouchers
through two surfaces — the ERPNext POS cashier screen and a public
self-service checkout page — paying via PesaPay first, and only creating the
RadiusDesk voucher *after* PesaPay confirms the payment. Every sale
acknowledges with a POS Invoice.

## Goals / non-goals

Goals:
- Sell voucher plans through both ERPNext POS and a public web checkout.
- Collect payment via PesaPay mobile money (EcoCash / InnBucks / Omari) before
  creating the voucher.
- Create the voucher on the RadiusDesk v4 (cake4) server only after PesaPay
  confirms.
- Return the voucher code to the customer.
- Generate a submitted POS Invoice for every sale (walk-in customer on web
  self-checkout, the in-progress invoice on POS).
- POS cashier may accept any Mode of Payment on the POS Profile (including
  cash); the voucher is then created directly without a PesaPay initiation.
  Only self-checkout is restricted to PesaPay.

Non-goals:
- SMS / email delivery of the voucher code (screen + printable receipt only).
- Managing RadiusDesk realms/profiles from Frappe (configured by ID on the plan).
- Refunds / voucher revocation.
- Card (Visa / MasterCard) or other non-mobile-money PesaPay methods.

## Existing infrastructure (reused, not rebuilt)

The `pesepay` app already provides:
- `PesePayConnector` (seamless + redirect payments, status check, poll).
- `make_seamless_payment()` whitelisted method that creates an
  `Integration Request` keyed to `reference_doctype` / `reference_docname`.
- A webhook callback and a scheduler (`poll_pending_payments`) that, on
  payment success, call `on_payment_authorized("Completed")` on the reference
  document. **This gives us the payment-confirmation safety net for free.**
- POS client integration and Mode-of-Payment / Payment Gateway Account setup.

`radius_desk` reuses `pesepay`'s whitelisted methods to initiate and poll
payments and reuses its `Pesepay Settings` configuration. It does not modify
the `pesepay` app.

## Data model

### `Radius Desk Settings` (Single)
- `server_url` (Data, reqd) — RadiusDesk base URL (with or without
  `/cake4/rd_cake`; normalized in the connector)
- `username` (Data, reqd)
- `password` (Password, reqd)
- `cloud_id` (Data, reqd)
- `default_company` (Link Company, reqd)
- `default_walkin_customer` (Link Customer, reqd) — used by web self-checkout
- `pesepay_gateway` (Link Payment Gateway, filtered to Pesepay Settings) —
  which gateway + Mode of Payment voucher sales use

### `Voucher Plan` (catalog)
- `enabled` (Check, default 1), `plan_name` (Data, unique, reqd)
- `description` (Text)
- `item` (Link Item, reqd), `company` (Link Company, reqd)
- `radius_realm_id` (Data, reqd), `radius_profile_id` (Data, reqd)
- `price` (Currency, reqd), `currency` (Link Currency, reqd)
- `never_expire` (Check, default 1), `sort_order` (Int, default 0)

### `Voucher Sale` (transaction record)
- `plan` (Link Voucher Plan, reqd), `amount` (Currency), `currency`
- `phone_number`, `payment_method`
- `sale_source` (Select: POS / Web)
- `payment_gateway` (Link Payment Gateway)
- `merchant_reference` (Data) — the PesaPay Integration Request name
- `pesepay_reference_number` (Data), `poll_url` (Data)
- `status` (Select):
  - `Draft`
  - `Payment Pending`
  - `Payment Confirmed`
  - `Voucher Created`
  - `Completed` (terminal success)
  - `Payment Failed` (terminal)
  - `Fulfillment Failed` (retryable — money taken, voucher not created)
- `voucher_code` (Data), `voucher_id` (Data)
- `radius_error` (Text)
- `pos_invoice` (Link POS Invoice)
- `checkout_token` (Data, random UUID) — auth token for the guest web polling
  endpoint

`Voucher Sale.on_payment_authorized("Completed")` — called by the pesepay
webhook/scheduler — sets status `Payment Confirmed` and runs fulfillment.

### Custom fields on invoices
- `radius_voucher_code` (Data) on `POS Invoice` and `Sales Invoice`
- `radius_voucher_plan` (Link Voucher Plan) on `POS Invoice` and `Sales Invoice`

So the voucher code is visible on the invoice, survives conversion, and can be
printed on the POS receipt.

### App-managed POS infrastructure (for web-created POS Invoices)
Web self-checkout invoices are real **POS Invoices**, which ERPNext only lets you
submit against an **open POS Opening Entry dated today** for the invoice's POS
Profile. The app therefore manages its own POS infrastructure so the operator
never touches POS setup:

- `ensure_pos_profile(company)` creates/maintains a dedicated POS Profile
  `RD Web - {abbr}` (company defaults + the Pesepay mode as payment mode).
- `ensure_today_open_pos_entry(company, mode)` returns today's **open POS
  Opening Entry** for that profile, creating it if missing. When the date
  rolls over it first closes the prior open entry with a **POS Closing Entry**
  built via ERPNext's `make_closing_entry_from_opening` (the platform's
  standard POS close, which consolidates the period's POS invoices into POS
  Invoice Merge Logs), then opens today's entry.
- Opening/closing entries use the `Guest` user — a system-owned cashier that
  can never hold a real POS session — so the app's entries never collide with
  a human cashier's POS session, and the closing entry's `owner == user`
  validation passes. Web POS invoices are likewise created under the `Guest`
  user (via a scoped `frappe.set_user`).
- A **daily scheduler hook** pre-creates each day's entry; the fulfillment
  path calls the same helper lazily so a first sale of the day still works.

## Server side

### `RadiusDeskConnector` (`radius_desk.radius_desk.utils.radiusdesk` or
`radius_desk/radius_desk/connectors/radiusdesk.py`)
Mirrors the `PesePayConnector` pattern (own HTTP client, no external package):
- Base URL normalization: append `/cake4/rd_cake` if absent.
- `login()` → `POST {base}/dashboard/authenticate.json` with
  `auto_compact=false`, `username`, `password` → returns `data.token`.
  Token cached via `frappe.cache` (keyed by server url + username), reused as
  both the `token` query param and the `Token` cookie.
- `create_voucher(realm_id, profile_id, never_expire=True, extra_value="")` →
  `POST {base}/vouchers/add.json` with form data:
  `single_field=true`, `realm_id`, `profile_id`, `quantity=1`,
  `never_expire=on|off`, `extra_name`, `extra_value`, `token`,
  `sel_language=4_4`, `cloud_id`; cookie `Token`.
  Returns the voucher dict `{id, name}` where `name` is the voucher code.
- `extra_value` carries the Voucher Sale name for traceability.
- On token rejection (401 / `success=false`), refresh token once and retry.
- Exceptions raise a `RadiusDeskException`; HTTP via `get_request_session()`.

### `fulfill_voucher_sale(sale)` — the single fulfillment path
Whitelisted (authed). Used by POS, web, webhook, scheduler, and manual retry.
Idempotent:
1. If status is `Completed` or `Voucher Created`, return the existing code.
2. Require status `Payment Confirmed` (or `Fulfillment Failed` retry), else
   raise.
3. Resolve plan + `Radius Desk Settings`; build connector; call
   `create_voucher()`. Store `voucher_code`, `voucher_id`; status
   `Voucher Created`. On exception: status `Fulfillment Failed`,
   `radius_error` = traceback, raise for caller.
4. If web flow (no `pos_invoice` yet): create and submit a POS Invoice:
   - customer = `default_walkin_customer`, company = plan.company
   - item = plan.item, qty 1, rate = plan.price
   - payment row: `pesepay_gateway`'s Mode of Payment, amount = plan.price
   - `radius_voucher_code` = code, `radius_voucher_plan` = plan
5. Link `pos_invoice` on the sale; status `Completed`.
6. Return `{voucher_code, voucher_id, pos_invoice}`.

### `initiate_voucher_payment(sale, phone, method)` — whitelisted
Calls `pesepay.templates.pages.pesepay_checkout.make_seamless_payment` with
`reference_doctype="Voucher Sale"`, `reference_docname=sale.name`, the
configured `pesepay_gateway`, plan currency/amount, phone and method. Stores
the returned `merchant_reference`, `poll_url`, `reference_number` on the sale,
sets status `Payment Pending`. The webhook + scheduler then trigger
`on_payment_authorized` → fulfillment automatically.

### Whitelisted API surface
- `get_voucher_plans()` — guest; enabled plans for the web checkout page.
- `get_voucher_plans_for_pos()` — authed; enabled plans mapped by Item for the
  POS client script.
- `create_voucher_sale(plan, phone, source)` — authed (POS); creates a Draft
  sale linked to the invoice later.
- `create_web_checkout(plan, phone, method)` — guest; creates a sale (Web),
  calls `initiate_voucher_payment`, returns `checkout_token`, merchant ref,
  poll URL.
- `confirm_voucher_payment(merchant_reference)` — authed (POS client); marks
  `Payment Confirmed` and runs fulfillment; returns voucher code.
- `get_voucher_sale_status(checkout_token)` — guest; web polling. Returns
  status and, once `Completed`, the voucher code + a print view link.
- `retry_voucher_sale(sale)` — authed; re-runs fulfillment from the form.

## POS surface

`radius_desk/public/js/radius_desk_pos.js` mounted on `POS Invoice` (and
`Sales Invoice`) via `doctype_js` in hooks:
- Detect a cart line whose item maps to an enabled `Voucher Plan`. If present,
  hide the existing pesepay "Pay with Pesepay" button and mount a
  "Buy Voucher" button in the payment panel.
- On click: dialog (payment mode + phone number) → save invoice → create
  Voucher Sale (linked to invoice) →
  - **PesaPay mode** (EcoCash / InnBucks / Omari on the POS Profile):
    `initiate_voucher_payment()` → poll PesaPay via the existing
    `poll_payment_status` / `poll_payment_reference` methods → on SUCCESS →
    `confirm_voucher_payment()` → server creates the voucher and returns the
    code.
  - **Any other POS Profile mode** (cash etc.): `confirm_voucher_sale()` —
    the cashier has already collected the money at the till, so the Draft sale
    is confirmed and the voucher created directly, without a PesaPay
    initiation.
- Either way: stamp `radius_voucher_code` on the invoice → show the code in a
  dialog (cashier reads it to the customer) → record the payment row for the
  chosen mode → submit via `cur_pos.payment.events.submit_invoice()`.
- Receipt: the POS print format renders `radius_voucher_code`.

## Web checkout surface

`www/voucher-checkout/` page (guest, `no_cache=1`):
1. Renders enabled `Voucher Plan`s (name, description, price, currency).
2. Customer picks a plan, enters phone + payment method.
3. `create_web_checkout()` → sale created, payment initiated, returns
   `checkout_token`.
4. JS polls `get_voucher_sale_status(token)` every 3 s.
5. On `Completed`: show the voucher code prominently + "print receipt" button.
6. On `Payment Failed`: show declined message.
7. On `Fulfillment Failed`: show "payment received, voucher failed — contact
   support" (admin retries from the Sale form).

## Error handling & edge cases

- **Money taken, voucher failed**: status `Fulfillment Failed`, `radius_error`
  logged, retry button on the Sale form re-runs fulfillment.
- **Voucher created but response lost**: `extra_value` stores the sale name;
  retry checks for an existing voucher for that sale before re-creating
  (avoid duplicates).
- **Double confirmation** (client poll + webhook + scheduler): `fulfill_voucher_sale`
  is idempotent; IR status dedupe already exists in pesepay.
- **Unsupported currency / amount**: delegated to `Pesepay Settings`
  validation.
- **POS submit race**: voucher code stamped before submit; submit is
  single-flight via the existing POS flow.

## Security

- Guest endpoints are allow-listed and minimal: plan list, web checkout
  creation, and status-by-token. The voucher code is only returned for
  `Completed` sales via a random `checkout_token` (no enumeration).
- Fulfillment, confirmation, and retry are authed-only.
- Connector follows the pesepay SSRF guard pattern where URLs are constructed
  from configured `server_url` only.

## Testing

- Unit tests with mocked RadiusDesk HTTP (login, voucher create, token
  refresh) and mocked PesaPay.
- `fulfill_voucher_sale` lifecycle: draft → pending → confirmed → voucher
  created → completed; idempotency on repeat call; Fulfillment-Failed retry.
- Web-checkout invoice creation (walk-in customer, item, payment row, code).
- `get_voucher_sale_status` returns code only when completed; token mismatch
  rejected.
- Manual smoke test: POS flow with a live PesaPay sandbox + a dev RadiusDesk
  instance.

## Decisions
- POS receipt: the voucher code is BOTH printed on the POS receipt (via the
  `radius_voucher_code` field) and shown in a dialog for the cashier.
- SMS / email delivery of the voucher code is out of scope for v1; voucher is
  shown on screen and printable.