#!/usr/bin/env python3
"""Plan checks and the final outcome of a PINS system upgrade.

system-upgrade.sh collects raw APT state into a work directory and calls this
helper. Decisions are based on the machine-readable `apt-get -s` action lines
(Inst/Remv), `apt-mark showhold`, `dpkg-query` and `apt-cache policy` (all run
with LC_ALL=C), never on the human summary line alone.

Work directory files (written by system-upgrade.sh):
  plan.txt / plan.rc           simulation before the transaction
  upgrade.rc                   exit status of the real `apt-get upgrade`
  targeted-plan.txt / .rc      simulation of the explicit PINS package install
  targeted.rc                  exit status of the explicit PINS package install
  verify.txt / verify.rc       simulation after the transaction
  held.txt                     apt-mark showhold
  installed.txt                dpkg-query: package, version, status abbrev
  policy.txt                   apt-cache policy for installed PINS packages
  dpkg-audit.txt               dpkg --audit after the transaction
"""
import argparse
import fnmatch
import json
import os
import re
import sys

PINS_PACKAGE_PATTERNS = ("pins", "pinsdaemon", "pins-*")

EXIT_OK = 0
EXIT_PINS_PENDING = 20
EXIT_DEPENDENCY_PROBLEM = 21
EXIT_REMOVAL_REQUIRED = 22
EXIT_APT_FAILED = 23
EXIT_DPKG_INTERRUPTED = 24
EXIT_PROTECTED_PACKAGE_CHANGE = 25

CODES = {
    EXIT_PINS_PENDING: "UPGRADE_PINS_PACKAGE_PENDING",
    EXIT_DEPENDENCY_PROBLEM: "UPGRADE_DEPENDENCY_PROBLEM",
    EXIT_REMOVAL_REQUIRED: "UPGRADE_REMOVAL_REQUIRED",
    EXIT_APT_FAILED: "UPGRADE_APT_FAILED",
    EXIT_DPKG_INTERRUPTED: "UPGRADE_DPKG_INTERRUPTED",
    EXIT_PROTECTED_PACKAGE_CHANGE: "UPGRADE_PROTECTED_PACKAGE_CHANGE",
}

MAX_DIAGNOSTIC_LINES = 20

_INST_RE = re.compile(r"^Inst (\S+) (?:\[([^\]]+)\] )?\((\S+)")
_REMV_RE = re.compile(r"^Remv (\S+)(?: \[([^\]]+)\])?")
# apt-get (classic) and apt 3.x wording for packages an upgrade leaves alone.
_KEPT_BACK_HEADERS = ("The following packages have been kept back:", "Not upgrading:")
_DIAGNOSTIC_RE = re.compile(
    r"^(E:|W: .*(held|broken|depend)|\s+\S+ : (Depends|PreDepends|Conflicts|Breaks):|\s+(Depends|PreDepends|Conflicts|Breaks):"
    r"|The following packages have unmet dependencies|Unable to correct problems|You have held broken packages)",
    re.IGNORECASE,
)


def strip_arch(name):
    return name.split(":", 1)[0]


def is_pins_package(name):
    return any(fnmatch.fnmatchcase(strip_arch(name), pattern) for pattern in PINS_PACKAGE_PATTERNS)


def parse_simulation(text):
    """Actions from `apt-get -s` output: installs, upgrades, removals, kept back."""
    plan = {"upgrade": [], "install": [], "remove": [], "keptBack": []}
    in_kept_back = False
    for line in (text or "").splitlines():
        if in_kept_back:
            if line.startswith(" ") and line.strip():
                plan["keptBack"].extend(strip_arch(n) for n in line.split())
                continue
            in_kept_back = False
        if line.strip() in _KEPT_BACK_HEADERS:
            in_kept_back = True
            continue
        match = _INST_RE.match(line)
        if match:
            name, old, new = strip_arch(match[1]), match[2], match[3]
            if old:
                plan["upgrade"].append({"name": name, "from": old, "to": new})
            else:
                plan["install"].append({"name": name, "from": None, "to": new})
            continue
        match = _REMV_RE.match(line)
        if match:
            plan["remove"].append({"name": strip_arch(match[1]), "from": match[2]})
    plan["keptBack"] = sorted(set(plan["keptBack"]))
    return plan


