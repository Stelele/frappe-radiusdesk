# Hotspot-Embedded Voucher Purchase Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> **STATUS NOTE (2026-09-13):** This plan is the historical execution record.
> During Task 6 the design changed: the Frappe Cloud edge sends
> `X-Frame-Options: SAMEORIGIN` site-wide, so the **iframe embed was dropped —
> the Buy tab is a full-page CTA link** and the `#rd-voucher=` fragment
> redirect is the PRIMARY delivery mechanism. Task 6 below still shows the
> original iframe tab; what actually shipped is in the hotspot-cafe-config
> history. The authoritative design is the spec at
> `docs/superpowers/specs/2026-09-12-hotspot-embed-voucher-purchase-design.md`
> (rev 3). The dormant postMessage path now requires `linklogin` and posts
> only to the router's origin.

**Goal:** Embed the guest voucher portal into the MikroTik hotspot login page and auto-login the customer with the voucher they just bought (postMessage from iframe, URL-fragment redirect from full-page fallback), with rate limits, fulfilment retry, and walled-garden config.

**Architecture:** One code-delivery mechanism, two transports: the portal page (`/voucher-checkout/`) gains an `?embed=1` mode (bare base template) that, on payment success, either postMessages the voucher code to the embedding parent (referrer-origin target only, never `'*'`) or redirects the top window to `{linklogin}#rd-voucher=CODE`. The hotspot `login.html` receives the code (message listener or hash reader), stashes it in `sessionStorage`, and PAP-submits its own form, with a rescue banner and auto-retry on reload. Guest APIs get rate limits + outbound-poll backoff; a scheduler retries Fulfillment Failed sales and emails System Managers.

**Tech Stack:** Frappe v16 www pages (Jinja, jQuery, `frappe.call`), Python 3.14, ruff (line-length 110, tab indent, double quotes), RouterOS hotspot HTML, bench pytest (`IntegrationTestCase`).

**Design doc:** `docs/superpowers/specs/2026-09-12-hotspot-embed-voucher-purchase-design.md`

**Environment notes (read first):**
- Two repos: Frappe app at `/home/gift/Documents/code-projects/frappe/v16/apps/radius_desk` (main worktree, bench at `/home/gift/Documents/code-projects/frappe/v16`, site `development.localhost`), and router config at `/home/gift/Documents/code-projects/hotspot-cafe-config` (Tasks 6-7).
- Tests: `bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.<module>` (allow_tests already enabled).
- App package layout: utils live in `radius_desk/radius_desk/radius_desk/utils/`; www page in `radius_desk/www/voucher-checkout/`.
- Git commits: include per convention; if the environment denies git, skip and continue.
- The MikroTik tasks are code-reviewed manually (no test harness for router files); deployment happens later via the rollout checklist in the spec, NOT in this plan.

---

### Task 1: Hotspot embed guards — URL validator + rate limiter

**Files:**
- Create: `radius_desk/radius_desk/utils/hotspot_embed.py`
- Test: `radius_desk/tests/test_hotspot_embed.py`

- [ ] **Step 1: Write the failing tests**

Create `radius_desk/tests/test_hotspot_embed.py`:

```python
import uuid

import frappe
from frappe.tests import IntegrationTestCase

from radius_desk.radius_desk.utils.hotspot_embed import ensure_rate_limit, validate_hotspot_url


class TestValidateHotspotUrl(IntegrationTestCase):
	def test_accepts_private_ipv4_login_urls(self):
		self.assertEqual(
			validate_hotspot_url("http://192.168.88.1/login"),
			"http://192.168.88.1/login",
		)
		self.assertEqual(
			validate_hotspot_url("http://10.5.50.1/login?dst=http%3A%2F%2Fexample.com%2F"),
			"http://10.5.50.1/login?dst=http%3A%2F%2Fexample.com%2F",
		)

	def test_accepts_https_loopback_and_ipv6_ula(self):
		self.assertEqual(
			validate_hotspot_url("https://127.0.0.1/login"),
			"https://127.0.0.1/login",
		)
		self.assertEqual(
			validate_hotspot_url("http://[fd00::1]/login"),
			"http://[fd00::1]/login",
		)
		self.assertEqual(
			validate_hotspot_url("http://[fe80::1]/login"),
			"http://[fe80::1]/login",
		)

	def test_rejects_public_hosts(self):
		self.assertIsNone(validate_hotspot_url("https://evil.com/login"))
		self.assertIsNone(validate_hotspot_url("http://8.8.8.8/login"))

	def test_rejects_single_label_and_dns_names(self):
		self.assertIsNone(validate_hotspot_url("http://router/login"))
		self.assertIsNone(validate_hotspot_url("http://evil/login"))

	def test_rejects_non_http_schemes(self):
		self.assertIsNone(validate_hotspot_url("javascript:alert(1)"))
		self.assertIsNone(validate_hotspot_url("ftp://192.168.88.1/login"))

	def test_rejects_exotic_ip_encodings(self):
		# decimal / hex IP encodings that browsers would resolve
		self.assertIsNone(validate_hotspot_url("http://3232238081/login"))
		self.assertIsNone(validate_hotspot_url("http://0x7f.0.0.1/login"))

	def test_rejects_userinfo_and_injection(self):
		self.assertIsNone(validate_hotspot_url("http://user:pass@192.168.88.1/login"))
		self.assertIsNone(validate_hotspot_url('http://192.168.88.1/login"><script>alert(1)</script>'))

	def test_rejects_garbage(self):
		self.assertIsNone(validate_hotspot_url(""))
		self.assertIsNone(validate_hotspot_url(None))
		self.assertIsNone(validate_hotspot_url("not a url"))


class TestEnsureRateLimit(IntegrationTestCase):
	def test_allows_up_to_limit_then_raises(self):
		key = f"test:{uuid.uuid4().hex}"
		for _ in range(3):
			ensure_rate_limit(key, limit=3, window_seconds=60)
		with self.assertRaises(frappe.exceptions.TooManyRequestsError):
			ensure_rate_limit(key, limit=3, window_seconds=60)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.test_hotspot_embed
```
Expected: FAIL — `ModuleNotFoundError: No module named ... hotspot_embed`.

