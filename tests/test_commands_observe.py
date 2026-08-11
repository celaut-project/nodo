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


class HumanBytes1dpTests(unittest.TestCase):
    def test_one_decimal_scaling(self):
        self.assertEqual(observe.human_bytes_1dp(None), "N/A")
        self.assertEqual(observe.human_bytes_1dp(512), "512 B")
        self.assertEqual(observe.human_bytes_1dp(int(1.5 * 1024)), "1.5 KB")
        self.assertEqual(observe.human_bytes_1dp(int(38.4 * 1024)), "38.4 KB")
        self.assertEqual(observe.human_bytes_1dp(int(2.3 * 1024 ** 2)), "2.3 MB")


class FlowTableTests(unittest.TestCase):
    """The live per-flow aggregation: counters update, nothing is suppressed."""

    def _event(self, direction="OUT", peer_ip="34.117.5.5"):
        return {
            "direction": direction,
            "transport": "tcp",
            "protocol": "TCP",
            "src_ip": "10.0.0.2",
            "src_port": 54000,
            "dst_ip": peer_ip,
            "dst_port": 443,
            "peer_ip": peer_ip,
        }

    def _peer(self, host="34.117.5.5"):
        return {"kind": "external", "host": host}

    def test_repeated_packets_accumulate_and_do_not_suppress(self):
        table = observe.FlowTable()
        ev = self._event()
        key = observe.flow_key(ev)
        for i in range(3):
            row = table.update(
                key, direction="OUT", protocol="TCP", peer=self._peer(),
                tag=None, frame_len=100, timestamp=1000.0 + i, source="pcap")
        # Three packets on one flow → one row, counters summed, NOT shown once.
        rows = table.active_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(row["packets"], 3)
        self.assertEqual(row["bytes"], 300)
        self.assertEqual(row["first_seen"], 1000.0)
        self.assertEqual(row["last_seen"], 1002.0)

    def test_distinct_flows_are_separate_rows(self):
        table = observe.FlowTable()
        a = self._event(peer_ip="34.117.5.5")
        b = self._event(peer_ip="8.8.8.8")
        table.update(observe.flow_key(a), direction="OUT", protocol="TCP",
                     peer=self._peer("34.117.5.5"), tag=None, frame_len=60,
                     timestamp=5.0, source="pcap")
        table.update(observe.flow_key(b), direction="OUT", protocol="TCP",
                     peer=self._peer("8.8.8.8"), tag=None, frame_len=60,
                     timestamp=6.0, source="pcap")
        self.assertEqual(len(table.active_rows()), 2)

    def test_active_rows_sorted_newest_first_and_capped(self):
        table = observe.FlowTable(max_rows=2)
        for i in range(3):
            ev = self._event(peer_ip=f"9.9.9.{i}")
            table.update(observe.flow_key(ev), direction="OUT", protocol="TCP",
                         peer=self._peer(f"9.9.9.{i}"), tag=None, frame_len=10,
                         timestamp=float(i), source="pcap")
        rows = table.active_rows()
        self.assertEqual(len(rows), 2)                 # capped to max_rows
        self.assertGreater(rows[0]["last_seen"], rows[1]["last_seen"])  # newest first

    def test_conntrack_source_leaves_bytes_zero(self):
        table = observe.FlowTable()
        ev = self._event()
        table.update(observe.flow_key(ev), direction="OUT", protocol="TCP",
                     peer=self._peer(), tag=None, frame_len=None,
                     timestamp=1.0, source="conntrack")
        row = table.active_rows()[0]
        self.assertEqual(row["packets"], 1)
        self.assertEqual(row["bytes"], 0)              # no per-flow bytes in conntrack


class FormatFlowLineTests(unittest.TestCase):
    def _row(self, **over):
        base = {
            "direction": "OUT",
            "protocol": "TCP",
            "peer": {"kind": "instance", "id": "c92ae2ffdeadbeef",
                     "relationship": "parent"},
            "tag": "gateway",
            "packets": 142,
            "bytes": int(38.4 * 1024),
            "first_seen": 1_700_000_000.0,
            "last_seen": 1_700_000_000.0,
            "source": "pcap",
        }
        base.update(over)
        return base

    def test_instance_row_has_counts_and_bytes(self):
        line = observe.format_flow_line(self._row())
        self.assertIn("OUT", line)
        self.assertIn("→", line)
        self.assertIn("instance c92ae2ff", line)
        self.assertIn("[gateway]", line)
        self.assertIn("(parent)", line)
        self.assertIn("TCP", line)
        self.assertIn("142 pkts", line)
        self.assertIn("38.4 KB", line)

    def test_inbound_uses_left_arrow(self):
        line = observe.format_flow_line(self._row(direction="IN"))
        self.assertIn("←", line)

    def test_conntrack_row_shows_conntrack_instead_of_bytes(self):
        line = observe.format_flow_line(
            self._row(source="conntrack", bytes=0,
                      peer={"kind": "external", "host": "34.117.5.5"}))
        self.assertIn("conntrack", line)
        self.assertIn("34.117.5.5", line)

    def test_long_peer_is_truncated_to_one_line(self):
        long_host = "verylonghostname." * 5
        line = observe.format_flow_line(
            self._row(peer={"kind": "external", "host": long_host}))
        self.assertIn("…", line)
        # peer column stays bounded (truncation applied).
        self.assertLess(len(line), 120)


