# Guest Portal Theme + Payment Selector — Design

Date: 2026-09-15 (rev 2 — post adversarial review)
App: `radius_desk` (Frappe v16)
Author: Gift Mugweni

## Summary

Restyle the guest voucher portal (`/voucher-checkout/`, both normal and
embed modes) to match the MikroTik hotspot login page — dark navy gradient,
white text, teal accents — so the handoff between hotspot and portal feels
like one product. Replace the payment-method dropdown with three tappable
cards in the same theme. No API or fulfillment changes.

## Goals / non-goals

Goals:
- Portal visually matches the hotspot page (palette, buttons, cards, inputs).
- Payment method becomes 3 tappable cards, uniform hotspot style, teal
  selected state, EcoCash preselected, per-method hints preserved.
- Works in both normal (website chrome) and embed (bare) modes.
- Print receipt still prints clean black-on-white.
- Failure dialogs (`frappe.msgprint`) and status alerts remain legible.

Non-goals:
- Any change to `create_web_checkout` args, fulfillment, polling, or security model.
- Sharing CSS assets with the router (hotspot pages are served offline from
  the router; the theme is ported, not shared).
- Card logos / provider brand colors (uniform theme chosen).

## Decisions (from brainstorm + review)

| # | Decision |
|---|----------|
| 1 | Theme everything: dark theme covers page content AND website chrome (navbar, footer, breadcrumbs, modal), scoped to this page only. |
| 2 | Payment cards uniform hotspot style (dark card, white text, teal border/glow when selected). |
| 3 | Keep EcoCash preselected; keep `METHOD_HINTS` behavior unchanged. |
| 4 | Selector contract is `body[data-path="voucher-checkout"]` (verified: `base.html` renders `data-path`; page content alone cannot reach navbar/footer/modal). |

## Architecture

```
radius_desk/www/voucher-checkout/
├─ index.html      # <select id="payment-method"> → radiogroup of 3 buttons
│                 # (same value strings: EcoCash / InnBucks / Omari)
├─ index.js       # selected_method state + click handler (exact diff below)
├─ hotspot-theme.css  (NEW, in public/css/) — ported palette:
│                     bg gradient #1a1a2e→#16213e→#0f3460, text #fff,
│                     primary #1EBAB9, success #97CC22
│                     loaded via {% block style %} in both bases,
│                     referenced with a ?v= cache-buster
└─ embed_base.html (unchanged structure; gains the stylesheet link)

No Frappe app code changes beyond the page assets (the notify-throttle
recovery in `fulfillment_retry.py` already shipped — verified present).
```

## Component changes

### Theme stylesheet (NEW file: `public/css/hotspot-theme.css`)

- Scoped root: `body[data-path="voucher-checkout"]` — the ONLY selector
  that reaches page content, navbar, footer, breadcrumbs, AND body-level
  overlays (modal, backdrop) without bleeding onto other website pages.
- Explicit surface list (every rule verified against actual markup):
  page gradient + text; `.voucher-checkout .card`, `.rd-embed` panels;
  `.btn-primary` (+`:hover`, `:disabled`), `.btn-outline-secondary`
  (print button), `.btn-block`; `.alert-info/.alert-danger/.alert-success`;
  `.form-control` + `:focus` ring + `::placeholder` contrast +
  `:-webkit-autofill`; `.text-muted` → `rgba(255,255,255,.6)` (plan
  descriptions) and `.text-danger` (required marker); `a` → `#1EBAB9`;
  `.navbar`, `.navbar .dropdown-menu`, `.web-footer`, `.breadcrumb`;
  `.modal-content`, `.modal-backdrop` (msgprint dialogs stay legible —
  explicit stance: themed dark, not left light);
  `#voucher-code` result panel; `#selected-plan.form-control`.
- Loading: `{% block style %}` override in `index.html` (both bases inherit
  `templates/base.html`, which defines the block). Cache-buster query string
  so captive-portal mini-browsers pick up redeploys.

### Print (explicit reset — the existing block only hides chrome)

```css
@media print {
  body[data-path="voucher-checkout"] .voucher-checkout,
  body[data-path="voucher-checkout"] .rd-embed,
  body[data-path="voucher-checkout"] .card,
  body[data-path="voucher-checkout"] .alert {
    background: #fff !important; color: #000 !important;
    border-color: #000 !important; box-shadow: none !important;
  }
}
```
Receipt prints black-on-white; chrome stays hidden per the existing block.

### Payment selector (exact JS contract)

```js
let selected_method = "EcoCash";                       // was: const $method = $("#payment-method")
function show_method_hint(m) { $methodHint.text(METHOD_HINTS[m] || ""); }
show_method_hint(selected_method);
$(".rd-method-card").on("click", function () {
  selected_method = $(this).data("method");
  $(".rd-method-card").attr("aria-checked", "false").removeClass("selected");
  $(this).attr("aria-checked", "true").addClass("selected");
  show_method_hint(selected_method);
});
// create_web_checkout args change: payment_method: selected_method
```
- HTML: `<div role="radiogroup" aria-label="Payment method">` wrapping three
  `<button type="button" class="rd-method-card" role="radio"
  aria-checked data-method="...">`; EcoCash card starts
  `aria-checked="true" class="selected"`. Plus a synced hidden
  `<input type="hidden" id="payment-method" value="EcoCash">` as the no-JS
  fallback channel (JS keeps it in sync; server ignores it — single source
  of truth stays the API arg).
- Selection survives plan re-selection (method state is independent of the
  plan-card click handler).
- No-JS posture: cards are inert without JS (same as today's plan-select
  buttons, which also require JS); the hidden input preserves the default.

### What explicitly does NOT change

- `create_web_checkout` signature, validation, and allowed values
  (`EcoCash`/`InnBucks`/`Omari`); `checkout_token` flow; polling;
  `deliver_code`; rate limits; fulfillment; Voucher Sale lifecycle.
- `embed_base.html` structure.

## Error handling

- No-method-selected is impossible by construction (EcoCash preselected;
  single-select, exactly one `aria-checked="true"` at all times).
- Server still rejects unknown `payment_method` (existing combo validation);
  the negative path is now explicitly tested (see below).

## Testing

Automated (bench pytest):
- Template asserts: exactly 3 `.rd-method-card` elements, EcoCash card
  `aria-checked="true"`, hidden fallback input present with value `EcoCash`.
- `METHOD_HINTS` parity: hint text for each of the 3 values matches the map.
- Negative: `create_web_checkout(..., payment_method="FakePay")` fails
  validation (proves the server doesn't trust the new client control).
- Existing portal tests stay green; run the module to catch regressions.

Manual QA (live site, desktop + phone, normal + `?embed=1`):
- Each method selectable, teal selected state, correct hint text.
- Trigger `frappe.msgprint` ("Select a plan first", empty phone) — dialog
  legible in dark mode.
- One real EcoCash purchase end-to-end; confirm `Voucher Sale.payment_method`
  persisted; print receipt → clean black-on-white from both modes.
- Staff observation: watch 3 real customer buys, note hesitation points
  (validates the "one product" premise beyond cosmetics).

## Limitations

- Theme is a port, not a shared asset: future hotspot-page rebrands must be
  manually mirrored in `hotspot-theme.css`.
- No separate mini-browser layout (vertical stack works everywhere).
- Cross-domain trust (`jh.erpnext.com` URL) is out of scope for CSS — noted,
  accepted.
