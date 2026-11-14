"""The payee states how many confirmations it needs, and a Payable too early is "not yet" (#626).

Before #626 a payer could not know the number, and a `Payable` that came before it set
the deposit token to 'rejected' for good: the transaction paid the node, and no client
got the MU. Between two nodes the payer waited for its *own* number, so a payee that
needs more refused the payment.

Three changes, tested here:

1. The Bitcoin declaration states ``payment.min_confirmations``. It is a payee
   parameter: two nodes that select different numbers still declare the same ledger.
2. A transaction that carries the token and pays enough, with fewer confirmations than
   the payee needs, raises `PaymentNotConfirmed`. The token stays 'pending', and the
   payer can call again.
3. A node that pays waits for the payee's number, read from the payee's stored
   announcement.

Only mocked chains here. Nothing is signed or sent.
"""
import sqlite3
import unittest
from decimal import Decimal
from unittest import mock

IMPORT_ERROR = None
try:
    from protos import celaut_pb2
    from src.identity.node_identity import parse_component_formal
    from src.payment_system import payee
    from src.payment_system.contracts.bitcoin import interface as btc
    from src.payment_system.contracts.bitcoin.backend import BackendUnavailable
    from src.payment_system.exceptions import NOT_CONFIRMED_YET, PaymentNotConfirmed
    from src.utils import ledger_descriptors
    from src.utils.bitcoin_units import script_pubkey_from_address
    from src.utils.contract_xattrs import set_contract_type, set_script, set_token_id
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

MANAGER_IMPORT_ERROR = None
try:
    from src.manager import manager
    from src.payment_system import payment_process
except Exception as import_exc:  # pragma: no cover - environment-dependent
    MANAGER_IMPORT_ERROR = import_exc

ADDRESS = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
TOKEN = "deposit-token-626"
KEY = "payment.min_confirmations"
TOO_EARLY = "payment.too_early"


def _with_min_confirmations(value):
    """``ledger_descriptors`` reading ``MIN_CONFIRMATIONS: value`` from the config."""
    real = ledger_descriptors.ConfigManager

    class _Config:
        def get(self, key, default=None):
            if key == "ledgers.bitcoin.payments.MIN_CONFIRMATIONS":
                return value
            return real().get(key, default)

    return mock.patch.object(ledger_descriptors, "ConfigManager", _Config)


def _declaration(min_confirmations):
    with _with_min_confirmations(min_confirmations):
        return ledger_descriptors.bitcoin_payment_ledger()


def _formal(ledger):
    return parse_component_formal(ledger.formal)


def _with_formal_line(ledger, line):
    """``ledger`` with its payee-parameter line replaced by ``line`` (b"" removes it)."""
    lines = [l for l in bytes(ledger.formal).split(b"\n") if not l.startswith(KEY.encode())]
    if line:
        lines.append(line)
    out = celaut_pb2.Contract.Ledger()
    out.CopyFrom(ledger)
    # Sorted by key, as `component_formal` sorts (not by the whole line).
    out.formal = b"\n".join(sorted(lines, key=lambda l: l.split(b"=", 1)[0]))
    return out


def _without_line(ledger, key):
    """``ledger`` without the formal line ``key``."""
    out = celaut_pb2.Contract.Ledger()
    out.CopyFrom(ledger)
    out.formal = b"\n".join(
        l for l in bytes(ledger.formal).split(b"\n") if not l.startswith(key.encode() + b"=")
    )
    return out


# ---------------------------------------------------------------------------------
# 1. The declaration
# ---------------------------------------------------------------------------------

