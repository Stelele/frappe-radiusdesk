from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document


class RadiusDeskSettings(Document):
	def validate(self):
		self.validate_pesepay_gateway()

	def validate_pesepay_gateway(self):
		if self.pesepay_gateway:
			settings_doctype = frappe.db.get_value(
				"Payment Gateway", self.pesepay_gateway, "gateway_settings"
			)
			if settings_doctype != "Pesepay Settings":
				frappe.throw(_("Pesepay Gateway must be a Pesepay payment gateway."))
