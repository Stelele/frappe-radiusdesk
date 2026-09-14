from __future__ import annotations

from unittest.mock import Mock, patch

import frappe
from frappe.tests import IntegrationTestCase
from requests import HTTPError

from radius_desk.radius_desk.utils.radiusdesk import RadiusDeskConnector, RadiusDeskException


class TestRadiusDeskConnector(IntegrationTestCase):
	def setUp(self):
		self.connector = RadiusDeskConnector(
			server_url="https://radius.example.com",
			username="admin",
			password="secret",
			cloud_id="1",
		)

	def tearDown(self):
		frappe.cache.delete_value(self.connector._token_cache_key)

	@patch("radius_desk.radius_desk.utils.radiusdesk.get_request_session")
	def test_login_then_create_voucher(self, mock_get_session):
		session = Mock()
		login_resp = Mock()
		login_resp.raise_for_status.return_value = None
		login_resp.json.return_value = {"success": True, "data": {"token": "tok123"}}
		create_resp = Mock()
		create_resp.raise_for_status.return_value = None
		create_resp.json.return_value = {"success": True, "data": [{"id": 42, "name": "HOME-COFFEE-1234"}]}
		session.post.side_effect = [login_resp, create_resp]
		mock_get_session.return_value = session

		result = self.connector.create_voucher(realm_id=1, profile_id=2, extra_value="RD-1")

		self.assertEqual(result, {"id": 42, "name": "HOME-COFFEE-1234"})
		self.assertEqual(session.post.call_count, 2)
		login_url = session.post.call_args_list[0].args[0]
		create_url = session.post.call_args_list[1].args[0]
		self.assertTrue(login_url.endswith("/cake4/rd_cake/dashboard/authenticate.json"))
		self.assertTrue(create_url.endswith("/cake4/rd_cake/vouchers/add.json"))

	@patch("radius_desk.radius_desk.utils.radiusdesk.get_request_session")
	def test_token_is_cached_between_calls(self, mock_get_session):
		session = Mock()
		login_resp = Mock()
		login_resp.raise_for_status.return_value = None
		login_resp.json.return_value = {"success": True, "data": {"token": "tok"}}
		create_resp = Mock()
		create_resp.raise_for_status.return_value = None
		create_resp.json.return_value = {"success": True, "data": [{"id": 1, "name": "A"}]}
		session.post.side_effect = [login_resp, create_resp, create_resp]
		mock_get_session.return_value = session

		self.connector.create_voucher(realm_id=1, profile_id=2)
		self.connector.create_voucher(realm_id=1, profile_id=2)

		self.assertEqual(session.post.call_count, 3)  # 1 login + 2 creates

	@patch("radius_desk.radius_desk.utils.radiusdesk.get_request_session")
	def test_refresh_token_on_401(self, mock_get_session):
		session = Mock()
		unauth = Mock()
		unauth.status_code = 401
		unauth.raise_for_status.side_effect = HTTPError("unauthorized")
		create_ok = Mock()
		create_ok.status_code = 200
		create_ok.raise_for_status.return_value = None
		create_ok.json.return_value = {"success": True, "data": [{"id": 7, "name": "REFRESHED"}]}
		session.post.side_effect = [unauth, create_ok]
		mock_get_session.return_value = session

		with patch.object(self.connector, "_login", return_value="tok-new"):
			result = self.connector.create_voucher(realm_id=1, profile_id=2)

		self.assertEqual(result["name"], "REFRESHED")
		self.assertEqual(session.post.call_count, 2)

	@patch("radius_desk.radius_desk.utils.radiusdesk.get_request_session")
	def test_login_failure_raises(self, mock_get_session):
		session = Mock()
		bad = Mock()
		bad.raise_for_status.return_value = None
		bad.json.return_value = {"success": False, "message": "Invalid credentials"}
		session.post.side_effect = [bad]
		mock_get_session.return_value = session

		with self.assertRaises(RadiusDeskException):
			self.connector.create_voucher(realm_id=1, profile_id=2)

	def test_base_url_normalization(self):
		self.assertTrue(self.connector._base_url.endswith("/cake4/rd_cake"))
		connector = RadiusDeskConnector("https://rd.example.com/cake4/rd_cake", "u", "p", "1")
		self.assertTrue(connector._base_url.endswith("/cake4/rd_cake"))
		self.assertFalse(connector._base_url.endswith("/cake4/rd_cake/cake4/rd_cake"))

	@patch("radius_desk.radius_desk.utils.radiusdesk.get_request_session")
	def test_find_voucher_by_extra_value_parses_wrapped_index_response(self, mock_get_session):
		session = Mock()
		login_resp = Mock()
		login_resp.raise_for_status.return_value = None
		login_resp.json.return_value = {"success": True, "data": {"token": "tok"}}
		lookup_resp = Mock()
		lookup_resp.raise_for_status.return_value = None
		# cake4 index responses wrap rows: {"data": {"data": [...]}}
		lookup_resp.json.return_value = {
			"success": True,
			"data": {"data": [{"id": 55, "name": "ADOPTED-1"}]},
		}
		session.post.return_value = login_resp
		session.get.return_value = lookup_resp
		mock_get_session.return_value = session

		result = self.connector.find_voucher_by_extra_value("RD-VS-20260912-00001")

		self.assertEqual(result, {"id": 55, "name": "ADOPTED-1"})
		lookup_url = session.get.call_args_list[0].args[0]
		self.assertTrue(lookup_url.endswith("/cake4/rd_cake/vouchers/index.json"))
		params = session.get.call_args_list[0].kwargs["params"]
		self.assertEqual(params["extra_value"], "RD-VS-20260912-00001")

	@patch("radius_desk.radius_desk.utils.radiusdesk.get_request_session")
	def test_find_voucher_by_extra_value_returns_none_when_no_match(self, mock_get_session):
		session = Mock()
		login_resp = Mock()
		login_resp.raise_for_status.return_value = None
		login_resp.json.return_value = {"success": True, "data": {"token": "tok"}}
		lookup_resp = Mock()
		lookup_resp.raise_for_status.return_value = None
		lookup_resp.json.return_value = {"success": True, "data": {"data": []}}
		session.post.return_value = login_resp
		session.get.return_value = lookup_resp
		mock_get_session.return_value = session

		self.assertIsNone(self.connector.find_voucher_by_extra_value("RD-NOPE"))
