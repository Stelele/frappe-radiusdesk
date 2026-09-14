from __future__ import annotations

import re
import uuid

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt
from pesepay.templates.pages.pesepay_checkout import make_seamless_payment

from radius_desk.radius_desk.utils.hotspot_embed import ensure_rate_limit
from radius_desk.radius_desk.utils.pos_infra import get_pesepay_mode_of_payment
from radius_desk.radius_desk.utils.radiusdesk import RadiusDeskException

SUCCESS_STATUSES = ("Completed", "Voucher Created")


class VoucherSale(Document):
	def validate(self):
		self.validate_amount()

	def validate_amount(self):
		if flt(self.amount) <= 0:
			frappe.throw(_("Amount must be greater than zero."))

	def on_payment_authorized(self, status):
		"""Called by the pesepay webhook / scheduler on successful payment."""
		if status != "Completed":
			return
		if self.status in SUCCESS_STATUSES:
			return
		self.db_set("status", "Payment Confirmed", update_modified=True)
		try:
			fulfill_voucher_sale(self.name)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "RadiusDesk fulfillment via payment hook")


# ---------------------------------------------------------------------------
# Settings / connector helpers
# ---------------------------------------------------------------------------


def _validate_pesepay_combo(payment_method: str, currency: str) -> None:
	"""Fail fast when PesaPay cannot process this method/currency pair.

	PesaPay only settles specific combos (e.g. EcoCash in USD or ZiG). Without
	this check the sale reaches the gateway and dies with a generic
	"Payment processing failed. Please try again.", leaving the cashier with
	no idea that the sale currency is the problem.
	"""
	try:
		from pesepay.pesepay.doctype.pesepay_settings.pesepay_connector import METHOD_CODES
	except ImportError:
		return

	if (payment_method, currency) in METHOD_CODES:
		return

	supported = ", ".join(f"{m} ({c})" for m, c in sorted(METHOD_CODES))
	frappe.throw(
		_(
			"PesaPay does not support {0} payments in {1}. The sale currency must match a supported combination ({2})."
		).format(frappe.bold(payment_method), frappe.bold(currency), supported)
	)


def _get_settings() -> frappe._dict:
	settings = frappe.get_doc("Radius Desk Settings", ignore_permissions=True)
	missing = []
	if not settings.server_url:
		missing.append("Server URL")
	if not settings.username:
		missing.append("Username")
	if not settings.get_password("password"):
		missing.append("Password")
	if not settings.cloud_id:
		missing.append("Cloud ID")
	if not settings.default_company:
		missing.append("Default Company")
	if not settings.default_walkin_customer:
		missing.append("Default Walk-in Customer")
	if not settings.pesepay_gateway:
		missing.append("Pesepay Gateway")
	if missing:
		frappe.throw(_("Radius Desk Settings are incomplete: {0}").format(", ".join(missing)))
	return settings


def _client_ip() -> str:
	"""Best-effort client IP for the coarse per-IP abuse limit. Takes the first
	X-Forwarded-For entry, which is only trustworthy when the immediate peer is
	an overwriting proxy (true on Frappe Cloud; a client CAN spoof it on a bare
	app server — acceptable because the per-phone limit is the real control).
	All hotspot clients share the cafe NAT's public IP, so this stays generous;
	it only stops bulk abuse."""
	request = getattr(frappe.local, "request", None)
	if request is None:
		return "tests"
	forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
	return forwarded or getattr(request, "remote_addr", "") or "unknown"


def _should_poll_pesepay(checkout_token: str) -> bool:
	"""Outbound PesaPay poll backoff: at most one real poll per 3s per token
	(the web page polls every 3s, so every other poll does the real work).
	Atomic SET NX avoids the get-then-set race between concurrent polls.
	Raw set is used so the key is prefixed exactly once by make_key."""
	cache = frappe.cache()
	key = cache.make_key(f"rd-pesepay-poll:{checkout_token}")
	if cache.set(key, 1, ex=3, nx=True):
		return True
	return False


def _get_connector(settings):
	from radius_desk.radius_desk.utils.radiusdesk import RadiusDeskConnector

	return RadiusDeskConnector(
		server_url=settings.server_url,
		username=settings.username,
		password=settings.get_password("password"),
		cloud_id=settings.cloud_id,
	)


