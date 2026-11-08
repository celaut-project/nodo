"""The check of execution receipts against the rows of a balance statement (#419, #631)."""
import time
import unittest
from collections import namedtuple

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from protos import celaut_pb2
    from src.identity import execution_receipt as er
    from src.identity import receipt_statement as rs
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

Row = namedtuple("Row", "seq ts amount kind detail")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ReceiptStatementTests(unittest.TestCase):

    def setUp(self):
        self.key = ed25519.Ed25519PrivateKey.generate()
        self.peer_id = er.public_key_hex(self.key) if hasattr(er, "public_key_hex") else \
            self.key.public_key().public_bytes_raw().hex()
        self.token = "token-of-the-instance"
        self.name = er.execution_name(self.token, self.peer_id)
        self.now = int(time.time())

    def sign(self, receipt):
        er.sign_with(receipt, self.key)

    def chain(self, mu, final=True, session="ab" * 8):
        stream = er.ReceiptChain(
            self.token, er.PLANE_TUNNEL, er.COVERAGE_CLIENT_TUNNEL, slot="80",
            signer=self.sign, peer_id=self.peer_id, session=session)
        stream.record(er.DIRECTION_IN, b"ping")
        stream.record(er.DIRECTION_OUT, b"pong")
        return stream.checkpoint(mu_charged=mu, final=final)

    def rows(self, charges=(10, 2, 3), session="ab" * 8, anchor_digest=None, anchor_mu=None,
             extra=()):
        """The rows of one session: the opening, some blocks, and the anchor."""
        cause = {"execution_name": self.name, "session": session}
        rows = [Row(1, self.now, -charges[0], "tunnel", {**cause, "open": True})]
        for seq, mu in enumerate(charges[1:], start=2):
            rows.append(Row(seq, self.now, -mu, "tunnel", {**cause, "bytes": "1024"}))
        if anchor_digest is not None:
            rows.append(Row(len(rows) + 1, self.now, 0, "tunnel", {
                **cause, "close": True, "receipt_digest": anchor_digest,
                "mu_charged": str(sum(charges) if anchor_mu is None else anchor_mu)}))
        return rows + list(extra)

    def kinds(self, report):
        return [f["kind"] for f in report["findings"]]

    def test_a_close_that_agrees_with_its_rows_and_its_anchor_is_valid(self):
        close = self.chain(15)
        report = rs.check([close], self.rows(anchor_digest=er.receipt_digest(close)),
                          peer_id=self.peer_id)
        self.assertEqual(report["findings"], [])
        self.assertTrue(report["valid"])
        self.assertEqual(report["checked"], 1)

    def test_a_close_that_says_another_total_than_the_rows_is_a_finding(self):
        close = self.chain(99)
        report = rs.check([close], self.rows(anchor_digest=er.receipt_digest(close),
                                             anchor_mu=99), peer_id=self.peer_id)
        self.assertEqual(self.kinds(report), [rs.SESSION_TOTAL])
        self.assertFalse(report["valid"])

    def test_a_row_that_holds_the_digest_of_another_receipt_is_a_finding(self):
        close = self.chain(15)
        report = rs.check([close], self.rows(anchor_digest="00" * 32), peer_id=self.peer_id)
        self.assertEqual(self.kinds(report), [rs.ANCHOR_DIGEST])

    def test_an_anchor_that_says_another_total_is_a_finding(self):
        close = self.chain(15)
        report = rs.check([close], self.rows(anchor_digest=er.receipt_digest(close),
                                             anchor_mu=14), peer_id=self.peer_id)
        self.assertEqual(self.kinds(report), [rs.ANCHOR_TOTAL])

    def test_no_anchor_is_a_finding_only_when_the_account_went_on(self):
        close = self.chain(15)
        close_ts = int(close.ts)
        report = rs.check([close], self.rows(), peer_id=self.peer_id)
        self.assertEqual(report["findings"], [])
        later = Row(9, close_ts + rs.ANCHOR_GRACE_SECONDS + 5, -1, "maintenance", {})
        report = rs.check([close], self.rows(extra=[later]), peer_id=self.peer_id)
        self.assertEqual(self.kinds(report), [rs.ANCHOR_MISSING])
        self.assertEqual(report["findings"][0]["seq"], 9)

    def test_a_cancelled_stream_has_the_rows_at_least_as_high_as_its_checkpoint(self):
        checkpoint = self.chain(12, final=False)
        self.assertEqual(rs.check([checkpoint], self.rows(), peer_id=self.peer_id)["findings"], [])
        report = rs.check([checkpoint], self.rows(charges=(10, 1)), peer_id=self.peer_id)
        self.assertEqual(self.kinds(report), [rs.SESSION_BELOW_CHECKPOINT])

    def test_a_session_whose_opening_is_not_in_the_rows_is_not_judged(self):
        close = self.chain(15)
        tail = self.rows(anchor_digest="00" * 32)[1:]
        report = rs.check([close], tail, peer_id=self.peer_id)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["checked"], 0)
        self.assertEqual(len(report["skipped"]), 1)

    def test_a_refund_of_an_opening_is_taken_from_the_total(self):
        close = self.chain(10)
        cause = {"execution_name": self.name, "session": "ab" * 8}
        rows = [Row(1, self.now, -10, "tunnel", {**cause, "open": True}),
                Row(2, self.now, 5, "tunnel", {**cause, "open_refund": True}),
                Row(3, self.now, -5, "tunnel", {**cause, "bytes": "1"})]
        self.assertEqual(rs.check([close], rows, peer_id=self.peer_id)["findings"], [])

    def start_receipt(self, mu):
        return er.start_receipt(
            self.token, celaut_pb2.Metadata.HashTag.Hash(type=b"\x12", value=b"\x01" * 32),
            {"A": b"1"}, mu, signer=self.sign, peer_id=self.peer_id)

    def test_the_amount_of_the_start_row_is_the_initial_mu_of_the_receipt(self):
        start = self.start_receipt(500)
        row = Row(1, self.now, 500, "start", {"execution_name": self.name})
        self.assertEqual(rs.check([start], [row], peer_id=self.peer_id)["findings"], [])
        report = rs.check([start], [row._replace(amount=400)], peer_id=self.peer_id)
        self.assertEqual(self.kinds(report), [rs.START_AMOUNT])

    def test_the_start_row_holds_the_digest_of_the_start_receipt(self):
        start = self.start_receipt(500)
        row = Row(1, self.now, 500, "start", {
            "execution_name": self.name, "receipt_digest": er.receipt_digest(start)})
        report = rs.check([start], [row], peer_id=self.peer_id)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["checked"], 1)

        wrong = row._replace(detail={**row.detail, "receipt_digest": "00" * 32})
        report = rs.check([start], [wrong], peer_id=self.peer_id)
        self.assertEqual(self.kinds(report), [rs.START_DIGEST])
        self.assertEqual(report["findings"][0]["seq"], 1)

    def test_a_start_row_with_no_digest_is_not_judged_on_it(self):
        start = self.start_receipt(500)
        row = Row(1, self.now, 500, "start", {"execution_name": self.name})
        self.assertEqual(rs.check([start], [row], peer_id=self.peer_id)["findings"], [])

    def test_a_receipt_that_does_not_verify_is_a_finding_and_is_not_read_further(self):
        close = self.chain(15)
        close.mu_charged.n = "1"
        report = rs.check([close], self.rows(), peer_id=self.peer_id)
        self.assertEqual(self.kinds(report), [rs.INVALID_RECEIPT])

    def test_a_receipt_of_another_node_is_a_finding(self):
        close = self.chain(15)
        other = ed25519.Ed25519PrivateKey.generate().public_key().public_bytes_raw().hex()
        report = rs.check([close], self.rows(), peer_id=other)
        self.assertEqual(self.kinds(report), [rs.INVALID_RECEIPT])


if __name__ == "__main__":
    unittest.main()
