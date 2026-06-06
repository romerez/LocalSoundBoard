# LocalSoundBoard — Build Checklist (Steps & Checkpoints)

This is the canonical, ordered playbook for taking LocalSoundBoard from a single-user freeware app to a signed, paid, remotely-controlled product with a real installer and virtual mic. It folds five workstreams (Phase 0 backend spine, Phase 1 paid MVP, Phase 2 control plane, Phase 3 lockout/moat/compliance, plus the parallel Installer and Pre-launch-Ops tracks) into one dependency-correct sequence. The architecture is already approved — do not re-debate it; just execute.

**How to use this:** Tick each `- [ ]` as you complete it. Every phase and every parallel track ends at a bold **✅ CHECKPOINT** gate with objective, testable acceptance criteria. **Do not start the next phase until the current checkpoint passes** (the one exception: the four Week-1 long-lead items and the two parallel tracks, which run alongside but must *merge* at the gates called out below). When a step says **BLOCKED BY**, that upstream step must be green first.

---

## 0. Pre-flight — Lock these before you write code

### 0.1 — Decisions to confirm (no code until these are settled)
- [ ] **Auth/DB/billing stack:** Supabase (GoTrue + Postgres + RLS + Edge Functions) + Paddle (Merchant-of-Record). Confirmed, not re-debated.
- [ ] **Two Supabase projects:** create **`localsoundboard-staging`** and **`localsoundboard-prod`** now. All destructive tests (ban, delete-cascade, killswitch, rollout) run against **staging only**. Record both project refs/URLs/anon keys.
- [ ] **Channels:** `stable` and `beta`. Confirm both will exist in `app_releases`/`release_channel` from day one.
- [ ] **Kill-switch / lock TTLs:** `normal` = `exp=+7d, grace_days=14`; `hardened` = `exp=+24h, grace_days=0, require_online=true`. Locked in.
- [ ] **Kill-switch / version-state SCHEMA HOME (decide once, here):** all kill/version state — `global_killswitch`, `blocked_versions`, `min_supported_version`, `lock_mode_default` — lives in **`app.flags` (jsonb)**. Phase 2/3 read/write ONLY `app.flags`. No competing `kill_switch` singleton or loose `blocked_versions` table. (Resolves the schema triplication.)
- [ ] **Seat cap:** 3 devices/user. **Device-bound entitlements** (claim `device_id` must match local install GUID).
- [ ] **Free tier is anonymous** (no login required); login gates only Pro + cloud sync.
- [ ] **Recordings are LOCAL-ONLY by default** (never uploaded, including by cloud sync).
- [ ] **Single Ed25519 signing identity (k1)** signs `/entitlements`, `/handshake`, and update manifests. Private key → Supabase secret; **public key → embedded in client** before the signer serves prod traffic.
- [ ] **VB-CABLE bundling** = base/free edition, silent install, one VB-Audio trust dialog. We sign NO kernel driver.

### 0.2 — Week-1 long-lead items (start ALL on Day 1; none block each other; all block later work)
- [ ] **L1 — Ed25519 keypair** (program Step 0; blocks every signed thing). → see Phase 0 Step 6, but *generate the key in Week 1*.
- [ ] **L2 — Azure Trusted Signing identity validation** (days→weeks). → Ops WS-A Step 1. Open tracking issue `signing-cert-pending`, check every 2 business days.
- [ ] **L3 — Domain + SPF/DKIM/DMARC** (DNS propagation + warmup). → Ops WS-B Step 4.
- [ ] **L4 — VB-Audio redistribution permission** (3rd-party human reply, no SLA). → Installer Step 1. **Set a fallback trigger date now:** if no written grant by **Week 3**, switch to "download VB-CABLE from VB-Audio URL at install time + attribution."

### 0.3 — Cross-track dependency & sequence map (the spine)

```
WEEK 1 (parallel, no blockers):  L1 keypair · L2 signing cert · L3 domain/DNS · L4 VB-Audio grant

STRICT ORDER after Week 1:
  Step 0  L1 Ed25519 keypair ─────────────► blocks /entitlements, /handshake, client pubkey, updater, key-rotation
  Phase 0  Backend spine (needs L1; needs Ops WS-B for real email — else use dashboard auto-confirm)
  Phase 1 / WS-A  PREREQUISITE MIGRATION (app\ vs workspace\, mutex, paths, argparse flags, .gitignore)
        │
        ├─► Phase 1 / WS-B..F  Paid MVP client   (needs: embedded pubkey, migration, Phase 0, signing cert APPROVED)
        │
        └─► INSTALLER track     (needs: migration for app\ layout, L4 grant, signing cert, owns virtual_device.py)
  Phase 2  Control plane (needs Phase 1 shipped, migration's app\ layout, Phase 0 handshake)
  Phase 3  Lockout/moat/GDPR (needs Phase 2 telemetry for k2 adoption gate, Phase 0 signer)

PARALLEL TRACKS join at gates:
  OPS WS-A signing ─► MERGE BEFORE Phase 1/WS-F (sign exe) AND Installer Step 7 (sign Setup.exe)
  OPS WS-B email   ─► MERGE AT/BEFORE Phase 0 Step 9 (GoTrue SMTP)  [same wiring — do once]
  OPS WS-C legal   ─► MERGE BEFORE Phase 1/D1 (register records acceptance)
  OPS WS-D admin   ─► admin RPCs OWNED by Phase 3 migration; Ops/Phase 2 reference them
  OPS WS-E Sentry/uptime ─► MERGE AT Phase 0 deploy (functions need DSN day one)
  OPS WS-F secret-scan ─► VERIFIES Phase 1/WS-A git hygiene (does not re-do it)

SINGLE-OWNER dedup (created once, consumed elsewhere):
  • virtual_device.py  → OWNED by Installer Step 2 (full route-in/capture/samplerate API); Phase 1/E5 consumes.
  • git rm --cached + .gitignore  → OWNED by Phase 1/WS-A6; Ops WS-F verifies.
  • Authenticode signing job  → OWNED by Ops WS-A Step 3; Phase 1/F, Phase 2/9, Installer/7 invoke it.
  • GoTrue SMTP/domain  → OWNED by Ops WS-B; Phase 0 Step 9 points to it.
  • All admin RPCs  → OWNED by one Phase 3 migration; Phase 2 + Ops WS-D reference.
  • Kill/version state  → ONE home: app.flags (decided in 0.1).
```

**✅ CHECKPOINT — Pre-flight locked:**
**OBJECTIVE:** All long-lead clocks are ticking and every cross-track collision has a single owner.
**DONE WHEN:**
- [ ] Tracking issues open for `signing-cert-pending` (L2) and VB-Audio grant (L4) with status cadence + fallback date.
- [ ] Staging + prod Supabase projects exist; refs recorded; Edge Function secrets parameterized per env.
- [ ] One-line answers written down for: kill-switch schema home (`app.flags`), TTLs, seat cap, channels, who-owns-`virtual_device.py`.
- [ ] The Week-1 four (L1–L4) are all *in progress* (not "scheduled"), verifiable by artifacts: a generated public key, a submitted Azure identity packet, DNS records added, an outbound VB-Audio email.

---

## Phase 0 — Backend Spine (NO client; prove pay-flow via HTTPie)

> Goal: a fully working auth + billing + entitlement backend, exercised end-to-end with HTTPie/curl. No app code. **All `supabase db push` here target STAGING first, then prod.**

### Step 1 — Tooling & repo scaffolding
- [ ] Install CLIs: `npm i -g supabase` (or `scoop install supabase`), `winget install HTTPie.HTTPie`; verify `supabase --version`, `http --version`. (Paddle via dashboard — do NOT assume a winget CLI exists.)
- [ ] Create `backend/`: `mkdir backend; supabase init` (generates `supabase/config.toml`, `functions/`, `migrations/`).
- [ ] Add `backend/.env.example`: `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `PADDLE_API_KEY`, `PADDLE_WEBHOOK_SECRET`, `ED25519_PRIVATE_KEY`, `RESEND_API_KEY`, `SENTRY_DSN`.
- [ ] Append to `.gitignore`: `backend/.env`, `*.key`, `*.pem`, `ed25519_private*`.
- [ ] Create `backend/httpie/` for reusable per-endpoint test snippets.

**✅ CHECKPOINT — Scaffolding:** **DONE WHEN** the three `--version` commands print; `backend/supabase/config.toml` exists; `git status --ignored` shows `backend/.env` ignored and no secret files staged.

### Step 2 — Supabase projects + GoTrue email auth
- [ ] In **both** staging + prod: record URL/anon/service-role keys; `supabase link --project-ref <ref>` per env.
- [ ] Auth → Providers: enable **Email**, password sign-in ON.
- [ ] **Confirm email:** keep it ON in prod, but for Phase-0 testing **use dashboard "auto-confirm" for test users** until Ops WS-B (custom SMTP) lands — do NOT rely on Supabase's shared sender (avoids the "emails not arriving" rabbit hole). **BLOCKED BY (for real confirm mail):** Ops WS-B / Step 9.
- [ ] Set Site URL + redirect allowlist (`http://127.0.0.1` placeholder); note JWT secret + expiry (3600).
- [ ] Password policy: min 8+, leaked-password protection ON.

**✅ CHECKPOINT — Auth basics:** **VERIFY (HTTPie):** `http POST $URL/auth/v1/signup apikey:$ANON email=test@x.com password=Passw0rd!` then `http POST "$URL/auth/v1/token?grant_type=password" apikey:$ANON email=… password=…` → response has `access_token` (JWT with `sub`).

