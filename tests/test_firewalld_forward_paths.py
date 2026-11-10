"""firewalld and the two forwarded guest paths (#534, #624, #639).

nodo's accepts end only their own chain; ``inet firewalld filter_FORWARD`` then
rejects what firewalld's zones and policies do not allow. Guest to guest needs the
bridge in a zone that forwards; guest to the Internet needs a policy from that zone
to the uplink's. These pin how the configuration is read (``assess``), what the
installer writes (``apply``), what the daemon and the launch log say, and how the
egress probe reads a reject.

The fake ``firewall-cmd`` answers come from firewalld 1.1.1 on Ubuntu 22.04, the
version the fix was tested against on a real node.
"""

import subprocess
import unittest
from unittest import mock

from src.utils.firewall import compat
from src.utils.firewall import firewalld_bridge as fwd
from src.utils.firewall import reachability
from src.utils.firewall.backends import ForeignRejector, RejectorScan

BRIDGE = "nodo-br-ch"


def _proc(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _zone_info(name, interfaces="", forward="yes", target="default"):
    return (
        f"{name} (active)\n  target: {target}\n  icmp-block-inversion: no\n"
        f"  interfaces: {interfaces}\n  sources: \n  services: ssh dhcpv6-client\n"
        f"  ports: \n  protocols: \n  forward: {forward}\n  masquerade: no\n"
    )


def _policy_info(name, ingress, egress, target="ACCEPT"):
    return (
        f"{name} (active)\n  priority: -1\n  target: {target}\n"
        f"  ingress-zones: {ingress}\n  egress-zones: {egress}\n  services: \n"
    )


class FakeFirewalld:
    """A firewall-cmd (and ``ip route``) with a configuration, read and written."""

    def __init__(self, *, running=True, default="public", interfaces=None, zones=None,
                 policies=None, routes="default via 10.0.0.1 dev host0 proto static\n",
                 has_policies=True):
        self.running = running
        self.default = default
        self.interfaces = dict(interfaces or {"host0": "public"})
        self.zones = {name: dict(info) for name, info in (zones or {
            "public": {"forward": "yes", "target": "default", "ports": set()},
        }).items()}
        self.policies = {name: dict(info) for name, info in (policies or {}).items()}
        self.routes = routes
        self.has_policies = has_policies
        self.calls = []

    def __call__(self, command):
        command = list(command)
        self.calls.append(command)
        if command[:2] == ["ip", "-4"]:
            return _proc(0, self.routes)
        args = [a for a in command[1:] if a != "--permanent"]
        if args == ["--state"]:
            return _proc(0, "running\n") if self.running else _proc(252, "not running\n")
        if args == ["--get-default-zone"]:
            return _proc(0, self.default + "\n")
        if args == ["--reload"]:
            return _proc(0, "success\n")
        if args == ["--get-policies"]:
            if not self.has_policies:
                return _proc(2, "", "usage error")
            return _proc(0, " ".join(sorted(self.policies)) + "\n")
        flag, _, value = args[0].partition("=")
        if flag == "--get-zone-of-interface":
            zone = self.interfaces.get(value)
            return _proc(0, zone + "\n") if zone else _proc(2, "", "no zone")
        if flag == "--info-zone":
            if value not in self.zones:
                return _proc(112, "", "INVALID_ZONE")
            z = self.zones[value]
            ifaces = " ".join(i for i, zn in self.interfaces.items() if zn == value)
            return _proc(0, _zone_info(value, ifaces, z["forward"], z["target"]))
        if flag == "--new-zone":
            self.zones[value] = {"forward": "no", "target": "default", "ports": set()}
            return _proc(0, "success")
        if flag == "--info-policy":
            if value not in self.policies:
                return _proc(112, "", "INVALID_POLICY")
            p = self.policies[value]
            return _proc(0, _policy_info(value, " ".join(p["in"]), " ".join(p["out"]), p["target"]))
        if flag == "--new-policy":
            self.policies[value] = {"in": [], "out": [], "target": "CONTINUE"}
            return _proc(0, "success")
        if flag == "--zone":
            zone = self.zones[value]
            op, _, arg = args[1].partition("=")
            if op == "--query-interface":
                return _proc(0 if self.interfaces.get(arg) == value else 1)
            if op == "--change-interface":
                self.interfaces[arg] = value
                return _proc(0, "success")
            if op == "--query-port":
                return _proc(0 if arg in zone["ports"] else 1)
            if op == "--add-port":
                zone["ports"].add(arg)
                return _proc(0, "success")
            if op == "--query-forward":
                return _proc(0 if zone["forward"] == "yes" else 1)
            if op == "--add-forward":
                zone["forward"] = "yes"
                return _proc(0, "success")
        if flag == "--policy":
            policy = self.policies[value]
            op, _, arg = args[1].partition("=")
            if op == "--query-ingress-zone":
                return _proc(0 if arg in policy["in"] else 1)
            if op == "--add-ingress-zone":
                policy["in"].append(arg)
                return _proc(0, "success")
            if op == "--query-egress-zone":
                return _proc(0 if arg in policy["out"] else 1)
            if op == "--add-egress-zone":
                policy["out"].append(arg)
                return _proc(0, "success")
            if op == "--set-target":
                policy["target"] = arg
                return _proc(0, "success")
        raise AssertionError(f"unexpected command {command}")

    def writes(self):
        reads = ("--state", "--get-", "--info-", "--query-")
        return [
            c for c in self.calls
            if c[0] == "firewall-cmd" and not any(a.startswith(reads) for a in c[1:])
        ]


def _nodo_zone(forward="yes", ports=()):
    return {"forward": forward, "target": "default", "ports": set(ports)}


def _public():
    return {"forward": "yes", "target": "default", "ports": set()}


class AssessTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(fwd.shutil, "which", return_value="/usr/bin/firewall-cmd")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_issue_534_unzoned_bridge_reaches_the_internet_but_not_another_guest(self):
        # The default zone's forward rule names host0 only, so out-to-host0 passes
        # and out-to-the-bridge falls to the reject (seen in filter_FWD_public_allow).
        verdict = fwd.assess(BRIDGE, run=FakeFirewalld())
        self.assertTrue(verdict.unzoned)
        self.assertIs(verdict.guest_to_guest, False)
        self.assertIs(verdict.egress, True)
        self.assertTrue(verdict.blocked)
        self.assertIn("no firewalld zone", " ".join(verdict.problems(BRIDGE)))

    def test_issue_639_docs_zone_with_forward_but_no_policy_cuts_the_internet(self):
        fake = FakeFirewalld(
            interfaces={"host0": "public", BRIDGE: "nodo"},
            zones={"public": _public(), "nodo": _nodo_zone()},
        )
        verdict = fwd.assess(BRIDGE, run=fake)
        self.assertIs(verdict.guest_to_guest, True)
        self.assertIs(verdict.egress, False)
        self.assertEqual(verdict.uplink_zones, ("public",))
        problems = " ".join(verdict.problems(BRIDGE))
        self.assertIn("no firewalld policy", problems)
        self.assertIn("'*'", problems)

    def test_zone_plus_egress_policy_to_any_opens_both(self):
        fake = FakeFirewalld(
            interfaces={"host0": "public", BRIDGE: "nodo"},
            zones={"public": _public(), "nodo": _nodo_zone()},
            policies={"nodo-egress": {"in": ["nodo"], "out": ["ANY"], "target": "ACCEPT"}},
        )
        verdict = fwd.assess(BRIDGE, run=fake)
        self.assertEqual((verdict.guest_to_guest, verdict.egress), (True, True))
        self.assertFalse(verdict.blocked)
        self.assertTrue(verdict.conclusive)
        self.assertEqual(verdict.egress_policy, "nodo-egress")

    def test_a_policy_to_the_uplink_zone_by_name_counts(self):
        fake = FakeFirewalld(
            interfaces={"host0": "FedoraServer", BRIDGE: "nodo"},
            default="FedoraServer",
            zones={"FedoraServer": _public(), "nodo": _nodo_zone()},
            policies={"p": {"in": ["nodo"], "out": ["FedoraServer"], "target": "ACCEPT"}},
        )
        self.assertIs(fwd.assess(BRIDGE, run=fake).egress, True)

    def test_a_policy_that_does_not_accept_does_not_count(self):
        fake = FakeFirewalld(
            interfaces={"host0": "public", BRIDGE: "nodo"},
            zones={"public": _public(), "nodo": _nodo_zone()},
            policies={"p": {"in": ["nodo"], "out": ["ANY"], "target": "CONTINUE"}},
        )
        self.assertIs(fwd.assess(BRIDGE, run=fake).egress, False)

    def test_a_zone_without_forward_blocks_guest_to_guest(self):
        fake = FakeFirewalld(
            interfaces={"host0": "public", BRIDGE: "nodo"},
            zones={"public": _public(), "nodo": _nodo_zone(forward="no")},
        )
        self.assertIs(fwd.assess(BRIDGE, run=fake).guest_to_guest, False)

    def test_the_policy_to_any_also_covers_guest_to_guest_without_forward(self):
        fake = FakeFirewalld(
            interfaces={"host0": "public", BRIDGE: "nodo"},
            zones={"public": _public(), "nodo": _nodo_zone(forward="no")},
            policies={"nodo-egress": {"in": ["nodo"], "out": ["ANY"], "target": "ACCEPT"}},
        )
        verdict = fwd.assess(BRIDGE, run=fake)
        self.assertEqual((verdict.guest_to_guest, verdict.egress), (True, True))

    def test_trusted_accepts_both(self):
        fake = FakeFirewalld(
            interfaces={"host0": "public", BRIDGE: "trusted"},
            zones={"public": _public(),
                   "trusted": {"forward": "yes", "target": "ACCEPT", "ports": set()}},
        )
        verdict = fwd.assess(BRIDGE, run=fake)
        self.assertEqual((verdict.guest_to_guest, verdict.egress), (True, True))

    def test_no_firewalld_is_nothing_to_report(self):
        verdict = fwd.assess(BRIDGE, run=FakeFirewalld(running=False))
        self.assertFalse(verdict.running)
        self.assertFalse(verdict.blocked)
        self.assertEqual(verdict.problems(BRIDGE), [])

    def test_an_unreadable_zone_is_never_blocked(self):
        def run(command):
            if command[1] == "--state":
                return _proc(0, "running")
            return _proc(1, "", "Authorization failed")

        verdict = fwd.assess(BRIDGE, run=run)
        self.assertFalse(verdict.blocked)
        self.assertFalse(verdict.conclusive)

    def test_no_default_route_leaves_egress_unknown(self):
        fake = FakeFirewalld(
            interfaces={BRIDGE: "nodo"}, zones={"public": _public(), "nodo": _nodo_zone()},
            routes="",
        )
        verdict = fwd.assess(BRIDGE, run=fake)
        self.assertIsNone(verdict.egress)
        self.assertFalse(verdict.blocked)

    def test_a_firewalld_without_policies_leaves_egress_unknown(self):
        fake = FakeFirewalld(
            interfaces={"host0": "public", BRIDGE: "nodo"},
            zones={"public": _public(), "nodo": _nodo_zone()}, has_policies=False,
        )
        self.assertIsNone(fwd.assess(BRIDGE, run=fake).egress)

    def test_a_runner_that_raises_is_never_blocked(self):
        def boom(command):
            if command[1] == "--state":
                return _proc(0, "running")
            raise OSError("dbus gone")

        self.assertFalse(fwd.assess(BRIDGE, run=boom).blocked)


class ApplyTests(unittest.TestCase):
    def test_apply_writes_zone_forward_ports_and_policy_then_reloads(self):
        fake = FakeFirewalld()
        done = fwd.apply(BRIDGE, [59192, 59193], run=fake)
        self.assertEqual(fake.interfaces[BRIDGE], "nodo")
        self.assertEqual(fake.zones["nodo"]["forward"], "yes")
        self.assertEqual(fake.zones["nodo"]["ports"], {"59192/tcp", "59193/tcp"})
        self.assertEqual(
            fake.policies["nodo-egress"], {"in": ["nodo"], "out": ["ANY"], "target": "ACCEPT"}
        )
        self.assertEqual(fake.calls[-1], ["firewall-cmd", "--reload"])
        self.assertTrue(done)
        with mock.patch.object(fwd.shutil, "which", return_value="/usr/bin/firewall-cmd"):
            self.assertFalse(fwd.assess(BRIDGE, run=fake).blocked)

    def test_apply_twice_changes_nothing_and_does_not_reload(self):
        fake = FakeFirewalld()
        fwd.apply(BRIDGE, [59192], run=fake)
        fake.calls.clear()
        self.assertEqual(fwd.apply(BRIDGE, [59192], run=fake), [])
        self.assertEqual(fake.writes(), [])

    def test_apply_completes_the_docs_zone_without_recreating_it(self):
        fake = FakeFirewalld(
            interfaces={"host0": "public", BRIDGE: "nodo"},
            zones={"public": _public(), "nodo": _nodo_zone(ports={"59192/tcp"})},
        )
        fwd.apply(BRIDGE, [59192], run=fake)
        written = [" ".join(c) for c in fake.writes()]
        self.assertFalse(any("--new-zone" in c for c in written))
        self.assertTrue(any("--new-policy=nodo-egress" in c for c in written))

    def test_a_slow_reload_is_reported_as_unfinished_with_what_was_done(self):
        fake = FakeFirewalld()

        def run(command):
            if command == ["firewall-cmd", "--reload"]:
                raise subprocess.TimeoutExpired(command, 120)
            return fake(command)

        with self.assertRaises(RuntimeError) as caught:
            fwd.apply(BRIDGE, [59192], run=run)
        self.assertIn("did not answer within 120s", str(caught.exception))
        self.assertIn("created policy nodo-egress", str(caught.exception))
        self.assertEqual(fwd.APPLY_TIMEOUT_S, 120)

    def test_a_failing_step_raises_with_firewalld_words(self):
        def run(command):
            if any(a.startswith(("--info-", "--query-")) for a in command):
                return _proc(1)
            return _proc(13, "", "ALREADY_ENABLED or worse")

        with self.assertRaises(RuntimeError) as caught:
            fwd.apply(BRIDGE, [], run=run)
        self.assertIn("worse", str(caught.exception))


class CommandTests(unittest.TestCase):
    def test_commands_add_the_policy_and_never_trusted(self):
        text = "\n".join(fwd.setup_commands(BRIDGE, [59192]))
        self.assertNotIn("trusted", text)
        for piece in ("--new-zone=nodo", f"--change-interface={BRIDGE}", "--add-forward",
                      "--add-port=59192/tcp", "--new-policy=nodo-egress",
                      "--add-ingress-zone=nodo", "--add-egress-zone=ANY",
                      "--set-target=ACCEPT"):
            self.assertIn(piece, text)

    def test_the_one_line_form_is_safe_to_rerun(self):
        line = fwd.one_line(fwd.setup_commands(BRIDGE, [59192]))
        # Every "a || b" is grouped, so "&&" never binds to one branch only.
        self.assertIn("{ sudo firewall-cmd --permanent --info-zone=nodo", line)
        self.assertNotIn("\n", line)

    def test_the_unprivileged_form_fits_one_sudo_sh_c(self):
        # install.sh runs it as "sudo sh -c '<line>'": no sudo inside, no single quote.
        line = fwd.one_line(fwd.setup_commands(BRIDGE, [59192, 59193], sudo=False))
        self.assertNotIn("sudo", line)
        self.assertNotIn("'", line)

    def test_the_notice_names_the_chain_and_the_missing_policy(self):
        fake = FakeFirewalld(
            interfaces={"host0": "public", BRIDGE: "nodo"},
            zones={"public": _public(), "nodo": _nodo_zone()},
        )
        with mock.patch.object(fwd.shutil, "which", return_value="/usr/bin/firewall-cmd"):
            verdict = fwd.assess(BRIDGE, run=fake)
        body = fwd.notice_body(BRIDGE, fwd.setup_commands(BRIDGE, []), verdict)
        self.assertIn("inet firewalld filter_FORWARD", body)
        self.assertIn("no firewalld policy", body)
        self.assertIn("Internet", fwd.notice_title(BRIDGE, verdict))


class DaemonReportTests(unittest.TestCase):
    """``_report_firewalld_zone``: what the daemon stores at start and preflight."""

    def setUp(self):
        from tests.config_bootstrap import load_example_config

        load_example_config()
        from src.virtualizers.microvm import network

        self.network = network
        self.manager = mock.Mock()
        self.manager.gateway_port_or_none.return_value = 59192
        self.manager.get_plaintext_gateway_port.return_value = 59193
        patcher = mock.patch.object(network, "ConfigManager", return_value=self.manager)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, network, "FIREWALLD_VERDICT", None)

    def _verdict(self, **kwargs):
        return mock.patch.object(fwd, "assess", return_value=fwd.Assessment(**kwargs))

    def test_a_zone_without_an_egress_policy_raises_the_notice(self):
        with self._verdict(running=True, zone="nodo", effective_zone="nodo",
                           guest_to_guest=True, egress=False, uplink_zones=("public",)):
            self.network._report_firewalld_zone()
        kwargs = self.manager.emit_forward_notice.call_args.kwargs
        self.assertIn("--new-policy=nodo-egress", kwargs["command"])
        self.assertIn("--add-port=59192/tcp", kwargs["command"])
        self.assertTrue(self.network._egress_blocked_by_firewalld())

    def test_both_paths_open_clears_a_stale_notice(self):
        with self._verdict(running=True, zone="nodo", effective_zone="nodo",
                           guest_to_guest=True, egress=True):
            self.network._report_firewalld_zone()
        self.manager.emit_forward_notice.assert_not_called()
        self.manager.clear_forward_notice.assert_called_once()
        self.assertFalse(self.network._egress_blocked_by_firewalld())

    def test_an_inconclusive_reading_neither_raises_nor_clears(self):
        with self._verdict(running=True, zone="nodo", effective_zone="nodo",
                           guest_to_guest=True, egress=None):
            self.network._report_firewalld_zone()
        self.manager.emit_forward_notice.assert_not_called()
        self.manager.clear_forward_notice.assert_not_called()

    def test_it_is_never_fatal(self):
        with mock.patch.object(fwd, "assess", side_effect=RuntimeError("boom")):
            self.network._report_firewalld_zone()


