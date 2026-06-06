# LocalSoundBoard — Login + Subscription Design Document

**Status:** Canonical reference. Lead Architect's decisions are final; disagreements between source dimensions are resolved inline and marked **RULING**.
**Scope:** Add (1) user login/registration and (2) a paid subscription to a single-user, fully-local Windows desktop app (Python 3.13 + CustomTkinter, frozen `.exe` via PyInstaller).
**Audience:** The solo dev who will build this.
**Build checklist:** an ordered, tickable steps-and-checkpoints playbook derived from this doc lives in [BUILD_CHECKLIST.md](BUILD_CHECKLIST.md).

---

## 1. Executive Summary + Recommended MVP

LocalSoundBoard today has **zero** network, auth, or crypto code; a plaintext `soundboard_config.json`; and a frozen working directory anchored to a **user-writable** root. That last fact is load-bearing: anything cached next to the `.exe` is trivially editable, so trust must come from a **server signature the client verifies**, never from "we stored a flag."

The architecture that five of six design dimensions independently converged on is sound. The single hard conflict — **Stripe vs Paddle** — resolves to **Paddle** for a solo dev selling a downloadable `.exe` worldwide. The reason is decisive: with raw Stripe **you** are the Merchant of Record and personally liable for global VAT/GST/US-state sales tax from your first international sale. A Merchant-of-Record (MoR) provider remits all of that and absorbs chargeback operations. The ~2% fee premium is the cheapest tax-compliance insurance you can buy.

**The recommended MVP, in five bullets:**

