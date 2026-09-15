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
	const $payButton = $("#pay-button");
	const $voucherCode = $("#voucher-code");

	const METHOD_HINTS = {
		EcoCash: "Approve on your phone with your EcoCash PIN — works without mobile data.",
		InnBucks: "The InnBucks app needs mobile data to approve — keep mobile data ON.",
		Omari: "The Omari app needs mobile data to approve — keep mobile data ON.",
	};
	const $methodHint = $("#method-hint");
	const $methodFallback = $("#payment-method-fallback");
	let selected_method = "EcoCash";

	function show_method_hint(m) {
		$methodHint.text(METHOD_HINTS[m] || "");
	}
	show_method_hint(selected_method);
	$methodFallback.val(selected_method);
	$(".rd-method-card").on("click", function () {
		selected_method = $(this).data("method");
		$(".rd-method-card").attr("aria-checked", "false").removeClass("selected");
		$(this).attr("aria-checked", "true").addClass("selected");
		$methodFallback.val(selected_method);
		show_method_hint(selected_method);
	});

	// Deliver the voucher code to the hotspot login page (embed mode only).
	function deliver_code(code) {
		if (!window.RD_EMBED) return;
		// Framed (iframe embed): deliver via postMessage, but ONLY to the
		// router's origin — derived from the server-validated linklogin URL,
		// never from document.referrer (any site can embed this page; only
		// the hotspot router page may receive the code). If the parent is not
		// the router, the browser simply never delivers the message. The code
		// always stays on screen for manual entry as the fallback.
		if (window.parent !== window) {
			if (!window.RD_EMBED.linklogin) return;
			let target;
			try {
				target = new URL(window.RD_EMBED.linklogin).origin;
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
				args: { plan: selected_plan, phone_number: phone, payment_method: selected_method },
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