class LaunchLogTests(unittest.TestCase):
    """What ``configure_guest_firewall_policy`` logs for network tag '*' (#639)."""

    def setUp(self):
        from tests.config_bootstrap import load_example_config

        load_example_config()
        from protos import celaut_pb2 as celaut
        from src.virtualizers.microvm import network

        self.celaut, self.network = celaut, network
        self.addCleanup(setattr, network, "FIREWALLD_VERDICT", None)

    def _run(self, tags):
        resolution = self.celaut.ConfigurationFile.NetworkResolution()
        resolution.tags.extend(tags)
        lines = []
        n = self.network
        with mock.patch.object(n, "vm_block_all", return_value=True), \
                mock.patch.object(n, "vm_allow_host_connection", return_value=True), \
                mock.patch.object(n, "vm_allow_all_egress", return_value=True), \
                mock.patch.object(n, "vm_allow_connection_to_instance", return_value=True), \
                mock.patch.object(n.env_manager, "get_gateway_port", return_value=59192), \
                mock.patch.object(n.log, "LOGGER", side_effect=lines.append):
            n.configure_guest_firewall_policy("[t]", "vm", "192.168.200.9", [resolution])
        return "\n".join(lines)

    def test_star_no_longer_logs_a_rule_that_could_not_be_applied(self):
        logged = self._run(["*"])
        self.assertIn("allow-all egress", logged)
        self.assertNotIn("no egress rule could be applied", logged)

    def test_star_says_firewalld_will_reject_it_when_the_policy_is_missing(self):
        self.network.FIREWALLD_VERDICT = fwd.Assessment(
            running=True, zone="nodo", guest_to_guest=True, egress=False
        )
        self.assertIn("inet firewalld filter_FORWARD will reject", self._run(["*"]))

    def test_star_is_quiet_about_firewalld_when_egress_is_open(self):
        self.network.FIREWALLD_VERDICT = fwd.Assessment(
            running=True, zone="nodo", guest_to_guest=True, egress=True
        )
        self.assertNotIn("firewalld", self._run(["*"]))

    def test_a_tag_with_no_peer_is_not_called_a_failure(self):
        logged = self._run(["pow:ergo"])
        self.assertNotIn("could not be applied", logged)
        self.assertIn("resolved to no peer instance", logged)


