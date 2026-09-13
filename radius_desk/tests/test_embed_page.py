import importlib.util
import json
from pathlib import Path
from threading import Thread
from unittest.mock import patch

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


class TestEmbedPageRendering(IntegrationTestCase):
	def _get(self, path):
		client = frappe.utils.get_test_client()
		site = frappe.local.site
		result = {}

		def request_in_thread():
			# The WSGI app resolves the site from the request's Host header by
			# default and destroys the request-local context on close. Run it in
			# a worker thread with get_site_name pinned to this test site, like
			# frappe's own test_api.py does, so the test thread's context survives.
			with patch("frappe.app.get_site_name", return_value=site):
				result["response"] = client.get(path)

		thread = Thread(target=request_in_thread)
		thread.start()
		thread.join()
		response = result["response"]
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		return response.get_data(as_text=True)

	def test_embed_page_renders_without_chrome(self):
		html = self._get("/voucher-checkout/?embed=1")
		self.assertIn("window.RD_EMBED", html)
		self.assertIn("rd-embed", html)
		self.assertNotIn('class="navbar', html)
		self.assertNotIn("web-footer", html.split("<style")[0])  # ignore CSS selectors
		# the colocated checkout JS must load in embed mode (frappe.ready guard)
		self.assertIn("plan-list", html)

	def test_normal_page_still_has_chrome(self):
		html = self._get("/voucher-checkout/")
		# The colocated index.js *reads* window.RD_EMBED, so the bare string
		# appears in the inline script — only embed mode ever assigns it.
		self.assertNotIn("window.RD_EMBED = ", html)
		self.assertNotIn('class="rd-embed"', html)

	def test_injection_in_link_params_cannot_break_out(self):
		html = self._get(
			'/voucher-checkout/?embed=1&linklogin=http://192.168.88.1/login"><script>alert(1)</script>'
		)
		self.assertIn('"linklogin": null', html)
		self.assertNotIn("alert(1)", html)
