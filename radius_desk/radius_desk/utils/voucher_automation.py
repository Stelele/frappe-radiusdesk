from __future__ import annotations

import frappe
from frappe.utils import cint

from radius_desk.radius_desk.doctype.voucher_sale import voucher_sale as vs
from radius_desk.radius_desk.utils.system_user import SYSTEM_USER, ensure_system_user


def create_vouchers_for_invoice(doc, method=None):
	"""Auto-create a RadiusDesk voucher for any invoice (POS or Sales) that
	contains a Voucher Plan item.

	Triggered by the ``on_submit`` doc_event for ``POS Invoice`` / ``Sales
	Invoice``. The actual (privileged) work is enqueued as a background job so
	it runs in a *worker* process, NOT inside the web request that owns the
	cashier's HTTP session. Switching users with ``frappe.set_user`` inside a
	live web request corrupts/destroys that session -- the caller gets logged
	out and every subsequent call returns Guest ("not whitelisted").

	For each matching line item, one Voucher Sale is created per unit of ``qty``
	-- so a cart with several of the same plan (or a mix of plans) yields
	multiple vouchers. Fulfillment runs through the non-PesaPay direct path
	(cash/other mode collected at the till).

	Resilience:
	- Idempotent: if a Voucher Sale already exists for this invoice, do nothing.
	- Never rolls back the invoice submit: every failure is logged and the sale
	  is left retryable (``Fulfillment Failed``).
	"""
	# Skip invoices already linked to a voucher. Fulfillment itself produces an
	# invoice stamped with `radius_voucher_code` and submits it; without this
	# guard the on_submit hook would fire again and spawn a second Voucher Sale
	# (and a second RadiusDesk voucher).
	if doc.get("radius_voucher_code"):
		return

	if frappe.db.count(
		"Voucher Sale", {"invoice_doctype": doc.doctype, "invoice_name": doc.name}
	):
		return

	frappe.enqueue(
		"radius_desk.radius_desk.utils.voucher_automation.process_invoice_vouchers",
		queue="default",
		docdoctype=doc.doctype,
		docname=doc.name,
		job_name=f"voucher-auto-{doc.doctype}-{doc.name}",
		now=frappe.flags.in_test,
	)


def process_invoice_vouchers(docdoctype, docname):
	"""Background job: create + fulfill vouchers for a submitted invoice.

	Runs as the app system user (so it can create and submit the app's own POS
	Invoice, which trips an unconditional ``Account`` permission check that
	ignores ``ignore_permissions``). Because this executes in a worker, the
	cashier's web session is never touched.
	"""
	if frappe.db.count(
		"Voucher Sale", {"invoice_doctype": docdoctype, "invoice_name": docname}
	):
		return

	doc = frappe.get_doc(docdoctype, docname)

	plans = {
		p.item: p
		for p in frappe.get_all(
			"Voucher Plan",
			filters={"enabled": 1, "company": doc.company},
			fields=["name", "item", "company"],
		)
	}
	if not plans:
		return

	source = "POS" if doc.doctype == "POS Invoice" else "Sales"

	original_user = frappe.session.user
	ensure_system_user()
	frappe.set_user(SYSTEM_USER)
	try:
		for row in doc.get("items") or []:
			plan = plans.get(row.item_code)
			if not plan:
				continue
			qty = cint(row.get("qty")) or 1
			for _ in range(qty):
				price, _currency = vs.get_voucher_price(
					plan.item, plan.company, None, doc.currency
				)
				if not price:
					frappe.log_error(
						"No price found for Voucher Plan {0} (item {1}). "
						"Set an Item Price in the Radius Desk selling price list.".format(
							plan.name, plan.item
						),
						"RadiusDesk auto voucher",
					)
					continue
				phone = (
					doc.get("contact_mobile")
					or frappe.db.get_value("Customer", doc.customer, "mobile_no")
					or ""
				)
				sale = frappe.get_doc(
					{
						"doctype": "Voucher Sale",
						"plan": plan.name,
						"amount": price,
						"currency": doc.currency,
						"sale_source": source,
						"invoice_doctype": doc.doctype,
						"invoice_name": doc.name,
						"phone_number": phone or "",
						"status": "Draft",
					}
				).insert(ignore_permissions=True)
				try:
					vs.confirm_voucher_sale(sale.name)
				except Exception:
					frappe.log_error(
						frappe.get_traceback(),
						"RadiusDesk auto voucher fulfillment failed for {0}".format(sale.name),
					)
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			"RadiusDesk auto voucher (invoice submit {0} {1})".format(docdoctype, docname),
		)
	finally:
		frappe.set_user(original_user)