@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DeclarationTests(unittest.TestCase):

    def test_the_bitcoin_declaration_states_the_number(self):
        self.assertEqual(_formal(_declaration(1))[KEY], "1")
        self.assertEqual(_formal(_declaration(3))[KEY], "3")
        self.assertIn("3 confirmations or more", _declaration(3).prose)
        self.assertIn(NOT_CONFIRMED_YET, _declaration(3).prose)

    def test_the_number_is_the_one_the_validator_uses(self):
        for configured, declared in ((None, "1"), ("", "1"), (0, "1"), (6, "6"), ("4", "4")):
            with _with_min_confirmations(configured):
                self.assertEqual(_formal(ledger_descriptors.bitcoin_payment_ledger())[KEY],
                                 declared)
                self.assertEqual(str(btc.MIN_CONFIRMATIONS()), declared)

    def test_the_formal_stays_canonical(self):
        keys = [l.split(b"=", 1)[0] for l in bytes(_declaration(3).formal).split(b"\n")]
        self.assertEqual(keys, sorted(keys))

    def test_ergo_states_no_payee_parameter(self):
        self.assertNotIn(KEY, _formal(ledger_descriptors.ergo_payment_ledger()))

    def test_two_numbers_are_the_same_ledger(self):
        self.assertNotEqual(_declaration(1).formal, _declaration(3).formal)
        self.assertTrue(ledger_descriptors.same_payment_ledger(_declaration(1), _declaration(3)))

    def test_a_node_from_before_626_is_the_same_ledger(self):
        old = _with_formal_line(_declaration(1), b"")
        self.assertNotIn(KEY, _formal(old))
        self.assertTrue(ledger_descriptors.same_payment_ledger(old, _declaration(3)))
        self.assertIsNone(ledger_descriptors.declared_min_confirmations(old))

    def test_the_too_early_rule_is_a_formal_payee_parameter(self):
        declaration = _declaration(3)
        self.assertIn(TOO_EARLY, _formal(declaration))
        self.assertIn(NOT_CONFIRMED_YET, _formal(declaration)[TOO_EARLY])
        self.assertIn(TOO_EARLY, ledger_descriptors.PAYEE_PARAMETERS)
        self.assertNotIn(TOO_EARLY, _formal(ledger_descriptors.ergo_payment_ledger()))

    def test_a_node_without_the_too_early_line_is_the_same_ledger(self):
        # A node from the first #626 release states the number but not this line.
        without = _without_line(_declaration(3), TOO_EARLY)
        self.assertNotIn(TOO_EARLY, _formal(without))
        self.assertIn(KEY, _formal(without))
        self.assertTrue(ledger_descriptors.same_payment_ledger(without, _declaration(1)))
        self.assertTrue(ledger_descriptors.same_payment_ledger(_declaration(1), without))
        # A node from before #626 has neither line.
        old = _without_line(without, KEY)
        self.assertTrue(ledger_descriptors.same_payment_ledger(old, _declaration(3)))

    def test_another_too_early_text_is_still_the_same_ledger(self):
        other = _declaration(3)
        other.formal = bytes(other.formal).replace(b"keeps the deposit token pending",
                                                   b"keeps the token")
        self.assertNotEqual(other.formal, _declaration(3).formal)
        self.assertTrue(ledger_descriptors.same_payment_ledger(other, _declaration(3)))

    def test_every_other_line_is_still_compared_byte_for_byte(self):
        other = _declaration(3)
        other.formal = bytes(other.formal).replace(b"network=mainnet", b"network=signet")
        self.assertFalse(ledger_descriptors.same_payment_ledger(other, _declaration(3)))
        empty = celaut_pb2.Contract.Ledger(tags=["bitcoin"])
        self.assertFalse(ledger_descriptors.same_payment_ledger(empty, _declaration(3)))

    def test_a_number_a_payer_cannot_wait_for_is_refused(self):
        for bad in (b"0", b"-1", b"abc", b"37", b""):
            ledger = _with_formal_line(_declaration(1), KEY.encode() + b"=" + bad)
            with self.subTest(value=bad):
                self.assertIsNotNone(ledger_descriptors.payee_parameters_reason(ledger))
        self.assertEqual(ledger_descriptors.declared_min_confirmations(_declaration(36)), 36)


