"""The one hole nodo punches in a table it does not own.

nodo accepts guest traffic in ``inet nodo`` at priority -5, and that settles
nothing: ``accept`` ends its own chain, and Docker's ``ip filter FORWARD`` policy
DROP (or ufw's default forward policy) still discards the packet. A parent then
cannot reach the child it just launched, and the failure reads as a dead child.

What is tested here is mostly restraint: that nodo leaves a clean host alone, that
what it writes is a named chain it can take back rather than loose accepts, that
the jump goes in last and comes out first, and that none of it is ever fatal.
"""
import subprocess
import unittest
from unittest.mock import patch

from src.utils.firewall import compat, policy
from src.utils.firewall.backends import COMPAT_CHAIN, ForeignRejector, RejectorScan
from src.utils.firewall.compat import CompatMode, compat_state, ensure_compat, remove_compat

BRIDGE = "nodo-br-ch"


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class FakeIptables:
    """A host whose FORWARD chain and NODO_FWD chain are whatever the test says."""

    def __init__(self, chain_exists=False, forward="", compat_chain="", fail_on=None):
        self.chain_exists = chain_exists
        self.forward = forward
        self.compat_chain = compat_chain
        self.fail_on = fail_on or ()
        self.calls = []

    def __call__(self, command):
        command = [str(part) for part in command]
        self.calls.append(command)
        line = " ".join(command)
        for needle in self.fail_on:
            if needle in line:
                return _proc(1, "", "iptables: Operation not permitted")
        if command[:3] == ["iptables", "-L", COMPAT_CHAIN]:
            return _proc(0 if self.chain_exists else 1, "", "No chain/target/match by that name")
        if command[:3] == ["iptables", "-S", "FORWARD"]:
            return _proc(0, self.forward)
        if command[:3] == ["iptables", "-S", COMPAT_CHAIN]:
            if not self.chain_exists:
                return _proc(1, "", "No chain/target/match by that name")
            return _proc(0, self.compat_chain)
        if command[:2] == ["iptables", "-N"]:
            self.chain_exists = True
        return _proc(0)

    def commands(self):
        return [" ".join(call) for call in self.calls]

    def matching(self, *needles):
        return [line for line in self.commands() if all(n in line for n in needles)]

    def index_of(self, *needles):
        for position, line in enumerate(self.commands()):
            if all(n in line for n in needles):
                return position
        return -1


def _applied_chain(bridge=BRIDGE):
    """What NODO_FWD looks like once nodo has filled it, in iptables -S form."""
    from src.utils.firewall.backends import _render_iptables

    return "\n".join(
        f"-A {COMPAT_CHAIN} " + " ".join(_render_iptables(rule))
        for rule in policy.compat_rules(bridge)
    )


def _applied_jump():
    return f"-A FORWARD -j {COMPAT_CHAIN} -m comment --comment {policy.COMPAT_JUMP_COMMENT}"


def _scan(*rejectors, readable=True, reason=""):
    return RejectorScan(rejectors=tuple(rejectors), readable=readable, reason=reason, hook="forward")


DOCKER = ForeignRejector(
    table="ip filter", chain="FORWARD", priority=0, reason="chain policy is drop", hook="forward"
)


@patch("src.utils.firewall.compat.shutil.which", return_value="/usr/sbin/iptables")
class AutoModeTests(unittest.TestCase):
    """``auto`` looks before it writes. A clean host keeps a clean FORWARD chain."""

    def _ensure(self, runner, scan):
        with patch("src.utils.firewall.compat.detect_backend") as detect:
            detect.return_value.foreign_forward_rejectors.return_value = scan
            return ensure_compat(BRIDGE, CompatMode.AUTO, run=runner)

    def test_a_clear_forward_hook_is_left_completely_alone(self, _which):
        runner = FakeIptables()
        state = self._ensure(runner, _scan())

        self.assertIs(state.needed, False)
        self.assertFalse(state.changed)
        self.assertEqual(runner.matching("iptables -N"), [])
        self.assertEqual(runner.matching("iptables -I"), [])
        self.assertIn("Leaving the compatibility table alone", state.detail)

    def test_a_foreign_drop_is_what_makes_it_write(self, _which):
        runner = FakeIptables()
        state = self._ensure(runner, _scan(DOCKER))

        self.assertIs(state.needed, True)
        self.assertEqual(len(runner.matching(f"-N {COMPAT_CHAIN}")), 1)
        self.assertEqual(len(runner.matching(f"-A {COMPAT_CHAIN}")), 4)

    def test_a_ruleset_nobody_could_read_is_not_taken_as_clean(self, _which):
        # The safe side is four visible lines an operator can delete, not a node
        # whose services silently cannot reach their dependencies.
        runner = FakeIptables()
        state = self._ensure(runner, _scan(readable=False, reason="nft failed"))

        self.assertIsNone(state.needed)
        self.assertEqual(len(runner.matching(f"-N {COMPAT_CHAIN}")), 1)