- [ ] **Step 3: Implement the module**

Create `radius_desk/radius_desk/utils/hotspot_embed.py`:

```python
from __future__ import annotations

import ipaddress
import re
import time
from urllib.parse import urlsplit

import frappe
from frappe import _

# After IP validation, restrict the whole URL to a conservative charset so the
# validated value can be embedded in JSON/HTML without injection risk. RouterOS
# login URLs only ever contain host, path and an urlencoded dst parameter.
_SAFE_URL_RE = re.compile(r"^[A-Za-z0-9:/.?=&%_-]+$")


def validate_hotspot_url(url: str | None) -> str | None:
	"""Return the URL unchanged only if it points at a router: http(s) scheme
	and a private / loopback / link-local IP-literal host, with no userinfo and
	no unexpected characters. Anything else returns None so callers drop the
	parameter (prevents open redirects + injection from ?linklogin / ?linkorig).
	"""
	if not url:
		return None
	try:
		parts = urlsplit(url.strip())
	except ValueError:
		return None
	if parts.scheme not in ("http", "https"):
		return None
	if parts.username or parts.password:
		return None
	host = parts.hostname
	if not host:
		return None
	try:
		ip = ipaddress.ip_address(host)
	except ValueError:
		# No hostnames at all — not even single-label (http://evil/ resolves)
		return None
	if not (ip.is_private or ip.is_loopback or ip.is_link_local):
		return None
	if not _SAFE_URL_RE.fullmatch(url.strip()):
		return None
	return url.strip()


def ensure_rate_limit(identifier: str, limit: int, window_seconds: int) -> None:
	"""Raise TooManyRequestsError once `identifier` is used more than `limit`
	times in the current fixed window. Cache-backed and best-effort."""
	cache = frappe.cache()
	window_number = int(time.time() // window_seconds)
	key = f"rd-rate-limit:{identifier}:{window_number}"
	count = cache.incr(key)
	if count == 1:
		cache.expire(key, window_seconds)
	if count > limit:
		raise frappe.exceptions.TooManyRequestsError(
			_("Too many requests. Please wait a few minutes and try again.")
		)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.test_hotspot_embed
```
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add radius_desk/radius_desk/utils/hotspot_embed.py radius_desk/tests/test_hotspot_embed.py
git commit -m "feat: hotspot embed URL validator + cache rate limiter"
```

---

### Task 2: Embed mode on the checkout page (context + templates)

**Files:**
- Modify: `radius_desk/www/voucher-checkout/index.py`
- Create: `radius_desk/www/voucher-checkout/embed_base.html`
- Modify: `radius_desk/www/voucher-checkout/index.html` (extends switch, embed JSON, method hint div)
- Test: `radius_desk/tests/test_embed_page.py`

- [ ] **Step 1: Write the failing tests**

Create `radius_desk/tests/test_embed_page.py`:

```python
import importlib.util
import json
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase


def _load_page_module():
	path = Path(frappe.get_app_path("radius_desk")) / "www" / "voucher-checkout" / "index.py"
	spec = importlib.util.spec_from_file_location("rd_voucher_checkout_index", path)
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


