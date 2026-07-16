import json
import os
import struct
import unittest
from tempfile import NamedTemporaryFile, TemporaryDirectory

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


class TapResolutionTests(unittest.TestCase):
    def test_matches_virtualizer_derivation(self):
        # Must equal ch/execute.py::_create_tap: "tap" + sha1(id)[:10].
        import hashlib

        instance_id = "8a7fd2c0deadbeef"
        expected = "tap" + hashlib.sha1(instance_id.encode()).hexdigest()[:10]
        self.assertEqual(observe.tap_ifname_for_instance(instance_id), expected)
        self.assertTrue(observe.tap_ifname_for_instance(instance_id).startswith("tap"))
        # Length: "tap" (3) + 10 hex chars.
        self.assertEqual(len(observe.tap_ifname_for_instance(instance_id)), 13)

    def test_deterministic(self):
        a = observe.tap_ifname_for_instance("abc123")
        b = observe.tap_ifname_for_instance("abc123")
        self.assertEqual(a, b)


class PcapFramingTests(unittest.TestCase):
    def test_global_header_bytes(self):
        hdr = observe.pcap_global_header(snaplen=65535, network=1)
        self.assertEqual(len(hdr), 24)
        magic, vmaj, vmin, tz, sig, snap, net = struct.unpack("<IHHiIII", hdr)
        self.assertEqual(magic, 0xA1B2C3D4)
        self.assertEqual(vmaj, 2)
        self.assertEqual(vmin, 4)
        self.assertEqual(tz, 0)
        self.assertEqual(sig, 0)
        self.assertEqual(snap, 65535)
        self.assertEqual(net, 1)  # LINKTYPE_ETHERNET

    def test_global_header_magic_is_little_endian_on_disk(self):
        # First four bytes on disk are the canonical LE microsecond magic.
        hdr = observe.pcap_global_header()
        self.assertEqual(hdr[:4], bytes((0xD4, 0xC3, 0xB2, 0xA1)))

    def test_packet_header_bytes(self):
        rec = observe.pcap_packet_header(ts_sec=1_700_000_000, ts_usec=123456,
                                         incl_len=74, orig_len=74)
        self.assertEqual(len(rec), 16)
        ts_sec, ts_usec, incl, orig = struct.unpack("<IIII", rec)
        self.assertEqual(ts_sec, 1_700_000_000)
        self.assertEqual(ts_usec, 123456)
        self.assertEqual(incl, 74)
        self.assertEqual(orig, 74)

    def test_record_truncates_to_snaplen_but_keeps_orig_len(self):
        frame = b"\xff" * 100
        rec = observe.pcap_record(frame, timestamp=5.5, snaplen=40)
        ts_sec, ts_usec, incl, orig = struct.unpack("<IIII", rec[:16])
        self.assertEqual(ts_sec, 5)
        self.assertEqual(ts_usec, 500000)
        self.assertEqual(incl, 40)          # captured length is clamped
        self.assertEqual(orig, 100)         # original length preserved
        self.assertEqual(len(rec), 16 + 40)


def _build_frame(src_ip, dst_ip, proto, sport=0, dport=0, tcp_flags=0):
    """Craft a minimal ethernet+IPv4(+TCP/UDP) frame for parser tests."""
    eth = b"\x02" * 6 + b"\x03" * 6 + struct.pack("!H", 0x0800)  # dst, src, IPv4
    src = bytes(int(o) for o in src_ip.split("."))
    dst = bytes(int(o) for o in dst_ip.split("."))
    # IPv4 header: version 4, IHL 5 (20 bytes).
    ip = struct.pack("!BBHHHBBH", 0x45, 0, 0, 0, 0, 64, proto, 0) + src + dst
    l4 = b""
    if proto == 6:   # TCP: sport, dport, seq, ack, offset/flags...
        l4 = struct.pack("!HHIIBBHHH", sport, dport, 0, 0, 0x50, tcp_flags, 0, 0, 0)
    elif proto == 17:  # UDP
        l4 = struct.pack("!HHHH", sport, dport, 8, 0)
    return eth + ip + l4


