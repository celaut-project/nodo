"""An empty node_protocol_stack means the service does not use the gateway (#648)."""
import unittest
from unittest.mock import patch

from protos import celaut_pb2 as celaut

try:
    from src.virtualizers.microvm import network, rootfs
except ImportError as error:  # the environment lacks a matching bee_rpc
    raise unittest.SkipTest(f"microvm modules do not import: {error}")

VM, VM_IP, PORT = "vm1", "192.168.200.9", 50051


class NodeProtocolStackTest(unittest.TestCase):
    def test_empty_stack_does_not_use_the_gateway(self):
        self.assertFalse(rootfs.service_uses_gateway(celaut.Service()))

    def test_declared_stack_uses_the_gateway(self):
        service = celaut.Service()
        service.container.node_protocol_stack.add(tags=["grpc"])
        self.assertTrue(rootfs.service_uses_gateway(service))

    def test_config_names_the_gateway_only_if_used(self):
        with patch.object(rootfs, "generate_node_peer_info"), \
             patch.object(rootfs, "peer_gateway_instance",
                          return_value=celaut.Instance()) as gateway:
            cfg = rootfs.build_configuration_file(None, None, [], uses_gateway=False)
            self.assertFalse(cfg.HasField("gateway"))
            gateway.assert_not_called()
            cfg = rootfs.build_configuration_file(None, None, [], uses_gateway=True)
            self.assertTrue(cfg.HasField("gateway"))

    def _host_allows(self, uses_gateway):
        host = []
        with patch.object(network, "vm_block_all", return_value=True), \
             patch.object(network, "vm_allow_host_connection",
                          side_effect=lambda **kw: host.append(kw) or True), \
             patch.object(network.env_manager, "get_gateway_port", return_value=PORT), \
             patch.object(network.env_manager, "get_plaintext_gateway_port", return_value=0):
            network.configure_guest_firewall_policy(
                "[t]", VM, VM_IP, [], uses_gateway=uses_gateway)
        return host

    def test_firewall_opens_the_gateway_only_if_used(self):
        self.assertEqual(self._host_allows(False), [])
        self.assertEqual(len(self._host_allows(True)), 1)


if __name__ == "__main__":
    unittest.main()