- **Buy auth, buy billing, build only the desktop-license glue.** Supabase (GoTrue Auth + Postgres + RLS + Edge Functions) + Paddle (MoR billing + hosted checkout + customer portal) + ~250 LOC of Ed25519 signed-entitlement code you write.
- **The one irreducible custom piece** is an **offline-validatable, device-bound, Ed25519-signed entitlement token**. No vendor sells this for a desktop `.exe`; it is the crux of offline use and anti-piracy.
- **Two-tier gate:** authentication is a **hard** gate for *Pro features and sync only*; the **core soundboard runs anonymously/Free** with no account. (This corrects CLIENT's "no token, no app" — a hard login wall would kill adoption of a free desktop toy.)
- **One invariant makes everything cohere:** `Paddle → webhook → subscriptions table → recompute_entitlement() → entitlements row → /entitlements Edge Function signs an Ed25519 bundle → client verifies OFFLINE with an embedded public key → gates features on bundle.features[]`. The client never sees billing status, never reads the `subscriptions` table, never holds a server secret. The payment provider is therefore a swappable detail.
- **Honest anti-piracy:** a PyInstaller `.exe` decompiles in minutes. You will not stop a determined cracker. You *will* protect your server + every user's data (RLS, TLS, MoR-as-truth), make **cloud-dependent** Pro value impossible to fake, and raise the casual-sharing bar with device binding + signed licenses + short TTL. The real moat is **cloud sync**, not DRM.

---

## 2. How Hard Is It / What It Takes

### Build-vs-buy decision

| Component | Decision | Why |
|---|---|---|
| Auth (register/verify/reset/refresh-rotation/hashing) | **BUY — Supabase GoTrue** | Security-sensitive, undifferentiated, audited. Hand-rolling argon2id + JWT rotation is 4-6 wks of liability. |
| Billing + tax + chargebacks | **BUY — Paddle (MoR)** | MoR remits global VAT/GST/sales tax and absorbs disputes. Raw Stripe makes *you* liable. |
| Database + API host | **BUY — Supabase Postgres + Edge Functions** | Plain Postgres underneath → exportable, no lock-in, clean path to self-host later. |
| **Offline entitlement signer** | **BUILD — ~150 LOC Edge Function + ~250 LOC client** | The one thing no BaaS provides for a desktop client. |
| License-key SaaS (Keygen/Cryptolens) | **DEFER** | The DIY Ed25519 layer is small and you need a backend anyway. Same offline shape → migrate later if seat-mgmt becomes a sink. |

**Rejected:** Stripe-as-MoR-substitute on raw Stripe (tax liability); Lemon Squeezy (winding into Stripe post-acquisition); Firebase (NoSQL fights a relational billing domain); Clerk/Auth0 for MVP (solve web-session auth, not the desktop-license problem — you'd buy the easy part and keep the hard part).

### Effort (solo dev, Python-comfortable, new to backend/billing — the realistic case)

| Milestone | Dev-weeks | Notes |
|---|---|---|
| **Revenue-generating MVP** | **6-8** | Supabase + Paddle + entitlement signer + login gate + license verify + frozen+signed `.exe`, Pro Monthly only. |
| **Robust v1 (total)** | **12-17** | Adds yearly/proration/grandfathering, dunning, refund/chargeback revoke, device-management UI, cloud sync, clock-tamper + key rotation, GDPR delete/export, observability. |
| **Fastest path to first paid customer** | **3-4** | Server spine first, tested via HTTPie before touching the `.exe`; client cut to login + gate + checkout-open + poll. |

The single most underestimated line item is **PyInstaller frozen-`.exe` bundling** (keyring Windows backend, libsodium DLL for pynacl, certifi for requests) — budget a dedicated 0.5-1 week and test the *frozen build in CI*, not just `python main.py`.

### Monthly cost at small scale (<1k users)

| Item | Cost/mo |
|---|---|
| Supabase | $0 (free tier) → **$25** (Pro: daily backups, no project pausing — get it once you have real users) |
| Paddle (MoR) | **5% + $0.50 per transaction** (no fixed fee) |
| Email (Resend) | $0 (3k/mo free) → $20 |
| Code-signing cert | ~$15-40/mo amortized (or Azure Trusted Signing ~$10/mo) |
| Domain + Sentry (free) + uptime monitor (free) | ~$1-2/mo |
| **Fixed total, pre-revenue** | **~$0-30/mo** |
| **At ~100 paying subs (~$699 rev)** | **~$110-150/mo** (dominated by the ~5% MoR cut, which scales *with* revenue) |

The key property: fixed cost is near-zero until customers exist; the largest cost only appears when money is coming in.

---

## 3. Recommended Reference Architecture

```
+===============================================================================+
|                          UNTRUSTED  (the user's PC)                           |
|                                                                               |
|   LocalSoundBoard.exe  (PyInstaller, Python 3.13 + CustomTkinter)             |
|   +-----------------------------------------------------------------------+   |
|   |  main.py  -- PRE-GATE before SoundboardApp() (SKIPPABLE: Free=no acct) |   |
|   |     |                                                                 |   |
|   |     v                                                                 |   |
|   |  soundboard/auth.py       Session: bootstrap, silent refresh, logout  |   |
|   |  soundboard/login_ui.py   own throwaway CTk root (login/register/     |   |
|   |                           forgot/verify) + "Continue without account"  |   |
|   |  soundboard/license.py    Entitlement: Ed25519 verify (EMBEDDED PUBKEY)|   |
|   |                           + offline grace + clock-tamper guards        |   |
|   |  soundboard/billing.py    open_checkout(url)->browser; poll_entitlement|   |
|   |  soundboard/auth_store.py keyring (Win Credential Manager) + device_id |   |
|   +-----------------------------------------------------------------------+   |
|        |                |                  |                     ^             |
|  refresh token    Ed25519 entitlement   device_id          SOFT feature      |
|  + tamper HWM     bundle (cached file)   (hash of           gate reads        |
|  -> Cred Manager  -> auth_cache.json     MachineGuid)       features[] only   |
|                                                                               |
|   Pro gates at ACTION handlers (never in the hot audio loop):                 |
|     voice_fx.VoiceChanger | audio.Recorder.start() | yt-dl btn | editor       |
+===============================================================================+
        |  HTTPS / TLS 1.2+  (verify=True, NO leaf pinning)   |  system browser
        v                                                     v
+===============================================================================+
|                          TRUSTED  (managed cloud)                             |
|                                                                               |
|   SUPABASE                                          PADDLE (Merchant of Record)|
|   +----------------------------+                    +------------------------+ |
|   | GoTrue Auth  [BUY]         |                    | Hosted Checkout        | |
|   |  register/login/verify/    |                    | Customer Portal        | |
|   |  reset/refresh-rotation    |                    | Tax(VAT/GST)+disputes   | |
|   |  -> access JWT + refresh   |                    +-----------+------------+ |
|   +-------------+--------------+                                | signed       |
|                 | access JWT (Bearer)                          | webhook      |
|                 v                                              v              |
|   +-------------------------------+   reads     +--------------------------+   |
|   | Edge Functions  [BUILD ~8]    |<------------| /billing/webhook         |   |
|   |  GET /entitlements  *(SIGNER)*|  subs row   |  verify Paddle-Signature |   |
|   |  POST/GET/DEL /devices        |             |  -> upsert subscriptions |   |
|   |  POST /billing/checkout       |             |     (idempotent ledger)  |   |
|   |  POST /billing/portal         |             |  -> recompute_entitlement|   |
|   |  GET  /health (+min_version)  |             +-----------+--------------+   |
|   |  -- holds Ed25519 PRIVATE key |                         |                  |
|   |     (KMS/secret; NEVER ships) |                         v                  |
|   +---------------+---------------+             +--------------------------+   |
|                   |  signs bundle               | Postgres + RLS  [BUILD]  |   |
|                   +---------------------------->|  profiles, subscriptions,|   |
|                   reads entitlements row        |  entitlements, devices,  |   |
|                                                 |  license_tokens,         |   |
|                                                 |  webhook_events, audit   |   |
|                                                 |  RLS: user_id=auth.uid() |   |
|                                                 +--------------------------+   |
+===============================================================================+

KEY:  [BUY]=managed, zero code   [BUILD]=your code (~8 Edge Fns + DDL, ~600-800 LOC)
      *(SIGNER)* = the ONE piece no BaaS gives you: the offline entitlement issuer.
```

**Two token types — name them so they are never cross-wired:**

1. **Auth tokens** (from GoTrue): a short-lived **access JWT** (HS256, ~1h, in-memory only) + an **opaque rotating refresh token** (stored in Windows Credential Manager). Prove *identity*.
2. **Entitlement token** (from your Edge Function): an **Ed25519-signed bundle** (7-day `exp` + 14-day offline grace), device-bound, verified offline with the **embedded public key**. Proves *what you may do*; it is the offline/anti-piracy artifact.

The refresh token mints access JWTs → the access JWT authorizes the call to `/entitlements` → that endpoint signs the entitlement bundle → the client gates Pro features on the **bundle's `features[]`**, never on the auth JWT and never on billing status.

---

## 4. End-to-End Flows (ASCII sequence diagrams)

### (a) First-run register / login (hard gate is for Pro/sync; Free is skippable)

```
USER    CLIENT(.exe)         GoTrue           EdgeFn(/devices,/entitlements)   Postgres
 | launch    |                  |                       |                        |
 |---------->| Session.bootstrap(): load refresh -> none |                        |
 |           | show login_ui  [Sign in | Create | Continue without account]      |
 |           |                  |                       |                        |
 | (Path 1: Continue without account) -> app starts in FREE, no network needed   |
 |           |                  |                       |                        |
 | (Path 2: Register/Login)     |                       |                        |
 | enter creds                  |                       |                        |
 |---------->| POST /auth/v1/signup (or /token grant=pw) |                        |
 |           |----------------->| argon2 verify; send verify email               |
 |           |<-----------------| 200 access JWT + refresh token                 |
 |           | SAVE refresh -> Windows Credential Manager (NOT config.json)       |
 |           | device_id = hash(MachineGuid)            |                        |
 |           | POST /devices (Bearer access, device_id) |                        |
 |           |--------------------------------------->| enforce seat cap (3)      |
 |           |                  |                     | insert device ----------->| HMAC(guid)
 |           |<---------------------------------------| 201 (slots 1/3)           |
 |           | GET /entitlements (Bearer, X-Device-Hash)                          |
 |           |--------------------------------------->| read subs row: none ----->| FREE
 |           |<---------------------------------------| sign Ed25519{plan:free,    |
 |           |   cache bundle -> auth_cache.json      |   grace_days:14}          |
 |<----------| verify OK -> SoundboardApp(session) ; Pro shows LOCK + upsell      |
```

> Email-verify edge case: a user can complete Paddle checkout with an unverified email and then be unable to log in. **RULING:** the webhook auto-flags/auto-verifies a paying email, and "Refresh subscription" forces server-side reconciliation. This *will* happen — handle it.

### (b) Subscribe via checkout + webhook (webhook is source of truth)

```
USER    CLIENT             EdgeFn(/billing)        PADDLE(MoR)           Postgres
 | click Pro |                  |                      |                     |
 |---------->| POST /billing/checkout (Bearer) ------->| create checkout w/  |
 |           |                  | customData{user_id, device_id} --------->| session
 |           |<-----------------| {checkout_url}       |                     |
 |           | webbrowser.open(checkout_url) -- SYSTEM browser, NOT webview  |
 | pay in    |.............................................................>| charge OK
 | browser   |                  |                      |                     | + remit VAT
 |           |                  |   POST /billing/webhook (signed) <---------|
 |           |                  | VERIFY Paddle-Signature FIRST (reject else)|
 |           |                  | idempotent INSERT evt_id ON CONFLICT NOTHING
 |           |                  | apply-if-newer -> upsert subscriptions --->| status=active
 |           |                  | recompute_entitlement() -> entitlements -->| tier=pro
 |           | poll GET /entitlements every 3s up to ~60s, then back off     |
 |           |--------------------------------------->| read subs: ACTIVE/pro |
 |           |<---------------------------------------| sign Ed25519{plan:pro, |
 |           |   verify sig; cache; flip UI           |  features[...], exp=+7d,|
 |<----------| toast "You're Pro now"                 |  grace_days:14, dev_id}|
```

> **Do not trust the browser redirect** (it can be closed/lost). The webhook → DB is truth; the client polls `/entitlements` to flip to Pro.

### (c) Launch-time + periodic entitlement validation (online)

```
CLIENT (worker thread; NEVER on UI/audio thread)         EdgeFn            Postgres
 | launch -> Session.bootstrap()                            |                 |
 |   refresh token -> POST /auth/v1/token grant=refresh ----> rotate; access JWT
 |   GET /entitlements (Bearer) ---------------------------->| read subs ----->| pro/free
 |<--------------------------------------------------------- | fresh 7d bundle |
 |   verify sig (embedded pubkey) -> cache -> UI in sync     |                 |
 |                                                           |                 |
 | self.root.after(60_000, _auth_tick):  # 60s CHEAP LOCAL CHECK              |
 |   maybe_refresh()             # mint access JWT if <2min to expiry         |
 |   maybe_refresh_entitlement() # RE-FETCH bundle only if >6-24h since last  |
 |   (network in worker thread; results marshaled via root.after(0, ...))     |
```

> Separate "check cached state every 60s" (free, local) from "re-fetch the bundle from the server every 6-24h when online." That resolves the cadence disagreement between dimensions.

### (d) OFFLINE behavior + grace period (the desktop crux)

```
CLIENT  [on a plane, NO connectivity]
 | launch -> Session.bootstrap(): refresh attempt -> NETWORK FAIL (swallowed)
 | license.load(): read cached bundle from auth_cache.json
 |   1. Ed25519 verify(body, sig) with EMBEDDED PUBLIC KEY            [offline]
 |   2. bundle.device_id == hash(local MachineGuid) ?                 [binding]
 |   3. rollback guard:   bundle.iat >= stored max_seen_iat ?         [replay]
 |   4. trusted_now = min(wallclock, last_server_time + cap)          [tamper]
 |   5. time policy:
 |        now < exp                 -> VALID       (Pro ON, silent)
 |        exp <= now < exp + 14d    -> SOFT_GRACE  (Pro ON, dismissible banner:
 |                                                  "Reconnect within N days")
 |        now >= exp + 14d          -> EXPIRED     (Pro OFF, FREE still runs)
 | app starts; Pro gates read bundle.features[] -- ZERO network used
 |
 | [back online] background heartbeat re-fetches a fresh 7d bundle;
 |   banner clears; max_seen_iat advanced. Free tier ALWAYS works regardless.
```

**Principle:** a momentary lapse must never interrupt an *active* feature. Evaluate entitlement at **feature-start boundaries** (clicking Record, toggling Voice FX), never per audio frame. A running recording/stream is never torn down by grace ticking over — only the *next* start is gated.

### (e) Logout / subscription lapse / revocation

```
LOGOUT
 | user clicks Sign out
 |   best-effort: POST /auth/v1/logout (revoke refresh family server-side)
 |   keyring.delete(refresh_token); auth_cache <- {device_id only}
 |   access_token = entitlement = None
 |   clean shutdown (audio streams, hotkeys, tray all need teardown)
 |   -> relaunch shows login_ui (or "Continue without account" -> Free)

SUBSCRIPTION LAPSE (canceled / past_due exhausted)
 | Paddle -> subscription.canceled webhook -> subscriptions.status=canceled
 |   recompute_entitlement() -> tier=free
 | next client heartbeat: /entitlements signs tier=free
 |   running audio finishes; Pro toggles disable; banner:
 |   "Your Pro plan ended. Renew to re-enable voice FX & recording."
 | offline users keep Pro only until exp+grace lapses (acceptable).

REVOCATION (device revoked / token leaked / refund / chargeback)
 | Layer 1 (primary): short 7d exp -> revocation self-heals within a week
 |                     once the client cannot refresh.
 | Layer 2 (immediate, online): server refuses to sign for revoked device/jti;
 |   chargeback webhook -> tier=free + account.flagged=true IMMEDIATELY.
 |   Client never trusts a revocation list; it simply fails to refresh.
```

---

## 5. Database Schema (DDL)

Layout: `auth.*` is Supabase-managed (do **not** recreate it); you build the `app.*` business tables. Money is `bigint` minor units, never float. Stripe-style natural keys are replaced with Paddle IDs per the ruling.

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS citext;     -- case-insensitive email
CREATE SCHEMA IF NOT EXISTS app;

-- 1:1 with auth.users (GoTrue owns email, password hash, verify, reset)
CREATE TABLE app.profiles (
    user_id            uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    display_name       text,
    paddle_customer_id text UNIQUE,                 -- ctm_...  (was stripe_customer_id)
    flagged            boolean NOT NULL DEFAULT false,  -- abuse/chargeback flag
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Catalog (mirror of Paddle Product -> Prices). Drives entitlement tier.
CREATE TABLE app.prices (
    id               text PRIMARY KEY,              -- Paddle price id: pri_...
    nickname         text,                          -- "Pro Monthly"
    unit_amount_cents bigint NOT NULL,              -- 699 = $6.99
    currency         char(3) NOT NULL DEFAULT 'usd',
    interval         text NOT NULL CHECK (interval IN ('month','year','one_time')),
    entitlement_tier text NOT NULL DEFAULT 'pro' CHECK (entitlement_tier IN ('free','pro')),
    active           boolean NOT NULL DEFAULT true
);

-- Mirror of Paddle subscription state. Paddle is the source of truth.
CREATE TABLE app.subscriptions (
    id                   text PRIMARY KEY,          -- Paddle sub id: sub_... (null for lifetime)
    user_id              uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    price_id             text REFERENCES app.prices(id),
    plan                 text NOT NULL DEFAULT 'monthly',  -- monthly|yearly|lifetime
    status               text NOT NULL CHECK (status IN
                           ('trialing','active','past_due','paused','canceled')),
    current_period_end   timestamptz,               -- "paid through" date
    cancel_at_period_end boolean NOT NULL DEFAULT false,
    grandfathered_price_id text,                     -- locked-in pricing while active
    paddle_event_seq     bigint,                    -- monotonic out-of-order guard
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX subs_user_idx ON app.subscriptions(user_id);
CREATE UNIQUE INDEX subs_one_live_per_user ON app.subscriptions(user_id)
    WHERE status IN ('trialing','active','past_due');

-- Device binding. HASH of MachineGuid, never the raw GUID.
CREATE TABLE app.devices (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    device_hash      text NOT NULL,                 -- HMAC(server_pepper, client_machine_hash)
    label            text,                          -- "Roy's Desktop"
    os               text,
    app_version      text,
    last_seen_at     timestamptz NOT NULL DEFAULT now(),
    revoked_at       timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, device_hash)
);
CREATE INDEX devices_user_active_idx ON app.devices(user_id) WHERE revoked_at IS NULL;

-- The DERIVED, client-facing truth. Recomputed on every webhook. CLIENT READS THIS.
CREATE TABLE app.entitlements (
    user_id      uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    tier         text NOT NULL DEFAULT 'free' CHECK (tier IN ('free','pro')),
    features     jsonb NOT NULL DEFAULT '{}'::jsonb,  -- {"voice_fx":true,...}
    max_devices  int  NOT NULL DEFAULT 3,            -- seat cap (RULING: 3)
    source       text NOT NULL DEFAULT 'default'
                 CHECK (source IN ('default','subscription','trial','grant','comp')),
    source_id    text,
    valid_until  timestamptz,                        -- = current_period_end (2999 for lifetime)
    updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Idempotency ledger. Webhooks are at-least-once and out-of-order.
CREATE TABLE app.webhook_events (
    id             text PRIMARY KEY,                -- Paddle event_id: evt_...
    type           text NOT NULL,
    occurred_at    timestamptz NOT NULL,
    payload        jsonb NOT NULL,                  -- null out after ~90d (PII)
    status         text NOT NULL DEFAULT 'received'
                   CHECK (status IN ('received','processed','failed')),
    received_at    timestamptz NOT NULL DEFAULT now()
);

-- Metadata for offline tokens (the signed payload itself lives on the client).
CREATE TABLE app.license_tokens (
    jti           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    device_id     uuid REFERENCES app.devices(id) ON DELETE CASCADE,
    not_after     timestamptz NOT NULL,
    grace_until   timestamptz NOT NULL,
    revoked_at    timestamptz,
    issued_at     timestamptz NOT NULL DEFAULT now()
);

-- Append-only audit trail (INSERT/SELECT only; no UPDATE/DELETE for app roles).
CREATE TABLE app.audit_log (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    actor_type  text NOT NULL DEFAULT 'system' CHECK (actor_type IN ('user','admin','system','paddle')),
    actor_id    uuid,
    action      text NOT NULL,                      -- 'login.success','device.revoked',...
    target_type text, target_id text,
    metadata    jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now()
);
```

**Subscription status → entitlement mapping (pure function, run inside the webhook):**

| `subscriptions.status` | `entitlements.tier` | `valid_until` |
|---|---|---|
| `trialing` | `pro` | `trial_end` |
| `active` (incl. `cancel_at_period_end=true`) | `pro` | `current_period_end` |
| `past_due` | `pro` **through dunning grace** (period_end + 7d) | `current_period_end` |
| `paused` / `canceled` | `free` | `now()` |
| *(no row)* | `free` | n/a |
| `plan='lifetime'` | `pro` | `2999-01-01` (sentinel) |

```sql
CREATE OR REPLACE FUNCTION app.recompute_entitlement(p_user uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE s app.subscriptions%ROWTYPE; v_pro boolean := false; v_valid timestamptz := now();
BEGIN
  SELECT * INTO s FROM app.subscriptions
    WHERE user_id = p_user AND status IN ('trialing','active','past_due')
    ORDER BY current_period_end DESC NULLS LAST LIMIT 1;
  IF FOUND THEN
    v_pro := true;
    v_valid := CASE WHEN s.plan='lifetime' THEN '2999-01-01'::timestamptz
                    ELSE s.current_period_end END;
  END IF;
  INSERT INTO app.entitlements(user_id,tier,features,max_devices,source,source_id,valid_until,updated_at)
  VALUES (p_user,
          CASE WHEN v_pro THEN 'pro' ELSE 'free' END,
          CASE WHEN v_pro THEN jsonb_build_object(
              'voice_fx',true,'recording',true,'yt_download',true,
              'editor',true,'speed_pitch',true,'cloud_sync',true)
            ELSE '{}'::jsonb END,
          3, CASE WHEN v_pro THEN 'subscription' ELSE 'default' END, s.id, v_valid, now())
  ON CONFLICT (user_id) DO UPDATE SET
      tier=EXCLUDED.tier, features=EXCLUDED.features, max_devices=EXCLUDED.max_devices,
      source=EXCLUDED.source, source_id=EXCLUDED.source_id,
      valid_until=EXCLUDED.valid_until, updated_at=now();
END $$;
```

**RLS:** enable on every `app.*` table; client read policy `user_id = auth.uid()`. All billing/webhook writes use the **service-role key** (bypasses RLS). Clients can never write `subscriptions`/`entitlements`.

**Free-tier caps** (`max_tabs`/`max_sounds`) are a **deferred product decision** — ship Free = "core soundboard, no Pro features"; add quantity caps later only if needed.

---

## 6. Backend API Surface

`Auth` column: `none` (public) · `access` (GoTrue Bearer JWT) · `refresh` (rotating) · `webhook-sig` (Paddle-Signature). Rows marked **[GoTrue]** are configuration, not code. **[BUILD]** rows are the ~8 Edge Functions you write.

| # | Method & Path | Auth | Purpose | Owner |
|---|---|---|---|---|
| 1 | `POST /auth/v1/signup` | none | Register; triggers verify email | [GoTrue] |
| 2 | `POST /auth/v1/token?grant_type=password` | none | Login → access + refresh | [GoTrue] |
| 3 | `POST /auth/v1/token?grant_type=refresh_token` | refresh | Refresh (rotates refresh) | [GoTrue] |
| 4 | `POST /auth/v1/logout` | access | Revoke session/refresh family | [GoTrue] |
| 5 | `GET /auth/v1/user` | access | Current user | [GoTrue] |
| 6 | `POST /auth/v1/recover` | none | Password reset request | [GoTrue] |
| 7 | `POST /auth/v1/verify` | none | Email verification | [GoTrue] |
| 8 | `POST /auth/v1/resend` | none | Resend verification | [GoTrue] |
| 9 | **`GET /functions/v1/entitlements`** | access | **Sign + return Ed25519 entitlement bundle** | **[BUILD]** |
| 10 | **`POST /functions/v1/devices`** | access | Register/bind device (idempotent; enforces cap) | **[BUILD]** |
| 11 | **`GET /functions/v1/devices`** | access | List my devices | **[BUILD]** |
| 12 | **`DELETE /functions/v1/devices/{id}`** | access | Revoke a device (free a seat) | **[BUILD]** |
| 13 | **`POST /functions/v1/billing/checkout`** | access | Create Paddle checkout → upgrade | **[BUILD]** |
| 14 | **`POST /functions/v1/billing/portal`** | access | Create Paddle customer-portal session | **[BUILD]** |
| 15 | **`POST /functions/v1/billing/webhook`** | webhook-sig | Paddle webhook → update subscriptions | **[BUILD]** |
| 16 | **`POST /functions/v1/billing/reconcile`** | access | Force server-side PSP lookup ("I paid but I'm Free") | **[BUILD]** |
| 17 | **`GET /functions/v1/health`** | none | Liveness + `min_supported_version` kill-switch | **[BUILD]** |
| 18 | **`POST /functions/v1/account/delete`** | access | GDPR: purge + cancel sub at PSP | **[BUILD]** |
| 19 | **`GET /functions/v1/account/export`** | access | GDPR: download my data | **[BUILD]** |
| 20 | (opt) `POST/GET /functions/v1/sync/config` | access | Cloud config sync (the anti-sharing moat) | **[BUILD, Phase 3]** |

---

## 7. Payments / Subscription Design

**Provider: Paddle (Merchant of Record).** **RULING** over the Stripe assumption in CLIENT/BACKEND/DB. Rationale: MoR remits global VAT/GST/US-state sales tax and runs the dispute process; raw Stripe makes a solo dev personally liable for cross-border tax from sale #1. The swap is cheap because every dimension already isolated billing behind the entitlement indirection — only the webhook handler, `*_customer_id` columns, and checkout/portal URL minting change. **Revisit raw Stripe + Stripe Tax + an accountant at ~$1M/yr**, when the 5% fee exceeds in-house compliance cost.

**Plans (ruthlessly simple):**

| Plan | Price | Notes |
|---|---|---|
| Free | $0 | No account required. Gates Pro features. The Free tier *is* the trial. |
| Pro Monthly | $6.99/mo | Default entry point (MVP ships this only). |
| Pro Yearly | $49.99/yr | Push hard — best LTV, fewer dunning events (Phase 2). |
| Lifetime | $129 one-time | **Launch-only / limited promo.** Scope to "lifetime of major version v1.x"; cap metered cost centers. Put in EULA. |

**Trial:** no card-not-required trial (trivially farmed on a piracy-prone desktop app). The Free tier is the trial. Optionally a 7-day **card-required** Paddle trial later.

**Checkout/portal (desktop nuances):** open Paddle hosted checkout in the **system default browser** (`webbrowser.open`), never an embedded webview (breaks 3-DS, password managers, looks phishy). Pass `customData={user_id, device_id}`. "Manage subscription" = open the Paddle customer portal URL — Paddle owns dunning emails, receipts, VAT invoices.

**Webhook → entitlement sync (the core, must be idempotent):**

```python
@app.post("/billing/webhook")
def paddle_webhook(req):
    raw = req.raw_body
    if not paddle_verify_signature(raw, req.headers["Paddle-Signature"], WEBHOOK_SECRET):
        return 401                                   # signature FIRST, always
    event = json.loads(raw); eid = event["event_id"]
    try:
        db.execute("INSERT INTO app.webhook_events(id,type,occurred_at,payload) "
                   "VALUES (%s,%s,%s,%s)", eid, event["event_type"],
                   event["occurred_at"], json.dumps(event))
    except UniqueViolation:
        return 200                                   # duplicate retry -> ack & stop
    apply_event_if_newer(event)                      # paddle_event_seq guard (no rollback)
    recompute_entitlement(user_id_from(event))       # single source of truth
    return 200
```

**Events handled:** `subscription.created/activated/updated`, `transaction.completed` (lifetime), `subscription.past_due` (start 7-day dunning grace, Pro stays ON, soft banner), `subscription.canceled/paused`, `adjustment.created` (refund → revoke), dispute/chargeback (revoke immediately + flag). **Dunning policy:** keep Pro ON for 7 days past `current_period_end` — don't punish transient card declines. **Refund fee:** MoR keeps its ~5% on refunds, so set a 14-day refund window. **Upgrades/downgrades:** let Paddle compute proration (monthly→yearly immediate+prorated; yearly→monthly at period end). **Grandfathering:** existing subs keep their `price_id` while continuously active; store `grandfathered_price_id`; entitlement logic is price-agnostic (`tier=pro` regardless), so grandfathering is zero feature-gating code.

---

## 8. Offline Entitlement-Token Design

**Format:** bare Ed25519 (`base64url(claims) + "." + base64url(sig)`), **not** a JWT — smaller dependency, no `alg:none` footgun. **RULING — the exact canonical serialization is mandatory on both sides:** `json.dumps(claims, separators=(",",":"), sort_keys=True)` over UTF-8. Mismatched whitespace or key order silently breaks every signature.

**Claim shape (the signed payload):**

```jsonc
{
  "v": 1,                       // format version
  "kid": "k1",                  // key id (rotation: ship k1 + k2)
  "sub": "usr_8f2a...",         // user id
  "plan": "pro",                // "free" | "pro"
  "features": ["voice_fx","recording","yt_download","editor","speed_pitch","cloud_sync"],
  "seats": 3,                   // device cap (RULING)
  "device_id": "dev_a17c...",   // BOUND: token only valid on this machine
  "iat": 1748736000,            // issued-at
  "exp": 1749340800,            // hard expiry  (iat + 7 DAYS)  [RULING]
  "grace_days": 14,             // offline grace after exp      [RULING — ALWAYS present]
  "jti": "tok_3b9e...",         // revocation handle + replay nonce
  "sub_status": "active",
  "sub_period_end": 1751932800  // decoupled from exp; honors paid time
}
```

**Default knobs (single source of truth — put in `soundboard/constants.py` AND the Edge Function config):**

| Knob | Value | Note |
|---|---|---|
| Signature algorithm | **Ed25519** | tiny keys, fast verify, no curve footguns |
| Token `exp` | **7 days** | short → revocation propagates within a week |
| Offline grace | **14 days** | client default fallback MUST equal the stamped `grace_days` |
| Seat cap (Pro) | **3 devices** | desktop + laptop + friend's PC |
| In-app check cadence | **60 s** | cheap local re-read of the cached bundle |
| Server re-fetch cadence | **6-24 h when online** | mints a fresh 7-day bundle |
| Access JWT TTL | **~1 h** (GoTrue default) | 15-min is the self-hosted hardening target |
| Lifetime tier | longer grace **30 d** | else lifetime users offline >14d wrongly lose Pro |

**Client verify pseudocode** (the entire hot path — small, offline, side-effect-light):

```python
# soundboard/license.py
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
import json, time

EMBEDDED_PUBKEYS = {"k1": VerifyKey(bytes.fromhex("...")),   # public = safe to ship
                    "k2": VerifyKey(bytes.fromhex("..."))}   # 'next' for rotation
FREE = {"plan": "free", "features": [], "state": "free"}

def load_entitlement():
    token = keystore_get("lsb.token")                 # Windows Credential Manager
    if not token: return FREE
    body_b64, sig_b64 = token.split(".")
    body, sig = b64url(body_b64), b64url(sig_b64)
    try:
        claims = json.loads(body)
        EMBEDDED_PUBKEYS[claims.get("kid","k1")].verify(body, sig)   # raises if tampered
    except (BadSignatureError, ValueError, KeyError):
        return FREE
    if claims["device_id"] != local_device_id():                    # device binding
        return FREE
    hwm = keystore_get_int("lsb.max_seen_iat", 0)                    # rollback guard
    if claims["iat"] < hwm: return FREE
    keystore_set_int("lsb.max_seen_iat", max(hwm, claims["iat"]))
    return _apply_time_policy(claims)

def _apply_time_policy(claims):
    now = _trusted_now()                                            # forward-jump guard
    exp = claims["exp"]; grace_end = exp + claims.get("grace_days", 14) * 86400
    if now < exp:           state = "valid"
    elif now < grace_end:   state = "soft_grace"                    # Pro ON + banner
    else:                   return {**FREE, "reason": "grace_elapsed"}
    return {"plan": claims["plan"], "features": claims["features"],
            "state": state, "days_left": max(0, (grace_end - now)//86400)}

def _trusted_now():
    wall = int(time.time()); last = keystore_get_int("lsb.last_server_time", 0)
    return min(wall, last + 30*86400) if last else wall             # cap if clock jumped

def has_feature(ent, name): return name in ent.get("features", [])
```

**Device binding:** `device_id = base64url(BLAKE2b(MachineGuid ‖ install_salt)[:16])`, MachineGuid from `HKLM\SOFTWARE\Microsoft\Cryptography\MachineGuid` via `winreg`. The **server** stores `HMAC(server_pepper, that)` — never the raw GUID (privacy; pepper enables global invalidation). The `device_id` lives *inside* the signed payload, so copying a token to another machine fails the local-match check even with a valid signature.

**Clock-tamper:** rollback guard (reject `iat < max_seen_iat`) + forward-jump guard (cap `now` at `last_server_time + 30d`). First-run-after-install has no baseline → seed `last_server_time` at first successful login; absent it, treat wall clock as authoritative but cap grace conservatively. Markers live in the keystore (DPAPI-protected), not a JSON file.

**Grace rules:** `VALID` (silent) → `SOFT_GRACE` (Pro on, dismissible banner) → `EXPIRED` (Pro off, **Free always works**). Never a hard lock mid-session; gate at feature-start boundaries only.

---

## 9. Client Integration into THIS App

**Gate location — RULING: pre-gate in `main.py` *before* `SoundboardApp()` is constructed**, with a throwaway Tk root (CLIENT's reasoning beats LICENSE's "gate inside `__init__`": `SoundboardApp.__init__` builds the entire UI, audio cache, tabs, animation loop, and tray, and calls `update_idletasks()` which *paints the window* — you must not build any of that for a gating decision). **But** the gate is **skippable** ("Continue without account" → anonymous Free), per the §15 reconciliation.

```python
# main.py  (replace main() at lines 63-68, after DPI setup + logging)
from soundboard.auth import Session, run_login_gate

def main():
    session = Session.bootstrap()                 # load cached tokens; silent refresh
    if not session.is_authenticated():
        session = run_login_gate(session)         # own Tk root; allows "Continue without account"
        if session is None:                       # user closed/quit
            sys.exit(0)
        # session may be ANONYMOUS (Free) or authenticated here
    from soundboard import SoundboardApp
    app = SoundboardApp(session=session)
    app.run()
```

```python
# soundboard/gui.py  -- SoundboardApp.__init__ (~line 2047)
def __init__(self, session=None):
    self.session = session
    self.root = ctk.CTk()
    ...
    self.root.after(60_000, self._auth_tick)      # near other after() calls

def is_pro(self) -> bool:
    return bool(self.session and self.session.is_pro())

def _require_pro(self, feature_name: str) -> bool:
    if self.is_pro(): return True
    self._show_upsell(feature_name)               # CTkToplevel -> opens checkout
    return False
```

**Feature gates at ACTION handlers (never in hot audio loops):** wrap the GUI handler that *enables* the voice changer (not `VoiceChanger.process()`), `Recorder.start()`'s Record-button callback, the YouTube→MP3 download callback, and the editor/speed-pitch open/apply handlers. Hot paths read a single cached boolean; entitlement is recomputed on start, resume-from-sleep, and the 60s tick.

**Login window (CustomTkinter sketch):** one `ctk.CTk()` root with swappable views (Login / Register / Forgot / Verify) reusing the existing Discord palette/fonts from `constants.py`; "Remember me" defaults ON; `Return` submits; errors render inline (no `messagebox`); **add a show/hide password toggle** (fewer failed logins → fewer resets → fewer tickets); **all networking off the Tk thread**, marshaled back via `root.after(0, ...)` with `timeout=(3, 8)`.

```python
# soundboard/login_ui.py (essentials)
def _do_login(self):
    email, pw = self.email.get().strip(), self.pw.get()
    self._set_busy(True, "Contacting server...")
    def work():
        try:
            tokens = self.api.login(email, pw, device_id=self.session.device_id())
            self.root.after(0, lambda: self._on_login_ok(tokens))
        except EmailNotVerified: self.root.after(0, self._show_verify)
        except AuthError as e:   self.root.after(0, lambda: self._fail(str(e)))
        except NetworkError:     self.root.after(0, lambda: self._fail(
            "Can't reach the server. Check your connection."))
    threading.Thread(target=work, daemon=True).start()

def _on_login_ok(self, tokens):
    if self.remember.get():
        TokenStore.save_refresh(tokens["refresh_token"])    # secret -> keyring
    self.session.adopt(tokens); self.result = self.session
    self.root.destroy()                                      # hand back to main()
```

**Windows-keyring token store:**

```python
# soundboard/auth_store.py
import keyring, json, uuid
from pathlib import Path
_SERVICE = "LocalSoundBoard"; _CACHE = Path("auth_cache.json")  # signed bundle; NOT secret

class TokenStore:
    @staticmethod
    def save_refresh(t):  keyring.set_password(_SERVICE, "refresh_token", t)
    @staticmethod
    def load_refresh():
        try: return keyring.get_password(_SERVICE, "refresh_token")
        except keyring.errors.KeyringError: return None
    @staticmethod
    def clear_refresh():
        try: keyring.delete_password(_SERVICE, "refresh_token")
        except keyring.errors.KeyringError: pass
    @staticmethod
    def device_id():
        c = json.loads(_CACHE.read_text("utf-8")) if _CACHE.exists() else {}
        if not c.get("device_id"):
            c["device_id"] = str(uuid.uuid4())
            tmp = _CACHE.with_suffix(".tmp"); tmp.write_text(json.dumps(c),"utf-8"); tmp.replace(_CACHE)
        return c["device_id"]
```

**Storage decision table:**

| Item | Store | Why |
|---|---|---|
| Refresh token (the secret) | **keyring → Windows Credential Manager** (DPAPI) | Encrypted at rest; survives reinstall; OS-managed. |
| Access token (short JWT) | **in memory only** | Re-minted from refresh on launch. |
| Signed entitlement bundle + device_id | `auth_cache.json` | Signed → safe in cleartext; enables offline. |
| Tamper markers (`max_seen_iat`, `last_server_time`) | keyring | Harder to wipe than a JSON file. |
| `remember_me`, `last_email` | `soundboard_config.json` | Non-secret convenience only. **Never tokens here.** |

**New/edited client files:** `auth.py`, `auth_store.py`, `login_ui.py`, `api_client.py`, `license.py`, `billing.py` (new); `main.py` (gate), `gui.py` (`__init__` + gates + account chip + `_auth_tick`), `voice_fx.py`/`audio.py` (gate sites). **`requirements.txt`** += `requests`, `keyring`, `pynacl`. **`soundboard.spec`** hidden imports += `keyring.backends.Windows`, `win32ctypes.core`, `win32ctypes.core.ctypes`, `requests`, `nacl` (verify the libsodium DLL is bundled — test the *frozen* `.exe`).

---

## 10. Security & Threat Model

**Stance:** A PyInstaller bundle is a ZIP of `.pyc` + CPython; `pyinstxtractor` + a decompiler reconstructs source in minutes. **Treat the client as fully readable and patchable.** One rule governs everything:

> The server is the only thing you control. The client is enemy territory. Never put a secret, a trust decision, or an authority check on the client that you can put on the server instead.

| # | Threat | Real mitigation | Don't bother (theater) |
|---|---|---|---|
| T1 | Server-secret extraction from exe | Ship **zero** secrets: no DB creds, no Paddle secret, no Ed25519 **private** key, no service-role key. Client carries only the Supabase anon key (RLS-scoped) + the Ed25519 **public** key. | "Encrypting" an embedded key with another embedded key. |
| T2 | Token theft/replay | Short access JWT (~1h) + rotating revocable refresh token; `jti` + device binding; revoke on logout/abuse. | Long-lived bearer tokens "hidden" in the binary. |
| T3 | MITM / fake server | TLS 1.2+, `verify=True` (the default — **never** set `verify=False`). | Custom "encryption" over HTTP; leaf-cert pinning (breaks on rotation; attacker owns the box anyway). |
| T4 | Binary patching (`is_pro→True`) | **Accept it.** Make the cracked client useless for cloud features (sync/recording/account) which need a real server token a patch can't fake. | PyArmor as *primary* defense. |
| T5 | License-file tampering (`expiry:2099`) | Server-signed Ed25519 bundle; tampered field → signature fails → treated as Free. | Storing plaintext `is_pro:true` and trusting it. |
| T6/T7 | Account/license sharing | Device binding + concurrent-seat cap (3); server refuses to sign for a 4th device; impossible-travel soft-flag. | Making sharing *impossible*; over-aggressive HW fingerprints that false-positive on a GPU swap. |
| T8 | Refund/trial abuse | Card fingerprint via PSP, email normalization (strip `+tags`, block disposables), one trial per card, signup rate-limit per IP. | Email-only dedup (defeated by Gmail aliases instantly). |
| T9 | Brute-force / cred stuffing | Server-side rate limit + backoff + argon2id (GoTrue does this); HIBP breached-password check on signup. | Client-side throttling (attacker hits the API directly). |
| T10/T11 | Privilege escalation / data exfiltration | Refresh rotation w/ reuse detection; **RLS** so user A can never read user B's rows; per-user storage prefixes + short-TTL signed URLs. | Client-side "encryption" keyed by something also on the client. |

**Password hashing:** GoTrue owns it. The argon2id snippet (`time_cost=3, memory_cost=64MiB, parallelism=2`) is only relevant on the self-hosted-auth upgrade path.

**What NOT to build (explicit):** custom crypto / "secure channel" over HTTP; client-side encryption keyed locally; hardware DRM/dongles/kernel anti-tamper; aggressive multi-signal fingerprinting; leaf-cert pinning; anti-debug/VM-detection/packers; trying to make the 100%-local playback uncrackable. **Code-signing the exe: YES** — not anti-piracy, but it kills SmartScreen "Unknown Publisher" (which tanks install conversion) and prevents tampered-binary impersonation. **PyArmor: optional, low priority**, a cheap speed bump only.

**Privacy red line — call recordings capture non-consenting third parties.** **Default recordings to LOCAL-ONLY**; cloud backup is explicit opt-in with retention limits and one-click delete. This is your worst-headline risk, bigger than piracy, and the cheapest to neutralize.

---

## 11. Remote Control Plane — Auto-Update, Version Gating & Remote Lockout

### 11.1 Overview — one spine, three powers

The remote control plane is **not new infrastructure**. It is three operational powers that fall out of the entitlement spine you already built (§3, §8): the client trusts *only* server-signed, device-bound, Ed25519 evidence it verifies **offline** with an embedded **public** key, and it re-checks that evidence on the existing heartbeat (60 s local tick / 6–24 h online re-fetch, networking off the Tk thread, results marshaled via `root.after(0, …)`). Three things ride that exact mechanism:

| Power | What it does | Rides which spine primitive |
|---|---|---|
| **Auto-update** | Push fixes; converge the fleet to a new build | A **second** Ed25519-signed artifact (the *update manifest*) verified by the *same* embedded-pubkey machinery; downloaded on the existing heartbeat worker thread |
| **Version gating** | Force-update / drop-retire bad or old builds; staged rollout; instant rollback | `min_supported_version` (already seeded in `/health`), enriched + **signed**, evaluated like the entitlement time-policy |
| **Remote lockout** | Ban a user, disable a device, force-logout, kill a version, global kill | The signer **refusing to vouch** at `/entitlements` (already §4e "revocation") + the anonymous `/health` verdict channel |

**The one idea:** there is no parallel trust system, no second crypto stack, no second polling loop. *Update* answers "what code may this user run"; *entitlement* answers "what may this user do." Both are a signature you verify offline, both are re-checked on the same handshake, and **lockout is simply the absence of a fresh favorable signature**. Worst-case latency is therefore governed by the same `exp + grace` dial that already governs entitlement — except for **hardened mode** (§11.4), the one knob that buys fast lockdown for flagged accounts without punishing honest offline users.

**Honest stance (unchanged from §10):** a PyInstaller `.exe` is decompilable and patchable. None of this is DRM. A cracker patches the version check on his own box — accepted (T4). What this control plane reliably does is (a) **deliver fixes**, (b) **force-retire broken/old builds for the honest 99 %**, and (c) ensure an attacker who controls the **network or the CDN cannot turn auto-update into remote code execution** against honest users. That last boundary — defended by *dual signatures* — is the only security claim made here, and it's free given the spine.

> **PREREQUISITE — read before building any of this.** The current code (`main.py` lines 16–19) anchors the frozen exe's cwd to **two levels up from `dist\SoundBoard\`** and writes `soundboard_config.json`, `sounds\`, `images\`, and `debug.log` *there* — i.e. **into the same tree the exe lives in**, not a separate workspace. The exe is named **`SoundBoard.exe`** (`soundboard.spec` line 161/182). The atomic directory-swap updater below is **unbuildable on that layout** (a folder swap would orphan or destroy user config). **§11.10 "Prerequisite migration" must land first**: relocate the workspace to an explicit `%LOCALAPPDATA%\LocalSoundBoard\workspace`, move `debug.log` there, and keep the exe name canonical. Every path/name in this section assumes that migration is done.

---

### 11.2 Auto-update mechanism

**Decision: a custom Ed25519-signed-manifest updater, with a stub launcher + a separately-signed `updater.exe` relauncher. Reject Velopack / WinSparkle / MSIX / PyUpdater as the *mechanism*; use Inno Setup as the per-user *installer* artifact.**

Rationale in one breath: you already own an Ed25519 verify path, an embedded public key, a `requests` stack, a Supabase Storage/CDN bucket, an Authenticode CI step, and a `/health` handshake. A custom updater (~250 LOC) reuses *every one* of those. Every off-the-shelf option forces a **second** signing identity and a **second** update-server format that fights the "frozen exe + user-writable workspace" model. Velopack becomes attractive *only* once you'd otherwise reimplement deltas + a release server (noted as the scalable alternative).

#### The hard Windows problem: you cannot overwrite a running `.exe`

Windows holds an exclusive lock on the running image. We solve it with a **directory swap performed by a separate short-lived process**, plus a **stub launcher** that survives a mid-swap kill (see §11.8 for why the stub is non-negotiable):

```
%LOCALAPPDATA%\LocalSoundBoard\
  Launch.exe              <- STUB the Start-Menu shortcut points at; reconciles + launches.
                             NEVER swapped, so it always survives to self-heal.
  app\                    <- CURRENT build: SoundBoard.exe + _internal\  (the swap target)
  staging\<version>\      <- downloaded + DOUBLY verified, ready to promote
  updater.exe             <- standalone relauncher, separately Authenticode-signed
  update_state.json       <- swap-intent JOURNAL + crash-loop counter
  workspace\              <- USER DATA (config, sounds, images, recordings, debug.log)
                             — outside app\, NEVER in the swap blast radius
```

Code (`app\`) and data (`workspace\`) are now on **separate swap boundaries**, so a swap can never touch user data — the prerequisite migration is what makes this true.

#### Full vs delta, background vs prompted

- **Ship full ZIP updates for MVP.** Defer deltas. A one-folder PyInstaller build is ~40–90 MB; resumable via HTTP `Range`. **Phase 2 (only if bandwidth bites):** a *file-hash manifest* delta — re-download only files whose SHA-256 changed (the heavy `python313.dll`/`numpy`/`scipy` libs almost never do; ~90 % of the win). **Never** binary `bsdiff`.
- **Class decides UX.** *Optional* → silent background download on the heartbeat worker thread, then a **dismissible** "Update ready — restart to apply" banner (same banner system as the soft-grace banner). *Mandatory* → background download, then a **non-dismissible** modal. **Never auto-apply mid-session** — a soundboard may be live in a Discord call; apply only at a quiescent restart boundary (defined in §11.8).

#### Channels

`channel` ∈ `{stable, beta}` stored in `soundboard_config.json` (non-secret, default `stable`), exposed via a **visible Settings toggle** (not hidden JSON). Manifests are per-channel: `update/stable.json`, `update/beta.json`. **Leaving beta must downgrade to current stable** — a deliberate, allowed exception to the monotonic-version rule (§11.8), special-cased on channel switch so a stuck beta tester always has a road back.

#### Update manifest JSON (signed — same wire format as the entitlement bundle)

Static file at `https://<cdn>/update/<channel>.json`. Wire = `base64url(canonical_json(manifest)) + "." + base64url(ed25519_sig)`, where `canonical = json.dumps(obj, separators=(",",":"), sort_keys=True)` over UTF-8 — **byte-for-byte the §8 rule**. Signed in CI by the same Ed25519 key *family*, distinct `kid` (`u1`) so update-signing and entitlement-signing rotate independently while sharing the embedded-pubkey machinery.

```jsonc
{
  "v": 1,
  "kid": "u1",                          // update-signing key id (rotation: ship u1+u2)
  "channel": "stable",
  "generated_at": 1749340800,           // freshness anchor (see §11.8 downgrade defense)
  "not_before": 1749340800,             // reject manifests "predated" before this (anti-replay)
  "min_supported_version": "1.2.0",     // mirrors /health; below this => hard-block
  "latest_version": "1.3.0",
  "releases": [
    {
      "version": "1.3.0",
      "mandatory": false,
      "rollout": 25,                     // % of devices, bucketed by hash(device_id|version)
      "min_os_build": 19045,             // Win10 22H2+; null = no floor
      "artifact": {
        "kind": "zip",                   // "zip" (full) | "filelist" (Phase-2 delta)
        "url": "https://cdn/.../SoundBoard-1.3.0-win64.zip",
        "size": 71303168,
        "sha256": "9f2c…e0",             // MUST equal downloaded bytes
        "exe_subpath": "SoundBoard.exe", // <-- the REAL exe name
        "authenticode_subject": "CN=<Your Publisher>, O=<Your Org>"
      }
    }
  ]
}
```

#### Client updater code sketch

New `soundboard/updater.py`, called from the existing heartbeat worker tick. Reuses `license.py`'s verify primitive — **no new crypto**.

```python
# soundboard/updater.py  -- heartbeat WORKER thread; UI via root.after(0, ...)
import json, hashlib, os, sys, subprocess
from pathlib import Path
from packaging.version import Version          # add to requirements.txt AND spec hiddenimports
from soundboard import __version__ as CURRENT_VERSION
from soundboard.license import b64url, EMBEDDED_PUBKEYS  # reuse entitlement machinery
from nacl.signing import VerifyKey

UPDATE_PUBKEYS = {"u1": VerifyKey(bytes.fromhex("..."))}    # public => safe to ship
ROOT    = Path(os.environ["LOCALAPPDATA"]) / "LocalSoundBoard"
APP_DIR = ROOT / "app"; STAGING = ROOT / "staging"; UPDATER = ROOT / "updater.exe"
CANON   = lambda o: json.dumps(o, separators=(",", ":"), sort_keys=True).encode()

def _verify_manifest(blob: bytes) -> dict:
    body_b64, sig_b64 = blob.decode().split(".")
    obj = json.loads(b64url(body_b64))
    UPDATE_PUBKEYS[obj.get("kid", "u1")].verify(CANON(obj), b64url(sig_b64))  # raises if tampered
    return obj                                                               # trust ONLY after this

def _rollout_ok(device_id: str, version: str, pct: int) -> bool:
    h = hashlib.blake2b(f"{device_id}|{version}".encode(), digest_size=8).digest()
    return (int.from_bytes(h, "big") % 100) < pct          # deterministic, offline-consistent

def check(api, device_id: str, health: dict) -> dict | None:
    manifest = _verify_manifest(api.get_bytes(health["manifest_url"]))       # NEVER trust unsigned
    # AUTHORITATIVE gate wins over a possibly-stale static manifest (downgrade defense, §11.8):
    if manifest["generated_at"] < health["server_time"] - 86400:
        return None                                        # manifest too stale -> ignore, keep running
    forced_min = max(Version(health["min_supported_version"]),
                     Version(manifest["min_supported_version"]))
    blocked    = set(health.get("blocked_versions", []))
    too_old    = Version(CURRENT_VERSION) < forced_min or CURRENT_VERSION in blocked
    cands = [r for r in manifest["releases"]
             if Version(r["version"]) > Version(CURRENT_VERSION)
             and r["version"] not in blocked                # /health beats manifest, always
             and _rollout_ok(device_id, r["version"], r["rollout"])
             and (r.get("min_os_build") is None or _os_build() >= r["min_os_build"])]
    # GUARDRAIL: if forced but there is NO valid target to update to -> fail-OPEN, never brick (§11.10)
    if too_old and not any(Version(r["version"]) >= forced_min for r in manifest["releases"]):
        _audit_local("update.misconfig_no_target"); return None
    if not cands and not too_old:
        return None
    target = max(cands, key=lambda r: Version(r["version"]))
    return {"release": target,
            "mandatory": too_old or target.get("mandatory") or health.get("update_required", False)}

def download_and_stage(api, release: dict) -> Path:
    art = release["artifact"]; dest = STAGING / f'{release["version"]}.zip'
    dest.parent.mkdir(parents=True, exist_ok=True)
    _preflight_free_space(STAGING, art["size"] * 3)        # need ~3x: zip + extract + app.old
    api.download_with_resume(art["url"], dest)             # HTTP Range; resumable
    if _sha256(dest) != art["sha256"]:                     # integrity gate #1
        dest.unlink(missing_ok=True); raise UpdateError("hash mismatch — refusing to apply")
    out = STAGING / release["version"]; _safe_extract(dest, out)
    if not _authenticode_valid(out / art["exe_subpath"], art.get("authenticode_subject")):
        _rmtree(out); raise UpdateError("Authenticode verify failed — refusing to apply")  # gate #2
    return out

def apply_and_relaunch(staged: Path):
    subprocess.Popen([str(UPDATER), "--pid", str(os.getpid()), "--staged", str(staged),
                      "--app", str(APP_DIR), "--from", CURRENT_VERSION],
                     creationflags=subprocess.DETACHED_PROCESS)
    sys.exit(0)                                            # release the lock on the .exe
```

The standalone **`updater.exe`** does the lock-free swap and writes a **swap-intent journal** so a mid-swap kill is recoverable (§11.8):

```python
# updater_main.py -> updater.exe ; NO Tk, NO audio, minimal deps, separately signed
def main():
    a = _parse_args()                       # --pid --staged --app --from
    _wait_for_exit(a.pid, timeout=30)       # main app releases the lock; releases held PTT first
    if not _authenticode_valid(Path(a.staged) / "SoundBoard.exe"):
        sys.exit(2)                         # re-verify; never swap an unsigned/altered payload
    ts = int(time.time()); old = Path(f"{a.app}.old.{ts}")
    _journal_write({"intent": "swap", "app": a.app, "old": str(old), "staged": a.staged})  # BEFORE
    try:
        os.replace(a.app, old)              # rename running-app dir aside (atomic, same vol)
        os.replace(a.staged, a.app)         # promote staged -> app (ATOMIC)
    except OSError:
        if old.exists() and not Path(a.app).exists(): os.replace(old, a.app)  # in-proc rollback
        _relaunch(a.app); sys.exit(3)
    _journal_clear()                        # only after BOTH renames succeed
    _relaunch(a.app, updated_from=a.frm)    # start SoundBoard.exe --updated-from <old>
```

#### Two signatures, both required (the only security claim)

1. **Manifest Ed25519** (your key, embedded pubkey, distinct `kid`) — proves *you* authored "version X at URL Y with hash H." Defeats CDN/MITM tampering, verified **offline**.
2. **Artifact SHA-256** in the signed manifest — binds the manifest to exact bytes.
3. **Authenticode** on the new `SoundBoard.exe` / `updater.exe` (Azure Trusted Signing, RFC-3161) — OS publisher trust, re-verified after extraction *and* again inside `updater.exe` before the rename.

**Hard rule:** if manifest-sig **or** SHA-256 **or** Authenticode fails → discard, keep running the current build, log `update.verify_failed`. Always `verify=True` TLS. Never a secret in the updater or manifest.

---

### 11.3 Version lifecycle — force-update, drop, staged rollout, rollback

**Promote `/health` into a richer, *signed* `GET /functions/v1/handshake`** that returns the full release picture *for this device*; keep `/health` as a dumb UptimeRobot liveness probe. The version-gating fields are security-relevant — an unsigned `min_supported_version` lets a MITM either brick the fleet (set `99.0.0`) or defeat the kill-switch (set `0.0.0`). Signing costs ~5 lines and reuses the entitlement signer (`kid` k1/k2, canonical serialization, 1 h `exp`, 7-day stale-cache grace).

**Handshake request** (launch + each online heartbeat; **auth optional** so version gating reaches anonymous Free users too):

```jsonc
// GET /functions/v1/handshake  (params + X-Device-Hash header)
{ "app_version": "1.2.3", "channel": "stable",
  "device_id": "dev_a17c…",            // SAME value as /entitlements
  "os": "Windows-11-10.0.22631", "frozen": true }   // frozen=false (source) skips force-update
```

**Handshake response** (Ed25519-signed envelope, same shape as the entitlement bundle):

```jsonc
{
  "v": 1, "kid": "k1", "iat": 1748736000, "exp": 1748739600,   // 1h: floor changes propagate fast
  "device_id": "dev_a17c…",                                    // echoed => binds response to device
  "channel": "stable",
  "min_supported_version": "1.1.0",     // HARD floor: client < this => Update-Required screen
  "blocked_versions": ["1.2.2"],        // instant per-build kill (a bad release)
  "latest_version": "1.3.0",
  "update_required": false,             // global force-update flip
  "rollout_bucket_max": 100,            // 0..999 threshold; device self-selects by hashing device_id
  "manifest_url": "https://cdn/.../update/stable.json",
  "channel_disabled": false, "disabled_message": null,
  "server_time": 1748736000             // feeds clock-tamper baseline AND manifest-freshness check
}
```

#### Force-update / drop a version (the hard kill)

```
client_version < min_supported_version  OR  client_version ∈ blocked_versions   =>   HARD BLOCK
```

Evaluated at launch (pre-gate in `main.py`, alongside the auth gate) and on every online heartbeat. **To drop/retire a build:** raise `min_supported` above it or add it to `blocked_versions` — every old client hard-blocks on next handshake into an "Update Required" screen with no path into the soundboard. **`frozen == false` skips force-update** (never lock yourself out of `python main.py`). **Unparseable version → treated as oldest → forced** (correct: a spoofed version is ancient). **Fail-open** on network/signature failure past the 7-day stale-cache grace — the gate is support tooling, not DRM, and must never brick a paying user on a transient outage.

#### Staged rollout — deterministic, offline-consistent, stateless

The server ships only the **threshold**; the client computes its own bucket from `hash(device_id|version)` and compares. Same device + same version is *always* consistently in or out, even reading a cached handshake offline — no flapping, works for anonymous users, zero server-side per-device state.

```python
def device_bucket(device_id, version):
    h = hashlib.blake2b(f"{device_id}|{version}".encode(), digest_size=8).digest()
    return int.from_bytes(h, "big") % 1000          # stable 0..999
def in_rollout(hs):
    return device_bucket(hs["device_id"], hs["update"]["version"]) <= hs["rollout_bucket_max"]
```

**Ramp ladder:** `1% → 10% → 50% → 100%` (`rollout_permille` 10 → 100 → 500 → 1000). Soak each step watching the success-rate metric (§11.10). **Instant abort:** set `rollout_permille = 0` → every device computes `bucket ≤ -1` → false → no *new* device pulls it.

> **Canary ring vs random %.** Use the **beta channel** as a *stable, opt-in* "ring 0" (you + known testers, always first). Use the per-version reshuffle only for the *stable* channel's gradual percentage. They are different tools — don't make your canaries random strangers each release. Independently, `min_supported_version` is the **laggard pull**: even if a stable ramp stalls at 50 %, raising the floor eventually drags stuck devices forward.

#### Rollback — flip server state, zero client push

A bad release is contained by starting at 5–10 % and yanked **entirely server-side**:

| Severity | Lever | DB change | Client effect |
|---|---|---|---|
| Bad, not widespread | **Abort** | `rollout_permille = 0` | No new device pulls it |
| Want everyone on prev good | **Repoint** | `latest_version = <prev good>`, bad row `status='yanked'` | New handshakes advertise the old build; not-yet-updated devices never see the bad one |
| Bad build actively harmful & installed | **Raise floor past it** | `min_supported = <good>` + ship fixed `<good+1>` | Devices on the bad build now `< min_supported` → force-updated to the fix |

Each is a single-row `UPDATE` on `app.release_channel`, propagating within one heartbeat (≤ next launch). Every flip writes `app.audit_log` (`release.repoint` / `release.floor_raised` / `release.yanked`).

---

### 11.4 Remote lockout / kill-switch

Lockout = **the server declines to vouch**, expressed two ways: (1) the signer at `/entitlements` **refuses / downgrades** the bundle (already §4e), and (2) `/health` + `/handshake` return a **verdict** the anonymous channel can enforce even for users who never call `/entitlements`. Latency is governed by `exp + grace` — except **hardened mode**, the fast-lockdown override.

#### Granularity matrix

| Granularity | Set by (admin RPC) | Propagated via | Worst-case latency (normal) | Hardened |
|---|---|---|---|---|
| **Global kill** | `global_killswitch(on,reason)` | `/health.global_kill` **+** signer refuses | seconds online; ≤ `exp+grace` offline | ≤ next heartbeat |
| **Per-version** | `disable_version(v,…)` | `/health.blocked_versions` + `min_supported` | seconds online; ≤ exp+grace offline | force-update is online-by-nature |
| **Per-user ban** | `ban_user(u,reason,hard)` | signer refuses / signs `disabled` | ≤ 6–24 h online; ≤ exp+grace offline | **≤ 24 h** |
| **Per-device revoke** | `revoke_device(dev,…)` | signer refuses for that `device_id` + refresh-family revoke | ≤ 6–24 h online; ≤ exp+grace offline | **≤ 24 h** |
| **Force-logout** | `force_logout_all(u,…)` | GoTrue refuses refresh + `sessions_valid_after` watermark in bundle | next refresh; entitlement still graces | pair with ban/revoke for fast Pro-off |

> **Critical correction (common design mistake):** **force-logout alone does NOT remove Pro offline.** Killing the refresh token only stops *new* bundles; the *cached* bundle keeps Pro alive until `exp + grace`. To disable Pro fast you must combine force-logout with a ban/revoke (signer refuses) **and** move the account to **hardened mode** so the cached bundle expires soon.

#### The one knob: TTL-vs-grace

```
   Offline UX  ◄─────────────────────────────────────────►  Lockdown speed
  long grace(30d)     7d exp/14d grace (DEFAULT)     24h hardened      online-only
  Lifetime users      normal users                   flagged accounts   enterprise
  locks ≤ exp+grace   locks ≤ ~21d offline            locks ≤ 24h        minutes
```

**Decisive defaults:** normal users keep **7-day exp + 14-day grace** (Lifetime 30 d). Flagged / chargeback / abusive accounts auto-promote to **`lock_mode='hardened'`** → 24 h TTL, `grace_days:0`, `require_online_h:24` → locks within **24 h**, instantly the moment they're online. **Do not** globally shorten TTL to get fast lockdown — that punishes every honest offline user; per-account hardened mode is strictly better.

**Chargeback policy (legal nuance):** chargeback ⇒ **hardened + drop to Free** (keep the free app usable), *never* hard-lock — hard-locking a legitimate dispute can be an unfair commercial practice in the EU/UK. Reserve `ban_user(hard:=true)` (signer always serves a `disabled` bundle → hard screen) for actual ToS/abuse with `audit_log` evidence.

**Mid-session rule:** if a heartbeat *while running* finds `forced`/banned, **do not yank audio mid-playback**. Release held PTT, finish the active stream, then gate the *next* action — exactly like the Pro gates. The "Update Required" / "Locked" modal blocks new actions but lets a live sound finish.

---

### 11.5 New database tables (additive to §5)

All in schema `app.*`, RLS-locked, service-role-write only; **clients never read these directly** — they only ever see the signed handshake/manifest. `app.audit_log`, `app.profiles.flagged`, `app.devices.revoked_at`, `app.license_tokens.revoked_at` already exist and are reused verbatim.

```sql
-- Immutable-ish catalog: every build that ever shipped (audit + rollback targets).
CREATE TABLE app.app_releases (
    version          text NOT NULL,                       -- SemVer, matches __version__
    channel          text NOT NULL DEFAULT 'stable' CHECK (channel IN ('stable','beta')),
    artifact_url     text NOT NULL,
    artifact_sha256  text NOT NULL,                        -- updater verifies before run
    artifact_size    bigint NOT NULL,
    signed_by_cert   text,                                 -- Authenticode thumbprint (provenance)
    release_notes    text,
    status           text NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft','rolling','live','superseded','yanked')),
    rollout_permille int  NOT NULL DEFAULT 0 CHECK (rollout_permille BETWEEN 0 AND 1000),
    min_os_build     int,
    created_at       timestamptz NOT NULL DEFAULT now(),
    published_at     timestamptz,
    PRIMARY KEY (channel, version)
);
CREATE INDEX releases_channel_status_idx ON app.app_releases(channel, status);

-- The 1-row-per-channel MUTABLE control cursor — the lever you flip during an incident.
CREATE TABLE app.release_channel (
    channel           text PRIMARY KEY CHECK (channel IN ('stable','beta')),
    latest_version    text NOT NULL,
    min_supported     text NOT NULL,                       -- HARD floor (force-update / drop)
    rollout_permille  int  NOT NULL DEFAULT 1000 CHECK (rollout_permille BETWEEN 0 AND 1000),
    blocked_versions  text[] NOT NULL DEFAULT '{}',        -- instant per-build kill
    channel_disabled  boolean NOT NULL DEFAULT false,      -- panic: hard-stop whole channel
    disabled_message  text,
    updated_by        uuid,
    updated_at        timestamptz NOT NULL DEFAULT now()
);
INSERT INTO app.release_channel(channel,latest_version,min_supported) VALUES
  ('stable','1.2.3','1.0.0'), ('beta','1.2.3','1.0.0');

-- Global emergency switch (single-row guard).
CREATE TABLE app.control_plane (
    id                 int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    global_kill        boolean NOT NULL DEFAULT false,
    global_kill_reason text,
    updated_by         uuid,
    updated_at         timestamptz NOT NULL DEFAULT now()
);
INSERT INTO app.control_plane(id) VALUES (1) ON CONFLICT DO NOTHING;

-- The fast-lockdown knob + force-logout watermark, added to the existing profiles table.
ALTER TABLE app.profiles
    ADD COLUMN lock_mode            text NOT NULL DEFAULT 'normal'
        CHECK (lock_mode IN ('normal','hardened','banned')),
    ADD COLUMN flagged_reason       text,
    ADD COLUMN flagged_at           timestamptz,
    ADD COLUMN sessions_valid_after timestamptz NOT NULL DEFAULT '1970-01-01';  -- bump = force-logout
```

> **`ALT` (scalable):** when you have geo/segment A-B testing, split rollout into `app.release_rollout (channel, version, segment, permille, targeting jsonb)`. For solo-dev pre-scale, the single per-channel cursor is correct — don't build the segmentation engine before you have segments.

#### Admin RPCs (all `SECURITY DEFINER`, admin-role-only, idempotent, audit-logged, with inverses)

```sql
-- BAN (refund/abuse): hard-lock OR soft-harden + revoke devices/tokens + force-logout.
CREATE FUNCTION app.ban_user(p_user uuid, p_reason text, p_hard boolean DEFAULT true) ...
-- REVOKE one device (free a seat / lost laptop).
CREATE FUNCTION app.revoke_device(p_device uuid, p_reason text) ...
-- FORCE LOGOUT everywhere (bump sessions_valid_after; revoke refresh family).
CREATE FUNCTION app.force_logout_all(p_user uuid, p_reason text) ...
-- DISABLE a version (force-update). REJECTS a floor above max live version (anti-brick, §11.10).
CREATE FUNCTION app.disable_version(p_version text, p_reason text, p_min_replacement text) ...
-- GLOBAL kill (emergency). Returns affected-device COUNT before acting (blast-radius preview).
CREATE FUNCTION app.global_killswitch(p_on boolean, p_reason text) ...
-- HARDEN without banning (suspected sharing → 24h leash, Pro stays).
CREATE FUNCTION app.set_lock_mode(p_user uuid, p_mode text, p_reason text) ...
```

Every RPC has a trivial inverse (clear `flagged`, set `lock_mode='normal'`, `status='live'`, `global_kill=false`). **Always pair ban/un-ban** — a tool that can ban but not un-ban bricks a paying customer at 2 am. Kill RPCs return the **blast-radius count** (active devices from `devices.last_seen`) so a solo dev sees the impact before confirming.

---

### 11.6 New / changed API endpoints

| Method & Path | Auth | Purpose | CDN-cacheable? |
|---|---|---|---|
| **`GET /functions/v1/handshake`** *(new, supersedes thin `/health`)* | none/access | Signed per-device release picture: `min_supported`, `blocked_versions`, `rollout_bucket_max`, `manifest_url`, `global_kill`, `channel_disabled`, `server_time` | **No** — `Cache-Control: no-store` (≤60 s) so kill/un-kill propagates fast (§11.8) |
| `GET /functions/v1/health` *(kept, demoted)* | none | Dumb liveness for UptimeRobot | yes |
| **`GET /<cdn>/update/<channel>.json`** *(new, static)* | none | Signed update manifest (what/where to download) | **Yes** — survives Edge Function downtime |
| **`POST /functions/v1/telemetry/version`** *(new, anonymous)* | none | Fire-and-forget `{device_id, app_version, channel, outcome}` so rollout health covers **Free** users | yes (write-sink) |
| **`POST /functions/v1/admin/releases`** *(new)* | service | CI publish step inserts an `app_releases` row | n/a |
| Admin RPCs (§11.5) | service | `ban_user` / `revoke_device` / `force_logout_all` / `disable_version` / `global_killswitch` / `set_lock_mode` | n/a |

> **Reconcile the cache tension:** the *artifact manifest* is CDN-cached and survives the backend being down; the *authoritative gate* (`/handshake`) is **never** aggressively cached, or you cannot un-kill fast. When the manifest and a fresher `/handshake` disagree, `/handshake` always wins (§11.2 `check()`).

---

### 11.7 ASCII flows

**(a) Launch handshake → update / force-update decision**

```
launch / heartbeat online tick (WORKER thread)
   |
   +-> GET /functions/v1/handshake  (app_version, channel, device_id, frozen)
   |      verify Ed25519 (EMBEDDED pubkey) --fail--> use cached signed handshake (<=7d)
   |                                                  else FAIL-OPEN -> run (never brick)
   v
 channel_disabled? --yes--> [Channel Disabled] hard screen   (whole-version panic)
   |no
 global_kill?       --yes--> [App Disabled] hard screen
   |no
 frozen == false?   --yes--> run (dev: skip force-update)
   |no
 app_version < min_supported  OR  in blocked_versions?
   |                                   yes--> NO valid target? --yes--> FAIL-OPEN, log misconfig
   |                                          |no
   |                                          v
   |                                    [Update Required] hard modal -> force-update path
   |no
 newer release AND device in rollout bucket? --yes--> dismissible "Update available" banner
   |no
   v
 app runs normally
```

**(b) Auto-update download + verify + swap + relaunch**

```
 running SoundBoard.exe          updater.exe (child)             disk
 --------------------            -------------------             ----
 download -> staging\1.3.0\                                      app\  (v1.2.3, LOCKED)
 sha256 == manifest?  ---------------------------------------->  integrity gate #1
 authenticode OK?                                                integrity gate #2
 release held PTT / unhook                                       (avoid stuck-key on handoff)
 spawn updater.exe(pid,...) ----> wait pid exit (<=30s)
 sys.exit(0)            (app UNLOCKS)
                                  re-verify staging\1.3.0\authenticode
                                  JOURNAL: write swap-intent  ----> update_state.json
                                  rename app\ -> app.old.<ts>  (atomic)
                                  rename staging\1.3.0 -> app\ (ATOMIC)
                                  JOURNAL: clear
                                  relaunch app\SoundBoard.exe --updated-from 1.2.3
 new app starts ---------------------------------------------->  app\  (v1.3.0)
   healthy checkpoint reached (window + stream-open + /health + entitlement)?
     yes -> mark_healthy(); GC app.old.*       no/crash-loop -> maybe_revert() restores app.old.*
 -------------------------------------------------------------------------------------------
 If updater.exe is KILLED mid-swap: next click runs Launch.exe (stub) -> reads dangling
   journal -> restores app.old.<ts> -> app\  -> launches.  (Stub is never swapped -> survives.)
```

**(c) Remote kill propagation**

```
 admin RPC (ban_user / disable_version / global_killswitch)
   |  writes flag + app.audit_log('*.set'), returns blast-radius count
   v
 Postgres: profiles.lock_mode / release_channel.* / control_plane.global_kill
   |                                   |
   | read by /entitlements signer      | read by /handshake (+ anonymous /health verdict)
   v                                   v
 signer REFUSES or signs {disabled}   handshake returns global_kill / blocked / min_supported
   |                                   |
   +-----------------+-----------------+
                     v
        client heartbeat (60s local / 6-24h online, root.after(0,...))
                     v
   normal: locked at next exp+grace  |  hardened: 24h TTL, grace 0 -> locks <=24h
   online: enforced at next heartbeat (seconds-minutes)
                     v
   client posts outcome -> /telemetry/version ('locked'|'updated'|'force_update_shown')
                          -> audit_log('*.served') closes the propagation loop
```

---

### 11.8 Security & honest realism

| Concern | What actually defends it | Honest limit |
|---|---|---|
| **MITM / malicious CDN → RCE** | Manifest Ed25519 (embedded pubkey) **+** SHA-256 **+** Authenticode, all three required pre-swap and re-checked in `updater.exe`. | The *only* real security boundary here — and it holds. |
| **Downgrade / replay of an old *valid* manifest** (re-serves a yanked bad build) | `/handshake` (fresh, signed, 1 h exp) **beats** the static manifest; reject manifests with `generated_at` older than `server_time − 24 h`; honor `blocked_versions` even if the manifest still advertises the build; `not_before` blocks predated manifests. | A stolen update key still verifies on already-installed exes — see key-compromise row. |
| **Key compromise (leaked Ed25519)** | Forced rebuild signed by `u2` + bump `min_supported_version` via `/health` to hard-block all builds carrying only the leaked `u1` key. | You cannot revoke an embedded pubkey without shipping a new build; state this plainly. |
| **`updater.exe` killed mid-swap → bricked install** | **Stub `Launch.exe`** (never swapped) reconciles on every launch via the **swap-intent journal**; a dangling journal triggers restore of `app.old.<ts>`. Crash-loop watchdog (`update_state.json`, N failed launches) auto-reverts. | Without the stub + journal this *is* unrecoverable — hence both are mandatory, not optional. |
| **AV / SmartScreen / EDR** | A self-rewriting exe that downloads+spawns another exe is a textbook dropper signature; **Azure Trusted Signing has zero initial reputation** and every auto-update is a new hash. Mitigate: pre-submit each signed build to Microsoft; **set `upx=False`** (UPX inflates false positives); keep DLL hashes stable (Phase-2 file-delta preserves their reputation); **provide a browser-download fallback** when the silent swap is blocked. | A **mandatory** update that trips SmartScreen needs a click a *silent* updater can't provide — the force-update UX must fall back to opening the signed installer in the browser. Consider folding the swap into an Inno `/SILENT` installer run (less alarming to AV than a bespoke `updater.exe`). |
| **AppLocker / WDAC fleets** | Per-user `%LOCALAPPDATA%` dodges UAC but is *exactly* what "no exec from user-writable paths" policies block. Detect write/exec failure → clear "your administrator must install this" message + reachable per-machine MSI fallback. | On a locked-down corporate image, auto-update simply can't run; fail loudly, don't loop silently. |
| **Cloned-image / VDI `device_id` collision** | MachineGuid collides across golden images → shared cohort *and* shared seat/revoke. Mix a per-install random salt (persisted in `workspace\`) into the **cohort** hash so rollout is per-install; keep entitlement device-binding on MachineGuid (separate concern). | One revoke still revokes all clones for *entitlement* — acceptable, documented. |
| **Patched client (`is_pro→True`, stub `/handshake`)** | **Accept it (T4).** The cracked client still cannot fake cloud Pro (sync/recording upload need a real server token) and cannot forge a signed artifact. | This is not DRM. It protects the honest majority and your support burden — its actual job. |
| **Clock rollback extends grace / cached kill** | Kill-relevant evaluations use server-anchored `_trusted_now()` (cap at `last_server_time + 30 d`), not raw wall clock. | A fully-offline banned user keeps a *normal* bundle up to exp+grace; hardened mode (24 h) is the fix for accounts you care about. |

**"Finish current playback" defined precisely:** a soundboard is always potentially about to play, so the quiescent boundary is **"no active output stream AND app unfocused ≥ N s"** (optional updates), or a **hard countdown** ("updating in 60 s") for a true-mandatory security fix. The **healthy checkpoint** the watchdog requires before GC-ing `app.old.*` is: window built **AND PortAudio output stream opened successfully** (a device-open failure counts as unhealthy → revert) **AND** one successful `/handshake` **AND** entitlement verified. Held PTT is released before process handoff so a swap never leaves a stuck key/audio gate.

---

### 11.9 Free (no-account) users

This is the **majority** of the installed base, and the control plane reaches them only partially — state it plainly:

- **No `/entitlements` ⇒ no per-user lever.** A Free user has no JWT, so the signer-refusal channel (ban, device-revoke, force-logout) **does not apply to them**. For Free users the **only** levers are the anonymous channels: **version-kill** (`blocked_versions` / `min_supported`) and **global-kill**. You **cannot ban or revoke an individual Free user** — accept and document this.
- **Updates work anonymously.** `/handshake` and the static manifest are reachable without auth, so Free users force-update and auto-update normally.
- **Telemetry must be anonymous too.** `/handshake` stamps `devices.app_version` only when a JWT is present → you'd be blind to Free-user versions (most of the fleet). The **anonymous `POST /telemetry/version`** ping (`device_id + app_version + channel + outcome`, fire-and-forget) is what makes "is the rollout healthy?" answerable for the whole fleet, not just payers.
- **Offline-forever Free user** never gets force-updated and never gets killed. Acceptable; the honest coverage statement is: **the kill-switch covers *online* users.**

---

### 11.10 Ops — publish, watch, recover

**Prerequisite migration (do FIRST, before any updater code):**
1. Keep the exe name canonical (`SoundBoard.exe`) everywhere — manifest `exe_subpath`, `updater.exe` swap, kill screens — *or* rename in `soundboard.spec` (`name=`) and update all references. Pick one; today drafts and spec disagree.
2. Re-anchor the workspace in `main.py` to an explicit `%LOCALAPPDATA%\LocalSoundBoard\workspace` created on first run — **not** "two levels up from the exe." Move `debug.log` there (and switch to `RotatingFileHandler`). Until this lands, the directory-swap model is unbuildable and `update_state.json` would split across old/new trees.
3. Add `pynacl`, `requests`, `packaging` to **`requirements.txt`** *and* `soundboard.spec` `hiddenimports` (and bundle the libsodium DLL); add a **single-instance named mutex** (already advisable for a hotkey app, required for the updater). **Smoke-test the *frozen* exe in CI** — `deploy.bat` currently only checks the file exists, never launches it, so a missing `nacl`/`packaging` import would surface only in production.

**Publish a release (CI):** `bump_version.py` → tag → GitHub Actions builds via `soundboard.spec` → **launch-smoke-test the frozen exe** → Azure Trusted Signing (`/tr` RFC-3161) → SHA-256 the *signed* zip → upload to CDN (immutable path) → `POST /admin/releases` (`status='draft'`, `rollout_permille=0`) → CI regenerates + signs `update/<channel>.json`. Nobody pulls it yet (rollout 0).

**Watch a rollout's health:** emit `update.applied`, `update.healthy`, `update.reverted`, `update.verify_failed`, `update.download_failed` (with `from`/`to` version) so you have a real **denominator** — "5 % reverted" is computable only with a success counter. **Primary dashboard tile = fleet version distribution** (from the anonymous telemetry ping, covering Free users). Ramp `1% → 10% → 50% → 100%`, soaking each step. **Automatic halt:** a tiny `pg_cron` rule — if `(reverted + verify_failed) / applied` for the rolling version exceeds X % over a window, auto-set `rollout_permille = 0` and **page your phone**. This is the difference between "10 % saw a bad build" and "100 % did" while you sleep.

**Recover from a bad `min_version` / bad kill-switch (the self-inflicted-brick footgun):**
- **Client guardrail (the nuke-defuser):** if `min_supported_version > latest available version`, the client treats it as server misconfiguration and **runs anyway** (logs `update.misconfig_no_target`) — *never* hard-blocks into a dead end with no valid target. This single rule prevents a fat-fingered floor from bricking the fleet.
- **Server guardrail:** `disable_version()` / floor-raise RPCs **reject** a floor higher than the max `status='live'` version — the DB refuses to brick.
- **Fast un-kill:** because `/handshake` is `no-store` (≤60 s, never aggressively CDN-cached) and its `exp` is 1 h, flipping `global_kill=false` / lowering the floor recovers online users in **seconds-to-minutes**. Keep the *floor* fields short-lived even if the rest of the bundle is long-lived, so a bad signed floor can't linger in cache.
- **Blast-radius preview + always-pair-the-inverse:** every kill RPC returns the active-device count before acting; every kill has a tested un-kill. A solo dev never fires a fleet-wide lever blind.

---

---

## 12. Installer & Virtual-Microphone Setup

### 12.1 Overview & the two hard truths

A "virtual mic Discord can select" is not a user-mode trick. Two truths govern every decision below, and the design states them up front so nothing downstream pretends otherwise.

**Hard truth #1 — it is a kernel-mode driver and needs admin once.**
A capture endpoint other apps (Discord) can choose is a kernel WDM/PortCls audio miniport publishing into MMDevAPI. Installing it requires an **elevated token** and a write to the protected DriverStore (`pnputil /add-driver … /install`). There is **no per-user / no-admin escape hatch** — Microsoft removed them all. This is the one place the otherwise frictionless model *must* take a UAC prompt. It happens **at most once per machine**, owned by the installer (and, for repair, by a separate signed helper). The self-updating app and the §11 updater **never** touch the driver or admin again.

**Hard truth #2 — licensing and signing are separate, non-negotiable gates.**
- **Signing:** a kernel driver must be **Microsoft-signed** to load on retail Win10/11. Your **Azure Trusted Signing cert does NOT qualify** for this — it is Authenticode for user-mode only. A driver needs an **EV cert + Microsoft Partner Center (Hardware Program) attestation/WHQL**, a *second, separate* signing pipeline. Self-signed kernel drivers do not load.
- **Licensing:** VB-CABLE and VAC are donationware/commercial. You generally cannot silently bundle them without permission. **The good news (verified):** VB-Audio's public licensing page **explicitly permits embedding base VB-CABLE in your installer with silent installation**, under the donationware model, provided you (a) keep VB-CABLE identifiable as a VB-Audio product the user could donate to, and (b) carry the attribution strings. So we **can** bundle — legally — and should.

Two honesty caveats the design carries everywhere:
- **"Silent" is not invisible.** Even with VB-CABLE's `-i -h` switches, Windows shows a **"install this device software? Publisher: VB-Audio"** trust dialog on first install of that publisher. We pre-warn the user; we never claim a zero-click install.
- **A 2026 enforcement wave overlaps our shipping window.** Microsoft is removing default trust for legacy **cross-signed** kernel drivers (evaluation **April 2026**, then enforcement) on Win11 24H2+. This makes the "build-your-own driver, attestation is easy" story *worse* over time (budget full **WHCP/HLK** for retail, not breezy attestation) and is a *named runtime risk* for any third-party cable too (see 12.6 / 12.8).

### 12.2 Virtual-audio-driver decision

**Recommended path (ship now): bundle base VB-CABLE, silent (`-i -h`), detect-first, with a guided-download fallback.**

| | MVP — ship now | Scalable / branded — earn into it |
|---|---|---|
| **Choice** | **License & bundle base VB-CABLE** (donationware terms, silent install), gated behind detect-first | **Build "LocalSoundBoard Virtual Mic"** from Microsoft's SYSVAD / Simple Audio Sample |
| **Why** | $0–low upfront, legally explicit silent-bundle permission, a real Microsoft-signed driver shipped by VB-Audio, no kernel work, no per-Windows maintenance tax | Branded name in Discord's dropdown, clean uninstall we own, no third-party/donationware string |
| **Cost** | Attribution + a goodwill donation (VB-Audio cite ~$500–$2,000) for a paper trail | EV cert + Partner Center + **full WHCP/HLK** + perpetual per-Windows-release + ARM64 second binary |

**Why not the alternatives:** **VAC** is rejected — its free tiers forbid commercial bundling; there's no scenario where it beats VB-CABLE for us. **Build-your-own** is the single largest cost/effort/risk item in the entire product (a bad driver build BSODs the user's machine vs. a bad app build crashing one process) — it must **never** be on the MVP critical path. **Guided-download-only** is kept solely as the *fallback* when the bundled silent install fails (locked-down PC, AV quarantine, conflicting cable).

**The build-your-own tripwire — build Path C only if ALL are true:**
1. VB-Audio (and VAC) refuse a workable redistribution/silent-install license, **AND**
2. You have *data* (12.4 telemetry) that the cable step measurably kills conversion, **AND**
3. You can absorb a recurring **0.5–2 dev-weeks per Windows feature update** + WHCP calendar latency without blocking releases, **AND**
4. You hold an EV cert + Partner Center account and have shipped an attestation/WHCP driver before, **AND**
5. The branded dropdown entry / custom topology is a genuine differentiator.
If even one is false, stay on the bundle path.

**Detection of an existing device (one shared detector).** Today the repo uses a loose substring match (`"cable" in name or "virtual" in name`, `gui.py:2872`) that both over-matches ("Virtual Desktop Audio", VoiceMeeter aux) and under-verifies (never checks the *capture* side Discord picks). We replace it with **one** ranked, paired detector in `soundboard/virtual_device.py`, called by the wizard **and** by `gui.py` (kill the three drifting copies):

```python
# soundboard/virtual_device.py  — the ONE detector. gui.py + wizard both call this.
import re, sounddevice as sd
from dataclasses import dataclass

_CABLE_TABLE = [   # (output_regex, exact-ish Discord input name, rank; 0 = preferred)
    (r"\bcable input\b",                 "CABLE Output",                 0),  # VB-CABLE
    (r"cable in 16ch",                   "Cable Out 16ch",               1),
    (r"voicemeeter input\b",             "VoiceMeeter Output",           2),
    (r"voicemeeter aux input",           "VoiceMeeter Aux Output",       2),
    (r"vac.*line|virtual audio cable",   "Line 1 (Virtual Audio Cable)", 3),
]

@dataclass
class CableMatch:
    out_index: int; out_name: str; discord_input: str
    rank: int; default_sr: int; capture_present: bool

def detect_cables() -> list[CableMatch]:
    devs = sd.query_devices()
    capture = {d["name"].lower() for d in devs if d["max_input_channels"] > 0}
    out = []
    for i, d in enumerate(devs):
        if d["max_output_channels"] <= 0: continue
        low = d["name"].lower()
        for rx, din, rank in _CABLE_TABLE:
            if re.search(rx, low):
                cap = any(din.lower() in cn or cn in din.lower() for cn in capture)
                out.append(CableMatch(i, d["name"], din, rank, int(d["default_samplerate"]), cap))
                break
    out.sort(key=lambda m: (m.rank, not m.capture_present, m.out_index))
    return out

def detect_cable():
    c = detect_cables(); return c[0] if c else None
```

The detector reports **both** sides: an output endpoint we route *into* (`CABLE Input`) **and** whether the matching **capture** endpoint (`CABLE Output`) actually enumerates — because routing to a cable whose mic side is disabled in `mmsys.cpl` is useless to Discord. It also surfaces `default_samplerate` so the wizard can pre-flight the 48 kHz contract (12.4 / 12.8).

**Install / uninstall via pnputil (and the ownership marker).** Install (bundle path) is VB-Audio's own elevated installer; for a future own-driver it is `pnputil /add-driver LSBVirtualMic.inf /install`. Critically, we **stamp ownership** in HKLM the instant our driver leg succeeds so install is idempotent and uninstall is *safe*:

```
HKLM\SOFTWARE\LocalSoundBoard\Driver
   InstalledByUs = 1   OemInfName = oem42.inf   DriverVersion = 1.0.0.0   RefCount = 1
```

Skip logic: marker present + driver present → **no-op** (re-run/repair); endpoint present but **no marker** → a *foreign* cable (user's own VB-CABLE / VoiceMeeter) → **skip install and NEVER remove on uninstall**; neither present → install, then write the marker. The **uninstall driver-removal checkbox defaults OFF**; and the *correct* removal for bundled VB-CABLE is its own uninstaller `VBCABLE_Setup_x64.exe -u -h`, **not** raw `pnputil /delete-driver` (which orphans VB-Audio's services/registry).

**Branding / naming.** Bundle path: you **cannot** rename it — Discord shows **"CABLE Output (VB-Audio Virtual Cable)."** The wizard shows the user that literal string. Own-driver path brands the INF so Discord shows **"LocalSoundBoard Virtual Mic"** — the entire point of building it.

### 12.3 Installer architecture

**Toolchain decision: Inno Setup 6.x (Unicode), single signed `Setup.exe`.** Not MSI. The deal-breaker is coexistence with §11: the updater **rewrites files inside the install dir**, which MSI treats as drift (repair/patch reconciliation would clobber the updater's work). Inno lays files down and walks away — exactly what a self-updating per-user app wants. *Scalable alternative (name only):* a **WiX Burn bundle** (`driver.msi` + per-user app) for enterprise GPO/Intune mass-deploy — build only when a customer demands it.

**Two legs, one bootstrapper, ONE elevation.** The earlier-draft "lazy double-elevation" (`PrivilegesRequired=lowest` + a second `runas` for the driver) is **rejected** — it produces *two* UAC prompts plus the VB-Audio device dialog. Instead: **`PrivilegesRequired=admin` → one UAC prompt**, and resolve the *real* (non-elevated) user's `%LOCALAPPDATA%` via the originating/parent SID so the per-user app still lands in the user's profile, not the admin's.

```ini
; ===== SoundBoard.iss (skeleton) =====
[Setup]
AppId={{8F3C2A10-7E4B-4D9A-9C21-LSB000000001}
AppName=LocalSoundBoard
AppVersion={#AppVersion}            ; injected from soundboard/__init__.py via bump_version.py
DefaultDirName={localappdata}\LocalSoundBoard   ; resolved to the INVOKING user's profile
UsePreviousAppDir=yes
PrivilegesRequired=admin            ; ONE prompt. Driver leg needs it; app paths via parent SID.
ArchitecturesAllowed=x64compatible arm64
ArchitecturesInstallIn64BitMode=x64compatible arm64
SignTool=azuretrustedsigning $f    ; Azure Trusted Signing dlib; signs Setup.exe + helper
SignedUninstaller=yes
Compression=lzma2/max              ; do NOT UPX the installer or driver (AV trigger)
WizardStyle=modern
OutputBaseFilename=LocalSoundBoard-Setup-{#AppVersion}

[Tasks]
Name: desktopicon;   Description: "Create a &desktop shortcut"
Name: installdriver; Description: "Install the virtual microphone (recommended)"; Check: not VirtualMicPresent

[Files]
; PER-USER app (no admin needed for these files themselves)
Source: "payload\app\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion; Check: ShouldCopyApp
; The signed elevated helper — same binary the wizard re-uses for "Repair audio device"
Source: "payload\vcable_setup.exe"; DestDir: "{app}"; Flags: ignoreversion
; Bundled base VB-CABLE redistributable, extracted to {tmp}, only when we may install
Source: "driver\vbcable\*"; DestDir: "{tmp}\vbcable"; Flags: deleteafterinstall; Check: WillTouchDriver

[Icons]
Name: "{userprograms}\LocalSoundBoard\LocalSoundBoard"; Filename: "{app}\SoundBoard.exe"
Name: "{userdesktop}\LocalSoundBoard"; Filename: "{app}\SoundBoard.exe"; Tasks: desktopicon

[Run]
; ONE elevated install of the cable, then the per-user first-run wizard
Filename: "{app}\SoundBoard.exe"; Parameters: "--first-run-setup"; Flags: nowait postinstall skipifsilent
```

`ShouldCopyApp()` enforces **no-downgrade**: the installer is the *floor*, the §11 updater owns the *ceiling*. It reads `HKCU\…\Version` (which the updater keeps current) and **refuses to overwrite a newer app** — and uses Inno's real `CompareVersions()` (NOT string `CompareStr`, which sorts "1.2.10" < "1.2.9").

**Bundling the driver payload.** The VB-CABLE redistributable lives in the **installer source tree** (`driver\vbcable\`, Inno LZMA2-compressed), **not** in the PyInstaller `soundboard.spec` `datas=` — shipping a driver into a per-user folder is pointless and bloats the auto-updated app. Keep the app spec's `upx=True` for `_internal`; **never** UPX the installer or the driver.

**Signing — two distinct pipelines (kept separate):**

| Artifact | Signer | Why |
|---|---|---|
| `Setup.exe`, `vcable_setup.exe`, `SoundBoard.exe` + DLLs | **Azure Trusted Signing** (Authenticode, RFC-3161) | SmartScreen reputation; same identity as §11 updater → shared reputation |
| Driver `.sys`/`.cat` (own-driver path only) | **EV cert → Partner Center attestation/WHCP** | Kernel drivers must be Microsoft-signed; Trusted Signing cannot do this |

**AV / SmartScreen reality.** A non-console exe that shells `pnputil`/installs drivers is textbook heuristic-malware, and a brand-new "Romerez" identity has **zero reputation** → first users hit the red wall regardless of a valid signature. Mitigations baked in: sign everything with **one stable identity** (never rotate), **pre-submit** `Setup.exe` + `vcable_setup.exe` to Microsoft and major AV vendors *before* launch, **don't UPX**, call `pnputil` directly (not `powershell -enc`), pin the VB-CABLE installer hash, and budget for "More info → Run anyway" support copy for the first weeks.

**Silent switches & uninstaller.**
```
Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART /LOG="%TEMP%\lsb_setup.log"
          /NODRIVER      ← app-only (CI smoke tests, IT mass-deploy, GPO-pushed driver)
          /FORCEDRIVER   ← (re)install driver even if detected
```
Uninstaller: removes per-user app files from `%LOCALAPPDATA%` (no admin); the driver-removal step is **opt-in, default OFF**, runs only if `InstalledByUs=1` and `RefCount` hits 0, uses VB-CABLE's own `-u -h` uninstaller, and **never** touches a foreign cable. Leave user data (`workspace\`, config, sounds) unless the user ticks "also remove my sounds & settings." Treat `pnputil` exit **3010 = reboot-required** as success.

### 12.4 First-run setup wizard & audio routing

**File:** `soundboard\setup_wizard.py`. **Palette (real keys):** `COLORS["blurple"|"green"|"red"|"yellow"|"bg_darkest"|"text_primary"|…]`, `FONTS`. The wizard does **zero** driver installation in-process; it *detects* and *delegates* the one elevated action to the separate signed `vcable_setup.exe` via `ShellExecuteW(verb="runas")` — preserving the no-admin app + auto-update model (elevating the GUI would re-launch CTk as admin and poison the `%LOCALAPPDATA%\…\workspace` ownership).

**Gate (in `main.py`, before `app.mainloop()`):**
```python
from soundboard.setup_wizard import maybe_run_first_run_wizard
app = SoundboardApp()
maybe_run_first_run_wizard(app)   # modal Toplevel on first run; soft cable re-check otherwise
app.mainloop()
```

**Screen flow — a 5-step `CTkToplevel`:**
```
●───○───○───○───○   Step 1 of 5
 1 WELCOME      "We route mic + sounds through a virtual cable so Discord hears one mic. ~2 min."
 2 DETECT       ✓ found <device> [Use this] [pick other ▾]   |   ✗ [Install for me] [Manual] [I have one ▾]
 3 ROUTING      Output → CABLE Input [locked]   Your mic → [dropdown ▾]   ☑ Mix my mic into the cable
 4 TEST         [Hold to test mic] [Play test sound]   meter ▁▃▅▇  +  end-to-end loopback ✓/✗  + xrun counter
 5 DISCORD      copyable "CABLE Output (VB-Audio Virtual Cable)" [Copy] + GIF + 4-item checklist + [Finish ✓]
```

**Device detect + routing** reuse the §12.2 detector and write the **exact same config keys** the live mixer consumes (so test == production):
```python
def apply_routing(app, cable, mic_index, mix_mic):
    cfg = app.config
    cfg["output_device"] = f"{cable.out_index}: {cable.out_name}"   # format gui.py parses: int(v.split(':')[0])
    mic = sd.query_devices(mic_index)["name"]
    cfg["input_device"]  = f"{mic_index}: {mic}"
    cfg["mix_mic"] = bool(mix_mic)
    cfg["discord_input_hint"] = cable.discord_input
    app.output_var.set(cfg["output_device"]); app.input_var.set(cfg["input_device"])
    app.mic_muted_var.set(not mix_mic)         # "mix mic" == mic NOT muted (audio.py:770/1205/1245)
    app._save_config()
```

**Mic passthrough is already implemented** — `AudioMixer` mixes the mic into the cable unless `mic_muted` is set (`audio.py:1205/1245`). "Mix my microphone into the cable" maps to `mic_muted = not mix_mic`; the wizard is the **single writer** of this mapping so Audio Options never disagrees.

**Test step — verify the RIGHT half (end-to-end), not just our half.** The naive output meter (`mixer.output_peak`, `audio.py:1457`) only proves audio reaches **CABLE Input** — it says nothing about the capture side Discord needs. So step 4 does two things:
1. Plays the real production path (same `_toggle_stream`/`AudioMixer`) and shows the meter — with a **text status label + dB readout**, never color alone (accessibility / deaf users).
2. Runs a **loopback self-test**: capture **CABLE Output** via the already-shipped `soundcard` WASAPI loopback while a test tone plays to CABLE Input. Capturing the tone back proves the cable's capture side is live and selectable — "✓ Your virtual mic works end-to-end." It also counts sounddevice callback `status` xruns during the ~10–15 s test and warns on glitches before the user believes setup is fine.

**The foolproof Discord step (un-automatable, made verifiable-up-to-the-dropdown).** Step 5 shows the **exact full endpoint string** the user will see (resolved from enumeration, not the alias "CABLE Output"), a **[Copy]** button, an annotated **GIF** (with static PNG fallback), and a **4-item checklist** — because Input Device alone is not enough:
1. Set **Input Device** → "CABLE Output (VB-Audio Virtual Cable)".
2. Set **Input Mode** to Push-to-Talk *or* drag **Input Sensitivity** to manual/minimum (else VAD gates out quiet sounds → "my sounds cut off").
3. Turn **OFF** Noise Suppression (Krisp), Echo Cancellation, and Automatic Gain Control for that input (else non-voice audio is mangled/muted).
4. **Use headphones** (mic passthrough + speakers + echo-cancellation-off = feedback/echo).
`[Finish]` is gated on `☑ I've set Discord's input to CABLE Output`. The pre-prompt screen also **warns about the unavoidable VB-Audio device dialog** ("Windows will ask to install 'VB-Audio' device software — click Install. This is expected.").

**Sample-rate pre-flight (both endpoints).** `AUDIO["sample_rate"]=48000`, `block_size=1024` (`constants.py:431`). The wizard pre-flights **both** sides (`sd.check_output_settings` for the cable **and** `sd.check_input_settings` for the mic) and, on mismatch, shows the exact `mmsys.cpl` fix + a **[Open Sound Control Panel]** (`rundll32 shell32.dll,Control_RunDLL mmsys.cpl,,0`) + **[Re-test]**. We **do not** silently downsample — the 48 kHz contract is load-bearing (sound cache, RNNoise's fixed 480-sample frames, Discord). Architectural honesty: the mixer opens **two independent, un-clock-synced streams** (`sd.InputStream`/`sd.OutputStream`, `audio.py:946/956`) — a latent robotic-audio/drift source the wizard cannot fully fix; the real fix (single duplex stream or async-resampling ring buffer off one clock) is an audio-engine change, flagged here so it isn't forever mis-attributed to "user's mmsys settings."

**Re-run / repair / "already have VB-CABLE".** A first-class **"Repair virtual mic"** action (Settings *and* the missing-cable banner) jumps straight to detect→install via the same `vcable_setup.exe`, skipping welcome/Discord. Re-running is **idempotent**: live re-detect, pre-check passing steps. "Already have a cable" → step 2 shows ✓ found, **skips install entirely**, one click to accept (multiple cables → ranked dropdown, VB-CABLE preselected). **Telemetry** instruments the funnel (anonymous, disclosed, off-by-default, one Settings toggle, queued offline to `workspace\telemetry_queue.jsonl`) on ~13 events — `cable_install_uac_declined`, `cable_appeared_after_install_timeout`, `setup_abandoned(discord_checklist_shown)` are the three numbers that tell you whether to invest in Path B/C, reboot UX, or better Discord copy.

**Config keys (with `config_version` for forward-migration):**
```jsonc
{ "config_version": 2, "setup_complete": true,
  "input_device": "1: Microphone (Realtek Audio)", "output_device": "7: CABLE Input (VB-Audio Virtual Cable)",
  "mix_mic": true, "discord_input_hint": "CABLE Output", "cable_warning_dismissed": false }
```

### 12.5 Legal, cost & effort

**Driver-path scorecard (solo dev; order-of-magnitude):**

| Path | Up-front dev | Fixed $/yr | One-time $ | Latency | Maintenance | Legal risk | Verdict |
|---|---|---|---|---|---|---|---|
| **A. Bundle base VB-CABLE (silent)** | **~3 wk** total incl. installer+wizard | Trusted Signing ~$120 | $0 (+ optional goodwill donation) | none | ~none (vendor owns compat) | **Low** — silent bundle explicitly licensed | ✅ **SHIP** |
| **B. License-and-bundle (paid OEM)** | ~3 wk | Trusted Signing ~$120 | vendor-quoted (budget low-4-figures or per-seat) | none | vendor | Low (written license) | ⚠️ if/when revenue justifies |
| **C. Build-your-own driver** | **11–19 wk** | EV ~$200–400 + Trusted Signing ~$120 | Partner Center ~$99 | **weeks** (WHCP/HLK) | **0.5–2 wk per Win release + ARM64** | Low IP, **very high support** | ❌ tripwire only |

**Fixed line items:**

| Item | Cost | For |
|---|---|---|
| Azure Trusted Signing (already decided) | ~$120/yr | app + installer + helper Authenticode |
| EV code-signing cert | ~$200–400/yr | **Path C only** — used to EV-sign the driver/cab *and* enroll Partner Center |
| Partner Center (Hardware) | ~$99 one-time + EV | **Path C only** — attestation/WHCP submission |
| VB-CABLE redistribution license | $0 donationware (silent bundle permitted) / optional ~$500–$2,000 goodwill for paper trail | Path A/B |

**Dev-weeks by leg (Path A):** installer scaffold ~1.0, cable bundle+detect ~0.3, signing pipeline ~0.3, wizard ~1.0, QA/AV shakeout ~0.5 → **~3 weeks**, none blocked on a vendor reply (fallback is always "open the download page").

**Support burden — the real cost (engineer the wizard to pre-empt):**

| Trigger | Likelihood | Pre-empt |
|---|---|---|
| UAC / VB-Audio device dialog "is this a virus?" | very high | pre-warn screen + signed helper |
| AV/SmartScreen flags installer | high | sign one identity, pre-submit, no UPX, hash-pin |
| "No CABLE device after install" (reboot/PortAudio cache) | high | `sd._terminate/_initialize` re-enum + 20 s poll + reboot-resume |
| Robotic/choppy/cutting-out | high | dual-endpoint 48 kHz pre-flight + xrun detect + Discord VAD/Krisp/AGC off |
| Discord still on real mic | guaranteed | exact-string + GIF + checklist + loopback ✓ |
| Echo / "everyone hears themselves" | medium | headphones warning |

**Recommendation:** **Bundle base VB-CABLE, silent, detect-first, guided-download fallback.** Carry the attribution, send VB-Audio a goodwill donation for a paper trail (and to confirm ARM64 + WHCP-signing status). Do **not** build a kernel driver for MVP. Reconcile all prior drafts to this single story (bundle, not "no-driver-Tier-1").

### 12.6 Security / privacy & honest realism

- **Two signing tracks, never conflated:** Trusted Signing for app/installer/helper; EV+Partner Center attestation/WHCP for any own-driver. Same Trusted Signing identity end-to-end so SmartScreen reputation is shared with §11-delivered binaries, not split.
- **Driver maintenance per Windows release:** with the bundle path this is **VB-Audio's** tax, not ours — a core reason to rent theirs. The **April 2026 cross-signed-trust enforcement** is a *named runtime risk*: confirm VB-CABLE's shipping driver is WHCP/attestation-signed (not legacy cross-signed) so it survives the wave, and ship a **runtime cable-health check** — the app already enumerates devices, so detect "cable was present, now gone after a Windows update" and surface a Repair banner instead of a mystery silence that looks like *our* bug.
- **Privacy statement (installer + first-run):** "LocalSoundBoard routes your microphone and soundboard audio through a virtual audio cable so Discord receives them as one microphone. **No audio is recorded, stored, or transmitted to us.** All mixing happens locally in real time." The optional **Call Recorder** (WASAPI loopback) is the *only* feature with a real privacy footprint — keep its consent/disclosure separate. Setup **telemetry** (event counters, no audio) is disclosed, off-by-default, toggleable — reconcile it explicitly with the "nothing leaves the machine" promise rather than letting them silently conflict.
- **What NOT to build:** a hand-rolled kernel driver on the MVP path; a test-signed driver demoed as "done" (it loads only on your dev box with `testsigning on` — validate any real driver on a clean VM, Secure Boot ON, test-signing OFF); silent-bundling of VAC or VB-CABLE A+B/C+D (only **base** VB-CABLE is redistributable); setting the cable as the **Windows default** device (strands the user's audio on uninstall).

### 12.7 ASCII flows

**(a) Installer run (one elevation, detect-first, idempotent):**
```
Setup.exe  (signed; PrivilegesRequired=admin → ONE UAC prompt)
   │  resolve invoking user's %LOCALAPPDATA% via parent SID
   ├─[detect]  virtual_device.detect_cables()  +  HKLM InstalledByUs marker?
   │     ├─ cable present (ours OR foreign) ─────────► SKIP driver leg
   │     └─ none ───► [installdriver task]──► vcable_setup.exe → VBCABLE_Setup_x64.exe -i -h
   │                         (VB-Audio "install device software?" dialog → Install)
   │                         exit 0 or 3010 → write HKLM marker (oemNN.inf, RefCount=1)
   │                         3010 → set NeedsRestart
   ├─[app leg, per-user]  ShouldCopyApp()?  (no-downgrade vs HKCU\…\Version, CompareVersions)
   │     └─ copy payload → %LOCALAPPDATA%\LocalSoundBoard ; write HKCU Version
   └─[handoff]  SoundBoard.exe --first-run-setup  (per-user, NO admin)
                         │
                         ▼
              §11 Ed25519 updater takes over — NO admin, NEVER touches the driver, forever
```

**(b) Audio routing (mic + sounds → cable → Discord):**
```
 Real mic ─┐                                   ┌─ (mix_mic ? passthrough : muted)
 (48k)     ├─► AudioMixer ──► CABLE Input ═════╪═══(virtual cable)═══► CABLE Output ──► Discord
 Sounds ───┘   + RNNoise      (VB-Audio)       │                       (capture side)   Input Device
 (48k cache)   + pitch/FX                       └─ output_peter meter (our half only)        │
                                                                                             ▼
   loopback self-test: capture CABLE Output while tone→CABLE Input  ⇒  end-to-end ✓     others hear you
```

### 12.8 Edge cases & support

| Situation | Behavior |
|---|---|
| **No-admin / managed laptop** | Detect elevation-impossible **before** offering "Install for me"; don't dangle an unusable button. Show "needs administrator rights this PC doesn't allow — ask IT to install VB-CABLE" + a copy-paste block/link for IT. App runs in **local-preview mode** (sounds to speakers); banner explains *why* it can't self-fix. A pre-installed cable (IT/VoiceMeeter) is detected and fully supported. |
| **Existing driver / multiple cables** | Detect-first → skip install; never remove a foreign cable on uninstall; ranked dropdown (VB-CABLE preselected) when several exist; route + Discord-name track the chosen one. |
| **Existing cable, wrong version / capture side disabled** | Probe *suitability* (`check_output_settings(48000)`, version via `pnputil /enum-drivers`), not just presence; if `capture_present=False`, tell the user to enable "CABLE Output" in `mmsys.cpl` Recording tab; offer "update the cable," not just "found one." |
| **Sample-rate mismatch** | Pre-flight **both** mic and cable at 48 kHz; exact `mmsys.cpl` fix + [Open Sound Control Panel] + [Re-test]; never silently downsample. |
| **Uninstall while in use / shared cable** | Driver-removal opt-in (default OFF); only if `InstalledByUs=1` and `RefCount→0`; VB-CABLE's own `-u -h`; never strand the user (never set cable as Windows default). |
| **Offline at first run** | Detect offline before the download fallback; pin installer hash; **defer-and-resume** ("cable_pending" state) — finish in local-preview, re-surface on next online launch. Bundling (Path A) removes this dependency for most users. |
| **Reboot required after install** | Treat `pnputil` 3010 / absent-after-poll as reboot-needed: save wizard state, prompt restart, **resume at the routing step** next launch (don't dead-end at a test that can't pass pre-reboot). |
| **ARM64 (Copilot+/Snapdragon)** | App x64 runs emulated; a kernel driver must be **native ARM64** — that's **VB-Audio's** responsibility. ARM64 support = whatever VB-CABLE ships; if it has no ARM64 driver, **detect and degrade gracefully** (local-preview + honest banner), never silently assume parity. |
| **HVCI / Memory Integrity ON** | A non-HVCI-compatible third-party driver can be blocked on machines with Memory Integrity on (increasingly the Win11 default) — fold into the detect/health story; Secure Boot itself is **not** a blocker for a properly-signed driver (don't list non-risks). |

**Symptom-titled FAQ (wired to each wizard step's "Having trouble?" link):** "People can't hear my sounds in Discord" (master flowchart), "My audio is robotic/choppy/cutting out" (both 48 kHz + Krisp/AGC off + latency preset + headphones), "Windows asked for an admin password I don't have" (no-admin / hand-to-IT), "My antivirus flagged the installer" (signing + More info→Run), "How do I uninstall the cable" (+ promise we won't remove a pre-existing one), "I have VoiceMeeter / multiple cables", "Echo / everyone hears themselves", "Sounds work but my voice doesn't" (`mix_mic`).

---

---

## 13. Legal / Ops Checklist

You become a data controller and a seller the moment you store an email and take money. None of this is optional.

| Item | Action |
|---|---|
| **Web pages** (you have no website today) | Stand up a minimal static site for: password-reset landing, email-verify landing, **ToS/EULA, Privacy Policy, Refund Policy**. GoTrue/Paddle host reset/verify; you still need the legal pages. |
| **Privacy Policy** | GDPR/CCPA disclosure: what you collect, why, retention, sub-processors (Supabase, Paddle, Sentry, PostHog, email). Mandatory once you store an email. |
| **ToS / EULA** | License grant, "lifetime = major version" caveat, acceptable use, liability cap. Record acceptance + version + timestamp **server-side** at signup (wire the register-view checkbox to versioned docs). |
| **Refund policy** | Publish the 14-day window; honor EU's 14-day right of withdrawal for digital goods (or capture a waiver at purchase). |
| **Recording consent** | In-app consent notice before first recording; default local-only; ToS clause placing recording-legality on the user. Deserves real (even templated) legal review. |
| **Tax / MoR** | Paddle remits VAT/GST/sales tax. **You still owe income tax** and need a business entity + KYC to receive payouts — confirm Paddle's payout requirements *before* launch (they gate getting paid). Don't hardcode displayed prices (MoR shows tax-inclusive in some regions); fetch from remote config. |
| **Email deliverability** | Buy a domain; set **SPF + DKIM + DMARC** (start `p=none`); send transactional mail from a subdomain (`mail.localsoundboard.app`); **do not** send from a Gmail from-address. Wire bounce/complaint webhooks. ~half a day of DNS that gates your entire funnel. |
| **Code-signing** | Start cert acquisition **now** (identity validation takes days-weeks). Use **Azure Trusted Signing** (cloud, ~$10/mo, no hardware token). Always timestamp (`/tr`, RFC-3161) so signatures survive cert expiry. Sign in CI with the key in a cloud KMS. |
| **Support** | A support email + FAQ page + a **minimal admin tool** (Supabase table editor + RPCs for "grant comp," "force-logout," "resend verify," "free a seat," "look up by email"). Build before launch — week one is support-heavy. |
| **Observability** | Sentry on Edge Functions + client (opt-in, PII-scrubbed); alert on any webhook 5xx and on `webhook_events` insert-rate → 0; UptimeRobot/BetterStack on `/health`; `RotatingFileHandler` for `debug.log` (currently unbounded, DEBUG, appended, and **committed to git**). |
| **GDPR ops** | "Delete my account" (cascade devices/sessions/entitlements + **storage objects incl. recordings** + **cancel sub at PSP** to avoid post-delete chargebacks); "download my data" export; per-recording + bulk delete; maintain sub-processor list + DPAs; know the **72-hour breach-notification** clock. |
| **`.gitignore` now** | Add `debug.log`, `*.bak`, `__pycache__/`, `auth_cache.json` — currently tracked, an active credential-leak vector once auth lands. |

---

## 14. Phased Roadmap

| Phase | Duration | Goal | Ships |
|---|---|---|---|
| **0 — Backend spine (no client)** | Wk 1-2 | Pay flow works end-to-end via HTTPie before touching the `.exe` | Supabase project + email auth + DDL + RLS; Paddle product + Pro Monthly price; `/billing/checkout` + `/billing/portal` + `/billing/webhook` (idempotent) + `/entitlements` signer (Ed25519 keypair, private→secret, public→client) + `/devices` + `/health`. |
| **1 — Paid MVP** | Wk 3-7 | **First paid customer** | Pre-gate in `main.py` (with "Continue without account"); login/register (+show-password); keyring token store; `license.py` verify + 14-day grace + device binding; gate 4 Pro handlers; PyInstaller hidden imports + **frozen-exe smoke test in CI**; **Authenticode sign**; upgrade button → browser + post-pay poll. |
| **2 — Monetization depth** | Wk 8-11 | Reduce churn, raise LTV | Yearly plan + proration + grandfathering; dunning grace; refund/chargeback → revoke; **device-management UI** (list/revoke/"sign out everywhere"); in-app upsell polish; email change flow; reconcile button. |
| **3 — Moat + compliance** | Wk 12-16 | Un-pirateable Pro value + be legal | **Cloud config sync** (the structural anti-sharing lever); GDPR delete/export; clock-tamper + dual-key rotation (`k2` ships *before* the server signs with it); abuse logging/rate-limit tuning; PostHog opt-in funnel analytics; status page. |
| **Pre-launch, parallel to all** | continuous | Don't get blindsided | **Code-signing cert acquisition (start Wk 1)**; SPF/DKIM/DMARC; legal pages; admin/ops tool; observability + alerting to your phone; **auto-updater with `min_supported_version` kill-switch** (required to fix anything post-launch *and* to complete key rotation). |

**De-risking rule:** build Phase 0 server-first and test it without the client. Most billing bugs are server bugs; finding them through a frozen `.exe` is 10× slower.

---


**Remote-control-plane additions to the roadmap:**

Add to the **Phased Roadmap** (renumbered §14). The updater promotes from the current single "Pre-launch, parallel" bullet into real phased work:

| Phase | Add | Notes |
|---|---|---|
| **Pre-launch (parallel)** | **Prerequisite migration**: canonical `SoundBoard.exe` name, relocate workspace to `%LOCALAPPDATA%\LocalSoundBoard\workspace`, move `debug.log` (RotatingFileHandler), add `pynacl`/`requests`/`packaging` to `requirements.txt` + `soundboard.spec`, single-instance mutex, **frozen-exe launch smoke test in CI** (`deploy.bat` only checks existence today). | Hard blocker — the swap model is unbuildable until this lands. |
| **Phase 1 — Paid MVP** | Ship the **signed `/handshake` gate + `min_supported_version` force-update** (hard kill-switch) and the **anonymous `/telemetry/version`** ping. | Minimal control plane: you can drop a bad build and see fleet versions from day one. |
| **Phase 2 — Monetization depth** | Full **auto-updater**: stub `Launch.exe`, separately-signed `updater.exe`, directory-swap + swap-intent journal + crash-loop watchdog, dual-signature verify, `app_releases`/`release_channel` tables, staged rollout + instant abort/rollback, **automatic rollout-halt `pg_cron`**, browser-download AV fallback, `upx=False`. | The "ship updates + stage + rollback" muscle. |
| **Phase 3 — Moat + compliance** | **Hardened-mode lockout** (24 h TTL for flagged/chargeback accounts) + admin RPCs (`ban_user`/`revoke_device`/`force_logout_all`/`disable_version`/`global_killswitch`/`set_lock_mode`) with blast-radius preview; per-channel beta toggle in Settings; Phase-2 file-hash delta if bandwidth bites. | Completes per-user remote lockout; pairs with cloud-sync moat. |


**Installer & virtual-mic additions to the roadmap:**

- **[Pre-launch track: Installer & Virtual Mic]** Bundle base VB-CABLE (silent `-i -h`, attribution + goodwill donation/paper-trail to VB-Audio), build `installer\SoundBoard.iss` (Inno, one-UAC admin + parent-SID per-user app, no-downgrade floor), and the signed `vcable_setup.exe` helper (re-used by installer **and** wizard "Repair"). Sign all via Azure Trusted Signing; pre-submit to Microsoft/AV. *(~3 dev-weeks; not blocked on vendor reply — fallback is guided download.)*
- **[Pre-launch track: First-run wizard]** Ship `soundboard\setup_wizard.py` (5-step CTk), the single shared `soundboard\virtual_device.py` detector (replace the loose `gui.py:2872` match; wire `--first-run-setup` in `main.py`), end-to-end loopback self-test, dual-endpoint 48 kHz pre-flight, the 4-item Discord checklist + headphones warning, `config_version` migration, and disclosed off-by-default setup telemetry.
- **[Phase: Post-launch hardening]** Runtime cable-health check + Repair banner (survive the **April 2026** cross-signed-trust enforcement; confirm VB-CABLE is WHCP/attestation-signed), ARM64 detect-and-degrade, symptom-titled FAQ, and act on telemetry (UAC-declined / install-timeout / Discord-step-abandon rates).
- **[Phase: Scale — only if tripwire fires]** Build branded "LocalSoundBoard Virtual Mic" (SYSVAD-derived, EV cert + Partner Center **WHCP**) and/or a WiX Burn enterprise bundle; longer-term, fix the two-stream/no-shared-clock audio engine (single duplex stream / async-resampling ring buffer).

## 15. Decisions You Must Make (checklist + recommended default)

| # | Decision | Recommended default | Why |
|---|---|---|---|
| 1 | **Payment provider** | **Paddle (MoR)** | Dodges personal global-tax liability; swap is cheap behind the entitlement indirection. |
| 2 | **Hard login wall vs anonymous Free** | **Anonymous Free; gate only Pro + sync** | A forced wall kills a free desktop toy. Existing local configs keep working untouched. The gate must be **skippable**. |
| 3 | **Token `exp` / offline grace** | **7-day exp / 14-day grace** (Lifetime: 30-day grace) | Short exp → fast revocation; 14d survives a vacation; 30d offline for "lifetime" is too long for a regular sub. `grace_days` is always stamped in the token; client fallback must equal 14. |
| 4 | **Device seat cap** | **3 (Pro)** | Desktop + laptop + friend's PC; shared cracked logins hit the wall fast. Set once in `entitlements.max_devices`. |
| 5 | **Token format** | **Bare Ed25519** with the exact canonical serialization in §8 | Smaller dep, no `alg:none`; both sides MUST agree byte-for-byte. |
| 6 | **Gate location** | **Pre-gate in `main.py` before `SoundboardApp()`** | Don't build the whole UI/audio/tray for an ungated user. |
| 7 | **Trial** | **No card-less trial; Free tier is the trial** | Card-less trials are trivially farmed on desktop. |
| 8 | **Auth/hashing** | **Buy — Supabase GoTrue** | The argon2/JWT/rotation DDL is migration-only, not MVP build. |
| 9 | **Lifetime SKU** | **Launch-only promo, scoped to major version v1.x** | One-time payment vs perpetual server cost is a structural liability. |
| 10 | **Recordings storage** | **Local-only by default; cloud opt-in** | Non-consenting third-party voice data is your worst legal/headline risk. |
| 11 | **MFA** | **None in v1; email is the recovery root of trust** | Defensible for a $5/mo toy — but document it and keep reset-link TTL short (1h). |
| 12 | **Anonymous→signed config merge** | **Prompt "keep local / keep cloud" on first sign-in with existing cloud data** | Avoids silently clobbering a user's sounds/tabs. |
| 13 | **Client hardening ceiling** | **Signed license + keyring + code-signing; PyArmor only if time allows** | Everything past this is theater for a decompilable `.exe`; invest the saved time in cloud sync. |

---


**Remote-control-plane additions to the decisions checklist:**

Add to the **Decisions You Must Make** checklist (renumbered §15), continuing the numbering:

| # | Decision | Recommended default | Why |
|---|---|---|---|
| 14 | **Update channels** | **`stable` (default) + `beta`** via a visible Settings toggle; `beta` doubles as the stable canary ring | Two channels cover a solo dev; opt-in beta gives consistent early signal; leaving beta downgrades to current stable. |
| 15 | **Mandatory-update policy** | **Mandatory only via `min_supported_version` / `blocked_versions`** (security & broken builds); everything else **optional** (dismissible "restart to update"); never auto-apply mid-session, gate at restart boundary | Tearing down a process that may be live in a Discord call is hostile; reserve force for fixes that must ship. |
| 16 | **Rollout ramp** | **`1% → 10% → 50% → 100%`**, bucketed by `hash(device_id\|version)`, with **automatic halt** if revert-rate exceeds threshold; abort = set `rollout_permille=0` | Contains a bad release to a small bucket; deterministic/offline-consistent; the auto-halt protects a sleeping solo dev. |
| 17 | **Kill-switch TTL / hardened mode** | **Normal: 7-day exp / 14-day grace** (Lifetime 30 d). **Flagged/chargeback/abuse: `lock_mode='hardened'` → 24 h TTL, grace 0, `require_online_h:24`** | Don't globally shorten TTL (punishes honest offline users); per-account hardening locks abusers ≤24 h online while everyone else keeps offline-friendly grace. Chargeback ⇒ hardened + Free, never hard-lock (EU/UK legal). |

---


**Installer & virtual-mic additions to the decisions checklist:**

| Decision | Recommended default | Why |
|---|---|---|
| Driver path (license vs build vs guided) | **License & bundle base VB-CABLE, silent, detect-first** (guided-download as fallback) | VB-Audio explicitly permits silent bundling of base VB-CABLE; near-zero cost, a real Microsoft-signed driver, zero kernel risk/maintenance. Building your own is the highest-cost/risk item in the product (EV cert + WHCP + per-Windows + ARM64 + BSOD blast radius) — tripwire only. |
| Installer toolchain | **Inno Setup 6.x, single signed `Setup.exe`** | MSI's file-ownership model fights the §11 self-updater (repair/patch would clobber updated files). Inno lays files and walks away. Scalable alt: WiX Burn bundle for enterprise GPO/Intune. |
| Is the driver mandatory at install? | **Optional / recommended (not mandatory)** | One UAC + a VB-Audio device dialog is unavoidable; locked-down/no-admin machines literally cannot install it. App must run in local-preview mode without it; `installdriver` is a recommended task + `/NODRIVER` switch. |
| Bundle vs on-demand driver download | **Bundle the base VB-CABLE redistributable in the installer** (on-demand download only as offline/AV/locked-down fallback) | Bundling removes the first-run internet dependency, is hash-stable, and is explicitly licensed. Keep the driver in the Inno source tree, **not** in PyInstaller `datas=` (per-user folder is the wrong place); never UPX it. |

---

**Bottom line.** Buy auth (Supabase GoTrue), buy MoR billing (Paddle), build only the ~250-LOC Ed25519 offline-entitlement glue no vendor sells. The two decisions that matter most: (1) **Merchant-of-Record billing** to dodge personal tax liability, and (2) the client trusts **only** a server-signed, device-bound, offline-graced entitlement bundle — never billing status. Ship Free anonymously, gate Pro at action handlers, keep recordings local by default, and start the code-signing cert + auto-updater early. MVP in 6-8 dev-weeks (first paid customer in 3-4), ~$0-30/mo fixed cost until customers exist, with a clean, export-friendly path to a self-hosted FastAPI v2 when revenue justifies it.