def _scan(*tables):
    return RejectorScan(
        rejectors=tuple(
            ForeignRejector(table=t, chain=c, priority=p, reason="contains a reject rule",
                            hook="forward")
            for t, c, p in tables
        ),
        hook="forward",
    )


class CompatLogTests(unittest.TestCase):
    """The compatibility step is not logged as a fix for firewalld's chain (#639)."""

    def _ensure(self, scan):
        lines = []
        backend = mock.Mock()
        backend.foreign_forward_rejectors.return_value = scan
        with mock.patch.object(compat, "detect_backend", return_value=backend), \
                mock.patch.object(compat.shutil, "which", return_value="/usr/sbin/iptables"), \
                mock.patch.object(compat, "_iptables_backend") as ipt, \
                mock.patch.object(compat, "compat_state",
                                  return_value=compat.CompatState(mode=compat.CompatMode.AUTO)):
            ipt.return_value.ensure_own_chain.return_value = False
            ipt.return_value.ensure.return_value = False
            ipt.return_value.ensure_first.return_value = False
            state = compat.ensure_compat(BRIDGE, log=lines.append)
        return state, "\n".join(lines), ipt

    def test_only_firewalld_rejects_so_nothing_is_applied_and_that_is_said(self):
        state, logged, ipt = self._ensure(_scan(("inet firewalld", "filter_FORWARD", 10)))
        self.assertFalse(state.needed)
        ipt.return_value.ensure_own_chain.assert_not_called()
        self.assertIn("cannot help with inet firewalld/filter_FORWARD", logged)
        self.assertNotIn("Applying the forward compatibility rules", logged)

    def test_docker_and_firewalld_applies_for_docker_and_names_firewalld_apart(self):
        _, logged, ipt = self._ensure(_scan(
            ("ip filter", "FORWARD", 0), ("inet firewalld", "filter_FORWARD", 10)
        ))
        ipt.return_value.ensure_own_chain.assert_called_once()
        self.assertIn("because the forward hook is not clear: ip filter/FORWARD.", logged)
        self.assertIn("cannot help with inet firewalld/filter_FORWARD", logged)


