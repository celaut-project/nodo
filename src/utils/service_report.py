"""What a service declares, as sections a person can browse and assumptions they accept.

``nodo inspect`` and the TUI's service details both come from here, so they cannot
disagree. :func:`build_report` is pure: it reads a ``celaut.Service`` (and what the
caller knows about this node) and returns plain data -- no disk, no config, no network.

The report has two parts:

* ``assumptions``: what running the service asks you to accept, one line each, with a
  level. ``ok`` is a restriction that protects you, ``info`` is a fact, ``warn`` is a
  cost or a reach that you should mean, and ``risk`` is a thing that can leak data or
  that this node cannot do.
* ``sections``: the specification, in the order a reader asks about it: environment,
  machine, network, interface, dependencies, filesystem, metadata. Each has its
  ``lines`` already rendered, so a front end only has to show them.
"""
from typing import Any, Dict, Iterable, List, Optional, Sequence

from protos import celaut_pb2 as celaut
from src.utils import keyvalue, policies
from src.utils.hashing import SHA3_256_ID, SHAKE_256_ID
from src.utils.service_envs import env_specs

OK, INFO, WARN, RISK = "ok", "info", "warn", "risk"

#: Marker printed before an assumption, so the level reads without colour.
MARKERS = {OK: "[ ok ]", INFO: "[info]", WARN: "[warn]", RISK: "[RISK]"}

#: Section keys, in display order, with their titles.
SECTIONS = (
    ("summary", "Assumptions"),
    ("env", "Environment"),
    ("machine", "Machine"),
    ("network", "Network"),
    ("api", "Interface"),
    ("deps", "Dependencies"),
    ("fs", "Filesystem"),
    ("meta", "Metadata"),
)
SECTION_KEYS = tuple(key for key, _ in SECTIONS)

ALL_EGRESS_TAG = "*"
POW_TAG_PREFIX = "pow:"
DEFAULT_CPU_PERIOD = 100000


def format_size(count: int) -> str:
    """Bytes as B, KB, MB, GB or TB."""
    value = float(count)
    if count < 1024:
        return f"{count} B"
    for unit in ("KB", "MB", "GB", "TB"):
        value /= 1024
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
    return f"{count} B"  # pragma: no cover


def _cores(sysres: celaut.Sysresources) -> Optional[float]:
    if not sysres.HasField("cpu_quota"):
        return None
    period = sysres.cpu_period if sysres.HasField("cpu_period") and sysres.cpu_period else DEFAULT_CPU_PERIOD
    return sysres.cpu_quota / period


def _number(value: float) -> str:
    return f"{value:g}"


def describe_sysresources(sysres: celaut.Sysresources) -> List[str]:
    """One short phrase for each limit that ``sysres`` sets."""
    parts: List[str] = []
    cores = _cores(sysres)
    if cores is not None:
        parts.append(f"{_number(cores)} CPU core{'' if cores == 1 else 's'}")
    if sysres.HasField("mem_limit"):
        parts.append(f"{format_size(sysres.mem_limit)} memory")
    if sysres.HasField("disk_space"):
        parts.append(f"{format_size(sysres.disk_space)} disk")
    if sysres.HasField("blkio_weight"):
        parts.append(f"disk I/O weight {sysres.blkio_weight}")
    return parts


def _benchmarks(sysres: celaut.Sysresources) -> List[str]:
    return [f"{name} >= {minimum} per core" for name, minimum in keyvalue.items(sysres.benchmark)]


def _short(value: bytes, size: int = 12) -> str:
    return value.hex()[:size]


def _formal_lines(formal: bytes) -> List[str]:
    if not formal:
        return []
    try:
        return bytes(formal).decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return [f"(binary, {len(formal)} bytes)"]


def _protocol_text(protocol: celaut.Service.Api.Protocol) -> str:
    return "/".join(protocol.tags) or "(untagged)"


def classify_tag(tag: str) -> str:
    """How far the traffic of one network tag reaches: ``open``, ``pow``, ``host`` or ``local``."""
    tag = tag.strip()
    if tag == ALL_EGRESS_TAG:
        return "open"
    if tag.startswith(POW_TAG_PREFIX):
        return "pow"
    if tag.islower() and "." in tag:
        return "host"
    return "local"


