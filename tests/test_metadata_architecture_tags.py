"""``AttrHashTag.key`` is the field number of a part in the message that contains it.

The packer wrote 1 for the container, but ``Service.container`` is field 2, and the
reader of architecture tags read the list by position. These pin both to the field
numbers.
"""
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2 as celaut
    from src.virtualizers import architecture
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def _attr(key, *hashtags):
    return celaut.Metadata.HashTag.AttrHashTag(key=key, value=list(hashtags))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class MetadataArchitectureTagsTests(unittest.TestCase):
    def test_the_architecture_is_read_by_field_number(self):
        container = celaut.Metadata.HashTag(attr_hashtag=[
            _attr(2, celaut.Metadata.HashTag()),  # filesystem
            _attr(1, celaut.Metadata.HashTag(tag=["linux/arm64"])),  # architecture
        ])
        metadata = celaut.Metadata(hashtag=celaut.Metadata.HashTag(attr_hashtag=[
            _attr(5, celaut.Metadata.HashTag(tag=["not-a-container"])),
            _attr(2, container),
        ]))
        self.assertEqual(architecture._tags_from_metadata(metadata), {"linux/arm64"})

    def test_a_metadata_with_only_the_filesystem_gives_no_tags(self):
        # What the packer writes: the container with its filesystem, no architecture.
        metadata = celaut.Metadata(hashtag=celaut.Metadata.HashTag(attr_hashtag=[
            _attr(2, celaut.Metadata.HashTag(attr_hashtag=[_attr(2, celaut.Metadata.HashTag())])),
        ]))
        self.assertEqual(architecture._tags_from_metadata(metadata), set())

    def test_the_field_numbers_come_from_the_schema(self):
        self.assertEqual(architecture._SERVICE_CONTAINER_FIELD, 2)
        self.assertEqual(architecture._CONTAINER_ARCHITECTURE_FIELD, 1)


class PackerContainerKeyTests(unittest.TestCase):
    def test_the_packer_writes_the_container_at_field_two(self):
        # The packer imports heavy build tooling, so its source is read as text.
        import os
        path = os.path.join(os.path.dirname(__file__), "..", "src", "packers", "zip_with_dockerfile.py")
        source = open(path, encoding="utf-8").read()
        self.assertIn("key=2,  # Service.container is field 2", source)
        self.assertNotIn("key=1,  # Container attr.", source)


if __name__ == "__main__":
    unittest.main()
