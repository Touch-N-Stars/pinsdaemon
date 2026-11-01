# Agent guide: future hotspot improvements

Updated: 2026-11-01. Status: implementation roadmap, not an enabled feature.

## Objective

Keep Touch-N-Stars reachable while a rig joins, loses, or changes its router
connection. The next major candidate is an optional persistent hotspot plus
client connection on one capable Wi-Fi radio, with deterministic recovery.

Use this document with [AGENT_GUIDELINES.md](AGENT_GUIDELINES.md). Execute one
bounded phase at a time. Record the intended behavior, acceptance criteria,
verification results, and unresolved hardware questions before moving on.
The Planner, Engineer, Developer and Reviewer roles can be performed by one
agent; this document does not require spawning agents.

## Read before editing

| Concern | Owning files |
| --- | --- |
| API contracts and interface/status reporting | `app/main.py`, `README.md` |
| Persisted intent, interface selection and AP settings | `app/wifi_config.py`, `app/hotspot_config.py`, `app/wifi_regulatory.py` |
| Client/AP transitions and rollback | `scripts/wifi-connect.sh`, `scripts/pins-wifi-profile.py` |
| Connection liveness and peer identity | `scripts/pins-wifi-viability.py` |
| Automatic recovery, retries and manual hand-off | `scripts/pins-wifi-watchdog.sh`, `scripts/wifi-automanage.py`, `scripts/90-pins-wifi-recovery` |
| Deployment, privileges and diagnostics | `packaging/`, `systemd/`, `scripts/collect-diagnostics.sh`, `.github/workflows/` |
| Existing regression coverage | `tests/test_wifi_*.py`, especially viability, hotspot profile, recovery coordination and saved networks |

Also read [WIFI_MULTINETWORK_VALIDATION.md](WIFI_MULTINETWORK_VALIDATION.md). Reinspect
the current implementation: this guide describes the baseline at `89bfda8`,
not a guarantee that later branches still behave identically.

Use Context7 and current official/local references before changing
NetworkManager, `nmcli`, `iw`, systemd or framework-specific behavior. Cite
the references used in the implementation handoff.

## Baseline to preserve

- Default AP address: `10.42.0.1/24`. Its local-only DHCP omits gateway/DNS
  options so phones can retain cellular Internet routing.
- New AP profiles explicitly request WPA2/RSN and CCMP pairwise/group ciphers.
  Unspecified radio settings currently select 2.4 GHz/channel 6; explicit
  supported settings and the configured regulatory country remain relevant.
- Gateway liveness uses interface-bound ICMP, then fresh on-link ARP if ICMP
  fails. The ARP path requires a connected infrastructure profile with IPv4
  and excludes local/self gateway addresses. `iputils-arping` is a dependency.
- Gatewayless PINS-to-PINS links require the existing verified peer identity
  path. Do not replace it with association, a DHCP lease, or cached ARP alone.
- Automatic fallback requires consecutive failures; a single failed probe
  must not tear down a usable connection.
- Saved profiles, priorities, explicit auto-connect choices and manual mode
  selection survive transitions. Failed joins must retain useful profiles.
- Mutations share `/run/pins-wifi-coordination.lock`. The timer owns automatic
  recovery; the NetworkManager dispatcher does not launch recovery commands.
- Single-radio background retries protect associated hotspot users. Distinct
  adapters can provide parallel service, subject to subnet-conflict handling.
- Two PINS hotspots both using `10.42.0.1/24` need the existing peer/subnet
  conflict safeguards; concurrency does not make overlapping subnets safe.

## Hardware constraint already observed

A read-only query on the authorized PINS test Pi, running kernel
`6.18.50+rpt-rpi-2712`, advertised:

```text
#{ managed } <= 1, #{ AP } <= 1,
#{ P2P-client } <= 1, #{ P2P-device } <= 1,
total <= 4, #channels <= 1
```

This is evidence of advertised AP+station capability with **one shared
channel**. A different advertised two-channel combination did not include an
AP. No virtual interface was created during that query.

