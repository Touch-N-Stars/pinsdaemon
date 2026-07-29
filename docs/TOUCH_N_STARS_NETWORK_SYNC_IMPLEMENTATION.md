# Touch-N-Stars: Local Rig Synchronization and PINS Network Resilience

Use this document as the complete implementation instruction for a coding AI
working in:

<https://github.com/acocalypso/Touch-N-Stars/tree/develop>

## Role and objective

Act as a senior Vue, Pinia, Capacitor, Android networking, and distributed-state
engineer. Inspect the current `develop` branch before editing.

Implement a local-only, account-free client architecture where:

1. The astrophotography rig is the authoritative source of operational and shared
   configuration state.
2. The Android/iOS app and browser show the same rig-owned state after connecting
   to the same rig.
3. Device-specific presentation preferences remain device-local.
4. Touch-N-Stars survives PINS switching between its home-LAN address and its
   outdoor hotspot address.
5. Outdoor operation works through the PINS hotspot while mobile data remains
   available where the OS supports it.

Do not introduce cloud services or user accounts. Keep the existing PINS daemon
API token mechanism and current token resolution behavior. Never print tokens or
Wi-Fi passwords.

## Existing code that must be understood first

Trace and preserve the behavior of at least:

- `src/store/settingsStore.js`
- `src/store/store.js`
- `src/plugins/pins/store/pinsStore.js`
- `src/plugins/pins/views/pins.vue`
- `src/plugins/pins/components/tabs/PinsNetworkTab.vue`
- `src/services/api/core.js`
- `src/services/apiPinsService.js`
- `src/services/pinsConfig.js`
- `src/services/wifiBindingService.js`
- `src/utils/wifiNetworkBinder.js`
- `src/utils/wifiNetworkBinderWeb.js`
- the existing mDNS/Capacitor discovery code
- backend settings calls such as `getSetting`, `createSetting`, and `updateSetting`
- WebSocket/SignalR reconnection and `switchBackend()`

The current `settingsStore` persists connection data and many settings in
`localStorage`. Several rig settings already use backend settings keys. The PINS
store also persists Wi-Fi passwords locally. Do not assume Pinia state is shared
between installations: browser storage, Android WebView storage, and another
browser profile are independent.

## PINS daemon API contract

The updated PINS daemon listens on port `8000`. Authenticated calls continue to use
the existing bearer token.

### Discovery

`GET http://<candidate>:8000/health` does not require authentication:

```json
{
  "status": "ok",
  "service": "pinsdaemon",
  "rigId": "pins-658f1",
  "apiVersion": 2
}
```

Debian installations advertise `_pinsdaemon._tcp` over mDNS on port 8000.
Avahi owns the rig's stable hostname. The Linux N.I.N.A. plugin continues to
advertise `_touchnstars._tcp` with its active HTTP port, but marks that mDNS
profile as shared so it does not compete with Avahi for the hostname.

### Desired and observed network mode

`GET /wifi/mode`:

```json
{
  "desiredMode": "auto",
  "observedMode": "client",
  "availableModes": ["auto", "hotspot"]
}
```

`PUT /wifi/mode` with:

```json
{
  "desiredMode": "hotspot"
}
```

returns:

```json
{
  "desiredMode": "hotspot",
  "job": {
    "jobId": "uuid",
    "status": "pending",
    "exitCode": null,
    "startedAt": 0,
    "finishedAt": null,
    "command": "redacted command"
  }
}
```

Allowed desired modes:

- `auto`: try saved home Wi-Fi and fall back to the hotspot.
- `hotspot`: persistently retain the hotspot, including after reboot.

Observed modes can be `disconnected`, `client`, `hotspot`, `dual`, or `unknown`.

`GET /wifi/status` retains all existing fields and now also contains:

```json
{
  "desiredMode": "hotspot",
  "observedMode": "hotspot"
}
```