class EgressProbeTests(unittest.TestCase):
    """``probe_tcp_egress``: what an answer, a reject and silence mean."""

    def _probe(self, connect_rc, connect_out, host_ok=True):
        calls = []

        def run(command):
            calls.append(list(command))
            if command[:2] == ["sysctl", "-n"]:
                return _proc(0, "1\n")
            if reachability.sys.executable in command:
                return _proc(connect_rc, connect_out)
            return _proc(0, "")

        entry = reachability.NamespaceEntry(name="nsenter", ip_prefix=("ip",), exec_prefix=())
        with mock.patch.object(reachability, "can_create_network_namespace", return_value=True), \
                mock.patch.object(reachability, "_namespace_entry", return_value=entry), \
                mock.patch.object(reachability, "_addresses_in_use", return_value=set()):
            result = reachability.probe_tcp_egress(
                bridge=BRIDGE, subnet="192.168.200.0/24", gateway_ip="192.168.200.1",
                run=run, host_connects=lambda ip, port, t: host_ok,
                attempts=2, sleep=lambda s: None,
            )
        return result, calls

    def test_an_answer_is_reachable(self):
        result, calls = self._probe(0, "connected")
        self.assertIs(result.reachable, True)
        self.assertTrue(any(c[-4:] == ["default", "via", "192.168.200.1", "dev"] or
                            "default" in c for c in calls))

    def test_an_icmp_reject_is_not_reachable(self):
        result, _ = self._probe(2, "OSError: [Errno 113] No route to host")
        self.assertIs(result.reachable, False)
        self.assertIn("rejected with an ICMP error", result.detail)

    def test_silence_while_the_host_gets_through_is_not_reachable(self):
        result, _ = self._probe(1, "TimeoutError: timed out")
        self.assertIs(result.reachable, False)
        self.assertIn("got no answer", result.detail)

    def test_a_host_without_internet_is_unknown_not_closed(self):
        result, calls = self._probe(2, "unreachable", host_ok=False)
        self.assertIsNone(result.reachable)
        self.assertFalse(any("netns" in c for c in calls))

    def test_the_probe_endpoint_is_an_isolated_port_like_a_guest(self):
        _, calls = self._probe(0, "connected")
        self.assertTrue(any("isolated" in c for c in calls))