@patch("src.utils.firewall.compat.shutil.which", return_value="/usr/sbin/iptables")
class ApplyTests(unittest.TestCase):
    def test_what_it_writes_is_a_named_chain_and_a_jump_to_it(self, _which):
        runner = FakeIptables()
        ensure_compat(BRIDGE, CompatMode.ON, run=runner)

        self.assertEqual(len(runner.matching(f"-N {COMPAT_CHAIN}")), 1)
        self.assertEqual(len(runner.matching("-I FORWARD", f"-j {COMPAT_CHAIN}")), 1)
        # Nothing loose in FORWARD besides the jump: that is the whole point of
        # having a chain, so that "what did nodo add" has a one-line answer.
        self.assertEqual(runner.matching("-A FORWARD"), [])

    def test_the_jump_goes_in_last_so_the_chain_is_never_reachable_half_filled(self, _which):
        runner = FakeIptables()
        ensure_compat(BRIDGE, CompatMode.ON, run=runner)

        last_rule = max(runner.index_of(f"-A {COMPAT_CHAIN}", suffix) for suffix in
                        ("guests", "egress", "replies", "published"))
        self.assertGreater(runner.index_of("-I FORWARD", f"-j {COMPAT_CHAIN}"), last_rule)

    def test_the_four_paths_are_each_asked_for_by_name(self, _which):
        runner = FakeIptables()
        ensure_compat(BRIDGE, CompatMode.ON, run=runner)

        written = " ".join(runner.matching(f"-A {COMPAT_CHAIN}"))
        self.assertIn(f"-i {BRIDGE} -o {BRIDGE} -j ACCEPT", written)          # parent -> child
        self.assertIn(f"-i {BRIDGE} ! -o {BRIDGE} -j ACCEPT", written)        # guest egress
        self.assertIn("--ctstate RELATED,ESTABLISHED", written)               # replies
        self.assertIn("--ctstate DNAT", written)                              # published ports

    def test_inbound_to_a_guest_is_never_opened_unconditionally(self, _which):
        # A bare '-o <bridge> -j ACCEPT' would accept unsolicited inbound to every
        # guest. nodo does not need that path, so it does not ask for it.
        runner = FakeIptables()
        ensure_compat(BRIDGE, CompatMode.ON, run=runner)

        for line in runner.matching(f"-A {COMPAT_CHAIN}"):
            if f"-o {BRIDGE}" in line and f"-i {BRIDGE}" not in line:
                self.assertIn("--ctstate", line, line)

    def test_a_second_run_changes_nothing(self, _which):
        runner = FakeIptables(chain_exists=True, forward=_applied_jump(), compat_chain=_applied_chain())
        state = ensure_compat(BRIDGE, CompatMode.ON, run=runner)

        self.assertFalse(state.changed)
        self.assertTrue(state.complete)
        self.assertEqual(runner.matching("iptables -N"), [])
        self.assertEqual(runner.matching("iptables -A"), [])

    def test_off_writes_nothing_at_all(self, _which):
        runner = FakeIptables()
        state = ensure_compat(BRIDGE, CompatMode.OFF, run=runner)

        self.assertEqual(runner.calls, [])
        self.assertIn("FORWARD_COMPAT is off", state.detail)

    def test_a_failure_is_reported_and_never_raised(self, _which):
        # A node that cannot write here still boots guests. What it must not do is
        # boot them while claiming the network is fine.
        runner = FakeIptables(fail_on=("-N NODO_FWD",))
        state = ensure_compat(BRIDGE, CompatMode.ON, run=runner)

        self.assertIsNotNone(state.error)
        self.assertFalse(state.complete)
        self.assertIn("nodo doctor", state.detail)

    def test_a_host_without_iptables_has_nothing_to_compensate_for(self, _which):
        with patch("src.utils.firewall.compat.shutil.which", return_value=None):
            runner = FakeIptables()
            state = ensure_compat(BRIDGE, CompatMode.ON, run=runner)

        self.assertFalse(state.available)
        self.assertEqual(runner.calls, [])


