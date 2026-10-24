"""`ledgers.ergo.NODE_URL` must never end up empty, and an empty one must say so (#441).

The node blanked the key itself when its Ergo node and every crawled peer were
unreachable at once, and saved that to config.yaml. From then on every Ergo request
was built on "" -- the balance check hit requests' "Invalid URL '/info': No scheme
supplied", reported "insufficient balance", and no deposit could ever be paid.
"""
import json

import pytest

from tests.config_bootstrap import load_example_config

load_example_config()

from src.manager import ergo as manager_ergo  # noqa: E402
from src.payment_system.contracts.ergo import interface  # noqa: E402
from src.utils.ergo_node_url import (  # noqa: E402
    ErgoNodeUrlNotConfigured,
    ergo_node_url_problem,
    require_ergo_node_url,
)

NODE = "https://node.example:9053"


class _Config:
    def __init__(self, values):
        self.values = dict(values)
        self.writes = []

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.writes.append((key, value))
        self.values[key] = value


@pytest.fixture
def config(monkeypatch, tmp_path):
    cfg = _Config({
        "ledgers.ergo.NODE_URL": NODE,
        "ledgers.ergo.HTTP_PEERS_PATH": str(tmp_path / "peers.json"),
        "ledgers.ergo.GENESIS_BLOCK_ID": "genesis",
    })
    monkeypatch.setattr(manager_ergo, "env_manager", cfg)
    monkeypatch.setattr(manager_ergo, "internet_available", lambda: True)
    return cfg


def _unreachable(calls):
    def get(url, *args, **kwargs):
        calls.append(url)
        raise manager_ergo.requests.exceptions.ConnectionError(f"down: {url}")
    return get


@pytest.mark.parametrize("value", [None, "", "   ", "/", "node.sigmaspace.io", "ftp://node.example"])
def test_unusable_node_urls_are_named_as_a_config_problem(value):
    assert "ledgers.ergo.NODE_URL" in ergo_node_url_problem(value)
    with pytest.raises(ErgoNodeUrlNotConfigured, match="ledgers.ergo.NODE_URL"):
        require_ergo_node_url(value)


def test_a_usable_node_url_passes_normalised():
    assert ergo_node_url_problem(NODE) == ""
    assert require_ergo_node_url(f" {NODE}/ ") == NODE


def test_an_outage_with_no_reachable_peer_keeps_the_configured_node(config, monkeypatch):
    calls = []
    monkeypatch.setattr(manager_ergo.requests, "get", _unreachable(calls))

    manager_ergo.check_ergo_node_availability()

    assert config.values["ledgers.ergo.NODE_URL"] == NODE
    assert ("ledgers.ergo.NODE_URL", "") not in config.writes
    assert calls  # it did try


def test_a_crawl_that_reached_nobody_keeps_the_known_peers(config, monkeypatch, tmp_path):
    peers_file = tmp_path / "peers.json"
    known = {"https://peer.example:9053": {"appVersion": "6.0"}}
    peers_file.write_text(json.dumps(known))
    monkeypatch.setattr(manager_ergo.requests, "get", _unreachable([]))

    assert manager_ergo.get_refresh_peers() == {}
    assert json.loads(peers_file.read_text()) == known


def test_an_empty_node_url_is_never_requested_as_a_relative_url(config, monkeypatch):
    config.values["ledgers.ergo.NODE_URL"] = ""
    calls = []
    monkeypatch.setattr(manager_ergo.requests, "get", _unreachable(calls))

    manager_ergo.check_ergo_node_availability()

    assert all(url.startswith(("http://", "https://")) for url in calls), calls


def test_the_payment_system_refuses_an_empty_node_url_by_name(monkeypatch):
    built = []

    class _AppKit:
        @staticmethod
        def ErgoAppKit(node_url):
            built.append(node_url)
            return object()

    monkeypatch.setattr(interface, "_ergo_runtime", lambda: (_AppKit, None, None, None))
    init_ergo = getattr(interface, "__init_ergo")

    monkeypatch.setattr(interface, "ERGO_NODE_URL", lambda: "")
    with pytest.raises(ErgoNodeUrlNotConfigured, match="ledgers.ergo.NODE_URL"):
        init_ergo()
    assert built == []

    monkeypatch.setattr(interface, "ERGO_NODE_URL", lambda: NODE)
    init_ergo()
    assert built == [NODE + "/"]


