"""``nodo protocol``: the protocol this node speaks, and whether a peer speaks it too.

Every address a node announces declares, in ``Peer.Uri.transport`` and
``Peer.Uri.protocol_stack``, what it speaks layer by layer, and the announcement as a
whole declares the cryptography it is signed in (``Peer.signature_scheme``). Nodes
compare those declarations on their own (``manager.add_peer_instance`` skips an address
whose stack differs), but until now nobody could *read* one: an operator saw a peer's
address skipped with a hex dump in the log, and a developer could not see what their
change did to the declaration before peers started refusing it.

    nodo protocol [--json] [--no-prose]
        This node's own declaration -- signature scheme, transport, the stack of
        layers and every ledger it uses -- with the prose an announcement over gRPC
        leaves out by default (``communication.SHARE_PROSE_ON_GET_PEER_INFO``).

    nodo protocol <peer_id | ip:port> [--json]
        Asks the peer for its announcement (``GetPeerInfo``) and compares it with this
        node's, layer by layer, and each ledger its payment contracts and reputation
        proofs declare. Exits 0 when the peer speaks this node's protocol on at least
        one address, 1 otherwise; a ledger that differs is reported, and is what this
        node will not pay into, but does not by itself make the peer unreachable.

The comparison is the one the node itself makes -- ``tags`` and ``formal``, never
``prose`` (``node_identity.same_component``) -- so what this prints is what the node
decides, explained.
"""
import textwrap
from typing import Dict, List, Optional, Tuple

from src.commands import _catalogue as catalogue


def _component(component, *, prose: bool = True) -> Dict:
    entry = {
        "tags": list(component.tags),
        "formal": bytes(component.formal).decode("utf-8", errors="replace"),
    }
    if prose:
        entry["prose"] = component.prose
    return entry


def own_declaration(*, prose: bool = True) -> Dict:
    """What this node announces about how to talk to it, as one document.

    Built from the same functions the announcement is (``gateway.utils._build_peer``),
    so it cannot describe something other than what peers receive -- with the one
    difference that the prose is always available here, whatever the announcement does.
    """
    from src.identity.node_identity import node_signature_scheme
    from src.identity.transport_stack import node_transport, node_transport_stack
    from src.utils.ledger_descriptors import payment_ledgers, reputation_ledgers

    return {
        "signature_scheme": [
            _component(c, prose=prose) for c in node_signature_scheme().components
        ],
        "transport": _component(node_transport(), prose=prose),
        "protocol_stack": [
            _component(c, prose=prose) for c in node_transport_stack(prose=prose)
        ],
        # Every ledger this node declares, as each place carries it: a payment contract
        # declares the network and how to pay; a reputation proof, the network and how
        # a proof is laid out (ledger_descriptors).
        "payment_ledgers": [
            _component(ledger, prose=prose) for ledger in payment_ledgers()
        ],
        "reputation_ledgers": [
            _component(ledger, prose=prose) for ledger in reputation_ledgers()
        ],
    }


def _compare_ledgers(peer) -> List[Dict]:
    """Each ledger ``peer`` declares, against this node's declaration of the same ledger.

    Each against the declaration for its place: a payment contract against how this
    node declares that ledger for payments, a reputation proof against how it declares
    it for proofs. A ledger this node has no declaration for there is ``unknown``.
    """
    from src.identity.node_identity import same_declaration
    from src.identity.transport_stack import formal_difference
    from src.utils.ledger_descriptors import payment_ledger, reputation_ledger

    declared = [
        (f"payment_contracts[{i}]", rate.contract.ledger, payment_ledger)
        for i, rate in enumerate(peer.payment_contracts)
    ] + [
        (f"reputation_proofs[{i}]", proof.ledger, reputation_ledger)
        for i, proof in enumerate(peer.reputation_proofs)
    ]
    report: List[Dict] = []
    for where, ledger, local_ledger in declared:
        tags = list(ledger.tags)
        ours = local_ledger(tags[0]) if tags else None
        entry = {"where": where, "tags": tags}
        if ours is None:
            entry["status"] = "unknown"
        elif same_declaration(ledger, ours):
            entry["status"] = "match"
        else:
            entry["status"] = "differs"
            entry["formal_difference"] = formal_difference(
                bytes(ours.formal), bytes(ledger.formal)
            )
        report.append(entry)
    return report


def _looks_like_address(target: str) -> bool:
    host, _, port = target.rpartition(":")
    return bool(host) and port.isdigit()


def _fetch_announcement(target: str) -> Tuple[object, Optional[str]]:
    """``(peer, certificate_peer_id)`` for ``target``, a peer id or an ``ip:port``.

    An address goes through the same first-contact path ``nodo connect`` takes: the
    TLS certificate proves which identity holds it, and a client_id is minted there,
    since ``GetPeerInfo`` refuses a caller without one. A known peer id goes through
    ``manager.fetch_peer_info``, the same call a peer refresh makes.
    """
    from src.identity.grpc_transport import channel_and_peer_id, peer_channel
    from src.manager.manager import fetch_peer_info, mint_client_id_on_channel
    from src.utils.bee_client import BeeClient

    if _looks_like_address(target):
        channel, certificate_peer_id = channel_and_peer_id(target)
        try:
            client_id = mint_client_id_on_channel(channel) or ""
            return BeeClient.get_peer_info(channel, client_id=client_id), certificate_peer_id
        finally:
            channel.close()

    channel = peer_channel(peer_id=target)
    try:
        return fetch_peer_info(channel, target), target
    finally:
        channel.close()