def diagnostics(text):
    lines = [line.rstrip() for line in (text or "").splitlines() if _DIAGNOSTIC_RE.search(line)]
    return lines[:MAX_DIAGNOSTIC_LINES]


def parse_installed(text):
    """{name: version} of fully installed packages from dpkg-query output."""
    installed = {}
    for line in (text or "").splitlines():
        parts = line.split("\t")
        # Status-Abbrev: wanted state (i/h/...), then current state ("i" = installed).
        if len(parts) >= 3 and parts[2][1:2] == "i":
            installed[strip_arch(parts[0])] = parts[1]
    return installed


def parse_policy(text):
    """{name: {"installed": v, "candidate": v}} from `apt-cache policy` output."""
    result = {}
    current = None
    for line in (text or "").splitlines():
        header = re.match(r"^(\S+):$", line)
        if header:
            current = strip_arch(header[1])
            result[current] = {"installed": None, "candidate": None}
            continue
        field = re.match(r"^\s+(Installed|Candidate): (\S+)", line)
        if field and current:
            value = None if field[2] == "(none)" else field[2]
            result[current][field[1].lower()] = value
    return result


def pins_pending(policy):
    """Installed PINS packages whose selected candidate is not installed."""
    pending = []
    for name, versions in sorted(policy.items()):
        if not is_pins_package(name):
            continue
        installed, candidate = versions["installed"], versions["candidate"]
        if installed and candidate and installed != candidate:
            pending.append({"name": name, "installed": installed, "candidate": candidate})
    return pending


def check_plan(text, rc, protected):
    """(exit code, message) for a simulation, before anything is changed."""
    if rc != 0:
        detail = "; ".join(line.strip() for line in diagnostics(text)) or f"apt-get exited with {rc}"
        return EXIT_DEPENDENCY_PROBLEM, f"APT cannot resolve the upgrade: {detail}"
    plan = parse_simulation(text)
    if plan["remove"]:
        names = ", ".join(item["name"] for item in plan["remove"])
        return EXIT_REMOVAL_REQUIRED, (
            f"The upgrade would remove packages ({names}); nothing was changed. "
            "Review the change manually before upgrading."
        )
    touched = sorted(
        item["name"] for item in plan["upgrade"] + plan["install"] if item["name"] in protected
    )
    if touched:
        return EXIT_PROTECTED_PACKAGE_CHANGE, (
            f"The upgrade would change pinned kernel packages ({', '.join(touched)}); nothing was changed."
        )
    return EXIT_OK, "ok"


def _read(workdir, name):
    path = os.path.join(workdir, name)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return None


def _read_rc(workdir, name):
    raw = _read(workdir, name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw.strip())
    except ValueError:
        return None


def _names(items):
    return ", ".join(f"{item['name']} {item['installed']} -> {item['candidate']}" for item in items)


