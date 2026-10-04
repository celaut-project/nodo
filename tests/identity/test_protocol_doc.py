"""The celaut-gateway prose describes every RPC, message and field (``protocol_doc``).

``formal`` identifies a field only by its number. A reader who must decide if two
declarations are the same protocol also needs what each element means. These pin that
every element has a description, that the descriptions come from the comments of
``celaut.proto`` and no other place, and that ``protos/celaut_doc.binpb`` is current.
"""
import os
import subprocess
import sys
import tempfile
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from google.protobuf import descriptor_pb2
    from protos import celaut_pb2
    from protos.gateway_bee import GATEWAY_RPCS
    from src.identity import protocol_doc, transport_stack
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

PROTO_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "protos")


def _without_source_info(file):
    """``file`` as the gencode has it: no comments and no ``json_name``."""
    copy = descriptor_pb2.FileDescriptorProto()
    copy.CopyFrom(file)
    copy.ClearField("source_code_info")

    def clear(messages):
        for message in messages:
            for field in message.field:
                field.ClearField("json_name")
            clear(message.nested_type)

    clear(copy.message_type)
    return copy


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CoverageTests(unittest.TestCase):
    def test_every_message_field_and_rpc_has_a_description(self):
        self.assertEqual(protocol_doc.undescribed(), [])

    def test_the_doc_descriptor_has_the_schema_of_the_gencode(self):
        # Run bash/generate_protos.sh after a change to celaut.proto.
        gencode = descriptor_pb2.FileDescriptorProto()
        gencode.ParseFromString(celaut_pb2.DESCRIPTOR.serialized_pb)
        self.assertEqual(
            _without_source_info(protocol_doc.load_file_descriptor()),
            _without_source_info(gencode),
        )

    def test_the_doc_descriptor_has_the_comments_of_celaut_proto(self):
        # Also run bash/generate_protos.sh after a change to a comment only.
        try:
            import grpc_tools  # noqa: F401
        except ImportError:
            self.skipTest("grpcio-tools is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "celaut_doc.binpb")
            subprocess.run(
                [sys.executable, "-m", "grpc_tools.protoc", f"-I{PROTO_DIR}",
                 "--include_source_info", f"--descriptor_set_out={out}",
                 os.path.join(PROTO_DIR, "celaut.proto"),
                 "--experimental_allow_proto3_optional"],
                check=True,
            )
            fresh = protocol_doc.load_file_descriptor(out)
        committed = protocol_doc.load_file_descriptor()
        self.assertEqual(
            {tuple(l.path): l.leading_comments for l in fresh.source_code_info.location},
            {tuple(l.path): l.leading_comments for l in committed.source_code_info.location},
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GatewayProseTests(unittest.TestCase):
    def setUp(self):
        self.prose = transport_stack.gateway_component()[1]

    def test_each_rpc_gives_its_indices_auth_and_description(self):
        for name, rpc in GATEWAY_RPCS.items():
            with self.subTest(rpc=name):
                description = protocol_doc.method_prose("Gateway", name)
                self.assertTrue(description)
                line = next(l for l in self.prose.splitlines() if l.startswith(f"{name}. "))
                self.assertIn(f"Authentication: {rpc.auth}.", line)
                self.assertIn(description, line)

    def test_each_field_gives_its_number_name_type_and_description(self):
        client = protocol_doc.descriptions()["celaut.Client.client_id"]
        self.assertIn(f"  Field 1, client_id: singular string. {client}", self.prose)

    def test_a_oneof_is_named_by_its_field_numbers(self):
        self.assertIn("  Fields 10, 11, 12, 13 are one oneof", self.prose)

    def test_the_prose_is_not_part_of_formal(self):
        # The descriptions are for a reader. A reworded comment must not change what
        # nodes compare.
        formal = transport_stack.gateway_component()[2].decode()
        self.assertNotIn(protocol_doc.descriptions()["celaut.Client.client_id"], formal)


if __name__ == "__main__":
    unittest.main()