class _Assumptions:
    def __init__(self) -> None:
        self.items: List[Dict[str, str]] = []

    def add(self, level: str, topic: str, text: str) -> None:
        self.items.append({"level": level, "topic": topic, "text": text})


def _network_assumptions(service: celaut.Service, out: _Assumptions, node: Dict[str, Any]) -> None:
    if not service.network:
        out.add(OK, "Network", "Isolated: it cannot connect to anything outside. It only reaches "
                               "its own dependencies, and you reach it through its ports.")
    for network in service.network:
        prose = f" ({network.prose})" if network.prose else ""
        for tag in network.tags:
            kind = classify_tag(tag)
            if kind == "open":
                out.add(RISK, "Network", "Unrestricted Internet: it can reach any server, so it can "
                                         "send out anything it can read.")
            elif kind == "pow":
                out.add(WARN, "Network", f"Connects to proof-of-work peers of '{tag}' directly{prose}.")
            elif kind == "host":
                out.add(WARN, "Network", f"Connects to the host '{tag}'{prose}.")
            else:
                out.add(INFO, "Network", f"Joins the network '{tag}'{prose}: it talks to the "
                                         f"instances that this node resolves for that tag.")
    if service.network:
        out.add(INFO, "Network", "These networks are the limit for the whole tree: the services it "
                                 "starts can only use a subset of them.")
    verdict = node.get("network_verdict")
    if verdict and service.network:
        out.add(WARN if verdict.startswith("refused") else OK, "This node", f"Network policy: {verdict}")


def _resource_assumptions(service: celaut.Service, out: _Assumptions) -> None:
    resources = service.container.resources if service.container.HasField("resources") else None
    if resources is None:
        out.add(WARN, "Cost", "Declares no resources: the specification does not bound what it "
                              "uses, so it cannot bound what it costs.")
    else:
        at_init = describe_sysresources(resources.at_init)
        at_most = describe_sysresources(resources.at_most)
        if at_init:
            out.add(INFO, "Cost", "Reserves at start: " + ", ".join(at_init) + ".")
        if at_most:
            out.add(INFO, "Cost", "May grow, but not past: " + ", ".join(at_most) + ".")
        elif at_init:
            out.add(WARN, "Cost", "Declares no upper limit: it may use more than it reserves.")
        for name in _benchmarks(resources.at_init):
            out.add(INFO, "Cost", f"Needs a node at least this fast: {name}.")
    out.add(INFO, "Cost", "The price is set by the node that runs it (a start cost plus a cost while "
                          "it runs). `nodo estimate <service>` quotes it before you pay.")
    if any(slot.mu_per_call for slot in service.api.slot):
        out.add(INFO, "Cost", "Some methods list an MU price per call. It is information; the node "
                              "does not charge it.")


def _workloads(clauses: Iterable[celaut.Service.Clause]):
    for clause in clauses:
        for group in clause.any_of:
            for workload in group.workloads:
                yield workload


def _dependency_assumptions(service: celaut.Service, out: _Assumptions) -> None:
    initial = list(_workloads(service.initial_environment_workload))
    possible = list(_workloads(service.possible_environment_workload))
    if initial:
        total = sum(w.count for w in initial)
        out.add(WARN, "Dependencies", f"Starts {total} child instance{'' if total == 1 else 's'} "
                                      f"at launch. They run, and cost, as part of it.")
    if possible:
        out.add(WARN, "Dependencies", "Can start more child services while it runs, up to the limits "
                                      "in the Dependencies section. You pay for those too.")
    if not initial and not possible:
        out.add(OK, "Dependencies", "Starts no other services.")


