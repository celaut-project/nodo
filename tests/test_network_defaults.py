from unittest.mock import MagicMock, patch
from tests.config_bootstrap import load_example_config
load_example_config()
from src.manager import network_defaults as defaults
from tests.test_pow_networks import _load_networks_module, _network


def config(entries):
    obj = MagicMock()
    obj.get.side_effect = lambda key, default=None: entries if key == defaults.DEFAULT_INSTANCES_KEY else default
    return obj


def test_arbitrary_tag_resolves_separate_instances_before_dns():
    module = _load_networks_module()
    assert module is not None
    with patch.object(module, "env_manager", config({"custom.test": ["tcp://10.0.0.1:9000", "tcp://10.0.0.2:9000"]})), \
         patch.object(defaults.socket, "getaddrinfo", side_effect=lambda host, port, *args: [(None, None, None, None, (host, port))]), \
         patch.object(module, "resolve_domain") as dns:
        peers = module.resolve_network(_network(tags=("custom.test",), formal=b""))
    assert len(peers) == 2
    assert [p.uri_slot[0].uri[0].ip for p in peers] == ["10.0.0.1", "10.0.0.2"]
    dns.assert_not_called()


def test_bad_entries_do_not_hide_good_seeds():
    entries = defaults.configured_endpoints("custom", config({"custom": [None, 42, "tcp://host:999999", "http://host", "http://host"]}))
    with patch.object(defaults.socket, "getaddrinfo", return_value=[(None, None, None, None, ("10.0.0.3", 80))]):
        assert defaults.endpoint_addresses(entries) == [("10.0.0.3", 80)]


def test_bad_defaults_fall_back_to_existing_dns_resolution():
    module = _load_networks_module()
    assert module is not None
    from protos import celaut_pb2 as celaut
    with patch.object(module, "env_manager", config({"custom.test": ["tcp://host:999999"]})), \
         patch.object(module, "resolve_domain", return_value=[celaut.Instance.Uri(ip="10.0.0.4", port=443)]) as dns:
        peers = module.resolve_network(_network(tags=("custom.test",), formal=b""))
    dns.assert_called_once_with("custom.test", ports=[80, 443])
    assert peers[0].uri_slot[0].uri[0].port == 443


def test_old_pow_scoped_config_is_not_read():
    old = MagicMock()
    old.get.side_effect = lambda key, default=None: {"pow:ergo": ["http://old"]} if key == "pow_networks.ENDPOINTS" else default
    assert defaults.configured_endpoints("pow:ergo", old) == []