class TestEmbedContext(IntegrationTestCase):
	def setUp(self):
		self.mod = _load_page_module()
		self.original_form_dict = getattr(frappe.local, "form_dict", None)
		frappe.local.form_dict = frappe._dict({})

	def tearDown(self):
		frappe.local.form_dict = self.original_form_dict or frappe._dict({})

	def _get_context(self):
		context = frappe._dict()
		self.mod.get_context(context)
		return context

	def test_normal_mode_has_no_embed(self):
		context = self._get_context()
		self.assertNotIn("embed", context)
		self.assertTrue(context.plans is not None)

	def test_embed_mode_sets_flag_and_validates_params(self):
		frappe.local.form_dict = frappe._dict(
			{
				"embed": "1",
				"linklogin": "http://192.168.88.1/login",
				"linkorig": "http://192.168.88.1/",
			}
		)
		context = self._get_context()
		self.assertTrue(context.embed)
		self.assertEqual(context.linklogin, "http://192.168.88.1/login")
		self.assertEqual(context.linkorig, "http://192.168.88.1/")
		parsed = json.loads(context.embed_json)
		self.assertEqual(parsed["linklogin"], "http://192.168.88.1/login")

	def test_embed_mode_drops_invalid_params(self):
		frappe.local.form_dict = frappe._dict(
			{
				"embed": "1",
				"linklogin": "https://evil.com/login",
				"linkorig": "http://192.168.88.1/",
			}
		)
		context = self._get_context()
		self.assertTrue(context.embed)
		self.assertIsNone(context.linklogin)
		self.assertEqual(context.linkorig, "http://192.168.88.1/")
		parsed = json.loads(context.embed_json)
		self.assertIsNone(parsed["linklogin"])
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.test_embed_page
```
Expected: FAIL — `AttributeError: '_dict' object has no attribute 'embed'` on the embed test (normal-mode test may pass).

- [ ] **Step 3: Implement `index.py` embed parsing**

Replace the contents of `radius_desk/www/voucher-checkout/index.py`:

```python
from __future__ import annotations

import json

import frappe

no_cache = 1


def get_context(context):
	from radius_desk.radius_desk.doctype.voucher_sale.voucher_sale import get_voucher_plans
	from radius_desk.radius_desk.utils.hotspot_embed import validate_hotspot_url

	context.no_cache = 1
	context.plans = get_voucher_plans()

	if frappe.form_dict.get("embed"):
		context.embed = True
		context.linklogin = validate_hotspot_url(frappe.form_dict.get("linklogin"))
		context.linkorig = validate_hotspot_url(frappe.form_dict.get("linkorig"))
		context.embed_json = json.dumps({"linklogin": context.linklogin, "linkorig": context.linkorig})
```

- [ ] **Step 4: Create the embed base template**

Create `radius_desk/www/voucher-checkout/embed_base.html` — extends the full Frappe base (so `frappe.call`, jQuery, bootstrap CSS and the colocated `index.js` all load, same-origin) but removes navbar/footer/breadcrumbs:

```jinja
{% extends "templates/base.html" %}

{% block banner %}{% endblock %}
{% block navbar %}{% endblock %}

{% block content %}
	<div class="rd-embed" style="max-width: 100%; padding: 0 8px;">
		{% block page_content %}{% endblock %}
	</div>
{% endblock %}

{% block footer %}{% endblock %}
```

- [ ] **Step 5: Switch `index.html` to conditional extends + add embed JSON and hint markup**

In `radius_desk/www/voucher-checkout/index.html`, replace line 1:

```jinja
{% extends "templates/web.html" %}
```

with:

```jinja
{% if embed %}{% extends "radius_desk/www/voucher-checkout/embed_base.html" %}{% else %}{% extends "templates/web.html" %}{% endif %}
```

Then, immediately after `{% block page_content %}` (before the `<h3>`), add:

```jinja
{% if embed %}
<script>
	window.RD_EMBED = {{ embed_json | safe }};
</script>
<style>
	.rd-embed h3 { font-size: 1.1rem; }
	.rd-embed .card { font-size: 0.9rem; }
</style>
{% endif %}
```

And in the payment method form-group (after the `</select>` on the `payment-method` select), add:

```html
<div id="method-hint" class="small text-muted mt-1"></div>
```

- [ ] **Step 6: Run tests + verify page renders both ways**

```bash
bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.test_embed_page
bench --site development.localhost clear-cache
```

Then with `bench start` running (or the dev web server up), check both render without a Jinja error:

```bash
curl -s http://development.localhost:8000/voucher-checkout/ | rg -c "navbar|page-content"
curl -s "http://development.localhost:8000/voucher-checkout/?embed=1" | rg -c "rd-embed|RD_EMBED"
```
Expected: first >0 (normal chrome present), second >0 (embed shell present), no traceback in either.

- [ ] **Step 7: Commit**

```bash
git add radius_desk/www/voucher-checkout/ radius_desk/tests/test_embed_page.py
git commit -m "feat: embed mode for voucher checkout (bare base, validated link params)"
```

---

### Task 3: Portal JS — code delivery, approval hints, honest timeout copy

**Files:**
- Modify: `radius_desk/www/voucher-checkout/index.js`

No JS test infra exists — syntax check + manual browser QA (spec checklist). Keep the diff minimal and mechanical.

- [ ] **Step 1: Add the delivery helper and method hints**

In `index.js`, after the `const $voucherCode = ...` line block, add:

```js
	const METHOD_HINTS = {
		EcoCash: "Approve on your phone with your EcoCash PIN — works without mobile data.",
		InnBucks: "The InnBucks app needs mobile data to approve — keep mobile data ON.",
		Omari: "The Omari app needs mobile data to approve — keep mobile data ON.",
	};
	const $methodHint = $("#method-hint");

	function show_method_hint() {
		$methodHint.text(METHOD_HINTS[$method.val()] || "");
	}
	show_method_hint();
	$method.on("change", show_method_hint);

	// Deliver the voucher code to the hotspot page (embed mode only).
	// - Fallback full-page mode (linklogin known): top-level redirect with the
	//   code in the URL fragment. Navigation is never mixed-content blocked
	//   (a cross-scheme form POST would be); the fragment is not sent to the
	//   router or logged.
	// - Iframe mode: postMessage to the parent. targetOrigin is the parent's
	//   origin derived from document.referrer — NEVER '*' (an evil embedding
	//   page could harvest the code). If the referrer is unavailable, do
	//   nothing: the code stays on screen for manual entry.
	function deliver_code(code) {
		if (!window.RD_EMBED) return;
		if (window.RD_EMBED.linklogin) {
			set_status(__("Connecting you to the internet..."));
			setTimeout(function () {
				window.location.href = window.RD_EMBED.linklogin + "#rd-voucher=" + encodeURIComponent(code);
			}, 1500);
			return;
		}
		if (window.parent === window) return;
		try {
			var target = new URL(document.referrer).origin;
		} catch (e) {
			return;
		}
		window.parent.postMessage({ source: "rd-voucher", code: code }, target);
	}