def test_the_balance_check_logs_the_config_error_and_its_type(monkeypatch):
    logged = []
    monkeypatch.setattr(interface, "LOGGER", logged.append)
    monkeypatch.setattr(interface, "ERGO_NODE_URL", lambda: "")
    monkeypatch.setattr(interface, "WALLET_MNEMONIC", lambda: "unused")

    assert interface.check_sender_balance(1) is False
    assert any(
        "ErgoNodeUrlNotConfigured" in line and "ledgers.ergo.NODE_URL is not set" in line
        for line in logged
    ), logged


# -- hidden mode: the crawl never leaves the host directly (#552) ------------------

STRANGER = "https://stranger.example:9053"


class _Answer:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def _hidden(monkeypatch, *, egress=True):
    monkeypatch.setattr(manager_ergo, "hidden_mode", lambda: True)
    monkeypatch.setattr(manager_ergo, "service_egress_via_tor", lambda: egress)
    monkeypatch.setattr(manager_ergo, "is_local_address", lambda host: host == "192.168.1.5")


def _tor(monkeypatch, answers):
    asked = []

    def get(url, timeout):
        asked.append(url)
        return 200, json.dumps(answers[url]).encode()

    monkeypatch.setattr(manager_ergo.routing_http, "get", get)
    return asked


def test_a_hidden_crawl_makes_no_direct_request(config, monkeypatch, tmp_path):
    _hidden(monkeypatch)
    (tmp_path / "peers.json").write_text(json.dumps({STRANGER: {}}))
    direct = []
    monkeypatch.setattr(manager_ergo.requests, "get", _unreachable(direct))
    asked = _tor(monkeypatch, {
        f"{STRANGER}/peers/connected": [{"restApiUrl": "https://other.example:9053"}],
        "https://other.example:9053/info": {"genesisBlockId": "genesis"},
        "https://other.example:9053/peers/connected": [],
    })

    found = manager_ergo.get_refresh_peers()

    assert direct == []
    assert "https://other.example:9053/info" in asked
    assert "https://other.example:9053" in found
    # the file that feeds the `pow:` candidates is still written
    assert "https://other.example:9053" in json.loads((tmp_path / "peers.json").read_text())


def test_a_hidden_crawl_without_tor_egress_asks_nobody(config, monkeypatch, tmp_path):
    _hidden(monkeypatch, egress=False)
    known = {STRANGER: {"appVersion": "6.0"}}
    (tmp_path / "peers.json").write_text(json.dumps(known))
    direct = []
    monkeypatch.setattr(manager_ergo.requests, "get", _unreachable(direct))
    asked = _tor(monkeypatch, {})

    assert manager_ergo.get_refresh_peers() == {}
    assert direct == [] and asked == []
    assert json.loads((tmp_path / "peers.json").read_text()) == known


def test_a_hidden_crawl_keeps_the_operators_own_network_direct(config, monkeypatch):
    _hidden(monkeypatch)
    direct = []

    def get(url, *args, **kwargs):
        direct.append(url)
        return _Answer([])

    monkeypatch.setattr(manager_ergo.requests, "get", get)
    asked = _tor(monkeypatch, {})

    assert manager_ergo._crawl_get_json("http://192.168.1.5:9053/peers/connected") == []
    assert direct == ["http://192.168.1.5:9053/peers/connected"] and asked == []


def test_a_crawl_that_is_not_hidden_goes_direct(config, monkeypatch):
    monkeypatch.setattr(manager_ergo, "hidden_mode", lambda: False)
    monkeypatch.setattr(manager_ergo.requests, "get", lambda url, **kw: _Answer({"ok": True}))
    asked = _tor(monkeypatch, {})

    assert manager_ergo._crawl_get_json(f"{STRANGER}/info") == {"ok": True}
    assert asked == []
