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
							"Payment is still being processed. If approved on your phone, the voucher will be sent to you shortly.",
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
