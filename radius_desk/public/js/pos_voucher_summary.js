// radius_desk POS receipt voucher-code display.
//
// Loaded only on the POS page (via the `page_js` hook). Monkey-patches
// erpnext.PointOfSale.PastOrderSummary so the completed-order (receipt)
// screen shows the voucher codes that were auto-created for the submitted
// invoice -- letting a cashier read/write them down if the printer fails.
//
// The voucher is created by a background job, so the code may not exist the
// instant the receipt appears. We poll until it is ready.

(function () {
	function inject_styles() {
		let style = document.getElementById("rd-voucher-summary-style");
		if (!style) {
			style = document.createElement("style");
			style.id = "rd-voucher-summary-style";
			document.head.appendChild(style);
		}
		const css = [
			".voucher-codes-block {",
			"  margin-top: 14px; padding: 10px 12px;",
			"  border: 1px dashed #f0a500; border-radius: 6px; background: #fffbea;",
			"}",
			".voucher-codes-block .voucher-codes-label {",
			"  font-weight: 600; font-size: 12px; text-transform: uppercase;",
			"  color: #a86a00; margin-bottom: 6px; letter-spacing: 0.04em;",
			"}",
			".voucher-codes-block .voucher-code-row {",
			"  display: flex; justify-content: space-between; align-items: center;",
			"  gap: 12px; padding: 4px 0;",
			"}",
			".voucher-codes-block .voucher-plan { color: #6c757d; font-size: 13px; }",
			".voucher-codes-block .voucher-code {",
			"  font-family: monospace; font-size: 15px; font-weight: 700;",
			"  color: #1a1a1a; letter-spacing: 0.06em; user-select: all;",
			"}",
			".voucher-codes-block .voucher-generating,",
			".voucher-codes-block .voucher-note { font-size: 13px; color: #8a6d00; }",
			".voucher-codes-block .rd-spinner {",
			"  display: inline-block; width: 14px; height: 14px;",
			"  border: 2px solid #f0a500; border-top-color: transparent;",
			"  border-radius: 50%; animation: rd-spin 0.7s linear infinite;",
			"  vertical-align: middle; margin-right: 6px;",
			"}",
			"@keyframes rd-spin { to { transform: rotate(360deg); } }",
		].join("\n");
		style.textContent = css;
	}

	let patchedClass = null;

	function patch_pos_summary() {
		// Only act on the POS page. On other desk/guest pages bail out with no
		// retry (otherwise the "PastOrderSummary not ready" retry would spin
		// forever since that class only exists on the POS page).
		if (!window.location.pathname.includes("point-of-sale")) return;
		if (!window.erpnext) return;
		const POS = erpnext.PointOfSale;
		if (!POS || !POS.PastOrderSummary) {
			// page script not loaded yet; retry shortly
			setTimeout(patch_pos_summary, 300);
			return;
		}
		const Klass = POS.PastOrderSummary;
		// Skip if this exact class is already patched. A fresh class object
		// (e.g. after the POS page re-initializes on "Complete Order") carries
		// no patch, so we re-apply it -- see watch_and_patch().
		if (patchedClass === Klass) return;

		const _load = Klass.prototype.load_summary_of;
		Klass.prototype.load_summary_of = function (doc, after_submission) {
			_load.call(this, doc, after_submission);
			if (after_submission) {
				this.rd_render_voucher_codes(doc);
			} else {
				// Not a just-completed sale (e.g. viewing a past order): the
				// voucher block only belongs on fresh completions, so drop any
				// leftover block from a previous sale to avoid showing stale codes.
				this.$summary_container.find(".voucher-codes-block").remove();
			}
		};

		Klass.prototype.rd_render_voucher_codes = function (doc) {
			const self = this;
			inject_styles();

			let $block = this.$summary_container.find(".voucher-codes-block");
			if (!$block.length) {
				$block = $('<div class="voucher-codes-block"></div>');
				this.$summary_container.find(".summary-btns").after($block);
			}
			// Always reset to a loading state so a previous sale's codes are
			// wiped the instant a new receipt is shown (no stale-code flash
			// before the new ones load). The spinner conveys "working".
			show_generating();

			const method =
				"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.get_voucher_codes_for_invoice";

			let attempt = 0;
			const GRACE_ATTEMPTS = 10; // wait ~7s before deciding it is not a voucher invoice
			const MAX_ATTEMPTS = 30; // keep polling for fulfillment up to ~21s

			function show_generating() {
				$block.html(
					'<div class="voucher-codes-label">' +
						__("Voucher Codes") +
						'</div><div class="voucher-generating"><span class="rd-spinner"></span>' +
						__("Generating voucher…") +
						"</div>"
				);
			}

			function render_codes(vouchers) {
				const rows = vouchers
					.map(function (v) {
						const plan = frappe.utils.escape_html(v.plan || "");
						const code = frappe.utils.escape_html(v.voucher_code || "");
						return (
							'<div class="voucher-code-row">' +
							'<span class="voucher-plan">' +
							plan +
							"</span>" +
							'<span class="voucher-code">' +
							code +
							"</span></div>"
						);
					})
					.join("");
				$block.html(
					'<div class="voucher-codes-label">' +
						__("Voucher Codes") +
						"</div>" +
						(rows ||
							'<div class="voucher-note">' +
								__("No voucher codes available.") +
								"</div>")
				);
			}

			function poll() {
				frappe.call({
					method: method,
					args: { doctype: doc.doctype, invoice_name: doc.name },
					freeze: false,
					callback: function (r) {
						const all = r.message || [];
						const ready = all.filter(function (v) {
							return v.voucher_code;
						});
						if (ready.length) {
							render_codes(ready);
							return;
						}
						if (all.length) {
							// sale record exists but not fulfilled yet
							show_generating();
							if (attempt++ < MAX_ATTEMPTS) {
								setTimeout(poll, 700);
							} else {
								$block.html(
									'<div class="voucher-codes-label">' +
										__("Voucher Codes") +
										'</div><div class="voucher-note">' +
										__("Voucher still being prepared. Check Voucher Sales.") +
										"</div>"
								);
							}
							return;
						}
						// no records yet -- might just be a non-voucher invoice
						if (attempt++ < GRACE_ATTEMPTS) {
							setTimeout(poll, 700);
						} else {
							$block.remove();
						}
					},
				});
			}

			poll();
		};

		Klass.prototype.__rd_voucher_patched = true;
		patchedClass = Klass;
	}

	// The POS page re-initializes (and re-runs point_of_sale.js, redefining
	// PastOrderSummary) on actions like "Complete Order", but our app_include_js
	// only runs once per full page load. So we keep an observer alive that
	// re-applies the patch whenever the POS summary element is (re)created.
	let lastPatchCheck = 0;
	function watch_and_patch() {
		if (!window.location.pathname.includes("point-of-sale")) return;
		patch_pos_summary();
		if (window.__rd_pos_observer) return;
		const observer = new MutationObserver(function () {
			const now = Date.now();
			if (now - lastPatchCheck < 500) return;
			lastPatchCheck = now;
			if (document.querySelector(".past-order-summary")) {
				patch_pos_summary();
			}
		});
		observer.observe(document.body, { childList: true, subtree: true });
		window.__rd_pos_observer = observer;
	}

	$(function () {
		watch_and_patch();
	});
})();