class LinkTypeDetectionTests(unittest.TestCase):
    def test_ether_type_selects_ethernet(self):
        original = observe.read_interface_arptype
        try:
            observe.read_interface_arptype = lambda name: observe.ARPHRD_ETHER
            lt, is_eth = observe.detect_link_type("tapabc")
            self.assertEqual(lt, observe.LINKTYPE_ETHERNET)
            self.assertTrue(is_eth)
        finally:
            observe.read_interface_arptype = original

    def test_arphrd_none_selects_raw(self):
        original = observe.read_interface_arptype
        try:
            observe.read_interface_arptype = lambda name: observe.ARPHRD_NONE
            lt, is_eth = observe.detect_link_type("tun0")
            self.assertEqual(lt, observe.LINKTYPE_RAW)
            self.assertFalse(is_eth)
        finally:
            observe.read_interface_arptype = original

    def test_unknown_type_defaults_to_ethernet(self):
        original = observe.read_interface_arptype
        try:
            observe.read_interface_arptype = lambda name: None
            lt, is_eth = observe.detect_link_type("weird0")
            self.assertEqual(lt, observe.LINKTYPE_ETHERNET)
            self.assertTrue(is_eth)
        finally:
            observe.read_interface_arptype = original

    def test_first_frame_sniff_when_sysfs_unavailable(self):
        original = observe.read_interface_arptype
        try:
            observe.read_interface_arptype = lambda name: None
            # A raw IPv4 packet (version nibble 4) → RAW.
            raw_ip = bytes([0x45]) + b"\x00" * 30
            lt, is_eth = observe.detect_link_type("x", first_frame=raw_ip)
            self.assertEqual(lt, observe.LINKTYPE_RAW)
            self.assertFalse(is_eth)
            # An ethernet frame with an IPv4 ethertype → ETHERNET.
            eth = _build_frame("10.0.0.2", "8.8.8.8", proto=17, sport=1, dport=53)
            lt2, is_eth2 = observe.detect_link_type("x", first_frame=eth)
            self.assertEqual(lt2, observe.LINKTYPE_ETHERNET)
            self.assertTrue(is_eth2)
        finally:
            observe.read_interface_arptype = original


class RawIpParsingTests(unittest.TestCase):
    """LINKTYPE_RAW path: frames are IP packets with no ethernet header."""

    VM_IP = "10.0.0.2"

    def _raw_ip(self, src, dst, proto, sport=0, dport=0):
        eth_framed = _build_frame(src, dst, proto, sport, dport)
        return eth_framed[14:]  # strip the ethernet header → raw IP

    def test_raw_ip_outbound_parsed_without_ethernet_header(self):
        raw = self._raw_ip("10.0.0.2", "34.117.5.5", proto=6, sport=1, dport=443)
        ev = observe.parse_ethernet_frame(raw, vm_ip=self.VM_IP, is_ethernet=False)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["direction"], "OUT")
        self.assertEqual(ev["peer_ip"], "34.117.5.5")
        self.assertEqual(ev["transport"], "tcp")

    def test_ethernet_frame_fed_as_raw_would_misparse(self):
        # Sanity: an ethernet frame parsed as raw IP does NOT yield a valid event
        # (the leading MAC bytes aren't a valid IPv4 header) — proving the flag matters.
        eth = _build_frame("10.0.0.2", "34.117.5.5", proto=6, sport=1, dport=443)
        self.assertIsNone(
            observe.parse_ethernet_frame(eth, vm_ip=self.VM_IP, is_ethernet=False))

    def test_ethernet_mode_still_default(self):
        eth = _build_frame("10.0.0.2", "34.117.5.5", proto=6, sport=1, dport=443)
        ev = observe.parse_ethernet_frame(eth, vm_ip=self.VM_IP)  # default is_ethernet=True
        self.assertEqual(ev["direction"], "OUT")


