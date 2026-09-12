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