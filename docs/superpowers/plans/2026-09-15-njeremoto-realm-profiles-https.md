# Njeremoto Realm + Profiles + HTTPS — Execution Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision the Njeremoto realm and three duration profiles on the live RadiusDesk server, prove realm auth end-to-end, migrate plans, and pre-flight (not execute) the HTTPS hotspot design.

**Architecture:** Read-only spikes first (no writes until attribute names are proven); an idempotent bash+cURL provision script (token-in-body auth, env-supplied secrets, re-fetch-and-diff after every write); a live-device auth gate; Desk data-entry checklist with DB verification queries; HTTPS split out as its own design note with an approval gate.

**Tech Stack:** RadiusDesk v4 cake4 JSON API (`dashboard/authenticate.json`, `profiles/view.json`, `profiles/simple-add.json`, `vouchers/add.json`), droplet MySQL (`radgroupcheck`/`radgroupreply`), `radtest`, MikroTik RouterOS SSH, ERPNext Desk.

**Design doc:** `docs/superpowers/specs/2026-09-15-njeremoto-realm-profiles-https-design.md`

**Environment notes (read first):**
- RadiusDesk host, root API password, RADIUS secret, and MySQL credentials come from the operator at run time as env vars (`RD_HOST`, `RD_PASS`, `RADIUS_SECRET`, `DB_PASS`). Never hardcode, commit, or log secrets.
- `cloud_id=23` (per SESSION-LOG). Profile 49 = `1 Hour Uncapped`, 10M/5M speeds, Simultaneous-Use=1, 3600s cap.
- Repo: `/home/gift/Documents/code-projects/hotspot-cafe-config`. The script lives at `droplet/provision-njeremoto.sh` (new file, no secrets in repo).
- Conventions: `set -euo pipefail` in bash; `jq` for JSON; every write prints the created ID; every write is followed by a re-fetch-and-diff that fails loudly on mismatch.

---

# PART 1 — Pre-flight spikes (READ-ONLY, no writes)

### Task 1: Dump profile 49's real components

**Files:** none (terminal only; paste outputs into the spec appendix)

- [ ] **Step 1: Authenticate and capture a token**

```bash
export RD_HOST="<droplet-host-or-ip>" RD_PASS="<radiusdesk-root-password>"
TOKEN=$(curl -s -X POST "https://$RD_HOST/cake4/rd_cake/dashboard/authenticate.json" \
  --data-urlencode token= \
  --data-urlencode username=root \
  --data-urlencode password="$RD_PASS" | jq -r .data.token)
test -n "$TOKEN" && test "$TOKEN" != null && echo "AUTH OK"
```
Expected: prints `AUTH OK`. If not, stop — fix host/credentials before anything else.

- [ ] **Step 2: Dump profile 49**

```bash
curl -s -X POST "https://$RD_HOST/cake4/rd_cake/profiles/view.json" \
  --data-urlencode token="$TOKEN" --data-urlencode id=49 | jq . | tee /tmp/opencode/profile49.json
```
Expected: JSON showing profile 49's fields. Record: exact attribute carrying the 3600s cap (candidate: `Session-Timeout`, `Rd-Total-Time`, or a `radgroupcheck` row), the burst attribute value, `realm_id` (null/shared vs 19), and `cloud_id`.

- [ ] **Step 3: Dump the group tables for profile 49's group**

On the droplet (`ssh root@<droplet>`), using the naming from README §4 (`SimpleAdd_49` pattern — confirm exact groupname from the Step 2 output first):

```bash
mysql -u root -p"$DB_PASS" rd -e "SELECT attribute,op,\`value\` FROM radgroupcheck WHERE groupname='<GROUP-FROM-STEP-2>';"
mysql -u root -p"$DB_PASS" rd -e "SELECT attribute,op,\`value\` FROM radgroupreply WHERE groupname='<GROUP-FROM-STEP-2>';"
```
Expected: rows showing the cap (`Rd-Total-Time := 3600` or equivalent) and burst (`Mikrotik-Rate-Limit := 10M/5M ...`). Paste both outputs into the spec appendix.

- [ ] **Step 4: Establish session_auto_close semantics**