@unittest.skipIf(
    IMPORT_ERROR is not None or MANAGER_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {IMPORT_ERROR or MANAGER_IMPORT_ERROR}",
)
class DeclarationMatchingTests(unittest.TestCase):
    """The three places a declaration is compared: store, Payable, `nodo protocol`."""

    def test_a_payee_with_another_number_is_stored(self):
        with _with_min_confirmations(1):
            self.assertTrue(manager._accept_contract(
                celaut_pb2.Contract(ledger=_declaration(3)), "peer-min-3"))

    def test_a_payee_from_before_626_is_stored(self):
        with _with_min_confirmations(1):
            self.assertTrue(manager._accept_contract(
                celaut_pb2.Contract(ledger=_with_formal_line(_declaration(1), b"")), "old"))

    def test_a_payee_with_an_unusable_number_is_not_stored(self):
        with _with_min_confirmations(1):
            for bad in (b"0", b"abc", b"1000"):
                ledger = _with_formal_line(_declaration(1), KEY.encode() + b"=" + bad)
                self.assertFalse(manager._accept_contract(celaut_pb2.Contract(ledger=ledger), "p"))

    def test_a_payer_with_another_number_pays_into_our_ledger(self):
        # On a node that needs 3, a Payable from a payer that needs 1 for itself.
        with _with_min_confirmations(3):
            self.assertEqual(payment_process._ledger_tag(_declaration(1)), "bitcoin")

    def test_nodo_protocol_reports_a_match(self):
        from src.commands import protocol

        peer = celaut_pb2.Peer()
        peer.payment_contracts.add().contract.ledger.CopyFrom(_declaration(3))
        with _with_min_confirmations(1):
            report = protocol._compare_ledgers(peer)
        self.assertEqual([r["status"] for r in report], ["match"])


# ---------------------------------------------------------------------------------
# 2. The validator
# ---------------------------------------------------------------------------------

def _tx(token=TOKEN, amount_sat=1_000):
    outputs = [{
        "script_hex": script_pubkey_from_address(ADDRESS).hex(),
        "value_sat": amount_sat,
        "op_return": None,
    }]
    if token is not None:
        outputs.append({
            "script_hex": "6a" + f"{len(token):02x}" + token.encode().hex(),
            "value_sat": 0,
            "op_return": token.encode("utf-8"),
        })
    return {"outputs": outputs}


