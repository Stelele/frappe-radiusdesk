from __future__ import annotations

import frappe
from frappe.utils import get_request_session


class RadiusDeskException(Exception):
	"""Raised for RadiusDesk API errors (login, HTTP, rejected creation)."""


class RadiusDeskConnector:
	"""HTTP client for the RadiusDesk v4 (cake4) API.

	Login: POST {base}/dashboard/authenticate.json -> {"success": true, "data": {"token": ...}}
	Voucher creation: POST {base}/vouchers/add.json -> {"success": true, "data": [{id, name}]}
	The token is passed both as the ``token`` form field and the ``Token`` cookie.
	"""

	TOKEN_CACHE_TTL = 3600

	def __init__(self, server_url: str, username: str, password: str, cloud_id: str, timeout: int = 30):
		self._base_url = self._normalize_base_url(server_url)
		self._username = username
		self._password = password
		self._cloud_id = cloud_id
		self._timeout = timeout

	@staticmethod
	def _normalize_base_url(url: str) -> str:
		url = (url or "").rstrip("/")
		if "/cake4/rd_cake" not in url:
			url = f"{url}/cake4/rd_cake"
		return url

	@property
	def _token_cache_key(self) -> str:
		return f"radius_desk:token:{self._base_url}:{self._username}"

	def _get_token(self) -> str:
		token = frappe.cache.get_value(self._token_cache_key)
		if token:
			return token
		token = self._login()
		frappe.cache.set_value(self._token_cache_key, token, expires_in_sec=self.TOKEN_CACHE_TTL)
		return token

	def _login(self) -> str:
		url = f"{self._base_url}/dashboard/authenticate.json"
		try:
			session = get_request_session()
			resp = session.post(
				url,
				data={"auto_compact": "false", "username": self._username, "password": self._password},
				timeout=self._timeout,
			)
			resp.raise_for_status()
			data = resp.json()
		except Exception as exc:
			raise RadiusDeskException(f"RadiusDesk login failed: {exc}") from exc
		if not data.get("success"):
			raise RadiusDeskException(f"RadiusDesk login rejected: {data.get('message', '')}")
		return data["data"]["token"]

	def create_voucher(self, realm_id, profile_id, never_expire: bool = True, extra_value: str = "") -> dict:
		url = f"{self._base_url}/vouchers/add.json"
		payload = {
			"single_field": "true",
			"realm_id": realm_id,
			"profile_id": profile_id,
			"quantity": 1,
			"never_expire": "on" if never_expire else "off",
			"extra_value": extra_value or "",
			"token": self._get_token(),
			"sel_language": "4_4",
			"cloud_id": self._cloud_id,
		}
		try:
			session = get_request_session()
			resp = session.post(url, data=payload, cookies={"Token": payload["token"]}, timeout=self._timeout)
			if resp.status_code in (401, 403):
				frappe.cache.delete_value(self._token_cache_key)
				payload["token"] = self._get_token()
				resp = session.post(
					url, data=payload, cookies={"Token": payload["token"]}, timeout=self._timeout
				)
			resp.raise_for_status()
			data = resp.json()
		except Exception as exc:
			raise RadiusDeskException(f"RadiusDesk voucher creation failed: {exc}") from exc
		if not data.get("success"):
			raise RadiusDeskException(f"RadiusDesk voucher creation rejected: {data.get('message', '')}")
		vouchers = data.get("data") or []
		if not vouchers:
			raise RadiusDeskException("RadiusDesk returned no voucher data")
		return {"id": vouchers[0].get("id"), "name": vouchers[0].get("name")}