class FrontendZoneTests(unittest.TestCase):
    """The gateway-port advice names the bridge's zone once it has one."""

    def _run(self, zone_answer):
        def run(command):
            if command[1] == "--state":
                return _proc(0, "running\n")
            if command[1].startswith("--get-zone-of-interface"):
                return zone_answer
            raise AssertionError(command)

        return run

    def test_the_scoped_rule_goes_in_the_bridge_zone(self):
        from src.utils.firewall import frontend

        with mock.patch.object(frontend.shutil, "which", return_value="/usr/bin/firewall-cmd"):
            found = frontend._firewalld_scoped(59193, "192.168.200.0/24",
                                               self._run(_proc(0, "nodo\n")))
        self.assertIn("--zone=nodo --add-rich-rule", found.command)

    def test_no_zone_keeps_the_default_zone_command(self):
        from src.utils.firewall import frontend

        with mock.patch.object(frontend.shutil, "which", return_value="/usr/bin/firewall-cmd"):
            found = frontend._firewalld(59192, self._run(_proc(2, "", "no zone")))
        self.assertNotIn("--zone=", found.command)

    def test_the_tls_port_is_opened_for_peers_and_for_the_guest_zone(self):
        from src.utils.firewall import frontend

        with mock.patch.object(frontend.shutil, "which", return_value="/usr/bin/firewall-cmd"):
            found = frontend._firewalld(59192, self._run(_proc(0, "nodo\n")))
        self.assertIn("--permanent --add-port=59192/tcp", found.command)
        self.assertIn("--zone=nodo --add-port=59192/tcp", found.command)


if __name__ == "__main__":
    unittest.main()
