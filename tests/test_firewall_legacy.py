"""Sweeping up after the versions that wrote everything through iptables.

On a host where ``iptables`` is the nft shim, those rules sit in the ``ip
filter``/``ip nat`` compatibility tables -- somewhere nodo no longer looks. Left
after an upgrade they are duplicates at best and, in PREROUTING, a stale DNAT
still pointing a published port at a VM that no longer exists.
"""
import subprocess
import unittest

from src.utils.firewall.backends import IptablesBackend, NftBackend
from src.utils.firewall.legacy import (
    LEGACY_COMMENT_ROOT,
    SWEPT_CHAINS,
    sweep_compat_tables,
)
from src.utils.firewall.rules import Chain


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class SweepTests(unittest.TestCase):
    def setUp(self):
        self.calls = []

    def _run(self, command):
        command = list(command)
        self.calls.append(command)
        if "-S" in command:
            chain = command[-1]
            if chain == "PREROUTING":
                return _proc(
                    0,
                    '-A PREROUTING -p tcp --dport 59629 -j DNAT --to-destination 10.0.0.2:5000 '
                    '-m comment --comment "nodo;vm=dead;dnat;tcp;59629"\n',
                )
            if chain == "FORWARD":
                return _proc(
                    0,
                    '-A FORWARD -s 10.0.0.2 -j ACCEPT -m comment --comment "nodo;vm=dead;allow_all_egress"\n'
                    '-A FORWARD -j ACCEPT -m comment --comment "someone-elses-rule"\n',
                )
            return _proc(0, "")
        return _proc(0)

    def test_removes_nodo_rules_from_the_compatibility_tables(self):
        removed = sweep_compat_tables(NftBackend(run=self._run))

        self.assertEqual(removed, 2)
        deletes = [" ".join(c) for c in self.calls if "-D" in c]
        self.assertEqual(len(deletes), 2, deletes)
        self.assertTrue(any("DNAT" in d for d in deletes), deletes)

    def test_leaves_rules_it_does_not_own_alone(self):
        sweep_compat_tables(NftBackend(run=self._run))
        deletes = " ".join(" ".join(c) for c in self.calls if "-D" in c)
        self.assertNotIn("someone-elses-rule", deletes)

    def test_postrouting_is_never_swept(self):
        # The only rule nodo puts there is the subnet masquerade. A duplicate is
        # harmless -- a connection is NAT'd once -- but a gap in it would cut
        # outbound connectivity for every running instance.
        self.assertNotIn(Chain.POSTROUTING, SWEPT_CHAINS)
        self.assertIn(Chain.PREROUTING, SWEPT_CHAINS)
        sweep_compat_tables(NftBackend(run=self._run))
        listed = [c[-1] for c in self.calls if "-S" in c]
        self.assertNotIn("POSTROUTING", listed)

    def test_it_is_a_no_op_when_iptables_is_the_live_backend(self):
        # There the compatibility tables *are* the real rules.
        removed = sweep_compat_tables(IptablesBackend(run=self._run))
        self.assertEqual(removed, 0)
        self.assertEqual(self.calls, [])

    def test_an_unreadable_chain_does_not_stop_the_sweep(self):
        def run(command):
            command = list(command)
            self.calls.append(command)
            if "-S" in command and command[-1] == "INPUT":
                return _proc(1, "", "Permission denied")
            return self._run(command)

        removed = sweep_compat_tables(NftBackend(run=run))
        self.assertEqual(removed, 2)

    def test_the_prefix_is_the_one_nodo_actually_writes(self):
        self.assertEqual(LEGACY_COMMENT_ROOT, "nodo;")


if __name__ == "__main__":
    unittest.main()