```

- [ ] **Step 2: Call `deliver_code` on success and fix the timeout copy**

In `poll_status()`, in the `st.status === "Completed"` branch, after `$resultPanel.show();` add:

```js
					deliver_code(st.voucher_code);
```

Replace the MAX_POLLS message:

```js
					__(
						"Payment is still being processed. If approved on your phone, please ask the cafe staff to check your purchase.",
					),
```

- [ ] **Step 3: Syntax check**

```bash
node --check radius_desk/www/voucher-checkout/index.js
```
Expected: no output (valid syntax).

- [ ] **Step 4: Commit**

```bash
git add radius_desk/www/voucher-checkout/index.js
git commit -m "feat: deliver voucher code to hotspot page (postMessage / fragment), method hints, honest timeout copy"
```

---

### Task 4: Guest API hardening — rate limits, redirect fail-fast, poll backoff

**Files:**
- Modify: `radius_desk/radius_desk/radius_desk/doctype/voucher_sale/voucher_sale.py`
- Test: `radius_desk/tests/test_voucher_sale.py` (append)

- [ ] **Step 1: Write the failing tests**

Append to the `TestVoucherSale` class in `radius_desk/tests/test_voucher_sale.py` (module already imports `frappe`, `patch`, `vs`; add `uuid` import at top if missing):

```python
	def test_create_web_checkout_rejects_pesepay_redirect_url(self):
		"""PesaPay seamless must never return a redirect for embed buyers —
		pre-auth clients can't reach PesaPay (outside the walled garden)."""
		with patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.make_seamless_payment"
		) as mock_ms, patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._validate_pesepay_combo"
		):
			mock_ms.return_value = None
			frappe.response["message"] = {
				"success": True,
				"merchant_reference": "PES-R",
				"poll_url": "",
				"reference_number": "REF-R",
				"redirect_url": "https://pay.pesepay.com/x",
			}
			with self.assertRaises(frappe.ValidationError):
				vs.create_web_checkout(self.plan.name, "0778889900", "EcoCash")

		sale = frappe.get_doc("Voucher Sale", {"phone_number": "0778889900"})
		self.assertEqual(sale.status, "Payment Failed")

	def test_create_web_checkout_rate_limits_per_phone(self):
		suffix = uuid.uuid4().hex[:6]
		with patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.make_seamless_payment"
		) as mock_ms, patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._validate_pesepay_combo"
		), patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._client_ip",
			return_value=f"10.9.9.{suffix[:2]}",
		):
			mock_ms.return_value = None
			frappe.response["message"] = {
				"success": True,
				"merchant_reference": "PES-RL",
				"poll_url": "https://poll.example.com",
				"reference_number": "REF-RL",
			}
			phone = f"0771{suffix}"
			for _ in range(3):
				vs.create_web_checkout(self.plan.name, phone, "EcoCash")
			with self.assertRaises(frappe.exceptions.TooManyRequestsError):
				vs.create_web_checkout(self.plan.name, phone, "EcoCash")

	def test_confirm_poll_backs_off_outbound_pesepay_calls(self):
		"""confirm_voucher_web_checkout must not hammer PesaPay outbound —
		at most one real poll per 3s per checkout_token."""
		sale = self._make_sale()
		sale.db_set("status", "Payment Pending")
		sale.db_set("poll_url", "https://poll.example.com")
		with patch(
			"pesepay.pesepay.doctype.pesepay_settings.pesepay_settings.poll_payment_status",
			return_value={},
		) as mock_poll:
			vs.confirm_voucher_web_checkout("tok0001")
			vs.confirm_voucher_web_checkout("tok0001")
		self.assertEqual(mock_poll.call_count, 1)