def compare_with(peer, certificate_peer_id: Optional[str]) -> Dict:
    """How ``peer``'s announcement compares with this node's, address by address."""
    from src.identity.node_identity import (
        node_signature_scheme,
        same_component,
        speaks_our_signature_scheme,
    )
    from src.identity.transport_stack import (
        compare_component_sets,
        compare_layer_stacks,
        node_transport,
        node_transport_stack,
        speaks_our_transport_stack,
    )
    from src.manager.manager import verified_peer_public_key

    our_transport = node_transport()
    our_stack = node_transport_stack(prose=False)

    uris: List[Dict] = []
    for uri in peer.uri:
        uris.append({
            "uri": f"{uri.ip}:{uri.port}",
            "transport": {
                "tags": list(uri.transport.tags),
                "match": same_component(uri.transport, our_transport),
            },
            "speaks": speaks_our_transport_stack(uri.protocol_stack),
            "layers": compare_layer_stacks(our_stack, uri.protocol_stack),
        })

    scheme = peer.signature_scheme
    return {
        "peer_id": peer.public_key or None,
        "certificate_peer_id": certificate_peer_id,
        "signature_verifies": verified_peer_public_key(peer) is not None,
        "signature_scheme": {
            "declares_scheme": bool(len(scheme.components)),
            "speaks": speaks_our_signature_scheme(peer),
            "layers": compare_component_sets(
                node_signature_scheme().components, scheme.components
            ),
        },
        "uris": uris,
        "ledgers": _compare_ledgers(peer),
        "compatible": speaks_our_signature_scheme(peer) and any(
            u["speaks"] and u["transport"]["match"] for u in uris
        ),
    }


def _print_component(title: str, component: Dict) -> None:
    print(f"  {title}: {' '.join(component['tags']) or '(no tags)'}")
    for line in component["formal"].splitlines():
        print(f"      {line}")
    if component.get("prose"):
        for paragraph in component["prose"].split("\n"):
            for line in textwrap.wrap(paragraph, width=74) or [""]:
                print(f"    | {line}")
    print()


def _print_declaration(declaration: Dict) -> None:
    print("Signature scheme (Peer.signature_scheme)")
    for component in declaration["signature_scheme"]:
        _print_component("component", component)
    print("Every address (Peer.Uri)")
    _print_component("transport", declaration["transport"])
    for number, component in enumerate(declaration["protocol_stack"], start=1):
        _print_component(f"layer {number}", component)
    print("Ledgers in payment_contracts (Contract.Ledger)")
    for component in declaration["payment_ledgers"]:
        _print_component("ledger", component)
    print("Ledgers in reputation_proofs (Contract.Ledger)")
    for component in declaration["reputation_ledgers"]:
        _print_component("ledger", component)


def _print_layers(layers: List[Dict], indent: str = "    ") -> None:
    for layer in layers:
        print(f"{indent}{layer['status']:<8} {' '.join(layer['tags'])}")
        for key, values in layer.get("formal_difference", {}).items():
            print(f"{indent}           {key}: ours={values['ours']!r} theirs={values['theirs']!r}")


def _print_comparison(report: Dict) -> None:
    print(f"Peer:      {report['peer_id'] or '(announced no identity)'}")
    if report["certificate_peer_id"] and report["certificate_peer_id"] != report["peer_id"]:
        print(f"Warning:   the address is held by {report['certificate_peer_id']}, "
              "which is not the identity it announced.")
    print(f"Signature: {'verifies' if report['signature_verifies'] else 'DOES NOT VERIFY'}")

    scheme = report["signature_scheme"]
    print(f"Scheme:    {'same as ours' if scheme['speaks'] else 'DIFFERENT'}"
          + ("" if scheme["declares_scheme"] else " (none declared)"))
    _print_layers(scheme["layers"])

    for uri in report["uris"]:
        verdict = "speaks our protocol" if uri["speaks"] and uri["transport"]["match"] \
            else "does NOT speak our protocol"
        print(f"\n{uri['uri']}  {verdict}")
        print(f"    {'match' if uri['transport']['match'] else 'differs':<8} "
              f"transport {' '.join(uri['transport']['tags']) or '(none)'}")
        _print_layers(uri["layers"])

    if report["ledgers"]:
        print("\nLedgers")
        for ledger in report["ledgers"]:
            print(f"    {ledger['status']:<8} {' '.join(ledger['tags']) or '(no tags)'}"
                  f"  ({ledger['where']})")
            for key, values in ledger.get("formal_difference", {}).items():
                print(f"               {key}: ours={values['ours']!r} theirs={values['theirs']!r}")

    print(f"\nCompatible: {'yes' if report['compatible'] else 'no'}")


def protocol_command(argv=None) -> bool:
    """``nodo protocol [<peer_id | ip:port>] [--json] [--no-prose]``."""
    argv = list(argv or [])
    as_json = "--json" in argv
    args = catalogue.positionals(argv, valued_flags=())

    if not args:
        declaration = own_declaration(prose="--no-prose" not in argv)
        if as_json:
            catalogue.emit_json({"protocol": declaration})
        else:
            _print_declaration(declaration)
        return True

    target = args[0]
    try:
        peer, certificate_peer_id = _fetch_announcement(target)
    except Exception as e:
        return catalogue.emit_error(as_json, f"Could not read the announcement of {target}: {e}")
    if peer is None:
        return catalogue.emit_error(as_json, f"{target} did not answer GetPeerInfo.")

    report = compare_with(peer, certificate_peer_id)
    if as_json:
        catalogue.emit_json({"comparison": report})
    else:
        _print_comparison(report)
    return report["compatible"]
