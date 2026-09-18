# Radius Desk User Guide

This guide walks end-users and cashiers through the voucher purchase and fulfillment process.

---

## Buying a Voucher at the POS

### Step 1: Select a Plan
1. Open the POS screen.
2. Add a **Voucher Plan** item (e.g. "WiFi Voucher") to the cart.
3. The plan name, description, and price are shown per the plan configuration.

### Step 2: Submit the Invoice
1. Complete the POS invoice as normal.
2. On submit, the app auto-creates a **Voucher Sale** in `Draft` status (background job).
3. The cashier does not need to take any action at this stage.

### Step 3: Collect Payment
Depending on the payment mode configured:

#### PesaPay Payment (EcoCash / InnBucks / Omari)
1. On the POS completed-order screen, tap **Pay**.
2. Select the payment method (EcoCash, InnBucks, or Omari).
3. Enter the customer's phone number.
4. Confirm the payment amount.
5. The customer receives a payment prompt on their phone.
6. The customer approves the payment via their app.
7. The cashier waits for the status to update (polls every 3s).

#### Cash / Other Payment
1. On the POS completed-order screen, mark the payment as received.
2. The cashier confirms the payment method as "Cash".
3. The app immediately creates the RadiusDesk voucher and stamps the invoice.

### Step 4: Read the Voucher Code
1. The voucher code appears on the completed-order receipt.
2. The cashier can read it down or the customer enters it at the router's login page.
3. If the printer is unavailable, the **Voucher Codes** block on the receipt screen polls for the code until it is ready (up to ~21s).

---

## Buying a Voucher on the Web (Guest / Self-Service)

### Step 1: Open the Checkout Page
Navigate to `/voucher-checkout/` (or the embedded version on a portal page).

### Step 2: Select a Plan
1. Browse available **Voucher Plans**.
2. Click **Select** on the desired plan.
3. The price and plan name are shown in the checkout modal.

### Step 3: Enter Customer Details
1. Enter a **phone number** (e.g. `0771234567`).
2. Select a **payment method** (EcoCash, InnBucks, or Omari).
3. Click **Pay Now**.

### Step 4: Initialize Payment
1. The page calls `create_web_checkout`, which:
   - Creates a `Voucher Sale` in `Draft` status.
   - Initiates a PesaPay seamless payment.
   - Returns a **checkout token**.
2. The page starts polling the status every 3 seconds.

### Step 5: Poll Status
The page repeatedly calls `get_voucher_sale_status` with the checkout token. Possible statuses:

| Status | Meaning |
|---|---|
| `Draft` | Sale created, payment not yet initiated |
| `Payment Pending` | Payment initiated, awaiting customer approval |
| `Payment Confirmed` | Payment confirmed by gateway |
| `Voucher Created` | RadiusDesk voucher created; invoice stamping still pending (retryable via `retry_voucher_sale`; `fulfill_voucher_sale` promotes it to `Completed`) |
| `Completed` | Voucher created, invoice stamped |
| `Payment Failed` | Gateway declined |
| `Fulfillment Failed` | Payment received but voucher could not be created |

### Step 6: Receive the Voucher Code
Once the status becomes `Completed`:
1. The voucher code is displayed in the **Result** panel.
2. The code is also delivered to the hotspot login page via URL fragment (`#rd-voucher=<code>`).
3. The customer can log in to the WiFi network using this code.

---

## Admin: Retry Failed Fulfillment

1. Go to the **Radius Desk** menu → **Fulfillment Retry** (if available) or check the scheduler logs.
2. The every-5-minute cron scheduler (`0/5 * * * *`) re-runs for `Voucher Sale` records with status `Fulfillment Failed` that are older than 15 minutes.
3. On success, the voucher code is adopted (if already created on RadiusDesk) or a new voucher is created, and the sale is promoted to `Completed`.
4. If after 15 minutes the sale still fails, an email is sent to System Managers (throttled to once per 30 minutes).

---

## Hotspot Portal Return Flow

When **Hotspot Portal Return Prefix** is configured in Radius Desk Settings:

1. After payment, the guest checkout redirects to the portal login page whose URL exactly matches the pinned prefix.
2. The voucher code is appended as a URL fragment (`#rd-voucher=<code>`).
3. The captive portal reads the fragment and provisions the customer's access.
4. If the return URL does not match the pinned prefix, the fragment is dropped and the code is not delivered — fail-closed security.

When the prefix is **not** configured:
- The app falls back to `https://radius.giftmugweni.com/login/`.
- The voucher code is delivered via the same fragment mechanism.