class _Chain:
    """A wallet that knows how many confirmations each transaction has.

    ``list_received(address, n)`` answers like Core's ``listreceivedbyaddress``: the
    transactions with ``n`` confirmations or more, the mempool (0) included for ``n=0``.
    ``mine()`` adds one confirmation to every transaction.
    """

    def __init__(self, transactions):
        self.transactions = {tx_id: dict(tx) for tx_id, tx in transactions.items()}

    def mine(self, blocks=1):
        for tx in self.transactions.values():
            if tx["confirmations"] >= 0:
                tx["confirmations"] += blocks

    def list_received(self, address, min_conf):
        txids = [t for t, tx in self.transactions.items() if tx["confirmations"] >= min_conf]
        return [{"txids": txids}] if txids else []

    def raw_transaction(self, tx_id):
        return self.transactions[tx_id]["body"]

    def tx_status(self, tx_id):
        return {"confirmations": self.transactions[tx_id]["confirmations"]}


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ValidatorTests(unittest.TestCase):

    def _validate(self, chain, min_conf=3, amount_mu=1_000):
        with mock.patch.object(btc, "backend", return_value=chain), \
                mock.patch.object(btc, "get_wallet_address", return_value=ADDRESS), \
                mock.patch.object(btc.rate, "mu_per_satoshi", return_value=Decimal(1)), \
                mock.patch.object(btc, "NETWORK", lambda: "mainnet"), \
                mock.patch.object(btc, "MIN_CONFIRMATIONS", lambda: min_conf):
            return btc.payment_process_validator(
                amount=amount_mu, token=TOKEN, ledger="bitcoin",
                script=script_pubkey_from_address(ADDRESS),
            )

    def test_too_few_confirmations_is_not_yet_then_accepted(self):
        chain = _Chain({"tx-1": {"confirmations": 0, "body": _tx()}})
        for have in (0, 1, 2):
            with self.subTest(confirmations=have):
                with self.assertRaises(PaymentNotConfirmed) as caught:
                    self._validate(chain, min_conf=3)
                self.assertIn(NOT_CONFIRMED_YET, str(caught.exception))
                self.assertEqual(caught.exception.confirmations, have)
                self.assertEqual(caught.exception.wanted, 3)
                self.assertEqual(caught.exception.tx_id, "tx-1")
            chain.mine()
        self.assertTrue(self._validate(chain, min_conf=3))

    def test_an_unknown_token_is_still_no(self):
        chain = _Chain({
            "tx-1": {"confirmations": 0, "body": _tx(token="someone-elses")},
            "tx-2": {"confirmations": 5, "body": _tx(token="another")},
        })
        self.assertFalse(self._validate(chain))

    def test_no_transaction_at_all_is_still_no(self):
        self.assertFalse(self._validate(_Chain({})))

    def test_too_little_is_no_however_few_confirmations_it_has(self):
        # More confirmations will not make it pay more.
        chain = _Chain({"tx-1": {"confirmations": 1, "body": _tx(amount_sat=999)}})
        self.assertFalse(self._validate(chain))

    def test_a_replaced_transaction_is_no_not_not_yet(self):
        chain = _Chain({"tx-1": {"confirmations": -1, "body": _tx()}})
        chain.list_received = lambda address, n: [{"txids": ["tx-1"]}] if n == 0 else []
        self.assertFalse(self._validate(chain))

    def test_a_transaction_confirmed_between_the_two_reads_is_accepted(self):
        chain = _Chain({"tx-1": {"confirmations": 2, "body": _tx()}})
        real = chain.list_received

        def list_received(address, n):
            answer = real(address, n)
            if n > 0:
                chain.mine()  # The block arrives right after the first read.
            return answer

        chain.list_received = list_received
        self.assertTrue(self._validate(chain, min_conf=3))

    def test_unknown_confirmations_are_still_not_yet(self):
        chain = _Chain({"tx-1": {"confirmations": 0, "body": _tx()}})

        def tx_status(tx_id):
            raise BackendUnavailable("down")

        chain.tx_status = tx_status
        with self.assertRaises(PaymentNotConfirmed) as caught:
            self._validate(chain)
        self.assertIsNone(caught.exception.confirmations)

    def test_an_unreadable_mempool_is_the_old_no(self):
        chain = _Chain({"tx-1": {"confirmations": 0, "body": _tx()}})
        real = chain.list_received

        def list_received(address, n):
            if n == 0:
                raise BackendUnavailable("down")
            return real(address, n)

        chain.list_received = list_received
        self.assertFalse(self._validate(chain))

    def test_a_confirmed_payment_does_not_read_the_mempool(self):
        chain = mock.Mock(wraps=_Chain({"tx-1": {"confirmations": 3, "body": _tx()}}))
        self.assertTrue(self._validate(chain))
        self.assertEqual([c.args[1] for c in chain.list_received.call_args_list], [3])


