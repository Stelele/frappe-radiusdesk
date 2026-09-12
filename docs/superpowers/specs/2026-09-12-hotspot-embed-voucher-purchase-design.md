# Hotspot-Embedded Voucher Purchase — Design

Date: 2026-09-12 (rev 2 — post adversarial review)
App: `radius_desk` (Frappe v16) + `hotspot-cafe-config` (MikroTik)
Author: Gift Mugweni

## Summary

Embed the existing guest self-service voucher portal
(`/voucher-checkout/`) into the MikroTik hotspot login page as a
"Buy Voucher" tab, so an unconnected customer can buy a voucher with
mobile money and be **automatically logged in** — without ever typing a
voucher code. A full-page fallback covers captive-portal mini-browsers.
Both delivery paths converge on one mechanism: login.html receives the
code (postMessage or URL fragment), stashes it, and PAP-submits its own
form — with a sessionStorage rescue banner if anything goes wrong.

## Goals / non-goals

Goals:
- Embed the guest portal into `login.html` (iframe tab) on the hotspot.
- After payment confirmation, auto-submit the voucher so the customer
  gets online with zero manual steps.
- Full-page fallback flow for mini-browsers that mishandle iframes.
- Voucher code recoverable by the customer after any failure short of
  closing the browser (sessionStorage rescue + always-on-screen display).
- Make the Frappe site reachable pre-auth via walled-garden entries.
- Abuse controls on guest APIs (rate limits) since the portal is now
  front-and-centre for every walk-in.
- Single source of truth for the buy flow: all purchase UI lives in
  Frappe; the hotspot page only embeds and auto-logins.

Non-goals:
- Rebuilding the buy UI inside the hotspot page (Approach C, rejected).
- Voucher recovery/lookup by phone number (staff retrieve from the
  Voucher Sale desk list; the rescue banner covers the customer side).
- Changes to payment methods, fulfilment, or the Voucher Sale lifecycle
  (exception: small additions listed below — fail-fast, retry scheduler).
- SMS delivery of codes; mini-browser quirks "solved" — only mitigated.

## Assumptions

| # | Assumption | Status |
|---|---|---|
| 1 | Frappe site live at `https://njeremoto.jh.erpnext.com/`, app installed, Settings + Plans configured | rollout prerequisite |
| 2 | PesaPay initiation is server-side (seamless); approval happens on the customer's phone | verified in code; **fail-fast added** if PesaPay ever returns a `redirect_url` |
| 3 | Router `login-by=http-chap,http-pap,cookie` | verified (`03-hotspot-radius.rsc:47`) — PAP auto-login permitted |
| 4 | Frappe core sets no frame-ancestors for www pages; Frappe Cloud edge neither | core verified (`web_form.py:22` is the only hit); edge = verify live at rollout |
| 5 | Hotspot DNS resolves the portal domain pre-auth | verified (`dns-server` = router); DoH divergence handled in walled-garden script |
| 6 | EcoCash approval rides USSD (works without mobile data); InnBucks/Omari are app-based and need internet | UX hints + QA cases added |
| 7 | Frappe Cloud site does not auto-hibernate | verify plan at rollout + keep-alive cron on the droplet |

## Architecture

```
┌─ MIKROTIK hAP lite (hotspot, pre-auth) ────────────────────────────┐
│ login.html                                                          │
│ ├─ tab-voucher (default, unchanged)                                 │
│ ├─ tab-userpass (unchanged)                                         │
│ └─ tab-buy (NEW)                                                    │
│     ├─ iframe → PORTAL/voucher-checkout?embed=1                     │
│     │   (+ "portal unreachable" note + fixed-height scroll area)    │
│     ├─ "Open shop in full page →" link carrying                     │
│     │   ?embed=1&linklogin=$(link-login-only-esc)                   │
│     │             &linkorig=$(link-orig-esc)                        │
│     └─ code receiver (one mechanism, two transports):               │
│         iframe  → postMessage (targetOrigin = referrer origin,     │
│                    NEVER '*'; unknown → code display only)          │
│         fallback → top-level location redirect to                   │
│                    {linklogin}#rd-voucher=CODE (http→http nav,      │
│                    fragment never sent to router / logged)          │
│         on receive: sessionStorage stash → fill username=           │
│           password=CODE → document.login.submit() (plain PAP,       │
│           bypasses CHAP onSubmit) → rescue banner on failure        │
└───────────────┬─────────────────────────────┬───────────────────────┘
                │ HTTPS (walled-garden-ip)    │ RADIUS auth
                ▼                             ▼
┌─ FRAPPE njeremoto.jh.erpnext.com ─┐  ┌─ DROPLET RADIUSDesk ─┐
│ /voucher-checkout/                 │  │ FreeRADIUS + cake4   │
│ ├─ normal mode (unchanged)         │  │ (unchanged)          │
│ ├─ embed=1 → bare template +       │  └──────────▲───────────┘
│ │   frappe web bundle (no chrome)  │             │ REST (existing)
│ ├─ embed=1&linklogin=… → also      │─────────────┘
│ │   deliver code via fragment      │
│ └─ guest APIs: + rate limits,      │
│     + redirect_url fail-fast       │
│ scheduler: retry Fulfillment       │
│   Failed sales + notify operator   │
└────────────────────────────────────┘
```

