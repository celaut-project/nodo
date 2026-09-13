"""The two questions a node asks once, and the ways they must not be skipped.

The whole point of moving them out of `install.sh` is that an install is not reliably
a place where a question can be asked -- on Windows it is a pipe, and under
`Nodo-Setup.exe` there is no console at all. So what is pinned here is mostly about
*silence*: what happens when there is nobody to ask, when the answer is unusable, and
when the question has already been answered once.

The asymmetry between the two is the thing to keep:

* a declined **KyA** stops the node, because it is what running it is conditional on;
* the **donation share** never stops anything -- refusing is setting it to 0, and every
  failure path leaves the config alone and the node running.
"""
import os
import tempfile
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.commands import onboarding
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    onboarding = None  # type: ignore[assignment]


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class MarkerTests(unittest.TestCase):
    """Asked once, and never again."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.main = self._dir.name
        os.makedirs(os.path.join(self.main, "storage"), exist_ok=True)
        self.addCleanup(self._dir.cleanup)

    def _touch(self, name):
        open(os.path.join(self.main, "storage", name), "a").close()

    def test_an_accepted_kya_is_not_asked_again(self):
        self._touch(onboarding.KYA_MARKER)
        with mock.patch("subprocess.run") as run:
            self.assertTrue(onboarding.accept_kya(self.main))
        run.assert_not_called()

    def test_an_answered_donation_question_is_not_asked_again(self):
        self._touch(onboarding.DONATION_MARKER)
        with mock.patch("builtins.input") as ask:
            onboarding.ask_donation(self.main)
        ask.assert_not_called()

    def test_an_existing_install_is_not_nagged_about_donations(self):
        """The upgrade path, and the reason the two markers are separate.

        A node that accepted the KyA before this existed was already asked the
        donation question by `install.sh`. Seeing one marker and not the other means
        "upgrade", not "never asked" -- so it is recorded as answered rather than put
        to an operator whose node has been running for months.
        """
        self._touch(onboarding.KYA_MARKER)
        with mock.patch("builtins.input") as ask:
            onboarding.ask_donation(self.main)
        ask.assert_not_called()
        self.assertTrue(os.path.exists(os.path.join(self.main, "storage", onboarding.DONATION_MARKER)))

    def test_a_marker_that_cannot_be_written_is_not_fatal(self):
        """Asking twice is annoying. Refusing to start over a dotfile is worse."""
        with mock.patch("os.makedirs", side_effect=OSError("read-only")):
            self.assertFalse(onboarding._record(self.main, onboarding.KYA_MARKER))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class KyaTests(unittest.TestCase):
    """A refusal has to actually refuse."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.main = self._dir.name
        os.makedirs(os.path.join(self.main, "bash"), exist_ok=True)
        open(os.path.join(self.main, "bash", "accept_kya.sh"), "a").close()
        self.addCleanup(self._dir.cleanup)

    def test_declining_the_kya_stops_the_node(self):
        """The bug this replaces: `os.system` dropped the exit code on the floor, so
        answering "no" started the node exactly like answering "yes"."""
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1)):
            self.assertFalse(onboarding.accept_kya(self.main))

    def test_accepting_the_kya_continues(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)):
            self.assertTrue(onboarding.accept_kya(self.main))

    def test_a_declined_kya_is_never_recorded_as_accepted(self):
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=1)):
            self.assertFalse(onboarding.run(self.main))
        self.assertFalse(os.path.exists(os.path.join(self.main, "storage", onboarding.KYA_MARKER)))

    def test_a_missing_kya_script_does_not_lock_the_operator_out(self):
        """A broken install saying so is more use than a node exiting in silence."""
        os.remove(os.path.join(self.main, "bash", "accept_kya.sh"))
        self.assertTrue(onboarding.accept_kya(self.main))

    def test_the_donation_question_comes_after_the_kya(self):
        """Order, not decoration: asking someone to fund the project before they have
        agreed to run it is backwards, and a refused KyA leaves no node to fund."""
        calls = []
        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)), \
             mock.patch.object(onboarding, "accept_kya", side_effect=lambda d: calls.append("kya") or True), \
             mock.patch.object(onboarding, "ask_donation", side_effect=lambda d: calls.append("donation")):
            onboarding.run(self.main)
        self.assertEqual(calls, ["kya", "donation"])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ShareValidationTests(unittest.TestCase):
    """A share is a fraction of one, and a wrong one is refused rather than coerced."""

    def test_a_percentage_typed_as_a_whole_number_is_refused(self):
        """`2` meant as "2 %" would donate everything this node earns."""
        self.assertIsNone(onboarding.valid_share("2"))
        self.assertIsNone(onboarding.valid_share("100"))

    def test_a_negative_share_is_refused(self):
        self.assertIsNone(onboarding.valid_share("-0.5"))

    def test_nonsense_is_refused(self):
        for answer in ("", "   ", "abc", "0.5%", "nan"):
            self.assertIsNone(onboarding.valid_share(answer), answer)

    def test_the_ends_of_the_range_are_accepted(self):
        self.assertEqual(onboarding.valid_share("0"), "0")
        self.assertEqual(onboarding.valid_share("1"), "1")

    def test_ordinary_shares_are_accepted(self):
        self.assertEqual(onboarding.valid_share("0.02"), "0.02")
        self.assertEqual(onboarding.valid_share(" 0.05 "), "0.05")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DonationPromptTests(unittest.TestCase):

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.main = self._dir.name
        os.makedirs(os.path.join(self.main, "storage"), exist_ok=True)
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(lambda: os.environ.pop("NODO_DONATION_PERCENTAGE", None))

    def _marker_written(self):
        return os.path.exists(os.path.join(self.main, "storage", onboarding.DONATION_MARKER))

    def test_no_terminal_means_no_answer_and_no_marker(self):
        """The whole reason this moved out of the installer.

        Under `Nodo-Setup.exe` there is no console. Recording the question as asked
        there would bury the default exactly the way the installer's prompt did -- so
        nothing is written, and the next run on a real terminal asks.
        """
        with mock.patch.object(onboarding, "_interactive", return_value=False), \
             mock.patch.object(onboarding, "_write_share") as write:
            onboarding.ask_donation(self.main)
        write.assert_not_called()
        self.assertFalse(self._marker_written())

    def test_the_environment_answers_without_a_terminal(self):
        """The scripted path: a fleet install names the share and is never asked."""
        os.environ["NODO_DONATION_PERCENTAGE"] = "0"
        with mock.patch.object(onboarding, "_interactive", return_value=False), \
             mock.patch.object(onboarding, "_current_share", return_value="0.02"), \
             mock.patch.object(onboarding, "_write_share", return_value=True) as write:
            onboarding.ask_donation(self.main)
        write.assert_called_once_with(self.main, "0")
        self.assertTrue(self._marker_written())

    def test_an_empty_answer_keeps_the_suggested_share(self):
        with mock.patch.object(onboarding, "_interactive", return_value=True), \
             mock.patch.object(onboarding, "_current_share", return_value="0.02"), \
             mock.patch("builtins.input", return_value=""), \
             mock.patch.object(onboarding, "_write_share") as write:
            onboarding.ask_donation(self.main)
        # Nothing to write: the answer is what the file already says.
        write.assert_not_called()
        self.assertTrue(self._marker_written())

    def test_zero_is_one_keystroke_away(self):
        with mock.patch.object(onboarding, "_interactive", return_value=True), \
             mock.patch.object(onboarding, "_current_share", return_value="0.02"), \
             mock.patch("builtins.input", return_value="0"), \
             mock.patch.object(onboarding, "_write_share", return_value=True) as write:
            onboarding.ask_donation(self.main)
        write.assert_called_once_with(self.main, "0")

    def test_an_unusable_answer_keeps_the_suggested_share_and_says_so(self):
        with mock.patch.object(onboarding, "_interactive", return_value=True), \
             mock.patch.object(onboarding, "_current_share", return_value="0.02"), \
             mock.patch("builtins.input", return_value="2"), \
             mock.patch.object(onboarding, "_write_share") as write:
            onboarding.ask_donation(self.main)
        write.assert_not_called()
        self.assertTrue(self._marker_written())

    def test_ctrl_c_is_not_consent(self):
        """Interrupting the question leaves it unanswered, so it is asked again."""
        with mock.patch.object(onboarding, "_interactive", return_value=True), \
             mock.patch.object(onboarding, "_current_share", return_value="0.02"), \
             mock.patch("builtins.input", side_effect=KeyboardInterrupt), \
             mock.patch.object(onboarding, "_write_share") as write:
            onboarding.ask_donation(self.main)
        write.assert_not_called()
        self.assertFalse(self._marker_written())

    def test_a_failed_write_is_not_recorded_as_answered(self):
        with mock.patch.object(onboarding, "_interactive", return_value=True), \
             mock.patch.object(onboarding, "_current_share", return_value="0.02"), \
             mock.patch("builtins.input", return_value="0.05"), \
             mock.patch.object(onboarding, "_write_share", return_value=False):
            onboarding.ask_donation(self.main)
        self.assertFalse(self._marker_written())

    def test_the_share_is_shown_before_it_is_accepted(self):
        """"A default nobody is told about is not consent" -- so the number appears."""
        with mock.patch.object(onboarding, "_interactive", return_value=True), \
             mock.patch.object(onboarding, "_current_share", return_value="0.02"), \
             mock.patch("builtins.input", return_value="") as ask, \
             mock.patch("builtins.print"):
            onboarding.ask_donation(self.main)
        self.assertIn("0.02", ask.call_args[0][0])


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConfigWriteTests(unittest.TestCase):
    """config.yaml is a file operators read. Writing it must not rewrite it."""

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.main = self._dir.name
        self.addCleanup(self._dir.cleanup)

    def test_the_value_never_reaches_the_yq_expression(self):
        """Through the environment, like the TUI's config editor: nothing typed at the
        prompt can be read as yq syntax."""
        os.makedirs(os.path.join(self.main, "bin"), exist_ok=True)
        yq = os.path.join(self.main, "bin", "yq")
        open(yq, "a").close()
        os.chmod(yq, 0o755)
        open(os.path.join(self.main, "config.yaml"), "a").close()

        with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as run:
            onboarding._write_share(self.main, '0.5" | .x = "pwned')

        argv, kwargs = run.call_args[0][0], run.call_args[1]
        self.assertIn("strenv(NODO_SHARE)", " ".join(argv))
        self.assertNotIn("pwned", " ".join(argv))
        self.assertEqual(kwargs["env"]["NODO_SHARE"], '0.5" | .x = "pwned')

    def test_a_missing_yq_is_reported_rather_than_guessed_at(self):
        self.assertFalse(onboarding._write_share(self.main, "0.02"))

    def test_a_missing_yq_falls_back_to_the_documented_default(self):
        self.assertEqual(onboarding._current_share(self.main), onboarding.FALLBACK_SHARE)


if __name__ == "__main__":
    unittest.main()
