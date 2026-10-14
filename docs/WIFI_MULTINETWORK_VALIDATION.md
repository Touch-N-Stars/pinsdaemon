# Saved Wi-Fi networks: implementation and validation

Validated on 2026-10-14 using a Raspberry Pi reached over Ethernet. The device
has one brcmfmac Wi-Fi adapter and NetworkManager 1.52.1. Existing profiles,
credentials and daemon configuration were backed up before radio tests.

## Behavior

NetworkManager stores credentials, auto-connect settings, priorities, bands and
hidden-network settings. Automatic selection tries compatible enabled client
profiles by descending priority, then descending last-used timestamp. It keeps
a healthy active connection. A single coordinated radio transition scans and
tries candidates before hotspot rollback. Updates no longer flatten priorities.

The watchdog retries after a 120-second cooldown. Single-radio background
attempts require successful station inspection and no associated AP clients.
Explicit automatic-mode requests can retry while AP clients are connected.
Persistent hotspot mode suppresses client retries. Separate adapters can retry
on the client radio while retaining AP access, subject to the existing PINS-peer
subnet-conflict handling.

The API and restricted sudo facade expose non-secret saved-profile metadata,
priority/auto-connect edits, UUID activation, and inactive-client deletion. AP
profiles and deletion of active client profiles are rejected. Jobs remain the
owner of long-running mutations, using the existing coordination lock.

## Verification

- 180 daemon unit/API/packaging tests passed on Linux in an isolated test venv
  using FastAPI 0.138.2, Pydantic 2.13.4, Starlette 1.8.0 and httpx2 2.13.1.
- 111 Wi-Fi tests passed locally, with one Linux-only check skipped on Windows.
- The frontend's full 908-test suite passed; the subsequently added saved-profile
  API-client test also passed. Frontend lint, translations and production build
  passed.
- Live Pi API probes verified bearer authentication, malformed UUID and priority
  rejection, and absence of AP profiles/passwords in the saved-network response.
- Temporary clones of an existing profile verified skipping an unavailable SSID,
  rejection of incorrect credentials on a higher-priority profile, and successful
  connection through the next profile.
- Editing preferences retained the healthy client connection. Explicit UUID
  activation retained disabled auto-connect and a custom priority. Active-profile
  deletion returned 409; inactive deletion completed through a job.
- No eligible saved networks produced a verified AP at 10.42.0.1. Background
  retries retained fallback, respected cooldown, and completed through the real
  watchdog without a coordination-lock deadlock.
- The deployed frontend index and its JS/CSS assets were verified over port 5000.
  A browser loaded the saved-network card and successfully saved the existing
  profile's unchanged settings without an error.

Radio integration tests used a single adapter. Dual-adapter station handling,
hidden-network selection, and AP-client retry safeguards have mocked regression
coverage; they were not exercised with a second adapter or a connected AP client.

After integration testing, temporary profiles were removed and the original
network configuration restored. At the user's request, the fixed daemon/helper
files and frontend were then deployed again and left running as a test deployment.
It does not change the installed Debian package version; a package update can
replace the test files until the fix is included in a released package.

## Documentation consulted

- Context7: FastAPI request fields/dependency authentication and Vue Composition
  API lifecycle/watchers.
- Context7: NetworkManager priority/autoconnect semantics and Starlette 1.8.0's
  TestClient dependency on httpx2.
- Existing project API, job, sudoers, systemd and Debian packaging contracts.
