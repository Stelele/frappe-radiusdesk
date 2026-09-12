import importlib.util
import json
from pathlib import Path

import frappe
from frappe.tests import IntegrationTestCase


def _load_page_module():
	path = Path(frappe.get_app_path("radius_desk")) / "www" / "voucher-checkout" / "index.py"
	spec = importlib.util.spec_from_file_location("rd_voucher_checkout_index", path)
	mod = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(mod)
	return mod


class TestEmbedContext(IntegrationTestCase):
	def setUp(self):
		self.mod = _load_page_module()
		self.original_form_dict = getattr(frappe.local, "form_dict", None)
		frappe.local.form_dict = frappe._dict({})

	def tearDown(self):
		frappe.local.form_dict = self.original_form_dict or frappe._dict({})

	def _get_context(self):
		context = frappe._dict()
		self.mod.get_context(context)
		return context

	def test_normal_mode_has_no_embed(self):
		context = self._get_context()
		self.assertNotIn("embed", context)
		self.assertTrue(context.plans is not None)

	def test_embed_mode_sets_flag_and_validates_params(self):
		frappe.local.form_dict = frappe._dict(
			{
				"embed": "1",
				"linklogin": "http://192.168.88.1/login",
				"linkorig": "http://192.168.88.1/",
			}
		)
		context = self._get_context()
		self.assertTrue(context.embed)
		self.assertEqual(context.linklogin, "http://192.168.88.1/login")
		self.assertEqual(context.linkorig, "http://192.168.88.1/")
		parsed = json.loads(context.embed_json)
		self.assertEqual(parsed["linklogin"], "http://192.168.88.1/login")

	def test_embed_mode_drops_invalid_params(self):
		frappe.local.form_dict = frappe._dict(
			{
				"embed": "1",
				"linklogin": "https://evil.com/login",
				"linkorig": "http://192.168.88.1/",
			}
		)
		context = self._get_context()
		self.assertTrue(context.embed)
		self.assertIsNone(context.linklogin)
		self.assertEqual(context.linkorig, "http://192.168.88.1/")
		parsed = json.loads(context.embed_json)
		self.assertIsNone(parsed["linklogin"])