```

(`poll_payment_status` is imported inside the function body, so it must be patched at its source module, not on `voucher_sale`.)

Also add cache clearing to `TestVoucherSale.setUp` so repeated test runs don't hit stale rate-limit counters (Redis survives between runs, and existing web-checkout tests reuse the same phone numbers):

```python
		frappe.cache().delete_keys("rd-rate-limit:*")
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.test_voucher_sale
```
Expected: the 3 new tests FAIL (no redirect check → no raise; no rate limit → no raise; backoff → call_count 2).

- [ ] **Step 3: Implement the hardening in `voucher_sale.py`**

Add to the imports at the top (near the other stdlib imports):

```python
import re
```

and after the existing `from radius_desk...` style imports (match the file's existing import style):

```python
from radius_desk.radius_desk.utils.hotspot_embed import ensure_rate_limit
```

Add two helpers near the other module-level helpers (e.g. after `_get_settings`):

```python
def _client_ip() -> str:
	"""Best-effort client IP. Behind Frappe Cloud/Cloudflare the X-Forwarded-For
	first entry is the connecting NAT (the café router); hotspot clients share
	it, so the per-IP limit must stay generous — it only stops bulk abuse."""
	request = frappe.local.request
	if request is None:
		return "tests"
	forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
	return forwarded or getattr(request, "remote_addr", "") or "unknown"


def _should_poll_pesepay(checkout_token: str) -> bool:
	"""Outbound PesaPay poll backoff: at most one real poll per 3s per token
	(the web page polls every 3s, so every other poll does the real work)."""
	cache = frappe.cache()
	key = f"rd-pesepay-poll:{checkout_token}"
	if cache.get(key):
		return False
	cache.set_value(key, 1, expires_in_sec=3)
	return True
```

In `create_web_checkout` (voucher_sale.py:498), insert at the very top of the function body (before `plan_doc = ...`):

```python
	# Abuse controls: the guest API pushes real USSD/app payment prompts to a
	# phone number — cap per phone (anti-bomb) and per IP (bulk abuse; note
	# hotspot clients share the café NAT so this stays generous).
	ensure_rate_limit(f"web-checkout:phone:{re.sub(r'[^0-9]', '', phone_number or '')}", 3, 3600)
	ensure_rate_limit(f"web-checkout:ip:{_client_ip()}", 20, 3600)
```

In `initiate_voucher_payment` (voucher_sale.py:345), after `msg = frappe.response.get("message") or {}` and the existing `success is False` check, add:

```python
	if msg.get("redirect_url"):
		frappe.throw(_("This payment method requires a web redirect, which is not supported here."))
```

In `confirm_voucher_web_checkout` (voucher_sale.py:562), change `if sale.poll_url:` to:

```python
	if sale.poll_url and _should_poll_pesepay(checkout_token):
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.test_voucher_sale
```
Expected: PASS (new + all existing).

- [ ] **Step 5: Commit**

```bash
git add radius_desk/radius_desk/radius_desk/doctype/voucher_sale/voucher_sale.py radius_desk/tests/test_voucher_sale.py
git commit -m "feat: harden guest checkout API (rate limits, redirect fail-fast, poll backoff)"
```

---

### Task 5: Fulfilment-failed retry scheduler + operator notification

**Files:**
- Create: `radius_desk/radius_desk/utils/fulfillment_retry.py`
- Modify: `radius_desk/hooks.py` (scheduler_events)
- Test: `radius_desk/tests/test_fulfillment_retry.py`

- [ ] **Step 1: Write the failing tests**

Create `radius_desk/tests/test_fulfillment_retry.py`:

```python
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from radius_desk.radius_desk.utils import fulfillment_retry as fr_retry


class TestFulfillmentRetry(IntegrationTestCase):
	def _make_failed_sale(self, minutes_old=30):
		from radius_desk.radius_desk.doctype.voucher_plan.voucher_plan import VoucherPlan

		if not frappe.db.exists("Voucher Plan", "_Test Retry Plan"):
			plan = VoucherPlan(
				plan_name="_Test Retry Plan",
				company="_Test Company",
				radius_realm_id=1,
				radius_profile_id=1,
				price=10,
				currency="INR",
				enabled=1,
			).insert(ignore_permissions=True)
		sale = frappe.get_doc(
			{
				"doctype": "Voucher Sale",
				"plan": "_Test Retry Plan",
				"amount": 10,
				"currency": "INR",
				"sale_source": "Web",
				"status": "Fulfillment Failed",
				"radius_error": "boom",
				"checkout_token": "retrytok0001",
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Voucher Sale",
			sale.name,
			"modified",
			add_to_date(now_datetime(), minutes=-minutes_old),
			update_modified=False,
		)
		self.addCleanup(frappe.delete_doc, "Voucher Sale", sale.name, force=True)
		return sale.name

	def test_retries_and_completes_stale_failed_sale(self):
		name = self._make_failed_sale()
		with patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector"
		) as mock_conn:
			mock_conn.return_value.create_voucher.return_value = {"id": 77, "name": "RETRY-OK"}
			fr_retry.retry_fulfillment_failed_sales()
		sale = frappe.get_doc("Voucher Sale", name)
		self.assertEqual(sale.status, "Completed")
		self.assertEqual(sale.voucher_code, "RETRY-OK")

	def test_still_failing_sale_notifies_operator_throttled(self):
		name = self._make_failed_sale()
		frappe.cache().delete_value("rd-fulfillment-notify")
		with patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector"
		) as mock_conn, patch("frappe.sendmail") as mock_send:
			mock_conn.return_value.create_voucher.side_effect = Exception("still down")
			fr_retry.retry_fulfillment_failed_sales()
			# second run within the throttle window must not re-send
			fr_retry.retry_fulfillment_failed_sales()
		self.assertEqual(mock_send.call_count, 1)
		sale = frappe.get_doc("Voucher Sale", name)
		self.assertEqual(sale.status, "Fulfillment Failed")
