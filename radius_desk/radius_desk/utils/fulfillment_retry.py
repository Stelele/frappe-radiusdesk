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
	key = cache.make_key("rd-fulfillment-notify")
	if cache.get(key):
		return
	cache.set(key, 1, ex=NOTIFY_THROTTLE_SECONDS)

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