### Step 3 — Core DDL (schema `app.*`)
- [ ] `supabase migration new 001_core_schema`: `create schema app; grant usage on schema app to authenticated, service_role;`.
- [ ] `app.profiles` (`user_id uuid pk references auth.users on delete cascade, email text, created_at, legal_accepted_at, legal_version text`).
- [ ] `app.prices` (`id text pk, paddle_price_id text unique, plan text, interval text, active bool default true`).
- [ ] `app.subscriptions` (`id text pk, user_id uuid references auth.users **on delete cascade**, status, plan, current_period_end, paddle_customer_id, cancel_at, updated_at`).
- [ ] `app.devices` (`device_id text, user_id uuid references auth.users **on delete cascade**, name, last_seen, created_at, revoked_at, primary key (user_id, device_id)`).
- [ ] `app.entitlements` (`user_id uuid pk references auth.users **on delete cascade**, plan default 'free', features text[] default '{}', lock_mode default 'normal', grace_days int default 14, exp_days int default 7, updated_at`).
- [ ] `app.webhook_events` (`event_id text pk, source, type, received_at, processed_at, payload jsonb`).
- [ ] `app.license_tokens` (`jti uuid pk, user_id uuid **references auth.users on delete cascade**, device_id, plan, iat, exp, revoked bool default false`).
- [ ] `app.audit_log` (`id bigint generated always as identity pk, actor, action, target, meta jsonb, created_at`).
- [ ] `app.app_releases` (`version text pk, channel, min_supported bool default false, blocked bool default false, rollout_permille int default 0, manifest jsonb, published_at`).
- [ ] `app.flags` (`key text pk, value jsonb, updated_at`) — **the single home for kill/version state.** Seed: `global_killswitch=false`, `lock_mode_default='normal'`, `blocked_versions=[]`, `min_supported_version=null`.
- [ ] **D4 fix — consistent cascades:** every `user_id` FK above is `on delete cascade`, so a single GoTrue `deleteUser` fully cascades (Phase 3 deletes become belt-and-suspenders).
- [ ] `supabase db push` (staging → prod).

**✅ CHECKPOINT — DDL applied:** **VERIFY:** `select tablename from pg_tables where schemaname='app';` returns all tables; all `user_id` FKs show `ON DELETE CASCADE` in `\d`.

### Step 4 — RLS policies
- [ ] `002_rls`: `enable row level security` on every `app.*` table.
- [ ] Read-own (`using (user_id = auth.uid())`) on `profiles`, `subscriptions`, `devices`, `entitlements`, `license_tokens`.
- [ ] No `authenticated` write on billing-owned tables (`subscriptions`, `entitlements`, `webhook_events`, `license_tokens`, `audit_log`, `app_releases`, `flags`) — service-role only.
- [ ] `devices`: allow user `insert/update/delete` of own rows (`with check (user_id = auth.uid())`).
- [ ] `prices`, `app_releases` (non-secret fields), `flags` (public pointers like `min_supported_version`/`blocked_versions` only): `select` to `anon, authenticated`; no write.
- [ ] `revoke all on schema app from anon;` then re-grant only the explicit selects.

**✅ CHECKPOINT — RLS enforced:** **VERIFY:** `GET /rest/v1/entitlements` with a user JWT returns only that user's row; `PATCH …plan=pro` → 401/403 or 0 rows.

### Step 5 — `recompute_entitlement()` + profile bootstrap
- [ ] `003_functions`: `app.recompute_entitlement(p_user uuid)` (security definer, `search_path=app`): read latest non-cancelled `subscriptions` + `app.flags`; derive `plan` (`pro` if active sub else `free`); set `features` (`{}` free; `['voice_fx','recording','yt_download','editor','cloud_sync']` pro — **these strings are the gate contract with the client**); apply `lock_mode/grace_days/exp_days` (hardened → `'hardened',0,1`); upsert `app.entitlements`; write `audit_log`; return row.
- [ ] Trigger `on auth.users insert` → `app.handle_new_user()` inserting `app.profiles` + default `app.entitlements(plan='free')`.
- [ ] Grant `execute` on `recompute_entitlement` to `service_role` only.

**✅ CHECKPOINT — Entitlement recompute:** **VERIFY (SQL):** insert a fake active sub for `$UID`, `select app.recompute_entitlement($UID);` → `entitlements.plan='pro'`, `features` non-empty.

### Step 6 — Ed25519 keypair (= program Step 0 / L1; do this in Week 1)
- [ ] Generate: `python -c "from nacl.signing import SigningKey; import base64; sk=SigningKey.generate(); print('PRIV',base64.b64encode(bytes(sk)).decode()); print('PUB',base64.b64encode(bytes(sk.verify_key)).decode())"`. Store PRIV in a password manager, never the repo.
- [ ] `supabase secrets set ED25519_PRIVATE_KEY=<b64priv>` (staging + prod).
- [ ] Record **public** key + `kid=k1` in `backend/PUBLIC_KEY.md` with "EMBED THIS IN CLIENT license.py" note.
- [ ] Document bundle format: base64url(`header.payload.sig`); payload claims `{v, kid, sub, plan, features[], device_id, iat, exp, grace_days, lock_mode, require_online, jti}`; sig = Ed25519 over `header.payload`.
- [ ] **Invariant (B1/B5):** the k1 public key must be embedded in a client build (Phase 1/B2) and that build verified BEFORE the prod `/entitlements` signer serves real users. State it; back-apply the same "client-first" rule used for k2 in Phase 3.

**✅ CHECKPOINT — Keypair provisioned:** **VERIFY:** `supabase secrets list` shows `ED25519_PRIVATE_KEY`; round-trip sign-with-priv / verify-with-pub in a throwaway REPL passes.

### Step 7 — Edge Function `/entitlements` (the signer)
- [ ] `supabase functions new entitlements`: verify `Authorization: Bearer <JWT>`, extract `sub`; read `app.entitlements` (service role); honor `app.flags.global_killswitch`, per-user lock, `license_tokens.revoked`; assemble claims (`iat=now`, `exp=now+exp_days`, fresh `jti`, `kid=k1`, `device_id` from query/body); sign with `ED25519_PRIVATE_KEY`; insert `license_tokens`; return `{token, exp}`.
- [ ] Refuse to sign → `403 {code:"LOCKED", reason}` when killswitch on, user banned, device revoked, or `app_version` blocked/below min.
- [ ] CORS; reject missing/invalid JWT with 401. `supabase functions deploy entitlements`.

**✅ CHECKPOINT — Signed bundle:** **VERIFY:** `http GET "$URL/functions/v1/entitlements?device_id=dev-001&app_version=1.2.3" Authorization:"Bearer $JWT"`; in a REPL Ed25519-verify the token with the public key (passes) and assert `plan` == `app.entitlements.plan`.

### Step 8 — Edge Functions `/devices`, `/health`, `/handshake`
- [ ] `devices`: `POST` (upsert, enforce **seat cap 3** → `409 seat_limit`), `GET` (list own), `DELETE` (revoke → set `revoked_at`, invalidate tokens). User JWT.
- [ ] `health`: unauthenticated `GET` → `{status:'ok', time}`.
- [ ] `handshake`: `GET` returns **Ed25519-signed** `{latest_version, min_supported_version, blocked_versions[], rollout_permille, channel, server_time}` read from `app_releases` + `app.flags`.
- [ ] Deploy all.

**✅ CHECKPOINT — Devices + handshake:** **VERIFY:** 4th device → `409`; `/health` → 200 unauthenticated; `/handshake` payload verifies against the public key (flip a byte → fails).

### Step 9 — Email provider wired (Resend/Postmark) — **MERGE POINT with Ops WS-B**
> This is the SAME wiring as Ops WS-B Step 5. Do it once, here, then Ops WS-B *verifies* deliverability.
- [ ] **BLOCKED BY:** Ops WS-B Steps 4–5 (domain + SPF/DKIM/DMARC + provider account).
- [ ] Supabase Auth → SMTP: configure custom SMTP with provider creds; sender = `mail.<domain>` subdomain.
- [ ] `supabase secrets set RESEND_API_KEY=<key>`.
- [ ] Register a fresh user; confirm the email arrives from your domain and the link works.

**✅ CHECKPOINT — Email deliverability:** **VERIFY:** new signup email lands from `@yourdomain` with `dkim=pass spf=pass` (check headers); clicking confirms; login then succeeds.

### Step 10 — Paddle account + Pro Monthly (sandbox)
- [ ] Create Paddle account → **Sandbox**; create product "LocalSoundBoard Pro" + price "Pro Monthly" (recurring monthly); copy `pri_...`.
- [ ] Insert `app.prices('pro_monthly','pri_...','pro','month',true)`.
- [ ] Create sandbox API key → `supabase secrets set PADDLE_API_KEY=<key>`; create webhook destination (URL from Step 12) → `supabase secrets set PADDLE_WEBHOOK_SECRET=<secret>`.

**✅ CHECKPOINT — Paddle catalog:** **VERIFY:** `select * from app.prices where plan='pro';` returns the row; Paddle dashboard shows the price active.

### Step 11 — `/billing/checkout` + `/billing/portal`
- [ ] `billing-checkout`: auth JWT; find/create Paddle customer (store `paddle_customer_id`); create transaction/checkout for `pro_monthly`, passing `user_id` in `custom_data`; return `{checkout_url}`.
- [ ] `billing-portal`: auth JWT; return Paddle customer-portal URL.
- [ ] Deploy both.

**✅ CHECKPOINT — Checkout creatable:** **VERIFY:** `http POST "$URL/functions/v1/billing-checkout" Authorization:"Bearer $JWT"` → URL opens a Paddle sandbox checkout for "Pro Monthly".

