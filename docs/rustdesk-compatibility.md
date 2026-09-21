# RustDesk client compatibility matrix

This document tracks how well this server's RustDesk-protocol endpoints
match what real RustDesk desktop clients actually send and expect.

**Status: partially verified against a real client.** On 2026-09-18, a real
RustDesk desktop client (Windows, version 1.4.9) was pointed at a local
instance of this server (`API Server` field set to `http://<host>:<port>` -
note this field requires an explicit `http://` scheme, unlike the `ID
Server`/`Relay Server` fields which take bare `host:port`). Findings below
marked "**Confirmed**" come directly from that session; everything else is
still the original assumption from the community reference project named
in `CLAUDE.md` section 73, unverified.

The client release shown on the Dashboard as "written against the RustDesk X client source" is
`RUSTDESK_CLIENT_SOURCE` in `src/rustdesk_api/buildinfo.py` (currently 1.4.9). Change it when endpoints have been
re-checked against a newer client's source, not merely because a newer client exists.

| RustDesk client | Endpoint(s) | API compatibility | Tested against real client |
| ---------------- | ----------- | ------------------ | --------------------------- |
| 1.4.9 (Windows)  | `/api/login`, `/api/heartbeat`, `/api/sysinfo`, `/api/currentUser` | Confirmed compatible | Yes (2026-09-18) |
| 1.4.9 (Windows)  | `/api/ab` (GET+POST) - the *legacy* address book | Confirmed compatible (envelope + persistence verified end to end) | Yes (2026-09-18) |
| Source-derived   | `/api/ab/personal`, `/api/ab/settings`, `/api/ab/shared/profiles`, `/api/ab/peers`, `/api/ab/tags/{guid}`, `/api/ab/peer/{add,update}/{guid}`, `/api/ab/peer/{guid}`, `/api/ab/tag/{add,rename,update}/{guid}`, `/api/ab/tag/{guid}` - the newer per-item address book | Implemented from the client source (2026-09-20); `ADDRESS_BOOK_LEGACY_MODE=true` falls back to the legacy book | **No** - see "Address book, newer per-item protocol" |
| 1.4.9 (Windows)  | `/api/device-group/accessible`, `/api/users` (list), `/api/login-options` | Implemented from the client source (2026-09-21); an earlier string-encoded `data` left the tab empty - see the section on the Accessible devices tab | Paths confirmed via 404 capture; no error seen after implementing, not otherwise verified |
| Source-derived   | `/api/audit/conn`, `/api/audit/file`, `/api/audit/alarm` | Implemented from the client source (paths + payloads); see "`/api/audit/alarm`" for its response shape | **No** - needs a second machine connecting in (alarms: one that gets refused) |
| Source-derived   | `GET /api/audit/conn/active`, `PUT /api/audit` (connection notes; the older client posts `{id, session_id, note}` to `/api/audit/conn`) | Implemented from the client source (2026-09-21) - see "Connection notes" | **No** - needs the controlling client signed in with "Ask for note at end of connection" on |
| Source-derived   | `POST /api/oidc/auth`, `GET /api/oidc/auth-query` (the client's "Continue with ..." button) and our `GET /api/oidc/callback` | Implemented from the client source (2026-09-21) - see "OIDC sign-in" | **No** - only tested against an in-process provider; needs a real provider and a client |
| Source-derived   | Heartbeat `conns` / `disconnect`, `modified_at` / `strategy`; `POST /api/devices/cli` (`--assign`); `preset-*` keys in sysinfo; the two-step 2FA `/api/login` | Implemented from the client source (2026-09-20) - see "Heartbeat: connections and strategies", "`--assign`", "Two-factor login" | **No** - the 1.4.9 client has all of it; not yet exercised live |
| Android (version not recorded) | `/api/login`, `/api/heartbeat`, `/api/sysinfo` | Works, but the client reports only while its service is running - see "Android reports only while its service is running" | Yes (2026-09-21), one device |
| 1.4.9 (Windows)  | `/api/logout` | Implemented, not exercised in this session | No |
| 1.4.9 (Windows)  | `/api/peers` (list, same query shape as `/api/users`) | Implemented from the client source (2026-09-21) - reference project's `peers` view is a non-functional stub | Path/method/query confirmed real via live 404 capture ("Błąd odświeżania grup" / "Error refreshing groups" shown in the client's Available Devices panel); fix not yet retested live

## Confirmed endpoints

### `POST /api/login` - **Confirmed**

Request body captured from the 1.4.9 client (password redacted; the id, uuid and machine name replaced with examples):

```json
{
  "username": "admin",
  "password": "***",
  "id": "123456789",
  "uuid": "M2Y5YzJhNzEtNmI0ZC00ZTA4LWE1ZDMtOWMxZTdiMjBmODQ2",
  "autoLogin": true,
  "type": "account",
  "deviceInfo": {"os": "windows", "type": "client", "name": "desktop-example"}
}
```

This matches `RustdeskLoginRequest` field-for-field. `deviceInfo` is
accepted but currently unused - it duplicates `hostname`/`platform` that
`sysinfo` already reports, so there's no immediate need to read it.

**No version, no User-Agent (observed with 1.4.9).** The login body has no
client version, and the stored client sessions show the client sends no
`User-Agent` header at all. So the activity feed's "logged in ... using RustDesk
client `<version>`" takes the version from the device record: the `version` that
same device (`id` + `uuid` from the login body) last reported in `/api/sysinfo`.
It is looked up before the login registers the device, so it is absent on a
device's very first login and whenever the `uuid` does not match. Self-reported,
display-only.

