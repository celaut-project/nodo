"""Does firewalld let the guest bridge forward? The question behind a silent failure.

nodo accepts guest traffic in its own tables (``inet nodo``, and ``ip filter``
through the compatibility chain). An ``accept`` only ends the chain it is in, so
the packet still crosses ``inet firewalld``'s ``filter_FORWARD`` (priority filter +
10), which ends in ``reject with icmpx admin-prohibited``. A guest sees that reject
as ``EHOSTUNREACH`` ("No route to host"). firewalld lets a forwarded packet through
in three ways only, and a node needs two of them:

* **guest to guest** (a parent and its child): the packet enters and leaves on the
  guest bridge, so it is forwarding *inside* the bridge's zone. That needs the
  bridge in a zone that has ``forward: yes``. A bridge in no zone is handled by the
  default zone, whose forward rule only names the interfaces listed in it, so the
  packet still reaches the reject.
* **guest to the Internet** (network tag ``*``, peers on other hosts): the packet
  leaves through the uplink, which is in another zone. Forwarding between zones
  needs a firewalld *policy* from the bridge's zone to the uplink's zone with
  target ``ACCEPT``. ``--add-forward`` does not do it.

Published ports (host to guest through DNAT) pass ``ct status dnat accept`` and
work either way, which is part of why the failure hides so well.

``assess`` reads firewalld's configuration and says which of the two paths it
blocks. It is a reading of the configuration, not a measurement: ``nodo doctor``
sends real packets and is the authority. What this buys is that the daemon can
say, at start and without a packet, what will break, and store it where ``nodo
info`` and ``nodo status`` read it without root.

``assess`` and the ``*_commands`` helpers change nothing. ``apply`` (used by the
installer) is the one function here that writes firewalld's configuration, and
only when it is asked to.
"""

import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]

#: Name of the zone the suggested commands create. Its own zone, not ``trusted``:
#: ``trusted`` would expose every port the host listens on to every guest
#: (docs/FIREWALL.md, "Sharing the host with another firewall").
GUEST_ZONE = "nodo"

#: Name of the policy that lets the guest zone forward to the rest of the world.
EGRESS_POLICY = "nodo-egress"

#: The chain that rejects. Named in every message, because it is the one an
#: operator has to look at and the one nodo cannot write to.
FIREWALLD_CHAIN = "inet firewalld filter_FORWARD"

_ANY = "ANY"


def _default_runner(command: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command), capture_output=True, text=True, check=False, timeout=10
    )


def _text(proc: subprocess.CompletedProcess) -> str:
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


@dataclass(frozen=True)
class BridgeZone:
    """What firewalld says about the guest bridge.

    ``running`` is False when firewalld is absent or stopped, in which case there
    is nothing to report. ``zone`` is None when firewalld has no zone for the
    bridge, and also when the answer could not be read: ``readable`` tells those
    apart, because "I could not look" must never be raised as an alert.
    """

    running: bool
    readable: bool = True
    zone: Optional[str] = None

    @property
    def unzoned(self) -> bool:
        """firewalld is running, was asked, and has the bridge in no zone."""
        return self.running and self.readable and self.zone is None


def _running(run: Runner) -> bool:
    if not shutil.which("firewall-cmd"):
        return False
    try:
        state = run(["firewall-cmd", "--state"])
    except Exception:
        return False
    # Exact: the answer is "running" or "not running", and a substring test would
    # read the second as the first.
    return state.returncode == 0 and _text(state).lower() == "running"


def _zone_of(interface: str, run: Runner) -> Tuple[bool, Optional[str]]:
    """``(readable, zone)`` for one interface; zone None means "no zone"."""
    try:
        proc = run(["firewall-cmd", f"--get-zone-of-interface={interface}"])
    except Exception:
        return False, None
    answer = _text(proc)
    # "no zone" is a successful answer on some versions and an error on others.
    if "no zone" in answer.lower():
        return True, None
    if proc.returncode != 0 or not answer:
        return False, None
    return True, answer.split()[0]


def bridge_zone(bridge: str, *, run: Optional[Runner] = None) -> BridgeZone:
    """Ask firewalld which zone ``bridge`` is in. Never raises."""
    run = run or _default_runner
    if not _running(run):
        return BridgeZone(running=False)
    readable, zone = _zone_of(bridge, run)
    return BridgeZone(running=True, readable=readable, zone=zone)


def _info(proc: subprocess.CompletedProcess) -> Dict[str, str]:
    """``key: value`` lines of ``--info-zone`` / ``--info-policy``, keys lowercased."""
    info: Dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        key, sep, value = line.strip().partition(":")
        if sep and key and " " not in key:
            info[key.lower()] = value.strip()
    return info


