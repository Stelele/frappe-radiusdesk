from __future__ import annotations

import frappe

CUSTOM_FIELDS = [
	{
		"fieldname": "radius_voucher_code",
		"label": "Radius Voucher Code",
		"fieldtype": "Data",
		"module": "Radius Desk",
		"insert_after": "rounded_total",
	},
	{
		"fieldname": "radius_voucher_plan",
		"label": "Radius Voucher Plan",
		"fieldtype": "Link",
		"options": "Voucher Plan",
		"module": "Radius Desk",
		"insert_after": "radius_voucher_code",
	},
]

INVOICE_DOCTYPES = ["POS Invoice", "Sales Invoice"]


def after_migrate():
	"""Idempotently sync radius_desk custom fields on invoice doctypes, force POS
	Invoice mode so the app's POS invoices are accepted site-wide, and ensure the
	app-owned system user (used for privileged POS Invoice work) exists."""
	_ensure_pos_invoice_mode()
	for doctype in INVOICE_DOCTYPES:
		for cf in CUSTOM_FIELDS:
			name = f"{doctype}-{cf['fieldname']}"
			if frappe.db.exists("Custom Field", name):
				continue
			frappe.get_doc(
				{
					"doctype": "Custom Field",
					"dt": doctype,
					"fieldname": cf["fieldname"],
					"label": cf["label"],
					"fieldtype": cf["fieldtype"],
					"module": cf.get("module"),
					"options": cf.get("options"),
					"insert_after": cf["insert_after"],
				}
			).insert(ignore_permissions=True)
	from radius_desk.radius_desk.utils.system_user import ensure_system_user

	ensure_system_user()
	frappe.db.commit()


def _ensure_pos_invoice_mode():
	"""Force POS Invoice mode so the app's POS invoices are accepted site-wide.

	ERPNext only lets you create a ``POS Invoice`` when ``POS Settings.invoice_type``
	is ``POS Invoice``; in Sales Invoice mode the doctype is disabled and the app's
	till flows (and tests) fail. Honoring a manual admin change is still possible
	via ``get_invoice_type()`` — this just sets a sane default on install/migrate.
	"""
	if not frappe.db.exists("POS Settings", "POS Settings"):
		return
	frappe.db.set_single_value(
		"POS Settings", "invoice_type", "POS Invoice", update_modified=False
	)
