"""What an address says it speaks, and what a peer can check about it (issue #257).

An announced stack is a claim about parameters a caller has to agree with -- the
extension OID it will look the host key up by, what the signature in it covers, the
bee-rpc framing, which RPCs the address answers and with which messages. These pin
that the claim is *made* (the tags alone never said any of it), that it is *complete*
(every layer, the schema, the signed payloads), and that it is *checkable*: a node
that contradicts any of those parameters is seen as speaking something else. A node
that only worded its description differently, or that declares a message field or an
RPC more or less (protobuf and gRPC let the two talk), is not.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from protos.gateway_bee import GATEWAY_RPCS
    from src.identity import transport_stack
    from src.identity.node_identity import component_formal, parse_component_formal
    from src.identity.tls_identity import HOST_KEY_EXTENSION_OID, signature_prefix
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

LAYERS = ["tls", "http2", "grpc", "bee-rpc", "celaut-gateway"]


def _stack(prose=True):
    uri = celaut_pb2.Peer.Uri(ip="1.2.3.4", port=8080)
    transport_stack.declare_transport_stack(uri, prose=prose)
    return uri


def _component(components, tag):
    return next(c for c in components if tag in c.tags)


def _formal(tag):
    return parse_component_formal(_component(_stack().protocol_stack, tag).formal)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DeclarationTests(unittest.TestCase):
    def test_one_layer_per_level_bottom_to_top(self):
        # bee-rpc between grpc and the celaut gateway: it is what turns gRPC's stream of
        # Buffers into the objects the gateway's methods take.
        self.assertEqual([list(c.tags) for c in _stack().protocol_stack], [[t] for t in LAYERS])

    def test_the_tls_parameters_a_caller_needs_are_declared(self):
        formal = _formal("tls")
        self.assertEqual(formal["host_key_oid"], HOST_KEY_EXTENSION_OID.dotted_string)
        self.assertTrue(formal["host_key_signed"].startswith(signature_prefix()))
        self.assertEqual(formal["alpn"], "h2")

    def test_every_gateway_rpc_is_declared_with_its_messages(self):
        formal = _formal("celaut-gateway")
        served = {m.name for m in celaut_pb2.DESCRIPTOR.services_by_name["Gateway"].methods}
        declared = {k[len("rpc."):] for k in formal if k.startswith("rpc.")}
        self.assertEqual(declared, served)
        self.assertEqual(
            formal["rpc.Payable"],
            "in:1=celaut.Payment,2=celaut.Client;out:1=buffer.Empty;auth:client",
        )

    def test_the_registry_covers_exactly_the_served_rpcs(self):
        # The declaration refuses to build otherwise; this names the cause directly.
        served = {m.name for m in celaut_pb2.DESCRIPTOR.services_by_name["Gateway"].methods}
        self.assertEqual(set(GATEWAY_RPCS), served)

    def test_a_client_gated_rpc_takes_its_payload_at_one_and_the_client_at_two(self):
        # What client_gate.parse_with_client reads; a table that put them elsewhere
        # would describe a method the server does not serve.
        for name, rpc in GATEWAY_RPCS.items():
            if rpc.auth == "client" and len(rpc.input) == 2:
                self.assertIs(rpc.input[2], celaut_pb2.Client, name)

    def test_the_whole_schema_travels(self):
        gateway, bee = _formal("celaut-gateway"), _formal("bee-rpc")
        self.assertIn("schema.celaut.Peer.Uri", gateway)
        self.assertEqual(
            gateway["schema.celaut.Peer.Uri"],
            "1:singular:string,2:singular:int32,3:singular:int64,"
            "4:optional:celaut.Peer.Uri.Protocol,5:repeated:celaut.Peer.Uri.Protocol",
        )
        self.assertIn("schema.buffer.Buffer", bee)

    def test_every_agreed_prefix_is_declared(self):
        formal = _formal("celaut-gateway")
        self.assertTrue(formal["client_binding.payload"].startswith("celaut-client-binding:"))
        self.assertTrue(formal["ledger_attestation.payload"].startswith("celaut-ledger-attestation:"))
        self.assertEqual(formal["peer.signature.payload"], "<public_key_hex>|<ts>|<content_digest>")
        self.assertIn("blake2b-512", formal["pow.solution"])

    def test_what_a_sender_chooses_is_not_declared(self):
        # The port and the chunk size are each side's own choice: a peer that differs
        # on them still talks to this one, so they are not protocol.
        for tag in LAYERS:
            for key, value in _formal(tag).items():
                self.assertNotIn("chunk_size", key)
                self.assertNotIn("1048576", value)
        self.assertNotIn("8080", _component(_stack().protocol_stack, "tls").formal.decode())

    def test_the_prose_explains_the_framing_and_every_method(self):
        bee = _component(_stack().protocol_stack, "bee-rpc").prose
        for concept in ("chunk", "separator", "head", "signal", "block", "skip"):
            self.assertIn(concept, bee, f"the {concept} field is unexplained")
        gateway = _component(_stack().protocol_stack, "celaut-gateway").prose
        for method in celaut_pb2.DESCRIPTOR.services_by_name["Gateway"].methods:
            self.assertIn(method.name, gateway, f"{method.name} is announced but not described")

    def test_formal_is_canonical(self):
        # Compared byte for byte and covered by the announcement's signature, so it
        # must not depend on the order anything was built in.
        for component in _stack().protocol_stack:
            keys = [line.split("=", 1)[0] for line in component.formal.decode().splitlines()]
            self.assertEqual(keys, sorted(keys))

    def test_dropping_prose_keeps_what_is_compared(self):
        bare = _stack(prose=False)
        self.assertTrue(all(not c.prose for c in bare.protocol_stack))
        self.assertTrue(all(c.formal for c in bare.protocol_stack))
        self.assertTrue(transport_stack.speaks_our_transport_stack(bare.protocol_stack))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ComparisonTests(unittest.TestCase):
    def test_our_own_declaration_matches(self):
        self.assertTrue(transport_stack.speaks_our_transport_stack(_stack().protocol_stack))

    def test_a_different_host_key_oid_is_a_different_protocol(self):
        uri = _stack()
        tls = _component(uri.protocol_stack, "tls")
        tls.formal = tls.formal.replace(
            HOST_KEY_EXTENSION_OID.dotted_string.encode(), b"2.25.1"
        )
        self.assertFalse(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def test_a_different_signed_payload_is_a_different_protocol(self):
        uri = _stack()
        tls = _component(uri.protocol_stack, "tls")
        tls.formal = tls.formal.replace(
            f"host_key_signed={signature_prefix()}".encode(), b"host_key_signed=OTHER"
        )
        self.assertFalse(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def _gateway(self, edit):
        """Our stack, with ``edit`` applied to the gateway layer's ``formal`` pairs."""
        uri = _stack()
        gateway = _component(uri.protocol_stack, "celaut-gateway")
        pairs = parse_component_formal(gateway.formal)
        edit(pairs)
        gateway.formal = component_formal(pairs)
        return uri

    def test_an_rpc_only_one_side_declares_is_compatible(self):
        # A node does not call an RPC it does not know, so a peer with one RPC less
        # (or more) still talks with this node.
        fewer = self._gateway(lambda p: p.pop("rpc.Observe"))
        self.assertTrue(transport_stack.speaks_our_transport_stack(fewer.protocol_stack))
        more = self._gateway(lambda p: p.update({"rpc.NewMethod": "in:1=celaut.Client;out:;auth:none"}))
        self.assertTrue(transport_stack.speaks_our_transport_stack(more.protocol_stack))

    def test_an_rpc_with_other_messages_is_a_different_protocol(self):
        uri = self._gateway(lambda p: p.update({"rpc.Observe": "in:1=celaut.Client;out:1=celaut.Client;auth:token"}))
        self.assertFalse(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def test_an_rpc_with_another_auth_kind_is_a_different_protocol(self):
        def edit(pairs):
            pairs["rpc.Observe"] = pairs["rpc.Observe"].replace("auth:token", "auth:client")
        self.assertFalse(transport_stack.speaks_our_transport_stack(self._gateway(edit).protocol_stack))

    def test_a_field_only_one_side_declares_is_compatible(self):
        # A protobuf reader ignores a field it does not know, and reads a missing one
        # as its default value.
        def add(pairs):
            pairs["schema.celaut.Client"] += ",99:singular:uint64"
        self.assertTrue(transport_stack.speaks_our_transport_stack(self._gateway(add).protocol_stack))

        def remove(pairs):
            pairs["schema.celaut.Client"] = ""
        self.assertTrue(transport_stack.speaks_our_transport_stack(self._gateway(remove).protocol_stack))

    def test_a_message_only_one_side_declares_is_compatible(self):
        uri = self._gateway(lambda p: p.update({"schema.celaut.NewMessage": "1:singular:bytes"}))
        self.assertTrue(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def test_one_moved_field_number_is_compatible(self):
        # Field 1 is gone and field 9 is new: neither side contradicts the other.
        def edit(pairs):
            pairs["schema.celaut.Client"] = pairs["schema.celaut.Client"].replace(
                "1:singular:string", "9:singular:string"
            )
        self.assertTrue(transport_stack.speaks_our_transport_stack(self._gateway(edit).protocol_stack))

    def test_a_field_with_another_type_is_a_different_protocol(self):
        def edit(pairs):
            pairs["schema.celaut.Client"] = pairs["schema.celaut.Client"].replace(
                "1:singular:string", "1:singular:bytes"
            )
        self.assertFalse(transport_stack.speaks_our_transport_stack(self._gateway(edit).protocol_stack))

    def test_a_field_with_another_cardinality_is_a_different_protocol(self):
        def edit(pairs):
            pairs["schema.celaut.Client"] = pairs["schema.celaut.Client"].replace(
                "1:singular:string", "1:repeated:string"
            )
        self.assertFalse(transport_stack.speaks_our_transport_stack(self._gateway(edit).protocol_stack))

    def test_a_malformed_schema_line_is_a_different_protocol(self):
        uri = self._gateway(lambda p: p.update({"schema.celaut.Client": "not a field list"}))
        self.assertFalse(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def test_a_rule_only_one_side_declares_is_a_different_protocol(self):
        # Only messages and RPCs can grow. Any other key is a rule both sides follow.
        added = self._gateway(lambda p: p.update({"pow.extra": "a new rule"}))
        self.assertFalse(transport_stack.speaks_our_transport_stack(added.protocol_stack))
        removed = self._gateway(lambda p: p.pop("keyvalue"))
        self.assertFalse(transport_stack.speaks_our_transport_stack(removed.protocol_stack))

    def test_the_bee_rpc_schema_follows_the_same_rule(self):
        uri = _stack()
        bee = _component(uri.protocol_stack, "bee-rpc")
        pairs = parse_component_formal(bee.formal)
        pairs["schema.buffer.Buffer"] += ",99:singular:bool"
        bee.formal = component_formal(pairs)
        self.assertTrue(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def test_the_layers_out_of_order_are_a_different_protocol(self):
        uri = _stack()
        layers = list(uri.protocol_stack)
        del uri.protocol_stack[:]
        uri.protocol_stack.extend([layers[1], layers[0]] + layers[2:])
        self.assertFalse(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def test_rewording_the_prose_is_not_a_different_protocol(self):
        uri = _stack()
        for component in uri.protocol_stack:
            component.prose = "however this peer prefers to word it"
        self.assertTrue(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def test_an_empty_declaration_is_refused(self):
        # Every node declares its stack; an address that declares none gives a caller
        # nothing to check, and there is no older form to read it as.
        self.assertFalse(transport_stack.speaks_our_transport_stack([]))

    def test_a_partial_stack_is_refused(self):
        uri = _stack()
        del uri.protocol_stack[2:]
        self.assertFalse(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def test_a_stack_declaring_only_tags_is_refused(self):
        # Tags name a protocol without saying which version of it: a peer whose
        # layers carry only tags has not declared what this node checks (#467).
        uri = _stack()
        for layer in uri.protocol_stack:
            layer.ClearField("formal")
        self.assertTrue(all(layer.tags for layer in uri.protocol_stack))
        self.assertFalse(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def test_one_layer_declaring_only_tags_is_refused(self):
        uri = _stack()
        uri.protocol_stack[0].ClearField("formal")
        self.assertFalse(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def test_the_configured_service_hash_is_not_part_of_the_protocol(self):
        # hashing.HASH is each operator's choice (docs/CONFIG.md); two nodes on the
        # same code that chose differently still speak the same protocol (#467).
        from src.utils import hashing

        with mock.patch.object(hashing, "get_configured_hash_id", return_value=hashing.SHA256_ID):
            sha2 = _stack()
        with mock.patch.object(hashing, "get_configured_hash_id", return_value=hashing.SHA3_256_ID):
            sha3 = _stack()
        self.assertTrue(
            transport_stack.compatible_layer_stacks(sha2.protocol_stack, sha3.protocol_stack)
        )
        declared = _formal("celaut-gateway")["service_id.hash_types"].split(",")
        self.assertEqual(declared, sorted(h.hex() for h in hashing.HASH_SPECS))

    def test_a_layer_naming_nothing_is_refused(self):
        uri = _stack()
        layer = uri.protocol_stack[1]
        layer.ClearField("tags")
        layer.ClearField("formal")
        self.assertFalse(transport_stack.speaks_our_transport_stack(uri.protocol_stack))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LayerReportTests(unittest.TestCase):
    """``compare_layer_stacks``: the verdict above, explained position by position."""

    def _report(self, uri):
        return transport_stack.compare_layer_stacks(
            transport_stack.node_transport_stack(), uri.protocol_stack
        )

    def test_our_own_stack_matches_on_every_layer(self):
        self.assertEqual(
            [layer["status"] for layer in self._report(_stack(prose=False))],
            ["match"] * len(LAYERS),
        )

    def test_a_changed_parameter_is_named_with_both_values(self):
        uri = _stack()
        tls = _component(uri.protocol_stack, "tls")
        tls.formal = tls.formal.replace(
            HOST_KEY_EXTENSION_OID.dotted_string.encode(), b"2.25.1"
        )
        layer = self._report(uri)[0]
        self.assertEqual(layer["status"], "differs")
        self.assertEqual(
            layer["formal_difference"],
            {"host_key_oid": {
                "ours": HOST_KEY_EXTENSION_OID.dotted_string, "theirs": "2.25.1",
            }},
        )

    def test_a_dropped_rpc_shows_as_compatible_on_the_gateway_layer_only(self):
        uri = _stack()
        gateway = _component(uri.protocol_stack, "celaut-gateway")
        gateway.formal = b"\n".join(
            line for line in gateway.formal.split(b"\n") if not line.startswith(b"rpc.Observe=")
        )
        report = self._report(uri)
        self.assertEqual(
            [l["status"] for l in report], ["match"] * (len(LAYERS) - 1) + ["compatible"]
        )
        self.assertEqual(list(report[-1]["formal_difference"]), ["rpc.Observe"])

    def test_a_differing_layer_names_only_its_conflicts(self):
        uri = _stack()
        gateway = _component(uri.protocol_stack, "celaut-gateway")
        pairs = parse_component_formal(gateway.formal)
        pairs.pop("rpc.Observe")  # compatible on its own
        pairs["schema.celaut.Client"] = pairs["schema.celaut.Client"].replace(
            "1:singular:string", "1:singular:bytes"
        )
        gateway.formal = component_formal(pairs)
        layer = self._report(uri)[-1]
        self.assertEqual(layer["status"], "differs")
        self.assertEqual(list(layer["formal_difference"]), ["schema.celaut.Client"])

    def test_missing_and_extra_layers_are_told_apart(self):
        short = _stack()
        del short.protocol_stack[3:]
        self.assertEqual(
            [l["status"] for l in self._report(short)],
            ["match", "match", "match", "missing", "missing"],
        )
        long = _stack()
        long.protocol_stack.add(tags=["quic"], formal=b"version=1")
        self.assertEqual(self._report(long)[-1], {"tags": ["quic"], "status": "extra"})

    def test_an_unparseable_formal_is_still_reported(self):
        uri = _stack()
        _component(uri.protocol_stack, "grpc").formal = b"not key value"
        layer = self._report(uri)[2]
        self.assertEqual(layer["status"], "differs")
        self.assertIn("<formal>", layer["formal_difference"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ProsePolicyTests(unittest.TestCase):
    """Where the descriptions travel, and what it costs to hold them back.

    Two destinations charging differently for the same bytes: over gRPC they are
    transient, in a ledger register they are rented for as long as the box exists.
    """

    def _peer(self, prose=True):
        peer = celaut_pb2.Peer()
        uri = peer.uri.add(ip="1.2.3.4", port=8080)
        transport_stack.declare_transport_stack(uri, prose=prose)
        peer.signature_scheme.components.add(tags=["ed25519"], prose="a scheme" if prose else "")
        return peer

    def test_neither_destination_carries_prose_by_default(self):
        # gRPC pays it on every unauthenticated GetPeerInfo, a register pays rent on it
        # forever. Different bills, same answer: opt in where a reader needs to learn
        # the protocol from the announcement alone.
        self.assertFalse(transport_stack.share_prose_on_get_peer_info())
        self.assertFalse(transport_stack.share_prose_on_ledger())

    def test_prose_is_seen_in_either_declaration(self):
        # One policy, not two: the scheme and the stack are the same kind of thing --
        # what a reader is handed to understand the message -- so an announcement
        # carrying either is an announcement that is expensive to publish.
        peer = self._peer()
        self.assertTrue(transport_stack.carries_prose(peer))

        for component in peer.uri[0].protocol_stack:
            component.ClearField("prose")
        self.assertTrue(
            transport_stack.carries_prose(peer), "the scheme's prose still counts"
        )

        for component in peer.signature_scheme.components:
            component.ClearField("prose")
        self.assertFalse(transport_stack.carries_prose(peer))

    def test_a_bare_announcement_carries_none(self):
        # Which is what keeps a peer that already announces bare from being held back:
        # it is small already, so it is republished whole whatever the policy says.
        self.assertFalse(transport_stack.carries_prose(self._peer(prose=False)))

    def test_a_bare_announcement_still_declares_what_is_compared(self):
        # Why holding prose back is a size decision and never a correctness one.
        peer = self._peer(prose=False)
        self.assertTrue(all(c.formal for uri in peer.uri for c in uri.protocol_stack))
        self.assertTrue(
            transport_stack.speaks_our_transport_stack(peer.uri[0].protocol_stack)
        )


if __name__ == "__main__":
    unittest.main()