The hotspot management address is always `10.42.0.1`. `POST /wifi/disable` remains
supported and now behaves as a backward-compatible request for persistent
`hotspot` mode. A deliberate `POST /wifi/connect` returns the rig to `auto` mode.

Network jobs can intentionally make the current API address unreachable before
the client receives or polls the final result. Loss of the old connection is not
proof of failure.

## Required architecture

### 1. Logical rig identity

Replace the assumption that one saved IP address equals one instance.

Represent an instance as one logical rig:

```js
{
  id: "local UI record id",
  rigId: "pins-658f1",
  name: "Field Rig",
  preferredEndpoint: {
    protocol: "http",
    host: "192.168.1.80",
    port: 5000
  },
  candidateHosts: [
    "192.168.1.80",
    "pins.local",
    "10.42.0.1"
  ],
  apiToken: "existing token field"
}
```

Keep bootstrap connection information device-local. It cannot be loaded from the
rig until the rig has first been found.

Migrate existing `connection.instances` records in place. Do not delete existing
instances or force users through setup again. Add missing fields lazily after a
successful `/health` probe.

Never merge two records only because they temporarily have the same IP. Merge or
update endpoint candidates only after matching a non-empty `rigId`.

### 2. Endpoint resolver

Create a focused service, for example:

`src/services/rigEndpointResolver.js`

It must:

1. Build candidates from:
   - the active/preferred host;
   - previously successful hosts for that rig;
   - the current browser hostname;
   - mDNS results for `_pinsdaemon._tcp` in the native app;
   - `pins.local` where configured/supported;
   - `10.42.0.1`.
2. Normalize and deduplicate hosts.
3. Probe `http://<host>:8000/health` with a short timeout of approximately
   1.5–2.5 seconds.
4. Limit concurrency to avoid flooding the local network.
5. Require `service === "pinsdaemon"` and a valid `rigId`.
6. When the expected rig ID is known, reject responses from a different rig.
7. Return the winning host, health payload, latency, and discovery source.
8. Support cancellation through `AbortController`.
9. Never include the bearer token in the unauthenticated health probe.

Do not embed candidate-probing logic separately in Vue components, API modules,
and stores.

### 3. Connection supervisor

Create one connection supervisor, for example:

`src/services/rigConnectionSupervisor.js`

It owns endpoint recovery and calls the existing backend switch/teardown path only
after a candidate has been validated.

Required states:

- `idle`
- `probing`
- `connected`
- `network-transition`
- `reconnecting`
- `failed`

Required behavior:

- Start on application boot and whenever an instance is selected.
- Prefer the last successful endpoint during normal startup.
- During a deliberate network transition, probe candidates repeatedly with
  bounded exponential backoff and jitter.
- Use a total default transition window of at least 90 seconds.
- Promote the winning endpoint to `preferredEndpoint`.
- Call the existing `switchBackend()` exactly once per actual endpoint change.
- Reapply the existing Android Wi-Fi binding after the endpoint changes.
- Reload authoritative backend state after reconnection.
- Prevent overlapping probe loops using a generation ID or cancellation token.
- Pause or reduce probes while the app is backgrounded and resume immediately
  when foregrounded.
- Expose reactive state for connection banners and the PINS network screen.

Ordinary transient API errors must not immediately rewrite the active endpoint.

### 4. Network-operation workflow

Add typed API methods in `apiPinsService`:

- `getPinsHealthAt(host, options)`
- `getPinsWifiMode()`
- `setPinsWifiMode(desiredMode)`

Keep the existing token-resolution behavior for authenticated calls.

When changing mode or connecting Wi-Fi:

1. Confirm the requested action in the UI.
2. Save the returned `jobId` immediately when a response is received.
3. Mark the supervisor as `network-transition` before the old endpoint disappears.
4. Poll the job while the old endpoint remains reachable.
5. If it becomes unreachable, begin endpoint resolution. Do not display failure
   merely because polling the old address failed.