def _uplinks(run: Runner) -> List[str]:
    """Interfaces of the IPv4 default routes, in order, without duplicates."""
    try:
        proc = run(["ip", "-4", "route", "show", "default"])
    except Exception:
        return []
    found: List[str] = []
    for line in (proc.stdout or "").splitlines():
        words = line.split()
        if "dev" in words:
            index = words.index("dev") + 1
            if index < len(words) and words[index] not in found:
                found.append(words[index])
    return found


@dataclass(frozen=True)
class Assessment:
    """firewalld's verdict on the two forwarded guest paths, read from its config.

    ``guest_to_guest`` and ``egress`` are True (allowed), False (rejected by
    ``filter_FORWARD``) or None (could not tell). None is never an alert.
    """

    running: bool
    zone: Optional[str] = None            # the bridge's own zone; None = no zone
    effective_zone: Optional[str] = None  # the zone that handles it (default if none)
    zone_forward: Optional[bool] = None
    zone_target: Optional[str] = None
    uplinks: Tuple[str, ...] = ()
    uplink_zones: Tuple[str, ...] = ()
    egress_policy: Optional[str] = None
    guest_to_guest: Optional[bool] = None
    egress: Optional[bool] = None
    notes: Tuple[str, ...] = field(default_factory=tuple)

    @property
    def unzoned(self) -> bool:
        return self.running and self.zone is None and self.effective_zone is not None

    @property
    def blocked(self) -> bool:
        """firewalld rejects at least one of the two paths, as far as its config says."""
        return self.running and (self.guest_to_guest is False or self.egress is False)

    @property
    def conclusive(self) -> bool:
        return self.running and self.guest_to_guest is not None and self.egress is not None

    def problems(self, bridge: str) -> List[str]:
        """One line per blocked path, naming what is missing."""
        lines: List[str] = []
        if not self.running:
            return lines
        if self.guest_to_guest is False:
            if self.zone is None:
                lines.append(
                    f"guest to guest: {bridge} is in no firewalld zone, so the default zone "
                    f"'{self.effective_zone}' handles it, and its forward rule does not name "
                    f"{bridge}."
                )
            else:
                lines.append(
                    f"guest to guest: zone '{self.zone}' of {bridge} does not forward "
                    "(forward: no)."
                )
        if self.egress is False:
            uplink = ", ".join(self.uplink_zones) or "the uplink's zone"
            lines.append(
                f"guest to the Internet: no firewalld policy with target ACCEPT from zone "
                f"'{self.effective_zone}' to {uplink} (or ANY), so guests with network "
                "tag '*' and peers on other hosts are rejected."
            )
        return lines


def assess(bridge: str, *, run: Optional[Runner] = None) -> Assessment:
    """Read firewalld's configuration for ``bridge``. Never raises, never writes."""
    run = run or _default_runner
    try:
        return _assess(bridge, run)
    except Exception as e:  # advice about the host; never let it break a start
        return Assessment(running=True, notes=(f"could not read firewalld: {e!r}",))


