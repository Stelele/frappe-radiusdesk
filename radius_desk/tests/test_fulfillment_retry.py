from __future__ import annotations

from unittest.mock import patch

import frappe
from erpnext.accounts.test.accounts_mixin import AccountsTestMixin
from erpnext.stock.doctype.item.test_item import create_item
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from radius_desk.radius_desk.utils import fulfillment_retry as fr_retry
from radius_desk.tests.settings_guard import guard_shared_configuration


class TestFulfillmentRetry(AccountsTestMixin, IntegrationTestCase):
	def setUp(self):
		guard_shared_configuration(self)
		self.create_company("_Test Company", "_TC")
		create_item("WiFi Voucher", is_stock_item=0, company="_Test Company")
		self.create_customer("Walk-in Customer")
		self._setup_pesepay_gateway()
		self._setup_settings()

	def _setup_pesepay_gateway(self):
		gateway_name = "Test Gateway"
		if not frappe.db.exists("Pesepay Settings", gateway_name):
			frappe.get_doc(
				{
					"doctype": "Pesepay Settings",
					"gateway_name": gateway_name,
					"integration_key": "test-key",
					"encryption_key": "0123456789abcdef0123456789abcdef",
					"use_sandbox": 1,
				}
			).insert(ignore_permissions=True)
		gateway = f"Pesepay-{gateway_name}"
		if not frappe.db.exists("Payment Gateway", gateway):
			frappe.get_doc(
				{
					"doctype": "Payment Gateway",
					"gateway": gateway,
					"gateway_settings": "Pesepay Settings",
					"gateway_controller": gateway_name,
				}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Mode of Payment", gateway):
			frappe.get_doc(
				{
					"doctype": "Mode of Payment",
					"mode_of_payment": gateway,
					"enabled": 1,
					"type": "Bank",
				}
			).insert(ignore_permissions=True)
		from erpnext.accounts.doctype.mode_of_payment.test_mode_of_payment import (
			set_default_account_for_mode_of_payment,
		)

		set_default_account_for_mode_of_payment(
			frappe.get_doc("Mode of Payment", gateway), "_Test Company", self.cash
		)
		if not frappe.db.exists(
			"Payment Gateway Account", {"payment_gateway": gateway, "company": "_Test Company"}
		):
			frappe.get_doc(
				{
					"doctype": "Payment Gateway Account",
					"payment_gateway": gateway,
					"payment_account": self.cash,
					"company": "_Test Company",
					"payment_channel": "Email",
				}
			).insert(ignore_permissions=True)

	def _setup_settings(self):
		settings = frappe.get_single("Radius Desk Settings")
		settings.update(
			{
				"server_url": "https://radius.example.com",
				"username": "admin",
				"password": "secret",
				"cloud_id": "1",
				"default_company": "_Test Company",
				"default_walkin_customer": "Walk-in Customer",
				"pesepay_gateway": "Pesepay-Test Gateway",
			}
		)
		settings.save(ignore_permissions=True)

	def _make_failed_sale(self, minutes_old=30):
		if not frappe.db.exists("Voucher Plan", "_Test Retry Plan"):
			frappe.get_doc(
				{
					"doctype": "Voucher Plan",
					"plan_name": "_Test Retry Plan",
					"item": "WiFi Voucher",
					"company": "_Test Company",
					"radius_realm_id": 1,
					"radius_profile_id": 1,
					"price": 10,
					"currency": "INR",
					"enabled": 1,
				}
			).insert(ignore_permissions=True)
		sale = frappe.get_doc(
			{
				"doctype": "Voucher Sale",
				"plan": "_Test Retry Plan",
				"amount": 10,
				"currency": "INR",
				"sale_source": "Web",
				"status": "Fulfillment Failed",
				"radius_error": "boom",
				"checkout_token": "retrytok0001",
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value(
			"Voucher Sale",
			sale.name,
			"modified",
			add_to_date(now_datetime(), minutes=-minutes_old),
			update_modified=False,
		)
		# The site carries older real Fulfillment Failed rows; the batch (clamped
		# to a single sale in these tests) picks the oldest, so backdate this sale
		# beyond every real row to keep the run fully hermetic.
		frappe.db.set_value(
			"Voucher Sale", sale.name, "modified", "2000-01-01 00:00:00", update_modified=False
		)
		# The scheduler acts on already-committed sales; without this commit the
		# batch's per-sale rollback would undo the test's own insert.
		frappe.db.commit()
		self.addCleanup(frappe.delete_doc, "Voucher Sale", sale.name, force=True)
		return sale.name

	def test_retries_and_completes_stale_failed_sale(self):
		name = self._make_failed_sale()
		with (
			patch.object(fr_retry, "BATCH_LIMIT", 1),
			patch(
				"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector"
			) as mock_conn,
		):
			mock_conn.return_value.create_voucher.return_value = {"id": 77, "name": "RETRY-OK"}
			fr_retry.retry_fulfillment_failed_sales()
		sale = frappe.get_doc("Voucher Sale", name)
		self.assertEqual(sale.status, "Completed")
		self.assertEqual(sale.voucher_code, "RETRY-OK")

	def test_still_failing_sale_notifies_operator_throttled(self):
		name = self._make_failed_sale()
		frappe.cache().delete(frappe.cache().make_key("rd-fulfillment-notify"))
		with (
			patch.object(fr_retry, "BATCH_LIMIT", 1),
			patch(
				"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector"
			) as mock_conn,
			patch("frappe.sendmail") as mock_send,
		):
			mock_conn.return_value.create_voucher.side_effect = Exception("still down")
			fr_retry.retry_fulfillment_failed_sales()
			# second run within the throttle window must not re-send
			fr_retry.retry_fulfillment_failed_sales()
		self.assertEqual(mock_send.call_count, 1)
		sale = frappe.get_doc("Voucher Sale", name)
		self.assertEqual(sale.status, "Fulfillment Failed")

	def test_fresh_failure_is_not_picked_up(self):
		name = self._make_failed_sale(minutes_old=0)
		# `_make_failed_sale` backdates to 2000-01-01 for the oldest-first batch
		# tests; this test needs a genuinely fresh row, so pin `modified` to now.
		frappe.db.set_value("Voucher Sale", name, "modified", now_datetime(), update_modified=False)
		frappe.db.commit()
		with (
			patch(
				"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector"
			) as mock_conn,
			patch.object(fr_retry, "NOTIFY_AFTER_MINUTES", 100000),
		):
			fr_retry.retry_fulfillment_failed_sales()
		self.assertEqual(mock_conn.return_value.create_voucher.call_count, 0)
		sale = frappe.get_doc("Voucher Sale", name)
		self.assertEqual(sale.status, "Fulfillment Failed")
		self.assertEqual(sale.radius_error, "boom")

	def test_retry_after_invoice_failure_does_not_duplicate_voucher(self):
		name = self._make_failed_sale()
		calls = []

		def voucher_side_effect(**kwargs):
			calls.append(1)
			return {"id": 90, "name": "NODUP-1"}

		with (
			patch.object(fr_retry, "BATCH_LIMIT", 1),
			patch(
				"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector"
			) as mock_conn,
			patch(
				"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._create_pos_invoice",
				side_effect=Exception("invoice boom"),
			),
			patch("frappe.sendmail"),
		):
			mock_conn.return_value.create_voucher.side_effect = voucher_side_effect
			fr_retry.retry_fulfillment_failed_sales()  # creates voucher, invoice fails
		sale = frappe.get_doc("Voucher Sale", name)
		self.assertEqual(sale.status, "Fulfillment Failed")
		self.assertEqual(sale.voucher_code, "NODUP-1")  # milestone survived

		# Make it stale again, older than the real backlog so oldest-first picks
		# this row, and retry — it must resume, not re-create the voucher.
		frappe.db.set_value(
			"Voucher Sale", name, "modified", "2000-01-01 00:00:00", update_modified=False
		)
		with (
			patch.object(fr_retry, "BATCH_LIMIT", 1),
			patch(
				"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector"
			) as mock_conn2,
			patch(
				"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._create_pos_invoice",
				side_effect=Exception("invoice boom again"),
			),
			patch("frappe.sendmail"),
		):
			mock_conn2.return_value.create_voucher.side_effect = voucher_side_effect
			fr_retry.retry_fulfillment_failed_sales()
		self.assertEqual(len(calls), 1)  # no duplicate voucher
		self.assertEqual(frappe.get_doc("Voucher Sale", name).voucher_code, "NODUP-1")
