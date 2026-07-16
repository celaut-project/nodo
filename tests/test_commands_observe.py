import json
import unittest
from tempfile import NamedTemporaryFile

# The observe module keeps its pure logic free of nodo runtime dependencies,
# so it imports cleanly in minimal environments (no bee_rpc/psutil required).
from src.commands import observe


class BytesToHumanTests(unittest.TestCase):
    def test_scales_units(self):
        self.assertEqual(observe.bytes_to_human(None), "N/A")
        self.assertEqual(observe.bytes_to_human(512), "512 B")
        self.assertEqual(observe.bytes_to_human(2 * 1024), "2 KB")
        self.assertEqual(observe.bytes_to_human(186 * 1024 ** 2), "186 MB")
        self.assertEqual(observe.bytes_to_human(3 * 1024 ** 3), "3 GB")


class CpuPercentTests(unittest.TestCase):
    def test_returns_none_until_full_delta(self):
        self.assertIsNone(observe.compute_cpu_percent(None, 10, 0, 1))
        self.assertIsNone(observe.compute_cpu_percent(10, 20, None, 1))

    def test_one_core_fully_busy_is_100(self):
        # 1s of wall time (1e9 ns) with 1s of CPU (1e6 usec) == 100%.
        pct = observe.compute_cpu_percent(0, 1_000_000, 0, 1_000_000_000)
        self.assertAlmostEqual(pct, 100.0, places=3)

    def test_multicore_can_exceed_100(self):
        pct = observe.compute_cpu_percent(0, 2_000_000, 0, 1_000_000_000)
        self.assertAlmostEqual(pct, 200.0, places=3)

    def test_counter_reset_is_ignored(self):
        self.assertIsNone(observe.compute_cpu_percent(5_000_000, 10, 0, 1_000_000_000))

    def test_zero_wall_delta_is_ignored(self):
        self.assertIsNone(observe.compute_cpu_percent(0, 10, 50, 50))


class SessionMetricsTests(unittest.TestCase):
    def test_tracks_current_and_peak(self):
        m = observe.SessionMetrics()
        m.update_cpu(18.0)
        m.update_cpu(43.0)
        m.update_cpu(12.0)
        self.assertEqual(m.cpu_current, 12.0)
        self.assertEqual(m.cpu_peak, 43.0)

        m.update_memory(186 * 1024 ** 2)
        m.update_memory(241 * 1024 ** 2)
        m.update_memory(200 * 1024 ** 2)
        self.assertEqual(m.mem_current, 200 * 1024 ** 2)
        self.assertEqual(m.mem_peak, 241 * 1024 ** 2)

    def test_none_samples_do_not_reset_state(self):
        m = observe.SessionMetrics()
        m.update_cpu(30.0)
        m.update_cpu(None)
        self.assertEqual(m.cpu_current, 30.0)
        self.assertEqual(m.cpu_peak, 30.0)

    def test_cpu_str_formats(self):
        m = observe.SessionMetrics()
        self.assertEqual(m.cpu_str(None), "N/A")
        self.assertEqual(m.cpu_str(18.4), "18%")


class ConntrackParsingTests(unittest.TestCase):
    OUTBOUND = (
        "ipv4     2 tcp      6 431999 ESTABLISHED "
        "src=10.0.0.2 dst=34.117.5.5 sport=54000 dport=443 "
        "src=34.117.5.5 dst=10.0.0.2 sport=443 dport=54000 [ASSURED] mark=0 use=1"
    )
    INBOUND = (
        "ipv4     2 tcp      6 300 ESTABLISHED "
        "src=10.0.0.9 dst=10.0.0.2 sport=6001 dport=8080 "
        "src=10.0.0.2 dst=10.0.0.9 sport=8080 dport=6001 [ASSURED] mark=0 use=1"
    )

    def test_outbound_direction_and_protocol(self):
        event = observe.parse_conntrack_line(self.OUTBOUND, vm_ip="10.0.0.2")
        self.assertEqual(event["direction"], "OUT")
        self.assertEqual(event["peer_ip"], "34.117.5.5")
        self.assertEqual(event["transport"], "tcp")
        self.assertEqual(event["protocol"], "HTTPS")
        self.assertEqual(event["dst_port"], 443)

    def test_inbound_direction(self):
        event = observe.parse_conntrack_line(self.INBOUND, vm_ip="10.0.0.2")
        self.assertEqual(event["direction"], "IN")
        self.assertEqual(event["peer_ip"], "10.0.0.9")
        self.assertEqual(event["dst_port"], 8080)

    def test_unrelated_line_returns_none(self):
        self.assertIsNone(observe.parse_conntrack_line(self.OUTBOUND, vm_ip="192.168.1.1"))

    def test_non_tcp_udp_returns_none(self):
        line = "ipv4 2 icmp 1 30 src=10.0.0.2 dst=8.8.8.8 type=8 code=0 id=1"
        self.assertIsNone(observe.parse_conntrack_line(line, vm_ip="10.0.0.2"))

    def test_flow_key_is_stable(self):
        e1 = observe.parse_conntrack_line(self.OUTBOUND, vm_ip="10.0.0.2")
        e2 = observe.parse_conntrack_line(self.OUTBOUND, vm_ip="10.0.0.2")
        self.assertEqual(observe.flow_key(e1), observe.flow_key(e2))