Consequently, keeping an AP fixed on channel 6 while joining a router on
channel 11 is not a valid design for that combination. Advertised support
also does not prove reliable channel migration or continued phone access.
Record capability, driver, firmware, kernel, regulatory domain and observed
channel behavior for each adapter tested; never generalize one Pi's result.

Channel 13 visibility and USB 3 interference remain possible causes on some
systems, not validated explanations for the supplied Pi. Its original AP
used channel 11. Changing a channel cannot eliminate broadband USB noise.

## Implementation phases

### Phase 1 — Discover topology without changing connectivity

Map each configured Wi-Fi interface to its physical `phy` and collect its
supported interface combinations and regulatory/channel restrictions. Keep
device identity distinct from transient interface names. Treat missing or
ambiguous capability data as unavailable, not supported.

The first deliverable should distinguish:

1. One interface requiring client/AP switching.
2. Two interfaces sharing a radio and its channel constraint.
3. Two independent radios.

Acceptance: mocked tests cover all three, missing interfaces, malformed probe
output and unavailable tools. An authorized read-only hardware query agrees
with the reported topology. There are no NetworkManager profile mutations.

### Phase 2 — Specify optional persistent AP+STA transitions

Write a transition table before implementation. Keep current behavior as the
default until a separately tested rollout enables the new capability.
Define intent separately from observed state: AP ready, client associating,
IPv4 pending, client usable, router unavailable, and recovery in progress.
These are design states, not new public API values to introduce implicitly.

Specify AP/client band and channel compatibility, channel migration, router
roaming, activation timeouts, retry ownership and each failure's rollback.
Decide how an explicit fixed AP channel interacts with concurrent mode; do
not silently ignore a user's setting. Unsupported combinations must retain
or restore a reachable AP and report the reason.

Acceptance: every transition has a bounded outcome and a recovery action.
Review the design against existing endpoint/response contracts and peer-subnet
handling. Document unavoidable phone interruptions instead of promising
seamless service without measurements.

### Phase 3 — Implement managed virtual-interface lifecycle

Choose NetworkManager or another clearly defined owner for both interfaces.
Do not add a competing hostapd/dnsmasq stack beside NetworkManager without
explicitly redesigning service ownership. Keep DHCP ownership unambiguous.

Create only daemon-owned virtual interfaces/profiles, using validated names
and kernel length limits. Make creation, restart and cleanup idempotent.
Track ownership so rollback never deletes a user's interface or unrelated AP
profile. Reconcile stale state after reboot or daemon failure under the
existing Wi-Fi coordination lock.

Acceptance: an unsupported driver, failed interface creation, failed AP
activation or killed job cannot leave both access paths down indefinitely.
Independent-adapter behavior and the existing single-interface fallback still
pass. No background scan/retry policy mistakes a shared radio for two radios.

### Phase 4 — Preserve AP access during client changes

Coordinate the AP with the router's channel when required by the radio.
Handle client IPv4 configuration and liveness independently of AP readiness.
Keep current ICMP/ARP and verified-peer rules; association alone does not prove
a usable router connection. Router failure must not trigger unnecessary AP
recreation in a topology where the AP is already usable.

Acceptance: record actual AP frequency, DHCP lease, client association and
Touch-N-Stars/API reachability through each transition. Test a phone already
connected to the AP while the client joins and loses its router. Retain
credentials and saved-network preferences through failures and restart.

### Phase 5 — Expose actionable diagnostics and roll out

Report logical roles, shared/independent radio topology, effective band and
channel, liveness reason, last transition and recovery result without secrets.
Keep additions backward compatible and long operations job-owned. Distinguish
configured settings from effective shared-channel settings.

Acceptance: an agent/supporter can explain a failed transition from a redacted
diagnostic bundle. The unstable package includes every required helper and
dependency. Existing configurations load without forcing a mode change.

## Required verification matrix