def get_standard_selling_price_list(company=None):
	"""Resolve the price list that holds voucher item prices.

	Preference order:
	  1. Radius Desk Settings -> selling_price_list (explicit, most reliable)
	  2. ERPNext global Selling Settings -> selling_price_list
	  3. Hard fallback "Standard Selling"
	"""
	try:
		rds = frappe.get_doc("Radius Desk Settings", ignore_permissions=True)
		if getattr(rds, "selling_price_list", None) and rds.selling_price_list:
			return rds.selling_price_list
	except Exception:
		pass
	try:
		pl = frappe.get_doc("Selling Settings", ignore_permissions=True).selling_price_list
		if pl:
			return pl
	except Exception:
		pass
	return "Standard Selling"


def get_voucher_price(item_code, company=None, fallback_price=None, fallback_currency=None):
	"""Return the voucher price for an item as ``(price, currency)``.

	Pulls the rate from the resolved selling price list (Item Price with
	``selling=1``). Falls back to ``fallback_price``/``fallback_currency`` (the
	plan's own price) when no Item Price exists, so callers that pass the plan
	price keep working.
	"""
	price_list = get_standard_selling_price_list(company)
	rate = frappe.db.get_value(
		"Item Price",
		{"item_code": item_code, "price_list": price_list, "selling": 1},
		"price_list_rate",
	)
	if rate is not None:
		currency = frappe.db.get_value("Price List", price_list, "currency") or fallback_currency
		return flt(rate), currency
	return (flt(fallback_price) if fallback_price is not None else None), fallback_currency


def _stamp_invoice(sale: "VoucherSale"):
	if not sale.invoice_doctype or not sale.invoice_name:
		return
	frappe.db.set_value(
		sale.invoice_doctype,
		sale.invoice_name,
		{"radius_voucher_code": sale.voucher_code, "radius_voucher_plan": sale.plan},
		update_modified=True,
	)


def _create_pos_invoice(sale, plan, settings) -> tuple[str, str]:
	"""Create the RadiusDesk-linked invoice for a fulfilled sale.

	Returns ``(invoice_name, invoice_type)``. The doctype honors the system
	choice in ``POS Settings.invoice_type`` (POS Invoice or Sales Invoice) rather
	than being hard-coded, so the guest portal follows the site's configuration.
	"""
	from radius_desk.radius_desk.utils.pos_infra import (
		POS_USER,
		ensure_today_open_pos_entry,
		get_invoice_type,
	)

	company = settings.default_company
	invoice_type = get_invoice_type()
	price, _ = get_voucher_price(plan.item, plan.company, plan.price, plan.currency)
	price = flt(price or plan.price)
	currency = plan.currency
	mode = get_pesepay_mode_of_payment(settings.pesepay_gateway)
	debit_to = frappe.get_cached_value("Company", company, "default_receivable_account")

	if invoice_type == "POS Invoice":
		opening_entry = ensure_today_open_pos_entry(company, mode)
		pos_profile = frappe.db.get_value("POS Opening Entry", opening_entry, "pos_profile")
		income_account = frappe.db.get_value("POS Profile", pos_profile, "income_account")
		cost_center = frappe.db.get_value("POS Profile", pos_profile, "cost_center")
		selling_price_list = frappe.db.get_value("POS Profile", pos_profile, "selling_price_list")
		invoice = frappe.get_doc(
			{
				"doctype": "POS Invoice",
				"pos_profile": pos_profile,
				"is_pos": 1,
				"company": company,
				"customer": settings.default_walkin_customer,
				"currency": currency,
				"conversion_rate": 1,
				"posting_date": frappe.utils.today(),
				"selling_price_list": selling_price_list,
				"debit_to": debit_to,
				"items": [
					{
						"item_code": plan.item,
						"qty": 1,
						"rate": price,
						"income_account": income_account,
						"cost_center": cost_center,
					}
				],
				"payments": [{"mode_of_payment": mode, "amount": price}],
				"radius_voucher_code": sale.voucher_code,
				"radius_voucher_plan": plan.name,
			}
		)
		_invoice_user_create(invoice)
		# `owner` is set to the app-owned cashier user so the daily POS Closing
		# Entry's `owner == user` validation passes regardless of context.
		frappe.db.set_value("POS Invoice", invoice.name, "owner", POS_USER)
		return invoice.name, "POS Invoice"

	income_account = frappe.get_cached_value("Company", company, "default_income_account")
	cost_center = frappe.get_cached_value("Company", company, "cost_center")
	invoice = frappe.get_doc(
		{
			"doctype": "Sales Invoice",
			"company": company,
			"customer": settings.default_walkin_customer,
			"currency": currency,
			"conversion_rate": 1,
			"posting_date": frappe.utils.today(),
			"debit_to": debit_to,
			"items": [
				{
					"item_code": plan.item,
					"qty": 1,
					"rate": price,
					"income_account": income_account,
					"cost_center": cost_center,
				}
			],
			"payments": [{"mode_of_payment": mode, "amount": price}],
			"radius_voucher_code": sale.voucher_code,
			"radius_voucher_plan": plan.name,
		}
	)
	_invoice_user_create(invoice)
	return invoice.name, "Sales Invoice"