Read the `rd auto_close` task definition / RadiusDesk docs and record: does `session_auto_close=3600` kill sessions by age or only stale interim sessions? If age-based, raise/scope it for the router's client entry BEFORE any 3h+ plan sells — this is a hard gate on Phase 3.

- [ ] **Step 5: Decide reuse-vs-clone and write mechanism**

From Steps 2–3, record the decision: reuse profile 49 (only if `realm_id` is null/shared) or clone a 4th `1 Hour Uncapped (Njeremoto)` profile. Record the exact write path for caps/burst (API controllers if they exist, else the documented direct-DB procedure). If the cap cannot be expressed for clones, STOP — revise the design, do not proceed.

---

# PART 2 — Provision script + realm-auth gate

### Task 2: Write `droplet/provision-njeremoto.sh`

**Files:**
- Create: `droplet/provision-njeremoto.sh` (executable, `chmod +x`)
- Test: dry-run against the API (`--inspect-only` prints, creates nothing)

- [ ] **Step 1: Write the script skeleton (auth + idempotent realm)**

```bash
#!/usr/bin/env bash
# Provisions the Njeremoto realm + duration profiles. Secrets via env only:
# RD_HOST, RD_PASS (RadiusDesk root), DB_PASS (droplet MySQL, if DB writes needed).
set -euo pipefail
: "${RD_HOST:?set RD_HOST}" ; : "${RD_PASS:?set RD_PASS}"
CLOUD_ID="${CLOUD_ID:-23}"

api() { # api <controller/action.json> [k=v ...]
	local endpoint="$1"; shift
	curl -s -X POST "https://$RD_HOST/cake4/rd_cake/$endpoint" \
		--data-urlencode token="$TOKEN" "$@"
}

TOKEN=$(curl -s -X POST "https://$RD_HOST/cake4/rd_cake/dashboard/authenticate.json" \
	--data-urlencode token= --data-urlencode username=root \
	--data-urlencode password="$RD_PASS" | jq -r .data.token)
test -n "$TOKEN" && test "$TOKEN" != null || { echo "AUTH FAILED"; exit 1; }

realm_id_by_name() {
	# cake4 index responses wrap rows as {"data": {"data": [...]}} or {"data": [...]}
	api "realms/index.json" | jq -r --arg n "$1" \
		'if (.data | type) == "array" then .data elif (.data.data | type) == "array" then .data.data else [] end | .[] | select(.name == $n) | .id // empty' | head -1
}

if [ -z "$(realm_id_by_name Njeremoto)" ]; then
	echo "creating realm Njeremoto..."
	api "realms/add.json" --data-urlencode name=Njeremoto --data-urlencode cloud_id="$CLOUD_ID" | jq .
else
	echo "realm Njeremoto already exists — skipping create"
fi
REALM_ID=$(realm_id_by_name Njeremoto)
test -n "$REALM_ID" || { echo "realm ID lookup failed"; exit 1; }
echo "REALM_ID=$REALM_ID"
```

- [ ] **Step 2: Add profile creation from the Task 1 spike results**

