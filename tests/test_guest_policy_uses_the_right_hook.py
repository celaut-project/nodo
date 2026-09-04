"""Each destination in a guest's policy has to be written on the hook it traverses.

`microvm.network.configure_guest_firewall_policy` writes three kinds of allow,
and they do not belong in the same chain:

  * the node's own gRPC gateway is on the bridge's gateway address -- one of the
    *host's* addresses. A packet sent there is delivered locally and is evaluated on
    the input hook; it never reaches forward. Written as an ordinary egress allow
    (chain FORWARD) it could not match a single packet, while the log announced it
    as granted access.
  * a peer instance or an allow-listed domain is somewhere else, so the host routes
    it and forward is exactly right.

This pins which helper each one goes through, so the distinction cannot be lost in
a refactor that treats "allow this destination" as one operation. It is one
implementation for the whole microVM family, so a QEMU guest gets exactly these
rules on exactly these hooks too.
"""
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from protos import celaut_pb2 as celaut
    from src.virtualizers.microvm import network
    from src.virtualizers.microvm.errors import MicroVMError
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    network = None  # type: ignore[assignment]
    MicroVMError = Exception  # type: ignore[assignment]

VM = "d1cc08a0fb1cac5cb725d76629b7e06c186c7f0aa04d65fe469356f7f437e3c8"
VM_IP = "192.168.200.148"
GATEWAY_PORT = 58443


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PolicyHookTests(unittest.TestCase):
    def _configure(self, network_resolution=()):
        calls = {"host": [], "block_all": [], "instance": [], "all_egress": []}

        def _host(vmachine_id, host_ip, port=None, protocol=None, source_ip=None):
            calls["host"].append((host_ip, port, protocol.value, source_ip))
            return True

        def _block_all(vmachine_id, source_ip=None):
            calls["block_all"].append(source_ip)
            return True

        def _to_instance(vmachine_id, instance, source_ip=None):
            calls["instance"].append(source_ip)
            return True

        def _all_egress(vmachine_id, source_ip=None):
            calls["all_egress"].append(source_ip)
            return True

        with patch.object(network, "vm_allow_host_connection", side_effect=_host), \
             patch.object(network, "vm_block_all", side_effect=_block_all), \
             patch.object(network, "vm_allow_connection_to_instance", side_effect=_to_instance), \
             patch.object(network, "vm_allow_all_egress", side_effect=_all_egress), \
             patch.object(network.env_manager, "get_gateway_port", return_value=GATEWAY_PORT):
            network.configure_guest_firewall_policy(
                log_prefix=f"[CH][{VM}]",
                vmachine_id=VM,
                vm_ip=VM_IP,
                network_resolution=list(network_resolution),
            )
        return calls

    def test_the_gateway_goes_through_the_host_hook_never_the_forward_one(self):
        calls = self._configure()

        gateway = (network.NETWORK_GATEWAY_IP, GATEWAY_PORT, "tcp", VM_IP)
        self.assertIn(gateway, calls["host"])

    def test_both_gateway_ports_are_opened(self):
        # A guest is handed the plaintext port in its `__config__` and may pin the TLS
        # one instead (issue #257), so an allow for only one of them leaves a service
        # pointed at a port its own firewall drops.
        calls = self._configure()

        self.assertEqual(
            sorted(port for _, port, _, _ in calls["host"]),
            [GATEWAY_PORT, GATEWAY_PORT + 1],
        )

    def test_the_node_grants_no_dns_access_of_its_own(self):
        # nodo does not serve DNS: name resolution is a service's job, fed by the
        # `network_resolution` the node already hands over in `__config__`. A rule
        # for port 53 on the host would describe an access to nothing.
        calls = self._configure()

        self.assertEqual(
            [entry for entry in calls["host"] if entry[1] == 53], []
        )

    def test_the_forward_egress_helper_is_not_even_reachable_from_here(self):
        # `vm_allow_connection` (chain FORWARD) was imported by this module for the
        # gateway and the resolver alone. Both are host destinations and go through
        # the host helper, so the forward one has no business here: an import of it
        # is a sign someone wrote a host allow on the wrong hook again.
        self.assertFalse(hasattr(network, "vm_allow_connection"))

        calls = self._configure()
        # The gRPC gateway on its two ports, and no other host destination at all.
        self.assertEqual(
            {(host_ip, source_ip) for host_ip, _, _, source_ip in calls["host"]},
            {(network.NETWORK_GATEWAY_IP, VM_IP)},
        )

    def test_the_blanket_drop_is_still_applied_first(self):
        calls = self._configure()

        self.assertEqual(calls["block_all"], [VM_IP])

    def test_a_peer_instance_still_goes_through_the_forward_hook(self):
        resolution = celaut.ConfigurationFile.NetworkResolution()
        resolution.tags.append("some-dependency")
        resolution.peer_instances.append(celaut.Instance())

        calls = self._configure([resolution])

        # Routed elsewhere, so forward is the correct hook -- via the instance helper.
        self.assertEqual(calls["instance"], [VM_IP])

    def test_the_open_internet_tag_still_takes_the_forward_wide_allow(self):
        resolution = celaut.ConfigurationFile.NetworkResolution()
        resolution.tags.append("*")

        calls = self._configure([resolution])

        self.assertEqual(calls["all_egress"], [VM_IP])

    def test_a_failure_on_the_host_hook_aborts_the_launch(self):
        with patch.object(network, "vm_block_all", return_value=True), \
             patch.object(network, "vm_allow_host_connection", return_value=False), \
             patch.object(network.env_manager, "get_gateway_port", return_value=GATEWAY_PORT):
            with self.assertRaises(MicroVMError) as ctx:
                network.configure_guest_firewall_policy(
                    log_prefix=f"[CH][{VM}]",
                    vmachine_id=VM,
                    vm_ip=VM_IP,
                    network_resolution=[],
                )
        self.assertIn("gateway access", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