The client sent no `Authorization` header on this request (expected - it
doesn't have a token yet). Login succeeded (HTTP 200) with valid
credentials against an account created via `POST /api/v1/auth/setup`, and
the client proceeded past its login screen. **Still unverified**: the
failure-path assumption (HTTP 200 + `{"error": ...}` for bad credentials,
rather than a 4xx) - this session only exercised the success path.

**New finding - `uuid` is base64-encoded**: the client's `uuid` field is
not a raw UUID string. `M2Y5YzJhNzEtNmI0ZC00ZTA4LWE1ZDMtOWMxZTdiMjBmODQ2`
base64-decodes to `3f9c2a71-6b4d-4e08-a5d3-9c1e7b20f846` (a standard UUID).
The server currently stores whatever string arrives, verbatim, as an
opaque identifier - this still works correctly for uniqueness/lookup
purposes, but `Device.uuid` in the database will contain a base64 blob,
not a human-readable UUID. If a future feature wants to *display* this
value meaningfully, it should base64-decode it first.

### `POST /api/heartbeat` - **Confirmed**

Real request body, sent repeatedly (~every 15s) with **no `Authorization`
header even after the client is logged in**, confirming the original
assumption that heartbeat is unauthenticated:

```json
{"id": "123456789", "modified_at": 0, "uuid": "M2Y5YzJhNzEtNmI0ZC00ZTA4LWE1ZDMtOWMxZTdiMjBmODQ2", "ver": 1004090}
```

**New fields not previously modeled** (both accepted and ignored today via
Pydantic's default "ignore extra fields" behavior - no validation error):

- `modified_at` (int, seen as `0`) - likely a config/address-book sync
  version the client tracks locally; not needed for basic heartbeat.
- `ver` (int, seen as `1004090`) - an internal protocol/build version
  number, not the same string as `sysinfo`'s `version: "1.4.9"`. Exact
  encoding not reverse-engineered; not currently needed.

Response `{"data": "OK"}` was accepted without any client-side error. For a
device this server does not know (fresh database, or an administrator deleted
it) the response also carries `"sysinfo": true`, which is the client's own way
of asking it to send its system info again (`rsp.remove("sysinfo")` in
`hbbs_http/sync.rs`) - needed because `/api/sysinfo` is now acknowledged (see
below), after which the client would not otherwise send it again.

### `POST /api/sysinfo` - **Confirmed**

**Response (changed 2026-09-20): the plain text `SYSINFO_UPDATED`**, not JSON.
The client compares the body with exactly that string. `"ID_NOT_FOUND"` makes
it upload again on the next heartbeat; anything else (including the
`{"data": "ok"}` we returned before) leaves it in the "not uploaded" state, so
it re-posted the same data every ~2 minutes (120 s `UPLOAD_SYSINFO_TIMEOUT` +
its 3 s tick; `POST /api/sysinfo` at 2m03s intervals in the dev-server log).
After `SYSINFO_UPDATED` the client sends sysinfo again only when its username,
device id or API server changes, when the process restarts, or when a heartbeat
response asks for it (`"sysinfo"` key, see above). Consequence for the WebUI:
metadata such as a renamed host now refreshes on the client's next restart or
username change rather than within two minutes; a new client version is
reported at its restart anyway.

Side effect, source-checked against client `master`: answering it sets the
client's internal "Pro server" flag (`hbbs_http/sync.rs`, `PRO`), which the
open-source client reads in exactly two places. (1) `ipc.rs` then reports
`hide_cm` to the connection-manager window, so that window hides itself when
`allow-hide-cm` is enabled *and* approval is by password *and* only a
permanent password is used; the desktop settings checkbox for it is commented
out, so it takes a config/preset option to reach this. (2) On Windows the
"connection manager failed to start" error dialog is suppressed
(`connection.rs`, `try_start_cm_ipc`). Nothing else in the client UI depends
on it. Not exercised against a real client beyond reading the source.

Request body captured from the 1.4.9 client (id, uuid, hostname and user name replaced with examples):

```json
{
  "cpu": "AMD Ryzen 9 5900X 12-Core Processor, 3.61GHz, 24/12 cores",
  "hostname": "desktop-example",
  "id": "123456789",
  "memory": "63.91GB",
  "os": "windows / Windows 11 IoT Enterprise LTSC 2024 - 11 (26300)",
  "username": "alice",
  "uuid": "M2Y5YzJhNzEtNmI0ZC00ZTA4LWE1ZDMtOWMxZTdiMjBmODQ2",
  "version": "1.4.9"
}
```

**Source-verified payload (2026-09-20, `get_sysinfo()` in `src/common.rs` +
`src/hbbs_http/sync.rs`):** the client builds exactly `cpu`, `memory`, `os`,
`hostname` and (when known) `username`, plus `id`, `uuid` and `version`.
`cpu` is `"<brand>, <GHz>GHz, <logical>/<physical> cores"` (so `24/12` on a
12-core/24-thread CPU - threads first), and `os` is `"<distribution id> /
<long OS version>"` with `" - <build>"` appended on Windows. **No CPU/OS
architecture (x86-64, arm64, ...) is sent anywhere** - not in `sysinfo`,
`heartbeat` or `login` - so the WebUI cannot show a real architecture, and
deliberately does not guess one from the CPU brand string.

All field names match the implementation. `cpu`/`memory` are stored verbatim
on `Device` (2026-09-18, matching the reference project's own approach of
persisting them unparsed) and shown in the WebUI's device list/detail views.
The core count in `cpu` is `<logical>/<physical> cores` - the 5900X above has
12 cores / 24 threads and is reported as `24/12` - so the WebUI reformats it
for display as `12 cores / 24 threads` (and spaces units: `3.61 GHz`,
`63.91 GB`) without altering the stored value (`fmtCpu`/`fmtMemory` in
`web/static/js/app.js`).
Sent with no `Authorization`
header in this session (the client apparently calls `sysinfo` before or
independently of login), so the registered device stayed unowned - matches
documented behavior.

**New finding - `os` is a composite `"<platform> / <full OS description>"`
string**, not just a bare platform name. The implementation currently
stores the *entire* string into `Device.platform`
(`src/rustdesk_api/api/heartbeat.py`), so `platform` ends up containing
`"windows / Windows 11 IoT Enterprise LTSC 2024 - 11 (26300)"` instead of
just `"windows"`. **Action item**: split on `" / "` (first occurrence) and
store the first part in `platform`, the remainder in `os_version`, which
would also fill in the previously-empty `os_version` column. Not yet
changed pending confirmation this format is stable across client
versions/platforms (only one Windows client observed so far).

### Android reports only while its service is running - **Observed** (2026-09-21)

An Android client that was signed in to this server but had its service stopped showed up with nothing but its id,
address and dates: no host name, OS, CPU, memory or version. Starting the service (the Share Screen tab's "Start
service") made all of them appear within seconds. This is the client's design, not something the server can change:

- The client's sync loop (`start_hbbs_sync_async` in `hbbs_http/sync.rs`, master) does nothing while the
  `stop-service` option is set - neither the sysinfo upload nor the heartbeat.
- On Android, `main_stop_service` sets `stop-service` to `Y` and `main_start_service` clears it (`flutter_ffi.rs`).
- `/api/login` does not depend on the service, so a phone can sign in, which registers just the id and address.

Consequences: an Android device is online only while its service runs (the heartbeat is what keeps it online), and
its details are those of the last time it reported. Not a server fault, so there is no server-side workaround.
Separately, a record that a login (or an import) created and no sysinfo upload ever filled is now asked for its
system info on the next heartbeat (`"sysinfo": true`, see `has_reported_sysinfo` in `services/devices.py`), for a
client that believes it already uploaded. Android app version not recorded.

## Address book endpoints - confirmed working end to end

Immediately after a successful login, the 1.4.9 client made these calls,
which were 404 at the time (nothing registered for these paths yet),
causing a visible "address book update failed: HTTP_ERROR / Not Found"
banner in the client UI:

| Method | Path | Query params seen |
| --- | --- | --- |
| `GET` | `/api/login-options` | (none) - called once, before login |
| `GET` | `/api/device-group/accessible` | `current=1&pageSize=100` |
| `POST` | `/api/ab/personal` | (none) |
| `GET` | `/api/ab` | (none) |
| `GET` | `/api/users` | `current=1&pageSize=100&accessible&status=1` |

This was resolved through several rounds of live capture, each producing a
specific, actionable error from the client's own Dart runtime:

1. **First fix - `data` must be a JSON-encoded string, not a native
   array/object.** Returning `{"data": []}` (a native array) for `/api/ab`
   caused: `type 'List<dynamic>' is not a subtype of type 'String'`.
2. **Second fix - `/api/login-options` was changed to a JSON object `{}`.**
   After fix 1, the client raised
   `type 'String' is not a subtype of type 'int' of 'index'` - the
   signature of Dart code calling `someList['someKey']`. In hindsight this
   was explained by fix 3 below (an empty-array `data` for `/api/ab` has the
   same failure mode). **Reverted 2026-09-20:** the client's only reader of
   `/api/login-options`, `UserModel.queryOidcLoginOptions`
   (`flutter/lib/models/user_model.dart`), does `for (final item in
   jsonDecode(resp.body))`, so the body has to be an *array* - the login
   options are strings like `oidc/<name>`. With `{}` that loop throws: 1.4.9
   catches it and shows nothing, `master` shows a "network error" line with a
   Retry button in its login dialog. The endpoint answers `[]` again. Not yet
   re-checked against a live client.
3. **Third fix - the confirmed shape, from reading the reference
   project's source directly** (`api/views_api.py::ab` in
   <https://github.com/bryangerlach/rustdesk-api-server> - CLAUDE.md
   section 73). `data` must decode to
   `{"tags": [...], "peers": [...], "tag_colors": "<json string>"}`
   (`tag_colors` is *itself* JSON-encoded again as a string - confirmed
   directly from the reference source, `json.dumps(tag_colors)` assigned
   into a dict that is then itself `json.dumps`'d), with a top-level
   `updated_at` alongside `data`. Each peer has `id`, `username`,
   `hostname`, `alias`, `platform`, `tags`, `hash`.
4. **Fourth fix - `GET`/`POST /api/ab` is a single unified path** (not
   split into a separate write endpoint), confirmed both by the reference
   source (one `ab()` view dispatching on `request.method`) and by a live
   capture of the client doing `POST /api/ab` with the exact
   `{tags, peers, tag_colors}` shape it had just read from `GET /api/ab` -
   proving the read shape was correct, since the client echoed it back.
   This POST had been getting a 405 (only `GET` was registered), which
   the client surfaced as "Nie udało się zsynchronizować książki
   adresowej z serwerem: ... Method Not Allowed".

After all four fixes, `POST /api/ab` returns `200` and the pushed peer
count is reflected on the next `GET /api/ab` (confirmed with `peer_count`
going `0 -> 1 -> 2` across three separate manual client edits, verified
both via server logs and independently via the new read-only
`GET /api/v1/address-book` WebUI page, which survived a server restart
with the data intact).

**Fifth fix - root-caused (2026-09-18) via the actual client source, not
observation alone.** The "Nie udało się zsynchronizować książki adresowej z
serwerem:" banner with nothing after the colon, which kept recurring
specifically on manually-added entries, was previously guessed to be a
stale UI artifact. It was not. Read the real client source
(`flutter/lib/models/ab_model.dart`, `LegacyAb.pushAb()`, in
<https://github.com/rustdesk/rustdesk>) to find the exact success check:

```dart
if (resp.statusCode == 200 &&
    (resp.body.isEmpty || resp.body.toLowerCase() == 'null')) {
  ret = true;
} else {
  Map<String, dynamic> json = _jsonDecodeRespMap(...);
  if (json.containsKey('error')) {
    throw json['error'];              // <- fires even if the value is ""
  } else if (resp.statusCode == 200) {
    ret = true;
  } else {
    throw 'HTTP ${resp.statusCode}';
  }
}
```

This server's `POST /api/ab` was returning `{"error": ""}` on *success*.
Because the JSON has an `error` key at all - regardless of value - the
client throws it, and an empty string renders as exactly
`push_ab_failed_tip: ` with nothing after the colon. The full-list sync
path (`AbModel._syncAllFromRecent` timer) happened not to trigger the
visible banner as reliably as manual `addIdToCurrent()` presumably because
of toast suppression on some call sites (`toastIfFail: false`) - but the
same bug applied to every successful push, always. This project's own
production comparison confirmed it: their server's success path never
includes a key named `error` at all (confirmed independently by reading
the reference project's Django `ab()` POST branch, which returns `{"code":
102, "data": ...}` on the path the client currently treats as success -
different keys entirely, so `containsKey('error')` is always false there).

**Fix applied**: `POST /api/ab` now returns `{}` on success - no `error`
key present at all. The `Not authenticated.` / `Invalid request body.`
failure paths keep a non-empty `error` value, which is the genuinely
correct way to signal failure to this client. Not yet re-verified against
a real client after this specific fix (see test
`test_ab_post_persists_a_manually_added_entry`, updated to assert `"error"
not in body` rather than an exact `{"error": ""}` match).

**Confirmed (2026-09-18): `GET /api/ab` is not a login-only call - it fires
every time the client's Address Book view is opened or refreshed.** Live
capture showed repeated `POST /api/ab/personal` + `GET /api/ab` pairs
throughout a session, not just once right after login (e.g. three pairs
within a 70-second span, then another pair roughly 20 minutes later after
the tab had apparently been closed and reopened). Practical consequence:
an entry added directly through this server's own WebUI
(`POST /api/v1/address-book`, not the RustDesk-protocol path) reached a
real client's local address book with no client-side action beyond
opening/refreshing the Address Book tab - confirmed live. The WebUI's
former "may be overwritten" note was removed (2026-09-20) as not accurate;
whether a client's own full-replace push (`POST /api/ab`) can drop a
WebUI-added entry before the client has re-fetched it is not established.

**New finding - independent data model required.** A user can add an
address book entry for a peer id this server has never seen via
`/api/sysinfo` (confirmed: a manually-added entry with `id: "75646747685"`
was pushed and persisted). This is *not* derivable from `Device` - it's
genuinely independent, client-managed data. Implemented as a dedicated
`AddressBookEntry` model (`src/rustdesk_api/models/address_book_entry.py`,
`src/rustdesk_api/services/address_book.py`), keyed per-user, replaced in
full on every `POST /api/ab` (confirmed the client pushes its *entire*
local address book on every save, not incremental patches - verified via
a full-replace test: pushing entry B without entry A removes A
server-side).

Design choices, still not 100% confirmed:

- `POST /api/ab/personal` returns the same content as `GET /api/ab`. Not
  confirmed this is the *correct* relationship between the two paths -
  just the least-surprising one consistent with everything observed (no
  distinct write behavior was ever seen on this path; the one confirmed
  write path is `POST /api/ab`).
- `GET /api/login-options` returns `[]`, or `["oidc/<name>"]` when OIDC is configured (see "OIDC sign-in";
  it always returned `[]` before 2026-09-21). No LDAP. Not present in the reference project at all.
- None of these hard-fail on missing/invalid auth; they return an empty
  result instead, matching the tolerant style of the other rustdesk-compat
  endpoints.

**"Accessible devices" tab: `/api/device-group/accessible`, `/api/users`,
`/api/peers`** (source-verified 2026-09-21 against 1.4.9's
`flutter/lib/models/group_model.dart` and `flutter/lib/common/hbbs/hbbs.dart`,
NOT yet live-verified). All three take `current`, `pageSize` (and `accessible`,
`status=1`) and answer `{"total": N, "data": [...]}`. **`data` must be a real
JSON array** - the client does `if (data is List)` and skips anything else.
Earlier versions returned it as a JSON-encoded string (copying the `/api/ab`
envelope), which is why the tab stayed empty although every request returned
200. Fields the client reads:

| Endpoint | Item fields |
| -------- | ----------- |
| `/api/device-group/accessible` | `name` |
| `/api/users` | `name`, `display_name`, `email`, `note`, `status` (0 disabled, -1 unverified, else normal), `is_admin` |
| `/api/peers` | `id`, `info` (`username`, `os`, `device_name`), `status`, `user`, `user_name`, `device_group_name`, `note` |

The left list is groups plus users; choosing a user shows the peers whose
`user_name` equals it, choosing a group those whose `device_group_name`
equals it, and with nothing chosen all peers are shown. `info.os` is matched
lower-cased against `windows`/`linux`/`macos`/`android` for the icon.
Here: peers are the devices the caller owns or has had shared with them;
users are the caller plus owners of devices shared with them (not the whole
user table); groups are the caller's own (all for an administrator). The
reference project's `peers` is a non-functional stub, so it is no help.

### `POST /api/currentUser` - **Confirmed**

Called once, ~35 minutes into the session (not immediately after login -
likely triggered by opening some specific screen in the client, not part
of the normal startup sequence). Real request body:

```json
{"id": "123456789", "uuid": "M2Y5YzJhNzEtNmI0ZC00ZTA4LWE1ZDMtOWMxZTdiMjBmODQ2"}
```

Sent **with** an `Authorization: Bearer <token>` header (unlike
heartbeat/sysinfo). Returned 200; matches the implemented
`{"access_token": ..., "type": "access_token", "name": ...}` shape - no
client-side error observed.

## Address book, newer per-item protocol (source-verified 2026-09-20, NOT yet live-verified)

Until now the client showed one list labelled **"Legacy address book"**. That
is the client's own name for its old whole-list mode. It chooses the mode
itself, in `flutter/lib/models/ab_model.dart` (`_getPersonalAbGuid`,
`_pullAb`): it POSTs `/api/ab/personal`; if the JSON answer has a **`guid`**
it uses the newer protocol below, and if the answer is a **404** (or has no
`guid`) it stays in legacy mode (`legacyMode = _personalAbGuid == null`) and
uses `GET`/`POST /api/ab`. Before 2026-09-20 this server answered
`/api/ab/personal` with the legacy envelope (no `guid`), so every client
stayed in legacy mode. The newer protocol is what RustDesk's own (closed
source) Pro server implements; the open-source `hbbs`/`hbbr` has no API server
at all, so the client source is the only specification.

What the newer mode adds: several books per user (the personal one plus
books shared with a rule), per-item changes instead of re-uploading the whole
list on every save (the legacy `POST /api/ab` replaces the entire book, which
loses concurrent edits), and per-book tags with the client's own colors.

Wire format, read from the client (`ab_model.dart`, `hbbs.dart`,
`peer_model.dart`, `http_service.dart`):

| Call | Body / query | Success response |
| --- | --- | --- |
| `POST /api/ab/personal` | empty body (`Content-Length: 0`) | `{"guid": "<uuid>"}`; a 404 selects legacy mode |
| `POST /api/ab/settings` | empty | `{"max_peer_one_ab": N}` (0 = unlimited; a 404 is tolerated) |
| `POST /api/ab/shared/profiles` | `?current=1&pageSize=100` | `{"total": N, "data": [{"guid","name","owner","note","rule","info"}]}`; a 404 = no shared books |
| `POST /api/ab/peers` | `?current=1&pageSize=100&ab=<guid>` | `{"total": N, "data": [peer]}`, paged until `current*pageSize >= total` |
| `POST /api/ab/tags/{guid}` | empty | a bare JSON list `[{"name","color"}]` (`color` is an **integer**, ARGB) |
| `POST /api/ab/peer/add/{guid}` | one peer object | **200, empty body** |
| `PUT /api/ab/peer/update/{guid}` | `{"id", ...only the changed fields}` | 200, empty body |
| `DELETE /api/ab/peer/{guid}` | JSON list of ids (in the *request body*) | 200, empty body |
| `POST /api/ab/tag/add/{guid}` | `{"name","color"}` | 200, empty body |
| `PUT /api/ab/tag/rename/{guid}` | `{"old","new"}` | 200, empty body |
| `PUT /api/ab/tag/update/{guid}` | `{"name","color"}` | 200, empty body |
| `DELETE /api/ab/tag/{guid}` | JSON list of tag names | 200, empty body |

Details that are easy to get wrong (each is a client behaviour, not a guess):

- **Writes must return an empty 200 body.** `_jsonDecodeActionResp` treats
  `200` + empty body as success and otherwise parses the body and reads
  `["error"]`; a `{}` body yields `null.toString()` = the error text "null".
  (The legacy `POST /api/ab` is the opposite: `{}` is fine there.)
- **Errors** are `{"error": "<text>"}` with a non-200 status; the text is what
  the user sees. A **401** makes the client sign out (`userModel.reset`), so it
  is returned only for a missing/invalid token. Unknown and not-permitted books
  both answer 404, so a guid never confirms a book exists.
- **Share rules** (`ShareRule`): `1` read, `2` read/write, `3` full control. The
  client only asks `canWrite()` (rule 2 or 3); `fullControl()` is defined but
  never used in the UI, so this server treats 2 and 3 alike.
- **Passwords.** In a personal book the client sends a `hash` (its own
  encrypted blob) and strips `password`; in a shared book it strips `hash`
  and sends the connection **`password` in clear** (`addIdToCurrent` on add,
  `changeSharedPassword` -> `PUT /api/ab/peer/update/{guid}` `{id, password}`
  afterwards; an empty password is never sent on add, `""` is how it would be
  cleared). It reads it back from the peer JSON (`Peer.fromJson`, `password`),
  so a server has to return it. Checked in the client source at `1.4.9` and
  `master` 2026-09-20; not yet seen with a live client.

  This server stores `hash` for the personal book only. A shared book's
  `password` is stored **Fernet-encrypted** (`address_book_entries.password_enc`)
  with `DATA_ENCRYPTION_KEY` - a key of its own, not `SECRET_KEY`, so rotating
  the session secret cannot orphan the passwords - and returned decrypted in
  `/api/ab/peers` for that shared book only. Without the key it is accepted and
  dropped (the old behavior): "save password" in a shared book then does not
  persist. Behavior worth knowing:
  - anyone with any rule on the book (including read) receives the saved
    passwords, as in RustDesk's own shared books; no rule -> 404;
  - `password` is ignored in the personal book; absent on an update means
    unchanged; `""` clears it; over 256 characters or a non-string is a 422;
  - the WebUI/management API only ever exposes `has_password`;
  - a key that cannot open a row (lost or changed) makes the server omit that
    peer's `password` and log a warning naming the row id - never an error to
    the client, and the ciphertext is kept, so restoring the key restores it;
  - `DATA_ENCRYPTION_KEY` may hold several comma-separated keys (first
    encrypts, all decrypt) and `rustdesk-api rotate-data-key` re-encrypts.
  The legacy `/api/ab` is personal-only and unaffected.
- **Names are keys.** The client keys books by name (a later profile with the
  same name replaces an earlier one) and reserves "My address book" for the
  personal book, so `/api/ab/shared/profiles` disambiguates: the viewer's own
  books keep their names, someone else's colliding book becomes
  `"Name (owner)"`.
- A peer's other fields (`forceAlwaysRelay`, `rdpPort`, `rdpUsername`,
  `loginName`, `device_group_name`) are not stored; the client falls back to
  its defaults.
- `peer/add` is idempotent (an existing id is updated, not duplicated).

**Fallback.** `ADDRESS_BOOK_LEGACY_MODE=true` makes `/api/ab/personal` (and the
other newer endpoints) answer 404, which puts every client back in legacy mode.
`GET`/`POST /api/ab` always work and read/write the **same personal book** the
newer protocol uses, so switching modes loses nothing. Old clients that never
call `/api/ab/personal` keep working unchanged. Shared books are only visible
in the newer mode.

**Data.** `address_books` (personal + shared), `address_book_shares`,
`address_book_tags` (per book, integer ARGB color) and `address_book_entries`
(now owned by a book). Migration `a3c9d5e7b1f2` moves each user's existing
entries into a personal book and copies the tags they used into it (the global
`tags` table, used by devices, is untouched); it has a best-effort downgrade
that drops shared-book entries.

**To verify against a real client:** run with `LOG_LEVEL=DEBUG` (the request
logger covers `/api/ab/*` and logs only the *shape* of bodies, never values),
sign in on the client, and expect: `POST /api/ab/personal` -> 200 with a guid,
then `/api/ab/settings`, `/api/ab/shared/profiles`, `/api/ab/peers` and
`/api/ab/tags/{guid}`, and the address-book selector in the client showing
"My address book" instead of "Legacy address book". Then add/edit/tag/delete a
peer and a tag and confirm each shows up in the WebUI's Address Book page with
no error banner in the client.

## `https://` API server URLs and TLS (source-verified 2026-09-20)

The client uses two HTTP stacks, and only one of them is lenient about
certificates:

1. **The Rust side** - heartbeat, sysinfo and the audit posts (`post_request` in
   `src/common.rs`, `src/hbbs_http/http_client.rs`) - verifies with rustls,
   then retries with native-tls, then with **invalid certificates accepted**, and
   caches what worked. A self-signed certificate is silently tolerated here.
2. **The Flutter UI** - login, address book, users, peers, ...
   (`flutter/lib/utils/http_service.dart`) - defaults to pure-Dart
   `package:http`, which does strict validation with **no override**. Only when a
   proxy is configured in the client or the local option
   `enable-flutter-http-on-rust` is on does it go through the Rust client (and
   its accept-invalid-cert fallback) instead.

So a self-signed `https://` URL fails at login while background reporting would
still work. A certificate from a publicly trusted CA (Let's Encrypt, ...) served
by a reverse proxy validates normally and should work; an internal CA must be in
the client machine's OS trust store. The certificate has to match the exact
hostname in the `API Server` field, and the proxy must serve the full chain.
Not tested against a real proxy.

## Implemented but not yet exercised by a real client

### `POST /api/logout`

- Requires `Authorization: Bearer <token>`.
- Success: `{"code": 1}`. Failure (no/invalid token): `{"error": "..."}`.
- Not observed in this session (the client wasn't logged out during
  testing).

## `POST /api/audit/conn` and `/api/audit/file` - **Source-derived, NOT yet live-verified**

Sources (2026-09-20): `get_audit_server` in `src/common.rs`, and
`post_conn_audit` / `post_file_audit` / `post_alarm_audit` in
`src/server/connection.rs` of `github.com/rustdesk/rustdesk`. The reference
project handles both in one `audit()` view; this project splits them by the
real URL paths.

- URL: `<api-server>/api/audit/{conn,file,alarm}`. The client skips audit
  entirely when the derived server is a public RustDesk one.
- **No `Authorization` header** is sent. These endpoints are therefore
  unauthenticated by necessity. Containment: an event is accepted only for
  a device already registered here and - if a uuid is on record - with the
  same `uuid` (base64, same encoding as `sysinfo`); everything else gets the
  same empty `200` and is dropped, so unknown ids cannot be probed. Per-IP rate limit (`CLIENT_AUDIT_RATE_LIMIT_PER_MINUTE`),
  length-capped fields, and at most 200 stored file entries per event.
- The **controlled** machine posts these, so the machine being connected
  *to* must have its `API Server` pointed at this server.
- Retries: the client retries 5xx/408/429 (10s, 30s backoff, 120s
  deadline) and fails immediately on other 4xx. Every body carries a fresh
  `nonce`; `new` connection events and file events are de-duplicated on it.
- Common body fields: `id` (controlled device), `uuid`, `conn_id` (int,
  per-client-process counter - not globally unique), `session_id`, `nonce`.

`/api/audit/conn` has three shapes, told apart by key:

| Event | Extra keys |
| --- | --- |
| session opened | `action: "new"`, `ip` |
| session authorized | `peer: [controlling_id, controlling_name]`, `type`, optional `primary_auth`, `two_factor` |
| session closed | `action: "close"` |

`type`: 0 remote control, 1 file transfer, 2 port forward, 3 view camera,
4 terminal. `primary_auth`: 1 click-accept, 2 temporary password, 3
permanent password, 4 switch sides. `two_factor`: 1 TOTP, 2 trusted device.

**`controlling_name` is not the user's own spelling (source-verified
2026-09-21, `src/client.rs` 1.4.9, the `display_name` that becomes
`LoginRequest.my_name`):** the controlling client takes the name from the API
login (`display_name`, else `name`), else the OS user name, and upper-cases the
first letter of every word before sending it, so `inteliboy` arrives as
`Inteliboy`. The original case is lost on the wire. The WebUI therefore shows
the original spelling where the same name, compared case-insensitively, is the
OS user name of the controlling device (matched by its RustDesk ID) or one of
this server's user names (`restore_peer_names`); otherwise it shows the name
as reported. The database keeps the value as received.

`/api/audit/file` body: `peer_id`, `type` (`FileAuditType`), `path`,
`is_file` (bool; true when a single file with an empty name),
`info` - a **JSON-encoded string** (same quirk as `/api/ab`'s `data`)
containing `ip`, `name`, `num`, `files: [[name, size], ...]`.

**`FileAuditType` (verified 2026-09-20 from `src/server/connection.rs`,
`post_file_audit` and its call sites):** `0 = RemoteSend`, `1 =
RemoteReceive`, named from the *controlled* device's side.

| `type` | Meaning | `path` is |
| --- | --- | --- |
| `1` RemoteReceive | the controlling peer **uploaded** to this device (`FileAction::Receive`) | the **destination** on this device |
| `0` RemoteSend | the controlling peer **downloaded** from this device (directory listing / read job) | the **source** on this device |

The client reports exactly **one** path - the one on the controlled device.
The controlling side's own path is never sent, so the WebUI shows the
controlling peer (name + ID) as the other end of From -> To rather than
inventing a path. Files copied and pasted through the clipboard
(`Cliprdr`) are posted with `path: ""` and only file names/sizes. The
`files` list is the 10 largest entries, names as the client reports them.
The real 1.4.9 event captured on 2026-09-20 (`type: 1`, `path:
C:\Users\...\Desktop\files.zip`, `is_file: true`) is therefore an upload
of `files.zip` *to* the controlled machine.

Still unverified: exact behavior on non-Windows clients, and whether `type`
values beyond 0-4 exist for `/api/audit/conn` (an RDP value is mentioned by
the client's CLI flags). Any other file `type` is shown as its raw number.

### `/api/audit/alarm`

Implemented 2026-09-20 from `post_alarm_audit` and its callers in
`src/server/connection.rs`, read at **both** the `1.4.9` tag and `master`.
Not yet seen on the wire: provoking one needs a second machine that gets
refused (an IP/ID whitelist on the controlled client, or repeated wrong
passwords).

Like the other two it is posted by the **controlled** client with no
`Authorization` header, is never sent to a `rustdesk.com` API server
(`get_audit_server`), and is contained the same way (known device + matching
`uuid`, per-IP rate limit, length caps). Body: `id`, `uuid`, `typ` (int),
`conn_id`, `info` - a **JSON-encoded string** - and, on `master` only, a
`nonce` (used to de-duplicate retries) and, for the whitelist types, an
opaque `conn_audit_ref` (ignored here).

| `typ` | `AlarmAuditType` | `info` keys |
| --- | --- | --- |
| 0 | `IpWhitelist` | `ip` |
| 1 | `ExceedThirtyAttempts` | `ip`, `id`, `name` |
| 2 | `SixAttemptsWithinOneMinute` | `ip`, `id`, `name` |
| 6 | `ExceedIPv6PrefixAttempts` | `ip`, `id`, `name` |
| 7 | `TerminalOsLoginBackoff` | `ip`, `id`, `name` |
| 8 | `TerminalOsLoginConcurrency` | `ip`, `id`, `name` |
| 9 | `SessionScopeViolation` | `id`, `name`, `ip`, `conn_type` (string), `message` |
| 10 | `IdWhitelist` (`master` only) | `id`, `ip`, `name` |

`id`/`name` are the *connecting* peer's self-reported id and name (`lr.my_id`,
`lr.my_name`), so they identify who was refused only as far as that peer told
the truth. Values 3-5 are commented out in the client source. A `typ` this
server has no label for is stored and shown as its number. Only the keys above
are read from `info` (each length-capped); the raw string is never stored.

### Response body of all three audit endpoints: empty 200

In `master`, `post_audit_async` treats an empty 2xx body as "stored" and
*retries* any other 2xx body (as "unexpected response body" or "server
error"), 10s then 30s later, within a 120s deadline; `1.4.9` ignores the
response. `/api/audit/conn`, `/api/audit/file` and `/api/audit/alarm` therefore
all answer `200` with **no body** (changed 2026-09-20; `conn` and `file`
previously answered `{"data": "ok"}`, which a `master`-era client would have
sent three times, and a retried *authorization update* arriving after the
session's `close` would have created a second, never-closed row). Every
well-formed request gets the same answer whether or not the device is known, so
the endpoints cannot be used to probe device ids. A request without an `id`
still gets `200 {"error": "Missing device id."}`; no real client sends one.
The empty body has not been checked against a `master`-era client (only 1.4.9
has been available); 1.4.9 does not read it.

## `rustdesk://` client URI scheme

Not a server endpoint - a URI scheme the RustDesk desktop client itself
registers as a protocol handler. Verified (2026-09-18) against
`core_main.rs` in the real client source
(`github.com/rustdesk/rustdesk`): the client parses incoming links as
`rustdesk://<authority>/<id>[?params]`, where `authority` is one of
`connect`, `play`, `file-transfer`, `view-camera`, `port-forward`,
`terminal`, `rdp`. There is no bare `rustdesk://<id>` shorthand - the
authority segment is required. The WebUI's address book "Connect" link
uses `rustdesk://connect/<rustdesk_id>` and deliberately omits the
optional `password=` query parameter, since the server never exposes a
saved connection password's raw value anywhere (see `has_password` in
the address book management API) - only the RustDesk client itself ever
sees that value, via its own `/api/ab` round trip.

## Heartbeat: connections and strategies

Source: `hbbs_http/sync.rs` in client `master` and the 1.4.9 tag (the same code). Implemented 2026-09-20; not
yet observed against a live client.

**Request** (every 15 s, every 3 s while a connection is open): `{id, uuid, ver, modified_at, conns?}`.
`conns` is the list of live incoming connection ids and is *omitted* when there are none. `modified_at` is the
strategy version the client stored (`0` if none).

**Response keys the client acts on** (JSON object, all optional): `sysinfo` (any value: upload system info
again, see above), `disconnect: [ids]` (close those connections), `modified_at` + `strategy:
{config_options: {key: value}}` (store `modified_at`, apply every option; an option with an empty value is
removed from the client's config, i.e. reset to its default).

Behaviour here:

- The response is `{"data": "OK"}` plus whichever of those apply. The client is sent `disconnect` once (the
  server forgets the request when it hands it over) and only for connections still in the `conns` it just
  reported, so a stale id can never end a different connection that reuses it.
- The heartbeat is unauthenticated, so `conns`, `disconnect` and `strategy` are only honoured when the request's
  `uuid` equals the one the device uploaded in sysinfo (or the device has none). Someone who only knows a device
  id gets a bare `{"data": "OK"}` and changes nothing. (Pre-existing weakness this does not close: an
  unauthenticated `/api/sysinfo` can still re-bind a device's `uuid`.)
- A strategy is sent only when its `modified_at` differs from the client's. `modified_at` is the strategy's last
  change in Unix microseconds. Every push carries **all** non-sticky option keys the server knows, unset ones as
  `""`, so removing an option, or unassigning the strategy (sent as `modified_at: 0` with every key empty), resets
  the client rather than leaving the last value behind. The list of pushable options (`services/strategies.py`) is a
  deliberate allow-list of permission/behaviour switches and the "Servers" options below; no passwords, IP
  whitelists or proxy settings.
- **Server options (added 2026-09-21; source-derived, not observed on a live client).** `api-server`,
  `custom-rendezvous-server`, `relay-server`, `key` and `allow-websocket` are all on `KEYS_SETTINGS`, so the
  heartbeat can move a client to other servers. They are *sticky*: sent only while set, never as `""`, because
  `handle_config_options` deletes an option whose value is empty (unless the client has a built-in default for
  it), which would wipe the addresses and key a client was installed with; the price is that removing one from a
  strategy does not revert clients that already applied it. Validation: `api-server` is an `https://` origin
  without path, credentials or query, and not port 21114 (the client removes `:21114` from https API addresses,
  see above); ID/relay servers are host names or IPv4 addresses with an optional `:port`; the key matches
  `[A-Za-z0-9+/=_-]{1,128}`. The push changes the channel it travels on: once `api-server` is applied, the next
  heartbeat goes to the new address, so a wrong value cannot be corrected from here. Assign such a strategy to a
  single test device first.
  Values follow the client's `option2bool`: `enable-*` options are on unless `N`, `allow-*` options off unless
  `Y`; `access-mode` is `custom|full|view`, `approve-mode` `password|click`, `verification-method`
  `use-temporary-password|use-permanent-password`, `temporary-password-length` `6|8|10`.
- **Sending the strategy again (added 2026-09-21; source-derived, not observed on a live client).** A heartbeat
  carries no options, so the server cannot see whether a client still holds the `api-server` it was given; what it
  can see is *how the heartbeat arrived*. If the device's strategy sets an `https://` `api-server` and a heartbeat
  arrives over plain HTTP, the client is not using that setting - for example someone cleared it and the client
  fell back to `http://<ID server>:21114` (from memory of the client's API-server fallback, not re-checked here) -
  so the strategy is sent again even though the client's `modified_at` matches. The client applies any `strategy`
  in a response whatever its `modified_at` (`sync.rs`: it stores the timestamp only when it differs, and always calls
  `handle_config_options`). It is sent at most every 5 minutes per device (`RESEND_INTERVAL`, remembered in
  `devices.strategy_sent_at`), so a client that cannot apply it is not sent it every 15 s. The ID server, relay
  server and key leave no trace in a heartbeat, so they are not re-sent. The scheme is the connection's own, or the
  `X-Forwarded-Proto` of a peer listed in `TRUSTED_PROXIES` (a proxy that does not send it makes every device look
  like plain HTTP); it is stored in `devices.api_scheme` and shown as the **Connection** column in the WebUI.
- **Which keys work (source-checked 2026-09-21, `libs/base/src/config/keys.rs` on `master` `a5d4ef9`).**
  `handle_config_options` stores the pushed map with `Config::set_options`, i.e. in the client's `Config` options,
  so a key only has an effect if the client reads it from there: `KEYS_SETTINGS` in `keys.rs`. Every key in the
  catalog is on that list. Five keys the catalog offered until 2026-09-21 are **not**, so a strategy setting them
  did nothing on the client, and they were removed (`RETIRED_KEYS`; a strategy saved with one still loads and saves,
  the value is dropped and never pushed): `enable-check-update` and `allow-auto-record-outgoing` (read from
  `LocalConfig`), `one-way-clipboard-redirection` and `one-way-file-transfer` (read with `get_builtin_option`,
  the custom-build settings), `lock_after_session_end` (a per-session view option of the controlling side).
  Options added 2026-09-21: `enable-remote-printer`, `allow-only-conn-window-open`, `enable-trusted-devices`,
  `allow-numeric-one-time-password`, `temporary-password-length`, `allow-scope-violation-close` and
  `allow-scope-violation-alarm` (see the Alarms log), `keep-awake-during-incoming-sessions`, `enable-abr`,
  `enable-hwcodec`, `enable-directx-capture`. Not observed on a live client. Deliberately not offered although on
  `KEYS_SETTINGS`: `whitelist`/`id-whitelist`, `direct-server`/`direct-access-port`, proxy settings, the `preset-*` keys,
  `allow-insecure-tls-fallback` (weakens TLS) and network tuning (`disable-udp`, `allow-kcp-congestion-control`,
  ...). `allow-ask-for-note` (below) is a `LocalConfig` key and cannot be pushed either.

## Connection notes (`GET /api/audit/conn/active`, `PUT /api/audit`) - source-derived, NOT yet live-verified

Source: `flutter/lib/models/model.dart` (`_queryAuditGuid`), `flutter/lib/common/widgets/dialog.dart`
(`updateAuditNoteByGuid`), `src/ui_session_interface.rs` (`send_note`), identical in `1.4.9` and `master`. The
client setting is the local option `allow-ask-for-note` (client Settings, "Ask for note at end of connection";
off by default, can only be switched on while signed in). It lives in the client's `LocalConfig`, so a strategy
cannot turn it on: each user does that on the machine they control from.

1. Once connected, the **controlling** client calls `GET <api>/api/audit/conn/active?id=<controlled id>&session_id=
   <n>&conn_type=<0-4>` with its login token (`Authorization: Bearer`). It expects a JSON *string* (the GUID). Any
   status other than 200 ends the attempts; a 200 without a string is retried up to six times (1, 1, 2, 2, 3 s), which
   covers the controlled device's own `/api/audit/conn` report arriving late. `conn_type` is 0 remote control, 1 file
   transfer, 2 port forward or RDP, 3 view camera, 4 terminal, as in the audit type table above.
2. When the session closes and the user typed something, `PUT <api>/api/audit` with `{"guid", "note"}` and the same
   token. The client only logs the status.
3. The older (Sciter) client instead posts `{"id", "session_id", "note"}` to `/api/audit/conn` with **no** token and
   no `uuid`.

Behaviour here:

- The session is the newest `connection_logs` row of that device with that `session_id` (a random 64-bit number the
   two clients share; the client keeps it across reconnects) and, when the controlled device reported a type, the
   same `conn_type`. It must be open or ended within 12 hours (a session that never reported a close counts as
   open for a week).
- `GET` needs a valid client token (API keys and enrollment tokens are not client tokens) and answers `401
  {"error": ...}` without one, JSON `null` while there is no such session yet, else the GUID, made the first time it
  is asked for and stable after. `PUT` needs a token too and answers `200` (empty), `401` or `404` (unknown GUID,
  session too old, empty note). The old-client `POST` answers an empty `200` whatever happens, like the other audit
  posts, so it cannot be used to probe ids or session ids.
- The GUID is an unguessable handle for setting the note of that one session, nothing else; the management API never
  returns it. Notes are trimmed, stripped of control characters and cut at 1000 characters, stored in
  `connection_logs.note` (migration `a9c4b7e2d6f1`), shown as plain text under Logs and in the device timeline, and
  removed with the connection log. They are not written to the server log (the DEBUG capture masks `note`).
- Not covered: notes are set by whoever holds a client token for *any* account, not only the owner of the controlled
  device, because the controlling user is generally not its owner; the note describes the session, and the session's
  own visibility rules (owner, share, administrator) decide who reads it.

## `--assign` (`POST /api/devices/cli`)

Source: `core_main.rs` (`--assign`), `ui_interface.rs`. `rustdesk --assign --token <t> [--user_name]
[--strategy_name] [--address_book_name] [--address_book_tag] [--address_book_alias]
[--address_book_password] [--address_book_note] [--device_group_name] [--note] [--device_username]
[--device_name]` (needs root/administrator on the machine) posts `{id, uuid, ...}` with `Authorization:
Bearer <t>`. The client prints the response text, or "Done!" for an empty body; it returns 4xx bodies as they
are, so errors here are plain text with a 4xx status and success is an empty 200.

- `--token` is an *enrollment token* (Security page: a session of kind `enroll`, shown once, hashed at rest,
  30 days to a year, revocable) or any regular login token. Enrollment tokens are ignored by every other
  endpoint.
- The caller is authorised like any API call: only devices that are unowned or theirs, only giving a device to
  themselves, only their own address books (their personal book is what the client calls "My address book") and
  groups (looked up by name among the target user's groups). Administrators may assign to any user and set a
  strategy. Everything is validated before anything is changed.
- `--device_name` sets the device's alias; `--note` its note. `--device_username` is refused: the server reports
  the username the client sends. `--address_book_password` needs a shared book and `DATA_ENCRYPTION_KEY`.
- An id the server has not seen yet is registered by the command (its uuid must match if it is known).

The `preset-*` keys (`preset-user-name`, `preset-strategy-name`, `preset-address-book-name/-tag/-alias/
-password/-note`, `preset-device-group-name`, `preset-note`, `preset-device-name`) that customised clients add
to their `/api/sysinfo` body are honoured only with `ALLOW_SYSINFO_PRESETS=true`, only for a device's first
registration, and never at the cost of the registration itself (an unusable preset is logged and skipped).

## Two-factor login

Source: `hbbs.dart` (`LoginRequest`, `LoginResponse`), `login.dart`. Implemented 2026-09-20; not yet exercised
against a live client.

1. `POST /api/login` with the password. For an account with 2FA the answer is `{"type": "email_check",
   "tfa_type": "tfa_check", "secret": "<challenge>", "user": {"name": ...}}` instead of a token; the client
   then shows its verification-code dialog. (`tfa_type` absent or `email_check` would make it ask for an *email*
   code instead.)
2. It repeats the login with `{"type": "email_code", "username", "id", "uuid", "secret", "verificationCode",
   "tfaCode", "autoLogin"}` - **no password** - and gets the normal `{"access_token", "type":
   "access_token", "user"}`.

The `secret` is a single-use, five-minute challenge stored hashed and bound to that user; five wrong codes, a
different username, or expiry kill it. The code field of the client's dialog takes six digits, so recovery codes
are accepted in the WebUI only. Failures are `200 {"error": ...}` like the rest of the login.

## Device identity: a different `uuid` (implemented 2026-09-20, not yet exercised by a live client)

`/api/sysinfo` and `/api/heartbeat` carry no credentials, and a RustDesk id is a short number, so the client's own
`uuid` is the only thing binding a heartbeat to a device record. In the captured 1.4.9 traffic it is a base64-encoded
GUID (`3f9c2a71-...`, the shape of a Windows machine id). `hbb_common::get_uuid()` is in a submodule that the source
checked here does not include, so *how* it is derived is **not verified**; from memory it comes from the machine id
(falling back to the client key pair), which would mean a reinstall on the same machine keeps it and a cloned VM
shares it. Before this change any anonymous `POST /api/sysinfo` for a known id with a new `uuid` overwrote the
record and *adopted* the new uuid, after which the real client failed the heartbeat check (no strategy, no
disconnects) and the sender received them instead.

Now (`DEVICE_UUID_REBIND`, default `approve`), for a device that already has a uuid:

| Request | Result |
| --- | --- |
| `sysinfo`, same uuid | as before: fields updated, `SYSINFO_UPDATED` |
| `sysinfo`, different uuid, no login token | nothing changes; the uuid is parked (`devices.pending_uuid`, with time and source address), a `uuid_change_requested` timeline event and a `device_uuid_change_requested` audit entry are written once; **still answers `SYSINFO_UPDATED`** so the client does not start re-sending every two minutes |
| `sysinfo`, different uuid, `Authorization: Bearer` of the device's owner or an administrator | trusted: re-bound at once |
| `sysinfo`, different uuid, a token of anyone else | parked, and the sender does not become the owner |
| `POST /api/login` carrying `id` + a different `uuid` | same rule: trusted only for the owner or an administrator; an unowned or someone else's device is parked and not claimed |
| `heartbeat`, different uuid | `{"data": "OK"}` only - no `disconnect`, no `strategy`, no connection recording, and `last_seen` is **not** refreshed (the heartbeat used to make the device look online) |
| `heartbeat`, unknown id | unchanged: `{"data": "OK", "sysinfo": true}` |

`deny` drops the parked step (mismatching uploads are ignored); `allow` restores the old behaviour. The owner or an
administrator resolves a parked change with `POST /api/v1/devices/{id}/uuid/accept|reject` (a share, even with
"control", cannot). A registered device whose machine changed therefore shows a *Review* badge until decided; a
legitimate reinstall of the same machine should keep its uuid and never notice (unverified, see above). Also
untested against a real client: what a hardware change or a fresh OS install does to the uuid (if it changes, the
device waits for approval - the intended outcome).

`GET /api/v1/devices` no longer returns `uuid` (it is what authenticates the device's heartbeat, and a view-only
share used to be able to read it); it returns `uuid_change_pending`, `uuid_change_at` and `uuid_change_ip`.
Nothing in the client protocol reads this API.

Not covered: the *first* upload for an id nobody has registered still registers freely (there is nothing to compare
with), and `/api/audit/*` (which identifies devices by id + uuid as well) is unchanged.

## Sign-in lockout (implemented 2026-09-20)

`POST /api/login` from the client counts wrong passwords per account. At `LOGIN_LOCKOUT_THRESHOLD` the account is
locked for `LOGIN_LOCKOUT_MINUTES`; a locked account answers even a correct password with the usual HTTP 200 and
`{"error": "Too many failed sign-in attempts. Try again in N minutes."}` (which the client shows verbatim, like
"Invalid username or password."). A wrong second-factor code counts as well (`type: "email_code"`), and finishing a
login resets the count. Not yet seen on a live client; the client treats any `error` string as a failed login.

## Client setup: the config string, `--config` and the file name (source-verified 2026-09-21, NOT run against a client)

The WebUI's **Connect** page builds these from the server settings. Formats, from the client source:

- **Config string** (`ServerConfig.encode`/`decode` in `flutter/lib/common.dart`): the JSON object
  `{"host": <ID server>, "relay": ..., "api": ..., "key": ...}` as URL-safe base64, **reversed**. The Flutter decoder
  tries plain JSON first, then reverses the text and base64-decodes it after `base64.normalize` (so padding is optional);
  the desktop decoder in `src/custom_server.rs` tries URL-safe base64 without padding, then with. The server writes it
  without padding. The `host` may not be empty (`importConfig` shows "Invalid server configuration" otherwise).
- **`rustdesk --config <string>`** (`src/core_main.rs`): needs the client to be **installed** and the caller to be
  root/administrator ("Installation and administrative privileges required!" otherwise); it sets the options `key`,
  `custom-rendezvous-server`, `api-server` and `relay-server`. A running client should be restarted.
- **File name**: an executable named `rustdesk-host=<id>,key=<key>,api=<api>,relay=<relay>.exe` configures itself (the
  parts are found by their `host=`, `key=`, `api=`, `relay=` prefixes; a trailing comma protects against Windows adding
  " (1)"). The Connect page writes only the parts that are set.
- **`rustdesk://config/<string>`** exists on Android and iOS only, and is ignored unless the build option
  `allow-deep-link-server-settings` is on, so the page does not offer it.
- The QR code is the config string; the mobile apps' "import server config" reads it.

What was **not** done: running any of these against a real client. The tests check the string round-trips through the
decoder rules above and that the API returns it; whether a given client version accepts every command as written
(particularly on macOS) needs one try on one machine.

## Not implemented (explicitly out of scope)

- **`/api/sysinfo_ver` - deliberately not implemented, and no client asks for
  it.** Checked 2026-09-20 at the `1.4.9` tag and `master`: the client only
  requests it when its API server host is a `rustdesk.com` one (`is_public`),
  as part of skipping an unchanged sysinfo upload against RustDesk's own
  servers. A self-hosted server never receives it, so an implementation could
  not be exercised by any client.

- **`/api/record` (session-recording upload) - dead code in the open-source
  client; nothing to implement.** Checked 2026-09-20 in the full `master`
  tree (`a5d4ef9`) and the `1.4.9` tag. `video_service.rs::get_recorder` only
  starts the uploader when `record_upload::is_enable()` is true, but the
  `ENABLE` flag behind it is a private `static` that is initialised to `false`
  and never assigned anywhere, so no open-source client ever posts to
  `/api/record`. (The upload protocol itself is visible in
  `hbbs_http/record_upload.rs` - `POST /api/record?type=new|part|tail|remove
  &file=<name>` with the recording bytes as the body, JSON `{"error": ...}` on
  failure - but a build that flips the flag is not something we can test.)

- **`/api/switch-grant` and `/api/devices/deploy` - RustDesk Pro features;
  they cannot be implemented for the open-source setup.** Investigated
  2026-09-20 against client `master` (neither exists in `1.4.9`) and the
  open-source `rustdesk-server`. Both only make sense together with the
  closed-source Pro `hbbs`; the open-source `hbbs` never reads the values
  involved, and the Pro side is not public, so no implementation could be
  tested against anything that uses it. A missing endpoint is harmless: the
  client logs an error and carries on.
  - `POST /api/switch-grant` (client `register_switch_grant`,
    `src/hbbs_http/sync.rs`; added in #15615, "switch_code for hbbs to bypass
    ACL"). The controlled machine of a "switch sides" request registers a
    one-time grant: body `{id, switch_code_verifier, timestamp, signature}`,
    where the signature is an ed25519 detached signature by the device key.
    It expects `{"accepted": true}` (or `{"accepted": false, "server_time":
    N}` to retry once with the server's clock). The matching `switch_code` then
    travels to `hbbs` in the relay/punch-hole request, and Pro `hbbs` uses it
    to let that one connection past its access-control rules. Checking the
    signature needs the device's public key, which only `hbbs` receives
    (`register_pk`). The open-source `hbbs` has no ACLs and ignores
    `switch_code`. Skipped for RustDesk's public servers.
  - `POST /api/devices/deploy` (client `deploy_device`, `src/ui_interface.rs`).
    Enrollment for Pro servers that accept only approved devices: run by
    `rustdesk --deploy --token <api_token> [--id X]` (or the Android UI), or
    prompted when `hbbs` answers `register_pk` with `NOT_DEPLOYED`. Request
    `Authorization: Bearer <token>`, body `{id, uuid, pk}`; the client reads
    `{"result": "OK" | "NOT_ENABLED" | "INVALID_INPUT" | "ID_TAKEN"}`. The
    open-source `hbbs` has no `NOT_DEPLOYED` state, so no device is ever asked
    to deploy, and a deploy recorded here would not be enforced by anything.
- **OIDC login (`/api/oidc/auth`, `/api/oidc/auth-query`) - implemented 2026-09-21**: see "OIDC sign-in" below.

- The management additions of 2026-09-20 (`/api/v1/api-keys`, `/api/v1/auth/sessions`,
  `/api/v1/auth/register|reset-password|options`, `/api/v1/devices/bulk|{id}/timeline|{id}/uuid/*`,
  `/api/v1/views`, `/api/v1/export/*`, `/api/v1/import/devices`, `GET /metrics`) are this project's own API and are
  never called by the RustDesk client, so they carry no protocol-compatibility claims. API keys are deliberately
  ignored by every RustDesk client endpoint (`/api/login`, `/api/ab*`, `/api/sysinfo`, ...).
- Any endpoint not listed above (file transfer logging, custom client
  downloads, GitHub client update integration, etc. as seen in the
  reference project) - out of scope for this project, which is a
  control-plane/management layer, not a full reimplementation of every
  reference-project feature.

## OIDC sign-in (implemented 2026-09-21, source-derived, NOT yet live-verified)

Source: `src/hbbs_http/account.rs`, `flutter/lib/models/user_model.dart` (`queryOidcLoginOptions`),
`flutter/lib/common/widgets/login.dart`, client `master` (`a5d4ef9`); the 1.4.9 client has the same flow. Nothing
Pro-only is involved. Tested against an in-process provider (real RSA-signed tokens over `httpx.MockTransport`), never
a real provider or a real client.

- `GET /api/login-options` answers `["oidc/<OIDC_NAME>"]` when configured, else `[]` (an array either way; the client
  iterates it). The client labels the button "Continue with <Name>" (`azure` shows as Microsoft, `github`, `gitlab`
  and the names it has icons for get them).
- `POST /api/oidc/auth`, body `{op, id, uuid, deviceInfo, apiDomain}` -> `{"code": "<poll handle>", "url": "<authorize
  URL>"}` or `{"error": "..."}` (HTTP 200, like the other login answers). `op` must be the configured name
  (case-insensitive), `id` and `uuid` are required. `apiDomain` is ignored: the redirect URI is `EXTERNAL_URL` +
  `/api/oidc/callback`, never something the caller sends.
- `GET /api/oidc/auth-query?code=&id=&uuid=`, polled once a second for up to three minutes. While the user has not
  finished it answers `{"error": "No authed oidc is found"}` - that text is what keeps the client polling, so **no
  other answer may contain it**. A wrong `id`/`uuid`, an unknown or expired handle, and a used handle answer
  `{"error": "The sign-in request was not found or has expired."}`, a refused sign-in its reason (for example "No
  account is linked to this sign-in..."), and success the login body `{access_token, type: "access_token", user:
  {name, id, email, status: 1, is_admin, info: {}}}`. The client's parser (`AuthBody`/`UserPayload`) needs
  `user.name` and `user.info` (an object, may be empty); the password login's `user: {name, id}` lacks `info`, which
  `master` would reject for this flow. A result is handed over once, then the request is deleted.
- The browser leg is ours: `GET /api/oidc/callback?code=&state=` at `EXTERNAL_URL`. For a client request it shows
  "You are signed in. You can close this window"; the client (not the browser) receives the session.
- A device that signs in this way is registered and owned exactly as with a password login (`id` + `uuid` are the ones
  the client started the request with, and the poll must repeat them), so the uuid rules of "Device identity" apply.

Security decisions (`security/oidc.py`, `services/oidc.py`): authorization code + PKCE (S256), `state` and `nonce`;
the PKCE verifier and nonce are derived (HMAC of `SECRET_KEY` and a per-request salt) and never stored, `state` and the
poll handle only as SHA-256; requests live ten minutes, are single-use (claimed before the provider is contacted) and
are purged. ID tokens: signature against the provider's JWKS with an allow-list of asymmetric algorithms (`none` and
HMAC are refused, which blocks algorithm-confusion forgeries), `iss` = the discovered issuer, `aud` contains the client
id (and `azp` when there are several), `exp`/`iat`/`sub` required, the nonce. Discovery, JWKS and token endpoints must be
https (http only for a loopback host), redirects are not followed, answers are size-capped, only ID-token claims are used.
A WebUI sign-in or link is bound to the browser that started it by a cookie (`rd_oidc`, HttpOnly, SameSite=Lax, path
`/api/oidc`), so a copied link cannot be finished elsewhere. Who the user is: the linked identity `(issuer, subject)`;
else, only with `OIDC_LINK_BY_EMAIL`, the local user with the same *verified* address; else, only with
`OIDC_AUTO_CREATE_USERS` and `OIDC_ALLOWED_EMAIL_DOMAINS`, a new non-admin user; else refused. Users created this way get
an unusable password hash. The provider is trusted for MFA: a linked sign-in skips the local TOTP.

Not covered: several providers at once, the user-info endpoint, group/role mapping (a provider can never make anyone an
administrator), logout at the provider, refresh tokens, `common-oidc/<json>` login options, and `id_token` decryption.

## How to verify against a real client

1. Point a RustDesk desktop client's `API Server` field (in its ID/Relay
   server settings) at `http://<host>:<port>` of a running instance of
   this server - note the explicit `http://` scheme is required for this
   field specifically, unlike `ID Server`/`Relay Server`.
2. Run the server with `LOG_LEVEL=DEBUG`. This enables a request-body
   logger scoped *only* to the RustDesk-protocol paths
   (`app.py::_log_rustdesk_compat_request`), which redacts `password` and
   never logs `Authorization` header values. This is intentionally
   opt-in and DEBUG-only - CLAUDE.md section 21 says not to log full
   request bodies by default, and normal `INFO`-level logs already
   capture method/path/status/duration for every request without bodies.
3. Compare captured traffic against the assumptions documented above.
4. File discrepancies as issues, fix the endpoint, update this document,
   and add/adjust the corresponding test in `tests/compatibility/`.

## API server URL: `:21114` is stripped from `https://` addresses

Observed 2026-09-21 on a deployment behind a reverse proxy, then confirmed in the source at the 1.4.9 tag
(`src/common.rs`, `get_api_server`):

```rust
if res.starts_with("https")
    && res.ends_with(":21114")
    && get_builtin_option(keys::OPTION_ALLOW_HTTPS_21114) != "Y"
{
    return res.replace(":21114", "");
}
```

So a client whose **API server** is `https://example.com:21114` really calls `https://example.com` (port 443).
If another web server answers there, the client's JSON decoder fails with
`Unknown Error: FormatException: Unexpected character (at character 1) <html>` and the server never sees a request.
`OPTION_ALLOW_HTTPS_21114` is a build-time (built-in) option, not something a user can set in the UI. `http://` addresses
are not rewritten. With the field empty the client uses `http://<id server host>:21114` (`get_api_server_`).

Consequences for deployment: an HTTPS API server must not be on port 21114. Use another port
(`https://example.com:21120`) or a host name of its own on 443 (`https://rustdesk.example.com`). Direct requests to
`https://example.com:21114/api/...` (curl, a browser, the WebUI) work fine; only the client rewrites it. Verified that
the server itself answers login, login-options, heartbeat and sysinfo correctly over HTTP/1.1 and HTTP/2 through a
TLS-terminating reverse proxy.

