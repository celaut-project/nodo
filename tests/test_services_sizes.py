"""``nodo services`` and the TUI's SERVICES table say one size the same way (#438).

They showed the same two figures -- what a service's own directory stores, and
what it weighs with the shared blocks it names -- but not the same way: the CLI
printed MiB labelled "MB", always in MB with two decimals, so a 394-byte
directory read "0.00 MB" here and "394 B" in the TUI, and 39.7 MiB read
"39.70 MB". The cases below are the same ones the TUI's own test pins
(`sizes_read_as_nodo_services_prints_them` in src/commands/tui/src/app.rs).
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tests.config_bootstrap import load_example_config

load_example_config()

from src.commands import services  # noqa: E402


class FormatBytesTests(unittest.TestCase):
    def test_the_same_cases_as_the_tui(self):
        for size, text in [
            (0, "0 B"),
            (394, "394 B"),
            (1023, "1023 B"),
            (1024, "1.0 KiB"),
            (1536, "1.5 KiB"),
            (41_628_467, "39.7 MiB"),
            (5 * 1024 * 1024 * 1024, "5.0 GiB"),
        ]:
            self.assertEqual(services.format_bytes(size), text, size)


class ListServicesTests(unittest.TestCase):
    def test_both_figures_are_printed_in_the_tuis_units_and_words(self):
        service_id = "ab" * 32
        with tempfile.TemporaryDirectory() as tmp:
            registry = os.path.join(tmp, "registry")
            metadata = os.path.join(tmp, "metadata")
            service_dir = os.path.join(registry, service_id)
            os.makedirs(service_dir)
            os.makedirs(metadata)
            with open(os.path.join(service_dir, "0"), "wb") as f:
                f.write(b"\0" * 1500)
            with open(os.path.join(service_dir, "_.json"), "w") as f:
                json.dump([0], f)

            out = io.StringIO()
            with patch.object(services, "REGISTRY", registry), \
                 patch.object(services, "METADATA", metadata), \
                 redirect_stdout(out):
                services.list_services()

        line = out.getvalue().strip()
        self.assertTrue(line.startswith(service_id), line)
        # 1500 bytes of part; the directory also holds its 3-byte `_.json`.
        self.assertIn("1.5 KiB with blocks (1.5 KiB stored here)", line)
        self.assertNotIn(" MB", line)


if __name__ == "__main__":
    unittest.main()
