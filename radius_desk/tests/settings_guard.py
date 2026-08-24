"""Keep developer-facing configuration safe during radius_desk test runs.

Fixture setup saves over the Radius Desk Settings singleton, and the E2E
suite flips the shared `_Test Company` to USD (PesaPay settles only
USD/ZiG). Some helpers used mid-test (ERPNext chart-of-accounts bootstrap,
pos_infra opening-entry utilities) commit the surrounding transaction, so
per-test rollbacks cannot undo these writes — without protection a local
run leaves the fake gateway / placeholder server_url / USD company behind.

The guard snapshots every mutated value up front and registers an addCleanup
that restores them (with a commit) after the framework's per-test rollback,
so repeated `run-tests` invocations never corrupt the working configuration.
"""

from __future__ import annotations

import frappe

SETTINGS_FIELDS = (
	"server_url",
	"username",
	"cloud_id",
	"default_company",
	"default_walkin_customer",
	"pesepay_gateway",
)

# Shared records mutated by fixtures: (doctype, name, field)
SHARED_RECORD_FIELDS = (
	("Company", "_Test Company", "default_currency"),
	("Account", "Debtors - _TC", "account_currency"),
)

# Singletons mutated anywhere in the fixture chain: (doctype, field)
SINGLETON_FIELDS = (
	("System Settings", "time_zone"),
)


def guard_shared_configuration(testcase) -> None:
	"""Snapshot mutable config and register restoration as a cleanup."""
	single = frappe.get_single("Radius Desk Settings")
	original = {field: getattr(single, field, None) for field in SETTINGS_FIELDS}
	original["password"] = single.get_password("password", raise_exception=False) or ""
	for doctype, name, field in SHARED_RECORD_FIELDS:
		if frappe.db.exists(doctype, name):
			original[(doctype, name, field)] = frappe.db.get_value(doctype, name, field)
	for doctype, field in SINGLETON_FIELDS:
		original[(doctype, field)] = frappe.db.get_single_value(doctype, field)

	def _restore():
		# Only restore the settings singleton when the snapshot actually held
		# values. On a fresh site the singleton starts empty, so saving the
		# empty snapshot would raise MandatoryError; the next test's setUp
		# re-populates it anyway. Restoring shared records is still required
		# to undo per-test flips (e.g. company currency).
		has_settings = any(original.get(f) is not None for f in SETTINGS_FIELDS) or original.get("password")
		if has_settings:
			doc = frappe.get_doc("Radius Desk Settings")
			for field in SETTINGS_FIELDS:
				val = original.get(field)
				if val is not None:
					setattr(doc, field, val)
			if original.get("password"):
				doc.password = original["password"]
			doc.flags.ignore_permissions = True
			doc.save(ignore_permissions=True)
		for key, value in original.items():
			if not isinstance(key, tuple):
				continue
			if len(key) == 3:
				doctype, name, field = key
				frappe.db.set_value(doctype, name, field, value)
			else:
				doctype, field = key
				frappe.db.set_single_value(doctype, field, value)
		frappe.db.commit()

	testcase.addCleanup(_restore)