def _api_assumptions(service: celaut.Service, out: _Assumptions) -> None:
    if not service.api.slot:
        out.add(OK, "Interface", "Exposes no ports.")
    for slot in service.api.slot:
        try:
            access = policies.slot_access(slot)
        except policies.PolicyError as error:
            out.add(RISK, "Interface", f"Port {slot.port} is not valid and this node would refuse "
                                       f"the service: {error}")
            continue
        wording = {
            policies.ACCESS_ANY: "reachable through the node or by a direct connection",
            policies.ACCESS_DIRECT: "reachable only by a direct connection, which does not go "
                                    "through the node",
            policies.ACCESS_TUNNEL: "reachable only through a tunnel of the node",
        }.get(access.access, f"access={access.access}")
        out.add(INFO, "Interface", f"Port {slot.port}/{_protocol_text(slot.transport)}: {wording}.")
        if access.wake == policies.WAKE_ON_TUNNEL:
            out.add(WARN, "Interface", f"A tunnel connection to port {slot.port} wakes a hibernated "
                                       f"instance, so a caller can make it burn MU again.")


def _machine_assumptions(service: celaut.Service, out: _Assumptions, node: Dict[str, Any]) -> None:
    architecture = list(service.container.architecture.tags)
    supported = node.get("supported_architectures")
    text = f"Needs architecture {', '.join(architecture) or '(not declared)'}."
    if supported is not None and architecture:
        if any(tag in supported for tag in architecture):
            out.add(OK, "This node", text + " This node can run it.")
        else:
            out.add(RISK, "This node", text + f" This node runs {', '.join(supported) or 'nothing'}, "
                                              "so it cannot run it itself.")
    else:
        out.add(INFO, "Machine", text)
    specs = env_specs(service)
    if specs:
        out.add(INFO, "Environment", f"Asks for {len(specs)} variable{'' if len(specs) == 1 else 's'}: "
                                     f"{', '.join(spec.name for spec in specs)}. Nothing marks one as "
                                     f"required; you decide what to leave empty.")
    else:
        out.add(OK, "Environment", "Asks for no environment variables.")
    if service.container.devices:
        out.add(WARN, "Machine", f"Needs {len(service.container.devices)} device"
                                 f"{'' if len(service.container.devices) == 1 else 's'} of the node.")
    if service.container.HasField("notice_declaration"):
        out.add(INFO, "Machine", "Gets a notice before the node stops it, so it can finish its work.")


def _share_assumptions(shares: Optional[Sequence[Dict[str, Any]]], out: _Assumptions) -> None:
    for share in shares or ():
        if share["network"]:
            out.add(WARN, "Filesystem", f"Shares {share['path']} ({share['access']}) with the members "
                                        f"of the network '{share['network']}'.")
        elif share["guest"]:
            out.add(INFO, "Filesystem", f"Mounts {share['path']} ({share['access']}) from the parent "
                                        f"that starts it. It does not start without one.")
        elif share["shared"]:
            out.add(INFO, "Filesystem", f"Exports {share['path']} ({share['access']}) to its children.")


def _env_section(service: celaut.Service) -> List[str]:
    specs = env_specs(service)
    if not specs:
        return ["This service asks for no environment variables."]
    lines = ["Give each with: nodo execute <service> -e <NAME> <value>", ""]
    for spec in specs:
        lines.append(spec.name)
        if spec.tags:
            lines.append(f"    format: {', '.join(spec.tags)}")
        if spec.prose:
            lines.append(f"    {spec.prose}")
    return lines


def _machine_section(service: celaut.Service) -> List[str]:
    container = service.container
    lines = [f"Architecture: {', '.join(container.architecture.tags) or '(not declared)'}"]
    if container.architecture.prose:
        lines.append(f"    {container.architecture.prose}")
    lines += [f"    {line}" for line in _formal_lines(container.architecture.formal)]
    lines.append(f"Entry point: {' '.join(container.init.entry_path) or '(none)'}")
    if container.config_declaration.path:
        lines.append(f"Configuration file: {'/'.join(container.config_declaration.path)}")
        if container.config_declaration.format.tags:
            lines.append(f"    format: {', '.join(container.config_declaration.format.tags)}")
    if container.HasField("notice_declaration"):
        lines.append(f"Stop notice: {'/'.join(container.notice_declaration.path) or '(default)'}")
    lines.append("")
    if container.HasField("resources"):
        for label, sysres in (("At start (reserved)", container.resources.at_init),
                              ("At most (limit)", container.resources.at_most)):
            parts = describe_sysresources(sysres)
            lines.append(f"{label}: {', '.join(parts) or 'nothing declared'}")
            lines += [f"    needs {item}" for item in _benchmarks(sysres)]
    else:
        lines.append("Resources: none declared")
    if container.devices:
        lines.append("")
        lines.append("Devices:")
        for device in container.devices:
            attach = ", ".join(celaut.Device.Attach.Name(a) for a in device.attach) or "default"
            lines.append(f"  - {', '.join(device.architecture.tags) or 'any'} ({attach})")
    lines.append("")
    lines.append("Protocols the node must speak with it:")
    for protocol in container.node_protocol_stack or ():
        lines.append(f"  - {_protocol_text(protocol)}" + (f": {protocol.prose}" if protocol.prose else ""))
    if not container.node_protocol_stack:
        lines.append("  (none declared)")
    return lines


