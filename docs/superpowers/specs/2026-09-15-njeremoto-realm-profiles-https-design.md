# Njeremoto Realm + Duration Profiles + HTTPS Hotspot — Design

Date: 2026-09-15 (rev 2 — post adversarial review)
Scope: RadiusDesk server + MikroTik router (hotspot-cafe-config repo) + Voucher Plan records in ERPNext
Author: Gift Mugweni

## Summary

Provision a dedicated `Njeremoto` realm and 3h/5h/24h profiles (cloned from
the live `1 Hour Uncapped` profile) on the RadiusDesk server via a
ready-to-run API script; gate everything on an end-to-end realm-auth test
that also proves time caps and speeds; migrate all four plans to the new
realm in ERPNext; then harden the hotspot login with HTTPS behind a full
off-router backup gate and a dedicated pre-flight redesign.

No Frappe app code changes. RadiusDesk management from the app remains a
non-goal — the operator maps IDs into Voucher Plans manually.

## Goals / non-goals

Goals:
- Realm `Njeremoto` exists on RadiusDesk; all voucher sales move to it; the
  `Dev` realm stays alive through a grace window for in-flight vouchers, then
  retires for vouchers (never deleted while retries may reference it).
- Profiles `3 Hour Uncapped`, `5 Hour Uncapped`, `24 Hour Uncapped` clone
  profile 49's components exactly (speeds, burst, Simultaneous-Use=1),
  differing only in name + time cap. A 4th `1 Hour Uncapped (Njeremoto)`
  profile is created rather than reusing profile 49 IF the spike shows 49
  is realm-bound to Dev (decision after spike; default assumption: clone).
- Proven end-to-end: a test voucher under Njeremoto authenticates through
  the live hotspot with correct speeds AND correct time cap before any plan
  points at it.
- HTTPS hotspot login with valid public cert, verified warning-free on
  Android/iOS/laptop, with a rehearsed rollback — gated behind its own
  pre-flight redesign (not executable as currently specified).
- Full off-router backup (binary + export) is a hard gate before any HTTPS step.

Non-goals:
- Realm/profile CRUD from the Frappe app; automatic plan creation (operator
  maps IDs in Desk).
- Changing speeds, burst, or Simultaneous-Use on any tier.
- Card payments, SMS delivery, refunds (unchanged).
- Scheduler/webhook/email verification, break-glass voucher stock,
  walled-garden re-resolution live check (explicitly deferred).

## Decisions (from brainstorm + review)

| # | Decision |
|---|----------|
| 1 | API script (bash + curl, existing `token`-in-body pattern), not GUI clicks. |
| 2 | Clone profile 49's live components; change only name + time cap. |
| 3 | Existing 1 Hour plan moves to Njeremoto too (all four plans, Dev retired after grace). |
| 4 | HTTPS ships last; TLS trouble never blocks realm/plans work. |
| 5 | Full off-router backup is a hard gate before HTTPS (binary + export). |
| 6 | Reference semantics: today's live 1h product defines correct behavior (cap + burst + re-login); new tiers must behave identically, scaled. No product-semantics redesign smuggled in. |

## Architecture