def _assess(bridge: str, run: Runner) -> Assessment:
    if not _running(run):
        return Assessment(running=False)

    readable, zone = _zone_of(bridge, run)
    if not readable:
        return Assessment(running=True, notes=(f"the zone of {bridge} could not be read",))

    default = _text(run(["firewall-cmd", "--get-default-zone"])).split()
    default_zone = default[0] if default else None
    effective = zone or default_zone
    if not effective:
        return Assessment(running=True, zone=zone, notes=("the default zone could not be read",))

    zone_proc = run(["firewall-cmd", f"--info-zone={effective}"])
    if zone_proc.returncode != 0:
        return Assessment(
            running=True, zone=zone, effective_zone=effective,
            notes=(f"zone '{effective}' could not be read: {_text(zone_proc)}",),
        )
    info = _info(zone_proc)
    target = info.get("target", "default")
    forward = {"yes": True, "no": False}.get(info.get("forward", ""))
    accepts_all = target.upper() == "ACCEPT"

    # Guest to guest leaves on the interface it came in on. The zone's forward rule
    # matches the interfaces *listed* in the zone, so a bridge handled by the
    # default zone without being listed in it is not covered, forward or not.
    if accepts_all:
        guest_to_guest: Optional[bool] = True
    elif zone is None:
        guest_to_guest = False
    else:
        guest_to_guest = forward

    uplinks = _uplinks(run)
    uplink_zones: List[str] = []
    uplinks_listed = True
    for uplink in uplinks:
        ok, uplink_zone = _zone_of(uplink, run)
        if not ok:
            uplink_zones = []
            break
        uplinks_listed = uplinks_listed and uplink_zone is not None
        name = uplink_zone or default_zone
        if name and name not in uplink_zones:
            uplink_zones.append(name)

    policy = _egress_policy(effective, uplink_zones, run)
    # An ACCEPT policy out of the bridge's zone to ANY (or to that zone itself)
    # also covers guest to guest: tested on firewalld 1.1, where removing
    # --add-forward with nodo-egress in place left the parent-to-child path open.
    if guest_to_guest is False and zone is not None and _egress_policy(zone, [zone], run):
        guest_to_guest = True
    notes: List[str] = []
    if accepts_all:
        egress: Optional[bool] = True
    elif policy:
        egress = True
    elif not uplinks:
        egress = None
        notes.append("no IPv4 default route, so there is no uplink to assess")
    elif not uplink_zones:
        egress = None
        notes.append("the zone of the uplink could not be read")
    elif policy is None and not _policies_supported(run):
        egress = None
        notes.append("this firewalld has no policies (it predates 0.9)")
    elif all(name == effective for name in uplink_zones):
        # Same zone on both sides: intra-zone forwarding, whose rule matches the
        # *outgoing* interfaces listed in the zone (see above). That is how a bridge
        # in no zone still reaches the Internet when the uplink is listed in the
        # default zone (#534), while guest to guest fails.
        egress = bool(forward) and uplinks_listed
    else:
        egress = False

    return Assessment(
        running=True,
        zone=zone,
        effective_zone=effective,
        zone_forward=forward,
        zone_target=target,
        uplinks=tuple(uplinks),
        uplink_zones=tuple(uplink_zones),
        egress_policy=policy or None,
        guest_to_guest=guest_to_guest,
        egress=egress,
        notes=tuple(notes),
    )


def _policies_supported(run: Runner) -> bool:
    try:
        return run(["firewall-cmd", "--get-policies"]).returncode == 0
    except Exception:
        return False


def _egress_policy(zone: str, uplink_zones: Sequence[str], run: Runner) -> Optional[str]:
    """Name of an ACCEPT policy from ``zone`` to every uplink zone, "" if none, None if unknown."""
    try:
        listed = run(["firewall-cmd", "--get-policies"])
    except Exception:
        return None
    if listed.returncode != 0:
        return None
    for name in (listed.stdout or "").split():
        info = _info(run(["firewall-cmd", f"--info-policy={name}"]))
        if info.get("target", "").upper() != "ACCEPT":
            continue
        ingress = set(info.get("ingress-zones", "").split())
        egress = set(info.get("egress-zones", "").split())
        if not (zone in ingress or _ANY in ingress):
            continue
        if _ANY in egress or (uplink_zones and set(uplink_zones) <= egress):
            return name
    return ""


def setup_commands(bridge: str, ports: Sequence[int], *, sudo: bool = True) -> List[str]:
    """The commands that give ``bridge`` both paths, one per line, safe to re-run.

    A zone of its own with the gateway port(s) and ``forward`` (guest to guest),
    and a policy from that zone to ANY other zone with target ACCEPT (guest to the
    Internet). The policy does not widen what a guest may reach: nodo's own
    per-VM rules (``block_all`` and the allows) drop first, in ``inet nodo``, and a
    drop ends the packet whatever firewalld would do later. It only stops firewalld
    from rejecting what nodo already allowed. ``ANY`` rather than the uplink's zone,
    so that a host whose uplink moves (Wi-Fi, a VPN) keeps working; tested with
    firewalld 1.1 (Ubuntu 22.04) -- policies need firewalld 0.9 or later, and
    ``--add-forward`` needs 1.0.
    """
    s = "sudo " if sudo else ""
    fc = f"{s}firewall-cmd --permanent"
    zone, policy = GUEST_ZONE, EGRESS_POLICY
    commands = [
        f"{fc} --info-zone={zone} >/dev/null 2>&1 || {fc} --new-zone={zone}",
        f"{fc} --zone={zone} --change-interface={bridge}",
    ]
    for port in ports:
        commands.append(f"{fc} --zone={zone} --add-port={port}/tcp")
    commands += [
        f"{fc} --zone={zone} --add-forward",
        f"{fc} --info-policy={policy} >/dev/null 2>&1 || {fc} --new-policy={policy}",
        f"{fc} --policy={policy} --add-ingress-zone={zone}",
        f"{fc} --policy={policy} --add-egress-zone=ANY",
        f"{fc} --policy={policy} --set-target=ACCEPT",
        f"{s}firewall-cmd --reload",
    ]
    return commands


def zone_commands(bridge: str, ports: Sequence[int]) -> List[str]:
    """Kept for callers that predate the policy: the full set, see ``setup_commands``."""
    return setup_commands(bridge, ports)


