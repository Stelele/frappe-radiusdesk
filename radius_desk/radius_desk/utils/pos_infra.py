from __future__ import annotations

import frappe
from frappe import _

POS_USER = "Guest"


def ensure_pos_profile(company: str, payment_modes: list[str] | None = None) -> str:
	"""Return the app-managed POS Profile for the company, creating it if missing."""
	_ensure_pos_invoice_mode()
	abbr = frappe.get_cached_value("Company", company, "abbr")
	name = f"RD Web - {abbr}"

	if frappe.db.exists("POS Profile", name):
		profile = frappe.get_doc("POS Profile", name)
		modes = payment_modes or [row.mode_of_payment for row in profile.payments]
		changed = False
		for mode in modes:
			if not any(row.mode_of_payment == mode for row in profile.payments):
				profile.append("payments", {"mode_of_payment": mode, "default": not profile.payments})
				changed = True
		if changed:
			profile.save(ignore_permissions=True)
		return name

	modes = payment_modes or []
	if not modes:
		frappe.throw(_("Radius Desk POS Profile needs at least one payment mode."))
	frappe.get_doc(_pos_profile_dict(company, name, modes)).insert(ignore_permissions=True)
	return name


def get_invoice_type() -> str:
	"""Return the system-configured invoice type for sales made by the app.

	Honors ``POS Settings.invoice_type`` (``POS Invoice`` or ``Sales Invoice``)
	so the guest portal follows the site's configuration instead of being
	hard-wired to POS Invoice.
	"""
	try:
		return frappe.get_single("POS Settings").invoice_type or "POS Invoice"
	except Exception:
		return "POS Invoice"


def _ensure_pos_invoice_mode():
	"""No-op.

	The invoice type is now admin-configured via ``POS Settings.invoice_type``;
	the app no longer forces POS Invoice mode. Kept so existing callers keep
	working.
	"""
	return


def _pos_profile_dict(company: str, name: str, modes: list[str]) -> dict:
	currency = frappe.get_cached_value("Company", company, "default_currency")
	cost_center = frappe.get_cached_value("Company", company, "cost_center")
	income_account = frappe.get_cached_value("Company", company, "default_income_account")
	expense_account = frappe.get_cached_value("Company", company, "default_expense_account")
	return {
		"doctype": "POS Profile",
		"name": name,
		"company": company,
		"currency": currency,
		"income_account": income_account,
		"expense_account": expense_account,
		"cost_center": cost_center,
		"write_off_account": frappe.db.get_value("Company", company, "write_off_account") or expense_account,
		"write_off_cost_center": cost_center,
		"write_off_limit": 0,
		"warehouse": _pick_warehouse(company),
		"customer_group": frappe.db.get_value("Customer Group", {"is_group": 0}, "name"),
		"territory": frappe.db.get_value("Territory", {"is_group": 0}, "name"),
		"selling_price_list": _pick_selling_price_list(company),
		"naming_series": "RD-POS-",
		"payments": [{"mode_of_payment": m, "default": i == 0} for i, m in enumerate(modes)],
	}


def _pick_warehouse(company: str) -> str:
	stock_settings = frappe.get_single("Stock Settings")
	if (
		stock_settings.default_warehouse
		and frappe.db.get_value("Warehouse", stock_settings.default_warehouse, "company") == company
	):
		return stock_settings.default_warehouse
	warehouse = frappe.db.get_value("Warehouse", {"company": company, "is_group": 0}, "name")
	if warehouse:
		return warehouse
	abbr = frappe.get_cached_value("Company", company, "abbr")
	name = f"RD Web - {abbr}"
	if not frappe.db.exists("Warehouse", name):
		frappe.get_doc(
			{"doctype": "Warehouse", "warehouse_name": "RD Web", "company": company, "is_group": 0}
		).insert(ignore_permissions=True)
	return name


