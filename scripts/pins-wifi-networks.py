#!/usr/bin/env python3
"""Restricted sudo facade for non-secret saved client network management.

Connection provisioning (stdin secrets/config paths) is deliberately unavailable
through this facade. Network transitions remain owned by wifi-connect.sh.
"""
import argparse
import os
import re
import uuid


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", choices=("list", "update", "forget"), required=True)
    parser.add_argument("--client-iface", required=True)
    parser.add_argument("--profile-uuid", type=lambda value: str(uuid.UUID(value)))
    parser.add_argument("--priority", type=int)
    parser.add_argument("--auto-connect", choices=("yes", "no"))
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9._-]+", args.client_iface):
        parser.error("Invalid Wi-Fi interface")
    if args.priority is not None and not -999 <= args.priority <= 999:
        parser.error("Priority must be between -999 and 999")
    command = [os.path.join(os.path.dirname(__file__), "pins-wifi-profile.py"),
               "--action", args.action, "--client-iface", args.client_iface]
    if args.profile_uuid:
        command.extend(["--profile-uuid", args.profile_uuid])
    if args.priority is not None:
        command.extend(["--priority", str(args.priority)])
    if args.auto_connect is not None:
        command.extend(["--auto-connect", args.auto_connect])
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