class AppProtocolTests(unittest.TestCase):
    def test_well_known_ports(self):
        self.assertEqual(observe.app_protocol("tcp", 443), "HTTPS")
        self.assertEqual(observe.app_protocol("tcp", 80), "HTTP")

    def test_falls_back_to_transport(self):
        self.assertEqual(observe.app_protocol("udp", 51820), "UDP")


class ClassifyPeerTests(unittest.TestCase):
    INDEX = {
        "10.0.0.1": {"id": "parentid1234", "service_id": "svc-gw", "father_id": ""},
        "10.0.0.3": {"id": "childid5678", "service_id": "svc-wk", "father_id": "observedID"},
        "10.0.0.4": {"id": "peerid9999", "service_id": "svc-x", "father_id": "otherfather"},
    }

    def test_parent_relationship(self):
        peer = observe.classify_peer("10.0.0.1", "observedID", "parentid1234", self.INDEX)
        self.assertEqual(peer["kind"], "instance")
        self.assertEqual(peer["relationship"], "parent")

    def test_child_relationship(self):
        peer = observe.classify_peer("10.0.0.3", "observedID", "parentid1234", self.INDEX)
        self.assertEqual(peer["relationship"], "child")

    def test_peer_relationship(self):
        peer = observe.classify_peer("10.0.0.4", "observedID", "parentid1234", self.INDEX)
        self.assertEqual(peer["relationship"], "peer")

    def test_external_host(self):
        peer = observe.classify_peer("34.1.2.3", "observedID", "parentid1234", self.INDEX)
        self.assertEqual(peer["kind"], "external")
        self.assertEqual(peer["host"], "34.1.2.3")


class FormatEventLineTests(unittest.TestCase):
    def test_instance_line_with_tag_and_relationship(self):
        event = {
            "time": "12:31:04",
            "direction": "OUT",
            "peer": {"kind": "instance", "id": "c92ae2ffdeadbeef", "relationship": "parent"},
        }
        line = observe.format_event_line(event, tag="gateway")
        self.assertEqual(line, "12:31:04 OUT → instance c92ae2ff [gateway] (parent)")

    def test_inbound_uses_left_arrow(self):
        event = {
            "time": "12:31:05",
            "direction": "IN",
            "peer": {"kind": "instance", "id": "d83aa112", "relationship": "child"},
        }
        line = observe.format_event_line(event, tag="worker")
        self.assertEqual(line, "12:31:05 IN  ← instance d83aa112 [worker] (child)")

    def test_external_line(self):
        event = {
            "time": "12:31:06",
            "direction": "OUT",
            "protocol": "HTTPS",
            "peer": {"kind": "external", "host": "api.github.com"},
        }
        line = observe.format_event_line(event)
        self.assertEqual(line, "12:31:06 OUT → api.github.com  [HTTPS]")


class TraceWriterTests(unittest.TestCase):
    def _event(self):
        return {
            "time": "12:31:04",
            "direction": "OUT",
            "transport": "tcp",
            "protocol": "HTTPS",
            "src_ip": "10.0.0.2",
            "src_port": 54000,
            "dst_ip": "34.117.5.5",
            "dst_port": 443,
            "peer": {"kind": "external", "host": "34.117.5.5"},
            "tag": None,
        }

    def test_jsonl_selection_and_content(self):
        with NamedTemporaryFile(suffix=".jsonl", mode="r", delete=True) as tmp:
            writer = observe.TraceWriter(tmp.name)
            self.assertTrue(writer.jsonl)
            with writer:
                writer.write(self._event())
            tmp.seek(0)
            record = json.loads(tmp.read().strip())
            self.assertEqual(record["direction"], "OUT")
            self.assertEqual(record["protocol"], "HTTPS")
            self.assertEqual(record["peer_kind"], "external")
            self.assertEqual(record["dst"], "34.117.5.5:443")

    def test_log_selection_writes_formatted_line(self):
        with NamedTemporaryFile(suffix=".log", mode="r", delete=True) as tmp:
            writer = observe.TraceWriter(tmp.name)
            self.assertFalse(writer.jsonl)
            with writer:
                writer.write(self._event())
            tmp.seek(0)
            self.assertEqual(tmp.read().strip(), "12:31:04 OUT → 34.117.5.5  [HTTPS]")


if __name__ == "__main__":
    unittest.main()
