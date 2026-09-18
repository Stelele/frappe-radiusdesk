from __future__ import annotations

import json

import frappe
from frappe import _

no_cache = 1


def get_context(context):
	from radius_desk.radius_desk.doctype.voucher_sale.voucher_sale import get_voucher_plans
	from radius_desk.radius_desk.utils.hotspot_embed import validate_hotspot_url

	context.no_cache = 1
	context.plans = get_voucher_plans()
	# Brand the website footer for this page only: footer_info.html renders
	# `footer_powered` raw when set, falling back to "Powered by ERPNext".
	# (Embed mode strips the footer entirely, so this only affects normal mode.)
	context.footer_powered = _("Powered by {0}").format(
		'<a href="https://bsmtechsolutions.co.zw/" target="_blank" class="text-muted">Bsmitech Solutions</a>'
	)

	if frappe.form_dict.get("embed") == "1":
		context.embed = True
		# Production pins the exact router login URL in Radius Desk Settings;
		# when unset, the validator falls back to the private-IP heuristic.
		expected = frappe.db.get_single_value("Radius Desk Settings", "hotspot_login_url") or None
		# Captive-portal return leg: after payment the portal redirects to the
		# portal login page itself (droplet, trusted HTTPS) with #rd-voucher=.
		# The validator only ever allows this single pinned https prefix.
		return_prefix = (
			frappe.db.get_single_value("Radius Desk Settings", "hotspot_portal_return_prefix")
			or "https://radius.giftmugweni.com/login/"
		)
		# NOTE: embed_json is rendered with | safe in index.html — only values
		# validated by validate_hotspot_url may ever go into it (charset is
		# injection-safe by design).
		context.linklogin = validate_hotspot_url(
			frappe.form_dict.get("linklogin"), expected_url=expected, return_prefix=return_prefix
		)
		context.linkorig = validate_hotspot_url(frappe.form_dict.get("linkorig"), expected_url=expected)
		context.embed_json = json.dumps({"linklogin": context.linklogin, "linkorig": context.linkorig})
