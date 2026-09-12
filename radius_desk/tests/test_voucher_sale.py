from __future__ import annotations

import uuid
from unittest.mock import patch

import frappe
from erpnext.accounts.test.accounts_mixin import AccountsTestMixin
from erpnext.stock.doctype.item.test_item import create_item
from frappe.tests import IntegrationTestCase

from radius_desk.radius_desk.doctype.voucher_sale import voucher_sale as vs
from radius_desk.radius_desk.utils.pos_infra import ensure_today_open_pos_entry
from radius_desk.radius_desk.utils.radiusdesk import RadiusDeskException
from radius_desk.tests.settings_guard import guard_shared_configuration


class TestVoucherSale(AccountsTestMixin, IntegrationTestCase):
	def setUp(self):
		guard_shared_configuration(self)
		self.create_company("_Test Company", "_TC")
		create_item("WiFi Voucher", is_stock_item=0, company="_Test Company")
		self.create_customer("Walk-in Customer")
		# Drop any WiFi Voucher item price left over from a prior (committed)
		# test run so get_voucher_price falls back to the plan price.
		self._clear_wifi_item_prices()
		self._setup_pesepay_gateway()
		self._setup_settings()
		self.plan = self._make_plan()
		frappe.cache().delete_keys("rd-rate-limit:*")

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

	def _make_plan(self):
		if frappe.db.exists("Voucher Plan", "1 Hour"):
			return frappe.get_doc("Voucher Plan", "1 Hour")
		return frappe.get_doc(
			{
				"doctype": "Voucher Plan",
				"plan_name": "1 Hour",
				"item": "WiFi Voucher",
				"company": "_Test Company",
				"radius_realm_id": "1",
				"radius_profile_id": "2",
				"price": 10,
				"currency": "INR",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)

	def _clear_wifi_item_prices(self):
		"""Remove any WiFi Voucher item prices so get_voucher_price falls back
		to the plan price. A committed Item Price bleeds across tests/runs and
		flips the effective currency, so every price test starts clean."""
		for name in frappe.get_all(
			"Item Price", {"item_code": "WiFi Voucher", "price_list": "Standard Selling"}, pluck="name"
		):
			frappe.delete_doc("Item Price", name, force=True)

	def _make_sale(self, status="Draft"):
		return frappe.get_doc(
			{
				"doctype": "Voucher Sale",
				"plan": self.plan.name,
				"amount": 10,
				"currency": "INR",
				"sale_source": "Web",
				"checkout_token": "tok0001",
				"status": status,
			}
		).insert(ignore_permissions=True)

	def test_web_fulfillment_creates_pos_invoice(self):
		sale = self._make_sale()
		sale.db_set("status", "Payment Confirmed")

		with patch("radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector") as mock_conn:
			mock_conn.return_value.create_voucher.return_value = {"id": 42, "name": "HOME-1234"}
			result = vs.fulfill_voucher_sale(sale.name)

		self.assertEqual(result["voucher_code"], "HOME-1234")
		sale.reload()
		self.assertEqual(sale.status, "Completed")
		self.assertEqual(sale.invoice_doctype, "POS Invoice")
		self.assertTrue(sale.invoice_name)
		inv = frappe.get_doc("POS Invoice", sale.invoice_name)
		self.assertEqual(inv.docstatus, 1)
		self.assertTrue(inv.pos_profile)
		self.assertEqual(inv.radius_voucher_code, "HOME-1234")
		self.assertEqual(inv.radius_voucher_plan, self.plan.name)
		self.assertEqual(inv.payments[0].mode_of_payment, "Pesepay-Test Gateway")

	def test_invoice_step_failure_marks_sale_fulfillment_failed(self):
		"""If the POS invoice step fails after the voucher was created, the sale
		must become Fulfillment Failed (retryable) rather than stranded at
		Voucher Created, so the web page/admin can surface a terminal state."""
		sale = self._make_sale()
		sale.db_set("status", "Payment Confirmed")

		with (
			patch("radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector") as mock_conn,
			patch(
				"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._create_pos_invoice",
				side_effect=Exception("invoice boom"),
			),
		):
			mock_conn.return_value.create_voucher.return_value = {"id": 44, "name": "HOME-5678"}
			with self.assertRaises(Exception):
				vs.fulfill_voucher_sale(sale.name)

		sale.reload()
		self.assertEqual(sale.status, "Fulfillment Failed")
		self.assertEqual(sale.voucher_code, "HOME-5678")
		self.assertIn("invoice boom", sale.radius_error)

	def test_guest_fulfillment_uses_system_user_not_admin(self):
		"""The webhook path runs as Guest; fulfillment must work via the app's
		system user with minimal permissions, not by elevating to Administrator."""
		sale = self._make_sale()
		sale.db_set("status", "Payment Confirmed")

		original_user = frappe.session.user
		frappe.set_user("Guest")
		try:
			with patch(
				"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector"
			) as mock_conn:
				mock_conn.return_value.create_voucher.return_value = {"id": 43, "name": "WEB-1234"}
				result = vs.fulfill_voucher_sale(sale.name)
		finally:
			frappe.set_user(original_user)

		self.assertEqual(result["voucher_code"], "WEB-1234")
		sale.reload()
		self.assertEqual(sale.status, "Completed")
		self.assertTrue(sale.invoice_name)
		inv = frappe.get_doc("POS Invoice", sale.invoice_name)
		self.assertEqual(inv.docstatus, 1)
		self.assertEqual(inv.radius_voucher_code, "WEB-1234")
		# Owner is re-stamped to the app-owned cashier (Guest), not the system user
		self.assertEqual(inv.owner, "Guest")

	def test_fulfillment_is_idempotent(self):
		sale = self._make_sale()
		sale.db_set("status", "Payment Confirmed")

		with patch("radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector") as mock_conn:
			mock_conn.return_value.create_voucher.return_value = {"id": 42, "name": "HOME-1234"}
			vs.fulfill_voucher_sale(sale.name)
			vs.fulfill_voucher_sale(sale.name)

		sale.reload()
		self.assertEqual(sale.status, "Completed")
		self.assertEqual(mock_conn.return_value.create_voucher.call_count, 1)

	def test_fulfillment_failure_sets_retryable_status(self):
		sale = self._make_sale()
		sale.db_set("status", "Payment Confirmed")

		with patch("radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector") as mock_conn:
			mock_conn.return_value.create_voucher.side_effect = RadiusDeskException("boom")
			with self.assertRaises(frappe.ValidationError):
				vs.fulfill_voucher_sale(sale.name)

		sale.reload()
		self.assertEqual(sale.status, "Fulfillment Failed")
		self.assertIn("boom", sale.radius_error)

	def test_on_payment_authorized_fulfills(self):
		sale = self._make_sale(status="Payment Pending")
		with patch("radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector") as mock_conn:
			mock_conn.return_value.create_voucher.return_value = {"id": 1, "name": "WEB-77"}
			sale.run_method("on_payment_authorized", "Completed")

		sale.reload()
		self.assertEqual(sale.status, "Completed")
		self.assertEqual(sale.voucher_code, "WEB-77")

	def test_reset_voucher_sale_reenables_declined_retry(self):
		sale = self._make_sale(status="Payment Pending")
		sale.db_set("merchant_reference", "PES-DEC")
		sale.db_set("poll_url", "https://poll.example.com")
		sale.db_set("payment_gateway", "Pesepay-Test Gateway")

		res = vs.reset_voucher_sale(sale.name)
		self.assertTrue(res["ok"])
		sale.reload()
		self.assertEqual(sale.status, "Draft")
		self.assertEqual(sale.merchant_reference, "")
		self.assertEqual(sale.poll_url, "")
		self.assertEqual(sale.payment_gateway, "")

	def test_reset_voucher_sale_rejects_non_pending(self):
		sale = self._make_sale()
		with self.assertRaises(frappe.ValidationError):
			vs.reset_voucher_sale(sale.name)

	def test_create_voucher_sale_is_idempotent_per_invoice(self):
		"""Re-opening the Buy Voucher dialog after a failed submit must never
		create a second sale for the same invoice."""
		inv = self._make_pos_invoice()
		first = vs.create_voucher_sale(self.plan.name, "POS Invoice", inv.name, phone_number="+263770000001")
		second = vs.create_voucher_sale(self.plan.name, "POS Invoice", inv.name, phone_number="+263770000001")
		self.assertEqual(first["voucher_sale"], second["voucher_sale"])
		count = frappe.db.count("Voucher Sale", {"invoice_doctype": "POS Invoice", "invoice_name": inv.name})
		self.assertEqual(count, 1)

	def test_create_voucher_sale_idempotent_for_completed_sale(self):
		"""A terminal (Completed) sale for the same invoice must still be
		returned, not duplicated — this is the exact no-status-filter behavior
		that closes the double-charge path after a failed invoice submit."""
		inv = self._make_pos_invoice()
		first = vs.create_voucher_sale(self.plan.name, "POS Invoice", inv.name, phone_number="+263770000002")
		frappe.db.set_value(
			"Voucher Sale", first["voucher_sale"], {"status": "Completed", "voucher_code": "DONE-2"}
		)
		second = vs.create_voucher_sale(self.plan.name, "POS Invoice", inv.name, phone_number="+263770000002")
		self.assertEqual(second["voucher_sale"], first["voucher_sale"])
		count = frappe.db.count("Voucher Sale", {"invoice_doctype": "POS Invoice", "invoice_name": inv.name})
		self.assertEqual(count, 1)

	def test_create_voucher_sale_blocks_when_invoice_already_has_voucher(self):
		"""Once a voucher code is stamped on the invoice, a new sale is refused
		even if the earlier sale is in a terminal state (Completed)."""
		inv = self._make_pos_invoice()
		frappe.get_doc(
			{
				"doctype": "Voucher Sale",
				"plan": self.plan.name,
				"amount": 10,
				"currency": "INR",
				"sale_source": "POS",
				"invoice_doctype": "POS Invoice",
				"invoice_name": inv.name,
				"status": "Completed",
				"voucher_code": "DONE-1",
			}
		).insert(ignore_permissions=True)
		inv.db_set("radius_voucher_code", "DONE-1")

		with self.assertRaises(frappe.ValidationError):
			vs.create_voucher_sale(self.plan.name, "POS Invoice", inv.name, phone_number="+263770000001")

	def _make_pos_invoice(self):
		entry = ensure_today_open_pos_entry("_Test Company", "Pesepay-Test Gateway")
		pos_profile = frappe.db.get_value("POS Opening Entry", entry, "pos_profile")
		return frappe.get_doc(
			{
				"doctype": "POS Invoice",
				"pos_profile": pos_profile,
				"customer": "Walk-in Customer",
				"company": "_Test Company",
				"currency": "INR",
				"items": [{"item_code": "WiFi Voucher", "qty": 1, "rate": 10}],
				"payments": [{"mode_of_payment": "Pesepay-Test Gateway", "amount": 10}],
			}
		).insert(ignore_permissions=True)

	def test_create_web_checkout_initiates_payment(self):
		with patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.make_seamless_payment"
		) as mock_ms, patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._validate_pesepay_combo"
		):
			mock_ms.return_value = None
			frappe.response["message"] = {
				"success": True,
				"merchant_reference": "PES-1",
				"poll_url": "https://poll.example.com",
				"reference_number": "REF-1",
			}
			result = vs.create_web_checkout(self.plan.name, "0771112222", "EcoCash")

		self.assertTrue(result["success"])
		self.assertTrue(result["checkout_token"])
		sale = frappe.get_doc("Voucher Sale", {"checkout_token": result["checkout_token"]})
		self.assertEqual(sale.status, "Payment Pending")
		self.assertEqual(sale.merchant_reference, "PES-1")
		self.assertEqual(sale.sale_source, "Web")

	def test_create_web_checkout_works_as_guest(self):
		"""The public checkout runs as Guest; initiation must not require read
		permission on Voucher Sale (the sale is created by the same request)."""
		original_user = frappe.session.user
		frappe.set_user("Guest")
		try:
			with patch(
				"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.make_seamless_payment"
			) as mock_ms, patch(
				"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._validate_pesepay_combo"
			):
				mock_ms.return_value = None
				frappe.response["message"] = {
					"success": True,
					"merchant_reference": "PES-2",
					"poll_url": "https://poll.example.com",
					"reference_number": "REF-2",
				}
				result = vs.create_web_checkout(self.plan.name, "0773334444", "EcoCash")
		finally:
			frappe.set_user(original_user)

		self.assertTrue(result["success"])
		sale = frappe.get_doc("Voucher Sale", {"checkout_token": result["checkout_token"]})
		self.assertEqual(sale.status, "Payment Pending")
		self.assertEqual(sale.sale_source, "Web")

	def test_create_web_checkout_marks_payment_failed_on_initiate_error(self):
		"""If PesaPay initiation fails, the sale must persist as Payment Failed
		(even though the request raises) so admin can see what happened."""
		with patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.make_seamless_payment",
			side_effect=frappe.ValidationError("boom"),
		):
			with self.assertRaises(frappe.ValidationError):
				vs.create_web_checkout(self.plan.name, "0775556666", "EcoCash")

		sale = frappe.get_doc("Voucher Sale", {"phone_number": "0775556666"})
		self.assertEqual(sale.status, "Payment Failed")

	def test_status_endpoint_returns_code_only_when_completed(self):
		sale = self._make_sale()
		res = vs.get_voucher_sale_status("tok0001")
		self.assertEqual(res["status"], "Draft")
		self.assertNotIn("voucher_code", res)

		sale.db_set("status", "Completed")
		sale.db_set("voucher_code", "DONE-99")
		res = vs.get_voucher_sale_status("tok0001")
		self.assertEqual(res["voucher_code"], "DONE-99")

		res = vs.get_voucher_sale_status("wrong-token")
		self.assertEqual(res["status"], "Not Found")

	def test_confirm_voucher_sale_direct_fulfills_draft_sale(self):
		"""Cashier path for a non-PesaPay mode (e.g. cash): a Draft sale linked
		to the POS invoice is confirmed directly (no PesaPay initiation) and
		fulfilled, stamping the voucher code on the invoice."""
		inv = self._make_pos_invoice()
		sale = frappe.get_doc(
			{
				"doctype": "Voucher Sale",
				"plan": self.plan.name,
				"amount": 10,
				"currency": "INR",
				"sale_source": "POS",
				"invoice_doctype": "POS Invoice",
				"invoice_name": inv.name,
				"status": "Draft",
			}
		).insert(ignore_permissions=True)

		with patch("radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector") as mock_conn:
			mock_conn.return_value.create_voucher.return_value = {"id": 50, "name": "POS-9000"}
			result = vs.confirm_voucher_sale(sale.name, payment_method="Cash")

		self.assertTrue(result["ok"])
		self.assertEqual(result["voucher_code"], "POS-9000")
		sale.reload()
		self.assertEqual(sale.status, "Completed")
		self.assertEqual(sale.payment_method, "Cash")
		inv.reload()
		self.assertEqual(inv.radius_voucher_code, "POS-9000")

	def test_confirm_voucher_sale_rejects_non_draft(self):
		sale = self._make_sale(status="Payment Confirmed")
		with self.assertRaises(frappe.ValidationError):
			vs.confirm_voucher_sale(sale.name)

	def test_confirm_voucher_payment_fulfills_pending_sale(self):
		"""POS client path once PesaPay reports SUCCESS: a Payment Pending sale
		with a matching merchant reference is confirmed and fulfilled. Guards a
		regression where the result was double-wrapped through
		_fulfillment_result(fulfill_voucher_sale(...)) and crashed."""
		inv = self._make_pos_invoice()
		sale = frappe.get_doc(
			{
				"doctype": "Voucher Sale",
				"plan": self.plan.name,
				"amount": 10,
				"currency": "INR",
				"sale_source": "POS",
				"invoice_doctype": "POS Invoice",
				"invoice_name": inv.name,
				"status": "Payment Pending",
				"merchant_reference": "PES-TEST-REF-1",
			}
		).insert(ignore_permissions=True)
		ir = frappe.get_doc(
			{
				"doctype": "Integration Request",
				"integration_request_service": "Pesepay",
			}
		)
		ir.flags._name = "PES-TEST-REF-1"
		if frappe.db.exists("Integration Request", "PES-TEST-REF-1"):
			frappe.delete_doc("Integration Request", "PES-TEST-REF-1", force=True)
		ir.insert(ignore_permissions=True)

		with patch("radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector") as mock_conn:
			mock_conn.return_value.create_voucher.return_value = {"id": 51, "name": "POS-9001"}
			result = vs.confirm_voucher_payment("PES-TEST-REF-1")

		self.assertTrue(result["ok"])
		self.assertEqual(result["voucher_code"], "POS-9001")
		sale.reload()
		self.assertEqual(sale.status, "Completed")
		self.assertEqual(sale.voucher_code, "POS-9001")

	def test_initiate_rejects_unsupported_method_currency(self):
		"""PesaPay settles only specific combos (EcoCash: USD/ZiG). An
		unsupported pair must fail fast before any gateway call."""
		sale = self._make_sale()
		self.assertEqual(sale.currency, "INR")

		with (
			patch("radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.make_seamless_payment") as mock_pay,
			patch("radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.get_pesepay_mode_of_payment"),
		):
			with self.assertRaisesRegex(frappe.ValidationError, "does not support"):
				vs.initiate_voucher_payment(sale.name, "0777777777", "EcoCash")

		mock_pay.assert_not_called()

	def test_validate_pesepay_combo_accepts_supported_pairs(self):
		vs._validate_pesepay_combo("EcoCash", "USD")
		vs._validate_pesepay_combo("EcoCash", "ZiG")

	def test_ensure_system_user_grants_warehouse_read(self):
		"""The system user needs Warehouse read as insurance for stock-item
		sales (warehouse lookup paths), so it is granted explicitly."""
		from radius_desk.radius_desk.utils.system_user import SYSTEM_ROLE, ensure_system_user

		ensure_system_user()
		perm = frappe.get_value(
			"Custom DocPerm",
			{"parent": "Warehouse", "role": SYSTEM_ROLE, "permlevel": 0},
			"read",
		)
		self.assertEqual(perm, 1)

	def _make_sales_invoice(self):
		return frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"customer": "Walk-in Customer",
				"company": "_Test Company",
				"currency": "INR",
				"items": [{"item_code": "WiFi Voucher", "qty": 1, "rate": 10}],
				"payments": [{"mode_of_payment": "Pesepay-Test Gateway", "amount": 10}],
			}
		).insert(ignore_permissions=True)

	def test_get_voucher_price_falls_back_to_plan_price(self):
		self._clear_wifi_item_prices()
		price, _ = vs.get_voucher_price("WiFi Voucher", "_Test Company", 10, "INR")
		self.assertEqual(price, 10)

	def test_get_voucher_price_uses_item_price(self):
		if not frappe.db.exists("Price List", "Standard Selling"):
			self.skipTest("Standard Selling price list not present")
		self._clear_wifi_item_prices()
		ip = frappe.get_doc(
			{
				"doctype": "Item Price",
				"item_code": "WiFi Voucher",
				"price_list": "Standard Selling",
				"price_list_rate": 7,
				"currency": "INR",
				"selling": 1,
			}
		).insert(ignore_permissions=True)
		try:
			price, _ = vs.get_voucher_price("WiFi Voucher", "_Test Company", 10, "INR")
			self.assertEqual(price, 7)
		finally:
			# Self-clean: a committed Item Price flips the effective currency in
			# every later reader of get_voucher_price, so never leave it behind.
			frappe.delete_doc("Item Price", ip.name, force=True)

	def test_auto_voucher_on_invoice_submit(self):
		"""Submitting a Sales Invoice containing the voucher item must
		auto-create (and fulfill) a Voucher Sale and stamp the code back."""
		self._clear_wifi_item_prices()
		with patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector"
		) as mock_conn:
			mock_conn.return_value.create_voucher.return_value = {"id": 9, "name": "AUTO-1"}
			inv = self._make_sales_invoice()
			inv.submit()

		sale = frappe.get_doc(
			"Voucher Sale", {"invoice_doctype": "Sales Invoice", "invoice_name": inv.name}
		)
		self.assertEqual(sale.status, "Completed")
		self.assertEqual(sale.amount, 10)
		self.assertEqual(sale.sale_source, "Sales")
		inv.reload()
		self.assertEqual(inv.radius_voucher_code, "AUTO-1")
		self.assertEqual(inv.radius_voucher_plan, self.plan.name)

	def test_auto_voucher_is_idempotent(self):
		"""Re-submitting (or any second pass) must not create a second sale."""
		with patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._get_connector"
		) as mock_conn:
			mock_conn.return_value.create_voucher.return_value = {"id": 91, "name": "AUTO-2"}
			inv = self._make_sales_invoice()
			inv.submit()
		count = frappe.db.count(
			"Voucher Sale", {"invoice_doctype": "Sales Invoice", "invoice_name": inv.name}
		)
		self.assertEqual(count, 1)

	def test_pos_opening_entry_rollover_closes_prior_entry(self):
		mode = "Pesepay-Test Gateway"
		entry_name = ensure_today_open_pos_entry("_Test Company", mode)
		frappe.db.set_value("POS Opening Entry", entry_name, "period_start_date", "2020-01-01 00:00:00")

		next_name = ensure_today_open_pos_entry("_Test Company", mode)

		self.assertNotEqual(entry_name, next_name)
		open_entries = frappe.get_all(
			"POS Opening Entry",
			filters={"pos_profile": "RD Web - _TC", "status": "Open"},
			pluck="name",
		)
		self.assertEqual(open_entries, [next_name])

	def test_create_web_checkout_rejects_pesepay_redirect_url(self):
		"""PesaPay seamless must never return a redirect for embed buyers —
		pre-auth hotspot clients can't reach PesaPay (outside the walled garden)."""
		with patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.make_seamless_payment"
		) as mock_ms, patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._validate_pesepay_combo"
		):
			mock_ms.return_value = None
			frappe.response["message"] = {
				"success": True,
				"merchant_reference": "PES-R",
				"poll_url": "",
				"reference_number": "REF-R",
				"redirect_url": "https://pay.pesepay.com/x",
			}
			with self.assertRaises(frappe.ValidationError):
				vs.create_web_checkout(self.plan.name, "0778889900", "EcoCash")

		sale = frappe.get_doc("Voucher Sale", {"phone_number": "0778889900"})
		self.assertEqual(sale.status, "Payment Failed")

	def test_create_web_checkout_rate_limits_per_phone(self):
		suffix = uuid.uuid4().hex[:6]
		with patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.make_seamless_payment"
		) as mock_ms, patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._validate_pesepay_combo"
		), patch(
			"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale._client_ip",
			return_value="10.99.99.99",
		):
			mock_ms.return_value = None
			frappe.response["message"] = {
				"success": True,
				"merchant_reference": "PES-RL",
				"poll_url": "https://poll.example.com",
				"reference_number": "REF-RL",
			}
			phone = f"0771{suffix}"
			for _ in range(3):
				vs.create_web_checkout(self.plan.name, phone, "EcoCash")
			with self.assertRaises(frappe.exceptions.TooManyRequestsError):
				vs.create_web_checkout(self.plan.name, phone, "EcoCash")

	def test_confirm_poll_backs_off_outbound_pesepay_calls(self):
		"""confirm_voucher_web_checkout must not hammer PesaPay outbound —
		at most one real poll per 3s per checkout_token."""
		sale = self._make_sale()
		sale.db_set("status", "Payment Pending")
		sale.db_set("poll_url", "https://poll.example.com")
		token = sale.checkout_token
		frappe.cache().delete(frappe.cache().make_key(f"rd-pesepay-poll:{token}"))
		with patch(
			"pesepay.pesepay.doctype.pesepay_settings.pesepay_settings.poll_payment_status",
			return_value={},
		) as mock_poll:
			vs.confirm_voucher_web_checkout(token)
			vs.confirm_voucher_web_checkout(token)
		self.assertEqual(mock_poll.call_count, 1)
		# The backoff marker must expire (~3s), else sales would never confirm
		key = frappe.cache().make_key(f"rd-pesepay-poll:{token}")
		self.assertGreater(frappe.cache().ttl(key), 0)