def _invoice_user_create(invoice):
	"""Insert and submit an invoice under the app's system user.

	ERPNext's ``get_party_account`` runs an explicit ``has_permission`` check on
	the Account doctype that ignores ``ignore_permissions`` and fails for Guest,
	so the invoice is processed under a dedicated system user with the minimal
	permissions needed rather than elevated to Administrator.
	"""
	from radius_desk.radius_desk.utils.system_user import (
		SYSTEM_USER,
		ensure_system_user,
	)

	ensure_system_user()
	original_user = frappe.session.user
	frappe.set_user(SYSTEM_USER)
	try:
		invoice.insert(ignore_permissions=True)
		invoice.submit()
	finally:
		frappe.set_user(original_user)


def _fulfillment_result(sale: "VoucherSale") -> dict:
	return {
		"ok": True,
		"voucher_code": sale.voucher_code,
		"voucher_id": sale.voucher_id,
		"invoice": sale.invoice_name,
	}


# ---------------------------------------------------------------------------
# Whitelisted API
# ---------------------------------------------------------------------------


@frappe.whitelist(allow_guest=True)
def get_voucher_plans():
	"""Enabled plans for the public checkout page, limited to the Radius Desk
	company so every sale is fulfillable. Price comes from the item's selling
	price list (the single source of truth), not the plan's stored price."""
	plans = frappe.get_all(
		"Voucher Plan",
		filters={"enabled": 1, "company": _get_settings().default_company},
		fields=["name", "plan_name", "description", "item", "currency"],
		order_by="sort_order asc",
	)
	for plan in plans:
		price, currency = get_voucher_price(plan["item"], company=_get_settings().default_company, fallback_currency=plan["currency"])
		plan["price"] = price
		if currency:
			plan["currency"] = currency
	return plans


@frappe.whitelist()
def create_voucher_sale(plan, invoice_doctype, invoice_name, phone_number=None):
	"""Create a Draft Voucher Sale for an in-progress POS invoice. Idempotent
	per invoice: an invoice that already has a Voucher Sale (any status,
	including Completed after a failed invoice submit) returns the existing
	sale so a cashier can never charge the customer twice for the same
	invoice."""
	inv = frappe.get_doc(invoice_doctype, invoice_name)
	if inv.get("radius_voucher_code"):
		frappe.throw(_("Invoice {0} already has voucher {1}.").format(invoice_name, inv.radius_voucher_code))

	existing = frappe.db.get_value(
		"Voucher Sale",
		{
			"invoice_doctype": invoice_doctype,
			"invoice_name": invoice_name,
		},
		"name",
	)
	if existing:
		return {"ok": True, "voucher_sale": existing}

	# No phone-based dedup: two purchases from the same phone number are
	# distinct transactions, and matching by phone would wrongly return a
	# sale for a different invoice (cross-invoice charge). Duplicate-voucher
	# protection is the per-invoice dedup above plus the `radius_voucher_code`
	# guard and fulfillment idempotency (the RadiusDesk connector stores the
	# sale name as its extra_value, so a retry cannot create a second voucher).

	plan_doc = frappe.get_doc("Voucher Plan", plan)
	amount, _currency = get_voucher_price(plan_doc.item, plan_doc.company, plan_doc.price, plan_doc.currency)
	sale = frappe.get_doc(
		{
			"doctype": "Voucher Sale",
			"plan": plan,
			"amount": amount or plan_doc.price,
			"currency": inv.currency,
			"sale_source": "POS",
			"invoice_doctype": invoice_doctype,
			"invoice_name": invoice_name,
			"phone_number": phone_number or "",
			"status": "Draft",
		}
	).insert(ignore_permissions=True)
	return {"ok": True, "voucher_sale": sale.name}


