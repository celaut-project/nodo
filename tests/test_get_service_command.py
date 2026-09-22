"""`nodo get <service_id> [--now]` -- ask the network for a service not held locally.

Queued mode never talks to the network: it only has to prove it dropped the right
marker file in `maintain.WANTED_INBOX_DIR` for the running daemon to pick up.
`--now` is `check_wanted_service` run inline, so its own peer-loop behaviour is
already covered by `test_get_service_block_skip_e2e.py`; what's specific here is
that this command reports whether the fetch actually landed the service.

`get_id` is stubbed out everywhere but the one test about it: it walks
`METADATA_REGISTRY` on disk, which `config_bootstrap`'s temp config never
creates, and every other test here passes an id, not a tag, so there is nothing
for it to resolve anyway.
"""
import os
import unittest
from unittest.mock import patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from src.commands import get_service as get_service_cmd
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    get_service_cmd = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class GetServiceCommandTests(unittest.TestCase):
    HASH = "ab" * 32

    def test_a_service_already_local_is_left_alone(self):
        with patch.object(
            get_service_cmd, "resolve_service_hash", return_value=self.HASH
        ), patch.object(get_service_cmd, "check_wanted_service") as mock_check:
            get_service_cmd.get_service(self.HASH)

        mock_check.assert_not_called()

    def test_a_bad_id_is_refused_before_touching_the_network_or_the_inbox(self):
        with patch.object(
            get_service_cmd, "get_id", return_value=""
        ), patch.object(
            get_service_cmd, "resolve_service_hash", return_value=""
        ), patch.object(
            get_service_cmd, "check_wanted_service"
        ) as mock_check:
            get_service_cmd.get_service("not-a-hex-hash")

        mock_check.assert_not_called()

    def test_queuing_drops_exactly_one_marker_file_named_after_the_hash(self):
        with patch.object(get_service_cmd, "get_id", return_value=""), patch.object(
            get_service_cmd, "resolve_service_hash", return_value=""
        ):
            get_service_cmd.get_service(self.HASH)

        try:
            self.assertEqual(
                os.listdir(get_service_cmd.WANTED_INBOX_DIR), [self.HASH]
            )
        finally:
            for name in os.listdir(get_service_cmd.WANTED_INBOX_DIR):
                os.remove(os.path.join(get_service_cmd.WANTED_INBOX_DIR, name))

    def test_now_asks_the_peer_loop_directly(self):
        with patch.object(get_service_cmd, "get_id", return_value=""), patch.object(
            get_service_cmd, "resolve_service_hash", side_effect=["", self.HASH]
        ), patch.object(get_service_cmd, "check_wanted_service") as mock_check:
            get_service_cmd.get_service(self.HASH, now=True)

        mock_check.assert_called_once_with(self.HASH)

    def test_now_reports_failure_when_no_peer_had_it(self):
        printed = []
        with patch.object(get_service_cmd, "get_id", return_value=""), patch.object(
            get_service_cmd, "resolve_service_hash", return_value=""
        ), patch.object(get_service_cmd, "check_wanted_service"), patch(
            "builtins.print", printed.append
        ):
            get_service_cmd.get_service(self.HASH, now=True)

        self.assertTrue(any("Could not get" in line for line in printed))

    def test_a_tag_the_local_registry_already_names_is_resolved_before_fetching(self):
        with patch.object(
            get_service_cmd, "get_id", return_value=self.HASH
        ), patch.object(
            get_service_cmd, "resolve_service_hash", return_value=""
        ), patch.object(get_service_cmd, "check_wanted_service") as mock_check:
            get_service_cmd.get_service("my-tag", now=True)

        mock_check.assert_called_once_with(self.HASH)


if __name__ == "__main__":
    unittest.main()