class EthernetParsingTests(unittest.TestCase):
    VM_IP = "10.0.0.2"

    def test_outbound_tcp_direction_and_transport_from_header(self):
        frame = _build_frame("10.0.0.2", "34.117.5.5", proto=6,
                             sport=54000, dport=443, tcp_flags=0x02)  # SYN
        ev = observe.parse_ethernet_frame(frame, vm_ip=self.VM_IP)
        self.assertEqual(ev["direction"], "OUT")
        self.assertEqual(ev["peer_ip"], "34.117.5.5")
        self.assertEqual(ev["transport"], "tcp")     # from IP proto byte 6
        self.assertEqual(ev["protocol"], "TCP")      # display label, no app guess
        self.assertEqual(ev["src_port"], 54000)
        self.assertEqual(ev["dst_port"], 443)
        self.assertEqual(ev["tcp_flags"], "SYN")

    def test_inbound_udp_direction(self):
        frame = _build_frame("10.0.0.9", "10.0.0.2", proto=17,
                             sport=6001, dport=8080)
        ev = observe.parse_ethernet_frame(frame, vm_ip=self.VM_IP)
        self.assertEqual(ev["direction"], "IN")
        self.assertEqual(ev["peer_ip"], "10.0.0.9")
        self.assertEqual(ev["transport"], "udp")
        self.assertEqual(ev["dst_port"], 8080)
        self.assertIsNone(ev["tcp_flags"])

    def test_icmp_has_transport_but_no_ports(self):
        frame = _build_frame("10.0.0.2", "8.8.8.8", proto=1)
        ev = observe.parse_ethernet_frame(frame, vm_ip=self.VM_IP)
        self.assertEqual(ev["transport"], "icmp")
        self.assertEqual(ev["direction"], "OUT")
        self.assertIsNone(ev["src_port"])
        self.assertIsNone(ev["dst_port"])

    def test_unrelated_frame_returns_none(self):
        frame = _build_frame("10.0.0.5", "10.0.0.6", proto=6, sport=1, dport=2)
        self.assertIsNone(observe.parse_ethernet_frame(frame, vm_ip=self.VM_IP))

    def test_non_ipv4_ethertype_returns_none(self):
        arp = b"\x02" * 6 + b"\x03" * 6 + struct.pack("!H", 0x0806) + b"\x00" * 28
        self.assertIsNone(observe.parse_ethernet_frame(arp, vm_ip=self.VM_IP))

    def test_truncated_frame_returns_none(self):
        self.assertIsNone(observe.parse_ethernet_frame(b"\x00" * 8, vm_ip=self.VM_IP))

    def test_flow_key_is_stable(self):
        f = _build_frame("10.0.0.2", "34.117.5.5", proto=6, sport=54000, dport=443)
        e1 = observe.parse_ethernet_frame(f, vm_ip=self.VM_IP)
        e2 = observe.parse_ethernet_frame(f, vm_ip=self.VM_IP)
        self.assertEqual(observe.flow_key(e1), observe.flow_key(e2))

    def test_tcp_flags_decoding(self):
        self.assertEqual(observe.tcp_flags_str(0x12), "SYN,ACK")
        self.assertEqual(observe.tcp_flags_str(0x10), "ACK")
        self.assertIsNone(observe.tcp_flags_str(0x00))
        self.assertIsNone(observe.tcp_flags_str(None))


class ConntrackFallbackParsingTests(unittest.TestCase):
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

    def test_outbound_transport_from_conntrack_token(self):
        event = observe.parse_conntrack_line(self.OUTBOUND, vm_ip="10.0.0.2")
        self.assertEqual(event["direction"], "OUT")
        self.assertEqual(event["peer_ip"], "34.117.5.5")
        self.assertEqual(event["transport"], "tcp")
        self.assertEqual(event["protocol"], "TCP")   # transport-derived, not app
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

    def test_external_line_shows_transport_protocol(self):
        event = {
            "time": "12:31:06",
            "direction": "OUT",
            "protocol": "TCP",
            "peer": {"kind": "external", "host": "34.117.5.5"},
        }
        line = observe.format_event_line(event)
        self.assertEqual(line, "12:31:06 OUT → 34.117.5.5  [TCP]")


class SaveDirTests(unittest.TestCase):
    def test_named_dir_uses_tag_and_id(self):
        path = observe.build_save_dir("/tmp/out", "gateway", "8a7fd2c0")
        self.assertEqual(path, os.path.join("/tmp/out", "gateway_8a7fd2c0"))

    def test_unnamed_dir_is_just_id(self):
        path = observe.build_save_dir("/tmp/out", None, "8a7fd2c0")
        self.assertEqual(path, os.path.join("/tmp/out", "8a7fd2c0"))

    def test_tag_is_sanitised_into_one_component(self):
        path = observe.build_save_dir("/tmp/out", "my/app name", "id123")
        self.assertEqual(path, os.path.join("/tmp/out", "my-app-name_id123"))

    def test_filename_constants(self):
        self.assertEqual(observe.METRICS_FILENAME, "metrics.jsonl")
        self.assertEqual(observe.CAPTURE_FILENAME, "capture.pcap")


