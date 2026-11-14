"""Peer discovery from the reputation ledgers (#494): the reader of the Ergo boxes and the
run that registers what it finds."""
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from tests.config_bootstrap import load_example_config
load_example_config()

from src.manager import ledger_discovery as discovery
from src.manager.ledger_discovery import Introduction
from src.reputation_system.contracts.ergo import peers as ergo_peers
from src.reputation_system.contracts.ergo.utils import ProofStanding
from src.reputation_system.envs import REPUTATION_PROOF_ERGO_TREE
from src.utils.config import ConfigManager
from src.utils.keyvalue import message_to_dict
from protos import celaut_pb2 as pb
from src.identity import node_identity as identity
from src.identity.transport_stack import declare_transport

NFT = str(ConfigManager().get("ledgers.ergo.reputation.CELAUT_NODE_TYPE_NFT_ID"))
ERG = 10 ** 9
FAR = time.monotonic() + 3600


def signed_peer(*addresses, ts=100, identity_index=0):
    """A ``Peer`` as ``transaction._self_network_data`` publishes it: one transport
    declaration per address, no prose, small enough for the register of a box."""
    from mnemonic import Mnemonic
    mnemonic = Mnemonic("english").to_mnemonic(identity_index.to_bytes(16, "big"))
    pubkey, key = identity._cached_keypair(mnemonic)
    peer = pb.Peer(public_key=pubkey, ts=ts)
    for address in addresses or ("8.8.8.8",):
        uri = peer.uri.add(ip=address, port=40001, expiry_unix_timestamp=int(time.time()) + 3600)
        declare_transport(uri, prose=False, tag="tcp")
    identity.declare_signature_scheme(peer, prose=False)
    peer.signature = key.sign(identity.canonical_peer_payload(
        pubkey, ts, identity.canonical_peer_content_digest(peer),
    ).encode()).hex()
    return peer


def _serialized(payload: bytes) -> str:
    """A Coll[Byte] register as the explorer serialises it: 0e, VLQ length, bytes."""
    length, out = len(payload), bytearray()
    while True:
        byte = length & 0x7F
        length >>= 7
        out.append(byte | (0x80 if length else 0))
        if not length:
            break
    return "0e" + out.hex() + payload.hex()


def box(peer, proof="aa" * 32, r5=None, r9=None, nft=NFT):
    data = r9 if r9 is not None else json.dumps(message_to_dict(peer))
    registers = {
        "R4": _serialized(bytes.fromhex(nft)),
        "R5": _serialized(bytes.fromhex(r5 or peer.public_key)),
        "R9": _serialized(data.encode()),
    }
    return {
        "boxId": "b" * 64,
        "ergoTree": REPUTATION_PROOF_ERGO_TREE,
        "assets": [{"tokenId": proof, "amount": 1}],
        "additionalRegisters": {k: {"serializedValue": v} for k, v in registers.items()},
    }


def read(boxes, burned, limit=10, exclude=frozenset(), deadline=FAR):
    """``well_reputed_peers`` over ``boxes``; ``burned`` maps a proof id to nanoERG."""
    standing = MagicMock(
        side_effect=lambda api, token, deadline=None: ProofStanding(0, 0, burned[token])
    )
    with patch.object(ergo_peers, "_opinion_boxes", return_value=iter(boxes)), \
            patch.object(ergo_peers, "proof_standing", standing), \
            patch.object(ergo_peers, "explorer_api_url", return_value="http://explorer"):
        return ergo_peers.well_reputed_peers(limit, deadline, exclude), standing


def proof(n):
    return f"{n:02x}" * 32


