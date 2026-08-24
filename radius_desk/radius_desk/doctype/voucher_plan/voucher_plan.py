from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class VoucherPlan(Document):
	def validate(self):
		self.validate_currency_matches_company()
		self.sync_price_from_item()

	def sync_price_from_item(self):
		"""Keep the stored Price in step with the item's Standard Selling price
		list (the single source of truth). When no Item Price exists the plan's
		own price is left untouched so it remains a usable fallback."""
		if not self.item:
			return
		from radius_desk.radius_desk.doctype.voucher_sale.voucher_sale import get_voucher_price

		price, currency = get_voucher_price(self.item, self.company, self.price, self.currency)
		if price is not None:
			self.price = price
			if currency:
				self.currency = currency

	def validate_currency_matches_company(self):
		company_currency = frappe.db.get_value("Company", self.company, "default_currency")
		if company_currency and self.currency and company_currency != self.currency:
			frappe.throw(
				_("Currency must match the default currency of the Company ({0}).").format(company_currency)
			)