6. Once reconnected, retrieve `/wifi/status`, `/wifi/mode`, and the job result if
   it is still available.
7. Consider the operation successful only when observed state matches the intended
   outcome:
   - hotspot request: `observedMode` is `hotspot` or `dual`;
   - Wi-Fi connection: observed client connection is present.
8. If the deadline expires, show all attempted candidates and actionable recovery:
   connect to the `pins-*` SSID and retry `http://10.42.0.1`.

Persist a small transition record locally:

```js
{
  operationId: "job uuid",
  rigId: "pins-658f1",
  requestedMode: "hotspot",
  startedAt: "ISO timestamp"
}
```

Restore and resume this transition after app reload. Clear it only after confirmed
success, confirmed backend failure, user cancellation, or expiry.

### 5. PINS network UI

Update the PINS network tab to show:

- Desired mode: Automatic / Field hotspot.
- Observed state: Client / Hotspot / Dual / Disconnected / Transitioning.
- Active interface and current rig IP.
- Fixed field address: `http://10.42.0.1`.
- A “Use field hotspot” action.
- A “Try home Wi-Fi” or “Automatic mode” action.
- A visible explanation that a single Wi-Fi adapter cannot be AP and client
  simultaneously.
- A transition banner explaining that a temporary disconnect is expected.
- A recovery action that starts endpoint probing without repeating the network
  mutation.

Do not label hotspot activation as “Disable Wi-Fi”; that wording hides the actual
persistent desired state. Keep compatibility with the existing endpoint internally.

If two separate adapters are configured and observed mode is `dual`, present that
accurately without forcing single-radio warnings.

### 6. Shared versus device-local state

Audit every persisted Pinia store and direct `localStorage` use. Create and document
an explicit classification.

Keep device-local:

- language;
- touch optimization;
- theme and accessibility;
- modal positions;
- screen/layout choices that differ between phone and desktop;
- keep-awake and Android Wi-Fi-binding flags;
- setup/tutorial completion;
- logical-rig bootstrap endpoints;
- API token;
- short-lived transition recovery record.

Make rig-owned:

- equipment and imaging configuration;
- mount, camera, guider, flat, framing, and sequence settings;
- navbar feature visibility/order if the product expectation is one shared rig UI;
- favorite targets, plans, schedules, and reusable sequence data;
- plugin configuration that controls behavior on the rig;
- network desired mode and hotspot settings;
- current operations and job state.

Ephemeral live state must come from the rig’s existing APIs/WebSockets and should
not be persisted as a second source of truth.

Do not synchronize Wi-Fi passwords into general frontend settings. NetworkManager
on PINS owns saved client credentials. Avoid continuing to persist plaintext
passwords in `pins-plugin-store`; keep a password only long enough to submit the
connection request unless an explicit existing product requirement prevents this.

### 7. Shared settings repository

Reuse the existing backend settings API instead of introducing a cloud database.
Create a single repository abstraction, for example:

`src/services/rigSettingsRepository.js`

Each shared document should use a versioned envelope:

```json
{
  "schemaVersion": 1,
  "revision": 12,
  "updatedAt": "2026-07-26T20:15:00Z",
  "updatedBy": "random-client-installation-id",
  "value": {}
}
```

Requirements:

- Generate and persist a random `clientId`; it is not an account or credential.
- Validate and migrate document schema before applying it to Pinia.
- Debounce UI-originated saves.
- Do not write state back while hydrating from the backend.
- Reload shared documents after endpoint reconnection.
- Poll revisions at a modest interval when no backend setting-change event exists.
- Apply a newer remote revision to all connected clients.
- Before updating, read the current revision and detect a stale local edit.
- For stale writes, use a documented policy:
  - merge independent object keys where safe;
  - otherwise show a conflict/reload message rather than silently overwriting.
- Continue operating when optional shared documents are absent; initialize them
  from current backend/default state.

Migrate one settings domain at a time. Existing backend-owned settings must not be
replaced with local defaults during first load.