@frappe.whitelist()
def initiate_voucher_payment(sale_name, phone_number, payment_method, check_permission=True):
	"""Initiate a PesaPay seamless payment for a Draft Voucher Sale.

	`check_permission` defaults to True for direct (authed) API calls. The
	guest web checkout passes False because `create_web_checkout` creates the
	sale itself and initiates on it in the same request."""
	sale = frappe.get_doc("Voucher Sale", sale_name, check_permission=check_permission)
	if sale.status != "Draft":
		frappe.throw(_("Voucher Sale {0} is not in Draft status.").format(sale_name))

	settings = _get_settings()
	plan = frappe.get_doc("Voucher Plan", sale.plan)

	_validate_pesepay_combo(payment_method, sale.currency)

	make_seamless_payment(
		gateway_name=settings.pesepay_gateway,
		amount=str(sale.amount),
		currency=sale.currency,
		email="",
		phone_number=phone_number,
		customer_name=_("Voucher Customer {0}").format(phone_number),
		payment_method=payment_method,
		reference_doctype="Voucher Sale",
		reference_docname=sale.name,
		title=_("Voucher {0}").format(plan.plan_name),
	)
	msg = frappe.response.get("message") or {}
	if msg.get("success") is False:
		frappe.throw(msg.get("error") or _("Payment could not be initiated."))
	if msg.get("redirect_url"):
		frappe.throw(_("This payment method isn't available here. Please ask a staff member for help."))

	sale.db_set("phone_number", phone_number, update_modified=True)
	sale.db_set("payment_method", payment_method, update_modified=True)
	sale.db_set("payment_gateway", settings.pesepay_gateway, update_modified=True)
	sale.db_set("merchant_reference", msg.get("merchant_reference"), update_modified=True)
	sale.db_set("pesepay_reference_number", msg.get("reference_number"), update_modified=True)
	sale.db_set("poll_url", msg.get("poll_url") or "", update_modified=True)
	sale.db_set("status", "Payment Pending", update_modified=True)

	return {
		"success": True,
		"merchant_reference": msg.get("merchant_reference"),
		"poll_url": msg.get("poll_url"),
		"reference_number": msg.get("reference_number"),
		"payment_gateway": settings.pesepay_gateway,
	}


@frappe.whitelist()
def reset_voucher_sale(sale_name):
	"""Reset a Payment Pending sale back to Draft so a declined payment can be
	retried with another method. Never touches confirmed or fulfilled sales."""
	sale = frappe.get_doc("Voucher Sale", sale_name, check_permission=True)
	if sale.status != "Payment Pending":
		frappe.throw(_("Only Payment Pending sales can be reset (status: {0}).").format(sale.status))
	if sale.voucher_code:
		frappe.throw(_("A voucher was already created for this sale."))
	sale.db_set("status", "Draft", update_modified=True)
	sale.db_set("merchant_reference", "", update_modified=True)
	sale.db_set("pesepay_reference_number", "", update_modified=True)
	sale.db_set("poll_url", "", update_modified=True)
	sale.db_set("payment_gateway", "", update_modified=True)
	return {"ok": True}


@frappe.whitelist()
def confirm_voucher_payment(merchant_reference):
	"""POS client calls this once PesaPay reports SUCCESS. Marks the IR
	completed, then fulfills. Returns the voucher code synchronously."""
	if not merchant_reference:
		return {"ok": False, "error": _("Missing payment reference.")}

	sale_name = frappe.db.get_value("Voucher Sale", {"merchant_reference": merchant_reference}, "name")
	if not sale_name:
		return {"ok": False, "error": _("No voucher sale found for this payment.")}

	sale = frappe.get_doc("Voucher Sale", sale_name, check_permission=True)
	if sale.status in SUCCESS_STATUSES and sale.voucher_code and sale.invoice_name:
		return _fulfillment_result(sale)
	if sale.status not in ("Payment Pending", "Payment Confirmed"):
		frappe.throw(
			_("Voucher Sale {0} is not awaiting payment confirmation (status: {1}).").format(
				sale.name, sale.status
			)
		)

	# Only fulfill when the payment is confirmed — never promote an arbitrary
	# status or fulfill after a failed confirmation.
	from pesepay.overrides.invoice import mark_pesepay_payment_confirmed

	try:
		mark_pesepay_payment_confirmed(merchant_reference)
	except Exception as exc:
		frappe.log_error(frappe.get_traceback(), "RadiusDesk mark IR confirmed")
		frappe.throw(_("Payment could not be confirmed: {0}").format(exc))

	sale.db_set("status", "Payment Confirmed", update_modified=True)
	return {"ok": True, **fulfill_voucher_sale(sale.name)}