| Scenario | Required result/evidence |
| --- | --- |
| Router blocks ICMP but answers fresh ARP | Client remains healthy beyond the failure window. |
| Router loses Internet only | No fallback based solely on public Internet connectivity. |
| Router/client link becomes unusable | Recovery remains bounded; usable AP access is retained/restored. |
| DHCP is delayed or fails | Association, IPv4 readiness and liveness are not conflated. |
| Bad credentials, hidden/unavailable SSID | Useful error, saved profiles retained, AP recoverable. |
| Router uses a different channel or roams | Shared-radio behavior is measured; configuration conflict is explicit. |
| AP has an associated phone during retries | Background recovery does not repeatedly evict the phone. |
| Explicit 5 GHz AP plus incompatible client | No silent setting downgrade or indefinite loss of both paths. |
| Gatewayless PINS peer or overlapping subnet | Existing peer-identity and subnet-conflict safeguards hold. |
| Two real adapters | Existing parallel behavior does not regress. |
| Unsupported adapter or unavailable capability probe | Current fallback behavior remains available. |
| Job killed, daemon restart, reboot, package upgrade | Ownership/state reconciliation works without retry storms. |
| Country/channel restrictions | No hardcoded country overrides; unsupported channels fail predictably. |

Run tests from an isolated Linux environment, with mocked networking unless a
lab device is authorized in the active session. Start with relevant Wi-Fi tests,
then run the complete suite and shell checks before a networking handoff:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m unittest discover -s tests -p 'test_wifi_*.py' -v
.venv/bin/python -m unittest discover -s tests -p 'test_*.py' -v
bash -n scripts/*.sh scripts/pins-rig-name scripts/90-pins-wifi-recovery packaging/DEBIAN/postinst packaging/DEBIAN/prerm
git diff --check
```

The baseline passed 195 tests on the Pi. Its live ICMP-filtered client stayed
healthy through four explicit watchdog checks spanning more than 40 seconds;
a separate disconnected-radio test restored the AP on the third failed check.
Those results validate the existing liveness fix, **not** future AP+STA mode.
Windows cannot run every existing Linux-only test, notably the `fcntl` import.

## Lab recovery and rollback

Before a radio-changing test, use an independently verified Ethernet/console
path and retain the original profile/configuration metadata and permissions.
Keep credentials in protected backups, never command output or this document.
Arrange a local rollback action that does not depend on the client Wi-Fi link
being tested. Reconcile the lock before invoking scripts that acquire it.

Rollback must restore the prior desired mode and owned profile/interface state,
preserve the AP password, remove only test-created resources, restore the timer
to its original state, and verify the AP address plus actual user access.
Retain the existing AP implementation as the fallback for an unsuccessful
concurrent-mode rollout. Do not reboot or change unrelated host services as
an unexplained recovery shortcut.

## Branch and packaging boundaries

Keep pinsdaemon development on `Touch-N-Stars/pinsdaemon:unstable` unless the
active task says otherwise. The dedicated packaging workflow lives in
`acocalypso/pins:develop` at `.github/workflows/build-unstable-packages.yml`.
Its core source checkout is intentionally **`nitr57/pins:unstable`**; its
daemon checkout is **`Touch-N-Stars/pinsdaemon:unstable`**. The repository that
hosts a workflow is not necessarily the repository it builds.

Do not alter normal/release workflows for an unstable-only task. Keep package
metadata, dependencies and source-commit provenance accurate. A plain Debian
container needs the signed Raspberry Pi package source to resolve
`raspi-config`; do not remove runtime dependencies or skip installability
validation to make a build green. Verify the current workflow before editing.

## Completion checklist for the next implementation agent

- State the implemented phase and changed behavior; identify deferred phases.
- List the hardware/topology actually tested and the limits of that evidence.
- Preserve API/auth contracts, profile secrets, manual intent and lock ownership.
- Review the final diff, package contents, dependency resolution and rollback.
- Report test outcomes and unresolved failures without claiming untested phone
  stability, throughput or hardware compatibility.
- Update this guide and the nearest behavior documentation when a design
  decision or verified hardware result changes.

Suggested first task: **add read-only physical-radio topology discovery and
regression tests, without enabling virtual AP+STA or changing live connectivity.**