@unittest.skipIf(
    IMPORT_ERROR is not None or MANAGER_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {IMPORT_ERROR or MANAGER_IMPORT_ERROR}",
)
class DepositTokenTests(unittest.TestCase):
    """What `validate_payment_process` does with the token: pending, then payed."""

    def setUp(self):
        from src.database import migrate
        from src.database.sql_connection import SQLConnection

        self.connection = sqlite3.connect(":memory:", check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        migrate.ensure_tables(self.connection.cursor(), ("deposit_tokens",))
        self.sql = SQLConnection()
        saved = SQLConnection._connection
        SQLConnection._connection = self.connection
        self.addCleanup(lambda: setattr(SQLConnection, "_connection", saved))
        self.sql.record_payment = mock.MagicMock()
        self.token = self.sql.add_deposit_token(account_id="client-1", status="pending")
        self.credits = []

    def _status(self):
        return self.connection.execute(
            "SELECT status FROM deposit_tokens WHERE id = ?", (self.token,)
        ).fetchone()["status"]

    def _payable(self, check):
        manager_module = mock.MagicMock()
        manager_module.increase_local_balance_for_client.side_effect = \
            lambda account_id, amount_mu, detail=None: self.credits.append(amount_mu) or True
        with mock.patch.object(payment_process, "sc", self.sql), \
                mock.patch.object(payment_process, "_manager_module",
                                  return_value=manager_module), \
                mock.patch.object(payment_process, "_donation_accrual"), \
                mock.patch.object(payment_process, "__check_payment_process",
                                  side_effect=check, create=True):
            return payment_process.validate_payment_process(
                amount=1000, ledger=_declaration(3), contract=b"p2wpkh",
                script=b"script", token=self.token,
            )

    def _not_yet(self, *args, **kwargs):
        raise PaymentNotConfirmed(token=self.token, tx_id="tx-1", confirmations=1, wanted=3)

    def test_too_early_keeps_the_token_pending_then_it_is_credited(self):
        with self.assertRaises(PaymentNotConfirmed):
            self._payable(self._not_yet)
        self.assertEqual(self._status(), "pending")
        self.sql.record_payment.assert_not_called()
        self.assertEqual(self.credits, [])

        # The payer calls again, later: the same token is still there to be paid.
        self.assertTrue(self._payable(lambda *a, **k: True))
        self.assertEqual(self._status(), "payed")
        self.assertEqual(self.credits, [1000])
        self.assertEqual(self.sql.record_payment.call_args.kwargs["status"], "accepted")

    def test_a_payment_that_is_not_there_is_still_rejected(self):
        self.assertFalse(self._payable(lambda *a, **k: False))
        self.assertEqual(self._status(), "rejected")
        self.assertEqual(self.sql.record_payment.call_args.kwargs["status"], "rejected")

    def test_the_claim_is_released_after_not_yet(self):
        with self.assertRaises(PaymentNotConfirmed):
            self._payable(self._not_yet)
        self.assertNotIn(self.token, payment_process._deposit_claims)

    def test_end_to_end_through_the_bitcoin_validator(self):
        """`validate_payment_process` → `__check_payment_process` → the Bitcoin validator."""
        from hashlib import sha3_256
        from src.payment_system.contracts.registry import MethodKey

        chain = _Chain({"tx-1": {"confirmations": 0, "body": _tx(token=self.token)}})
        key = MethodKey("bitcoin", sha3_256(b"p2wpkh").hexdigest(), "BTC")
        envs = mock.MagicMock()
        envs.payment_process_validators.return_value = {key: btc.payment_process_validator}
        manager_module = mock.MagicMock()
        manager_module.increase_local_balance_for_client.return_value = True
        self.sql.payable_account = mock.MagicMock(return_value=True)

        def payable():
            with mock.patch.object(payment_process, "sc", self.sql), \
                    mock.patch.object(payment_process, "_payment_envs", return_value=envs), \
                    mock.patch.object(payment_process, "_manager_module",
                                      return_value=manager_module), \
                    mock.patch.object(payment_process, "_donation_accrual"), \
                    mock.patch.object(btc, "backend", return_value=chain), \
                    mock.patch.object(btc, "get_wallet_address", return_value=ADDRESS), \
                    mock.patch.object(btc, "get_wallet_script",
                                      return_value=script_pubkey_from_address(ADDRESS)), \
                    mock.patch.object(btc.rate, "mu_per_satoshi", return_value=Decimal(1)), \
                    mock.patch.object(btc, "NETWORK", lambda: "mainnet"), \
                    mock.patch.object(btc, "MIN_CONFIRMATIONS", lambda: 3), \
                    _with_min_confirmations(3):
                return payment_process.validate_payment_process(
                    amount=1000, ledger=_declaration(1), contract=b"p2wpkh",
                    script=script_pubkey_from_address(ADDRESS), token=self.token,
                    asset="BTC",
                )

        for _ in range(3):  # 0, 1 and 2 confirmations
            with self.assertRaises(PaymentNotConfirmed):
                payable()
            self.assertEqual(self._status(), "pending")
            chain.mine()
        self.assertTrue(payable())
        self.assertEqual(self._status(), "payed")


@unittest.skipIf(
    IMPORT_ERROR is not None or MANAGER_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {IMPORT_ERROR or MANAGER_IMPORT_ERROR}",
)
class GatewayTests(unittest.TestCase):

    def test_payable_answers_failed_precondition_with_the_reason(self):
        import grpc
        from src.gateway import gateway

        class _Aborted(Exception):
            pass

        context = mock.MagicMock()
        context.abort.side_effect = _Aborted
        error = PaymentNotConfirmed(token=TOKEN, tx_id="tx-1", confirmations=0, wanted=3)
        from src.utils.utils import to_amount

        payment = celaut_pb2.Payment(deposit_token=TOKEN, amount=to_amount(1000))
        with mock.patch.object(gateway, "parse_with_client", return_value=(payment, "c")), \
                mock.patch.object(gateway, "require_caller"), \
                mock.patch.object(gateway, "validate_payment_process", side_effect=error):
            with self.assertRaises(_Aborted):
                list(gateway.Gateway.Payable(mock.MagicMock(), iter(()), context))
        code, message = context.abort.call_args.args
        self.assertEqual(code, grpc.StatusCode.FAILED_PRECONDITION)
        self.assertIn(NOT_CONFIRMED_YET, message)


# ---------------------------------------------------------------------------------
# 3. Node to node: the payer waits for the payee's number
# ---------------------------------------------------------------------------------

def _peer_announcing(ledger):
    contract = celaut_pb2.Contract(ledger=ledger)
    set_token_id(contract, "BTC")
    set_script(contract, script_pubkey_from_address(ADDRESS))
    set_contract_type(contract, b"p2wpkh")
    peer = celaut_pb2.Peer()
    peer.payment_contracts.add(contract=contract)
    return peer


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PayerWaitTests(unittest.TestCase):

    def _await(self, payee_ledger, own=1, statuses=range(0, 10)):
        chain = mock.Mock()
        chain.tx_status.side_effect = [{"confirmations": c} for c in statuses]
        with mock.patch.object(btc, "MIN_CONFIRMATIONS", lambda: own), \
                mock.patch.object(btc, "WAIT_TX_ATTEMPTS", len(list(statuses))), \
                mock.patch.object(btc, "sleep", lambda _: None), \
                payee.paying(payee_ledger):
            btc.await_payment(tx_id="tx-1", script=script_pubkey_from_address(ADDRESS),
                              chain=chain)
        return chain.tx_status.call_count - 1  # confirmations when it stopped

    def test_a_payer_that_needs_1_waits_for_a_payee_that_needs_3(self):
        self.assertEqual(self._await(_declaration(3), own=1), 3)

    def test_a_payer_that_needs_6_does_not_wait_longer_than_a_payee_that_needs_2(self):
        self.assertEqual(self._await(_declaration(2), own=6), 2)

    def test_a_payee_from_before_626_gets_our_own_number(self):
        old = _with_formal_line(_declaration(1), b"")
        self.assertEqual(self._await(old, own=4), 4)

    def test_no_known_payee_gets_our_own_number(self):
        self.assertEqual(self._await(None, own=2), 2)

    def test_an_unusable_number_gets_our_own_number(self):
        bad = _with_formal_line(_declaration(1), KEY.encode() + b"=many")
        self.assertEqual(self._await(bad, own=2), 2)

    def test_the_payee_is_only_in_force_inside_the_block(self):
        with payee.paying(_declaration(5)):
            self.assertEqual(payee.ledger().formal, _declaration(5).formal)
        self.assertIsNone(payee.ledger())

    def test_the_payee_ledger_is_read_from_its_stored_announcement(self):
        from hashlib import sha3_256

        connection = mock.MagicMock()
        connection.get_peer_advertisement.return_value = \
            _peer_announcing(_declaration(3)).SerializeToString()
        found = payee.declared_ledger("peer-1", "bitcoin",
                                      sha3_256(b"p2wpkh").hexdigest(), connection=connection)
        self.assertEqual(ledger_descriptors.declared_min_confirmations(found), 3)
        self.assertIsNone(payee.declared_ledger("peer-1", "ergo", "", connection=connection))
        self.assertIsNone(payee.declared_ledger("peer-1", "bitcoin", "00" * 32,
                                                connection=connection))
        connection.get_peer_advertisement.return_value = None
        self.assertIsNone(payee.declared_ledger("peer-1", "bitcoin", "", connection=connection))


@unittest.skipIf(
    IMPORT_ERROR is not None or MANAGER_IMPORT_ERROR is not None,
    f"Missing runtime dependencies: {IMPORT_ERROR or MANAGER_IMPORT_ERROR}",
)
class PayerFlowTests(unittest.TestCase):

    def test_the_payment_process_gives_the_payee_declaration_to_the_contract(self):
        from contextlib import nullcontext
        from hashlib import sha3_256
        from src.payment_system.contracts.registry import MethodKey

        contract_hash = sha3_256(b"p2wpkh").hexdigest()
        method = MethodKey("bitcoin", contract_hash, "BTC")
        seen = []

        def process_payment(amount, deposit_token, ledger, script):
            seen.append(ledger_descriptors.declared_min_confirmations(payee.ledger()))
            return celaut_pb2.Contract(ledger=_declaration(1))

        envs = mock.MagicMock()
        envs.available_payment_process.return_value = {method: process_payment}
        envs.check_sender_balances.return_value = {method: lambda amount: True}
        envs.transaction_url_reporting.side_effect = lambda *a, **k: nullcontext()
        envs.transaction_id_reporting.side_effect = lambda *a, **k: nullcontext()
        connection = mock.MagicMock()
        connection.get_peer_advertisement.return_value = \
            _peer_announcing(_declaration(3)).SerializeToString()
        script = script_pubkey_from_address(ADDRESS)
        with mock.patch.object(payment_process, "_payment_envs", return_value=envs), \
                mock.patch.object(payment_process, "sc", connection), \
                mock.patch.object(payment_process, "get_peer_contract_instances",
                                  return_value=iter([(script, "bitcoin", "BTC")])), \
                mock.patch.object(payment_process, "ledger_balancer",
                                  side_effect=lambda ledger_generator: ledger_generator), \
                mock.patch.object(payment_process, "_reputation_interface"), \
                mock.patch.object(payment_process, "__obtain_deposit_token",
                                  return_value=TOKEN, create=True), \
                mock.patch.object(payment_process, "__attempt_payment_communication",
                                  return_value=True, create=True):
            plan = getattr(payment_process, "__peer_payment_process")(
                peer_id="peer-min-3",
                plans=[payment_process.SettlementPlan(
                    contract_hash=contract_hash, ledger_tag="bitcoin", asset="BTC",
                    amount=1000, peer_amount=1000,
                )],
            )
        self.assertIsNotNone(plan)
        self.assertEqual(seen, [3])
        self.assertIsNone(payee.ledger())

    def _communicate(self, answers, attempts=1):
        channel = mock.MagicMock()
        calls = []

        def payable(*args, **kwargs):
            calls.append(1)
            answer = answers[len(calls) - 1]
            if isinstance(answer, Exception):
                raise answer

        reputation = mock.MagicMock()
        manager_module = mock.MagicMock()
        manager_module.get_client_id_on_other_peer.return_value = "client-at-peer"
        with mock.patch.object(payment_process, "__get_channel", return_value=channel,
                               create=True), \
                mock.patch.object(payment_process, "_manager_module",
                                  return_value=manager_module), \
                mock.patch.object(payment_process.BeeClient, "payable", side_effect=payable), \
                mock.patch.object(payment_process, "_reputation_interface",
                                  return_value=reputation), \
                mock.patch.object(payment_process, "sleep"), \
                mock.patch.object(payment_process, "COMMUNICATION_ATTEMPTS", attempts):
            told = getattr(payment_process, "__attempt_payment_communication")(
                "peer-1", 1000, TOKEN, celaut_pb2.Contract(ledger=_declaration(1)))
        return told, len(calls), reputation

    def test_a_payee_one_block_behind_is_asked_again_without_a_penalty(self):
        behind = Exception("Payment not confirmed yet: transaction tx-1 has 2 ...")
        told, calls, reputation = self._communicate([behind, behind, None])
        self.assertTrue(told)
        self.assertEqual(calls, 3)
        reputation.update_peer_reputation.assert_not_called()

    def test_not_yet_is_asked_again_only_so_many_times(self):
        behind = Exception("Payment not confirmed yet")
        retries = payment_process.NOT_CONFIRMED_RETRIES
        told, calls, reputation = self._communicate([behind] * (retries + 1))
        self.assertFalse(told)
        self.assertEqual(calls, retries + 1)
        self.assertEqual(reputation.update_peer_reputation.call_count, 1)

    def test_another_error_is_still_an_attempt(self):
        told, calls, reputation = self._communicate([Exception("payment not valid")])
        self.assertFalse(told)
        self.assertEqual(calls, 1)
        reputation.update_peer_reputation.assert_called_once()


if __name__ == "__main__":
    unittest.main()
