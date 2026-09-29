"""Gossip privacy, signatures, resource bounds, and the real streaming wire shape."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import grpc
import pytest
import yaml

from tests.config_bootstrap import load_example_config
load_example_config()

from protos import celaut_pb2 as pb, celaut_pb2_grpc as rpc
from src.gateway.gateway import Gateway
from src.gateway.iterables import list_peers_iterable as server
from src.identity import node_identity as identity
from src.manager import gossip, manager
from src.utils.bee_client import BeeClient
from src.utils.peer_gossip import gossip_limit, iter_gossip_peers, relayable_peer
from tests.test_peer_identity_registration import _PeerFixture


def signed_peer(*addresses, ts=100, expiry=0, identity_index=0):
    # Fixed test-only mnemonic, with separate children for independent identities.
    from mnemonic import Mnemonic
    mnemonic = Mnemonic("english").to_mnemonic(identity_index.to_bytes(16, "big"))
    pubkey, key = identity._cached_keypair(mnemonic)
    peer = pb.Peer(public_key=pubkey, ts=ts)
    for address in addresses or ("8.8.8.8",):
        uri = peer.uri.add(ip=address, port=40001, expiry_unix_timestamp=expiry)
        uri.transport.tags.append("tcp")
    identity.declare_signature_scheme(peer)
    peer.signature = key.sign(identity.canonical_peer_payload(
        pubkey, ts, identity.canonical_peer_content_digest(peer),
    ).encode()).hex()
    return peer


def database(*peers):
    rows = {peer.public_key: peer.SerializeToString() for peer in peers}
    sc = MagicMock()
    sc.get_peers_id.side_effect = lambda: list(rows)
    sc.get_peer_advertisement.side_effect = rows.get
    return sc, rows


@pytest.mark.parametrize("address", [
    "10.0.0.1", "192.168.1.2", "172.16.0.1", "127.0.0.1", "0.0.0.0",
    "169.254.1.1", "100.64.0.1", "224.0.0.1", "255.255.255.255",
    "::1", "::", "fc00::1", "fe80::1", "ff02::1", "::ffff:192.168.1.2",
    "192.0.2.1", "2001:db8::1", "localhost", "node.local", "node.example.com", "",
])
def test_private_or_unproven_address_discards_whole_signed_claim(address):
    peer = signed_peer("8.8.8.8", address)
    before = peer.SerializeToString()
    assert not relayable_peer(peer, peer.public_key)
    assert peer.SerializeToString() == before
    assert manager.verified_peer_public_key(peer) == peer.public_key


def test_public_claim_is_relayed_byte_for_byte_and_still_verifies():
    peer = signed_peer("8.8.8.8", "2606:4700:4700::1111")
    sc, rows = database(peer)
    result, = iter_gossip_peers(sc, 100)
    assert result.SerializeToString() == rows[peer.public_key]
    assert manager.verified_peer_public_key(result) == peer.public_key


def test_expiry_is_filtered_without_editing_signed_fields():
    expired = signed_peer(expiry=100)
    assert not relayable_peer(expired, expired.public_key, now=100)
    live = signed_peer(expiry=101)
    assert relayable_peer(live, live.public_key, now=100)
    forever = signed_peer(expiry=0)
    assert relayable_peer(forever, forever.public_key, now=100)
    # Mixed expiry is still valid, but no URI can be removed from the signature.
    live.uri.add(ip="1.1.1.1", port=40001, expiry_unix_timestamp=99)
    assert relayable_peer(live, live.public_key, now=100)
    live.uri.add(ip="10.0.0.1", port=40001, expiry_unix_timestamp=99)
    assert not relayable_peer(live, live.public_key, now=100)


def test_bad_rows_and_mismatched_identity_do_not_spoil_response():
    peer = signed_peer()
    sc, rows = database(peer)
    sc.get_peers_id.side_effect = lambda: ["bad", "missing", "wrong", peer.public_key]
    rows.update(bad=b"\xff", wrong=peer.SerializeToString())
    result, = iter_gossip_peers(sc, 1)
    assert result == peer
    assert list(iter_gossip_peers(sc, 0)) == []
    assert list(iter_gossip_peers(sc, 100, exclude_peer_id=peer.public_key)) == []


def test_unsigned_and_addressless_claims_are_not_relayed():
    peer = signed_peer()
    peer.ClearField("signature")
    assert not relayable_peer(peer, peer.public_key)
    peer = signed_peer()
    peer.ClearField("uri")
    assert not relayable_peer(peer, peer.public_key)


@pytest.fixture
def tick(monkeypatch):
    settings = {}
    config = SimpleNamespace(get=lambda k, d=None: settings.get(k, d))
    peers = [signed_peer(identity_index=i) for i in range(4)]
    sc, rows = database(*peers)
    clock = [0.0]
    channel = MagicMock()
    monkeypatch.setattr(gossip, "_last_pull", None)
    monkeypatch.setattr(gossip, "_last_push", None)
    monkeypatch.setattr(gossip, "ConfigManager", lambda: config)
    monkeypatch.setattr(gossip, "SQLConnection", lambda: sc)
    monkeypatch.setattr(gossip.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(gossip.random, "choice", lambda ids: ids[0])
    monkeypatch.setattr(gossip, "peer_channel", MagicMock(return_value=channel))
    register = MagicMock(return_value="accepted")
    monkeypatch.setattr(manager, "add_peer_instance", register)
    monkeypatch.setattr(manager, "accept_peer_refresh", MagicMock(side_effect=AssertionError("not a direct refresh")))
    monkeypatch.setattr(manager, "get_client_id_on_other_peer", MagicMock(return_value="test-client"))
    pull = MagicMock(side_effect=lambda *a, **kw: iter(peers[1:]))
    push = MagicMock(return_value=pb.RecursionGuard(token="OK"))
    monkeypatch.setattr(BeeClient, "list_peers", pull)
    monkeypatch.setattr(BeeClient, "introduce_peer", push)
    return SimpleNamespace(**locals())


def test_flags_are_independent_live_and_interval_clocks_are_separate(tick):
    tick.settings["communication.DISCOVER_PEERS_VIA_GOSSIP"] = False
    gossip.gossip_pull_tick()
    assert not tick.pull.called
    gossip.gossip_push_tick()
    assert tick.push.call_count == 3
    tick.settings["communication.DISCOVER_PEERS_VIA_GOSSIP"] = True
    gossip.gossip_pull_tick()
    assert tick.pull.call_count == 1
    gossip.gossip_pull_tick()
    gossip.gossip_push_tick()
    assert tick.pull.call_count == 1 and tick.push.call_count == 3
    tick.clock[0] = 300
    tick.settings["communication.SHARE_KNOWN_PEERS"] = False
    gossip.gossip_pull_tick()
    gossip.gossip_push_tick()
    assert tick.pull.call_count == 2 and tick.push.call_count == 3
    # A shorter interval edited live takes effect without restarting.
    tick.settings["communication.GOSSIP_INTERVAL_SECONDS"] = 5
    tick.clock[0] = 305
    gossip.gossip_pull_tick()
    assert tick.pull.call_count == 3


def test_pull_registers_third_parties_and_caps_even_bad_messages(tick):
    tick.settings["communication.MAX_PEERS_PER_GOSSIP_RESPONSE"] = 2
    tick.pull.side_effect = lambda *a, **kw: iter([object(), tick.peers[1], tick.peers[2]])
    gossip.gossip_pull_tick()
    tick.register.assert_called_once_with(peer=tick.peers[1])
    manager.accept_peer_refresh.assert_not_called()
    tick.pull.assert_called_once_with(tick.channel, client_id="test-client", timeout=10)
    tick.channel.close.assert_called_once()


def test_pull_one_registration_failure_does_not_abort_stream(tick):
    tick.register.side_effect = [ValueError("bad"), None, "accepted"]
    gossip.gossip_pull_tick()
    assert tick.register.call_count == 3


def test_pull_stream_failure_closes_channel_and_backoffs(tick):
    def broken(*a, **kw):
        yield tick.peers[1]
        raise RuntimeError("broken stream")
    tick.pull.side_effect = broken
    gossip.gossip_pull_tick()
    gossip.gossip_pull_tick()
    assert tick.register.call_count == 1
    assert tick.pull.call_count == 1
    tick.channel.close.assert_called_once()


def test_pull_rejects_private_claims_before_registration(tick):
    tick.pull.side_effect = lambda *a, **kw: iter([signed_peer("10.0.0.1")])
    gossip.gossip_pull_tick()
    tick.register.assert_not_called()


def test_pull_closes_capped_generator(tick):
    closed = []
    def replies(*a, **kw):
        try:
            while True:
                yield tick.peers[1]
        finally:
            closed.append(True)
    tick.settings["communication.MAX_PEERS_PER_GOSSIP_RESPONSE"] = 1
    tick.pull.side_effect = replies
    gossip.gossip_pull_tick()
    assert tick.register.call_count == 1 and closed == [True]


def test_push_cap_excludes_target_and_keeps_signed_payload(tick):
    tick.settings["communication.MAX_PEERS_PER_GOSSIP_PUSH"] = 2
    gossip.gossip_push_tick()
    assert tick.push.call_count == 2
    for call, peer in zip(tick.push.call_args_list, tick.peers[1:3]):
        assert call.args[1].SerializeToString() == peer.SerializeToString()
        assert call.kwargs == dict(client_id="test-client", timeout=10)
    tick.channel.close.assert_called_once()


def test_push_uses_same_privacy_filter_and_survives_refusal_and_error(tick):
    private = signed_peer("8.8.8.8", "192.168.1.1", identity_index=1)
    tick.rows[private.public_key] = private.SerializeToString()
    tick.push.side_effect = [RuntimeError("offline"), pb.RecursionGuard(token="REFUSED")]
    gossip.gossip_push_tick()
    assert tick.push.call_count == 2
    assert all(call.args[1].public_key != private.public_key for call in tick.push.call_args_list)
    tick.channel.close.assert_called_once()


def test_push_total_deadline_stops_after_slow_call(tick):
    def slow(*args, **kwargs):
        tick.clock[0] += 11
        raise TimeoutError()
    tick.push.side_effect = slow
    gossip.gossip_push_tick()
    assert tick.push.call_count == 1


@pytest.mark.parametrize("value", [0, -1])
def test_zero_caps_do_not_even_dial(tick, value):
    tick.settings["communication.MAX_PEERS_PER_GOSSIP_RESPONSE"] = value
    tick.settings["communication.MAX_PEERS_PER_GOSSIP_PUSH"] = value
    gossip.gossip_pull_tick()
    gossip.gossip_push_tick()
    gossip.peer_channel.assert_not_called()


def test_empty_database_and_storage_failures_never_raise(tick):
    tick.sc.get_peers_id.side_effect = lambda: []
    gossip.gossip_pull_tick()
    gossip.gossip_push_tick()
    gossip.peer_channel.assert_not_called()
    tick.clock[0] = 300
    tick.sc.get_peers_id.side_effect = RuntimeError("storage unavailable")
    gossip.gossip_pull_tick()
    gossip.gossip_push_tick()
    gossip.peer_channel.assert_not_called()


@pytest.mark.parametrize("value", ["bad", None, float("inf")])
def test_bad_limits_use_default(value):
    assert gossip_limit(SimpleNamespace(get=lambda *args: value), "key", 100) == 100


def test_rpc_four_way_wiring_and_actual_gateway_handler():
    assert hasattr(rpc.GatewayServicer, "ListPeers")
    assert hasattr(Gateway, "ListPeers")
    channel = MagicMock()
    rpc.GatewayStub(channel)
    assert "/celaut.Gateway/ListPeers" in [c.args[0] for c in channel.stream_stream.call_args_list]
    grpc_server = MagicMock()
    rpc.add_GatewayServicer_to_server(MagicMock(), grpc_server)
    handlers = grpc_server.add_generic_rpc_handlers.call_args.args[0][0]
    assert "/celaut.Gateway/ListPeers" in handlers._method_handlers
    with patch("grpc.experimental.stream_stream", return_value="result") as call:
        assert rpc.Gateway.ListPeers(iter(()), "target") == "result"
        assert call.call_args.args[2] == "/celaut.Gateway/ListPeers"


def test_live_grpc_multiple_messages_cap_empty_policy_and_auth(monkeypatch):
    peers = [signed_peer(identity_index=i) for i in range(3)]
    sc, rows = database(*peers)
    settings = {"communication.MAX_PEERS_PER_GOSSIP_RESPONSE": 2}
    monkeypatch.setattr(server, "SQLConnection", lambda: sc)
    monkeypatch.setattr(server, "ConfigManager", lambda: SimpleNamespace(get=lambda k, d=None: settings.get(k, d)))
    def require(context, client):
        if client != "allowed":
            context.abort(grpc.StatusCode.UNAUTHENTICATED, "client required")
    monkeypatch.setattr(server, "require_caller", require)
    grpc_server = grpc.server(ThreadPoolExecutor(max_workers=2))
    rpc.add_GatewayServicer_to_server(Gateway(), grpc_server)
    port = grpc_server.add_insecure_port("127.0.0.1:0")
    grpc_server.start()
    try:
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            received = list(BeeClient.list_peers(channel, client_id="allowed", timeout=2))
            assert received == peers[:2]
            assert all(manager.verified_peer_public_key(p) == p.public_key for p in received)
            settings["communication.SHARE_KNOWN_PEERS"] = False
            assert list(BeeClient.list_peers(channel, client_id="allowed", timeout=2)) == []
            with pytest.raises(grpc.RpcError):
                list(BeeClient.list_peers(channel, timeout=2))
    finally:
        grpc_server.stop(0).wait()


# Reuse the existing real SQLite fixture: no mock verifier/registration here.
class TestTransitiveRegistration(_PeerFixture):
    @pytest.fixture(autouse=True)
    def db(self):
        self.setUp()
        yield
        self.tearDown()

    def test_a_learns_c_from_b_and_cannot_be_pinned_backwards(self, monkeypatch):
        b = signed_peer(identity_index=1)
        c = signed_peer(identity_index=2)
        assert manager.add_peer_instance(b) == b.public_key
        relay_db, _ = database(c, signed_peer("10.0.0.1", identity_index=3))
        monkeypatch.setattr(gossip, "_last_pull", None)
        monkeypatch.setattr(gossip, "ConfigManager", lambda: SimpleNamespace(get=lambda k, d=None: d))
        monkeypatch.setattr(gossip, "SQLConnection", lambda: manager.sc)
        monkeypatch.setattr(manager, "get_client_id_on_other_peer", lambda **kw: "a-at-b")
        monkeypatch.setattr(gossip, "peer_channel", lambda **kw: MagicMock())
        monkeypatch.setattr(BeeClient, "list_peers", lambda *a, **kw: iter_gossip_peers(relay_db, 100))
        monkeypatch.setattr(manager, "accept_peer_refresh", MagicMock(side_effect=AssertionError("must not refresh B with C")))
        gossip.gossip_pull_tick()
        assert set(manager.sc.get_peers_id()) == {b.public_key, c.public_key}
        assert self._uris(c.public_key) == ["8.8.8.8"]
        # A fresher signed direct claim wins. Gossip of old C cannot undo it.
        fresh = signed_peer("1.1.1.1", identity_index=2, ts=200)
        assert manager.add_peer_instance(fresh) == c.public_key
        monkeypatch.setattr(gossip, "_last_pull", None)
        gossip.gossip_pull_tick()
        assert self._uris(c.public_key) == ["1.1.1.1"]
        # A tampered payment/address claim does not bypass the real verifier either.
        c.uri[0].ip = "9.9.9.9"
        monkeypatch.setattr(BeeClient, "list_peers", lambda *a, **kw: iter([c]))
        monkeypatch.setattr(gossip, "_last_pull", None)
        gossip.gossip_pull_tick()
        assert self._uris(c.public_key) == ["1.1.1.1"]


def test_config_defaults():
    config = yaml.safe_load((Path(__file__).parents[1] / "config.example.yaml").read_text())["communication"]
    assert {key: config[key] for key in (
        "SHARE_KNOWN_PEERS", "DISCOVER_PEERS_VIA_GOSSIP", "MAX_PEERS_PER_GOSSIP_RESPONSE",
        "MAX_PEERS_PER_GOSSIP_PUSH", "GOSSIP_INTERVAL_SECONDS",
    )} == dict(SHARE_KNOWN_PEERS=True, DISCOVER_PEERS_VIA_GOSSIP=True,
              MAX_PEERS_PER_GOSSIP_RESPONSE=100, MAX_PEERS_PER_GOSSIP_PUSH=20,
              GOSSIP_INTERVAL_SECONDS=300)


def test_manager_pass_calls_both_self_gated_ticks(monkeypatch):
    from src.manager import maintain
    hooks = ("drain_wanted_inbox", "maintain_vmachines", "maintain_delegated_instances",
             "enforce_activity_window", "maintain_clients", "peer_deposits",
             "scheduler_tick", "energy_tick", "donations_tick", "onchain_reputation_tick",
             "network_change_tick", "gossip_pull_tick", "gossip_push_tick", "sleep")
    for name in hooks:
        monkeypatch.setattr(maintain, name, MagicMock())
    monkeypatch.setattr(maintain, "wanted_services", set())
    monkeypatch.setattr(maintain, "SHORT_INTERVAL_COUNT", 100)
    assert maintain._manager_pass(0) == 1
    maintain.gossip_pull_tick.assert_called_once_with()
    maintain.gossip_push_tick.assert_called_once_with()


def test_list_policy_off_does_not_read_advertisements(monkeypatch):
    context = MagicMock()
    config = SimpleNamespace(get=lambda k, d=None: False if k.endswith("SHARE_KNOWN_PEERS") else d)
    monkeypatch.setattr(server, "ConfigManager", lambda: config)
    db = MagicMock()
    monkeypatch.setattr(server, "SQLConnection", db)
    assert list(server.ListPeersIterable(iter(()), context)._peers()) == []
    db.assert_not_called()


@pytest.mark.parametrize("interval", [None, "invalid", float("nan"), float("inf"), 0, -1])
def test_bad_interval_falls_back_instead_of_hot_looping(tick, interval):
    tick.settings["communication.GOSSIP_INTERVAL_SECONDS"] = interval
    gossip.gossip_pull_tick()
    tick.clock[0] = 1
    gossip.gossip_pull_tick()
    assert tick.pull.call_count == 1
    tick.clock[0] = 300
    gossip.gossip_pull_tick()
    assert tick.pull.call_count == 2