@frappe.whitelist()
def confirm_voucher_sale(sale_name, payment_method=None):
	"""POS cashier path for a non-PesaPay payment mode (cash, another POS
	Profile mode): the cashier has collected the payment at the till, so the
	Draft sale is confirmed and fulfilled immediately, without a PesaPay
	initiation. Self-checkout remains PesaPay-only; this is never exposed to
	the guest page."""
	sale = frappe.get_doc("Voucher Sale", sale_name, ignore_permissions=True)
	if sale.status != "Draft":
		frappe.throw(
			_("Voucher Sale {0} is not in Draft status (status: {1}).").format(sale_name, sale.status)
		)
	if sale.voucher_code:
		frappe.throw(_("A voucher was already created for this sale."))

	if payment_method:
		sale.db_set("payment_method", payment_method, update_modified=True)
	sale.db_set("status", "Payment Confirmed", update_modified=True)
	return fulfill_voucher_sale(sale.name)


@frappe.whitelist()
def get_voucher_codes_for_invoice(doctype, invoice_name):
	"""Return voucher codes created for a submitted POS/Sales Invoice.

	Used by the POS receipt screen so a cashier can read/write down the
	voucher codes when the printer is unavailable. Runs as the logged-in
	user and respects Voucher Sale read permissions.
	"""
	if not doctype or not invoice_name:
		return []

	sales = frappe.get_all(
		"Voucher Sale",
		filters={"invoice_doctype": doctype, "invoice_name": invoice_name},
		fields=["name", "plan", "status", "amount", "currency", "voucher_code", "voucher_id"],
	)

	return [
		{
			"name": s.name,
			"plan": s.plan,
			"status": s.status,
			"amount": s.amount,
			"currency": s.currency,
			"voucher_code": s.voucher_code,
			"voucher_id": s.voucher_id,
		}
		for s in sales
	]


@frappe.whitelist(allow_guest=True)
def create_web_checkout(plan, phone_number, payment_method):
	"""Create a Web Voucher Sale and initiate payment. Returns a checkout token
	for the public page to poll with."""
	# Abuse controls: the guest API pushes real USSD/app payment prompts to a
	# phone number — cap per phone (anti-bomb) and per IP (bulk abuse; note
	# hotspot clients share the cafe NAT so this stays generous).
	ensure_rate_limit(f"web-checkout:phone:{re.sub(r'[^0-9]', '', phone_number or '')}", 3, 3600)
	ensure_rate_limit(f"web-checkout:ip:{_client_ip()}", 60, 3600)
	plan_doc = frappe.get_doc("Voucher Plan", plan)
	if not plan_doc.enabled:
		frappe.throw(_("Voucher plan is not available."))

	amount, currency = get_voucher_price(plan_doc.item, plan_doc.company, plan_doc.price, plan_doc.currency)
	sale = frappe.get_doc(
		{
			"doctype": "Voucher Sale",
			"plan": plan,
			"amount": amount or plan_doc.price,
			"currency": currency or plan_doc.currency,
			"sale_source": "Web",
			"phone_number": phone_number or "",
			"payment_method": payment_method or "",
			"checkout_token": uuid.uuid4().hex,
			"status": "Draft",
		}
	).insert(ignore_permissions=True)

	try:
		result = initiate_voucher_payment(sale.name, phone_number, payment_method, check_permission=False)
	except Exception:
		sale.db_set("status", "Payment Failed", update_modified=True)
		# The exception re-raised below would roll back this update, so persist
		# the terminal status explicitly before propagating.
		frappe.db.commit()
		raise
	if not result.get("success"):
		sale.db_set("status", "Payment Failed", update_modified=True)
		frappe.db.commit()
		frappe.throw(result.get("error") or _("Payment could not be initiated."))

	return {
		"success": True,
		"checkout_token": sale.checkout_token,
		"merchant_reference": result.get("merchant_reference"),
		"poll_url": result.get("poll_url"),
		"reference_number": result.get("reference_number"),
		"payment_gateway": result.get("payment_gateway"),
	}


