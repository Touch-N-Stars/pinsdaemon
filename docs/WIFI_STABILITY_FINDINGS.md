# Wi-Fi stability investigation — 2026-11-01

## Findings and changes

The connection commit path accepts a default gateway without pinging it.
The watchdog previously required the same gateway to answer ICMP on every
check. Three failures caused fallback, matching the reported approximately
30-second disconnect. A router that filters ICMP could therefore lose an
otherwise working PINS connection. This is a demonstrated failure mechanism,
not proof that every reported customer disconnect has this cause.

The viability helper now tries a fresh, bounded, interface-bound ARP request
after failed ICMP. It requires an active NetworkManager infrastructure client
with valid IPv4, an on-link gateway, and a gateway address that is not local.
Association alone or a cached neighbour entry does not count. Both probes
failing still causes normal fallback. Gatewayless PINS identity validation,
API responses, radio coordination locks, and authentication are unchanged.
The Debian package now explicitly depends on `iputils-arping`.

New AP profiles explicitly use `proto=rsn`, `pairwise=ccmp`, `group=ccmp`.
Unspecified band/channel now means 2.4 GHz/channel 6. Explicit selections
remain supported; country settings are not overridden. Settings are supplied
in the initial profile creation instead of ignoring subsequent modification
errors. Existing APs adopt the settings at their next recreation.

On the supplied Pi, the original profile had unspecified band/channel and
security protocol/cipher lists; the actual radio used channel 11. This does
not establish that channel 13 or TKIP caused that device's trouble. Channel
13 visibility can depend on client/regulatory support. USB 3 interference
with 2.4 GHz is a plausible physical factor, but was not measured here.
Channel 6 cannot eliminate broadband USB interference. Physical separation,
better shielding, or an explicitly selected supported 5 GHz band can help;
no USB mode or country was changed.

## Shipped ASIAIR evidence

The supplied `ASIAIR_PRO_V1.4.img` is 31,914,983,424 bytes. Its MBR points to
the root filesystem at sector 46,712,832 and the writable `/home/pi`
filesystem at sector 46,221,312 (512-byte sectors). The image was opened
read-only with Dissect ExtFS 3.15; selected files were read without mounting
or executing firmware. Firmware extraction/scan via REA requires Linux tools
and its signature scan has a 128 MiB limit, so it does not cover this Windows
host/whole-image combination. REA also rejects the extracted shell script
as an unsupported binary format. The following findings come directly from
the shipped text files, not native decompilation or runtime observation.

| Firmware file | Observed settings | SHA-256 |
| --- | --- | --- |
| `/home/pi/AP_2.4G.conf` | `uap0`, channel 11, WPA2, RSN CCMP | `027cd71b53d21ccc1e87a994d3a316f1c5a166e47c01d1880d01191e1563c9f9` |
| `/home/pi/AP_5G.conf` | `uap0`, channel 36, WPA2, RSN CCMP | `ef5d533a7e84234a62fa5ff287ad307782898261c28dd8a59256cd474c2004f5` |
| `/home/pi/ASIAIR/asiair.sh` | Starts hostapd/dnsmasq and optional wpa_supplicant, invokes bridgeAP | `16b52969d2b43b3157f29f1d96c43e4a6fe65f8a39bee1d83430a0ae78dff836` |
| `/home/pi/bridgeAP` | Forwarding/NAT between `uap0` and `wlan0` | `ba527853b93620245f3befa510773e7b6f7a49e81f7287997342f00b2e5f4211` |
| `/home/pi/ASIAIR/bin/network.sh` | wpa_cli management, association/IP/routing status, channel updates | `9fe287dbc94f7b45ba8a57901f8fb87a1ea2c265bcab1752a8b080299b3f574b` |

The startup and configuration support a separate virtual AP/client design.
This is not evidence that arbitrary PINS adapters support concurrent AP/STA,
so pinsdaemon retains its existing single/dual-adapter behavior. ASIAIR's
hardcoded country codes and NAT design were not adopted. No proprietary
firmware code or credentials were added to this repository.

## Verification

On the authorized Raspberry Pi (aarch64, NetworkManager 1.52.1):

- All 195 unit tests passed, including executable Bash AP-profile tests.
- All shipped shell scripts and maintainer scripts passed `bash -n` after
  normalizing the Windows staging archive to the LF bytes used by Git/CI.
- Live AP recreation produced channel 6 / 2437 MHz, RSN, CCMP pairwise/group,
  and `10.42.0.1/24`. Its password was compared privately with the original
  NetworkManager backup and matched.
- Activated an existing saved client profile, blocked incoming gateway ICMP
  echo replies only on `wlan0` in a temporary nftables table, and confirmed
  `gateway_arp_reachable`. Four explicit watchdog invocations across more
  than 40 seconds all retained the client and recorded zero failures.
- Disconnected `wlan0`; the third explicit failed watchdog check restored
  the hotspot with the required IPv4 address.
- Temporary firewall rules were removed, original desired network mode was
  restored, and the watchdog timer was restarted. SSH stayed on Ethernet.
- Executed the dedicated unstable workflow's daemon packaging function on
  the Pi: all 195 tests passed, `pinsdaemon_1.0.6~unstable+999.1_arm64.deb`
  built successfully, and package contents, metadata, checksum and Debian
  version ordering checks passed. The full PINS/plugin build was not run.
- Workflow lint passed for the dedicated unstable workflow.

Windows full-suite execution has a pre-existing Linux-only `fcntl` import
limitation; Linux/Pi execution above is the complete suite result.
These checks do not establish phone visibility on every handset, USB RF
performance, or the cause of an individual customer's IoT-network failure.

The two changed runtime scripts are deployed on the Pi. Original scripts,
NetworkManager profiles and network configuration are retained there under
`/var/backups/pins-wifi-20261022-001843` (the Pi's own clock supplied this name).
To roll back, stop the watchdog timer, restore the original two scripts and
network configuration from that backup under the Wi-Fi coordination lock,
then recreate the hotspot and restart the timer. A previously absent
`hotspot_config.json` was created to preserve the active AP password; retain
it while recreating the AP so its password is not reset to the default.

## Unstable builds

The existing dedicated `build-unstable-packages.yml` in the
`pins-workflows-develop` worktree checks out `Touch-N-Stars/pinsdaemon` at
`unstable`, tests it, builds its package and checksum, records its source
commit, and includes it in the six-package validation and prerelease.
The normal workflows in both repositories are unchanged.

Daemon versions follow that workflow's existing `BASE~unstable+RUN.ATTEMPT`
convention, sorting below the corresponding stable base version. Switching
an already installed stable package to the same base's unstable package may
therefore require an explicit downgrade. The new package membership check
fully consumes the tar listing before checking paths, avoiding SIGPIPE with
`pipefail`.

## Documentation consulted

- [NetworkManager connectivity states](https://networkmanager.dev/docs/api/latest/nm-dbus-types.html): local/site connectivity differs from Internet access.
- [NetworkManager Wi-Fi security settings](https://networkmanager.dev/docs/api/latest/nm-settings-nmcli.html): RSN means WPA2; unset protocol lists allow WPA and RSN.
- [iputils arping manual](https://man7.org/linux/man-pages/man8/arping.8.html): interface binding, reply count, deadline and CAP_NET_RAW.
- [actions/checkout](https://github.com/actions/checkout): explicit cross-repository branch selection.
- [Apple wireless interference guidance](https://support.apple.com/en-us/102319): USB 3/Thunderbolt interference and physical mitigation.

NetworkManager and checkout references were queried through Context7.
