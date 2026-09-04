"""What an address says it speaks, and what a peer can check about it (issue #257).

An announced ``["tls", "grpc"]`` is a claim about parameters a caller has to agree with
byte for byte -- the extension OID it will look the host key up by, what the signature
in it covers, which RPCs the address answers. These pin that the claim is *made* (the
tags alone never said any of it) and that it is *checkable*: a node differing in any of
those parameters is seen as speaking something else, while one that merely worded its
description differently is not.
"""
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.utils import transport_stack
    from src.utils.tls_identity import HOST_KEY_EXTENSION_OID, signature_prefix
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def _stack(prose=True):
    uri = celaut_pb2.Peer.Uri(ip="1.2.3.4", port=8080)
    transport_stack.declare_transport_stack(uri, prose=prose)
    return uri


def _component(components, tag):
    return next(c for c in components if tag in c.tags)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DeclarationTests(unittest.TestCase):
    def test_the_tags_stay_the_plain_protocol_names(self):
        # A variant is the same protocol with different parameters, not a new one, so
        # the parameters go in `formal` and the tag keeps naming the protocol.
        self.assertEqual(
            sorted(tuple(c.tags) for c in _stack().protocol_stack),
            [("grpc",), ("tls",)],
        )

    def test_the_tls_parameters_a_caller_needs_are_declared(self):
        # Everything a caller must agree with to verify a certificate at all: which OID
        # holds the host key, and what the signature inside it covers.
        formal = _component(_stack().protocol_stack, "tls").formal.decode()
        self.assertIn(f"host_key_oid={HOST_KEY_EXTENSION_OID.dotted_string}", formal)
        self.assertIn(f"host_key_signed={signature_prefix()}", formal)

    def test_every_gateway_rpc_is_declared(self):
        # Read from the compiled descriptor, so a node that adds or drops an RPC
        # announces a different stack without anyone remembering to update a list.
        formal = _component(_stack().protocol_stack, "grpc").formal.decode()
        declared = next(
            line[len("methods="):] for line in formal.splitlines()
            if line.startswith("methods=")
        ).split(",")
        served = celaut_pb2.DESCRIPTOR.services_by_name["Gateway"].methods
        self.assertEqual(sorted(declared), sorted(m.name for m in served))

    def test_the_prose_explains_the_framing_and_every_method(self):
        # While `formal` points at no published specification, the prose IS the
        # specification: a reader holding only this announcement cannot follow a link
        # into a repository, so naming bee-rpc without saying what it is, or listing an
        # RPC without saying what it does, would leave the descriptor unimplementable.
        prose = _component(_stack().protocol_stack, "grpc").prose
        for framing_concept in ("chunk", "separator", "head", "signal", "block"):
            self.assertIn(framing_concept, prose, f"the {framing_concept} field is unexplained")
        for method in celaut_pb2.DESCRIPTOR.services_by_name["Gateway"].methods:
            self.assertIn(method.name, prose, f"{method.name} is announced but not described")

    def test_formal_is_canonical(self):
        # It is compared byte for byte and covered by the announcement's signature, so
        # it must not depend on the order anything was built in.
        for component in _stack().protocol_stack:
            lines = component.formal.decode().splitlines()
            self.assertEqual(lines, sorted(lines))

    def test_dropping_prose_keeps_what_is_compared(self):
        # The Ergo-register form pays storage rent forever, so it drops the prose --
        # which must cost a reader detail, never a verifier its decision.
        bare = _stack(prose=False)
        self.assertTrue(all(not c.prose for c in bare.protocol_stack))
        self.assertTrue(all(c.formal for c in bare.protocol_stack))
        self.assertTrue(transport_stack.speaks_our_transport_stack(bare.protocol_stack))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ComparisonTests(unittest.TestCase):
    def test_our_own_declaration_matches(self):
        self.assertTrue(
            transport_stack.speaks_our_transport_stack(_stack().protocol_stack)
        )

    def test_a_different_host_key_oid_is_a_different_protocol(self):
        # The case the whole declaration exists for: same tags, and a caller that would
        # look the host key up somewhere this node does not put it.
        uri = _stack()
        tls = _component(uri.protocol_stack, "tls")
        tls.formal = tls.formal.replace(
            HOST_KEY_EXTENSION_OID.dotted_string.encode(), b"2.25.1"
        )
        self.assertFalse(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def test_a_different_signed_payload_is_a_different_protocol(self):
        # Same OID, same extension format, and every signature would still fail to
        # verify because the two sides hash different bytes.
        uri = _stack()
        tls = _component(uri.protocol_stack, "tls")
        tls.formal = tls.formal.replace(
            f"host_key_signed={signature_prefix()}".encode(), b"host_key_signed=OTHER"
        )
        self.assertFalse(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def test_one_missing_rpc_is_a_different_protocol(self):
        # "aun lo mas minimo": a gateway serving fifteen methods and one serving
        # fourteen do not speak the same thing, whatever both call themselves.
        uri = _stack()
        grpc = _component(uri.protocol_stack, "grpc")
        grpc.formal = grpc.formal.replace(b",Observe", b"")
        self.assertFalse(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def test_rewording_the_prose_is_not_a_different_protocol(self):
        # Deciding that two differently-worded descriptions mean the same protocol is a
        # judgement for a service to make, not a node -- so a node does not refuse over
        # it. `formal` is what carries the decidable part.
        uri = _stack()
        for component in uri.protocol_stack:
            component.prose = "however this peer prefers to word it"
        self.assertTrue(transport_stack.speaks_our_transport_stack(uri.protocol_stack))

    def test_an_empty_declaration_is_not_a_refusal(self):
        # An announcement that predates this declaration says nothing, rather than
        # saying it speaks nothing.
        self.assertTrue(transport_stack.speaks_our_transport_stack([]))

    def test_a_half_declared_stack_is_refused(self):
        # Announcing only one of the two components is not "tls and grpc".
        uri = _stack()
        del uri.protocol_stack[1:]
        self.assertFalse(transport_stack.speaks_our_transport_stack(uri.protocol_stack))


if __name__ == "__main__":
    unittest.main()