def _network_section(service: celaut.Service, node: Dict[str, Any]) -> List[str]:
    reach = {
        "open": "OPEN INTERNET", "pow": "proof-of-work peers, direct",
        "host": "a named host, direct", "local": "instances on this node's resolver",
    }
    if not service.network:
        return ["Isolated. It cannot connect to any external endpoint.",
                "You may connect to it through the ports of its interface.",
                "It may only start connections to its own dependencies, and they follow the same rules."]
    lines: List[str] = []
    for number, network in enumerate(service.network, start=1):
        lines.append(f"Network {number}")
        for tag in network.tags:
            lines.append(f"    {tag}  ->  {reach[classify_tag(tag)]}")
        if network.prose:
            lines.append(f"    {network.prose}")
        lines += [f"    {line}" for line in _formal_lines(network.formal)]
        for protocol in network.protocol_stack:
            lines.append(f"    speaks {_protocol_text(protocol)}")
        lines.append("")
    lines.append("Its dependencies can only use a subset of these networks.")
    if node.get("network_verdict"):
        lines.append(f"This node's network policy: {node['network_verdict']}")
    return lines


def _api_section(service: celaut.Service) -> List[str]:
    lines: List[str] = []
    for slot in service.api.slot:
        stack = " > ".join(_protocol_text(p) for p in slot.protocol_stack) or "(no protocol declared)"
        lines.append(f"Port {slot.port}  {_protocol_text(slot.transport)}  {stack}")
        try:
            access = policies.slot_access(slot)
            lines.append(f"    access={access.access}  tunnel_auth={access.tunnel_auth}  wake={access.wake}")
        except policies.PolicyError as error:
            lines.append(f"    NOT VALID: {error}")
        for protocol in slot.protocol_stack:
            if protocol.prose:
                lines.append(f"    {_protocol_text(protocol)}: {protocol.prose}")
        for method, amount in keyvalue.items(slot.mu_per_call):
            lines.append(f"    {method}: {amount.value} MU per call (information only)")
        lines.append("")
    if not service.api.slot:
        lines.append("No ports.")
    if service.api.payment_contracts:
        lines.append(f"Payment contracts accepted: {len(service.api.payment_contracts)}")
    return lines


def _workload_text(workload: celaut.Service.Workload) -> str:
    dependency = workload.dependency
    name = _short(dependency.hash[0].value) if dependency.hash else "(by specification)"
    text = f"{workload.count} x {name}"
    if dependency.service.container.architecture.tags:
        text += f" [{', '.join(dependency.service.container.architecture.tags)}]"
    if dependency.service.container.HasField("resources"):
        parts = describe_sysresources(dependency.service.container.resources.at_init)
        if parts:
            text += " reserving " + ", ".join(parts)
    return text


def _clauses_lines(title: str, clauses: Sequence[celaut.Service.Clause]) -> List[str]:
    if not clauses:
        return []
    lines = [title]
    for number, clause in enumerate(clauses, start=1):
        lines.append(f"  Requirement {number}: one of these groups")
        for group in clause.any_of:
            lines.append("    group: " + " AND ".join(_workload_text(w) for w in group.workloads))
    return lines + [""]


def _deps_section(service: celaut.Service) -> List[str]:
    lines = _clauses_lines("Started at launch:", service.initial_environment_workload)
    lines += _clauses_lines("May start while running:", service.possible_environment_workload)
    return lines or ["This service starts no other services."]