def test_the_minimum_of_locked_erg_picks_the_peers():
    low, high = signed_peer(identity_index=1), signed_peer(identity_index=2)
    chosen, _ = read(
        [box(low, proof(1)), box(high, proof(2))],
        {proof(1): ERG // 20, proof(2): ERG // 2},
    )
    assert [p.public_key for p in chosen] == [high.public_key]


def test_the_maximum_bounds_how_many_peers_are_read():
    claims = [signed_peer(identity_index=i) for i in range(1, 6)]
    boxes = [box(c, proof(i)) for i, c in enumerate(claims, 1)]
    chosen, standing = read(boxes, {proof(i): ERG for i in range(1, 6)}, limit=2)
    assert len(chosen) == 2
    # It stops asking once it has enough.
    assert standing.call_count == 2


def test_a_box_that_does_not_declare_a_node_is_not_read():
    good, other = signed_peer(identity_index=1), signed_peer(identity_index=2)
    boxes = [
        box(good, proof(1)),
        box(other, proof(2), r9="No IP available."),                       # no address
        box(other, proof(3), r5=good.public_key),                          # R5 is another key
        box(other, proof(4), nft="cd" * 32),                               # another node type
        box(other, proof(5), r9=json.dumps({"not": "a peer"})),            # not a Peer
    ]
    chosen, _ = read(boxes, {proof(n): ERG for n in range(1, 6)})
    assert [p.public_key for p in chosen] == [good.public_key]


def test_a_known_peer_costs_no_request():
    known = signed_peer(identity_index=1)
    chosen, standing = read([box(known, proof(1))], {proof(1): ERG},
                            exclude=frozenset({known.public_key}))
    assert chosen == [] and standing.call_count == 0


def test_a_failed_proof_read_skips_that_peer_only():
    broken, fine = signed_peer(identity_index=1), signed_peer(identity_index=2)

    def standing(api, token, deadline=None):
        if token == proof(1):
            raise RuntimeError("explorer down")
        return ProofStanding(0, 0, ERG)

    with patch.object(ergo_peers, "_opinion_boxes",
                      return_value=iter([box(broken, proof(1)), box(fine, proof(2))])), \
            patch.object(ergo_peers, "proof_standing", side_effect=standing), \
            patch.object(ergo_peers, "explorer_api_url", return_value="http://explorer"):
        chosen = ergo_peers.well_reputed_peers(10, FAR)
    assert [p.public_key for p in chosen] == [fine.public_key]


def test_a_deadline_that_has_passed_returns_what_it_has():
    chosen, standing = read([box(signed_peer(identity_index=1), proof(1))], {proof(1): ERG},
                            deadline=time.monotonic() - 1)
    assert chosen == [] and standing.call_count == 0


@pytest.mark.parametrize("value, expected", [
    ("0.1", ERG // 10), ("2", 2 * ERG), ("0", 0), ("nope", ERG // 10), ("-1", ERG // 10),
])
def test_the_minimum_reads_decimal_erg_and_falls_back_on_a_bad_value(value, expected):
    with patch.object(ConfigManager, "get", return_value=value):
        assert ergo_peers.min_locked_nanoerg() == expected


# ----- the run ---------------------------------------------------------------------------

@pytest.mark.parametrize("address", [
    "10.0.0.1", "192.168.1.2", "127.0.0.1", "169.254.1.1", "100.64.0.1", "::1", "fc00::1",
    "fe80::1", "localhost", "node.local", "printer.lan", "224.0.0.1", "",
])
def test_an_address_that_is_not_global_is_not_acceptable(address):
    assert not discovery.acceptable_address(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "2001:4860:4860::8888", "node.example.com"])
def test_a_global_address_is_acceptable(address):
    assert discovery.acceptable_address(address)


def test_a_claim_with_one_private_address_is_refused_whole():
    assert discovery.acceptable_claim(signed_peer("8.8.8.8", ts=1), own_key="")
    assert not discovery.acceptable_claim(signed_peer("8.8.8.8", "10.0.0.1"), own_key="")


def run(claims, introduce=lambda peer_id: Introduction.ACCEPTED, limit=10, known=(),
        inserted_elsewhere=(), chosen=(), timeout=30):
    """``discover_peers_from_ledgers`` with a fake ledger, and the real signature check."""
    from src.manager import manager

    registered = []
    inserted_elsewhere = set(inserted_elsewhere)

    def add(peer, learned_via_gossip=False):
        assert learned_via_gossip, "a ledger peer is not one the operator chose"
        peer_id = manager.verified_peer_public_key(peer)
        if peer_id:
            registered.append(peer_id)
        inserted = bool(peer_id) and peer_id not in known and peer_id not in inserted_elsewhere
        return peer_id, inserted

    reader = MagicMock(return_value=claims)
    sql = MagicMock()
    sql.return_value.get_peers_id.return_value = list(known)
    sql.return_value.remove_peer.return_value = True
    sql.return_value.peer_learned_via_gossip.side_effect = lambda peer_id: peer_id not in chosen
    with patch("src.database.sql_connection.SQLConnection", sql), \
            patch("src.reputation_system.interface._peer_readers", return_value={"ergo": reader}), \
            patch("src.manager.manager.register_peer_instance", side_effect=add), \
            patch("src.identity.node_identity.get_node_public_key_hex", return_value="ab" * 32), \
            patch.object(discovery, "_introduce_self",
                         side_effect=lambda peer_id, timeout=None: introduce(peer_id)):
        result = discovery.discover_peers_from_ledgers(limit=limit, timeout=timeout)
    run.removed = [c.args[0] for c in sql.return_value.remove_peer.call_args_list]
    return result, registered, reader


def test_the_run_registers_introduces_and_survives_a_failed_introduction():
    claims = [signed_peer(identity_index=i) for i in range(1, 4)]
    first = claims[0].public_key
    result, registered, _ = run(
        claims,
        introduce=lambda peer_id: Introduction.UNREACHABLE if peer_id == first
        else Introduction.ACCEPTED,
    )
    # All three were tried: the failure of the first did not stop the others.
    assert registered == [c.public_key for c in claims]
    assert (result.found, result.introduced, result.removed) == (3, 2, 1)
    assert (result.added, result.refused) == (2, 0)


def test_a_peer_that_does_not_answer_is_removed():
    peer = signed_peer(identity_index=1)
    result, _, _ = run([peer], introduce=lambda peer_id: Introduction.UNREACHABLE)
    assert run.removed == [peer.public_key]
    assert (result.added, result.removed) == (0, 1)


def test_a_peer_that_refuses_the_introduction_is_alive_and_stays():
    peer = signed_peer(identity_index=1)
    result, _, _ = run([peer], introduce=lambda peer_id: Introduction.REFUSED)
    assert run.removed == []
    assert (result.added, result.refused_introduction, result.removed) == (1, 1, 0)


def test_an_error_on_this_side_says_nothing_about_the_peer_and_it_stays():
    peer = signed_peer(identity_index=1)
    result, _, _ = run([peer], introduce=lambda peer_id: Introduction.ERROR)
    assert run.removed == []
    assert (result.added, result.introduction_errors, result.removed) == (1, 1, 0)


def test_a_peer_that_was_already_known_is_never_removed():
    peer = signed_peer(identity_index=1)
    result, _, _ = run([peer], introduce=lambda peer_id: Introduction.UNREACHABLE,
                       known=[peer.public_key])
    assert run.removed == []
    assert (result.added, result.removed, result.introduction_errors) == (1, 0, 1)


def test_nothing_is_removed_when_the_node_does_not_introduce_itself():
    peer = signed_peer(identity_index=1)
    real_get = ConfigManager.get

    def get(self, key, default=None):
        if key == "communication.SELF_ANNOUNCE_TO_CONNECTING_PEERS":
            return False
        return real_get(self, key, default)

    with patch.object(ConfigManager, "get", get):
        result, registered, _ = run([peer], introduce=lambda peer_id: Introduction.UNREACHABLE)
    assert registered == [peer.public_key] and run.removed == []
    assert (result.added, result.removed) == (1, 0)


def test_a_claim_with_an_invalid_signature_is_refused_and_does_not_stop_the_others():
    forged = signed_peer(identity_index=1)
    forged.signature = "00" * 64
    fine = signed_peer(identity_index=2)
    result, registered, _ = run([forged, fine])
    assert registered == [fine.public_key]
    assert (result.added, result.refused) == (1, 1)


def test_a_private_address_is_never_registered_nor_introduced():
    private = signed_peer("8.8.8.8", "192.168.0.7", identity_index=1)
    introduced = []
    result, registered, _ = run(
        [private], introduce=lambda peer_id: introduced.append(peer_id) or Introduction.ACCEPTED
    )
    assert registered == [] and introduced == []
    assert (result.added, result.refused) == (0, 1)


def test_the_maximum_of_the_run_is_respected():
    claims = [signed_peer(identity_index=i) for i in range(1, 6)]
    result, registered, reader = run(claims, limit=2)
    assert len(registered) == 2 and result.added == 2
    assert reader.call_args.args[0] == 2          # asked for no more than the limit


def test_the_known_peers_are_passed_to_the_reader_to_skip():
    _, _, reader = run([], known=["k1", "k2"])
    # Our own key too: our own box is on the chain and must not take a slot.
    assert reader.call_args.args[2] == frozenset({"k1", "k2", "ab" * 32})
    assert callable(reader.call_args.kwargs["accept"])


def test_a_ledger_that_cannot_be_read_is_reported_and_not_raised():
    from src.manager import manager

    reader = MagicMock(side_effect=RuntimeError("explorer unreachable"))
    sql = MagicMock()
    sql.return_value.get_peers_id.return_value = []
    with patch("src.database.sql_connection.SQLConnection", sql), \
            patch("src.reputation_system.interface._peer_readers", return_value={"ergo": reader}), \
            patch("src.identity.node_identity.get_node_public_key_hex", return_value=""):
        result = discovery.discover_peers_from_ledgers(limit=5, timeout=30)
    assert result.added == 0 and "unreachable" in result.errors["ergo"]


def test_the_startup_run_does_nothing_when_it_is_switched_off():
    real_get = ConfigManager.get

    def get(self, key, default=None):
        if key == "communication.DISCOVER_PEERS_ON_LEDGERS":
            return False
        return real_get(self, key, default)

    with patch.object(ConfigManager, "get", get), \
            patch.object(discovery, "discover_peers_from_ledgers") as run_discovery:
        discovery.startup_discovery()
    run_discovery.assert_not_called()


def test_the_startup_run_never_raises():
    with patch.object(discovery, "discover_peers_from_ledgers", side_effect=RuntimeError("x")):
        discovery.startup_discovery()


# ----- how the introduction is classified ----------------------------------------------

def introduce(client_id="cid", channel=None, answer=None, announcement=None, announce_error=None):
    """``_introduce_self`` with the network faked. ``answer`` may be an exception."""
    from src.utils.bee_client import BeeClient

    def call(*args, **kwargs):
        if isinstance(answer, Exception):
            raise answer
        return answer

    announce = MagicMock(side_effect=announce_error) if announce_error else MagicMock()
    peer_channel = MagicMock(side_effect=channel) if isinstance(channel, Exception) \
        else MagicMock(return_value=MagicMock())
    with patch("src.gateway.utils.generate_full_node_peer_info", announce), \
            patch("src.manager.manager.get_client_id_on_other_peer", return_value=client_id), \
            patch("src.manager.manager.associate_client_id_on_channel", return_value=True), \
            patch("src.identity.grpc_transport.peer_channel", peer_channel), \
            patch.object(BeeClient, "introduce_peer", side_effect=call):
        return discovery._introduce_self("peer")


def test_an_accepted_introduction():
    assert introduce(answer=pb.RecursionGuard(token="ok")) is Introduction.ACCEPTED


def test_a_refusal_means_the_peer_is_alive():
    assert introduce(answer=pb.RecursionGuard(token="REFUSED")) is Introduction.REFUSED


@pytest.mark.parametrize("kwargs", [
    {"client_id": None},                          # no client_id at the peer
    {"answer": None},                             # no answer
    {"answer": TimeoutError("deadline")},         # the RPC timed out
    {"channel": ConnectionError("no route")},     # no channel
])
def test_nobody_answering_is_unreachable(kwargs):
    assert introduce(**kwargs) is Introduction.UNREACHABLE


def test_a_failure_to_make_our_own_announcement_is_not_the_peers_fault():
    assert introduce(announce_error=RuntimeError("no identity")) is Introduction.ERROR


# ----- the review of #653 ----------------------------------------------------------------

from src.reputation_system.contracts.ergo import utils as ergo_utils  # noqa: E402

TOKEN = "11" * 32


def chain_box(value, amount, r5, tree=REPUTATION_PROOF_ERGO_TREE, token=TOKEN):
    """An unspent box that holds ``amount`` of ``token``, as ``byTokenId`` gives it."""
    return {
        "ergoTree": tree,
        "value": value,
        "assets": [{"tokenId": token, "amount": amount}],
        "additionalRegisters": {"R5": {"serializedValue": _serialized(bytes.fromhex(r5))}},
    }


def standing_of(boxes):
    with patch.object(ergo_utils, "iter_unspent_boxes_by_token",
                      side_effect=lambda api, token, deadline=None: iter(boxes)):
        return ergo_utils.proof_standing("http://explorer", TOKEN)


def test_b1_erg_in_a_wallet_box_is_not_locked():
    # The declaring box at the contract with 0.001 ERG, and 0.1 ERG in the owner's own
    # box with one unit of the token: only the first is locked.
    standing = standing_of([
        chain_box(ERG // 1000, 1, "22" * 32),
        chain_box(ERG // 10, 1, "22" * 32, tree="0008cd" + "33" * 33),
    ])
    assert standing.contract_nanoerg == ERG // 1000
    assert standing.burned_nanoerg == ERG // 1000
    assert standing.assigned_amount == 1


def test_b1_a_box_of_another_token_first_is_not_counted():
    standing = standing_of([
        chain_box(ERG, 1, "22" * 32),
        chain_box(ERG, 1, "22" * 32, token="44" * 32),
    ])
    assert standing.burned_nanoerg == ERG


def test_the_locked_erg_is_the_share_of_tokens_not_in_a_self_opinion():
    # 0.4 ERG in the reserve (a self-opinion: R5 is the token) with 3 tokens, 0.6 ERG in
    # an opinion with 1 token: 1 ERG at the contract, 1/4 of the tokens are not in a
    # self-opinion.
    standing = standing_of([
        chain_box(4 * ERG // 10, 3, TOKEN),
        chain_box(6 * ERG // 10, 1, "22" * 32),
    ])
    assert (standing.assigned_amount, standing.reserved_amount) == (1, 3)
    assert standing.contract_nanoerg == ERG
    assert standing.burned_nanoerg == ERG // 4


def test_a_proof_with_only_a_self_opinion_has_nothing_locked():
    assert standing_of([chain_box(10 * ERG, 5, TOKEN)]).burned_nanoerg == 0
    assert standing_of([]).burned_nanoerg == 0


def test_b2_a_deeply_nested_r9_is_one_bad_box():
    good = signed_peer(identity_index=1)
    poison = box(signed_peer(identity_index=2), proof(2),
                 r9='{"a":' + "[" * 2000 + "]" * 2000 + "}")
    chosen, _ = read([poison, box(good, proof(1))], {proof(1): ERG, proof(2): ERG})
    assert [p.public_key for p in chosen] == [good.public_key]


def test_b2_any_error_in_one_box_does_not_stop_the_read():
    good = signed_peer(identity_index=1)
    real = ergo_peers.peer_of_box

    def peer_of_box(found, nft):
        if found.get("boxId") == "poison":
            raise RecursionError("maximum recursion depth exceeded")
        return real(found, nft)

    poison = dict(box(signed_peer(identity_index=2), proof(2)), boxId="poison")
    with patch.object(ergo_peers, "peer_of_box", side_effect=peer_of_box):
        chosen, _ = read([poison, box(good, proof(1))], {proof(1): ERG, proof(2): ERG})
    assert [p.public_key for p in chosen] == [good.public_key]


def test_b3_a_peer_that_another_path_inserted_during_the_run_is_not_removed():
    peer = signed_peer(identity_index=1)
    result, _, _ = run([peer], introduce=lambda peer_id: Introduction.UNREACHABLE,
                       inserted_elsewhere=[peer.public_key])
    assert run.removed == []
    assert (result.added, result.removed) == (1, 0)


def test_b3_a_peer_that_the_operator_chose_during_the_introduction_is_not_removed():
    peer = signed_peer(identity_index=1)
    result, _, _ = run([peer], introduce=lambda peer_id: Introduction.UNREACHABLE,
                       chosen=[peer.public_key])
    assert run.removed == []


def test_b4_no_introduction_starts_after_the_deadline():
    peer = signed_peer(identity_index=1)
    introduced = []
    result, registered, _ = run(
        [peer], introduce=lambda peer_id: introduced.append(peer_id) or Introduction.ACCEPTED,
        timeout=1e-9,
    )
    assert registered == [peer.public_key] and introduced == []
    assert result.introduction_errors == 1 and run.removed == []


def test_b4_the_introduction_bounds_generate_client():
    from src.utils.bee_client import BeeClient

    with patch.object(BeeClient, "call_one", return_value=None) as call_one:
        BeeClient.generate_client(MagicMock(), client_id="c", timeout=7)
    assert call_one.call_args.kwargs["timeout"] == 7

    with patch("src.gateway.utils.generate_full_node_peer_info", MagicMock()), \
            patch("src.manager.manager.get_client_id_on_other_peer",
                  return_value=None) as get_client:
        discovery._introduce_self("peer", timeout=3)
    assert get_client.call_args.kwargs["timeout"] == 3


def test_b4_the_proof_read_gets_the_deadline():
    chosen, standing = read([box(signed_peer(identity_index=1), proof(1))], {proof(1): ERG})
    assert standing.call_args.kwargs["deadline"] == FAR


def test_b5_a_copy_under_an_empty_proof_does_not_hide_the_node():
    honest = signed_peer(identity_index=1, ts=100)
    chosen, _ = read(
        [box(honest, proof(9)), box(honest, proof(1))],          # the copy comes first
        {proof(9): 0, proof(1): ERG},
    )
    assert [p.public_key for p in chosen] == [honest.public_key]


def test_one_proof_gives_one_peer():
    claims = [signed_peer(identity_index=i, ts=i) for i in range(1, 11)]
    chosen, _ = read([box(c, proof(1)) for c in claims], {proof(1): ERG})
    # Its newest declaration only.
    assert [p.public_key for p in chosen] == [claims[-1].public_key]


def test_a_refused_claim_costs_no_request_and_no_slot():
    refused, fine = signed_peer(identity_index=1), signed_peer(identity_index=2)
    standing = MagicMock(side_effect=lambda api, token, deadline=None: ProofStanding(0, 0, ERG))
    with patch.object(ergo_peers, "_opinion_boxes",
                      return_value=iter([box(refused, proof(1)), box(fine, proof(2))])), \
            patch.object(ergo_peers, "proof_standing", standing), \
            patch.object(ergo_peers, "explorer_api_url", return_value="http://explorer"):
        chosen = ergo_peers.well_reputed_peers(
            1, FAR, accept=lambda peer: peer.public_key != refused.public_key
        )
    assert [p.public_key for p in chosen] == [fine.public_key]
    assert [c.args[1] for c in standing.call_args_list] == [proof(2)]


def test_a_box_without_its_ergo_tree_is_not_read():
    peer = signed_peer(identity_index=1)
    found = box(peer, proof(1))
    del found["ergoTree"]
    assert ergo_peers.peer_of_box(found, NFT) is None
    found["ergoTree"] = "0008cd" + "33" * 33
    assert ergo_peers.peer_of_box(found, NFT) is None


@pytest.mark.parametrize("address", [
    "127.1", "0x7f.1", "0177.0.0.1", "10.1", "192.168.1", "64:ff9b::7f00:1", "::7f00:1",
    "::ffff:127.0.0.1", "2002:7f00:1::", "0.0.0.0", "::",
])
def test_an_address_the_resolver_reads_as_private_is_not_acceptable(address):
    assert not discovery.acceptable_address(address)


# ----- `nodo reputation lock` ------------------------------------------------------------

from src.reputation_system.contracts.ergo import transaction as proof_tx  # noqa: E402

MIN = proof_tx.SAFE_MIN_BOX_VALUE


def test_the_node_box_keeps_the_locked_erg():
    assert proof_tx.proof_box_values(3, 0, 0, ERG // 10) == [ERG // 10, MIN, MIN]
    assert proof_tx.proof_box_values(3, 2, 0, 0) == [MIN, MIN, MIN]


def test_the_erg_of_the_proof_never_goes_down():
    # The proof held 1 ERG; a lower LOCKED_ERG takes nothing back (the contract requires
    # totalNativeOut >= totalNativeIn across the proof's boxes).
    values = proof_tx.proof_box_values(3, 1, ERG, ERG // 10)
    assert sum(values) == ERG and values[1] == ERG - 2 * MIN
    # More boxes than before: the proof keeps at least what it had.
    assert sum(proof_tx.proof_box_values(5, 0, 2 * MIN, 0)) >= 2 * MIN


def test_the_wallet_pays_only_what_the_proof_does_not_bring():
    values = proof_tx.proof_box_values(2, 0, ERG, ERG // 10)
    assert proof_tx.wallet_need(values, ERG, fee=MIN) == MIN + sum(values) - ERG
    assert proof_tx.wallet_need(proof_tx.proof_box_values(2, 0, 0, ERG), 0, fee=MIN) \
        == MIN + ERG + MIN


@pytest.mark.parametrize("value, expected", [
    ("0.1", ERG // 10), ("0", 0), ("", 0), (None, 0), ("nope", 0), ("-1", 0),
])
def test_the_locked_target_reads_decimal_erg(value, expected):
    with patch.object(proof_tx.env_manager, "get", return_value=value):
        assert proof_tx.locked_target_nanoerg() == expected


def test_nodo_reputation_lock_sets_the_value_and_publishes():
    from src.commands import reputation as command

    with patch.object(ConfigManager, "set") as set_value, \
            patch("src.reputation_system.interface.submit_reputation",
                  return_value=True) as submit, \
            patch("src.commands.daemon.config_digest", return_value="d"), \
            patch("src.commands.daemon.restart_after_config_write", return_value=True):
        assert command.reputation(["lock", "0.1"]) is True
    set_value.assert_called_once_with(proof_tx.LOCKED_ERG_KEY, "0.1")
    submit.assert_called_once_with(force_submit=True)


@pytest.mark.parametrize("args", [["lock"], ["lock", "0"], ["lock", "-1"], ["lock", "x"]])
def test_nodo_reputation_lock_refuses_a_bad_amount(args):
    from src.commands import reputation as command

    with patch.object(ConfigManager, "set") as set_value, \
            patch("src.reputation_system.interface.submit_reputation") as submit:
        assert command.reputation(args) is False
    set_value.assert_not_called() and submit.assert_not_called()
