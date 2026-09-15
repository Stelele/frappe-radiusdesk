# Portal Theme + Payment Selector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restyle the guest voucher portal to match the hotspot login page and replace the payment dropdown with three tappable cards, with zero behavior change to checkout, polling, or fulfillment.

**Architecture:** One new stylesheet (`public/css/hotspot-theme.css`, ported hotspot tokens) loaded via `{% block style %}` in both modes, scoped to `body[data-path="voucher-checkout"]`; the `<select>` becomes a radiogroup of buttons with identical value strings; JS swaps `$method.val()` reads for a `selected_method` variable.

**Tech Stack:** Frappe v16 www pages (Jinja, jQuery, `frappe.call`), Python 3.14, ruff (line-length 110, tab indent, double quotes), bench pytest (`IntegrationTestCase`).

**Design doc:** `docs/superpowers/specs/2026-09-15-portal-theme-payment-selector-design.md`

**Environment notes (read first):**
- Bench at `/home/gift/Documents/code-projects/frappe/v16`. App root: `/home/gift/Documents/code-projects/frappe/v16/apps/radius_desk`.
- Tests: `bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.<module>` (allow_tests already enabled).
- Public assets serve at `/assets/radius_desk/...` after `bench build`; Frappe Cloud rebuilds on deploy. Bump the `?v=` date in index.html whenever the CSS changes.
- `{% block style %}` exists in `frappe/templates/base.html:31` and is NOT overridden by `templates/web.html` or `embed_base.html`, so defining it in `index.html` applies in both modes.
- `<body data-path="{{ path }}">` is rendered by `base.html:57`; the checkout page's path is `voucher-checkout`.

---

### Task 1: Theme stylesheet

**Files:**
- Create: `radius_desk/public/css/hotspot-theme.css`
- Test: `radius_desk/tests/test_embed_page.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `radius_desk/tests/test_embed_page.py`, next to the existing render tests (which use the `self._get(path)` thread-based helper — copy that pattern exactly):

```python
def test_theme_stylesheet_linked_in_both_modes(self):
    for path in ("/voucher-checkout/", "/voucher-checkout/?embed=1"):
        html = self._get(path)
        self.assertIn("/assets/radius_desk/css/hotspot-theme.css", html)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.test_embed_page`
Expected: FAIL on `test_theme_stylesheet_linked_in_both_modes` (`AssertionError`, link absent)

- [ ] **Step 3: Create the stylesheet**

Create `radius_desk/public/css/hotspot-theme.css` with the complete content below (tokens ported from `hotspot/css/style.css` and `login.html`: gradient `#1a1a2e→#16213e→#0f3460`, text `#fff`, primary `#1EBAB9`, dark button text `#06232a`, success `#97CC22`, radius 8px, inputs `rgba(255,255,255,.8)`):