@frappe.whitelist(allow_guest=True)
def get_voucher_sale_status(checkout_token):
	"""Public status polling. The voucher code is only returned once Completed."""
	if not checkout_token:
		return {"status": "Not Found"}
	sale_name = frappe.db.get_value("Voucher Sale", {"checkout_token": checkout_token}, "name")
	if not sale_name:
		return {"status": "Not Found"}
	sale = frappe.get_doc("Voucher Sale", sale_name)
	result = {"status": sale.status, "amount": sale.amount, "currency": sale.currency, "plan": sale.plan}
	if sale.status == "Completed" and sale.voucher_code:
		result["voucher_code"] = sale.voucher_code
		result["voucher_id"] = sale.voucher_id
		result["invoice"] = sale.invoice_name
	return result


@frappe.whitelist(allow_guest=True)
def confirm_voucher_web_checkout(checkout_token):
	"""Guest-safe completion check for the public checkout page.

	The web page polls this on an interval. It verifies the real PesaPay
	transaction status via the stored poll_url, then fulfills the Voucher
	Sale on success. This doubles as both the status poll and the
	confirmation trigger, so the page no longer depends on the (often
	unreachable in dev) PesaPay webhook or the 60s scheduler to advance the
	sale. The production webhook still fulfills via `on_payment_authorized`;
	this just makes the client self-sufficient."""
	if not checkout_token:
		return {"status": "Not Found"}
	sale_name = frappe.db.get_value("Voucher Sale", {"checkout_token": checkout_token}, "name")
	if not sale_name:
		return {"status": "Not Found"}

	sale = frappe.get_doc("Voucher Sale", sale_name, ignore_permissions=True)
	if sale.status == "Completed" and sale.voucher_code:
		return {"status": "Completed", **_fulfillment_result(sale)}
	if sale.status == "Payment Failed":
		return {"status": "Payment Failed"}
	if sale.status not in ("Payment Pending", "Payment Confirmed"):
		return {"status": sale.status}

	if sale.poll_url and _should_poll_pesepay(checkout_token):
		try:
			from pesepay.pesepay.doctype.pesepay_settings.pesepay_settings import poll_payment_status

			result = poll_payment_status(sale.poll_url, sale.payment_gateway) or {}
			pesa_status = result.get("transactionStatus")
			if pesa_status == "SUCCESS":
				confirm_voucher_payment(sale.merchant_reference)
				sale = frappe.get_doc("Voucher Sale", sale_name, ignore_permissions=True)
				return {"status": "Completed", **_fulfillment_result(sale)}
			if pesa_status == "FAILED":
				sale.db_set("status", "Payment Failed", update_modified=True)
				return {"status": "Payment Failed"}
		except Exception:
			frappe.log_error(frappe.get_traceback(), "RadiusDesk web checkout poll")

	return {
		"status": sale.status,
		"amount": sale.amount,
		"currency": sale.currency,
		"plan": sale.plan,
	}


@frappe.whitelist()
def retry_voucher_sale(sale_name):
	"""Re-run fulfillment for a failed or partially-completed sale (manual
	retry). Resumes from "Voucher Created" too, so an invoice step that failed
	after the voucher was made is not a dead end."""
	sale = frappe.get_doc("Voucher Sale", sale_name, check_permission=True)
	if sale.status not in ("Fulfillment Failed", "Voucher Created"):
		frappe.throw(_("Only Fulfillment Failed or Voucher Created sales can be retried."))
	if sale.status == "Fulfillment Failed":
		sale.db_set("status", "Payment Confirmed", update_modified=True)
		sale.db_set("radius_error", "", update_modified=True)
	return {"ok": True, **_fulfillment_result(fulfill_voucher_sale(sale.name))}


