# Copyright (c) 2026, Gift Mugweni and Contributors
# See license.txt

from __future__ import annotations

import frappe
from erpnext.accounts.test.accounts_mixin import AccountsTestMixin
from erpnext.stock.doctype.item.test_item import create_item
from frappe.tests import IntegrationTestCase

from radius_desk.radius_desk.doctype.voucher_sale.voucher_sale import (
	get_standard_selling_price_list,
)

# On IntegrationTestCase, the doctype test records and all
# link-field test record dependencies are recursively loaded
# Use these module variables to add/remove to/from that list
EXTRA_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]
IGNORE_TEST_RECORD_DEPENDENCIES = []  # eg. ["User"]

PLAN_NAME = "Plan Sync Item Plan"
ITEM_CODE = "Plan Sync Item"


class IntegrationTestVoucherPlan(AccountsTestMixin, IntegrationTestCase):
	"""
	Integration tests for VoucherPlan.
	Use this class for testing interactions between multiple components.
	"""

	def setUp(self):
		self.create_company("_Test Company", "_TC")
		create_item(ITEM_CODE, is_stock_item=0, company="_Test Company")
		if frappe.db.exists("Voucher Plan", PLAN_NAME):
			frappe.delete_doc("Voucher Plan", PLAN_NAME, force=True)
		self._clear_item_prices()

	def _clear_item_prices(self):
		"""Remove leftover Item Prices so each test starts from a clean rate."""
		for name in frappe.get_all("Item Price", {"item_code": ITEM_CODE}, pluck="name"):
			frappe.delete_doc("Item Price", name, force=True)

	def _make_item_price(self, rate):
		frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": ITEM_CODE,
				"price_list": get_standard_selling_price_list(),
				"selling": 1,
				"price_list_rate": rate,
			}
		).insert(ignore_permissions=True)

	def _make_plan(self):
		return frappe.get_doc(
			{
				"doctype": "Voucher Plan",
				"plan_name": PLAN_NAME,
				"item": ITEM_CODE,
				"company": "_Test Company",
				"radius_realm_id": "1",
				"radius_profile_id": "2",
				"currency": frappe.db.get_value("Company", "_Test Company", "default_currency"),
			}
		).insert(ignore_permissions=True)

	def test_price_syncs_from_item_price_without_explicit_price(self):
		self._make_item_price(25)
		plan = self._make_plan()
		self.assertEqual(plan.price, 25)

	def test_plan_without_item_price_raises_actionable_error(self):
		with self.assertRaises(frappe.ValidationError) as se:
			self._make_plan()
		self.assertIn("Item Price", str(se.exception))
		self.assertIn(ITEM_CODE, str(se.exception))
