"""The tables donations live in, and the constraints that carry the design.

Two of them are load-bearing rather than defensive:

* ``donations``' UNIQUE key is what makes a re-scan a no-op, which is what lets the
  indexer be dumb about overlapping page boundaries and about reorgs near the tip.
  ``token_id`` is part of it because one Ergo transaction can pay the same address in
  ERG *and* in a token, in the same box.
* ``donation_accrual``'s primary key is the payment *method*, so two assets settling
  through one contract owe two independent debts.

They are also in the set the node creates on startup: `migrate` only runs from the
setup scripts, so pulling new code and restarting is the whole upgrade path most
operators take, and a donation debt that lost its table would be money already owed.
"""
import sqlite3
import unittest

IMPORT_ERROR = None
try:
    from src.database.migrate import TABLES, ensure_columns, ensure_tables
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

DONATION_TABLES = ("donation_accrual", "donations", "donation_scan_state")


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DonationSchemaTests(unittest.TestCase):

    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.cursor = self.connection.cursor()
        ensure_tables(self.cursor, DONATION_TABLES + ("payments",))

    def _insert_donation(self, **overrides):
        row = {
            "ledger": "ergo",
            "tx_id": "tx-1",
            "to_address": "9gGZ",
            "from_address": "9fXX",
            "token_id": "ERG",
            "amount_native": "5000000",
            "tx_height": 100,
        }
        row.update(overrides)
        self.cursor.execute(
            "INSERT OR IGNORE INTO donations (ledger, tx_id, to_address, from_address,"
            " token_id, amount_native, tx_height) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (row["ledger"], row["tx_id"], row["to_address"], row["from_address"],
             row["token_id"], row["amount_native"], row["tx_height"]),
        )
        return self.cursor.execute("SELECT COUNT(*) FROM donations").fetchone()[0]

    def test_a_database_from_before_these_tables_gets_them(self):
        tables = {
            row[0] for row in
            self.connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        self.assertTrue(set(DONATION_TABLES).issubset(tables))

    def test_reading_the_same_donation_twice_stores_it_once(self):
        self.assertEqual(self._insert_donation(), 1)
        self.assertEqual(self._insert_donation(), 1, "a re-scan must be a no-op")

    def test_the_same_transaction_paying_two_assets_is_two_donations(self):
        # One Ergo box can carry ERG and a token at once. Without token_id in the key
        # the second asset would collide with the first and be lost.
        self.assertEqual(self._insert_donation(), 1)
        self.assertEqual(self._insert_donation(token_id="a" * 64), 2)

    def test_two_donors_paying_one_address_in_one_scan_are_two_donations(self):
        self.assertEqual(self._insert_donation(), 1)
        self.assertEqual(self._insert_donation(from_address="9zzz"), 2)

    def test_one_debt_per_payment_method(self):
        for asset in ("ERG", "a" * 64):
            self.cursor.execute(
                "INSERT INTO donation_accrual (ledger, contract_hash, token_id, owed_native)"
                " VALUES ('ergo', 'c0ffee', ?, '1000')",
                (asset,),
            )
        self.assertEqual(
            self.cursor.execute("SELECT COUNT(*) FROM donation_accrual").fetchone()[0], 2
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.cursor.execute(
                "INSERT INTO donation_accrual (ledger, contract_hash, token_id, owed_native)"
                " VALUES ('ergo', 'c0ffee', 'ERG', '9999')"
            )

    def test_a_debt_keeps_its_fraction_through_the_column(self):
        # TEXT, and read back as text: a debt is stored exactly, fraction included, and
        # a float column would round the remainder away on every write.
        self.cursor.execute(
            "INSERT INTO donation_accrual (ledger, contract_hash, token_id, owed_native)"
            " VALUES ('ergo', 'c0ffee', 'ERG', '1200000.5')"
        )
        owed = self.cursor.execute("SELECT owed_native FROM donation_accrual").fetchone()[0]
        self.assertEqual(owed, "1200000.5")

    def test_the_scan_cursor_is_one_row_per_counted_address(self):
        self.cursor.execute(
            "INSERT INTO donation_scan_state (ledger, address, last_scanned_height)"
            " VALUES ('ergo', '9gGZ', 100)"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.cursor.execute(
                "INSERT INTO donation_scan_state (ledger, address, last_scanned_height)"
                " VALUES ('ergo', '9gGZ', 200)"
            )

    def test_a_payment_says_what_it_was_for_as_well_as_how_far_it_got(self):
        """`purpose` is orthogonal to `status`.

        A donation this node paid has a lifecycle like any other payment -- it was
        accepted or it was not -- so overloading `status` with 'donation' would cost
        the answer to the other question, and make every existing status filter learn
        a new value.
        """
        self.assertIn("purpose", TABLES["payments"])
        self.cursor.execute(
            "INSERT INTO payments (direction, status, amount_mu, purpose)"
            " VALUES ('out', 'accepted', '10000', 'donation')"
        )
        rows = self.cursor.execute(
            "SELECT status, purpose FROM payments WHERE purpose = 'donation'"
        ).fetchall()
        self.assertEqual(rows, [("accepted", "donation")])

    def test_an_existing_payments_table_gains_the_column_on_restart(self):
        # `CREATE TABLE IF NOT EXISTS` never alters a table that is already there, so
        # without this an in-place upgrade would fail to record every donation it pays.
        legacy = sqlite3.connect(":memory:")
        self.addCleanup(legacy.close)
        cursor = legacy.cursor()
        cursor.execute(
            "CREATE TABLE payments (id INTEGER PRIMARY KEY AUTOINCREMENT, direction TEXT,"
            " status TEXT, amount_mu TEXT)"
        )
        ensure_columns(cursor, "payments", {"purpose": "TEXT DEFAULT NULL"})
        columns = {row[1] for row in cursor.execute("PRAGMA table_info(payments)")}
        self.assertIn("purpose", columns)


if __name__ == "__main__":
    unittest.main()