### 8. Mobile and browser routing

Preserve `wifiBindingService.js`. On Android, after selecting `10.42.0.1`, invoke
the existing native binder so rig traffic remains on Wi-Fi while cellular data can
remain the default network.

The web implementation must remain a no-op. Browser JavaScript cannot bind the OS
network or programmatically force the device to stay on an internet-less Wi-Fi
network. Give browser users clear instructions to choose “stay connected” or “use
without internet” when their OS asks.

Do not attempt to add a fake Internet gateway, captive portal, or remote relay.

## Failure and security requirements

- Never log bearer tokens, hotspot passwords, or client Wi-Fi passwords.
- Mask sensitive Axios request data in debug/error logging.
- Reject a discovered daemon whose `rigId` differs from the selected rig.
- Do not automatically execute a network mutation merely because discovery found
  another endpoint.
- Make repeated `set mode` UI actions idempotent and disable duplicate submissions.
- Cancel stale discovery loops after switching instances.
- Handle malformed health, mode, status, and job payloads without crashing Pinia.
- Preserve non-PINS/NINA instance support.
- Do not hardcode the existing API secret into additional source files.
- Do not change the existing token value as part of this work.

## Required automated tests

Add unit tests for:

- candidate normalization, ordering, and deduplication;
- fixed hotspot candidate inclusion;
- mDNS result conversion;
- health timeout and cancellation;
- rejection of the wrong `rigId`;
- endpoint promotion after a successful probe;
- prevention of duplicate supervisor loops;
- expected loss of the old endpoint during transition;
- transition restoration after reload;
- correct desired/observed mode parsing;
- mode API request and token header behavior;
- no token on `/health`;
- legacy instance migration;
- shared/device-local state classification;
- shared settings hydration without write-back loops;
- revision conflict handling;
- Android binder reapplication after endpoint changes;
- web binder remaining a no-op.

Add component tests for:

- automatic versus field-hotspot selection;
- transition banner and recovery action;
- single-radio explanation;
- successful reconnection at `10.42.0.1`;
- timeout recovery instructions.

Run the repository’s full verification commands, including lint, formatting,
type-checking, unit tests, coverage, and production/native builds.

## Manual acceptance scenarios

1. **Home boot**
   - PINS is in `auto`.
   - It joins home Wi-Fi.
   - Browser and app find the same `rigId`.
   - Both show the same live rig state.

2. **Outdoor cold boot**
   - Saved home SSID is absent.
   - PINS exposes its `pins-*` hotspot at `10.42.0.1`.
   - Android app discovers or falls back to the fixed address.
   - Browser can open the rig by direct address.

3. **Explicit field mode**
   - Select field hotspot.
   - The original LAN endpoint disappears.
   - The UI shows transition instead of immediate failure.
   - The app reconnects at `10.42.0.1`.
   - Reboot PINS and verify hotspot mode remains selected.

4. **Return home**
   - Select automatic mode while connected to the hotspot.
   - The app probes both old and LAN candidates.
   - It reconnects to the same `rigId` on the LAN.

5. **Router outage**
   - Remove the home AP.
   - PINS falls back to hotspot.
   - Once hotspot is active, it remains stable and does not repeatedly steal the
     single radio back for client retries.

6. **Two clients**
   - Open Android and a desktop browser.
   - Change a rig-owned setting on one.
   - The other receives/reloads the newer revision.
   - Device-local layout preferences remain different.

7. **Wrong nearby rig**
   - Make another PINS daemon reachable.
   - Verify the selected logical rig never silently switches to the other rig.

## Delivery requirements

Return:

- a concise architecture summary;
- a list of modified files;
- migration notes;
- API compatibility notes;
- test commands and results;
- remaining platform limitations, especially browser handling of no-Internet Wi-Fi.

Do not claim success without running the available verification suite. Do not make
changes to the PINS daemon repository as part of the Touch-N-Stars task.
