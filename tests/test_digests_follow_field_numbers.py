"""The digest of a statement and of an offer follows the numbers of the fields.

Each text is written here by hand, part by part, in the order of the numbers of the fields
of ``celaut.proto``. Another implementation can reproduce a digest from the message and
this list only. A test also fails when a field of the message does not move the digest.
"""
import hashlib
import struct
import unittest

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.identity import statement_signature
    from src.utils.cost_functions import signed_offer
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc


def _hex(text):
    return text.encode("utf-8").hex()


def _blake(text):
    return hashlib.blake2b(text.encode("utf-8"), digest_size=32).hexdigest()


def _statement():
    statement = celaut_pb2.BalanceStatement(
        ts=1798329600, relayed=True, account_ref="aa" * 32, service_id="svc",
        instance_ref="bb" * 32, from_seq=3, to_seq=9, head_seq=12, head_hash="cc" * 32,
        oldest_seq=2, retention_days=30, signature="ignored",
    )
    statement.balance.n = "5000"
    return statement


def _add_movement(statement):
    movement = statement.movements.add()
    movement.seq = 1
    movement.amount.n = "10"
    movement.balance_after.n = "10"
    movement.kind = "start"


def _offer():
    offer = celaut_pb2.EstimatedCost(
        maintenance_seconds_loop=60, price_bounded=True, price_volatility=0.5,
        volatility_period_seconds=3600, valid_until=1798333200, closes_at=1798340000,
        on_close=celaut_pb2.ON_CLOSE_STOP, signature="ignored",
    )
    offer.cost.n = "100"
    offer.init_maintenance_cost.n = "7"
    offer.max_maintenance_cost.n = "9"
    entry = offer.launch_scarcity.add()
    entry.key = "ram"
    entry.value = 250000
    return offer


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StatementDigestTests(unittest.TestCase):

    def test_the_text_is_in_the_order_of_the_numbers_of_the_fields(self):
        statement = _statement()
        expected = "|".join((
            "1798329600",                    # ts = 1
            "1",                             # relayed = 3
            "aa" * 32,                       # account_ref = 4
            _hex("svc"),                     # service_id = 5
            _hex("bb" * 32),                 # instance_ref = 6
            "3", "9",                        # from_seq = 7, to_seq = 8
            "",                              # movements = 9 (none)
            _hex("5000"),                    # balance = 10
            "12", "cc" * 32,                 # head_seq = 11, head_hash = 12
            "2", "30",                       # oldest_seq = 13, retention_days = 14
        ))
        self.assertEqual(statement_signature.statement_digest(statement), _blake(expected))

    def test_the_movements_are_the_hash_of_each_one_joined_by_a_slash(self):
        statement = _statement()
        for seq in (1, 2):
            movement = statement.movements.add()
            movement.seq = seq
            movement.ts = 1798329000 + seq
            movement.amount.n = "10"
            movement.balance_after.n = str(100 * seq)
            movement.kind = "start"
            movement.prev_hash = "dd" * 32
        hashes = "/".join(statement_signature.movement_hash(m) for m in statement.movements)
        expected = "|".join((
            "1798329600", "1", "aa" * 32, _hex("svc"), _hex("bb" * 32), "3", "9", hashes,
            _hex("5000"), "12", "cc" * 32, "2", "30",
        ))
        self.assertEqual(statement_signature.statement_digest(statement), _blake(expected))

    def test_an_empty_optional_part_is_empty_and_keeps_its_place(self):
        statement = _statement()
        statement.ClearField("service_id")
        statement.ClearField("instance_ref")
        statement.relayed = False
        expected = "|".join((
            "1798329600", "0", "aa" * 32, "", "", "3", "9", "", _hex("5000"), "12",
            "cc" * 32, "2", "30",
        ))
        self.assertEqual(statement_signature.statement_digest(statement), _blake(expected))

    def test_every_field_but_the_signature_moves_the_digest(self):
        base = statement_signature.statement_digest(_statement())
        changes = {
            "ts": lambda s: setattr(s, "ts", s.ts + 1),
            "relayed": lambda s: setattr(s, "relayed", not s.relayed),
            "account_ref": lambda s: setattr(s, "account_ref", "ee" * 32),
            "service_id": lambda s: setattr(s, "service_id", "other"),
            "instance_ref": lambda s: setattr(s, "instance_ref", "ff" * 32),
            "from_seq": lambda s: setattr(s, "from_seq", 4),
            "to_seq": lambda s: setattr(s, "to_seq", 10),
            "movements": _add_movement,
            "balance": lambda s: setattr(s.balance, "n", "5001"),
            "head_seq": lambda s: setattr(s, "head_seq", 13),
            "head_hash": lambda s: setattr(s, "head_hash", "ee" * 32),
            "oldest_seq": lambda s: setattr(s, "oldest_seq", 3),
            "retention_days": lambda s: setattr(s, "retention_days", 31),
        }
        fields = {f.name for f in celaut_pb2.BalanceStatement.DESCRIPTOR.fields}
        self.assertEqual(fields - {"signature"}, set(changes))
        for name, change in changes.items():
            with self.subTest(field=name):
                statement = _statement()
                change(statement)
                self.assertNotEqual(statement_signature.statement_digest(statement), base)
        statement = _statement()
        statement.signature = "another"
        self.assertEqual(statement_signature.statement_digest(statement), base)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class OfferDigestTests(unittest.TestCase):

    def test_the_text_is_in_the_order_of_the_numbers_of_the_fields(self):
        expected = "|".join((
            _hex("100"), _hex("7"), _hex("9"),      # cost 1, init 2, max 3
            "60",                                   # maintenance_seconds_loop = 4
            "ram=250000",                           # launch_scarcity = 5
            "1",                                    # price_bounded = 6
            struct.pack(">f", 0.5).hex(),           # price_volatility = 7
            "3600",                                 # volatility_period_seconds = 8
            "1798333200", "1798340000",             # valid_until = 9, closes_at = 10
            str(int(celaut_pb2.ON_CLOSE_STOP)),     # on_close = 11
        ))
        self.assertEqual(signed_offer.offer_digest(_offer()), _blake(expected))

    def test_the_load_is_sorted_by_key_and_joined_by_a_semicolon(self):
        offer = _offer()
        extra = offer.launch_scarcity.add()
        extra.key = "cpu"
        extra.value = 1
        expected = "|".join((
            _hex("100"), _hex("7"), _hex("9"), "60", "cpu=1;ram=250000", "1",
            struct.pack(">f", 0.5).hex(), "3600", "1798333200", "1798340000",
            str(int(celaut_pb2.ON_CLOSE_STOP)),
        ))
        self.assertEqual(signed_offer.offer_digest(offer), _blake(expected))

    def test_every_field_but_the_signature_moves_the_digest(self):
        base = signed_offer.offer_digest(_offer())
        changes = {
            "cost": lambda o: setattr(o.cost, "n", "101"),
            "init_maintenance_cost": lambda o: setattr(o.init_maintenance_cost, "n", "8"),
            "max_maintenance_cost": lambda o: setattr(o.max_maintenance_cost, "n", "10"),
            "maintenance_seconds_loop": lambda o: setattr(o, "maintenance_seconds_loop", 61),
            "launch_scarcity": lambda o: setattr(o.launch_scarcity[0], "value", 1),
            "price_bounded": lambda o: setattr(o, "price_bounded", False),
            "price_volatility": lambda o: setattr(o, "price_volatility", 0.25),
            "volatility_period_seconds": lambda o: setattr(o, "volatility_period_seconds", 1),
            "valid_until": lambda o: setattr(o, "valid_until", o.valid_until + 1),
            "closes_at": lambda o: setattr(o, "closes_at", o.closes_at + 1),
            "on_close": lambda o: setattr(o, "on_close", celaut_pb2.ON_CLOSE_STOP + 1),
        }
        fields = {f.name for f in celaut_pb2.EstimatedCost.DESCRIPTOR.fields}
        self.assertEqual(fields - {"signature"}, set(changes))
        for name, change in changes.items():
            with self.subTest(field=name):
                offer = _offer()
                change(offer)
                self.assertNotEqual(signed_offer.offer_digest(offer), base)


if __name__ == "__main__":
    unittest.main()