Extend the script with one block per profile (3h/5h/24h, plus a 4th 1h-Njeremoto block if Task 1 decided clone). Each block MUST: name-check skip-if-exists, create via the spike-confirmed mechanism (API controllers or documented DB inserts — copy profile 49's exact attributes, new name + cap 10800/18000/86400), then re-fetch and diff name + cap field, failing loudly on mismatch. Add a `--force` flag that deletes and recreates on corrected input. Commit the script only after the spike appendix exists — no guessed field names.

- [ ] **Step 3: Dry-run and commit**

Run: `RD_HOST=... RD_PASS=... ./droplet/provision-njeremoto.sh --inspect-only` (prints what would be created, writes nothing).
Expected: exit 0, no creates. Then:

```bash
git add droplet/provision-njeremoto.sh
git commit -m "feat: Njeremoto realm + duration profile provisioning script"
```

### Task 3: Realm-auth gate (blocks everything below on ANY failure)

**Files:** none (live device + terminal)

- [ ] **Step 1: Mint one test voucher under the Njeremoto realm**

```bash
curl -s -X POST "https://$RD_HOST/cake4/rd_cake/vouchers/add.json" \
  --data-urlencode token="$TOKEN" --data-urlencode realm_id="$REALM_ID" \
  --data-urlencode profile_id="<new-3h-profile-id>" --data-urlencode cloud_id="$CLOUD_ID" \
  --data-urlencode single_field=true --data-urlencode never_expire=true \
  --data-urlencode quantity=1 | jq -r '.data[0].name'
```
Expected: prints a voucher code. Record it (redact before committing anywhere).

- [ ] **Step 2: Log in via the live hotspot from a test device**

Enter the code on the voucher tab. Expected: online.

- [ ] **Step 3: Assert the cap, not just connectivity**

On the device status page (or `SESSION-TIME-LEFT`): countdown shows ~3:00:00, not 1:00:00. On the router, the live queue shows `max-limit=10M/5M` with burst limits (check from a real client; RouterOS fetch underreports per SESSION-LOG Part 6).

- [ ] **Step 4: radtest cross-check**

```bash
radtest <voucher-code> <voucher-code> <radius-host> 0 "$RADIUS_SECRET"
```
Expected: `Access-Accept` carrying the expected `Session-Timeout` value (10800 for the 3h test).

- [ ] **Step 5: Delete the test voucher** (RadiusDesk GUI or API delete). On any failure in Steps 2–4: STOP. No plan changes until auth is proven.

---

# PART 3 — Plan mapping (ERPNext Desk, operator)

### Task 4: Items, prices, plans, retire Dev

**Files:** none (Desk data entry + SQL verification)

- [ ] **Step 1: Create Item + Item Price rows for each duration** (plan.price is read-only, synced from the selling price list — without these, Desk refuses to save the plan). Record the exact rates charged.
- [ ] **Step 2: Update the 1h plan in place** → realm ID = Njeremoto (keep profile 49, or the cloned 1h profile ID per Task 1 decision).
- [ ] **Step 3: Create 3h / 5h / 24h plans** (realm + profile IDs from Task 2 output, item + company + price + currency + sort order).
- [ ] **Step 4: Audit every plan consumer** (portal list, POS list, voucher_automation batch, fulfillment retry) for `enabled` + realm filtering; then disable legacy Dev-realm plan records. Keep the Dev realm + profile 49 alive through a grace window for in-flight vouchers and retries.
- [ ] **Step 5: Verify with one live purchase per duration** — speeds + session behaviour correct; card price == charged amount == Item Price. Then DB check (MariaDB console or Desk report):

```sql
SELECT name, plan_name, radius_realm_id, radius_profile_id, enabled
FROM `tabVoucher Plan` WHERE enabled = 1;
-- every row must show the Njeremoto realm ID
SELECT plan, COUNT(*) FROM `tabVoucher Sale`
WHERE creation > '<migration-timestamp>' GROUP BY plan;
-- every plan must be a Njeremoto plan
```

---

# PART 4 — HTTPS hotspot (PRE-FLIGHT DESIGN FIRST, not execution)

### Task 5: Write the HTTPS pre-flight design note and get approval

**Files:**
- Create: `hotspot-cafe-config` design note (markdown) answering, in writing: public DNS name (operator-controlled zone — Frappe Cloud's zone is unusable for DNS-01) + DNS control proof; split-horizon mechanics (router answers the hotspot name to itself for clients); cert install path on ROS; renewal cron + expiry alerting + expired-cert fallback posture; captive-probe (CNA) host matrix in the walled garden; pinned portal URL migration (`http://192.168.88.1/login` → https name) in Settings + `login.html` + full fragment auto-login re-QA; off-router backup gate (`/system backup` + `/export`, both downloaded off-router, SHA256 logged) before any TLS change; off-hours binary-restore drill (WG keys break on restore — checklist: handshake back, Accounting-On/Off observed, live voucher login).

- [ ] **Step 1: Write the note, submit for review, do not touch the router.** Execution of Phase 4 begins only after the note is approved. Rollback tier 1 (recorded profile-setting reverts) is primary; tier 2 (binary restore + cert re-import) is disaster-only.
