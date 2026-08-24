from __future__ import annotations

import frappe

DOCTYPES = [
	{
		"doctype": "DocType",
		"name": "Radius Desk Settings",
		"module": "Radius Desk",
		"custom": 0,
		"issingle": 1,
		"istable": 0,
		"is_submittable": 0,
		"editable_grid": 0,
		"engine": "InnoDB",
		"fields": [
			{"fieldname": "server_url", "label": "Server URL", "fieldtype": "Data", "reqd": 1},
			{"fieldname": "username", "label": "Username", "fieldtype": "Data", "reqd": 1},
			{"fieldname": "password", "label": "Password", "fieldtype": "Password", "reqd": 1},
			{"fieldname": "cloud_id", "label": "Cloud ID", "fieldtype": "Data", "reqd": 1},
			{"fieldname": "section_break_defaults", "fieldtype": "Section Break", "label": "Defaults"},
			{
				"fieldname": "default_company",
				"label": "Default Company",
				"fieldtype": "Link",
				"options": "Company",
				"reqd": 1,
			},
			{
				"fieldname": "default_walkin_customer",
				"label": "Default Walk-in Customer",
				"fieldtype": "Link",
				"options": "Customer",
				"reqd": 1,
			},
			{
				"fieldname": "pesepay_gateway",
				"label": "Pesepay Gateway",
				"fieldtype": "Link",
				"options": "Payment Gateway",
				"reqd": 1,
			},
		],
		"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
	},
	{
		"doctype": "DocType",
		"name": "Voucher Plan",
		"module": "Radius Desk",
		"custom": 0,
		"issingle": 0,
		"istable": 0,
		"is_submittable": 0,
		"engine": "InnoDB",
		"autoname": "field:plan_name",
		"fields": [
			{"fieldname": "enabled", "label": "Enabled", "fieldtype": "Check", "default": "1"},
			{"fieldname": "plan_name", "label": "Plan Name", "fieldtype": "Data", "reqd": 1, "unique": 1},
			{"fieldname": "description", "label": "Description", "fieldtype": "Text"},
			{"fieldname": "item", "label": "Item", "fieldtype": "Link", "options": "Item", "reqd": 1},
			{
				"fieldname": "company",
				"label": "Company",
				"fieldtype": "Link",
				"options": "Company",
				"reqd": 1,
			},
			{"fieldname": "radius_realm_id", "label": "RadiusDesk Realm ID", "fieldtype": "Data", "reqd": 1},
			{
				"fieldname": "radius_profile_id",
				"label": "RadiusDesk Profile ID",
				"fieldtype": "Data",
				"reqd": 1,
			},
			{"fieldname": "price", "label": "Price", "fieldtype": "Currency", "reqd": 1},
			{
				"fieldname": "currency",
				"label": "Currency",
				"fieldtype": "Link",
				"options": "Currency",
				"reqd": 1,
			},
			{"fieldname": "never_expire", "label": "Never Expire", "fieldtype": "Check", "default": "1"},
			{"fieldname": "sort_order", "label": "Sort Order", "fieldtype": "Int", "default": "0"},
		],
		"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
	},
	{
		"doctype": "DocType",
		"name": "Voucher Sale",
		"module": "Radius Desk",
		"custom": 0,
		"issingle": 0,
		"istable": 0,
		"is_submittable": 0,
		"engine": "InnoDB",
		"autoname": "format:RD-VS-{YYYY}{MM}{DD}-{#####}",
		"fields": [
			{
				"fieldname": "plan",
				"label": "Voucher Plan",
				"fieldtype": "Link",
				"options": "Voucher Plan",
				"reqd": 1,
			},
			{"fieldname": "amount", "label": "Amount", "fieldtype": "Currency", "reqd": 1},
			{
				"fieldname": "currency",
				"label": "Currency",
				"fieldtype": "Link",
				"options": "Currency",
				"reqd": 1,
			},
			{"fieldname": "phone_number", "label": "Phone Number", "fieldtype": "Data"},
			{
				"fieldname": "payment_method",
				"label": "Payment Method",
				"fieldtype": "Select",
				"options": "EcoCash\nInnBucks\nOmari\nVisa\nMasterCard\nZimswitch",
			},
			{
				"fieldname": "sale_source",
				"label": "Sale Source",
				"fieldtype": "Select",
				"options": "POS\nWeb",
			},
			{"fieldname": "section_break_payment", "fieldtype": "Section Break", "label": "Payment"},
			{
				"fieldname": "payment_gateway",
				"label": "Payment Gateway",
				"fieldtype": "Link",
				"options": "Payment Gateway",
			},
			{"fieldname": "merchant_reference", "label": "Merchant Reference", "fieldtype": "Data"},
			{
				"fieldname": "pesepay_reference_number",
				"label": "Pesepay Reference Number",
				"fieldtype": "Data",
			},
			{"fieldname": "poll_url", "label": "Poll URL", "fieldtype": "Small Text"},
			{"fieldname": "section_break_voucher", "fieldtype": "Section Break", "label": "Voucher"},
			{
				"fieldname": "status",
				"label": "Status",
				"fieldtype": "Select",
				"options": "Draft\nPayment Pending\nPayment Confirmed\nVoucher Created\nCompleted\nPayment Failed\nFulfillment Failed",
				"default": "Draft",
			},
			{"fieldname": "voucher_code", "label": "Voucher Code", "fieldtype": "Data", "read_only": 1},
			{"fieldname": "voucher_id", "label": "Voucher ID", "fieldtype": "Data", "read_only": 1},
			{"fieldname": "radius_error", "label": "Radius Error", "fieldtype": "Text", "read_only": 1},
			{"fieldname": "section_break_invoice", "fieldtype": "Section Break", "label": "Invoice"},
			{
				"fieldname": "invoice_doctype",
				"label": "Invoice Doctype",
				"fieldtype": "Link",
				"options": "DocType",
			},
			{
				"fieldname": "invoice_name",
				"label": "Invoice",
				"fieldtype": "Dynamic Link",
				"options": "invoice_doctype",
			},
			{"fieldname": "checkout_token", "label": "Checkout Token", "fieldtype": "Data", "read_only": 1},
		],
		"permissions": [{"role": "System Manager", "read": 1, "write": 1, "create": 1, "delete": 1}],
	},
]


def create_doctypes():
	"""Insert DocTypes if missing. With developer_mode on, writes JSON/controller files to disk."""
	for spec in DOCTYPES:
		if frappe.db.exists("DocType", spec["name"]):
			continue
		frappe.get_doc(spec).insert(ignore_permissions=True)
		frappe.db.commit()
	frappe.db.commit()
