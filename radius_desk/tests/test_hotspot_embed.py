import uuid

import frappe
from frappe.tests import IntegrationTestCase

from radius_desk.radius_desk.utils.hotspot_embed import ensure_rate_limit, validate_hotspot_url


class TestValidateHotspotUrl(IntegrationTestCase):
	def test_accepts_private_ipv4_login_urls(self):
		self.assertEqual(
			validate_hotspot_url("http://192.168.88.1/login"),
			"http://192.168.88.1/login",
		)
		self.assertEqual(
			validate_hotspot_url("http://10.5.50.1/login?dst=http%3A%2F%2Fexample.com%2F"),
			"http://10.5.50.1/login?dst=http%3A%2F%2Fexample.com%2F",
		)

	def test_accepts_https_loopback_and_ipv6_ula(self):
		self.assertEqual(
			validate_hotspot_url("https://127.0.0.1/login"),
			"https://127.0.0.1/login",
		)
		self.assertEqual(
			validate_hotspot_url("http://[fd00::1]/login"),
			"http://[fd00::1]/login",
		)
		self.assertEqual(
			validate_hotspot_url("http://[fe80::1]/login"),
			"http://[fe80::1]/login",
		)

	def test_rejects_public_hosts(self):
		self.assertIsNone(validate_hotspot_url("https://evil.com/login"))
		self.assertIsNone(validate_hotspot_url("http://8.8.8.8/login"))

	def test_rejects_single_label_and_dns_names(self):
		self.assertIsNone(validate_hotspot_url("http://router/login"))
		self.assertIsNone(validate_hotspot_url("http://evil/login"))

	def test_rejects_non_http_schemes(self):
		self.assertIsNone(validate_hotspot_url("javascript:alert(1)"))
		self.assertIsNone(validate_hotspot_url("ftp://192.168.88.1/login"))

	def test_rejects_exotic_ip_encodings(self):
		# decimal / hex IP encodings that browsers would resolve
		self.assertIsNone(validate_hotspot_url("http://3232238081/login"))
		self.assertIsNone(validate_hotspot_url("http://0x7f.0.0.1/login"))

	def test_rejects_userinfo_and_injection(self):
		self.assertIsNone(validate_hotspot_url("http://user:pass@192.168.88.1/login"))
		self.assertIsNone(validate_hotspot_url('http://192.168.88.1/login"><script>alert(1)</script>'))

	def test_rejects_garbage(self):
		self.assertIsNone(validate_hotspot_url(""))
		self.assertIsNone(validate_hotspot_url(None))
		self.assertIsNone(validate_hotspot_url("not a url"))

	def test_portal_return_prefix_accepts_droplet_login_with_query(self):
		"""Captive-portal return leg: the pinned https prefix passes with its
		query string intact (the login page needs its params)."""
		prefix = "https://radius.giftmugweni.com/login/"
		url = (
			"https://radius.giftmugweni.com/login/njeremoto/index.html"
			"?nasid=njeremoto-cafe-01&type=mikrotik"
		)
		self.assertEqual(validate_hotspot_url(url, return_prefix=prefix), url)

	def test_portal_return_prefix_rejects_wrong_host_scheme_path(self):
		prefix = "https://radius.giftmugweni.com/login/"
		self.assertIsNone(
			validate_hotspot_url("https://evil.com/login/njeremoto/", return_prefix=prefix)
		)
		self.assertIsNone(
			validate_hotspot_url(
				"http://radius.giftmugweni.com/login/njeremoto/", return_prefix=prefix
			)
		)
		self.assertIsNone(
			validate_hotspot_url(
				"https://radius.giftmugweni.com/other/page", return_prefix=prefix
			)
		)
		self.assertIsNone(
			validate_hotspot_url(
				"https://radius.giftmugweni.com/login/\"><script>alert(1)</script>",
				return_prefix=prefix,
			)
		)

	def test_portal_return_absent_keeps_old_behavior(self):
		# droplet hostname URLs still rejected without the prefix (fail closed)
		self.assertIsNone(
			validate_hotspot_url("https://radius.giftmugweni.com/login/njeremoto/")
		)
		# router URLs unaffected by the prefix
		self.assertEqual(
			validate_hotspot_url(
				"http://192.168.88.1/login",
				return_prefix="https://radius.giftmugweni.com/login/",
			),
			"http://192.168.88.1/login",
		)

	def test_portal_return_rejects_non_numeric_port_without_raising(self):
		prefix = "https://radius.giftmugweni.com/login/"
		self.assertIsNone(
			validate_hotspot_url(
				"https://radius.giftmugweni.com:abc/login/njeremoto/", return_prefix=prefix
			)
		)

	def test_misconfigured_prefix_fails_closed(self):
		self.assertIsNone(
			validate_hotspot_url(
				"https://radius.giftmugweni.com/login/x", return_prefix="https://evil.com/"
			)
		)
		self.assertIsNone(
			validate_hotspot_url("https://radius.giftmugweni.com/login/x", return_prefix="")
		)

	def test_exact_match_required_when_expected_url_configured(self):
		"""Production pins Hotspot Login URL in settings — only that exact URL
		may receive the voucher fragment (blocks crafted links to other LAN hosts)."""
		self.assertEqual(
			validate_hotspot_url("http://192.168.88.1/login", expected_url="http://192.168.88.1/login"),
			"http://192.168.88.1/login",
		)
		# a different private IP does not pass once an expected URL is set
		self.assertIsNone(
			validate_hotspot_url("http://192.168.88.1/login", expected_url="http://10.5.50.1/login")
		)
		# path differences matter too
		self.assertIsNone(
			validate_hotspot_url("http://192.168.88.1/login", expected_url="http://192.168.88.1/status")
		)
		# a misconfigured (public) expected URL rejects everything — fail closed
		self.assertIsNone(
			validate_hotspot_url("http://192.168.88.1/login", expected_url="https://evil.com/login")
		)


class TestEnsureRateLimit(IntegrationTestCase):
	def test_allows_up_to_limit_then_raises(self):
		key = f"test:{uuid.uuid4().hex}"
		for _ in range(3):
			ensure_rate_limit(key, limit=3, window_seconds=60)
		with self.assertRaises(frappe.exceptions.TooManyRequestsError):
			ensure_rate_limit(key, limit=3, window_seconds=60)