```

Note: the success path creates a POS Invoice (existing `fulfill_voucher_sale`) — if `_create_pos_invoice` fails in this test environment, mirror the existing test setup (`ensure_today_open_pos_entry`) from `test_voucher_sale.py::test_web_fulfillment_creates_pos_invoice`; the connector patch target is the same.

- [ ] **Step 2: Run tests to verify they fail**

```bash
bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.test_fulfillment_retry
```
Expected: FAIL — `ModuleNotFoundError: ... fulfillment_retry`.

- [ ] **Step 3: Implement the scheduler**

Create `radius_desk/radius_desk/utils/fulfillment_retry.py`:

```python
from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_to_date, now_datetime

# Only retry sales that have been failed for a while (the webhook / guest poll
# get the first chances), and throttle the operator email itself.
NOTIFY_AFTER_MINUTES = 15
NOTIFY_THROTTLE_SECONDS = 1800
BATCH_LIMIT = 20


def retry_fulfillment_failed_sales():
	"""Scheduled safety net: re-run fulfillment for stale Fulfillment Failed
	sales (money was taken, voucher missing) and email System Managers about
	anything still failing. Individual failures never abort the batch."""
	from radius_desk.radius_desk.doctype.voucher_sale.voucher_sale import fulfill_voucher_sale

	cutoff = add_to_date(now_datetime(), minutes=-NOTIFY_AFTER_MINUTES)
	names = frappe.get_all(
		"Voucher Sale",
		filters={"status": "Fulfillment Failed", "modified": ["<", cutoff]},
		pluck="name",
		limit=BATCH_LIMIT,
		order_by="modified asc",
	)

	still_failed = []
	for name in names:
		try:
			sale = frappe.get_doc("Voucher Sale", name)
			sale.db_set("status", "Payment Confirmed", update_modified=True)
			sale.db_set("radius_error", "", update_modified=True)
			fulfill_voucher_sale(name)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			still_failed.append(name)

	if still_failed:
		_notify_operator(still_failed)


def _notify_operator(failed_sales: list[str]) -> None:
	cache = frappe.cache()
	if cache.get_value("rd-fulfillment-notify"):
		return
	cache.set_value("rd-fulfillment-notify", 1, expires_in_sec=NOTIFY_THROTTLE_SECONDS)

	recipients = []
	for user in frappe.get_all(
		"Has Role",
		filters={"role": "System Manager", "parenttype": "User"},
		pluck="parent",
	):
		if frappe.db.get_value("User", user, "enabled"):
			recipients.append(user)
	if not recipients:
		return

	frappe.sendmail(
		recipients=recipients,
		subject=_("RadiusDesk: voucher fulfillment still failing"),
		message="<p>These paid voucher sales could not be fulfilled after retry:</p>"
		+ "".join(f"<p>{frappe.utils.escape_html(n)}</p>" for n in failed_sales)
		+ "<p>Please check Radius Desk Settings / the RadiusDesk server.</p>",
	)
```

- [ ] **Step 4: Wire the cron hook**

In `radius_desk/hooks.py`, replace the `scheduler_events` block (line ~160):

```python
scheduler_events = {
	"daily": ["radius_desk.radius_desk.utils.pos_infra.ensure_daily_pos_opening_entry"],
	"cron": {
		# every 5 minutes: retry paid-but-unfulfilled voucher sales
		"0/5 * * * *": ["radius_desk.radius_desk.utils.fulfillment_retry.retry_fulfillment_failed_sales"],
	},
}
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
bench --site development.localhost run-tests --app radius_desk --module radius_desk.tests.test_fulfillment_retry
```
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add radius_desk/radius_desk/utils/fulfillment_retry.py radius_desk/hooks.py radius_desk/tests/test_fulfillment_retry.py
git commit -m "feat: scheduled retry of failed voucher fulfillment + operator alert"
```

---

### Task 6: MikroTik login page — Buy tab, code receiver, rescue banner

**Files (repo `hotspot-cafe-config`):**
- Modify: `mikrotik/hotspot/login.html`

All work in `/home/gift/Documents/code-projects/hotspot-cafe-config`. No automated tests — review carefully, deployment is a later manual rollout.

- [ ] **Step 1: Add banner + tab styles and the banner element**

In `mikrotik/hotspot/login.html` `<style>` block (after the `.cafe-title` rule, before `</style>`), add:

