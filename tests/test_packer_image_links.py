"""The packer keeps the link text of the image (#485).

The packer used `os.path.realpath()` on each extracted symlink. An absolute
target then went through the root of the packing host, not of the image:

* `/usr/bin/awk -> /etc/alternatives/awk` became the host's `/usr/bin/gawk`,
  which a slim Debian image does not have.
* `/lib64/ld-linux-x86-64.so.2 -> /lib/x86_64-linux-gnu/...` of a distroless
  image became `/usr/lib/x86_64-linux-gnu/...` on a usrmerge host, so no
  dynamic binary of the image could start.

`image_link_target` must give the link text as it is, for absolute and
relative links, whatever the host has at that path. A link whose target
leaves the image root must stop the pack.
"""
import os
import tempfile
import unittest

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from src.packers.zip_with_dockerfile import ImageLinkEscapeError, image_link_target
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    image_link_target = None  # type: ignore[assignment]
    ImageLinkEscapeError = None  # type: ignore[assignment]


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

    def _target(self, rel_path, target):
        return image_link_target(self._link(rel_path, target), "/" + rel_path)

    def test_an_absolute_link_is_not_resolved_through_the_host(self):
        # The host has its own /etc/alternatives and /tmp; neither may leak in.
        self._link("etc/alternatives/awk", "/usr/bin/mawk")

        self.assertEqual(
            self._target("usr/bin/awk", "/etc/alternatives/awk"), "/etc/alternatives/awk"
        )
        self.assertEqual(self._target("var/tmp-link", "/tmp"), "/tmp")

    def test_a_link_through_a_host_usrmerge_path_keeps_its_text(self):
        loader = "/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"

        self.assertEqual(self._target("lib64/ld-linux-x86-64.so.2", loader), loader)

    def test_a_relative_link_keeps_its_text(self):
        os.makedirs(os.path.join(self.root, "usr/bin"))

        self.assertEqual(
            self._target("usr/bin/nawk", "../../etc/alternatives/nawk"),
            "../../etc/alternatives/nawk",
        )

    def test_a_dangling_link_keeps_its_text(self):

        self.assertEqual(
            self._target("usr/bin/pager", "/etc/alternatives/pager"),
            "/etc/alternatives/pager",
        )

    def test_a_link_with_dot_dot_that_stays_inside_keeps_its_text(self):
        self.assertEqual(
            self._target("usr/lib/x/libc.so", "../../../lib/libc.so"), "../../../lib/libc.so"
        )
        self.assertEqual(self._target("a/b", "/usr/../etc/passwd"), "/usr/../etc/passwd")

    def test_a_relative_link_that_climbs_above_the_root_stops_the_pack(self):
        with self.assertRaisesRegex(ImageLinkEscapeError, "outside the image root"):
            self._target("usr/bin/evil", "../../../etc/passwd")

    def test_a_link_at_the_root_that_climbs_stops_the_pack(self):
        with self.assertRaises(ImageLinkEscapeError):
            self._target("evil", "../host")

    def test_an_absolute_link_that_climbs_above_the_root_stops_the_pack(self):
        with self.assertRaises(ImageLinkEscapeError):
            self._target("usr/bin/evil", "/../etc/passwd")
        with self.assertRaises(ImageLinkEscapeError):
            self._target("usr/bin/evil2", "/usr/../../etc/passwd")


if __name__ == "__main__":
    unittest.main()
