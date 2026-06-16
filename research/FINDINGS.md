# Philips Home Access (com.conex.philips) — reverse-engineering map

> Status: **MAP ONLY** (paused before full algorithm reverse, per request).
> Source APK pulled from device via adb. Line numbers below refer to
> `bundle.decompiled.js` (hermes-dec output of `assets/index.android.bundle`).

## App / stack
- Package `com.conex.philips`, **v4.14.0** (versionCode 202508430), minSdk 26 / targetSdk 35.
- **React Native + Hermes** (HBC bytecode v96). App logic lives in the Hermes
  bundle, not the DEX. DEX is RN bridge/glue (not yet decompiled — jadx pending).
- Transports in use: **HTTP/REST (axios)**, **MQTT** (`mqtt.js`), **BLE**
  (custom framed protocol), **PPPP/CS2 P2P** (`libPPCS_API.so`, for video).
- Storage: **SQLCipher** (`libsqlcipher.so`), **MMKV** (`libmmkv.so`).
- Crypto building blocks:
  - **JSEncrypt-style RSA** module (`bundle.decompiled.js` ~914500–928670):
    `setPrivateKey` / `setPublicKey` / `sign256`, PEM `-----BEGIN RSA PRIVATE KEY-----`.
  - **CryptoJS** (HmacSHA256 @918661, PBKDF2 @921429).

---

## 1. HTTP / REST  (primary focus)
- Single **axios** instance with interceptors; `baseURL` set from **region
  config** at runtime (`validateBaseUrl` @56365, baseURL wiring ~942457–943177).
  No hardcoded host literal — get the real host from the proxy capture
  (your `idlespacetech` backend). Region context seen: `northAmerica`.
- API client object exposes typed methods (region ~178100–180300), e.g.:

  | Method | Path |
  |--------|------|
  | getDeviceList        | `POST /homeaccess/device/list` |
  | (account)            | `/homeaccess/account` |
  | findAllBindDevice    | `/app/user/findAllBindDevice` |
  | postPushSwitch       | `/philips/user/edit/postPushSwitch`, `/app/user/edit/postPushSwitch` |
  | downThingsDocument   | `GET /iot/thingmodel/instance/{esn}/document/get?version=` |
  | getChannelModePlan   | `POST /v3/device/get-channel-mode-plan-time` |
  | qrcode token         | `/app/qrcode/token`, `/app/qrcode/token/verify` |
  | BLE session key      | `/iot/ble/session-key/pre-create/`, `/iot/ble/session-key/post-create/` |

### Request signing (THE replay-killer for HTTP)
Example: `getChannelModePlan` @178708 (`Ht`):
```js
reqTime = Date.now()
sign    = rsa.sign256( JSON.stringify({ reqTime, wifiSN: esn }), <privateKey @slot21> )
POST /v3/device/get-channel-mode-plan-time
body: { wifiSN: esn, sign, reqTime }
```
- Signed device endpoints carry **`sign` = RSA-SHA256 over a JSON payload that
  INCLUDES `reqTime` (fresh ms timestamp)**. Other sign sites: @179099, @179166.
- **Why resending the same body fails:** `reqTime` is inside the signed blob.
  A captured `{sign, reqTime}` is only valid briefly — the server rejects stale
  timestamps (and likely also requires a valid Bearer token). To replay you must
  re-sign with a current `reqTime`, which needs the private key.
### Authorization (RESOLVED)
Built by the per-region axios factory (~1298420–1298480). Each region registers:
- `baseURL`  ← `storage.getItem('<region>Addr')`   e.g. `northAmericaAddr`
- header **`token`** ← `storage.getItem('<region>Token')`  e.g. `northAmericaToken`
- header `k-tenant: 'philips'`, `k-version: '4.0.0'`, `k-signv: '1.0.0'`,
  `k-language` ← `storage.getItem('appLanguage')`

So auth is a **custom `token` HTTP header** (NOT `Authorization: Bearer`). The
token is the **`networkToken`**, obtained by `requestNetworkToken` (@174189,
@980400) — that's the login call to capture in the proxy. Base URL + token both
live in on-device storage (MMKV/AsyncStorage) under region-keyed names, so they
can also be dumped straight from the device.