@frappe.whitelist()
def fulfill_voucher_sale(sale_name):
	"""The single fulfillment path: create the RadiusDesk voucher (only after
	payment is confirmed), then attach the invoice. Idempotent and resumable:
	a sale that already has a voucher but no invoice (e.g. the invoice step
	failed on a prior run) resumes from the invoice step instead of re-creating
	the voucher."""
	sale = frappe.get_doc("Voucher Sale", sale_name, ignore_permissions=True)
	if sale.status == "Completed" and sale.voucher_code:
		return _fulfillment_result(sale)
	if sale.status in SUCCESS_STATUSES and sale.voucher_code and sale.invoice_doctype and sale.invoice_name:
		return _fulfillment_result(sale)
	if sale.status not in ("Payment Confirmed", "Fulfillment Failed", "Voucher Created"):
		frappe.throw(
			_("Voucher Sale {0} is not ready for fulfillment (status: {1}).").format(sale.name, sale.status)
		)

	resume = bool(sale.voucher_code)
	if not resume:
		try:
			settings = _get_settings()
			plan = frappe.get_doc("Voucher Plan", sale.plan, ignore_permissions=True)
			if plan.company != settings.default_company:
				frappe.throw(
					_("Voucher Plan {0} belongs to {1} but Radius Desk Settings use {2}.").format(
						plan.name, plan.company, settings.default_company
					)
				)
			connector = _get_connector(settings)
			# Ambiguity guard: RadiusDesk's add endpoint is not idempotent. If a
			# previous attempt created the voucher but the response was lost
			# (timeout mid-flight), the sale has no recorded code and a blind
			# retry would create a duplicate. Vouchers carry extra_value=sale
			# name for exactly this traceability — look before creating. The
			# lookup is best-effort: if it fails, real errors still surface on
			# the create call below.
			try:
				existing = connector.find_voucher_by_extra_value(sale.name)
			except Exception:
				existing = None
			if isinstance(existing, dict) and existing.get("name"):
				result = existing
			else:
				result = connector.create_voucher(
					realm_id=plan.radius_realm_id,
					profile_id=plan.radius_profile_id,
					never_expire=cint(plan.never_expire),
					extra_value=sale.name,
				)
		except RadiusDeskException as exc:
			sale.db_set("status", "Fulfillment Failed", update_modified=True)
			sale.db_set("radius_error", str(exc), update_modified=True)
			# Persist the terminal state before propagating: callers that roll back
			# on exception (e.g. the retry scheduler) must not lose the voucher
			# milestone — otherwise a re-run would re-create the voucher.
			frappe.db.commit()
			frappe.throw(_("Could not create the voucher on RadiusDesk: {0}").format(exc))
		except Exception:
			# Any failure here means money was taken but no voucher was produced —
			# leave the sale retryable rather than stranded.
			sale.db_set("status", "Fulfillment Failed", update_modified=True)
			sale.db_set("radius_error", frappe.get_traceback(), update_modified=True)
			# Persist the terminal state before propagating: callers that roll back
			# on exception (e.g. the retry scheduler) must not lose the voucher
			# milestone — otherwise a re-run would re-create the voucher.
			frappe.db.commit()
			raise

		sale.db_set("voucher_code", result["name"], update_modified=True)
		sale.db_set("voucher_id", result["id"], update_modified=True)
		sale.db_set("status", "Voucher Created", update_modified=True)

	# Invoice step (runs for both fresh and resumed sales): stamp an already
	# linked invoice, otherwise create one. This must not be gated behind the
	# resume check — a brand-new sale has no invoice yet and would otherwise be
	# left Completed with no attached invoice.
	if sale.invoice_doctype and sale.invoice_name:
		_stamp_invoice(sale)
	else:
		try:
			settings = _get_settings()
			plan = frappe.get_doc("Voucher Plan", sale.plan)
			invoice_name, invoice_type = _create_pos_invoice(sale, plan, settings)
		except Exception:
			# Voucher already exists but the invoice step failed — mark the sale
			# retryable so the web page (and admin retry) see Fulfillment Failed
			# instead of a stranded "Voucher Created".
			sale.db_set("status", "Fulfillment Failed", update_modified=True)
			sale.db_set("radius_error", frappe.get_traceback(), update_modified=True)
			# Persist the terminal state before propagating: callers that roll back
			# on exception (e.g. the retry scheduler) must not lose the voucher
			# milestone — otherwise a re-run would re-create the voucher.
			frappe.db.commit()
			raise
		sale.db_set("invoice_doctype", invoice_type, update_modified=True)
		sale.db_set("invoice_name", invoice_name, update_modified=True)

	sale.db_set("status", "Completed", update_modified=True)
	return _fulfillment_result(sale)
