from __future__ import annotations

import ipaddress
import re
import time
from urllib.parse import urlsplit

import frappe
from frappe import _

# After IP validation, restrict the whole URL to a conservative charset so the
# validated value can be embedded in JSON/HTML without injection risk. RouterOS
# login URLs only ever contain host, path, an urlencoded dst parameter and
# [ ] around IPv6 literals.
_SAFE_URL_RE = re.compile(r"^[A-Za-z0-9:/.?=&%_\[\]-]+$")


def validate_hotspot_url(url: str | None) -> str | None:
	"""Return the URL unchanged only if it points at a router: http(s) scheme
	and a private / loopback / link-local IP-literal host, with no userinfo and
	no unexpected characters. Anything else returns None so callers drop the
	parameter (prevents open redirects + injection from ?linklogin / ?linkorig).
	"""
	if not url:
		return None
	try:
		parts = urlsplit(url.strip())
	except ValueError:
		return None
	if parts.scheme not in ("http", "https"):
		return None
	if parts.username or parts.password:
		return None
	host = parts.hostname
	if not host:
		return None
	try:
		ip = ipaddress.ip_address(host)
	except ValueError:
		# No hostnames at all — not even single-label (http://evil/ resolves)
		return None
	if not (ip.is_private or ip.is_loopback or ip.is_link_local):
		return None
	if not _SAFE_URL_RE.fullmatch(url.strip()):
		return None
	return url.strip()


def ensure_rate_limit(identifier: str, limit: int, window_seconds: int) -> None:
	"""Raise TooManyRequestsError once `identifier` is used more than `limit`
	times in the current fixed window. Cache-backed and best-effort."""
	cache = frappe.cache()
	window_number = int(time.time() // window_seconds)
	key = f"rd-rate-limit:{identifier}:{window_number}"
	count = cache.incr(key)
	if count == 1:
		cache.expire(key, window_seconds)
	if count > limit:
		raise frappe.exceptions.TooManyRequestsError(
			_("Too many requests. Please wait a few minutes and try again.")
		)