def _fs_section(shares: Optional[Sequence[Dict[str, Any]]], partial: bool) -> List[str]:
    if partial:
        return ["This node keeps the specification and not the filesystem.",
                "`nodo download` gets the complete service."]
    if shares is None:
        return ["The filesystem could not be read."]
    if not shares:
        return ["No directory is shared with other instances."]
    lines = []
    for share in shares:
        role = "network" if share["network"] else "guest" if share["guest"] else "shared"
        lines.append(f"{share['path']}  role={role}  access={share['access']}")
        for key in ("tag", "env", "network"):
            if share[key]:
                lines.append(f"    {key}={share[key]}")
    return lines


def _hash_name(hash_type: bytes) -> str:
    if hash_type == SHA3_256_ID:
        return "SHA3-256"
    if hash_type == SHAKE_256_ID:
        return "SHAKE-256"
    return hash_type.hex()[:6]


def _meta_section(service_id: str, service: celaut.Service, metadata: Optional[celaut.Metadata],
                  size: Optional[int], partial: bool, proofs: Sequence[Dict[str, str]]) -> List[str]:
    lines = [f"Id: {service_id}"]
    if metadata is not None:
        if metadata.hashtag.tag:
            lines.append(f"Tags: {', '.join(metadata.hashtag.tag)}")
        for hash_ in metadata.hashtag.hash:
            lines.append(f"Hash {_hash_name(hash_.type)}: {hash_.value.hex()}")
    lines.append("Stored: specification only (partial)" if partial
                 else f"Size: {format_size(size)}" if size is not None else "Size: unknown")
    if service.prose:
        lines += ["", "Description:", service.prose]
    for proof in proofs:
        lines += ["", f"Reputation ledger: {proof['ledger']}",
                  f"    script:   {proof['script']}",
                  f"    address:  {proof['address']}",
                  f"    token id: {proof['token_id']}"]
    return lines


def build_report(
        service_id: str,
        service: celaut.Service,
        metadata: Optional[celaut.Metadata] = None,
        *,
        size: Optional[int] = None,
        partial: bool = False,
        shares: Optional[Sequence[Dict[str, Any]]] = None,
        proofs: Sequence[Dict[str, str]] = (),
        node: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The report of ``service``.

    ``node`` carries what only the running node knows, and every key is optional:
    ``supported_architectures`` (a list of tags) and ``network_verdict`` (a sentence
    that starts with ``refused`` or ``allowed``). ``shares`` is a list of dicts with
    ``path``, ``shared``, ``guest``, ``network``, ``access``, ``tag`` and ``env``, or
    None when the filesystem cannot be read. ``proofs`` are reputation proofs as
    ``ledger``/``script``/``address``/``token_id`` strings.
    """
    node = node or {}
    out = _Assumptions()
    if partial:
        out.add(WARN, "This node", "Partial: this node keeps the specification and not the "
                                   "filesystem. It cannot run the service itself.")
    _network_assumptions(service, out, node)
    _resource_assumptions(service, out)
    _dependency_assumptions(service, out)
    _api_assumptions(service, out)
    _machine_assumptions(service, out, node)
    _share_assumptions(shares, out)
    if proofs:
        out.add(INFO, "Metadata", f"Carries {len(proofs)} reputation proof{'' if len(proofs) == 1 else 's'}.")

    summary_lines = [f"{MARKERS[item['level']]} {item['topic']}: {item['text']}" for item in out.items]
    lines_by_key = {
        "summary": summary_lines,
        "env": _env_section(service),
        "machine": _machine_section(service),
        "network": _network_section(service, node),
        "api": _api_section(service),
        "deps": _deps_section(service),
        "fs": _fs_section(shares, partial),
        "meta": _meta_section(service_id, service, metadata, size, partial, proofs),
    }
    return {
        "id": service_id,
        "partial": partial,
        "assumptions": out.items,
        "sections": [{"key": key, "title": title, "lines": lines_by_key[key]} for key, title in SECTIONS],
        "env": [spec.to_json() for spec in env_specs(service)],
        "architecture": list(service.container.architecture.tags),
        "network": [list(network.tags) for network in service.network],
    }