## User flows

### Happy path (iframe)

```
Connect → login.html → Buy Voucher tab
  → iframe: pick plan → phone number → method → Pay Now
  → approval hint shown per method (EcoCash: no data needed;
    InnBucks/Omari: keep mobile data ON)
  → "Approve on your phone" (portal polls, existing)
  → payment confirmed → voucher created (existing path)
  → code displayed on screen AND postMessage to parent
  → parent: validate origin + payload + code charset
  → stash code (sessionStorage) → fill form → PAP submit
  → alogin.html → original URL. Customer never types a code.
```

### Fallback path (flaky mini-browser)

```
Buy tab → iframe unusable → "Open shop in full page →"
  → /voucher-checkout/?embed=1&linklogin=…&linkorig=…
  → purchase as above → code displayed + "Connecting you…"
  → top-level redirect: {linklogin}#rd-voucher=CODE
  → login.html reloads → hash reader → stash → fill → PAP submit
  → alogin.html → original URL. (No cross-scheme form POST —
    navigation is not mixed-content blocked; submission is
    http→http on the router page itself.)
```

### Rescue path (any failure after code exists)

```
RADIUS rejects / page reloads / user lands back on login.html
  → login.html load: check sessionStorage for stashed code
  → render banner: "Your voucher code: XXXX — reconnecting…"
  → auto-re-submit up to 2 attempts (1s backoff; covers
    RADIUS voucher-propagation race)
  → after 2 failures: banner shows code + "enter it in the
    Voucher tab" (input pre-filled)
```

### Key decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Code always displayed after purchase, every mode | Safety net; cross-origin parents can't read the display |
| 2 | **Auto-login is always plain PAP** (`document.login.submit()` bypasses the CHAP `onSubmit`) | CHAP challenge can be stale after a multi-minute payment; code == username so CHAP protects nothing here. CHAP stays for manual tabs |
| 3 | Fragment redirect (not form POST) for fallback code delivery | HTTPS→HTTP **form submission** is mixed-content blocked by Chrome/WebView ≥81 — the exact fallback audience. Top-level navigation isn't blocked |
| 4 | postMessage targetOrigin: referrer origin only, **never `'*'`**; unknown referrer → no postMessage | `'*'` lets an evil embedding parent harvest codes post-payment; parent-side origin validation can't stop the case where the evil site IS the parent |
| 5 | sessionStorage stash + rescue banner + ≤2 auto-retries | Money-paid-code-vanished is the reputational killer; also covers RADIUS propagation race |
| 6 | `linklogin` AND `linkorig` both validated server-side; invalid → dropped | `linkorig` becomes `dst` → post-login redirect; unvalidated = open redirect |
| 7 | New third tab; Voucher tab stays default | Code entry remains the primary fast path |
| 8 | Full-page and iframe share one code-delivery mechanism; rollout phases full-page first | Halves the risk surface; iframe tab is a cheap increment on top |

## Security