```css
.rd-banner {
    display: none;
    background: rgba(30,186,185,0.15);
    border: 1px solid #1EBAB9;
    color: #fff;
    padding: 10px 14px;
    border-radius: 8px;
    margin-bottom: 14px;
    font-size: 13px;
    text-align: center;
}
.rd-buy-note {
    color: rgba(255,255,255,0.6);
    font-size: 12px;
    margin: 0 0 10px;
    line-height: 1.4;
}
.rd-buy-note a { color: #1EBAB9; }
```

- [ ] **Step 2: Add the receiver script**

After the existing `switchTab` `<script>` block (after its closing `</script>`, around line 85), add:

```html
    <script>
        // --- RadiusDesk voucher auto-login receiver ---
        var RD_PORTAL_ORIGIN = "https://njeremoto.jh.erpnext.com";
        var RD_CODE_RE = /^[A-Za-z0-9_-]+$/;
        var RD_MAX_RETRIES = 2;

        function rdStashCode(code) {
            try { sessionStorage.setItem("rd_voucher_code", code); } catch (e) {}
        }
        function rdSubmitCode(code) {
            // Plain PAP submit of the existing form: bypasses the CHAP
            // onSubmit (the challenge may be stale after a long payment),
            // and the code travels http->http to the router itself.
            document.login.username.value = code;
            document.login.password.value = code;
            document.login.submit();
        }
        function rdShowBanner(html) {
            var el = document.getElementById("rd-banner");
            if (!el) return;
            el.innerHTML = html;
            el.style.display = "block";
        }
        function rdReceiveCode(code) {
            rdStashCode(code);
            rdShowBanner("Connecting you to the internet&hellip;");
            rdSubmitCode(code);
        }
        window.addEventListener("message", function (e) {
            if (e.origin !== RD_PORTAL_ORIGIN) return;
            var d = e.data || {};
            if (d.source !== "rd-voucher" || !RD_CODE_RE.test(d.code || "")) return;
            rdReceiveCode(d.code);
        });
        (function () {
            // Full-page fallback delivery: ?#rd-voucher=CODE fragment
            var m = /(?:^|[#&])rd-voucher=([A-Za-z0-9_-]+)/.exec(window.location.hash || "");
            if (m) {
                try { history.replaceState(null, "", window.location.pathname + window.location.search); } catch (e) {}
                rdReceiveCode(m[1]);
                return;
            }
            // Rescue: a stashed code means we tried before (RADIUS race or
            // reload). Retry a couple of times, then fall back to manual.
            var stashed = null, attempts = 0;
            try {
                stashed = sessionStorage.getItem("rd_voucher_code");
                attempts = parseInt(sessionStorage.getItem("rd_voucher_attempts") || "0", 10);
            } catch (e) {}
            if (!stashed || !RD_CODE_RE.test(stashed)) return;
            attempts += 1;
            try { sessionStorage.setItem("rd_voucher_attempts", String(attempts)); } catch (e) {}
            if (attempts <= RD_MAX_RETRIES) {
                rdShowBanner("Reconnecting you&hellip; (attempt " + attempts + " of " + (RD_MAX_RETRIES + 1) + ")");
                setTimeout(function () { rdSubmitCode(stashed); }, 1000);
            } else {
                try {
                    sessionStorage.removeItem("rd_voucher_code");
                    sessionStorage.removeItem("rd_voucher_attempts");
                } catch (e) {}
                rdShowBanner("Auto-connect failed. Your voucher code is <b>" + stashed + "</b> &mdash; press Connect below.");
                var vInput = document.querySelector("#tab-voucher input");
                if (vInput) {
                    vInput.value = stashed;
                    document.login.username.value = stashed;
                    document.login.password.value = stashed;
                    switchTab("voucher", { currentTarget: document.querySelector(".tab-btn") });
                }
            }
        })();
    </script>
```

- [ ] **Step 3: Add the banner div and the Buy tab**

Add the banner div just before `<div class="tab-bar">` (line ~225):

```html
                    <div id="rd-banner" class="rd-banner"></div>
```

Add the third tab button inside `.tab-bar` (after the Username / Password `span`):

```html
                        <span class="tab-btn" onclick="switchTab('buy', event)">Buy Voucher</span>
```

Add the tab content after the closing `</div>` of `tab-voucher` (line ~246):

```html
                    <div id="tab-buy" class="tab-content">
                        <p class="rd-buy-note">
                            Buy a voucher with EcoCash / InnBucks / Omari and connect automatically.
                            Trouble seeing it?
                            <a href="https://njeremoto.jh.erpnext.com/voucher-checkout/?embed=1&amp;linklogin=$(link-login-only-esc)&amp;linkorig=$(link-orig-esc)">Open the shop in a full page</a>.
                        </p>
                        <iframe
                            src="https://njeremoto.jh.erpnext.com/voucher-checkout/?embed=1"
                            style="width:100%;height:480px;border:0;border-radius:8px;background:#fff;"
                            title="Buy a WiFi voucher"></iframe>
                    </div>
```

Note: `$(link-login-only-esc)` / `$(link-orig-esc)` are RouterOS URL-escaped expansions — required so an `&` in the original URL does not truncate the parameters.

- [ ] **Step 4: Sanity checks**

```bash
rg -c "rd-banner|tab-buy|rd-voucher" mikrotik/hotspot/login.html
```
Expected: matches across all three additions (banner div, tab bar button, tab content, receiver script).

