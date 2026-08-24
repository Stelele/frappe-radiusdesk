from __future__ import annotations

import frappe

SYSTEM_ROLE = "Radius Desk System"
SYSTEM_USER = "radius-desk-system@example.com"
SYSTEM_FULLNAME = "Radius Desk System"

# Minimal permissions the system user needs to create and submit the app's POS
# Invoice: `Account.read` satisfies ERPNext's unconditional `account_perm_check`
# (erpnext/accounts/party.py), which runs an explicit `frappe.has_permission`
# call that ignores `ignore_permissions`. The POS Invoice create/submit grants
# are belt-and-braces in case the doc-level ignore_permissions flag ever stops
# propagating through submit().
SYSTEM_PERMISSIONS = {
	"Account": {"read": 1},
	"POS Invoice": {"create": 1, "submit": 1, "write": 1, "read": 1},
	"Sales Invoice": {"create": 1, "submit": 1, "write": 1, "read": 1},
	"Voucher Sale": {"create": 1, "write": 1, "read": 1},
	"Voucher Plan": {"read": 1},
	"Radius Desk Settings": {"read": 1},
	"Item": {"read": 1},
	# Warehouse read is insurance for stock-item plans: POS Invoice submit runs
	# warehouse lookup paths (e.g. get_warehouse_bin) even though the app's own
	# voucher item is non-stock, and newer ERPNext runs explicit `has_permission`
	# checks in those paths that would fail the system user without it.
	"Warehouse": {"read": 1},
}


def ensure_system_user():
	"""Create the app-owned system role + user + minimal permissions.

	Idempotent; safe to call from after_migrate and lazily at runtime. The user
	is a service identity used with `frappe.set_user` around privileged work
	(creating/submitting the app's POS Invoice), never granted to humans and
	never a login account for the desk."""
	if not frappe.db.exists("Role", SYSTEM_ROLE):
		frappe.get_doc(
			{"doctype": "Role", "role_name": SYSTEM_ROLE, "desk_access": 0, "is_custom": 1}
		).insert(ignore_permissions=True)

	if not frappe.db.exists("User", SYSTEM_USER):
		frappe.get_doc(
			{
				"doctype": "User",
				"email": SYSTEM_USER,
				"first_name": SYSTEM_FULLNAME,
				"enabled": 1,
				"send_welcome_email": 0,
				"roles": [{"role": SYSTEM_ROLE}],
			}
		).insert(ignore_permissions=True)
	else:
		user = frappe.get_doc("User", SYSTEM_USER)
		if not any(r.role == SYSTEM_ROLE for r in user.roles):
			user.append("roles", {"role": SYSTEM_ROLE})
			user.save(ignore_permissions=True)

	for doctype, perms in SYSTEM_PERMISSIONS.items():
		for ptype, value in perms.items():
			_grant(doctype, ptype, value)

	frappe.db.commit()


def _grant(doctype: str, ptype: str, value: int):
	"""Grant a permission on `doctype` for the system role, creating the Custom
	DocPerm record if needed (idempotent)."""
	from frappe.permissions import add_permission, update_permission_property

	existing = frappe.db.get_value(
		"Custom DocPerm",
		{"parent": doctype, "role": SYSTEM_ROLE, "permlevel": 0},
		"name",
	)
	if not existing:
		add_permission(doctype, SYSTEM_ROLE, ptype=ptype)
		return
	if not frappe.db.get_value("Custom DocPerm", existing, ptype):
		update_permission_property(doctype, SYSTEM_ROLE, 0, ptype, 1)
