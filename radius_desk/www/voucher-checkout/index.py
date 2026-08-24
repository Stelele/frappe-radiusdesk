from __future__ import annotations

import frappe

no_cache = 1


def get_context(context):
	from radius_desk.radius_desk.doctype.voucher_sale.voucher_sale import get_voucher_plans

	context.no_cache = 1
	context.plans = get_voucher_plans()
