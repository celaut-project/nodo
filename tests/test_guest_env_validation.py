"""Which declared environment variables also reach a guest's real Linux
environment (#405), on top of the unconditional __config__ delivery.

src/utils/guest_env.py is the single point deciding this, so it is tested on
its own rather than through a full CH/QEMU launch.
"""
import unittest

from src.utils import guest_env


class LinuxEnvDeliveryTests(unittest.TestCase):
    def test_a_well_formed_variable_is_kept(self):
        kept = guest_env.linux_env_vars({"MY_ENV_VAR": b"some-value"})
        self.assertEqual(kept, {"MY_ENV_VAR": b"some-value"})

    def test_several_well_formed_variables_are_all_kept(self):
        kept = guest_env.linux_env_vars({"A": b"1", "B_2": b"two", "_C": b""})
        self.assertEqual(kept, {"A": b"1", "B_2": b"two", "_C": b""})

    def test_a_name_starting_with_a_digit_is_dropped(self):
        # Not a legal identifier: this is what execute.py's export would choke on.
        kept = guest_env.linux_env_vars({"2FAST": b"x"})
        self.assertEqual(kept, {})

    def test_a_name_carrying_shell_metacharacters_is_dropped(self):
        # The name is interpolated literally into `export "$name=$value"` in
        # bash/build_ch_initramfs.sh; nothing outside the identifier shape is safe
        # to hand that interpolation, so it never reaches the guest env at all.
        for name in ("FOO BAR", "FOO=BAR", "FOO;BAR", "$(id)", "FOO$BAR", "FOO-BAR"):
            with self.subTest(name=name):
                kept = guest_env.linux_env_vars({name: b"x"})
                self.assertEqual(kept, {})

    def test_reserved_linker_names_are_dropped_even_though_the_shape_is_legal(self):
        for name in ("LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT"):
            with self.subTest(name=name):
                kept = guest_env.linux_env_vars({name: b"/tmp/evil.so"})
                self.assertEqual(kept, {})

    def test_a_value_with_an_embedded_nul_byte_is_dropped(self):
        # A Linux env var is a NUL-terminated C string at the execve() level; a
        # value that already contains one would silently truncate, disagreeing
        # with what __config__ carries. Rejecting it beats delivering it wrong.
        kept = guest_env.linux_env_vars({"FOO": b"abc\x00def"})
        self.assertEqual(kept, {})

    def test_a_value_over_the_size_ceiling_is_dropped(self):
        oversized = b"x" * (guest_env.MAX_VALUE_BYTES + 1)
        kept = guest_env.linux_env_vars({"FOO": oversized})
        self.assertEqual(kept, {})

    def test_a_value_exactly_at_the_size_ceiling_is_kept(self):
        exact = b"x" * guest_env.MAX_VALUE_BYTES
        kept = guest_env.linux_env_vars({"FOO": exact})
        self.assertEqual(kept, {"FOO": exact})

    def test_one_bad_variable_does_not_drop_the_others(self):
        kept = guest_env.linux_env_vars(
            {
                "GOOD": b"kept",
                "2BAD": b"dropped: illegal name",
                "LD_PRELOAD": b"dropped: reserved",
                "ALSO_GOOD": b"kept too",
            }
        )
        self.assertEqual(kept, {"GOOD": b"kept", "ALSO_GOOD": b"kept too"})

    def test_an_empty_map_yields_an_empty_result(self):
        self.assertEqual(guest_env.linux_env_vars({}), {})


if __name__ == "__main__":
    unittest.main()
