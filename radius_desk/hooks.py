app_name = "radius_desk"
app_title = "Radius Desk"
app_publisher = "Gift Mugweni"
app_description = "A simple app to integrate with radius desk and accept payments"
app_email = "giftmugweni@gmail.com"
app_license = "mit"

# Apps
# ------------------

required_apps = ["frappe", "erpnext", "payments", "pesepay"]

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "radius_desk",
# 		"logo": "/assets/radius_desk/logo.png",
# 		"title": "Radius Desk",
# 		"route": "/radius_desk",
# 		"has_permission": "radius_desk.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/radius_desk/css/radius_desk.css"
# app_include_js = "/assets/radius_desk/js/radius_desk.js"

# include js, css files in header of web template
# web_include_css = "/assets/radius_desk/css/radius_desk.css"
# web_include_js = "/assets/radius_desk/js/radius_desk.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "radius_desk/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# Show auto-created voucher codes on the POS completed-order (receipt) screen.
# Loaded globally and self-guards to only patch the POS page (see the JS).
app_include_js = ["/assets/radius_desk/js/pos_voucher_summary.js"]

# The POS "Buy Voucher" button was removed; vouchers are now created
# automatically on invoice submit (see doc_events below).
doctype_js = {}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "radius_desk/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# automatically load and sync documents of this doctype from downstream apps
# importable_doctypes = [doctype_1]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "radius_desk.utils.jinja_methods",
# 	"filters": "radius_desk.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "radius_desk.install.before_install"
# after_install = "radius_desk.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "radius_desk.uninstall.before_uninstall"
# after_uninstall = "radius_desk.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "radius_desk.utils.before_app_install"
# after_app_install = "radius_desk.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "radius_desk.utils.before_app_uninstall"
# after_app_uninstall = "radius_desk.utils.after_app_uninstall"

# Build
# ------------------
# To hook into the build process

# after_build = "radius_desk.build.after_build"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "radius_desk.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# Document Events
# ---------------
# Hook on document methods and events

# Auto-create a RadiusDesk voucher when an invoice containing a Voucher Plan
# item is submitted (POS or Sales). Replaces the old POS "Buy Voucher" button.
doc_events = {
	"POS Invoice": {
		"on_submit": "radius_desk.radius_desk.utils.voucher_automation.create_vouchers_for_invoice"
	},
	"Sales Invoice": {
		"on_submit": "radius_desk.radius_desk.utils.voucher_automation.create_vouchers_for_invoice"
	},
}

# Scheduled Tasks
# ---------------

scheduler_events = {
	"daily": ["radius_desk.radius_desk.utils.pos_infra.ensure_daily_pos_opening_entry"],
	"cron": {
		# every 5 minutes: retry paid-but-unfulfilled voucher sales
		"0/5 * * * *": ["radius_desk.radius_desk.utils.fulfillment_retry.retry_fulfillment_failed_sales"],
	},
}

after_migrate = ["radius_desk.installer.after_migrate"]

# Testing
# -------

# before_tests = "radius_desk.install.before_tests"

# Extend DocType Class
# ------------------------------
#
# Specify custom mixins to extend the standard doctype controller.
# extend_doctype_class = {
# 	"Task": "radius_desk.custom.task.CustomTaskMixin"
# }

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "radius_desk.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "radius_desk.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["radius_desk.utils.before_request"]
# after_request = ["radius_desk.utils.after_request"]

# Job Events
# ----------
# before_job = ["radius_desk.utils.before_job"]
# after_job = ["radius_desk.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"radius_desk.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []
