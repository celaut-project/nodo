"""A hidden node sends its services' outside TCP through Tor (routing.tor.SERVICE_EGRESS).

What the tests protect:

* in hidden mode, no rule ever lets a guest's packet leave the host towards a global
  address: TCP is translated to Tor's TransPort, and the rest stays dropped;
* traffic to the node's own networks (the gateway, local instances, the LAN) is not
  sent into Tor;
* the host's own checks of pow: candidates go through Tor too;
* a launch skips this node while Tor is not ready, instead of starting a guest with
  no egress.
"""
import http.server
import json
import select
import socket
import threading
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.gateway.launcher import launch_service as launch_mod
    from src.manager import pow_networks
    from src.routing import policy, registry, tunneled_http
    from src.routing.base import RoutingUnavailable
    from src.utils.config import ConfigManager
    from src.utils.config_validation import ConfigValidationError, validate_routing_config
    from src.utils.firewall import backends, policy as fw_policy
    from src.utils.firewall.rules import Chain, Verdict
    from src.utils.network_policy import (
        enforce_hidden_resolution,
        hidden_mode_rejection,
        needs_outside_egress,
    )
    from src.virtualizers.firewall import TransportProtocol
    from src.virtualizers.microvm import firewall as vm_firewall
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

VM, VM_IP = "vm-1", "192.168.200.7"
BRIDGE_IP, TRANS_PORT, DNS_PORT = "192.168.200.1", 9040, 9053
SUBNET = "192.168.200.0/24"


def override(**values):
    wanted = {key.replace("__", "."): value for key, value in values.items()}
    original = ConfigManager.get

    def get(self, key, default=None):
        return wanted[key] if key in wanted else original(self, key, default)

    return mock.patch.object(ConfigManager, "get", get)


def hidden(**extra):
    return override(routing__HIDDEN=True, virtualizers__ch__NETWORK_SUBNET=SUBNET, **extra)


def net(*tags):
    return celaut.Service.Network(tags=list(tags))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PolicyTests(unittest.TestCase):
    def test_local_addresses(self):
        for host in ("127.0.0.1", "192.168.1.10", "10.0.0.1", "fe80::1", "[::1]", "localhost"):
            self.assertTrue(policy.is_local_address(host), host)
        for host in ("93.184.216.34", "2606:4700::1", "node.example.com", ""):
            self.assertFalse(policy.is_local_address(host), host)

    def test_on_by_default_and_only_in_hidden_mode(self):
        with override(routing__HIDDEN=True):
            self.assertTrue(policy.service_egress_via_tor())
        with override(routing__HIDDEN=True, routing__tor__SERVICE_EGRESS=False):
            self.assertFalse(policy.service_egress_via_tor())
        with override(routing__HIDDEN=False, routing__tor__SERVICE_EGRESS=True):
            self.assertFalse(policy.service_egress_via_tor())

    def test_with_tor_egress_nothing_is_refused_for_the_domain(self):
        with hidden():
            self.assertIsNone(hidden_mode_rejection([net("*"), net("pow:ergo"), net("a.example.com")]))
            enforce_hidden_resolution(net("pow:ergo"))
        self.assertEqual(needs_outside_egress([net("postgres"), net("x", "pow:ergo")]), "pow:ergo")
        self.assertIsNone(needs_outside_egress([net("postgres")]))

    def test_the_setting_must_be_a_boolean(self):
        validate_routing_config({"routing": {"tor": {"SERVICE_EGRESS": False}}})
        with self.assertRaises(ConfigValidationError):
            validate_routing_config({"routing": {"tor": {"SERVICE_EGRESS": "yes"}}})


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class RedirectRuleTests(unittest.TestCase):
    def test_one_destination(self):
        dnat, accept = fw_policy.tor_redirect_rules(VM, VM_IP, BRIDGE_IP, TRANS_PORT,
                                                    ip="93.184.216.34", port=443)
        self.assertEqual((dnat.chain, dnat.verdict), (Chain.PREROUTING, Verdict.DNAT))
        self.assertEqual((dnat.source, dnat.destination, dnat.protocol, dnat.dport),
                         (VM_IP, "93.184.216.34", "tcp", 443))
        self.assertEqual(dnat.dnat_to, f"{BRIDGE_IP}:{TRANS_PORT}")
        self.assertEqual((accept.chain, accept.destination, accept.dport),
                         (Chain.INPUT, BRIDGE_IP, TRANS_PORT))
        self.assertTrue(dnat.comment.startswith(fw_policy.vm_comment_prefix(VM)))

    def test_everything_but_the_guest_subnet(self):
        dnat, _ = fw_policy.tor_redirect_rules(VM, VM_IP, BRIDGE_IP, TRANS_PORT,
                                               except_subnet=SUBNET)
        self.assertEqual((dnat.destination, dnat.destination_is_negated, dnat.dport),
                         (SUBNET, True, None))
        self.assertEqual(
            backends._render_nft(dnat),
            f"ip saddr {VM_IP} ip daddr != {SUBNET} meta l4proto tcp "
            f"dnat to {BRIDGE_IP}:{TRANS_PORT} comment \"{dnat.comment}\"",
        )
        self.assertEqual(
            backends._render_iptables(dnat)[:9],
            ["-p", "tcp", "-s", VM_IP, "!", "-d", SUBNET, "-j", "DNAT"],
        )

    def test_one_destination_or_a_range_not_both(self):
        with self.assertRaises(Exception):
            fw_policy.tor_redirect_rules(VM, VM_IP, BRIDGE_IP, TRANS_PORT,
                                         ip="1.1.1.1", except_subnet=SUBNET)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GuestFirewallTests(unittest.TestCase):
    def setUp(self):
        self.applied = []
        fake = mock.Mock()
        fake.ensure.side_effect = lambda rule: self.applied.append(rule) or True
        patcher = mock.patch.object(vm_firewall, "backend", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.proxy = mock.patch.object(registry.TOR, "transparent_proxy",
                                       return_value=(BRIDGE_IP, TRANS_PORT))
        self.proxy.start()
        self.addCleanup(self.proxy.stop)
        self.dns = mock.patch.object(registry.TOR, "guest_dns", return_value=(BRIDGE_IP, DNS_PORT))
        self.dns.start()
        self.addCleanup(self.dns.stop)

    def _forward_accepts_to_outside(self):
        return [r for r in self.applied
                if r.chain is Chain.FORWARD and r.verdict is Verdict.ACCEPT
                and not (r.destination or "").startswith("192.168.200.")
                and r.destination != SUBNET]

    def test_a_global_tcp_destination_goes_into_tor(self):
        with hidden():
            self.assertTrue(vm_firewall.allow_connection(VM, "93.184.216.34", 9030,
                                                         TransportProtocol.TCP, source_ip=VM_IP))
        self.assertEqual([r.chain for r in self.applied], [Chain.PREROUTING, Chain.INPUT])
        self.assertEqual(self._forward_accepts_to_outside(), [])

    def test_a_local_destination_is_reached_as_before(self):
        with hidden():
            self.assertTrue(vm_firewall.allow_connection(VM, "192.168.200.9", 5432,
                                                         TransportProtocol.TCP, source_ip=VM_IP))
        self.assertEqual([(r.chain, r.destination) for r in self.applied],
                         [(Chain.FORWARD, "192.168.200.9")])

    def test_udp_to_the_outside_is_refused(self):
        with hidden():
            self.assertFalse(vm_firewall.allow_connection(VM, "93.184.216.34", 53,
                                                          TransportProtocol.UDP, source_ip=VM_IP))
        self.assertEqual(self.applied, [])

    def test_nothing_is_opened_while_tor_is_not_ready(self):
        self.proxy.stop()
        with mock.patch.object(registry.TOR, "transparent_proxy", return_value=None), hidden():
            self.assertFalse(vm_firewall.allow_connection(VM, "93.184.216.34", 443,
                                                          TransportProtocol.TCP, source_ip=VM_IP))
            self.assertFalse(vm_firewall.allow_all_egress(VM, source_ip=VM_IP))
        self.proxy.start()
        self.assertEqual(self.applied, [])

    def test_without_tor_egress_the_outside_is_refused(self):
        with hidden(routing__tor__SERVICE_EGRESS=False):
            self.assertFalse(vm_firewall.allow_connection(VM, "93.184.216.34", 443,
                                                          TransportProtocol.TCP, source_ip=VM_IP))
            self.assertFalse(vm_firewall.allow_all_egress(VM, source_ip=VM_IP))
        self.assertEqual(self.applied, [])

    def test_all_egress_is_tor_for_tcp_and_the_subnet_directly(self):
        with hidden():
            self.assertTrue(vm_firewall.allow_all_egress(VM, source_ip=VM_IP))
        chains = [(r.chain, r.destination, r.destination_is_negated) for r in self.applied]
        self.assertIn((Chain.PREROUTING, SUBNET, True), chains)
        self.assertIn((Chain.FORWARD, SUBNET, False), chains)
        # Never the unrestricted accept: that would let UDP and any TCP out directly.
        self.assertFalse([r for r in self.applied if r.comment.endswith("allow_all_egress")])
        self.assertEqual(self._forward_accepts_to_outside(), [])

    def test_all_egress_sends_udp_dns_into_tor(self):
        # A '*' service uses its own resolver, usually over UDP 53 (#550, P6).
        with hidden():
            self.assertTrue(vm_firewall.allow_all_egress(VM, source_ip=VM_IP))
        [dnat] = [r for r in self.applied if r.chain is Chain.PREROUTING and r.protocol == "udp"]
        self.assertEqual((dnat.source, dnat.dport, dnat.destination), (VM_IP, 53, None))
        self.assertEqual(dnat.dnat_to, f"{BRIDGE_IP}:{DNS_PORT}")
        self.assertIn((Chain.INPUT, BRIDGE_IP, DNS_PORT, "udp"),
                      [(r.chain, r.destination, r.dport, r.protocol) for r in self.applied])
        # Only port 53: the rest of UDP stays dropped.
        self.assertFalse([r for r in self.applied if r.protocol == "udp" and r.dport != 53
                          and r.chain is not Chain.INPUT])

    def test_all_egress_is_refused_while_tor_dns_is_not_ready(self):
        self.dns.stop()
        with mock.patch.object(registry.TOR, "guest_dns", return_value=None), hidden():
            self.assertFalse(vm_firewall.allow_all_egress(VM, source_ip=VM_IP))
        self.dns.start()
        self.assertEqual(self.applied, [])

    def test_a_node_that_is_not_hidden_is_unchanged(self):
        with override(routing__HIDDEN=False):
            vm_firewall.allow_connection(VM, "93.184.216.34", 443, TransportProtocol.TCP,
                                         source_ip=VM_IP)
            vm_firewall.allow_all_egress(VM, source_ip=VM_IP)
        self.assertEqual([r.chain for r in self.applied], [Chain.FORWARD, Chain.FORWARD])
        self.assertTrue(self.applied[1].comment.endswith("allow_all_egress"))


class _ForwardingProxy:
    """A CONNECT proxy that really opens the tunnel, as Tor's HTTPTunnelPort does."""

    def __init__(self):
        self.requests = []
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(4)
        self.port = self._listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                client, _ = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(client,), daemon=True).start()

    def _handle(self, client):
        data = b""
        while b"\r\n\r\n" not in data:
            data += client.recv(1024)
        line = data.split(b"\r\n", 1)[0].decode()
        self.requests.append(line)
        host, port = line.split(" ")[1].rsplit(":", 1)
        upstream = socket.create_connection(("127.0.0.1", int(port)))
        client.sendall(b"HTTP/1.1 200 OK\r\n\r\n")
        sockets = [client, upstream]
        while True:
            readable, _, _ = select.select(sockets, [], [], 5)
            if not readable:
                break
            for s in readable:
                chunk = s.recv(65536)
                if not chunk:
                    client.close(); upstream.close()
                    return
                (upstream if s is client else client).sendall(chunk)

    def close(self):
        self._listener.close()


class _Json(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class TunneledHttpTests(unittest.TestCase):
    def test_a_get_goes_through_connect_with_the_host_name(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), _Json)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        proxy = _ForwardingProxy()
        self.addCleanup(proxy.close)
        port = server.server_address[1]
        with mock.patch.object(registry.TOR, "http_tunnel_port", return_value=proxy.port):
            status, body = tunneled_http.get(f"http://node.example:{port}/info?x=1", timeout=5)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"path": "/info?x=1"})
        # http.client sends CONNECT as HTTP/1.0; Tor accepts it.
        self.assertEqual([r.rsplit(" ", 1)[0] for r in proxy.requests],
                         [f"CONNECT node.example:{port}"])

    def test_without_tor_nothing_is_sent(self):
        with mock.patch.object(registry.TOR, "http_tunnel_port", return_value=None):
            with self.assertRaises(RoutingUnavailable):
                tunneled_http.get("http://node.example/info", timeout=1)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PowCheckTests(unittest.TestCase):
    def test_a_hidden_node_checks_a_candidate_through_tor(self):
        with hidden(), \
                mock.patch.object(pow_networks.routing_http, "get",
                                  return_value=(200, b'{"name": "ergo"}')) as tor_get, \
                mock.patch.object(pow_networks.requests, "get") as direct:
            self.assertEqual(pow_networks._get_json("http://node.example:9053", "/info", 5),
                             {"name": "ergo"})
        tor_get.assert_called_once_with("http://node.example:9053/info", timeout=5)
        direct.assert_not_called()

    def test_a_local_node_is_still_asked_directly(self):
        response = mock.Mock(status_code=200, json=lambda: {"ok": True})
        with hidden(), \
                mock.patch.object(pow_networks.routing_http, "get") as tor_get, \
                mock.patch.object(pow_networks.requests, "get", return_value=response):
            self.assertEqual(pow_networks._get_json("http://127.0.0.1:9053", "/info", 5),
                             {"ok": True})
        tor_get.assert_not_called()

    def test_without_tor_egress_a_hidden_node_asks_nobody(self):
        with hidden(routing__tor__SERVICE_EGRESS=False), \
                mock.patch.object(pow_networks.routing_http, "get") as tor_get, \
                mock.patch.object(pow_networks.requests, "get") as direct:
            self.assertIsNone(pow_networks._get_json("http://node.example:9053", "/info", 5))
        tor_get.assert_not_called()
        direct.assert_not_called()


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class HiddenNameResolutionTests(unittest.TestCase):
    """A hidden node never resolves a name of a service or a peer itself (#550, F2)."""

    def test_a_host_name_tag_is_resolved_through_tor(self):
        from src.manager import networks
        from src.routing import tor_dns

        with hidden(), \
                mock.patch.object(tor_dns, "resolve_ipv4", return_value=["93.184.216.34"]) as tor, \
                mock.patch.object(networks.socket, "getaddrinfo") as host:
            uris = networks.resolve_domain("n1.attacker.example", ports=[443])
        tor.assert_called_once_with("n1.attacker.example")
        host.assert_not_called()
        self.assertEqual([(u.ip, u.port) for u in uris], [("93.184.216.34", 443)])

    def test_a_host_name_tag_does_not_resolve_while_tor_is_not_ready(self):
        from src.manager import networks

        with hidden(), mock.patch.object(registry.TOR, "dns_port", return_value=None), \
                mock.patch.object(networks.socket, "getaddrinfo") as host:
            with self.assertRaises(ValueError):
                networks.resolve_domain("n1.attacker.example", ports=[443])
        host.assert_not_called()

    def test_a_pow_candidate_name_is_resolved_through_tor(self):
        from src.routing import tor_dns

        with hidden(), \
                mock.patch.object(tor_dns, "resolve_ipv4", return_value=["93.184.216.34"]), \
                mock.patch.object(pow_networks.socket, "getaddrinfo") as host:
            self.assertEqual(pow_networks._resolve_host("node.example", 9030),
                             ("93.184.216.34", 9030))
        host.assert_not_called()

    def test_a_pow_candidate_is_dropped_while_tor_is_not_ready(self):
        with hidden(), mock.patch.object(registry.TOR, "dns_port", return_value=None), \
                mock.patch.object(pow_networks.socket, "getaddrinfo") as host:
            self.assertIsNone(pow_networks._resolve_host("node.example", 9030))
        host.assert_not_called()

    def test_a_node_that_is_not_hidden_uses_its_own_resolver(self):
        from src.manager import networks
        from src.routing import tor_dns

        info = [(networks.socket.AF_INET, 1, 6, "", ("93.184.216.34", 0))]
        with override(routing__HIDDEN=False), \
                mock.patch.object(tor_dns, "resolve_ipv4") as tor, \
                mock.patch.object(networks.socket, "getaddrinfo", return_value=info):
            uris = networks.resolve_domain("example.com", ports=[443])
        tor.assert_not_called()
        self.assertEqual([u.ip for u in uris], ["93.184.216.34"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LaunchTests(unittest.TestCase):
    def _launch(self, service, candidates, tor_proxy):
        cost = SimpleNamespace(cost=SimpleNamespace())
        mod = launch_mod
        with ExitStack() as stack:
            def patched(target, name, **kwargs):
                return stack.enter_context(mock.patch.object(target, name, **kwargs))

            stack.enter_context(hidden(service_networks__default_instances={}))
            patched(registry.TOR, "transparent_proxy", return_value=tor_proxy)
            patched(mod, "declarations_for_service", return_value=[])
            patched(mod, "service_requires_parent_colocation", return_value=False)
            patched(mod, "authorize_shares", return_value=[])
            spend = patched(mod, "spend_mu", return_value=True)
            balancer = patched(mod, "execution_balancer")
            local = patched(mod, "local_execution", return_value=SimpleNamespace(instance=None))
            delegate = patched(mod, "delegate_execution", return_value=SimpleNamespace(instance=None))
            patched(mod, "from_amount", return_value=1)
            patched(mod, "enforce_network_policy")
            patched(mod, "evaluate_possible_environment_workloads", return_value=None)
            patched(mod, "_detect_local_preflight_failure", return_value=None)
            patched(mod, "descends_from_dev_client", return_value=True)
            patched(mod.activity_window, "is_open", return_value=True)
            patched(mod.demand_history, "record_admission")
            patched(mod.demand_history, "record_refusal")
            patched(mod.sc, "pop_forced_execution_peer", return_value=None)
            patched(mod.sc, "internal_instance_exists", return_value=False)
            patched(mod.utils, "get_network_name", return_value="lan")
            patched(mod, "get_arch_tag", return_value="amd64")
            patched(mod, "default_initial_balance", return_value=1)
            balancer.return_value = iter([(peer, cost) for peer in candidates])
            error = None
            try:
                mod.launch_service(service=service, metadata=celaut.Metadata(),
                                   configuration=celaut.Configuration(), father_id="client-1",
                                   father_ip="", service_id="svc")
            except Exception as e:
                error = e
            return SimpleNamespace(error=error, local=local, delegate=delegate)

    def test_runs_here_when_tor_is_ready(self):
        r = self._launch(celaut.Service(network=[net("pow:ergo")]), ["local"],
                         (BRIDGE_IP, TRANS_PORT))
        self.assertIsNone(r.error)
        r.local.assert_called_once()

    def test_a_peer_runs_it_while_tor_is_not_ready(self):
        r = self._launch(celaut.Service(network=[net("*")]), ["local", "peer-a"], None)
        self.assertIsNone(r.error)
        r.local.assert_not_called()
        self.assertEqual(r.delegate.call_args.kwargs["peer"], "peer-a")

    def test_local_members_need_no_tor(self):
        r = self._launch(celaut.Service(network=[net("postgres")]), ["local"], None)
        self.assertIsNone(r.error)
        r.local.assert_called_once()


if __name__ == "__main__":
    unittest.main()