@patch("src.utils.firewall.compat.shutil.which", return_value="/usr/sbin/iptables")
class RemoveTests(unittest.TestCase):
    def test_the_jump_comes_out_before_the_chain(self, _which):
        # iptables refuses to delete a referenced chain; doing it the other way
        # round leaves a jump to a chain that accepts nothing, under nodo's name.
        runner = FakeIptables(chain_exists=True, forward=_applied_jump(), compat_chain=_applied_chain())
        state = remove_compat(BRIDGE, run=runner)

        self.assertTrue(state.changed)
        self.assertLess(
            runner.index_of("-D FORWARD", COMPAT_CHAIN),
            runner.index_of(f"-X {COMPAT_CHAIN}"),
        )

    def test_the_chain_is_flushed_and_deleted_not_just_emptied(self, _which):
        runner = FakeIptables(chain_exists=True, forward=_applied_jump(), compat_chain=_applied_chain())
        remove_compat(BRIDGE, run=runner)

        self.assertEqual(len(runner.matching(f"-F {COMPAT_CHAIN}")), 1)
        self.assertEqual(len(runner.matching(f"-X {COMPAT_CHAIN}")), 1)

    def test_removing_what_was_never_there_is_not_an_error(self, _which):
        runner = FakeIptables()
        state = remove_compat(BRIDGE, run=runner)

        self.assertFalse(state.changed)
        self.assertIsNone(state.error)
        self.assertIn("nothing of nodo's to remove", state.detail)


@patch("src.utils.firewall.compat.shutil.which", return_value="/usr/sbin/iptables")
class StateTests(unittest.TestCase):
    def test_a_half_applied_chain_reads_as_partial_not_as_absent(self, _which):
        # The state worth shouting about: the jump is in, so FORWARD sends guest
        # traffic to a chain that does not accept it.
        runner = FakeIptables(chain_exists=True, forward=_applied_jump(), compat_chain="")
        state = compat_state(BRIDGE, CompatMode.AUTO, run=runner)

        self.assertFalse(state.complete)
        self.assertTrue(state.partial)
        self.assertIn("half there", state.detail)

    def test_a_complete_chain_says_so(self, _which):
        runner = FakeIptables(chain_exists=True, forward=_applied_jump(), compat_chain=_applied_chain())
        state = compat_state(BRIDGE, CompatMode.AUTO, run=runner)

        self.assertTrue(state.complete)
        self.assertFalse(state.partial)

    def test_an_untouched_host_is_neither_complete_nor_partial(self, _which):
        runner = FakeIptables()
        state = compat_state(BRIDGE, CompatMode.AUTO, run=runner)

        self.assertFalse(state.complete)
        self.assertFalse(state.partial)
        self.assertIn("written nothing", state.detail)

    def test_reading_state_writes_nothing(self, _which):
        runner = FakeIptables(chain_exists=True, forward=_applied_jump(), compat_chain=_applied_chain())
        compat_state(BRIDGE, CompatMode.AUTO, run=runner)

        for line in runner.commands():
            self.assertNotRegex(line, r" -[NAIDFX] ")


class ModeTests(unittest.TestCase):
    def test_the_default_is_auto(self):
        self.assertIs(CompatMode.parse(None), CompatMode.AUTO)
        self.assertIs(CompatMode.parse(""), CompatMode.AUTO)

    def test_it_is_case_insensitive_and_trimmed(self):
        self.assertIs(CompatMode.parse("  OFF "), CompatMode.OFF)

    def test_an_unknown_value_names_the_ones_that_work(self):
        with self.assertRaises(ValueError) as raised:
            CompatMode.parse("yes")
        self.assertIn("auto, on, off", str(raised.exception))


class CoverageHonestyTests(unittest.TestCase):
    def test_the_module_says_what_it_cannot_reach(self):
        # Kept honest by a test, because the failure it prevents is the expensive
        # kind: an operator on Fedora reading "compatibility rules applied" and
        # concluding the forward hook is handled. It is not -- firewalld owns a
        # native nftables table that nothing in ip filter can override.
        self.assertIn("firewalld", compat.__doc__)
        self.assertIn("not** reach", compat.__doc__)

    def test_doctor_sends_a_fedora_host_to_firewalld_rather_than_to_nodo(self):
        import inspect

        from src.commands import doctor

        source = inspect.getsource(doctor._doctor_guest_to_guest_forwarding)
        self.assertIn("firewalld", source)
        self.assertIn("firewall-cmd", source)


if __name__ == "__main__":
    unittest.main()
