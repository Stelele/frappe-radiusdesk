frappe.ready(() => {
	if (!document.querySelector("#plan-list")) return;

	let selected_plan = null;
	let checkout_token = null;
	let poll_interval = null;
	let poll_count = 0;
	const MAX_POLLS = 100; // ~5 min of 3s polls

	const $planList = $("#plan-list");
	const $checkoutPanel = $("#checkout-panel");
	const $statusPanel = $("#status-panel");
	const $resultPanel = $("#result-panel");
	const $selectedPlan = $("#selected-plan");
	const $phone = $("#phone-number");
	const $method = $("#payment-method");
	const $payButton = $("#pay-button");
	const $voucherCode = $("#voucher-code");

	const METHOD_HINTS = {
		EcoCash: "Approve on your phone with your EcoCash PIN — works without mobile data.",
		InnBucks: "The InnBucks app needs mobile data to approve — keep mobile data ON.",
		Omari: "The Omari app needs mobile data to approve — keep mobile data ON.",
	};
	const $methodHint = $("#method-hint");

	function show_method_hint() {
		$methodHint.text(METHOD_HINTS[$method.val()] || "");
	}
	show_method_hint();
	$method.on("change", show_method_hint);

	// Deliver the voucher code to the hotspot login page (embed mode only).
	function deliver_code(code) {
		if (!window.RD_EMBED) return;
		// Framed (iframe embed): deliver via postMessage. targetOrigin is the
		// parent's origin from document.referrer — NEVER '*' (an evil embedding
		// page could harvest the code). If the referrer is unavailable, do
		// nothing: the code stays on screen for manual entry.
		if (window.parent !== window) {
			let target;
			try {
				target = new URL(document.referrer).origin;
			} catch (e) {
				return;
			}
			window.parent.postMessage({ source: "rd-voucher", code: code }, target);
			return;
		}
		// Top-level full-page fallback (linklogin known): redirect with the code
		// in the URL fragment. A top-level navigation is never mixed-content
		// blocked (a cross-scheme form POST would be); the fragment is not sent
		// to the router or logged.
		if (window.RD_EMBED.linklogin) {
			set_status(__("Connecting you to the internet..."));
			setTimeout(function () {
				window.location.href = window.RD_EMBED.linklogin + "#rd-voucher=" + encodeURIComponent(code);
			}, 1500);
		}
	}

	$planList.on("click", ".plan-select", function () {
		const $card = $(this).closest(".card");
		selected_plan = $card.attr("data-plan");
		const price = $card.attr("data-price-html");
		$selectedPlan.text(`${$card.find(".font-weight-bold").first().text()} — ${price}`);
		$checkoutPanel.show();
		$checkoutPanel[0].scrollIntoView({ behavior: "smooth", block: "center" });
	});

	$payButton.on("click", () => {
		if (!selected_plan) {
			frappe.msgprint(__("Select a plan first."));
			return;
		}
		const phone = ($phone.val() || "").trim();
		if (!phone) {
			frappe.msgprint(__("Enter your phone number."));
			return;
		}

		$payButton.prop("disabled", true);
		set_status(__("Initiating payment..."));

		frappe
			.call({
				method: "radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.create_web_checkout",
				args: { plan: selected_plan, phone_number: phone, payment_method: $method.val() },
				silent: true,
				error_msg: document.getElementById("checkout-error-sink"),
			})
			.then((r) => {
				const msg = r.message || {};
				if (!msg.success) {
					set_status(__("Payment could not be initiated. Please try again."), true);
					$payButton.prop("disabled", false);
					return;
				}
				checkout_token = msg.checkout_token;
				set_status(__("Waiting for you to approve the payment on your phone..."));
				poll_interval = setInterval(poll_status, 3000);
			})
			.catch(() => {
				set_status(__("Payment could not be initiated. Please try again."), true);
				$payButton.prop("disabled", false);
			});
	});

	function poll_status() {
		poll_count += 1;
		frappe
			.call({
				method:
					"radius_desk.radius_desk.doctype.voucher_sale.voucher_sale.confirm_voucher_web_checkout",
				args: { checkout_token: checkout_token },
			})
			.then((r) => {
				const st = r.message || {};
				if (poll_count > MAX_POLLS) {
					clearInterval(poll_interval);
					set_status(
						__(
							"Payment is still being processed. If approved on your phone, please ask the cafe staff to check your purchase.",
						),
						true,
					);
					return;
				}
				if (st.status === "Completed" && st.voucher_code) {
					clearInterval(poll_interval);
					$checkoutPanel.hide();
					$statusPanel.hide();
					$voucherCode.text(st.voucher_code);
					$resultPanel.show();
					deliver_code(st.voucher_code);
				} else if (st.status === "Payment Failed") {
					clearInterval(poll_interval);
					set_status(__("Payment was declined. Please try again."), true);
					$payButton.prop("disabled", false);
				} else if (st.status === "Fulfillment Failed") {
					clearInterval(poll_interval);
					set_status(
						__(
							"Payment received, but the voucher could not be created. Please contact support.",
						),
						true,
					);
				}
			})
			.catch(() => {
				// Transient poll failure — keep polling so a temporary server
				// hiccup doesn't strand the customer on a dead status panel.
				// The server-side webhook + scheduler finalize the sale anyway.
			});
	}

	function set_status(text, is_error) {
		$statusPanel.show();
		$statusPanel.html(
			`<div class="alert ${is_error ? "alert-danger" : "alert-info"} mb-2">${frappe.utils.escape_html(
				text,
			)}</div>`,
		);
	}
});