def _pick_selling_price_list(company: str) -> str:
	price_list = frappe.get_single("Selling Settings").selling_price_list
	if not price_list or not frappe.db.exists("Price List", price_list):
		price_list = "Standard Selling"
	if not frappe.db.exists("Price List", price_list):
		frappe.get_doc(
			{
				"doctype": "Price List",
				"price_list_name": price_list,
				"currency": frappe.get_cached_value("Company", company, "default_currency"),
				"selling": 1,
			}
		).insert(ignore_permissions=True)
	return price_list


def get_pesepay_mode_of_payment(gateway: str) -> str:
	"""Return the Mode of Payment for a PesaPay Payment Gateway."""
	controller = frappe.db.get_value("Payment Gateway", gateway, "gateway_controller")
	if not controller:
		frappe.throw(_("Payment Gateway {0} not configured").format(gateway))
	gateway_name = frappe.db.get_value("Pesepay Settings", controller, "gateway_name")
	return f"Pesepay-{gateway_name}"


def ensure_today_open_pos_entry(company: str, payment_mode: str) -> str:
	"""Return today's open POS Opening Entry for the app's POS Profile, closing
	any prior open entry first."""
	pos_profile = ensure_pos_profile(company, [payment_mode])
	today = frappe.utils.today()

	open_entries = frappe.get_all(
		"POS Opening Entry",
		filters={"pos_profile": pos_profile, "status": "Open"},
		fields=["name", "period_start_date"],
		order_by="period_start_date desc",
	)
	if open_entries:
		if frappe.utils.get_date_str(open_entries[0].period_start_date) == today:
			return open_entries[0].name
		for entry in open_entries:
			_close_open_entry(entry.name)

	entry = frappe.get_doc(
		{
			"doctype": "POS Opening Entry",
			"period_start_date": frappe.utils.now(),
			"posting_date": today,
			"company": company,
			"pos_profile": pos_profile,
			"user": POS_USER,
			"balance_details": _zero_balance_details(pos_profile, company),
		}
	)
	entry.insert(ignore_permissions=True)
	entry.submit()
	return entry.name


def _close_open_entry(opening_entry: str):
	"""Close an open POS Opening Entry with a POS Closing Entry."""
	from erpnext.accounts.doctype.pos_closing_entry.pos_closing_entry import (
		make_closing_entry_from_opening,
	)

	opening = frappe.get_doc("POS Opening Entry", opening_entry)
	closing = make_closing_entry_from_opening(opening)
	closing.insert(ignore_permissions=True)
	closing.submit()
	# Consolidation runs via a background job; close the opening entry
	# synchronously so today's entry can be created immediately. This mirrors
	# POSClosingEntry.update_opening_entry() but uses db.set_value so it is
	# permission-safe when called from a Guest (web checkout) context.
	frappe.db.set_value(
		"POS Opening Entry",
		opening_entry,
		{"pos_closing_entry": closing.name, "status": "Closed"},
	)


def _zero_balance_details(pos_profile: str, company: str) -> list[dict]:
	profile = frappe.get_doc("POS Profile", pos_profile)
	details = []
	for row in profile.payments:
		if frappe.db.get_value(
			"Mode of Payment Account",
			{"parent": row.mode_of_payment, "company": company},
			"default_account",
		):
			details.append({"mode_of_payment": row.mode_of_payment, "opening_amount": 0})
	if not details:
		frappe.throw(
			_(
				"The Mode of Payment used by the Radius Desk POS Profile needs a default Cash/Bank account for {0}."
			).format(company)
		)
	return details


def ensure_daily_pos_opening_entry():
	"""Daily scheduler hook: keep the app-managed POS opening entry fresh."""
	if frappe.flags.in_install or frappe.flags.in_migrate or frappe.flags.in_test:
		return
	# Only relevant when the site sells through POS Invoices.
	if get_invoice_type() != "POS Invoice":
		return
	settings = frappe.get_single("Radius Desk Settings")
	if not settings.default_company or not settings.pesepay_gateway:
		return
	try:
		mode = get_pesepay_mode_of_payment(settings.pesepay_gateway)
		ensure_today_open_pos_entry(settings.default_company, mode)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "RadiusDesk daily POS opening entry")
