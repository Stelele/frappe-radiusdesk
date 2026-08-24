"""End-to-end tests for the POS voucher flow (the server side of
radius_desk_pos.js). Each test walks the exact sequence the POS client
performs, with hermetic seams so the suite is repeatable and offline:

	draft POS invoice
	-> create_voucher_sale        Draft sale, idempotent per invoice
	-> initiate_voucher_payment   PesaPay boundary faked; IR + refs persisted
	-> [gateway approves: client poll returns SUCCESS]
	-> confirm_voucher_payment    fulfills through the RadiusDesk connector seam
	-> client submit step         payment row + voucher stamp + submit => Paid

The PesaPay HTTP boundary is faked at `make_seamless_payment` with its real
contract: it writes `frappe.response["message"]` and creates an Integration
Request named after the merchant reference. Everything radius_desk owns
around that boundary (sale state machine, IR bookkeeping, fulfillment,
invoice stamping) runs for real.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import patch

import frappe
from erpnext.accounts.test.accounts_mixin import AccountsTestMixin
from erpnext.stock.doctype.item.test_item import create_item
from frappe.tests import IntegrationTestCase

from radius_desk.radius_desk.doctype.voucher_sale import voucher_sale as vs
from radius_desk.tests.settings_guard import guard_shared_configuration
from radius_desk.radius_desk.utils.pos_infra import ensure_today_open_pos_entry

PESEPAY_MODE = "Pesepay-Test Gateway"


class TestPOSVoucherFlowE2E(AccountsTestMixin, IntegrationTestCase):
	"""Repeatable end-to-end coverage of the POS voucher cashier flow."""

	def setUp(self):
		guard_shared_configuration(self)
		self.create_company("_Test Company", "_TC")
		# PesaPay only settles USD/ZiG, so an EcoCash-capable deployment runs
		# in USD (Voucher Plan enforces plan currency == company currency).
		# The receivable account must follow so party-currency checks accept
		# USD invoices.
		frappe.db.set_value("Company", "_Test Company", "default_currency", "USD")
		frappe.db.set_value("Account", self.debit_to, "account_currency", "USD")
		create_item("WiFi Voucher", is_stock_item=0, company="_Test Company")
		self.create_customer("Walk-in Customer")
		# Drop any WiFi Voucher item price left over from a prior (committed)
		# test run so get_voucher_price cannot flip this plan's effective
		# currency to INR.
		for name in frappe.get_all(
			"Item Price", {"item_code": "WiFi Voucher", "price_list": "Standard Selling"}, pluck="name"
		):
			frappe.delete_doc("Item Price", name, force=True)
		self._setup_pesepay_gateway()
		self._setup_settings()
		self.plan = self._make_plan()

	# ------------------------------------------------------------------
	# Fixtures (mirroring test_voucher_sale.py conventions)
	# ------------------------------------------------------------------

	def _setup_pesepay_gateway(self):
		if not frappe.db.exists("Pesepay Settings", "Test Gateway"):
			frappe.get_doc(
				{
					"doctype": "Pesepay Settings",
					"gateway_name": "Test Gateway",
					"integration_key": "test-key",
					"encryption_key": "0123456789abcdef0123456789abcdef",
					"use_sandbox": 1,
				}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Payment Gateway", PESEPAY_MODE):
			frappe.get_doc(
				{
					"doctype": "Payment Gateway",
					"gateway": PESEPAY_MODE,
					"gateway_settings": "Pesepay Settings",
					"gateway_controller": "Test Gateway",
				}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Mode of Payment", PESEPAY_MODE):
			frappe.get_doc(
				{
					"doctype": "Mode of Payment",
					"mode_of_payment": PESEPAY_MODE,
					"enabled": 1,
					"type": "Bank",
				}
			).insert(ignore_permissions=True)
		from erpnext.accounts.doctype.mode_of_payment.test_mode_of_payment import (
			set_default_account_for_mode_of_payment,
		)

		set_default_account_for_mode_of_payment(
			frappe.get_doc("Mode of Payment", PESEPAY_MODE), "_Test Company", self.cash
		)

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
				"pesepay_gateway": PESEPAY_MODE,
			}
		)
		settings.save(ignore_permissions=True)

	def _make_plan(self):
		if frappe.db.exists("Voucher Plan", "E2E 1 Hour"):
			plan = frappe.get_doc("Voucher Plan", "E2E 1 Hour")
			# Repair a plan mutated by a prior (committed) test run so the E2E
			# flow always runs in USD for EcoCash.
			if plan.currency != "USD" or plan.price != 10:
				plan.currency = "USD"
				plan.price = 10
				plan.save(ignore_permissions=True)
			return plan
		return frappe.get_doc(
			{
				"doctype": "Voucher Plan",
				"plan_name": "E2E 1 Hour",
				"item": "WiFi Voucher",
				"company": "_Test Company",
				"radius_realm_id": "1",
				"radius_profile_id": "2",
				"price": 10,
				"currency": "USD",
				"enabled": 1,
			}
		).insert(ignore_permissions=True)

	def _make_invoice(self, currency="USD"):
		"""Draft POS invoice exactly as the POS page produces one before the
		cashier collects payment: single plan item, zero-valued payment rows.
		Pinned to the app-managed profile so validation cannot pick an
		unrelated profile with a stale opening entry."""
		entry = ensure_today_open_pos_entry("_Test Company", PESEPAY_MODE)
		pos_profile = frappe.db.get_value("POS Opening Entry", entry, "pos_profile")
		doc = frappe.get_doc(
			{
				"doctype": "POS Invoice",
				"pos_profile": pos_profile,
				"customer": "Walk-in Customer",
				"company": "_Test Company",
				"currency": currency,
				"conversion_rate": 1,
				"price_list_currency": currency,
				"plc_conversion_rate": 1,
				"items": [{"item_code": "WiFi Voucher", "qty": 1, "rate": 10}],
				"payments": [{"mode_of_payment": PESEPAY_MODE, "amount": 0}],
			}
		)
		return doc.insert(ignore_permissions=True)

	@contextmanager
	def _pesepay_gateway_stub(self, refs):
		"""Fake make_seamless_payment with its real contract: persists an
		Integration Request named after the merchant reference and writes the
		result into frappe.response["message"]. Records every initiation."""
		calls = []

		def fake_make_seamless_payment(**kwargs):
			ref = refs[len(calls)]
			calls.append(ref)
			# Tolerate leftovers from a previously interrupted run.
			if frappe.db.exists("Integration Request", ref):
				frappe.delete_doc("Integration Request", ref, force=True)
			ir = frappe.get_doc(
				{
					"doctype": "Integration Request",
					"integration_request_service": "Pesepay",
					"status": "Queued",
					"data": frappe.as_json(kwargs),
					"reference_doctype": kwargs.get("reference_doctype"),
					"reference_docname": kwargs.get("reference_docname"),
				}
			)
			ir.flags._name = ref
			ir.insert(ignore_permissions=True)
			if not hasattr(frappe.local, "response"):
				frappe.local.response = frappe._dict()
			frappe.response["message"] = {
				"success": True,
				"is_paid": False,
				"merchant_reference": ref,
				"poll_url": f"https://gateway.test/poll/{ref}",
				"reference_number": f"RCCH-{ref}",
				"status": "PENDING",
			}

		with patch.object(vs, "make_seamless_payment", side_effect=fake_make_seamless_payment):
			yield calls

	@contextmanager
	def _radiusdesk_stub(self, voucher_id=77, voucher_code="E2E-VCH"):
		with patch.object(vs, "_get_connector") as mock_conn:
			mock_conn.return_value.create_voucher.return_value = {
				"id": voucher_id,
				"name": voucher_code,
			}
			yield mock_conn

	def _pay_and_submit_invoice(self, inv, amount):
		"""The client's final step (confirm_and_submit_invoice): fill the
		PesaPay payment row with the collected amount and submit."""
		row = inv.payments[0]
		row.amount = amount
		row.base_amount = amount
		inv.paid_amount = amount
		inv.base_paid_amount = amount
		inv.submit()
		inv.reload()

	# ------------------------------------------------------------------
	# Tests
	# ------------------------------------------------------------------

	def test_happy_path_end_to_end(self):
		"""Full cashier happy path: draft -> sale -> initiation -> approval ->
		confirmation -> fulfilled -> submitted Paid invoice -> idempotent
		re-confirm."""
		inv = self._make_invoice("USD")

		# 1. Draft sale, idempotent per invoice (no double charge possible).
		first = vs.create_voucher_sale(self.plan.name, "POS Invoice", inv.name, phone_number="0777777777")
		second = vs.create_voucher_sale(self.plan.name, "POS Invoice", inv.name, phone_number="0777777777")
		self.assertEqual(first["voucher_sale"], second["voucher_sale"])
		sale_name = first["voucher_sale"]

		# 2. Initiate on the gateway (EcoCash/USD is a supported combo).
		with self._pesepay_gateway_stub(["PES-E2E-R1"]) as calls:
			result = vs.initiate_voucher_payment(sale_name, "0777777777", "EcoCash")
		self.assertEqual(calls, ["PES-E2E-R1"])
		self.assertTrue(result["success"])
		self.assertEqual(result["merchant_reference"], "PES-E2E-R1")

		sale = frappe.get_doc("Voucher Sale", sale_name)
		self.assertEqual(sale.status, "Payment Pending")
		self.assertEqual(sale.merchant_reference, "PES-E2E-R1")
		self.assertEqual(sale.phone_number, "0777777777")
		self.assertEqual(sale.payment_method, "EcoCash")
		self.assertEqual(sale.payment_gateway, PESEPAY_MODE)
		self.assertEqual(sale.poll_url, "https://gateway.test/poll/PES-E2E-R1")
		self.assertTrue(frappe.db.exists("Integration Request", "PES-E2E-R1"))

		# 3. Client poll reports SUCCESS; confirmation fulfills via RadiusDesk.
		with self._radiusdesk_stub(voucher_id=77, voucher_code="E2E-VCH-1"):
			confirmed = vs.confirm_voucher_payment("PES-E2E-R1")
		self.assertTrue(confirmed["ok"])
		self.assertEqual(confirmed["voucher_code"], "E2E-VCH-1")

		sale.reload()
		self.assertEqual(sale.status, "Completed")
		self.assertEqual(sale.voucher_code, "E2E-VCH-1")
		self.assertEqual(sale.voucher_id, "77")
		self.assertEqual(
			frappe.db.get_value("Integration Request", "PES-E2E-R1", "status"), "Completed"
		)

		# 4. Voucher stamped on the draft invoice before submission.
		inv.reload()
		self.assertEqual(inv.radius_voucher_code, "E2E-VCH-1")

		# 5. Client submit step: pay via the PesaPay mode and submit.
		self._pay_and_submit_invoice(inv, self.plan.price)
		self.assertEqual(inv.docstatus, 1)
		self.assertEqual(inv.status, "Paid")
		self.assertEqual(inv.radius_voucher_code, "E2E-VCH-1")

		# 6. Re-confirm after completion is idempotent.
		reconfirmed = vs.confirm_voucher_payment("PES-E2E-R1")
		self.assertTrue(reconfirmed["ok"])
		self.assertEqual(reconfirmed["voucher_code"], "E2E-VCH-1")
		self.assertEqual(reconfirmed["invoice"], inv.name)

	def test_declined_payment_resets_and_retries_end_to_end(self):
		"""Declined gateway payment -> reset to Draft -> retry with a fresh
		initiation -> approved -> completed. Exactly two initiations happen."""
		inv = self._make_invoice("USD")
		sale_name = vs.create_voucher_sale(
			self.plan.name, "POS Invoice", inv.name, phone_number="0777777777"
		)["voucher_sale"]

		with self._pesepay_gateway_stub(["PES-E2E-D1"]):
			vs.initiate_voucher_payment(sale_name, "0777777777", "EcoCash")
		self.assertEqual(
			frappe.db.get_value("Voucher Sale", sale_name, "status"), "Payment Pending"
		)

		# Gateway declines; the client resets the sale for another attempt.
		reset = vs.reset_voucher_sale(sale_name)
		self.assertTrue(reset["ok"])
		sale = frappe.get_doc("Voucher Sale", sale_name)
		self.assertEqual(sale.status, "Draft")
		self.assertEqual(sale.merchant_reference, "")

		# Retry initiates a second, distinct gateway reference.
		with self._pesepay_gateway_stub(["PES-E2E-D2"]) as calls:
			retry = vs.initiate_voucher_payment(sale_name, "0777777777", "EcoCash")
		self.assertEqual(calls, ["PES-E2E-D2"])
		self.assertEqual(retry["merchant_reference"], "PES-E2E-D2")

		with self._radiusdesk_stub(voucher_id=78, voucher_code="E2E-VCH-2"):
			confirmed = vs.confirm_voucher_payment("PES-E2E-D2")
		self.assertTrue(confirmed["ok"])
		self.assertEqual(confirmed["voucher_code"], "E2E-VCH-2")

		sale.reload()
		self.assertEqual(sale.status, "Completed")
		self.assertEqual(sale.voucher_code, "E2E-VCH-2")

		# Still exactly one sale for this invoice.
		self.assertEqual(
			frappe.db.count("Voucher Sale", {"invoice_doctype": "POS Invoice", "invoice_name": inv.name}),
			1,
		)

	def test_guest_web_checkout_end_to_end(self):
		"""Public /voucher-checkout flow: guest checkout initiates payment,
		the backend scheduler finalizes it (poll SUCCESS -> on_payment_authorized),
		and the public status endpoint reveals the voucher code only after
		completion."""
		with self._pesepay_gateway_stub(["PES-E2E-W1"]) as calls:
			checkout = vs.create_web_checkout(
				self.plan.name, phone_number="0777777777", payment_method="EcoCash"
			)
		self.assertEqual(calls, ["PES-E2E-W1"])
		self.assertTrue(checkout["success"])
		token = checkout["checkout_token"]
		ref = checkout["merchant_reference"]
		self.assertEqual(ref, "PES-E2E-W1")
		self.assertTrue(token)

		sale_name = frappe.db.get_value("Voucher Sale", {"checkout_token": token}, "name")
		sale = frappe.get_doc("Voucher Sale", sale_name)
		self.assertEqual(sale.status, "Payment Pending")
		self.assertEqual(sale.sale_source, "Web")
		self.assertEqual(sale.phone_number, "0777777777")

		# Pre-completion polling must not leak the voucher code.
		status = vs.get_voucher_sale_status(token)
		self.assertEqual(status["status"], "Payment Pending")
		self.assertNotIn("voucher_code", status)

		# The production scheduler/webhook marks the IR Completed and fires the
		# Voucher Sale's authorized hook; fulfillment runs through RadiusDesk.
		frappe.db.set_value("Integration Request", ref, "status", "Completed")
		with self._radiusdesk_stub(voucher_id=88, voucher_code="E2E-WEB-1"):
			sale.on_payment_authorized("Completed")

		status = vs.get_voucher_sale_status(token)
		self.assertEqual(status["status"], "Completed")
		self.assertEqual(status["voucher_code"], "E2E-WEB-1")

		sale.reload()
		self.assertEqual(sale.status, "Completed")
		inv = frappe.get_doc("POS Invoice", sale.invoice_name)
		self.assertEqual(inv.docstatus, 1)
		self.assertEqual(inv.status, "Paid")
		self.assertEqual(inv.radius_voucher_code, "E2E-WEB-1")

	def test_unsupported_currency_fails_fast_before_gateway(self):
		"""An unsupported method/currency pair (EcoCash/INR) aborts during
		initiation: no gateway call, no Integration Request, sale stays Draft
		and retryable."""
		inv = self._make_invoice("INR")
		sale_name = vs.create_voucher_sale(
			self.plan.name, "POS Invoice", inv.name, phone_number="0777777777"
		)["voucher_sale"]
		pesepay_irs_before = frappe.db.count(
			"Integration Request", {"integration_request_service": "Pesepay"}
		)

		with (
			self._pesepay_gateway_stub(["PES-E2E-X1"]) as calls,
			self.assertRaisesRegex(frappe.ValidationError, "does not support"),
		):
			vs.initiate_voucher_payment(sale_name, "0777777777", "EcoCash")

		self.assertEqual(calls, [])
		self.assertEqual(
			frappe.db.count("Integration Request", {"integration_request_service": "Pesepay"}),
			pesepay_irs_before,
		)
		self.assertEqual(frappe.db.get_value("Voucher Sale", sale_name, "status"), "Draft")