def one_line(commands: Sequence[str]) -> str:
    """``commands`` as a single pasteable line, for the notice's ``.cmd`` companion.

    Each command is grouped, because some of them are ``a || b`` and an unbraced
    ``a || b && c`` would run ``c`` only on one branch's success.
    """
    return " && ".join(f"{{ {command}; }}" if "||" in command else command for command in commands)


def apply(bridge: str, ports: Sequence[int], *, run: Optional[Runner] = None) -> List[str]:
    """Write the zone and policy of ``setup_commands`` into firewalld. Returns what it did.

    For the installer, which runs as root with the operator watching. Idempotent:
    every step is a query first. Raises ``RuntimeError`` with firewall-cmd's own
    words when a step fails, and leaves firewalld's runtime untouched until the
    final ``--reload``.
    """
    run = run or _default_runner
    done: List[str] = []
    fc = ["firewall-cmd", "--permanent"]
    zone, policy = GUEST_ZONE, EGRESS_POLICY

    def must(args: List[str]) -> None:
        proc = run(args)
        if proc.returncode != 0:
            raise RuntimeError(f"{' '.join(args)}: {_text(proc) or proc.returncode}")

    def query(args: List[str]) -> bool:
        return run(args).returncode == 0

    if not query([*fc, f"--info-zone={zone}"]):
        must([*fc, f"--new-zone={zone}"])
        done.append(f"created zone {zone}")
    if not query([*fc, f"--zone={zone}", f"--query-interface={bridge}"]):
        must([*fc, f"--zone={zone}", f"--change-interface={bridge}"])
        done.append(f"put {bridge} in zone {zone}")
    for port in ports:
        if not query([*fc, f"--zone={zone}", f"--query-port={port}/tcp"]):
            must([*fc, f"--zone={zone}", f"--add-port={port}/tcp"])
            done.append(f"opened {port}/tcp to the guests")
    if not query([*fc, f"--zone={zone}", "--query-forward"]):
        must([*fc, f"--zone={zone}", "--add-forward"])
        done.append(f"enabled forwarding inside zone {zone}")
    if not query([*fc, f"--info-policy={policy}"]):
        must([*fc, f"--new-policy={policy}"])
        done.append(f"created policy {policy}")
    if not query([*fc, f"--policy={policy}", f"--query-ingress-zone={zone}"]):
        must([*fc, f"--policy={policy}", f"--add-ingress-zone={zone}"])
    if not query([*fc, f"--policy={policy}", "--query-egress-zone=ANY"]):
        must([*fc, f"--policy={policy}", "--add-egress-zone=ANY"])
    info = _info(run([*fc, f"--info-policy={policy}"]))
    if info.get("target", "").upper() != "ACCEPT":
        must([*fc, f"--policy={policy}", "--set-target=ACCEPT"])
        done.append(f"policy {policy}: {zone} -> ANY, target ACCEPT")
    if done:
        must(["firewall-cmd", "--reload"])
    return done


def notice_title(bridge: str, assessment: Optional[Assessment] = None) -> str:
    if assessment is not None and assessment.guest_to_guest is not False:
        return f"guests on {bridge} cannot reach the Internet (firewalld)"
    if assessment is not None and assessment.egress is False:
        return f"guests on {bridge} cannot reach each other or the Internet (firewalld)"
    return f"guests on {bridge} cannot reach each other (firewalld)"


def notice_body(
    bridge: str, commands: Sequence[str], assessment: Optional[Assessment] = None
) -> str:
    problems = assessment.problems(bridge) if assessment is not None else []
    lines = [
        f"firewalld is running, and its {FIREWALLD_CHAIN} rejects guest traffic that "
        "nodo has allowed (an accept in nodo's own table does not end firewalld's "
        "chain; the guest sees 'No route to host'):",
        "",
    ]
    if problems:
        lines.extend(f"  - {problem}" for problem in problems)
    else:
        lines.append(f"  - {bridge} is in no firewalld zone.")
    lines.extend(
        [
            "",
            "Services that launch a dependency see a dead child; services with network "
            "tag '*' see no Internet. Published ports keep working, which hides it.",
            "",
            "Give the bridge a zone of its own and a policy out of it (not 'trusted': "
            "that would expose every port this host listens on to every guest). "
            "nodo's per-VM rules still decide what each guest may reach:",
            "",
        ]
    )
    lines.extend(f"  {command}" for command in commands)
    lines.extend(
        [
            "",
            "Then run 'sudo nodo doctor' to confirm. It sends real guest-to-guest and "
            "guest-to-Internet packets and clears this notice when both get through.",
        ]
    )
    return "\n".join(lines)