def evaluate(workdir, protected):
    plan = parse_simulation(_read(workdir, "plan.txt"))
    verify_text = _read(workdir, "verify.txt")
    verify = parse_simulation(verify_text)
    held = sorted({line.strip() for line in (_read(workdir, "held.txt") or "").splitlines() if line.strip()})
    approved_holds = sorted(name for name in held if name in protected)
    pending_pins = pins_pending(parse_policy(_read(workdir, "policy.txt")))
    pending_pins_names = {item["name"] for item in pending_pins}
    upgrade_rc = _read_rc(workdir, "upgrade.rc")
    targeted_rc = _read_rc(workdir, "targeted.rc")
    audit = (_read(workdir, "dpkg-audit.txt") or "").strip()

    still_changing = sorted({item["name"] for item in verify["upgrade"] + verify["install"]})
    kept_back = sorted(
        name for name in set(verify["keptBack"]) | set(still_changing)
        if name not in held and name not in pending_pins_names
    )
    changed = plan["upgrade"] + plan["install"]

    report = {
        "outcome": None,
        "errorCode": None,
        "message": None,
        "exitCode": EXIT_OK,
        "changedPackages": changed,
        "newPackages": [item["name"] for item in plan["install"]],
        "pendingPinsPackages": pending_pins,
        "keptBackPackages": kept_back,
        "heldPackages": held,
        "approvedKernelHolds": approved_holds,
        "diagnostics": [],
    }

    def finish(outcome, exit_code, message, extra_diagnostics=()):
        report.update(
            outcome=outcome,
            exitCode=exit_code,
            errorCode=CODES.get(exit_code),
            message=message,
            diagnostics=list(extra_diagnostics)[:MAX_DIAGNOSTIC_LINES],
        )
        return report

    if upgrade_rc not in (None, 0):
        return finish(
            "failed", EXIT_APT_FAILED,
            f"apt-get upgrade failed with exit code {upgrade_rc}; see the upgrade log for details.",
        )
    if audit:
        return finish(
            "interrupted", EXIT_DPKG_INTERRUPTED,
            "Package installation was interrupted and could not be completed automatically. "
            "Run 'sudo dpkg --configure -a' and upgrade again.",
            audit.splitlines(),
        )
    if pending_pins:
        reason = [
            f"{name} is on hold (apt-mark unhold {name} to allow updates)."
            for name in sorted(pending_pins_names & set(held))
        ]
        reason += diagnostics(_read(workdir, "targeted-plan.txt"))
        if targeted_rc not in (None, 0):
            reason.insert(0, f"Explicit install of the pending PINS packages failed with exit code {targeted_rc}.")
        targeted_check = _read(workdir, "targeted-check.txt")
        if targeted_check and targeted_check.strip():
            reason.insert(0, targeted_check.strip())
        names = " ".join(item["name"] for item in pending_pins)
        return finish(
            "incomplete", EXIT_PINS_PENDING,
            f"Upgrade incomplete: PINS packages are still pending ({_names(pending_pins)}). "
            f"Inspect with 'sudo apt-get install --only-upgrade {names}'.",
            reason,
        )
    if kept_back:
        return finish(
            "partial", EXIT_OK,
            f"Upgrade finished, but {len(kept_back)} package(s) were kept back: {', '.join(kept_back)}.",
            diagnostics(verify_text),
        )
    if changed:
        return finish("upgraded", EXIT_OK, f"Upgraded {len(changed)} package(s).")
    return finish("up_to_date", EXIT_OK, "System is already up to date.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check-plan", help="refuse unsafe simulations before changing anything")
    p.add_argument("simulation")
    p.add_argument("--rc", type=int, default=0)
    p.add_argument("--protected", nargs="*", default=[])
    p.add_argument("--report", help="write a blocked-upgrade report here when the plan is refused")

    p = sub.add_parser("pins-installed", help="print installed PINS package names")
    p.add_argument("installed")

    p = sub.add_parser("pins-pending", help="print PINS packages whose candidate is not installed")
    p.add_argument("policy")

    p = sub.add_parser("evaluate", help="write report.json and print the outcome")
    p.add_argument("workdir")
    p.add_argument("--protected", nargs="*", default=[])

    args = parser.parse_args(argv)

    if args.command == "check-plan":
        with open(args.simulation, encoding="utf-8", errors="replace") as f:
            text = f.read()
        code, message = check_plan(text, args.rc, set(args.protected))
        if code != EXIT_OK:
            print(message)
        if args.report and code != EXIT_OK:
            report = {
                "outcome": "blocked",
                "errorCode": CODES.get(code),
                "message": message,
                "exitCode": code,
                "plannedChanges": parse_simulation(text),
                "diagnostics": diagnostics(text),
            }
            with open(args.report, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=2)
                f.write("\n")
        return code

    if args.command == "pins-installed":
        with open(args.installed, encoding="utf-8", errors="replace") as f:
            for name in sorted(parse_installed(f.read())):
                if is_pins_package(name):
                    print(name)
        return EXIT_OK

    if args.command == "pins-pending":
        with open(args.policy, encoding="utf-8", errors="replace") as f:
            for item in pins_pending(parse_policy(f.read())):
                print(item["name"])
        return EXIT_OK

    report = evaluate(args.workdir, set(args.protected))
    with open(os.path.join(args.workdir, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.write("\n")
    print(f"PINS_UPGRADE_RESULT outcome={report['outcome']} code={report['errorCode'] or 'SUCCESS'}")
    print(report["message"])
    for line in report["diagnostics"]:
        print(f"  {line}")
    return report["exitCode"]


if __name__ == "__main__":
    sys.exit(main())