class ScmTimestampParseTests(unittest.TestCase):
    def _cmsg(self, tv_sec, tv_nsec, level=None, ctype=None):
        level = observe.socket.SOL_SOCKET if level is None else level
        ctype = observe.SCM_TIMESTAMPNS if ctype is None else ctype
        data = struct.pack(observe._TIMESPEC_FMT, tv_sec, tv_nsec)
        return (level, ctype, data)

    def test_parses_timespec_to_float_seconds(self):
        ancdata = [self._cmsg(1_700_000_000, 500_000_000)]
        ts = observe.parse_scm_timestampns(ancdata)
        self.assertAlmostEqual(ts, 1_700_000_000.5, places=6)

    def test_returns_none_without_timestamp_cmsg(self):
        # A control message of a different type → no usable timestamp.
        other = [(observe.socket.SOL_SOCKET, 0xABCD,
                  struct.pack(observe._TIMESPEC_FMT, 1, 0))]
        self.assertIsNone(observe.parse_scm_timestampns(other))
        self.assertIsNone(observe.parse_scm_timestampns([]))

    def test_ignores_truncated_cmsg_data(self):
        short = [(observe.socket.SOL_SOCKET, observe.SCM_TIMESTAMPNS, b"\x00\x00")]
        self.assertIsNone(observe.parse_scm_timestampns(short))


class CaptureUnavailableNoteTests(unittest.TestCase):
    def test_writes_reason_into_save_dir(self):
        with TemporaryDirectory() as d:
            reason = "AF_PACKET unavailable (non-Linux host); no pcap will be written"
            path = observe.write_capture_unavailable_note(d, reason)
            self.assertEqual(os.path.basename(path), observe.CAPTURE_UNAVAILABLE_FILENAME)
            with open(path, "r", encoding="utf-8") as f:
                body = f.read()
            self.assertIn(reason, body)
            self.assertIn("No packet capture", body)
            self.assertIn("CAP_NET_RAW", body)


class FormatBalanceTests(unittest.TestCase):
    def test_missing_or_empty_is_na(self):
        self.assertEqual(observe.format_balance(None), "N/A")
        self.assertEqual(observe.format_balance(""), "N/A")

    def test_non_numeric_is_flagged(self):
        self.assertEqual(observe.format_balance("not-a-number"), "Invalid balance")

    def test_numeric_matches_node_formatter(self):
        # A balance is rendered exactly like `nodo instances` does, whether the
        # catalogue hands us an int or the numeric string it actually stores.
        from src.utils.logger import ssformat

        # 1000 MU is 1000 nanoERG, rendered as ERG for whoever is reading the panel.
        self.assertEqual(observe.format_balance(1000), "0.000001 ERG")
        self.assertEqual(observe.format_balance("1000"), "0.000001 ERG")


class InstanceBalanceCatalogueTests(unittest.TestCase):
    """resolve_instance / read_instance_balance against a real sqlite catalogue."""

    def setUp(self):
        import sqlite3

        self._tmp = NamedTemporaryFile(suffix=".sqlite", delete=False)
        self._tmp.close()
        self._orig_db = observe.DATABASE_FILE
        observe.DATABASE_FILE = self._tmp.name
        conn = sqlite3.connect(self._tmp.name)
        conn.execute(
            "CREATE TABLE local_instances ("
            "id TEXT, ip TEXT, father_id TEXT, service_id TEXT, "
            "virtualizer TEXT, name TEXT, balance_mu TEXT)"
        )
        conn.execute(
            "INSERT INTO local_instances VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("abc123", "10.0.0.2", "dad", "svc", "ch", "inst", "5000"),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        observe.DATABASE_FILE = self._orig_db
        os.unlink(self._tmp.name)

    def test_resolve_instance_includes_balance(self):
        row = observe.resolve_instance("abc123")
        self.assertIsNotNone(row)
        self.assertEqual(row["balance_mu"], "5000")

    def test_read_instance_gas_reflects_updates(self):
        import sqlite3

        self.assertEqual(observe.read_instance_balance("abc123"), "5000")
        conn = sqlite3.connect(self._tmp.name)
        conn.execute("UPDATE local_instances SET balance_mu = ? WHERE id = ?",
                     ("4200", "abc123"))
        conn.commit()
        conn.close()
        self.assertEqual(observe.read_instance_balance("abc123"), "4200")

    def test_read_instance_gas_unknown_id_is_none(self):
        self.assertIsNone(observe.read_instance_balance("nope"))


class RenderBalancePanelTests(unittest.TestCase):
    def test_render_shows_gas_balance(self):
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            observe._render(
                header="inst",
                metrics=observe.SessionMetrics(),
                balance="0.0000012 ERG",
                events=[],
                save_dir=None,
                net_notice=None,
                capture_mode="conntrack",
            )
        out = buf.getvalue()
        self.assertIn("Balance", out)
        self.assertIn("Current: 0.0000012 ERG", out)


if __name__ == "__main__":
    unittest.main()