### Step 12 — `/billing/webhook` (verify + idempotent + recompute)
- [ ] `billing-webhook` deploy `--no-verify-jwt`; set its URL as the Paddle destination (back-fill Step 10).
- [ ] Verify `Paddle-Signature` (HMAC + timestamp window) → `400` on mismatch.
- [ ] Idempotency: `insert into app.webhook_events(...) on conflict do nothing`; if present, `200` no-op.
- [ ] Handle `subscription.created/updated/activated/canceled/paused` + `transaction.completed`: upsert `subscriptions` (resolve `user_id` from `custom_data`), call `recompute_entitlement`, stamp `processed_at`.
- [ ] On `past_due`/dispute: set hardened lock inputs → recompute yields `lock_mode='hardened'`.

**✅ CHECKPOINT — Webhook flips entitlement:** **VERIFY:** complete checkout with a sandbox test card → `entitlements.plan='pro'`; resend the same event → `webhook_events` count unchanged, no duplicate subscription rows, plan still `pro`.

### Step 13 — `/billing/reconcile`
- [ ] `billing-reconcile`: service-role job that fetches Paddle truth, upserts drift, recomputes changed users. Guard with admin/service header (not user-callable). Document manual run + future cron.

**✅ CHECKPOINT — Reconcile heals drift:** **VERIFY:** `update app.subscriptions set status='canceled'…` (Paddle says active) → run reconcile → status `active`, plan `pro`.

### Step 14 — End-to-end pay-flow proof (HTTPie only)
- [ ] `backend/httpie/e2e.md`: signup → confirm → login → checkout → pay → poll `entitlements.plan` until `pro` → `GET /entitlements` → verify sig + `plan=pro` in a REPL.
- [ ] Negative cases: bad webhook sig → reject; replay → no-op; 4th device → 409; `global_killswitch=true` → `/entitlements` → 403 `lock`.

**✅ CHECKPOINT — PHASE 0 COMPLETE (pay-flow proven, no client):**
**OBJECTIVE:** the monetization spine works end-to-end from a terminal.
**DONE WHEN — all via HTTPie + a Python verify REPL:**
- [ ] Register + login returns a JWT (`sub` present).
- [ ] `/billing/checkout` URL completes payment with a test card.
- [ ] Post-pay `entitlements.plan='pro'` automatically; replay idempotent (no dup rows).
- [ ] `GET /entitlements` token Ed25519-verifies; claims `{sub, plan:'pro', features non-empty, device_id, iat, exp=iat+7d, grace_days, jti, kid='k1'}`.
- [ ] Guards: bad `Paddle-Signature`→400; 4th device→409; `global_killswitch=true`→403 `lock`; `/health`→200 unauthenticated.
- [ ] **HOW TO VERIFY:** run `backend/httpie/e2e.md` top to bottom against STAGING. No client involved.

---

## Phase 1 — Paid MVP Client (First Paying Customer)

> **BLOCKED BY:** Phase 0 deployed + tested; k1 public key exported; signing cert APPROVED (Ops WS-A) for WS-F. All work on `phase1/paid-mvp`, not `main`.

### Step 0 — Branch + baseline
- [ ] `git checkout -b phase1/paid-mvp`
- [ ] Baseline build works: `pyinstaller soundboard.spec`; run `dist\SoundBoard\SoundBoard.exe` once; note behavior as the regression baseline.

### WORKSTREAM A — PREREQUISITE MIGRATION (hard blocker; lands & ships first; foundation for Installer + Phase 2)

#### A1 — Paths module
- [ ] Create `soundboard/paths.py`: `app_root()`→`%LOCALAPPDATA%\LocalSoundBoard`, `workspace_dir()`→`…\workspace`, `app_dir()`→`…\app`, `config_path()`, `sounds_dir()`, `images_dir()`, `log_path()`, `auth_cache_path()`. Each `mkdir(parents=True, exist_ok=True)` on first call.
- [ ] In `soundboard/constants.py` route `CONFIG_FILE`/`SOUNDS_DIR`/`IMAGES_DIR` (lines 476–478) through `paths.*`.
- [ ] In `main.py` DELETE the `os.chdir(workspace_root)` frozen-cwd hack (lines 16–19) — paths are now absolute.