class MetricsRecordTests(unittest.TestCase):
    def test_record_mirrors_panel(self):
        m = observe.SessionMetrics()
        m.update_cpu(18.4)
        m.update_cpu(43.9)
        m.update_memory(186 * 1024 ** 2)
        rec = observe.metrics_record(m, alive=True, timestamp="12:00:00")
        self.assertEqual(rec["time"], "12:00:00")
        self.assertTrue(rec["alive"])
        self.assertEqual(rec["cpu_percent"], 43.9)
        self.assertEqual(rec["cpu_peak_percent"], 43.9)
        self.assertEqual(rec["mem_bytes"], 186 * 1024 ** 2)
        self.assertEqual(rec["mem_peak_bytes"], 186 * 1024 ** 2)

    def test_none_metrics_serialise(self):
        rec = observe.metrics_record(observe.SessionMetrics(), alive=False,
                                     timestamp="12:00:01")
        self.assertIsNone(rec["cpu_percent"])
        self.assertIsNone(rec["mem_bytes"])
        self.assertFalse(rec["alive"])


class MetricsWriterTests(unittest.TestCase):
    def test_jsonl_line_format(self):
        with NamedTemporaryFile(suffix=".jsonl", mode="r", delete=True) as tmp:
            with observe.MetricsWriter(tmp.name) as w:
                w.write({"time": "12:00:00", "cpu_percent": 12.0, "mem_bytes": 1024})
                w.write({"time": "12:00:01", "cpu_percent": 15.0, "mem_bytes": 2048})
            tmp.seek(0)
            lines = tmp.read().strip().splitlines()
            self.assertEqual(len(lines), 2)
            first = json.loads(lines[0])
            self.assertEqual(first["cpu_percent"], 12.0)
            self.assertEqual(first["mem_bytes"], 1024)


class PcapWriterTests(unittest.TestCase):
    def test_writes_global_header_then_frames(self):
        with TemporaryDirectory() as d:
            path = os.path.join(d, "capture.pcap")
            frame_a = _build_frame("10.0.0.2", "8.8.8.8", proto=17, sport=1, dport=53)
            with observe.PcapWriter(path, snaplen=65535) as w:
                w.write_frame(frame_a, timestamp=1_700_000_000.5)
            with open(path, "rb") as f:
                data = f.read()
            # 24-byte global header first.
            magic, vmaj, vmin, tz, sig, snap, net = struct.unpack("<IHHiIII", data[:24])
            self.assertEqual(magic, 0xA1B2C3D4)
            self.assertEqual(net, 1)
            # One record header + frame body.
            ts_sec, ts_usec, incl, orig = struct.unpack("<IIII", data[24:40])
            self.assertEqual(ts_sec, 1_700_000_000)
            self.assertEqual(ts_usec, 500000)
            self.assertEqual(incl, len(frame_a))
            self.assertEqual(orig, len(frame_a))
            self.assertEqual(data[40:40 + len(frame_a)], frame_a)


class CaptureAvailabilityTests(unittest.TestCase):
    def test_capture_available_matches_platform(self):
        self.assertEqual(observe.capture_available(), hasattr(observe.socket, "AF_PACKET"))

    def test_resolve_capture_source_falls_back_without_af_packet(self):
        original = observe.capture_available
        try:
            observe.capture_available = lambda: False
            tap, reason = observe.resolve_capture_source("someid", "10.0.0.2")
            self.assertIsNone(tap)
            self.assertIn("AF_PACKET unavailable", reason)
            self.assertIn("no pcap", reason)
        finally:
            observe.capture_available = original

    def test_resolve_capture_source_falls_back_when_tap_missing(self):
        # AF_PACKET "available" but the tap interface does not exist → conntrack.
        original_avail = observe.capture_available
        original_exists = observe.interface_exists
        try:
            observe.capture_available = lambda: True
            observe.interface_exists = lambda name: False
            tap, reason = observe.resolve_capture_source("someid", "10.0.0.2")
            self.assertIsNone(tap)
            self.assertIn("not found", reason)
        finally:
            observe.capture_available = original_avail
            observe.interface_exists = original_exists


if __name__ == "__main__":
    unittest.main()
