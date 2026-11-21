"""``nodo inspect``: print what a packaged service declares, and what running it assumes.

    nodo inspect <service>              the assumptions, then every section
    nodo inspect <service> <section>    one section: env, machine, network, api, deps, fs, meta
    nodo inspect <service> --json       the same report as one JSON object (the TUI reads this)

The report is built by ``src/utils/service_report.py``; this module only reads the node.

Named ``inspect_service`` rather than ``inspect`` on purpose. A module called
``inspect`` here shadows the standard library module of that name for any script
run from inside this directory -- and one is: ``completion.py``, which the shell
invokes on every Tab keypress. Anything that then imported ``inspect``, directly
or through a stdlib module that does (``dataclasses`` does), got this file and a
``bee_rpc`` ImportError instead. The CLI command is still ``nodo inspect``.
"""
import os
from typing import Any, Dict, List, Optional

from bee_rpc.utils import getsize
from src.commands._catalogue import emit_json
from src.commands.__by_tag import get_id
from src.utils.config import ConfigManager
from src.utils.contract_xattrs import get_address, get_script, get_token_id
from src.utils.partial_service import is_partial
from src.utils.registry_errors import ServiceNotInRegistry, ServiceRegistryError
from src.utils.service_report import SECTION_KEYS, build_report, format_size
from src.utils.utils import load_service_from_disk, read_metadata_from_disk

env_manager = ConfigManager()

REGISTRY = env_manager.get("REGISTRY")

USAGE = f"Usage: nodo inspect <service id | tag> [{' | '.join(SECTION_KEYS)}] [--json]"


def _proofs(metadata) -> List[Dict[str, str]]:
    return [
        {
            "ledger": ", ".join(proof.ledger.tags),
            "script": get_script(proof).hex(),
            "address": get_address(proof),
            "token_id": get_token_id(proof),
        }
        for proof in (metadata.reputation_proofs if metadata is not None else ())
    ]


def _shares(service_obj) -> Optional[List[Dict[str, Any]]]:
    """The directories the service shares, or None when its filesystem cannot be read."""
    from src.utils.shared_filesystems import declarations_for_service
    try:
        return [
            {"path": d.path, "shared": d.shared, "guest": d.guest, "access": d.access,
             "tag": d.tag, "env": d.env, "network": d.network}
            for d in declarations_for_service(service_obj)
        ]
    except Exception:  # A spec this node does not accept is shown by the other sections.
        return None


def _node_context(service_obj) -> Dict[str, Any]:
    """What this node says about the service. Each answer is left out if it cannot be had."""
    node: Dict[str, Any] = {}
    try:
        from src.utils.architectures import SUPPORTED_ARCHITECTURES
        supported: List[str] = []
        for entry in SUPPORTED_ARCHITECTURES:
            supported += list(entry) if isinstance(entry, (list, tuple, set)) else [entry]
        node["supported_architectures"] = supported
    except Exception:
        pass
    try:
        from src.utils.network_policy import NetworkPolicy
        rejection = NetworkPolicy.from_config().check(networks=service_obj.network)
        node["network_verdict"] = (
            "allowed by this node." if rejection is None else f"refused by this node. {rejection}"
        )
    except Exception:
        pass
    return node


def read_report(service: str) -> Dict[str, Any]:
    """The report of a service of this node's registry. Raises ``ServiceRegistryError``."""
    service_id = get_id(service)
    metadata = read_metadata_from_disk(service_hash=service_id) if service_id else None
    if metadata is None:
        raise ServiceNotInRegistry(f"Service '{service}' is not on the local registry.")
    partial = is_partial(service_id)
    service_obj = load_service_from_disk(service_hash=service_id)
    return build_report(
        service_id, service_obj, metadata,
        size=None if partial else getsize(os.path.join(REGISTRY, service_id)),
        partial=partial,
        shares=_shares(service_obj),
        proofs=_proofs(metadata),
        node=_node_context(service_obj),
    )


def _print_section(section: Dict[str, Any], single: bool) -> None:
    title = section["title"]
    if single:
        print(f"{title}\n{'-' * len(title)}")
    else:
        print(f"\n{f' {title} '.center(60, '=')}")
    print("\n".join(section["lines"]))


def inspect(service: str, section: Optional[str] = None, as_json: bool = False) -> None:
    """Print the report of ``service``. Raises ``ServiceRegistryError`` if it cannot be read."""
    if section is not None and section not in SECTION_KEYS:
        raise ValueError(f"Unknown section '{section}'. Sections: {', '.join(SECTION_KEYS)}")
    report = read_report(service)
    if as_json:
        if section is not None:
            report["sections"] = [s for s in report["sections"] if s["key"] == section]
        emit_json(report)
        return
    for item in report["sections"]:
        if section is None or item["key"] == section:
            _print_section(item, single=section is not None)
    print()


def inspect_command(argv: List[str]) -> bool:
    """``nodo inspect`` with the arguments after the command name. Returns whether it worked."""
    as_json = "--json" in argv
    args = [a for a in argv if a != "--json"]
    if not args or len(args) > 2:
        print(USAGE)
        return False
    try:
        inspect(service=args[0], section=args[1] if len(args) == 2 else None, as_json=as_json)
    except ServiceRegistryError as error:
        if as_json:
            emit_json({"error": str(error)})
        else:
            print(f"❌ {error}")
        return False
    except ValueError as error:
        print(f"{error}\n{USAGE}")
        return False
    return True