#### A2 — One-time config/data migration
- [ ] `migrate_to_workspace()` in `paths.py`: if `workspace\soundboard_config.json` absent AND legacy `.\soundboard_config.json` exists, copy config + `sounds\` + `images\` into workspace, write `.migrated` marker. Idempotent; never deletes legacy source.
- [ ] Call it in `main.py` BEFORE `SoundboardApp()`.
- [ ] Update `gui.py` `_save_config_now` (13080) / `_load_config` (13169) + `.tmp`/`.bak` logic (13146–13159) to write under `paths.config_path()` (siblings in workspace, not cwd).

#### A3 — Canonical SoundBoard.exe
- [ ] Confirm `soundboard.spec` names EXE `SoundBoard` (line 161) + COLLECT `SoundBoard` (line 182). Grep repo for `main.exe`/`Discord Soundboard.exe`; normalize to `SoundBoard.exe`.

#### A4 — RotatingFileHandler
- [ ] In `main.py` REPLACE `logging.basicConfig(filename="debug.log",…)` (56–61) with `RotatingFileHandler(paths.log_path(), maxBytes=2_000_000, backupCount=3, encoding="utf-8")`, level DEBUG.
- [ ] In `gui.py` fix raw `open("debug.log","a")` breadcrumb writer (11009–11012) → `paths.log_path()`.

#### A5 — Single-instance mutex
- [ ] In `main.py` before app construct: `CreateMutexW(None, False, "Local\\LocalSoundBoard_SingleInstance")`; if `GetLastError()==183`, show "already running" + `sys.exit(0)`. Hold handle module-global.

#### A6 — .gitignore + repo hygiene (**SINGLE OWNER**; Ops WS-F only verifies)
- [ ] Add to `.gitignore`: `debug.log`, `*.bak`, `auth_cache.json`, `soundboard_config.json.bak`, `signing/*.pfx`, `*.key`, `.env`. (`__pycache__/`, `dist/`, `build/` already present.)
- [ ] `git rm --cached debug.log soundboard_config.json.bak` and every tracked `soundboard/__pycache__/*.pyc`; commit.

#### A7 — CLI argparse flags (**D5 — defines flags 3 tracks assume**)
- [ ] Add `argparse` to `main.py` for `--smoke-test` (boot, `root.after(5000, root.destroy)`, exit 0), `--first-run-wizard`, `--probe-cable` (write JSON + exit code). CI (A8), Installer (Steps 4–5), Release (Phase 2/9) all depend on these existing.

#### A8 — Frozen-exe launch smoke test in CI
- [ ] `.github/workflows/build.yml` (`windows-latest`): checkout → Python 3.13 → `pip install -r requirements.txt pyinstaller` → `pyinstaller soundboard.spec`.
- [ ] Smoke step: run `dist\SoundBoard\SoundBoard.exe --smoke-test`; assert exit 0. **Also launch from a directory ≠ install dir** and assert sounds/images/emoji render (catches `os.chdir` removal regressions).
- [ ] Upload `dist\SoundBoard\` artifact.

**✅ CHECKPOINT — Migration:**
- [ ] Fresh clean-machine run creates `%LOCALAPPDATA%\LocalSoundBoard\workspace\soundboard_config.json`; editing a slot persists there (mtime), NOT in repo.
- [ ] Legacy `.\soundboard_config.json` auto-migrates (config + sounds + images in workspace; `.migrated` written; legacy untouched).
- [ ] `debug.log` rotates: force >2 MB → `debug.log.1` appears under workspace.
- [ ] 2nd instance exits with "already running"; one process in Task Manager.
- [ ] Launching from a foreign cwd renders assets (no cwd-relative breakage).
- [ ] `git status` clean of `debug.log`, `*.bak`, `__pycache__`, `auth_cache.json`.
- [ ] CI build green; `SoundBoard.exe --smoke-test` exit 0; artifact present.

### WORKSTREAM B — Dependencies & PyInstaller spec
- [ ] **B1:** append to `requirements.txt`: `requests>=2.31`, `keyring>=24.0`, `pynacl>=1.5`, `packaging>=23.0`; `pip install -r requirements.txt`; verify `import requests, keyring, nacl.signing, packaging`.
- [ ] **B2:** `soundboard.spec` `hiddenimports` (71–143) += `keyring`, `keyring.backends.Windows`, `win32ctypes.pywin32`, `win32ctypes.core`, `requests`, `nacl`, `nacl.signing`, `nacl.encoding`, `packaging`, `packaging.version`. Embed pubkey: drop `soundboard/keys/entitlement_pubkey.pem` (k1) + add `('soundboard/keys','soundboard/keys')` to `datas` (56–70). Rebuild; no `ModuleNotFoundError`.

**✅ CHECKPOINT — Deps/spec:** **DONE WHEN** frozen exe runs, `keyring.get_keyring()` returns the Windows backend, `nacl.signing` imports, and `entitlement_pubkey.pem` is at `dist\SoundBoard\_internal\soundboard\keys\`.

### WORKSTREAM C — License/auth/billing modules
- [ ] **C1 `api_client.py`:** `ApiClient(base_url)` over `requests.Session` (8s timeout, 1 retry/backoff, Bearer injection). Methods: `login/register/forgot_password/refresh` (GoTrue), `get_entitlements(device_id)`, `register_device/list_devices/delete_device`, `get_handshake`, `checkout_url/portal_url`, `ping_version`. Supabase URL + anon key as module constants (anon is public).
- [ ] **C2 `auth_store.py`:** `save_tokens/load_tokens/clear_tokens` via `keyring` JSON blob (`"LocalSoundBoard","session"`) — never touch config. `device_id()` = stable GUID in keyring (uuid4 once). `cache_token_bundle/load_token_bundle` → `paths.auth_cache_path()` (gitignored).
- [ ] **C3 `license.py`:** load embedded pubkey (resolve via `sys._MEIPASS` when frozen). `verify_bundle(raw)->Claims` (`VerifyKey.verify`, raise on bad sig). **Device binding:** reject if `claims.device_id != auth_store.device_id()`. **Grace:** valid `now<=exp`; grace if `exp<now<=exp+grace_days`; else Free. **require_online:** if claim true, fail closed when no online re-fetch within `exp` (no grace). **Clock-tamper (D6 unified):** single monotonic high-watermark in keyring fed by BOTH entitlement `iat` and handshake `server_time`; if local clock < watermark beyond skew → ignore grace, drop to Free until online. `Entitlement` exposes `is_pro()`, `features`, `state∈{ACTIVE,GRACE,FREE}`, `reason`. Add `verify_handshake(payload,sig)` (reuses pubkey + `packaging.version`).
- [ ] **C4 `auth.py`:** `Session` boot: load tokens → refresh if near-expiry (**D1: persist the rotated refresh_token; on refresh 401, clear tokens → anonymous Free, never crash**) → fetch+verify entitlement online → else cached bundle (grace) → else anonymous Free. `login/register/logout/forgot`; on first login `register_device` (handle seat cap 3 → surface manage-devices path). `refetch_entitlement()` on a worker thread, result via `root.after`. `is_pro()/has_feature(name)`.
- [ ] **C5 `billing.py`:** `open_checkout(feature_hint)` → `webbrowser.open`. `open_portal()`. `poll_after_pay(on_pro, timeout=180s)` worker thread polling every ~5s; **D8: on timeout show "Payment received, finalizing — restart in a minute" (not silent revert); next `_auth_tick` picks it up.**

**✅ CHECKPOINT — License core (no GUI):**
- [ ] Genuine signed bundle → `is_pro()==True`; byte-flipped → raises.
- [ ] Different `device_id` rejected.
- [ ] Network blocked + cached bundle past `exp` within 14d → `GRACE`/`is_pro()`; past 14d → `FREE`.
- [ ] Rewinding clock below watermark → `FREE` despite valid cache.
- [ ] Tokens appear in Credential Manager (`cmdkey /list` shows `LocalSoundBoard`); nothing sensitive in `soundboard_config.json`.
- [ ] Refresh-token rotation persists; a revoked refresh token → clean drop to anonymous Free.

### WORKSTREAM D — Login UI
- [ ] **D1 `login_ui.py`:** `LoginWindow(on_done)` CTk modal, tabs Login/Register/Forgot; email+password+show-password toggle; inline errors; network on worker thread → `root.after`. Prominent **"Continue without account"** → anonymous Free. Success → store tokens, fetch entitlement, `on_done(session)`. **Register records legal acceptance** (ToS/Privacy checkbox → backend). **BLOCKED BY:** Ops WS-C (legal pages + `app.legal_acceptances`).

**✅ CHECKPOINT — Login UI:**
- [ ] Register a sandbox user → row in `app.profiles`.
- [ ] Login persists across relaunch (keyring).
- [ ] Show-password toggles; Forgot triggers a GoTrue reset email.
- [ ] "Continue without account" → Free with no network dependency.

### WORKSTREAM E — Wire into app (main.py + gui.py)
- [ ] **E1 main.py pre-gate:** after migration + mutex, construct `Session` (`auth.boot`). If no valid session and "show login on start" (default true unless previously skipped), show `LoginWindow` first; else anonymous. Pass `session` into `SoundboardApp(session=...)` (extend `__init__` at gui.py:2047).
- [ ] **E2 gui.py session + chip:** store `self.session`, `self._auth_after_id`. Add `is_pro()`, `_require_pro(feature, *, action_label)` (else upsell dialog with Upgrade → `billing.open_checkout`+`poll_after_pay`). `_auth_tick`: every **60s** re-evaluate state + refresh chip via `root.after(60000,…)`; kick **6–24h jittered online re-fetch** on a worker thread. **Account chip** in header: email + badge (Free/Pro/Pro (grace)) or "Sign in"; menu = Sign in/out, Manage devices, Upgrade/Manage billing (`open_portal`). Schedule `_auth_tick` in `run()` (14055); cancel `_auth_after_id` + join workers in `_on_close` (13437).
- [ ] **E3 — gate the 4 Pro handlers** (feature strings MUST match Phase 0 `features[]`):
  - [ ] `_on_voice_master_toggle` (7609): if turning ON and `not _require_pro("voice_fx", action_label="Voice Changer")` → revert `voice_enabled_var.set(False)`, return. Also guard `_apply_voice_preset` (7616).
  - [ ] `_toggle_recording` (3746): when starting, gate `_require_pro("recording", action_label="Call Recording")`. Defensive mirror at `Recorder.start` (audio.py:2392) + `_toggle_quick_record_to_sound` (3833).
  - [ ] `_start_youtube_download` (12254) + `_show_youtube_download_dialog` (12016): gate `_require_pro("yt_download", action_label="Web Audio Download")` before probe.
  - [ ] `_open_sound_editor` (11450) + `_open_prepared_sound_editor` (11393): gate `_require_pro("editor", action_label="Sound Editor")`.
- [ ] **E4 Upgrade flow:** upsell + chip Upgrade call `open_checkout()` then `poll_after_pay(on_pro=…)`; on success refresh entitlement, update chip, re-enable the attempted action (toast "Pro unlocked").
- [ ] **E5 virtual_device.py consumer (CONSUME ONLY — file OWNED by Installer Step 2):** replace inline `gui.py:2872` check with `virtual_device.is_virtual_cable(name)` (thin alias the installer track provides). Do NOT create the file here.
- [ ] **D2 seat-full-on-login wall:** when first login hits 3/3 seats, surface the device list + inline Revoke (not a dead-end error) so a paying user can free a seat and use Pro on this machine.

**✅ CHECKPOINT — Wiring:**
- [ ] Anonymous launch: all non-Pro features work; the 4 Pro actions upsell.
- [ ] After login + sandbox pay, all 4 Pro actions work without restart (poll flips chip + re-enables).
- [ ] Network kill mid-session keeps Pro per grace; `_auth_tick` shows "Pro (grace)" past `exp` within 14d.
- [ ] Cable auto-select still works via `virtual_device.is_virtual_cable` (no behavior change at 2872).
- [ ] Login at a full 3/3 seat presents inline revoke, not an error wall.

### WORKSTREAM F — Authenticode signing + ship (**MERGE with Ops WS-A**)
- [ ] **F1 — BLOCKED BY Ops WS-A `signing-cert-pending` = APPROVED.** Build `pyinstaller soundboard.spec`; invoke the **Ops WS-A signing job** (do not re-specify signtool flags) to sign `SoundBoard.exe`. Verify `signtool verify /pa /v dist\SoundBoard\SoundBoard.exe` → "Successfully verified"; Properties → Digital Signatures shows publisher + timestamp.

**✅ CHECKPOINT — PHASE 1 (first paying customer):**
- [ ] **Signed exe runs:** `signtool verify /pa` passes; reduced SmartScreen; launches from `%LOCALAPPDATA%`.
- [ ] **Free works with no account:** clean machine → "Continue without account" → usable; 4 Pro actions upsell.
- [ ] **Login + sandbox pay unlocks Pro:** all 4 live; Supabase `subscriptions`/`entitlements` reflect Pro.
- [ ] **Offline relaunch keeps Pro within grace then drops:** disconnect → still Pro; past `exp+14d` (mock `exp`, do NOT wall-clock-advance — that's the tamper trigger) → Free with a clear "needs re-check" notice.
- [ ] **Tokens in Credential Manager, never config.json:** `cmdkey /list` shows `LocalSoundBoard`; config has no tokens/emails/secrets; `auth_cache.json` holds only the signed bundle and is gitignored.

---

## ⟂ PARALLEL TRACK: INSTALLER + VIRTUAL MIC

> Runs alongside Phases 0–1. **Step 1 starts Week 1.** Building Steps 3+ are **BLOCKED BY Phase 1/WS-A migration** (app\ layout). **Owns `virtual_device.py` in full.** Signing Step 7 **BLOCKED BY Ops WS-A**.

### Step 1 — VB-CABLE bundling permission (BLOCKER, Week 1)
- [ ] Email VB-Audio (`contact@vb-audio.com` / redistribution form) requesting written permission to silently bundle VB-CABLE base edition in `Setup.exe`; state app name, channel, volume.
- [ ] Make a goodwill donation/license purchase; keep the receipt.
- [ ] Save the paper trail to `legal/vb-audio/` (request, receipt, grant reply).
- [ ] Add `THIRD-PARTY-NOTICES.txt` crediting "VB-CABLE © VB-Audio Software"; record any logo/text attribution they require.
- [ ] Download official VB-CABLE base ZIP; record SHA-256 → `installer/vbcable/VBCABLE.sha256`. Place payload at `installer/vbcable/` (binaries NOT committed — release-artifact storage, referenced by build script).
- [ ] **Fallback (set trigger date = Week 3):** if no written grant, switch installer to download VB-CABLE from VB-Audio's URL at install time + attribution.

**✅ CHECKPOINT — VB-CABLE redistribution cleared:** **DONE WHEN** `legal/vb-audio/` has a written grant (or documented permissive terms) + donation receipt; `THIRD-PARTY-NOTICES.txt` credits VB-Audio; `Get-FileHash` matches `VBCABLE.sha256`.

### Step 2 — The ONE detector: `soundboard/virtual_device.py` (**SINGLE OWNER**)
- [ ] Create with: `find_cable_output_route_in()` (CABLE Input = where app writes), `find_cable_capture()` (CABLE Output = what Discord selects), `is_installed()`, `device_default_samplerate(idx)`, plus thin alias `is_virtual_cable(name)` for Phase 1/E5.
- [ ] Match canonical VB-CABLE strings via normalized regex (not loose substring); detect both sides via `sd.query_devices()` (route-in = `max_output_channels>0` matching "CABLE Input"; capture = `max_input_channels>0` matching "CABLE Output"). Return `default_samplerate` + `hostapi`.
- [ ] Replace the loose check at `gui.py:2872`; **grep-sweep** `soundboard/` and replace every other loose `"cable"`/`"virtual"` device check with detector calls.
- [ ] `tests/test_virtual_device.py`: mocked `query_devices()` (with/without cable, mixed case, "CABLE-A"/"CABLE Output" variants) asserts correct route-in/capture/index/samplerate.

**✅ CHECKPOINT — Single detector:** **DONE WHEN** `Grep '"cable"|"virtual"'` over `soundboard/*.py` matches ONLY inside `virtual_device.py`; `pytest tests/test_virtual_device.py` passes; on a machine WITH VB-CABLE the detector returns route-in + capture indices + non-zero samplerate.

### Step 3 — Inno Setup Setup.exe skeleton (**BLOCKED BY Phase 1/WS-A**)
- [ ] `installer/LocalSoundBoard.iss` `[Setup]`: `AppId` GUID, `AppName=LocalSoundBoard`, `PrivilegesRequired=admin`, `OutputBaseFilename=Setup`, `DefaultDirName={localappdata}\LocalSoundBoard\app` (aligns with migration; per-user app, machine driver).
- [ ] `[Files]`: frozen `SoundBoard.exe` tree from `dist/`, `Launch.exe`, `updater.exe`, `installer/vbcable/*` (`Flags: dontcopy` for conditional driver).
- [ ] `[Run]`: `{app}\SoundBoard.exe --first-run-wizard` `nowait`.
- [ ] `[Code]`: `InitializeSetup`/`CurStepChanged` hooks for detect-first + ownership marker.
- [ ] `installer/build_installer.ps1` → `iscc.exe installer\LocalSoundBoard.iss`; assert `installer/Output/Setup.exe` produced.

**✅ CHECKPOINT — Installer skeleton compiles:** **DONE WHEN** `build_installer.ps1` produces `Setup.exe`; on a VM exactly ONE UAC prompt; installs to `%LOCALAPPDATA%\LocalSoundBoard\app`; launches the wizard.

### Step 4 — Bundle VB-CABLE + detect-first silent install
- [ ] In `[Code]`, before driver install, run `SoundBoard.exe --probe-cable` (exit code/JSON) to detect a working cable.
- [ ] If absent: extract payload, run `VBCABLE_Setup_x64.exe -i -h` (one VB-Audio trust dialog is expected — do NOT suppress the WHQL/publisher prompt).
- [ ] If present + functional: SKIP driver step; do NOT write ownership marker.
- [ ] Re-probe; verify route-in + capture enumerate before proceeding. Surface "restart recommended" on reboot-required exit codes (don't hard-fail).

**✅ CHECKPOINT — Detect-first driver install:** **DONE WHEN** (clean VM) Setup installs VB-CABLE with a SINGLE VB-Audio prompt and devices appear; (VM with cable) driver step skipped (no 2nd prompt, no marker) — verify via install log.

### Step 5 — First-run wizard (CustomTkinter), re-runnable
- [ ] `--first-run-wizard` (defined in A7) opens a CTk wizard before/independent of full `SoundboardApp`.
- [ ] Step A detect (`is_installed()`; offer install, elevate if needed); B auto-route (output → `find_cable_output_route_in()`, enable mic passthrough so call hears mic + sounds); C **48 kHz pre-flight** (read route-in + capture `default_samplerate`; if ≠48000, deep-link `mmsys.cpl` + screenshot to set both endpoints to 48000, re-verify — prevents robotic/pitch audio); D live test (play/speak → VU meter on the **capture** side moves); E foolproof "Discord → Input = `CABLE Output (VB-Audio Virtual Cable)`" card with screenshots/GIF under `assets/wizard/`.
- [ ] Settings entry "Re-run setup wizard"; persist `wizard_completed` + chosen device indices in workspace config (auto-open only once).

**✅ CHECKPOINT — First-run wizard:** **DONE WHEN** (clean VM) wizard auto-routes output→CABLE Input with passthrough; a test sound moves the CABLE Output meter; 48 kHz pre-flight detects + fixes a forced-44.1 kHz mismatch (clean audio); Discord card shows the GIF; "Re-run setup wizard" reopens it.

### Step 6 — Uninstaller with ownership marker
- [ ] On WE-installed path only, write `%PROGRAMDATA%\LocalSoundBoard\vbcable_owned.marker`.
- [ ] Uninstall hook: remove VB-CABLE (`VBCABLE_Setup_x64.exe -u -h`) ONLY IF marker exists AND no other consumer in use (best-effort). Always remove app files + marker; never remove driver if marker absent. Log decisions.

**✅ CHECKPOINT — Clean uninstall:** **DONE WHEN** uninstalling where WE installed removes the driver (cable gone from `query_devices()`); where it pre-existed, driver intact (marker never written).

### Step 7 — Authenticode-sign Setup.exe (**MERGE with Ops WS-A**)
- [ ] **BLOCKED BY Ops WS-A APPROVED.** In `build_installer.ps1`, invoke the Ops WS-A signing job to sign inner `SoundBoard.exe`/`Launch.exe`/`updater.exe`, then the final `Setup.exe`. Verify `signtool verify /pa /v installer\Output\Setup.exe`; UAC banner shows verified org (not "Unknown Publisher").

### Step 8 — Setup-completion telemetry (funnel) (**BLOCKED BY Phase 2/Step 5 endpoint**)
- [ ] Post non-PII funnel events to `/telemetry/version` (or `/telemetry/setup`): `setup_started`, `cable_detected_existing`, `cable_installed`, `wizard_routed`, `samplerate_fixed`, `wizard_completed`, + failure stage on abort. Fields: app version, OS build, anonymous device_id, stage only.

### Step 9 — Elevated-install ACL fix (**D7 — silent update-killer trap**)
- [ ] After the elevated install, **reset ACLs on `app\` and `workspace\` to grant the current user full control** (`icacls`), so the non-admin `updater.exe` (Phase 2/6) can overwrite files. Checkpoint that a non-admin process can replace a file under `app\`.

**✅ CHECKPOINT — INSTALLER + VIRTUAL MIC (clean-VM acceptance):**
- [ ] One signed `Setup.exe` installs app + VB-CABLE with a **single** VB-Audio prompt + **one** UAC elevation.
- [ ] Wizard auto-routes output→CABLE Input; a sound reaches CABLE Output and the meter moves.
- [ ] **Machine-verifiable proxy (no human Discord):** a loopback capture script on `CABLE Output` records N seconds and asserts non-silence RMS.
- [ ] VM that already has VB-CABLE → driver step skipped (no 2nd prompt, no marker).
- [ ] Forced 44.1 kHz mismatch is detected + fixed → clean playback (no chipmunk).
- [ ] Uninstall removes the driver only when WE installed it; pre-existing driver intact — no orphan either way.
- [ ] Non-admin `updater.exe` can overwrite files in `app\` (ACL trap closed).

---

## ⟂ PARALLEL TRACK: PRE-LAUNCH OPS / LEGAL / SECURITY

> Start Week 1, run throughout. Merge points are marked. **WS-A (signing) is the long pole — kick off Day 1.**

### WS-A — Code Signing (Azure Trusted Signing) + CI signing
- [ ] **Step 1 (Week 1):** `az login`; `az provider register --namespace Microsoft.CodeSigning`; `az trustedsigning create -n <acct> -g <rg> -l <region> --sku Basic`; create a **Public Trust** Certificate Profile; submit **identity validation** docs (legal name, address, D-U-N-S). Open issue `signing-cert-pending`; check every 2 business days.
- [ ] **Step 2 (once Approved):** install Trusted Signing dlib; `signing/metadata.json` (Endpoint, account, profile); assign **Trusted Signing Certificate Profile Signer** role to CI SP; local dry-run `signtool sign … /dlib … /dmdf signing/metadata.json test.exe` then `signtool verify /pa /v test.exe`.
- [ ] **Step 3 — the ONE signing job (consumed by Phase 1/F, Phase 2/9, Installer/7):** `.github/workflows/build-sign.yml` builds via PyInstaller, then signs `SoundBoard.exe`, `updater.exe`, `Launch.exe`, `Setup.exe`; verification gate fails the job if `signtool verify /pa` fails on ANY artifact; RFC3161 `/tr` timestamping; manual fallback runbook in `signing/README.md`. Prefer OIDC federated credential over stored `AZURE_CLIENT_SECRET`.

**✅ CHECKPOINT — Signing operational:** Profile = **Approved**; a pushed tag yields signed `SoundBoard.exe/updater.exe/Launch.exe/Setup.exe` all passing `signtool verify /pa /v`; clean-VM Properties shows org + valid timestamp; no "Unknown Publisher"; CI goes RED if any artifact is unsigned (verify by skipping a sign step once).

### WS-B — Domain + transactional email (**MERGE at Phase 0/Step 9**)
- [ ] **Step 4 (Week 1):** buy domain; DNSSEC if available; **SPF** `v=spf1 include:_spf.resend.com -all`; **DKIM** CNAME/TXT from provider; **DMARC** `_dmarc` `v=DMARC1; p=none; rua=mailto:dmarc@<domain>; pct=100` (tighten to quarantine→reject after a clean week).
- [ ] **Step 5:** Resend/Postmark account; dedicated sending subdomain `mail.<domain>`; wire GoTrue SMTP (this IS Phase 0/Step 9); bounce/complaint webhooks → `/email/webhook` Edge Function suppressing into `app.email_suppressions` (HMAC-verified, idempotent).

**✅ CHECKPOINT — Email deliverability:** verify email lands in Inbox (Gmail + Outlook); mail-tester shows SPF/DKIM/DMARC = pass; seed bounce fires the webhook → row in `app.email_suppressions`; DMARC `rua` report arrives ≤48h.

### WS-C — Legal pages + server-side acceptance (**MERGE before Phase 1/D1**)
- [ ] **Step 6:** write ToS/EULA (license grant, **"lifetime = current MAJOR version only"**, acceptable use, **recording-consent clause**), Privacy (data collected, Paddle MoR, Supabase/Sentry sub-processors, **recordings LOCAL-ONLY**, GDPR export/delete, retention), Refund (Paddle MoR, 14-day). Add `legal_version` + last-updated to each.
- [ ] **Step 7:** host `/terms`, `/privacy`, `/refunds`; link from site footer + in-app (`gui.py` about + `login_ui.py` signup). `app.legal_acceptances(user_id, doc, version, accepted_at, ip)` with RLS `user_id=auth.uid()`. On signup/first paid action, explicit accept checkbox → service-role insert with `legal_version`. On `legal_version` bump, re-prompt at next launch.

**✅ CHECKPOINT — Legal live + recorded:** `/terms`, `/privacy`, `/refunds` return 200, reachable from footer + in-app; ToS has the major-version + recording-consent clauses; Privacy states recordings are local-only; signup writes `app.legal_acceptances` with current `legal_version` (self-read via RLS).

### WS-D — Minimal admin / ops tool (**admin RPCs OWNED by Phase 3; this tool CONSUMES them**)
- [ ] **Step 8:** build a minimal admin surface (CLI or one internal page) gated behind an `is_admin` claim/role (NEVER the service-role key in any client) exposing: **lookup by email**, **grant_comp**, **force_logout_all**, **free_seat**, **resend_verify**, **disable_version**, **global_killswitch**. Each action goes through the Phase 3 audited RPCs. After `disable_version`/`global_killswitch`, confirm `/entitlements` refuses to re-sign for the affected scope.

**✅ CHECKPOINT — Admin tool usable:** lookup-by-email returns plan/devices/subscription; `grant_comp` flips a free user to Pro picked up on next `/entitlements`; `force_logout_all` invalidates sessions; `disable_version` causes that build to hit hard "Update required"; every action appears in `app.audit_log`.

### WS-E — Observability + alerting (**MERGE at Phase 0 deploy**)
- [ ] **Step 9:** Sentry in all Edge Functions (server DSN, env tag, release). **Opt-in, PII-scrubbed** Sentry in the client (off by default; Settings toggle; `before_send` strips email/paths/device_id). Uptime monitor on `/health` (alert on non-200/latency). Alert on Paddle `/billing/webhook` 5xx rate AND on `webhook_events` insert-rate → 0. Route alerts to email + push/SMS with **SLO ≤5 min**.

**✅ CHECKPOINT — Alerting pages you:** forcing a webhook 500 alerts your phone ≤5 min; pausing webhook delivery (insert-rate 0) fires the alarm; `/health` 503 fires the uptime alert; a test client error reaches Sentry with **no email/username/path/device_id**.

### WS-F — Secrets hygiene + security self-review (**VERIFIES Phase 1/WS-A6, does not re-do**)
- [ ] **Step 10:** confirm client embeds ONLY the Ed25519 public key (no private/service-role/SMTP/Paddle secret anywhere in `soundboard/`); server-only secrets stay in Edge Function env. **Verify** `.gitignore` covers `debug.log`, `*.bak`, `auth_cache.json` and that `git ls-files` shows none tracked (Phase 1/A6 did the work). Add CI secret-scan (gitleaks/trufflehog) failing on detected secrets; scan full history once.
- [ ] **Step 11 — auth hardening:** confirm GoTrue Argon2/bcrypt; enable HIBP leaked-password check on signup + change; enable per-IP + per-email rate-limiting; enforce email verification before paid actions.
- [ ] **Step 12 — recordings:** confirm `audio.py` writes recordings LOCAL-ONLY (no upload path); show a one-time consent notice before first recording (link the recording-consent clause).

**✅ CHECKPOINT — Security self-review:** secret-scan over built `SoundBoard.exe` + `soundboard/` finds zero private/service/SMTP/Paddle secrets (only Ed25519 public key); CI secret-scan green and FAILS on a planted dummy key (verify once); a known-pwned password is rejected (HIBP) and rapid logins are rate-limited (429); fresh install records audio with no network egress (network monitor) and shows consent first; `git ls-files` shows no `debug.log`/`*.bak`/`auth_cache.json`.

---

## Phase 2 — Remote Control Plane (Update + Version Gating + Rollback)

> **BLOCKED BY:** Phase 1 shipped; migration's `app\` vs `workspace\` layout final; Phase 0 `/handshake` + embedded pubkey. Kill/version state reads ONLY `app.flags` (0.1).

### Step 1 — Releases & rollout DDL
- [ ] `0xx_app_releases.sql`: `app.app_releases(version pk, channel check in('stable','beta'), semver_sort, manifest_url, manifest_sha256, ed25519_manifest_sig, authenticode_thumbprint, artifact_url, artifact_sha256, notes, created_at, published bool default false)`.
- [ ] `app.release_channel(channel pk, current_version references app_releases, min_supported_version, updated_at)` rows `stable`/`beta`. (`min_supported_version`/`blocked_versions` mirror into `app.flags` so the signer/handshake read one home.)
- [ ] `app.rollout(channel, version, rollout_permille int default 0 check 0..1000, auto_halt bool, halt_reason, revert_rate_threshold numeric default 0.05, pk(channel,version))`.
- [ ] `app.version_pings(device_id uuid, user_id uuid null, version, channel, os_build, keys_supported text[], seen_at default now(), pk(device_id, seen_at))` — **`keys_supported` added (E-fix) so Phase 3 k2-adoption is computable**; index `(version, seen_at)`.
- [ ] RLS: releases/channel/rollout = `SELECT` to anon+authenticated; writes service-role. `version_pings` = `INSERT` to anon+authenticated, no `SELECT` for non-service.
- [ ] Rollout admin RPCs live in the **Phase 3 admin RPC migration** (`set_rollout`, `halt_rollout`, `disable_version` write `app.flags.blocked_versions`).

**✅ CHECKPOINT — Release DDL:** service-role insert into `app_releases` succeeds, anon write rejected; anon `select release_channel` returns rows, `select version_pings` denied; `set_rollout('stable','1.2.4',10)` updates `rollout_permille` only with admin/service creds.

### Step 2 — Enrich `/handshake` (signed)
- [ ] Accept `{device_id, current_version, channel}`; return `{latest_version, min_supported_version, blocked_versions[], rollout:{permille,in_cohort}, manifest_url, server_time, sig}`.
- [ ] `in_cohort = (first 8 bytes of sha256(device_id) → uint mod 1000) < rollout_permille`; document the exact algorithm in a comment.
- [ ] Sign the whole canonical-JSON payload (sorted keys, excluding `sig`) with the k1 key; include `server_time` (epoch) inside the signed body.
- [ ] Pull `latest_version`/`min_supported_version`/`blocked_versions` from `release_channel` + `app.flags`; if `current_version` blocked OR `< min_supported`, include `update_required:true`. Deploy.

**✅ CHECKPOINT — Signed handshake:** HTTPie returns all fields incl. `sig`+`server_time`; verifying `sig` against the embedded pubkey passes, flipping a byte fails; two `device_id`s at permille=10 give a deterministic repeatable split; permille=0 → `in_cohort=false` for all.

### Step 3 — Client version source + launch handshake
- [ ] Single version source: `soundboard/__init__.py:24` `__version__="1.2.3"`; `from soundboard import __version__ as APP_VERSION`.
- [ ] `license.py.verify_handshake(payload,sig)` (added in Phase 1/C3) + clock-tamper: persist last `server_time` into the **unified watermark** (D6) at `workspace\handshake_anchor.json`; if local clock < anchor beyond skew → hardened re-fetch, never extend grace.
- [ ] `api_client.get_handshake(device_id, channel)` on a worker thread → result via `root.after`. In `gui.py.__init__`, after session boot, schedule one launch handshake; store `self._handshake`.

**✅ CHECKPOINT — Launch handshake wired:** verified `self._handshake` within seconds, UI never hangs; offline launch fails gracefully (Free unaffected), logged once; rolling clock back 1 day triggers tamper path and does NOT extend grace.

### Step 4 — "Update required" hard block (version gating)
- [ ] In `main.py` BEFORE `SoundboardApp`, run a synchronous-with-timeout handshake; if `update_required`, show a modal "Update required" with an Update button (launches `updater.exe`) and **block** app entry; exit on click.
- [ ] **B4 policy (state explicitly):** if handshake unreachable AND no cached signed handshake proving `update_required` → **fail-open** (min-version enforcement requires at least one prior online handshake). Ship the `min_supported_version` floor inside the signed installer manifest so even a first-ever offline launch has a baseline. Cache last verified handshake to `handshake_anchor.json`.
- [ ] Soft path: not below min but `latest > current` and in cohort → non-blocking "Update available" chip in the header.

**✅ CHECKPOINT — Version gating:** set `min_supported_version='1.2.4'` → `1.2.3` build shows "Update required", app NOT reachable; lower it → launches; `disable_version('1.2.3')` (writes `app.flags`) → hard-block, remove → restores; tampered `handshake_anchor.json` fails verify and is ignored; **documented gap test:** blocked build, never-online, network pulled → launches (fail-open) unless the installer-manifest floor catches it.

### Step 5 — `/telemetry/version` ping + dashboard
- [ ] `telemetry-version`: `POST {device_id, version, channel, os_build, keys_supported}` → insert `version_pings` (rate-limit per device, validate `device_id` shape, no auth).
- [ ] Client `api_client.ping_version()` once on launch (worker, fire-and-forget) + on the 6–24h re-fetch cycle. Deploy + smoke.
- [ ] View `app.v_version_distribution` (`version, channel, count(distinct device_id) filter seen_at>now()-24h as dau_24h, max(seen_at)`); view `app.v_rollout_health` (revert = pinged NEW then OLD within 1h → `revert_rate`). Add `app.v_key_adoption` (% devices reporting `'k2' = any(keys_supported)`) for Phase 3/8.

**✅ CHECKPOINT — Telemetry + dashboard:** launching builds bumps `version_pings`; `v_version_distribution` shows current version with nonzero `dau_24h`; simulating `1.2.4`→`1.2.3` within an hour increments `revert_rate`.

### Step 6 — Auto-updater: `Launch.exe` + signed `updater.exe` + directory swap (**BLOCKED BY migration app\ layout + Installer/Step 9 ACL fix**)
- [ ] `updater/launcher/main.py` → frozen **`Launch.exe`** (never swapped): check `current` pointer / `swap.journal`; roll forward/back on incomplete swap; launch real `SoundBoard.exe` under `…\app\<version>\`.
- [ ] Layout: `…\app\<version>\` immutable per-version + `current` marker (file/junction) + `workspace\` (never swapped). Matches migration.
- [ ] `updater/updater/main.py` → frozen **`updater.exe`** (separately signed): read signed `/updates` manifest → **Ed25519 manifest verify**; download artifact → `artifact_sha256` check; **Authenticode verify** via `WinVerifyTrust` (BOTH must pass); stage into new `app\<new>\`; write `swap.journal {from,to,state:'staged'}`; atomically flip `current`; `state:'committed'`; relaunch via `Launch.exe`.
- [ ] **Crash-loop watchdog:** `Launch.exe` records attempts; N crashes in T seconds → auto-revert `current` to previous, quarantine new.
- [ ] **Mid-swap self-heal:** `staged` at next start → discard staged, keep old; `committed` but new dir corrupt → roll back.
- [ ] Runs with **NO admin** (per-user); confirm no UAC.

**✅ CHECKPOINT — Updater core + self-heal:** valid newer manifest+artifact → stages, flips, relaunches → new `__version__`, no admin prompt; `taskkill` mid-swap (`staged`) → next `Launch.exe` runs OLD cleanly; sha256 mismatch → abort; forged Authenticode → abort (even if Ed25519 ok); bad manifest `sig` → abort before download; force 3 startup crashes → watchdog reverts + quarantines.

### Step 7 — Signed update manifest per channel on Storage/CDN
- [ ] Manifest schema `{channel, version, semver, artifact_url, artifact_sha256, min_supported_version, notes, server_time}` + detached `manifest.sig` (k1). `/updates` Edge Function (or static Storage object) returns the signed manifest per `?channel=`; publish pipeline writes `releases/<channel>/manifest.json`+`.sig`. Bucket public-read for manifest+artifact; signing key server-side only. Verify updater fetches `releases/stable/manifest.json`, validates `sig`, and `manifest_sha256` in `app_releases` matches served file.

**✅ CHECKPOINT — Signed manifests:** `GET releases/stable/manifest.json`+`.sig` download, updater verifies OK; editing `manifest.json` without re-signing → rejected; `beta` manifest independent of `stable`.

### Step 8 — Staged rollout (1→10→50→100%) + auto-halt + instant abort
- [ ] Updater/handshake gate: update only if `rollout.in_cohort==true`; bucketing identical server-side (and any client recompute).
- [ ] Ramp via `set_rollout('stable',v,10)`→100→500→1000; runbook = watch `v_rollout_health` between steps.
- [ ] **Auto-halt:** pg_cron/Edge cron computes `revert_rate`; if `> threshold` → `halt_rollout(channel,version,'auto: revert-rate')` (`rollout_permille=0, auto_halt=true`).
- [ ] **Instant abort/rollback:** `set_rollout(channel,version,0)` removes non-updated devices from cohort; reverting `release_channel.current_version` rolls clients back next cycle.
- [ ] **Monotonic inclusion:** same `device_id` stays in-cohort as permille only increases (never updates then un-updates).

**✅ CHECKPOINT — Staged rollout & abort:** **stated N + tolerance:** generate 10,000 device_ids, at permille=10 assert in-cohort ∈ [80,120]; ramp 10→500 only ADDS devices; `set_rollout(...,0)` → no new `in_cohort=true`; reverting `current_version` returns clients to old version (verified by `__version__` + `version_pings`); forcing `revert_rate>threshold` auto-sets permille=0 (`halt_reason='auto: revert-rate'`).

### Step 9 — Release PUBLISH pipeline (**MERGE with Ops WS-A signing**)
- [ ] `.github/workflows/release.yml` on tag `v*`: build `SoundBoard.exe` + `Launch.exe` + `updater.exe`; CI asserts `git tag == __version__`.
- [ ] Update `soundboard.spec` hidden imports for new client modules (keyring.backends.Windows, win32ctypes.core, requests, nacl, packaging) once auth/license/api_client/updater ship; CI frozen-exe `--smoke-test`.
- [ ] **Invoke Ops WS-A signing job** to Authenticode-sign all three (`updater.exe` independently); `signtool verify /pa /v`.
- [ ] Compute `artifact_sha256`; build channel `manifest.json`; **Ed25519-sign** (CI secret, separate store from Authenticode); write `manifest.sig`.
- [ ] Upload artifact + `manifest.json` + `.sig` to `releases/<channel>/`; upsert `app_releases` (sha256s + thumbprint); set `rollout_permille=10`.
- [ ] Do NOT auto-set `release_channel.current_version` until rollout hits 100% (or manual promote).

**✅ CHECKPOINT — Publish pipeline:** tag `v1.2.4` → CI green; `signtool verify /pa` passes all three; smoke test launches+exits clean; `releases/stable/manifest.json`+`.sig` verify; `app_releases` row + `rollout_permille=10`; in-cohort dev device picks up `1.2.4` and reports it; out-of-cohort stays on `1.2.3`; tag≠`__version__` fails CI.

**✅ CHECKPOINT — PHASE 2 EXIT (remote control plane operational):**
- [ ] Publish a build to 1% and watch it in `v_version_distribution`.
- [ ] In-cohort device updates + relaunches cleanly; mid-swap `taskkill` self-heals via `Launch.exe`.
- [ ] Raising `min_supported_version` hard-blocks an old build; lowering restores.
- [ ] `set_rollout(...,0)` and/or reverting `current_version` instantly stops rollout + rolls clients back next cycle.
- [ ] Tampered manifest (bad Ed25519) OR forged-Authenticode artifact is REJECTED by `updater.exe` with no swap.

---

## Phase 3 — Remote Lockout + Moat + Compliance

> **BLOCKED BY:** Phase 2 telemetry (`v_key_adoption` for k2 gate) + Phase 0 signer. All destructive tests run on **staging**. Kill/version state = `app.flags`.

### Step 1 — DB: lock-mode + ban/flag columns
- [ ] `2025_p3_01_lock_state.sql`: `app.profiles` += `account_status text not null default 'active' check in('active','flagged','banned')`, `lock_mode text not null default 'normal' check in('normal','hardened')`, `flagged_reason`, `flagged_at`.
- [ ] `app.devices` += `revoked_reason`, `force_logout_at`.
- [ ] `app.subscriptions` += `chargeback_at`, `chargeback_ref`.
- [ ] Kill/version state stays in **`app.flags`** (no new `kill_switch` table — 0.1 decision).
- [ ] Indexes: `devices(user_id) where revoked_at is null`; `profiles(account_status) where account_status<>'active'`.
- [ ] RLS: lock/ban/flag columns service-role write; user `select` own `account_status`/`lock_mode` read-only.
- [ ] Apply against **staging**; `\d app.profiles` shows new columns.

### Step 2 — `/entitlements` signer: enforce lock state + hardened TTL
- [ ] Before signing, load `profiles.account_status`, `lock_mode`, `app.flags`. **Refuse re-sign** `403 {code:"LOCKED",reason}` when: `global_killswitch`, OR `account_status='banned'`, OR `device.revoked_at not null`, OR `app_version ∈ blocked_versions`, OR `app_version < min_supported_version`.
- [ ] Chargeback path (`account_status='flagged'`): **still sign** but `plan=free` (never hard-lock — EU/UK law).
- [ ] TTL: `normal` → `exp+7d, grace_days=14`; `hardened` → `exp+24h, grace_days=0, require_online=true`.
- [ ] Add `require_online`, `lock_mode` to claims; bump claim schema `v`.
- [ ] Honor `device.force_logout_at`: if token `iat < force_logout_at`, refuse re-sign (forces re-auth).

### Step 3 — Admin RPCs (**SINGLE OWNER — all admin RPCs live here**) + blast-radius preview
- [ ] `app.admin_ban_user`, `admin_revoke_device`, `admin_force_logout_all`, `admin_disable_version` (→ `app.flags.blocked_versions`), `admin_set_min_version`, `admin_global_killswitch`, `admin_set_lock_mode`, plus rollout RPCs from Phase 2 (`set_rollout`, `halt_rollout`) and ops RPCs (`grant_comp`, `free_seat`, `resend_verify`, `lookup_by_email`). All `security definer`, write `audit_log`.
- [ ] `app.admin_preview(p_action, p_target)` (read-only) → `{affected_users, affected_devices, active_sessions_est, sample_emails[5]}` (e.g. `disable_version` counts distinct `device_id` last-seen on that version from `version_pings`).
- [ ] Guard: every admin RPC checks `app.admins` allowlist → else `raise 'forbidden'`.
- [ ] Admin tool (Ops WS-D) requires a preview call + typed confirmation before mutate.

### Step 4 — Client: enforce hardened mode + lock responses
- [ ] `license.py`: parse `require_online`/`lock_mode`/`grace_days`; when `require_online`, **fail closed** if no online re-fetch within `exp` (no grace).
- [ ] `api_client.py`: handle `403 {code:"LOCKED"}` → surface reason, no retry-loop (backoff).
- [ ] `gui.py._auth_tick`: on LOCKED, drop to Free (or "Update required" modal for version locks) on main thread via `root.after`; never crash.
- [ ] When `require_online`, ignore local-clock grace entirely (server is truth).

**✅ CHECKPOINT — Hardened-mode lockout:** `set_lock_mode(user,'hardened')` → next `/entitlements` `exp=+24h, grace_days=0, require_online=true` (decode token); banned/revoked/blocked → `403 LOCKED` immediately online, ≤24h for an already-offline hardened client (≤7d normal); `force_logout_all` → next re-sign refused until re-auth (token `iat<force_logout_at` rejected); every admin action wrote `audit_log`; preview counts == actual ±0. **Test hardened TTL by mocking `exp` / waiting, NOT by advancing the wall clock (that's the tamper trigger).**

### Step 5 — Device-management UI
- [ ] `/devices`: `GET` (device_id, name, os, last_seen, `current` flag, revoked), `DELETE /{id}` (self-revoke), `POST /sign-out-everywhere` → `force_logout_all(self)`.
- [ ] `devices_ui.py` panel from account chip: device table, "This device" badge, per-row Revoke, Sign-out-everywhere; revoke confirmation; if revoking current device → local logout. Seat-cap UX prompts "revoke one to continue" at 3/3 (ties into Phase 1/D2). Worker-thread fetch → `root.after`; offline/stale banner.

**✅ CHECKPOINT — Device management:** list shows non-revoked devices with correct "current" flag; revoking a device denies its next online re-sign within the window; sign-out-everywhere forces re-auth on all; revoking current device logs out locally.

### Step 6 — Cloud config sync (anti-sharing moat; Pro-gated)
- [ ] `app.config_blobs(user_id pk, blob jsonb, version bigint default 1, device_id, content_hash, updated_at)`; RLS `user_id=auth.uid()`.
- [ ] `/config/get` (blob + version + hash); `/config/put` (optimistic concurrency: client sends `base_version`; mismatch → `409 CONFLICT` with server blob).
- [ ] `sync.py`: serialize `soundboard_config.json` → canonical JSON → `content_hash`; pull on login, push on save (debounced), background re-pull on re-fetch tick.
- [ ] `409` conflict prompt: "Keep local / Keep cloud / View diff"; keep-local re-puts new `base_version`; keep-cloud overwrites local (**back up to `*.bak` first**). Never auto-merge; never push from anonymous/Free; **config only, never recordings**.

**✅ CHECKPOINT — Cloud sync:** push→pull on a 2nd device reproduces config byte-for-canonical-identical; stale push → `409` resolved via keep-local/keep-cloud (never silent merge); local `*.bak` created before any overwrite; recordings never uploaded.

### Step 7 — GDPR: export + delete (cascade + Paddle cancel)
- [ ] `/account/export`: signed time-limited JSON of all `app.*` rows for `auth.uid()`; audit it.
- [ ] `/account/delete` saga: (1) **Paddle cancel subscription** + record result; (2) delete `config_blobs, license_tokens, entitlements, devices, subscriptions, webhook_events(user-scoped), profiles`; (3) **GoTrue admin deleteUser** (cascades via Phase 0/Step 3 FKs); (4) audit tombstone (minimal legal record, no PII). **If Paddle cancel fails → do NOT delete auth row; retryable error + Sentry alert** (never orphan a billable sub).
- [ ] Client account panel: "Export my data" + "Delete account" (double-confirm, type email); offer "delete local recordings now". On delete success, wipe `workspace` secrets/cache (`auth_cache.json`, keyring creds, license token) + logout.

**✅ CHECKPOINT — GDPR delete/export:** post-delete every `app.*` table for the uid returns 0 rows, GoTrue `getUser` 404s, Paddle shows sub canceled, local creds/cache/keyring cleared, audit tombstone present; if Paddle cancel fails the delete aborts (no orphan billing); export returns complete valid JSON and is audited.

### Step 8 — Ed25519 key rotation (k1 → k2), zero-break
- [ ] Generate **k2** offline; record `kid=k2`.
- [ ] **Client first:** embed BOTH k1+k2 public keys; `license.py` selects verifier by token `kid` (accept k1 OR k2). Ship; confirm reach via `app.v_key_adoption` (uses `keys_supported` from Phase 2/Step 5).
- [ ] Gate server cutover on **≥95% k2-capable** (computable now); do NOT sign k2 before clients have it.
- [ ] **Server second:** signer emits `kid=k2`, signs k2; keep k1 acceptance window. Retire k1 signing after telemetry shows ~0 k1 tokens; later client drops k1 public. Feature-flag `signing_kid` for one-flip rollback.

**✅ CHECKPOINT — Key rotation:** clients verify both k1+k2 by `kid`; server signs k2 only after ≥95% `v_key_adoption`; during overlap both verify; flipping `signing_kid` back to k1 instantly restores valid entitlements; no Sentry error spike across cutover.

### Step 9 — Chargeback webhook → free + flagged (no hard-lock)
- [ ] `/billing/webhook` handles Paddle `subscription.payment_disputed`/chargeback; verify `Paddle-Signature`; idempotent via `webhook_events`. On chargeback: `recompute_entitlement` → sub inactive, `account_status='flagged'`, `flagged_reason='chargeback'`, `chargeback_at/ref`, **`lock_mode='hardened'`** but plan resolvable to **free** (never `banned`). On reversal → clear to `active`/`normal`. Audit + Sentry alert.

**✅ CHECKPOINT — Chargeback handling:** chargeback event → `free` + `flagged` + `hardened`; user retains Free (never `403 LOCKED` solely for chargeback); next entitlement is 24h/grace-0/online-required; webhook signature-verified + idempotent; reversal restores `active`/`normal`.

### Step 10 — Phase 3 integration regression
- [ ] E2E (HTTPie + lab client) on **staging**: ban → revoke → force-logout → version-disable → killswitch, each verified against blast-radius preview.
- [ ] Full GDPR delete on a paid user with active sync + Paddle sub → total purge.
- [ ] Key-rotation dry run with mixed k1/k2 clients.
- [ ] Cloud-sync conflict matrix (local-newer / cloud-newer / equal) all resolve.
- [ ] CI: frozen-exe `--smoke-test` still launches; `requirements.txt` + `soundboard.spec` hidden imports updated for new modules (`sync.py`, `devices_ui.py`).

**✅ CHECKPOINT — PHASE 3 COMPLETE:** every Step 1–9 checkpoint passes; ban/revoke denies next re-sign within window (immediate online, ≤24h hardened); force-logout works; deleted accounts fully purged + Paddle canceled; key rotation completes zero-break, no Sentry spike; cloud sync round-trips without clobbering local; chargebacks degrade to Free+flagged+hardened without hard-locking.

---

## Launch readiness — Definition of Done

Tick every box; this is the master gate before public launch.

**Signing & build**
- [ ] Azure Trusted Signing profile **Approved**; `signtool verify /pa` passes on `SoundBoard.exe`, `Launch.exe`, `updater.exe`, `Setup.exe`; clean-VM shows verified publisher, no "Unknown Publisher".
- [ ] CI build-sign job green; goes RED on any unsigned artifact; frozen-exe `--smoke-test` exits 0.

**Pay flow & entitlements**
- [ ] Register → checkout → pay (Paddle, switched from sandbox to **production** keys) flips `entitlements.plan='pro'`; webhook idempotent.
- [ ] `GET /entitlements` returns an Ed25519 bundle the client verifies **offline**; device-bound; grace + clock-tamper enforced; the 4 Pro actions gate on `features[]`.

**Remote control plane**
- [ ] Publish to 1% → ramp → 100% works; `set_rollout(...,0)` / revert `current_version` rolls clients back; mid-swap kill self-heals.
- [ ] `min_supported_version` / `disable_version` (via `app.flags`) hard-block old/blocked builds; tampered/forged-signature updates rejected.
- [ ] Kill-switch, ban, revoke, force-logout deny the next re-sign in the expected window; hardened mode = 24h/grace-0/online.

**Installer & virtual mic (clean VM)**
- [ ] One signed `Setup.exe` installs app + VB-CABLE with one VB-Audio prompt + one UAC; wizard auto-routes + passes the 48 kHz pre-flight; loopback capture on `CABLE Output` asserts non-silence; uninstall leaves no orphan driver; non-admin `updater.exe` can write `app\`.

**Legal, email, observability, compliance**
- [ ] `/terms`, `/privacy`, `/refunds` live + in-app; acceptance recorded in `app.legal_acceptances`; ToS has major-version + recording-consent clauses.
- [ ] Auth email from `mail.<domain>` passes SPF/DKIM/DMARC; bounce webhook suppresses.
- [ ] Sentry (server + opt-in PII-scrubbed client) + uptime + webhook-5xx/insert-rate alerts page you ≤5 min.
- [ ] `/account/export` + `/account/delete` work (Paddle cancel + full cascade); chargebacks degrade to Free+flagged+hardened (no hard-lock).
- [ ] Key rotation path (k1→k2, client-first, ≥95% gate) validated in staging.

**Security & hygiene**
- [ ] Client ships ONLY the Ed25519 public key — secret-scan over built exe + source finds zero private/service/SMTP/Paddle secrets; CI secret-scan green (and FAILS on a planted key).
- [ ] HIBP + rate-limiting + email-verification-before-paid enforced; recordings LOCAL-ONLY with consent notice; no network egress on record.
- [ ] `git ls-files` shows no `debug.log`/`*.bak`/`auth_cache.json`/`__pycache__`; all user data + logs under `%LOCALAPPDATA%\LocalSoundBoard\workspace`.