### Signing key (RESOLVED)
- The `sign256` key is a **static RSA-1024 private key hardcoded in the bundle**
  (`_closure1_slot21` @178431, PKCS#8 base64 `MIICdw...`). **Same for every
  install — NOT per-device.**
- `sign256` = JSEncrypt **`signSha256`** (@914547) = **RSASSA-PKCS1-v1.5 over
  SHA-256, base64-encoded**, computed over `JSON.stringify(payload)` (field order
  matters).
- => HTTP request signing is **fully reproducible offline**. Reimplemented and
  verified in `../signing.py` (self-test signs + verifies against the key).

---

## 2. MQTT  (secondary — relevant for a future local / cloudless HA path)
- `mqtt.js` bundled (`createMQTTClientManager`). Not yet mapped: broker host,
  auth (likely token/cert), topic scheme, and whether message payloads are
  encrypted or just TLS-wrapped. Worth tracing if you want push/state without
  cloud polling.

## 3. BLE  (local channel — proxy can't see it)
- Custom binary framing: **`createBleFrame`** @970040. 16-bit header packs:
  `contentType(4) | fragment(1) | cryptoType(2) | padding(4) | ext1 | ext2 | reserved` + checksum.
  Header parser @969981.
- **`cryptoType` enum** @972193: `{none:0, secretKey:1, kb:2, sessionKey:3}` —
  selects which key encrypts the payload. `ENCRYPT_FAIL_CAUSE_NO_KEY` @970080.
- **BLE `sessionKey` is negotiated through the cloud**:
  `/iot/ble/session-key/pre-create/` @178445 + `/post-create/` @178476.
  => fully offline/local BLE control is hard without replicating this handshake.

---

---

## HTTP capture analysis (HTTP Toolkit export, 2026-06-14)
31 real requests captured (app launch + lock open/close). Verified against the
recovered key. See `../signing.py`, `../explore.py`, and the `apk/_*.py` scripts.

### Confirmed
- **Host:** `https://api.idlespacetech.com` (also `app-sg.cone-x.com`,
  `things.idlespacetech.com`). Region = North America.
- **`token` header** = opaque **base64url JSON claims** (NOT a signed JWT):
  `{uid, unionId, _id, iss:auth.irevolohome.com, sub:<email>, aud, exp, nbf,
  iat, jti}`. ~2h lifetime. It's a live session credential -> treat as secret.
- **Signing rule (verified on all 21 signed requests, 0 fail):**
  `sign = base64( RSA-PKCS1v1.5-SHA256( JSON.stringify(sortKeysAsc({...params, reqTime})) ) )`
  with `reqTime` = ms epoch **as a string**, compact JSON, keys sorted ascending,
  signed by the static embedded RSA-1024 key. Reproducible offline.
- **Other headers:** `k-tenant: philips`, `k-version: 4.14.0`, `k-signv: 1.0.0`,
  `k-language: en_US`, UA `System: Android 10/ Model: ONEPLUS A6010/ Brand: OnePlus`.

### Endpoint inventory
| Type | Endpoints |
|------|-----------|
| signed | `/v3/app/function-List`, `/v3/app/get-newest-version`, `/v3/user/check-user-token`, `/v3/user/notification/{agree,hint}`, `/v3/user/upload/device-token`, `/v3/user/part/list`, `/v4/device/query-device-attr`, `/v4/device/query-device-versioninfo`, `/v3/device/dtim-wake` |
| plain  | `/homeaccess/device/list` (body `{uid}`), `things.idlespacetech.com/api/v1/product/model` |
| **encrypted** | **`/v3/device/open-device`** (unlock), **`/v3/device/close-device`** (lock) |

### Lock commands -- encryption (RESOLVED via DEX / jadx)
The open/close logic is in the **Kaadas lock SDK in the DEX** (Java, classes3/5),
not the RN bundle. Decompiled with jadx; crypto helper is `ld5` (uses hutool RSA).

- Endpoints (Retrofit `p90.java`): `POST /v3/device/open-device` (unlock),
  `POST /v3/device/close-device` (lock). Body = `SetLockOpenCloseReq` {esn,
  userNumberId}; header `encrypt_data: encrypt_data`. Caller `lf7.C(esn, i2, i3)`:
  i2==1 -> open else close; i3 = userNumberId (admin = 0, from /v3/user/part/list).
- Request pipeline (OkHttp interceptor `wp1`):
  1. add `reqTime` (ms string) to params
  2. canonicalize with **sorted keys**
  3. `sign = ld5.f(sortedJson)` = SHA256withRSA, private key A == our `sign256`
  4. `encryptData = ld5.a(signedJson)` = **RSA/ECB/PKCS1 encrypt with embedded
     SERVER public key B**, 117-byte blocks, base64 (-> 3 blocks = 384 bytes)
  5. body = `{"encryptData": ...}`
- `ld5` embeds **4 RSA-1024 keys**: A = our signing/response-decrypt PRIVATE key;
  **B = server PUBLIC key for request encryption** (its private half is server-
  side -- `pub(A) != B`, so captured commands are NOT decryptable by us, and the
  earlier "decrypt" was garbage); C = 2nd private key; D = 2nd server public key
  (used by the `xp1` "physical" interceptor with x-app-id/x-sign).
- Responses are encrypted too: `ld5.c(encryptData)` decrypts with private key A
  (we CAN read those).
- **We can fully BUILD open/close** (we hold pub B). Implemented in
  `../signing.py` (`encrypt_for_server`, `lock_command_body`) and
  `../client.py` (`post_encrypted`); structural output matches the capture
  (512 b64 / 384 bytes). Only true verification = actuating the lock.

## Token acquisition / login (RESOLVED -- 2nd capture, full sign-in)
Login is NOT OIDC/PKCE and NOT on the JWT issuer host. It's a plain
email+password POST to the **Juzi Wulian ("Oneness") backend**.

Routing (both unauthenticated, host `user-oneness.juziwulian.com`):
- `POST /user/region`  -> countries -> datacenterCode (areaCode 61 -> PhilipsSingapore acct)
- `POST /datacenters`  -> datacenter map:
  - PhilipsNorthAmerica -> `https://api.idlespacetech.com:443/`, ws `wss://ws.idlespacetech.com`
  - PhilipsSingapore    -> `https://app-sg.cone-x.com/`, mqtt `mqtt-sg-app.cone-x.com:5883`
  - PhilipsOneness      -> `https://user-oneness.juziwulian.com/`

Login:
```
POST https://user-oneness.juziwulian.com/homeaccess/oauth/login
headers: reqSource: app, timestamp: <epoch s>, lang/language: en_US, UA: okhttp/4.10.0
body:    {"identifier": <email>, "credential": <password>, "areacode": "61"}
resp:    data.users[] = one {uid, token, code} per datacenter
```
- The **PhilipsNorthAmerica** entry's `token` is the base64url-JSON token used as
  the `token` header against api.idlespacetech.com. (Oneness/Singapore return a
  different opaque/encrypted token format.)
- No refresh token; tokens last ~2h (exp-iat = 7200). Renew by re-logging in.
- Login itself is unsigned (no `sign`); credential is sent in cleartext over TLS.
- **Rejected/expired token** -> HTTP 200 with body `{"code":"444","msg":"Not
  logged in"}` (code is the string "444"). The client auto-detects this and
  transparently re-logs-in + retries (verified). Note: `/homeaccess/device/list`
  is keyed on `uid` and is NOT token-gated -- it still returns data with a dead
  token, so don't use it to test auth.

Implemented in `../auth.py`:  `python auth.py` (login), `show`, `ensure`.
Creds come from env (`PHILIPS_IDENTIFIER`/`PHILIPS_CREDENTIAL`), never committed.

## Realtime events (RESOLVED via DEX + live test)
NA datacenter uses a **WebSocket** (not MQTT; MQTT is the Singapore datacenter).
From `WebSocketService` (classes3.dex) and confirmed live in `../realtime.py`:
- URL: `wss://ws.idlespacetech.com/?client_id=app:<uid>` (trailing slash matters)
- Auth: handshake header `Sec-WebSocket-Protocol: <token>` (same token as HTTP)
- Keep-alive: **WS PING control frames** every 5s (NOT a JSON heartbeat)
- No login/subscribe frame is sent; the server pushes events for the account.
- The cloud pushes to OUR session too (multi-session works for app-initiated cmds).

Event frames (text JSON), decoded from live tests:
- `func:"setLock"` = command result. `body.params.dooropt` **1 = unlock, 0 = lock**.
- `func:"wfevent"`, `eventtype:"record"` = state-change log entry (the meaningful
  signal). Fields: `eventCode`, `eventSource`, `userID`, `appID`.
- `func:"wfevent"`, `eventtype:"action"` = full lock-state snapshot (config dump).
- Correlates with device/list `openStatus`: **1 = locked, 2 = unlocked**
  (`openStatusTime` = epoch of last change).

**Manual operations DO push events** (an earlier test missed them only because of
a broken JSON heartbeat + missing-slash URL). Remote vs manual is distinguishable
from the `record` event:
- **App/remote**: `eventSource:8`, `userID:0` (slot), `appID:2`;
  `eventCode` **1 = locked, 2 = unlocked**.
- **Manual/local**: `eventSource:9` or `255`, `userID:255` (0xFF = no app user),
  `appID:0`; `eventCode` **9 = unlocked, 8 = locked** (manual code space).

So the WebSocket alone tracks ALL lock activity (remote + physical) and attributes
each change to remote-vs-manual and to a user slot. A periodic device/list poll is
still a reasonable belt-and-suspenders fallback for HA, but not required for
manual changes.

## Tooling produced
- `index.android.bundle` — extracted Hermes bytecode (HBC v96).
- `bundle.decompiled.js` — 55 MB pseudo-JS (hermes-dec). Grep this for logic.
- `_scan_strings.py` — quick string bucketer.
- (pending) jadx on the 6 `classes*.dex` for the native bridge / Java crypto.

## Suggested next steps (pick when resuming)
1. **HTTP:** resolve the signing key source (above). If per-device, dump it from
   the device (MMKV/SQLCipher) so the Python tool can re-sign requests.
2. **HTTP:** confirm the login/token endpoint from a proxy capture; wire it into
   `../auth.py`.
3. **MQTT:** map broker + topics + payload crypto for a cloudless HA bridge.
4. **BLE:** only if you want local control — reverse the full frame + the
   secretKey/kb/sessionKey derivation.