Open `mikrotik/hotspot/login.html` in a local browser (`file://`) — must render without JS console errors (the form posts nowhere locally, that is fine; verify tabs switch and the banner div exists).

- [ ] **Step 5: Commit**

```bash
git add mikrotik/hotspot/login.html
git commit -m "feat: Buy Voucher tab embedding guest portal + voucher auto-login receiver"
```

---

### Task 7: Walled garden script, keep-alive cron, docs

**Files (repo `hotspot-cafe-config`):**
- Create: `mikrotik/04-walled-garden.rsc`
- Modify: `droplet/README.md` (keep-alive cron), `README.md`, `SESSION-LOG.md` (brief entries)

- [ ] **Step 1: Create `mikrotik/04-walled-garden.rsc`**

```
# Phase 4 — Walled garden for the Frappe guest voucher portal
# Lets pre-auth hotspot clients reach https://njeremoto.jh.erpnext.com
# (voucher shop embed + full-page fallback + status polling).
# Run AFTER the portal app is live. Verify with the QA checklist in
# radius_desk docs/superpowers/specs/2026-09-12-hotspot-embed-voucher-purchase-design.md

/ip hotspot walled-garden-ip
add action=allow dst-host=njeremoto.jh.erpnext.com comment="Frappe guest voucher portal (pre-auth)"

# Block DNS-over-HTTPS bypass: a client using DoH resolves the portal to
# different edge IPs than the ones the walled-garden-ip entry allowed,
# breaking the portal before login. Clients must use the router's DNS.
/ip firewall filter
add chain=forward action=drop protocol=tcp dst-port=853 comment="block DoH (DNS over TLS)"
add chain=forward action=drop protocol=udp dst-port=853 comment="block DoH (DNS over TLS)"
:foreach addr in={"1.1.1.1","1.0.0.1","8.8.8.8","8.8.4.4","9.9.9.9","149.112.112.112"} do={
    /ip firewall filter add chain=forward action=drop dst-address=$addr comment="block known public DNS/DoH resolver (use router DNS)"
}
```

- [ ] **Step 2: Add the keep-alive cron to `droplet/README.md`**

Append to the cron section of `droplet/README.md`:

```markdown
### Frappe portal keep-alive (anti-hibernate)

A quiet single-site Frappe Cloud site can cold-start (or hibernate) right when
a customer needs it. Keep it warm from the droplet (crontab -e):

```
*/15 * * * * curl -fsS -o /dev/null https://njeremoto.jh.erpnext.com/voucher-checkout/
```
```

- [ ] **Step 3: Note the new hotspot flow in `README.md` and `SESSION-LOG.md`**

In `README.md`, add a short section (after the hotspot portal description):

```markdown
### Voucher self-service (2026-09)

`login.html` has a **Buy Voucher** tab embedding
`https://njeremoto.jh.erpnext.com/voucher-checkout/?embed=1` (walled-gardened,
see `04-walled-garden.rsc`). After mobile-money payment the voucher code is
delivered to the login page (postMessage from the iframe, or `#rd-voucher=`
fragment in full-page mode) and auto-submitted as PAP. A sessionStorage rescue
banner retries and finally shows the code for manual entry. Requires
`login-by` to include `http-pap` (it does).

**Before uploading a new `login.html` to the router**: back up the live
hotspot directory first (`/file print` or fetch the current files) — rollback
is re-uploading the old `login.html`. The break-glass user documented above
still works even if the new page is broken.
```

Append one dated entry to `SESSION-LOG.md` summarising: embed tab added, fragment/postMessage auto-login, walled garden + DoH blocks, keep-alive cron; rollout pending QA checklist.

- [ ] **Step 4: Commit**

```bash
git add mikrotik/04-walled-garden.rsc droplet/README.md README.md SESSION-LOG.md
git commit -m "feat: walled garden for guest portal + DoH blocks + keep-alive + docs"
```

---

### Task 8: Full verification

- [ ] **Step 1: Run the entire app test suite**

```bash
bench --site development.localhost run-tests --app radius_desk
```
Expected: all PASS (existing + new modules).

- [ ] **Step 2: Lint**

```bash
cd /home/gift/Documents/code-projects/frappe/v16/apps/radius_desk && ruff check radius_desk && ruff format --check radius_desk
```
Expected: clean (fix any findings).

- [ ] **Step 3: Verify embed page end-to-end on dev**

With `bench start` running:

```bash
curl -s "http://development.localhost:8000/voucher-checkout/?embed=1&linklogin=https://evil.com/x&linkorig=http://192.168.88.1/" | rg -o "window.RD_EMBED = [^<]+"
```
Expected: `window.RD_EMBED = {"linklogin": null, "linkorig": "http://192.168.88.1/"}` — the evil linklogin is dropped server-side.

- [ ] **Step 4: Commit any stragglers + summarize**

```bash
git status --short   # both repos; commit anything left, then report done
```

Post-plan (manual, NOT in this session): deploy per the spec's rollout order (app → site config → router upload → walled garden → QA checklist).