```css
/* Njeremoto hotspot theme for the guest voucher portal. Scoped to
body[data-path="voucher-checkout"] so no other website page is affected. */
body[data-path="voucher-checkout"] {
	background: #1a1a2e;
	background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
	color: #fff;
	min-height: 100%;
}
body[data-path="voucher-checkout"] .voucher-checkout,
body[data-path="voucher-checkout"] .rd-embed {
	color: #fff;
}
body[data-path="voucher-checkout"] .voucher-checkout .card,
body[data-path="voucher-checkout"] .rd-embed .card {
	background: rgba(255, 255, 255, 0.08);
	border: 1px solid rgba(255, 255, 255, 0.15);
	border-radius: 8px;
	color: #fff;
}
body[data-path="voucher-checkout"] .btn-primary {
	background: #1EBAB9;
	border-color: #1EBAB9;
	color: #06232a;
	font-weight: 600;
}
body[data-path="voucher-checkout"] .btn-primary:hover {
	background: #25d0cf;
	border-color: #25d0cf;
	color: #06232a;
}
body[data-path="voucher-checkout"] .btn-primary:disabled {
	background: rgba(30, 186, 185, 0.4);
	border-color: transparent;
	color: #06232a;
}
body[data-path="voucher-checkout"] .btn-outline-secondary {
	border-color: rgba(255, 255, 255, 0.4);
	color: #fff;
}
body[data-path="voucher-checkout"] .form-control {
	height: 44px;
	border-radius: 6px;
	border: 1px solid transparent;
	background-color: rgba(255, 255, 255, 0.85);
	color: #1a1a2e;
}
body[data-path="voucher-checkout"] .form-control:focus {
	border-color: #1EBAB9;
	box-shadow: 0 0 0 2px rgba(30, 186, 185, 0.5);
	background-color: #fff;
	color: #1a1a2e;
}
body[data-path="voucher-checkout"] .form-control::placeholder {
	color: rgba(26, 26, 46, 0.55);
}
body[data-path="voucher-checkout"] .text-muted {
	color: rgba(255, 255, 255, 0.6) !important;
}
body[data-path="voucher-checkout"] .text-danger {
	color: #ff6b6b !important;
}
body[data-path="voucher-checkout"] a {
	color: #1EBAB9;
}
body[data-path="voucher-checkout"] .alert-info {
	background: rgba(30, 186, 185, 0.15);
	border: 1px solid #1EBAB9;
	color: #fff;
}
body[data-path="voucher-checkout"] .alert-danger {
	background: rgba(180, 40, 40, 0.25);
	border: 1px solid #e05252;
	color: #fff;
}
body[data-path="voucher-checkout"] .alert-success {
	background: rgba(151, 204, 34, 0.15);
	border: 1px solid #97CC22;
	color: #fff;
}
body[data-path="voucher-checkout"] #voucher-code {
	border: 2px dashed #97CC22;
	border-radius: 6px;
}
body[data-path="voucher-checkout"] .navbar {
	background: #141428 !important;
}
body[data-path="voucher-checkout"] .navbar .navbar-brand,
body[data-path="voucher-checkout"] .navbar .nav-link {
	color: #fff !important;
}
body[data-path="voucher-checkout"] .navbar .dropdown-menu {
	background: #1a1a2e;
}
body[data-path="voucher-checkout"] .web-footer {
	background: #141428;
	color: rgba(255, 255, 255, 0.6);
}
body[data-path="voucher-checkout"] .breadcrumb {
	background: transparent;
}
body[data-path="voucher-checkout"] .breadcrumb a {
	color: #1EBAB9;
}
body[data-path="voucher-checkout"] .modal-content {
	background: #1a1a2e;
	color: #fff;
	border: 1px solid rgba(255, 255, 255, 0.15);
	border-radius: 8px;
}
body[data-path="voucher-checkout"] .modal-backdrop {
	background: #0f3460;
}
body[data-path="voucher-checkout"] .modal-header .close,
body[data-path="voucher-checkout"] .modal-header .btn-close {
	color: #fff;
}
/* Payment method cards (Task 3 markup). */
body[data-path="voucher-checkout"] .rd-method-card {
	display: block;
	width: 100%;
	margin: 0 0 10px;
	padding: 12px;
	border: 1px solid rgba(255, 255, 255, 0.15);
	border-radius: 8px;
	background: rgba(255, 255, 255, 0.08);
	color: #fff;
	font-family: inherit;
	font-size: 15px;
	font-weight: 600;
	text-align: center;
	cursor: pointer;
}
body[data-path="voucher-checkout"] .rd-method-card.selected {
	border-color: #1EBAB9;
	box-shadow: 0 0 0 2px rgba(30, 186, 185, 0.5);
}
body[data-path="voucher-checkout"] .rd-method-card:focus-visible {
	outline: 2px solid #1EBAB9;
	outline-offset: 2px;
}
@media print {
	body[data-path="voucher-checkout"],
	body[data-path="voucher-checkout"] .voucher-checkout,
	body[data-path="voucher-checkout"] .rd-embed,
	body[data-path="voucher-checkout"] .card,
	body[data-path="voucher-checkout"] .alert {
		background: #fff !important;
		color: #000 !important;
		border-color: #000 !important;
		box-shadow: none !important;
	}
}
```

- [ ] **Step 4: Wire the stylesheet into `index.html`**

In `radius_desk/www/voucher-checkout/index.html`, add after the extends line (line 1), before `{% block title %}`:

```jinja
{% block style %}{{ super() }}<link rel="stylesheet" href="/assets/radius_desk/css/hotspot-theme.css?v=20260915">{% endblock %}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.test_embed_page`
Expected: PASS (production CSS is served by bench; if the link 404s locally, run `bench build --app radius_desk` first, then re-run)

- [ ] **Step 6: Commit**

```bash
git add radius_desk/public/css/hotspot-theme.css radius_desk/www/voucher-checkout/index.html radius_desk/tests/test_embed_page.py
git commit -m "feat: hotspot-matching theme for guest voucher portal"
```

---

### Task 2: Payment method cards (HTML)

**Files:**
- Modify: `radius_desk/www/voucher-checkout/index.html:60-68`
- Test: `radius_desk/tests/test_embed_page.py` (extend)

- [ ] **Step 1: Write the failing test**

```python
def test_payment_method_cards_rendered(self):
    html = self._get("/voucher-checkout/")
    self.assertEqual(html.count('class="rd-method-card'), 3)
    for method in ("EcoCash", "InnBucks", "Omari"):
        self.assertIn(f'data-method="{method}"', html)
    self.assertIn(
        '<button type="button" class="rd-method-card selected" role="radio" '
        'aria-checked="true" data-method="EcoCash">EcoCash</button>',
        html,
    )
    self.assertIn('id="payment-method-fallback" value="EcoCash"', html)
    # NOTE: the quoted id below does NOT match id="payment-method-fallback".
    self.assertNotIn('id="payment-method"', html)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.test_embed_page`
Expected: FAIL (no `.rd-method-card` in HTML)

- [ ] **Step 3: Replace the select with the radiogroup**

In `radius_desk/www/voucher-checkout/index.html`, replace lines 60-68 (the `Payment Method` form-group containing `<select class="form-control" id="payment-method">` with its 3 `<option>`s) with:

