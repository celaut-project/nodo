"""``nodo devices`` -- the PCI devices this node gives to guests with VFIO (#536).

EXPERIMENTAL (``devices.ENABLED``). For each address in ``devices.ALLOW``: the chip
read from sysfs, the host checks (driver, IOMMU group, reset, /dev/vfio), the price,
the kernels it is offered with, and its state: free, assigned to an instance,
quarantined, or why it is not offered.

    nodo devices [--json]          the list (the TUI reads --json)
    nodo devices clear [<address>] offer a quarantined device again (all with no address)

Read-only except ``clear``.
"""
import json
import sys
import time
from typing import List, Optional


def _print(rep: dict) -> None:
    if not rep["enabled"]:
        print("Devices are off (devices.ENABLED: false). EXPERIMENTAL: see docs/CONFIG.md, devices.")
    if not rep["devices"]:
        print("No device is listed in devices.ALLOW.")
    for problem in rep["problems"]:
        print(f"[WARN] {problem}")
    if rep["kernels"]:
        print("Kernels offered with the devices:")
        for k in rep["kernels"]:
            claims = f" (claims {', '.join(c[:12] for c in k['claims'])})" if k["claims"] else ""
            print(f"  {k['hash']}{claims}")
    for d in rep["devices"]:
        state = d["state"]
        if state == "assigned":
            state = f"assigned to {d['assigned_to'][:16]}"
        elif state == "quarantined":
            state = f"quarantined: {d['quarantine'].get('reason', '')}"
        print(f"\n{d['address']}  {d['chip'] or '?'}  [{state}]")
        if d["price_mu_per_second"] is not None:
            print(f"  price   {d['price_mu_per_second']} MU/s")
        if d["mem_limit"] is not None:
            print(f"  memory  {d['mem_limit']} bytes")
        if d["watts"] is not None:
            print(f"  power   {d['watts']} W (configured)")
        for f in d["findings"]:
            if f["level"] != "OK":
                print(f"  [{f['level']}] {f['message']}")
                if f["suggestion"]:
                    print(f"     {f['suggestion']}")
        if d["state"] == "no price":
            print(f"  [WARN] devices.BY_CHIP.{d['chip']}.mu_per_second is not set: not offered.")


def devices(argv: Optional[List[str]] = None) -> bool:
    argv = list(argv or [])
    as_json = "--json" in argv
    args = [a for a in argv if a != "--json"]
    try:
        from src.devices import inventory

        if args and args[0] == "clear":
            address = None
            if len(args) > 1:
                from src.devices.host import normalize_address

                address = normalize_address(args[1])
                if not address:
                    print(f"'{args[1]}' is not a PCI address.")
                    return _flushed(False)
            cleared = inventory.clear_quarantine(address)
            if as_json:
                json.dump({"cleared": cleared}, sys.stdout)
                print()
            else:
                print(f"Cleared: {', '.join(cleared)}" if cleared else "Nothing was quarantined.")
            return _flushed(True)
        if args:
            print("usage: nodo devices [--json] | nodo devices clear [<address>]")
            return _flushed(False)
        rep = inventory.report()
    except Exception as e:
        if as_json:
            json.dump({"error": str(e), "read_at": int(time.time())}, sys.stdout)
            print()
        else:
            print(f"Could not read the devices: {e}")
        return _flushed(False)
    if as_json:
        json.dump(dict(rep, read_at=int(time.time())), sys.stdout)
        print()
    else:
        _print(rep)
    return _flushed(True)


def _flushed(result: bool) -> bool:
    sys.stdout.flush()
    return result