```
THREAT                              MITIGATION
───────────────────────────────     ─────────────────────────────────
Evil site embeds portal,            No '*' postMessage; unknown referrer
harvests voucher codes              origin → code only rendered (cross-
                                    origin parent can't read DOM).
                                    Evil parent passing a fake linklogin
                                    → server-side validator blocks
                                    non-private hosts, so the fragment
                                    redirect can't be aimed at attacker
Parent receives spoofed message     Parent validates e.origin ===
                                    PORTAL_ORIGIN + payload.source ===
                                    'rd-voucher' + code charset
                                    /^[A-Za-z0-9_-]+$/
Open redirect after login           linklogin AND linkorig validated:
                                      scheme http/https AND host is
                                      private IPv4/IPv6 literal, loopback,
                                      or link-local. Single-label
                                      hostnames REJECTED (http://evil/
                                      is resolvable). Invalid → param
                                      dropped, RouterOS default redirect
Voucher code in server logs         Fragment (#rd-voucher=) never sent
                                    to the router; POST body never GET
Guest API abuse (USSD-push          Per-IP + per-phone rate limit on
bombs to arbitrary numbers,         create_web_checkout; cap outstanding
unbounded Draft sales, poll         Draft sales/IP; outbound PesaPay poll
amplification)                      no more than 1×/3s per checkout_token
                                    (client already polls 3s; server
                                    caches/backoffs)
Voucher theft via sniffing          Same exposure as the existing manual
(HTTP hotspot)                      voucher tab (username visible in CHAP
                                    too); accepted risk on LAN
```

## Component changes

### Repo: `radius_desk`

```
radius_desk/www/voucher-checkout/
├─ index.py     + parse ?embed / ?linklogin / ?linkorig
│                 + validate BOTH linklogin & linkorig server-side
│                   (drop when invalid; tests cover IPv6 ULA, decimal/
│                    hex IP encodings, javascript:, public hosts)
│                 + context: embed flag + validated params
├─ index.html   + conditional extends: embed ? embed_base : web
│               + embed chrome: compact cards, no navbar/footer
│               + per-method approval hints at checkout
│               + honest MAX_POLLS copy ("ask cafe staff to retrieve
│                 your voucher" — there is no SMS)
├─ index.js     + embed iframe mode: on success
│                   postMessage (referrer-origin targetOrigin only)
│                   + keep code on screen
│               + fallback mode: on success, brief "Connecting you…"
│                   then location redirect
│                   {linklogin}#rd-voucher={code}
└─ embed_base.html (NEW)
                  standard frappe web bundle (frappe.call, jQuery —
                  same-origin, required by index.js), minus
                  navbar/footer chrome; no third-party assets

radius_desk/radius_desk/radius_desk/doctype/voucher_sale/voucher_sale.py
├─ create_web_checkout: + fail fast w/ clear error if PesaPay response
│                         contains a redirect_url (pre-auth clients
│                         can't reach it)
├─ create_web_checkout / confirm_voucher_web_checkout:
│     + per-IP & per-phone rate limits; outbound poll backoff
│     (≥1 poll per 3s per checkout_token, cached)
└─ (unchanged otherwise: checkout_token flow, fulfilment, POS)

hooks.py
└─ scheduler_events: + frequent/5min retry of Fulfillment Failed sales
                      (existing retry_voucher_sale) + notify operator
                      (standard Frappe Notification email to System
                      Managers) when a sale fails after payment
```

### Repo: `hotspot-cafe-config`

```
mikrotik/hotspot/login.html
├─ tab nav      + "Buy Voucher" tab (voucher stays default)
├─ tab-buy      + iframe (fixed height ~480px, internal scroll,
│                 PORTAL/voucher-checkout?embed=1)
│               + "portal unreachable" note when site is down
│               + "Open shop in full page →" link using the
│                 -esc variants: $(link-login-only-esc),
│                 $(link-orig-esc) (RouterOS does NOT auto-encode;
│                 raw & in dst would truncate params)
└─ <script>     + code receiver:
                  • message listener: validate origin/source/charset
                  • location.hash reader (#rd-voucher=) on load
                  • stash to sessionStorage
                  • fill username=password=code
                  • document.login.submit() (PAP, bypasses CHAP)
                  • rescue banner on load if stashed code exists:
                    auto-retry ≤2 (1s backoff), then show code +
                    pre-filled voucher input
                  • clear stash after successful navigation

mikrotik/04-walled-garden.rsc (NEW — phase-4 script)
  /ip hotspot walled-garden-ip add
      dst-host=njeremoto.jh.erpnext.com action=allow
      comment="Frappe guest portal pre-auth"
  + block DoH bypass: drop hotspot-client TCP/UDP 853 and known
    DoH resolver IPs (clients using DoH resolve different edge IPs
    than the router allowed)
  + backup note in README: export hotspot dir + break-glass user
    before uploading new login.html (rollback = re-upload old file)
```