```html
	<div class="form-group">
		<label>{{ _("Payment Method") }}</label>
		<div role="radiogroup" aria-label="{{ _('Payment method') }}">
			<button type="button" class="rd-method-card selected" role="radio" aria-checked="true" data-method="EcoCash">EcoCash</button>
			<button type="button" class="rd-method-card" role="radio" aria-checked="false" data-method="InnBucks">InnBucks</button>
			<button type="button" class="rd-method-card" role="radio" aria-checked="false" data-method="Omari">Omari</button>
		</div>
		<input type="hidden" id="payment-method-fallback" value="EcoCash">
		<div id="method-hint" class="small text-muted mt-1"></div>
	</div>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.test_embed_page`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add radius_desk/www/voucher-checkout/index.html radius_desk/tests/test_embed_page.py
git commit -m "feat: tappable payment method cards replace dropdown"
```

---

### Task 3: Payment method cards (JS)

**Files:**
- Modify: `radius_desk/www/voucher-checkout/index.js:15-17,20-31,91`
- Test: manual QA (no JS test runner in repo) + `node --check`

- [ ] **Step 1: Rewrite the selector logic**

In `radius_desk/www/voucher-checkout/index.js`, replace lines 15-17 (`const $method = $("#payment-method");` — delete that line) and lines 20-31 with:

```js
	const METHOD_HINTS = {
		EcoCash: "Approve on your phone with your EcoCash PIN — works without mobile data.",
		InnBucks: "The InnBucks app needs mobile data to approve — keep mobile data ON.",
		Omari: "The Omari app needs mobile data to approve — keep mobile data ON.",
	};
	const $methodHint = $("#method-hint");
	const $methodFallback = $("#payment-method-fallback");
	let selected_method = "EcoCash";

	function show_method_hint(m) {
		$methodHint.text(METHOD_HINTS[m] || "");
	}
	show_method_hint(selected_method);
	$methodFallback.val(selected_method);
	$(".rd-method-card").on("click", function () {
		selected_method = $(this).data("method");
		$(".rd-method-card").attr("aria-checked", "false").removeClass("selected");
		$(this).attr("aria-checked", "true").addClass("selected");
		$methodFallback.val(selected_method);
		show_method_hint(selected_method);
	});
```

Change line 91 `args: { plan: selected_plan, phone_number: phone, payment_method: $method.val() },` to:

```js
			args: { plan: selected_plan, phone_number: phone, payment_method: selected_method },
```

Verify no remaining references to `$method` (other than `$methodHint`/`$methodFallback`) with: `rg -n '\$method\b' radius_desk/www/voucher-checkout/index.js` → must return nothing.

- [ ] **Step 2: Syntax-check**

Run: `node --check radius_desk/www/voucher-checkout/index.js`
Expected: silent (valid syntax)

- [ ] **Step 3: Commit**

```bash
git add radius_desk/www/voucher-checkout/index.js
git commit -m "feat: drive payment method from tappable cards"
```

---

### Task 4: Server-side negative test + full verification

**Files:**
- Test: `radius_desk/tests/test_voucher_sale.py` (extend)

- [ ] **Step 1: Write the failing test**

Append to `TestVoucherSale` in `radius_desk/tests/test_voucher_sale.py`, mirroring the existing `test_initiate_rejects_unsupported_method_currency` mock pattern:

```python
def test_create_web_checkout_rejects_unknown_method(self):
    """The card selector must not smuggle unvalidated values: an unknown
    payment_method fails validation before any gateway call."""
    with (
        patch("radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.make_seamless_payment") as mock_pay,
        patch("radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.get_pesepay_mode_of_payment"),
    ):
        with self.assertRaisesRegex(frappe.ValidationError, "does not support"):
            vs.create_web_checkout(self.plan.name, "0777777777", "FakePay")

    mock_pay.assert_not_called()
```

- [ ] **Step 2: Run test to verify it passes**

Run: `bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.test_voucher_sale`
Expected: PASS (server validation is unchanged; this locks it in)

- [ ] **Step 3: Run the full app suite + lint**

Run: `bench --site development.localhost run-tests --app radius_desk`
Expected: all green (62+ tests, per prior baseline)
Run: `env/bin/ruff check apps/radius_desk && env/bin/ruff format --check apps/radius_desk` from the bench root
Expected: clean (fix any findings; pre-existing findings stay untouched)

- [ ] **Step 4: Manual QA on the live site** (after deploy)

Normal mode + `?embed=1`, desktop + phone: each method selectable with teal state + correct hint; trigger msgprint dialogs (empty plan/phone) and confirm legibility; one real EcoCash purchase end-to-end (confirm `Voucher Sale.payment_method` persisted); print receipt from both modes (black-on-white); staff watches 3 real customer buys for hesitation points.

- [ ] **Step 5: Commit**

```bash
git add radius_desk/tests/test_voucher_sale.py
git commit -m "test: unknown payment method rejected before gateway"
```
