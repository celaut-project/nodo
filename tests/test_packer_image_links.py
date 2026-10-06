"""The packer keeps the link text of the image (#485).

The packer used `os.path.realpath()` on each extracted symlink. An absolute
target then went through the root of the packing host, not of the image:

* `/usr/bin/awk -> /etc/alternatives/awk` became the host's `/usr/bin/gawk`,
  which a slim Debian image does not have.
* `/lib64/ld-linux-x86-64.so.2 -> /lib/x86_64-linux-gnu/...` of a distroless
  image became `/usr/lib/x86_64-linux-gnu/...` on a usrmerge host, so no
  dynamic binary of the image could start.

`image_link_target` must give the link text as it is, for absolute and
relative links, whatever the host has at that path.
"""
import os
import tempfile
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from src.packers.zip_with_dockerfile import image_link_target
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    image_link_target = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"packer import failed: {IMPORT_ERROR}")
class ImageLinkTargetTest(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.addCleanup(self._tmp.cleanup)

    def _link(self, rel_path, target):
        path = os.path.join(self.root, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)
        return path

    def test_an_absolute_link_is_not_resolved_through_the_host(self):
        # The host has its own /etc/alternatives and /tmp; neither may leak in.
        self._link("etc/alternatives/awk", "/usr/bin/mawk")
        awk = self._link("usr/bin/awk", "/etc/alternatives/awk")
        tmp = self._link("var/tmp-link", "/tmp")

        self.assertEqual(image_link_target(awk), "/etc/alternatives/awk")
        self.assertEqual(image_link_target(tmp), "/tmp")

    def test_a_link_through_a_host_usrmerge_path_keeps_its_text(self):
        loader = "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"
        path = self._link("lib64/ld-linux-x86-64.so.2", loader)

        self.assertEqual(image_link_target(path), loader)

    def test_a_relative_link_keeps_its_text(self):
        os.makedirs(os.path.join(self.root, "usr/bin"))
        path = self._link("usr/bin/nawk", "../../etc/alternatives/nawk")

        self.assertEqual(image_link_target(path), "../../etc/alternatives/nawk")

    def test_a_dangling_link_keeps_its_text(self):
        path = self._link("usr/bin/pager", "/etc/alternatives/pager")

        self.assertEqual(image_link_target(path), "/etc/alternatives/pager")


if __name__ == "__main__":
    unittest.main()