## Error handling

| Failure | Behaviour |
|---|---|
| Payment fails / never approved | Existing states inside iframe ("Payment failed", retry). No router impact. |
| RADIUS rejects first auto-login (propagation race — the *most likely* first-submit failure) | Rescue path: banner + auto-retry ≤2 with backoff, then manual pre-filled entry |
| Any reload/navigation after code exists | sessionStorage stash → rescue banner (path A of stranded-sale cluster) |
| USSD overlay kills mini-browser mid-poll (path B) | Accepted risk v1: sale completes server-side; operator notified by scheduler on fulfilment failure; staff retrieve code from Voucher Sale |
| MAX_POLLS reached | Honest copy: "ask cafe staff"; code retrievable desk-side |
| InnBucks/Omari buyer on café WiFi without mobile data | Pre-emptive hint at checkout; QA confirms behaviour |
| `linklogin`/`linkorig` tampered | Validator drops → code shown manually / RouterOS default redirect |
| Cloudflare IP rotation | RouterOS dst-host re-resolution verified live in QA (TTL probe → wait → re-probe); DoH-blocked clients can't diverge |
| Frappe Cloud cold start / hibernation | Verify plan at rollout; droplet cron keep-alive curls /voucher-checkout/ every 15 min |
| PesaPay returns a redirect URL | Fail fast with clear error instead of a 5-minute silent poll |
| Cloud/droplet down after payment | Scheduler retries Fulfillment Failed every 5 min + notifies operator; break-glass: pre-made voucher stock on RADIUSDesk |

## Testing

Automated (Frappe, bench pytest):
- Embed context switching: no params / `embed=1` / `embed=1&linklogin=…`.
- Validators (both params): accept private IPv4/IPv6 literals, loopback,
  link-local; reject public hosts, single-label hosts, `javascript:`,
  decimal/hex IP encodings, malformed URLs.
- Rate limits: create_web_checkout throttles per IP/phone; poll backoff.
- `redirect_url` fail-fast.
- Retry scheduler picks up Fulfillment Failed sales.
- Existing tests green. Portal JS manual (no JS test infra).

Manual QA checklist (live hardware):

```
1.  Pre-auth reachability: /voucher-checkout/?embed=1 renders from a
    connected, not-logged-in client (walled garden works)
2.  Laptop iframe happy path → auto-logged-in → alogin → original URL
3.  DESKTOP CHROME full-page fallback path end-to-end (the browser that
    would have caught the mixed-content bug)
4.  Android mini-browser: iframe flow, else fallback flow
5.  iOS CNA (if any Apple device available): iframe/fallback behaviour
6.  Buy on phone connected to café WiFi, mobile data OFF:
    EcoCash → succeeds; InnBucks → hint visible, expected timeout
7.  Reload-after-purchase: refresh login.html mid-flow → rescue banner
    shows code, auto-retry works
8.  Sabotaged auto-login (bad code injected) → 2 retries → banner +
    pre-filled manual entry
9.  linkorig with & and query string → no truncation (-esc verified)
10. Tamper linklogin/linkorig to public host → params dropped
11. walled-garden dst-host re-resolution: query TTL, wait, re-probe
12. Frappe site briefly stopped → "portal unreachable" note in tab;
    rollback drill: re-upload previous hotspot dir, verify old login OK
```

## Rollout order

```
Phase 1 (full-page flow, ships value alone):
  ① Install app + Settings + Plans on njeremoto.jh.erpnext.com;
     verify site doesn't hibernate; add droplet keep-alive cron
  ② Upload hotspot/ with hash-reader + rescue banner +
     "Open shop in full page" link (no iframe yet)
  ③ Run 04-walled-garden.rsc (portal + DoH blocks)
  ④ QA items 1, 3, 6–12
Phase 2 (iframe tab):
  ⑤ Add Buy Voucher tab + iframe + postMessage listener
  ⑥ QA items 2, 4, 5
Always: backup hotspot dir on router before upload (rollback = restore)
```

## Limitations

- No self-service phone-number lookup; customer-side recovery is the
  sessionStorage banner only (closes with the browser).
- Mini-browser quirks mitigated, not eliminated (iOS CNA least testable).
- Portal URL hardcoded per hotspot page (one café, one router).
- PAP auto-login exposes the code on the local HTTP hop — same exposure
  class as the existing voucher tab (username visible under CHAP);
  accepted LAN risk.