```
Phase 0 — Pre-flight spikes (READ-ONLY, no writes; all must pass first)
  S1. Dump profile 49: profiles/view.json + profile-components +
      radgroupcheck/radgroupreply rows → exact attribute names for the
      3600s cap and the burst string (Mikrotik-Rate-Limit). If the cap is
      NOT a profile/API field (e.g. direct-DB-only), the write mechanism
      (API vs documented droplet-MySQL step) is named here before Phase 1.
  S2. Check profile 49's realm binding → decides: reuse 49 vs clone a 4th
      1h profile under Njeremoto. Default: clone (safer).
  S3. Establish session_auto_close=3600 semantics (session age vs interim
      staleness). If age-based, it MUST be raised/scoped before any 3h+
      plan sells — a 24h buyer silently capped at 1h loses real money.

Phase 1 — Provision (droplet / API, no router touch)
  provision-njeremoto.sh [--inspect-only | --cap-field NAME]
  ├─ authenticate (dashboard/authenticate.json, token in body + cookie,
  │   401 re-login; cloud_id + server URL from env, never hardcoded secrets)
  ├─ create realm "Njeremoto" → print realm ID (case-insensitive
  │   name-check; skip if exists)
  ├─ --inspect-only: print profile 49's raw components and STOP
  │   (operator confirms cap field; nothing is created)
  ├─ creation run: 3/5/24 Hour Uncapped as copies of 49 (new name +
  │   caps 10800s / 18000s / 86400s via the S1-confirmed mechanism)
  │   → print profile IDs
  └─ after every skip/create: re-fetch and diff (name + cap field);
      fail loudly on mismatch; --force recreates on corrected input.
      (Never silently reuse a half-configured profile.)

Phase 2 — Realm-auth gate (blocks everything below on failure)
  ├─ mint 1 test voucher under Njeremoto (new profile)
  ├─ log in via live hotspot from a test device → online
  ├─ assert the CAP: status/SESSION-TIME-LEFT shows the tier duration
  │   (e.g. 3:00:00, not 1:00:00) — a cap-less clone sells unlimited
  │   time and this gate is what catches it
  ├─ assert the BURST: live queue shows max-limit=10M/5M with burst
  │   limits (checked from a real client; RouterOS fetch underreports)
  ├─ radtest user@Njeremoto → Access-Accept carries the expected
  │   Session-Timeout value
  └─ delete test voucher. On ANY failure: stop. No plan changes.

Phase 3 — Plan mapping (ERPNext Desk, operator)
  ├─ prerequisite FIRST: Item + Item Price rows for each duration
  │   (plan.price is read-only, synced from the selling price list;
  │   without them Desk refuses to save the plan)
  ├─ update the 1h plan in place → realm Njeremoto (+ new 1h profile ID
  │   if S2 decided clone)
  ├─ create 3h / 5h / 24h plans (realm Njeremoto + new profile IDs,
  │   item + company + price + currency + sort order)
  ├─ disable legacy Dev-realm plan records — BUT first audit every plan
  │   consumer (portal list, POS list, voucher_automation batch,
  │   fulfillment retry) for enabled+realm filtering so no path sells
  │   mixed-realm stock and in-flight Dev vouchers still fulfill
  └─ verify: one real purchase per duration; assert speeds + session
      behaviour; DB check that post-migration sales reference only
      Njeremoto plans; card price == charged amount == Item Price.

Phase 4 — HTTPS hotspot (router, last; PRE-FLIGHT REDESIGN REQUIRED —
  not executable as a single bullet; needs its own design note first)
  Pre-flight must answer, in writing: public DNS name (operator-controlled
  zone — Frappe Cloud's zone is unusable for DNS-01) + DNS control proof;
  split-horizon mechanics (router answers the hotspot name to itself for
  clients); cert install path on ROS; renewal cron + expiry alerting +
  expired-cert fallback posture; captive-probe (CNA) host matrix in the
  walled garden; pinned portal URL migration
  (http://192.168.88.1/login → https name) in Settings + login.html +
  re-QA of the full fragment auto-login loop; off-hours binary-restore
  drill (WG keys break on restore — checklist: handshake back,
  Accounting-On/Off observed, live voucher login).
  ├─ GATE: /system backup + /export, both downloaded off-router, SHA256
  │   logged, before any TLS change.
  ├─ install cert, set ssl-certificate on hsprof1, verify warning-free on
  │   Android / iOS / laptop (first-run, unauthenticated devices).
  └─ rollback tier 1 = recorded profile-setting reverts (primary);
      tier 2 = binary restore + cert re-import (disaster only).
```

## Error handling

- Script is idempotent by case-insensitive name-check + re-fetch-and-diff;
  `--force` recreates on corrected input (no silent reuse).
- Realm-auth gate failure halts Phases 3–4.
- HTTPS failure never rolls back Phases 1–3 (independent rollout order).

## Testing

- Phase 0 spikes are committed as an appendix with real (redacted) API
  payloads — the script is not written against guessed fields.
- Phase 2 gate as specified above (cap + burst + radtest, not just online).
- Phase 3: one live purchase per duration + DB mixed-realm query
  (`SELECT DISTINCT plan FROM tabVoucher Sale WHERE creation > migration_ts`
  joined to plan realm — all Njeremoto).
- Phase 4: `openssl s_client` validity, three-client warning-free logins,
  rollback drill before sign-off.
- Deferred (explicitly out of scope): scheduler/webhook/email verification,
  break-glass voucher stock, walled-garden re-resolution live check.

## Limitations / open risks

- `never_expire` on Voucher Plan vs profile time caps: per Decision 6, new
  tiers mirror today's live 1h semantics exactly (same flag values, scaled
  caps). If the spike shows the live behavior depends on a contradiction,
  that becomes a follow-up design — not silently resolved here.
- Captive-portal HTTPS behaviour varies by OS version; three first-run
  client classes are the acceptance bar, not an exhaustive matrix.
- Realm suffix behaviour (`@dev` auto-append for permanent users): verified
  no-op for single-field vouchers during the Phase 2 gate.
