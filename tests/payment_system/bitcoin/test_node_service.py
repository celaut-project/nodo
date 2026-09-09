"""The bitcoind this node runs itself: what it is launched with, and when.

Bitcoin Core is what signs a Bitcoin transaction — nodo builds no raw ones — so being
able to *pay* in BTC has always meant having a Core with the keys. This backend makes
that Core infrastructure the node brings up, with its wallet derived from a mnemonic the
node holds, so the operator backs up one phrase the way they already do for Ergo.

Two properties carry the design, and both are about *where* things happen:

* **Attaching is not launching.** `backend()` is called per payment and per
  advertisement; launching a bitcoind there would hold a payment for as long as a
  service download takes. Launching belongs to `prepare()`, which the contract calls at
  boot and on its periodic tick.
* **The credentials are the ones nodo passes in.** Core's cookie lives inside the
  service's own filesystem, unreadable from here — and a stale cookie left on this host
  by some other node would authenticate against the wrong wallet, which is worse than
  failing.
"""
import unittest
from unittest import mock

IMPORT_ERROR = None
try:
    from src.payment_system.contracts.bitcoin import node_service
    from src.payment_system.contracts.bitcoin.backend import BackendUnavailable
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    node_service = None  # type: ignore[assignment]

SERVICE_ID = "a1" * 32
ENDPOINT = "http://10.0.0.7:8332"

CONFIGURED = {
    "ledgers.bitcoin.WALLET_MNEMONIC": "twelve words that are not really twelve words",
    "ledgers.bitcoin.NETWORK": "mainnet",
    "ledgers.bitcoin.RPC_USER": "nodo",
    "ledgers.bitcoin.RPC_PASSWORD": "hunter2",
    "ledgers.bitcoin.WALLET_NAME": "nodo",
    "ledgers.bitcoin.PRUNE_MIB": 10000,
}


class _Config:
    def __init__(self, values):
        self._values = values

    def get(self, key, default=None):
        return self._values.get(key, default)


def _with(values=None, service_id=SERVICE_ID):
    """The module's two sources of truth: the config, and the core-service id."""
    resolved = dict(CONFIGURED)
    if values is not None:
        resolved.update(values)
    return (
        mock.patch.object(node_service, "ConfigManager", lambda: _Config(resolved)),
        mock.patch("src.core_services.get_core_service_id",
                   return_value=service_id),
    )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class LaunchEnvironmentTests(unittest.TestCase):
    def _envs(self, values=None):
        config, service = _with(values)
        with config, service:
            return node_service.launch_envs()

    def test_the_wallet_the_network_and_the_credentials_are_passed(self):
        envs = self._envs()
        self.assertEqual(envs["BITCOIN_MNEMONIC"], CONFIGURED["ledgers.bitcoin.WALLET_MNEMONIC"])
        self.assertEqual(envs["BITCOIN_NETWORK"], "mainnet")
        self.assertEqual(envs["BITCOIN_RPC_USER"], "nodo")
        self.assertEqual(envs["BITCOIN_RPC_PASSWORD"], "hunter2")
        self.assertEqual(envs["BITCOIN_WALLET_NAME"], "nodo")
        self.assertEqual(envs["BITCOIN_PRUNE"], "10000")

    def test_an_unset_optional_is_left_out_rather_than_passed_empty(self):
        # An unset BIP-39 passphrase and one set to "" are different wallets, so the
        # service must be able to tell the two apart.
        self.assertNotIn("BITCOIN_MNEMONIC_PASSPHRASE", self._envs())
        envs = self._envs({"ledgers.bitcoin.WALLET_PASSPHRASE": "a second secret"})
        self.assertEqual(envs["BITCOIN_MNEMONIC_PASSPHRASE"], "a second secret")

    def test_no_mnemonic_launches_nothing(self):
        # A bitcoind with no wallet is a bitcoind that cannot sign, which is the whole
        # reason to run one.
        self.assertIsNone(self._envs({"ledgers.bitcoin.WALLET_MNEMONIC": ""}))

    def test_no_credentials_launches_nothing(self):
        # They are what nodo will authenticate with; a service the node cannot talk to
        # is worse than no service, because it looks like one.
        self.assertIsNone(self._envs({"ledgers.bitcoin.RPC_PASSWORD": ""}))
        self.assertIsNone(self._envs({"ledgers.bitcoin.RPC_USER": ""}))

    def test_every_declared_env_names_a_config_key_that_is_read(self):
        # The table is the contract with the published service. A name in it that maps
        # to a key nothing sets is a service that comes up misconfigured in a way only
        # its own log would show.
        for name, key in node_service.ENVIRONMENT.items():
            with self.subTest(env=name):
                self.assertTrue(key.startswith("ledgers.bitcoin."), key)
        for name in node_service.REQUIRED_ENVIRONMENT:
            self.assertIn(name, node_service.ENVIRONMENT)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class AttachAndLaunchTests(unittest.TestCase):
    def _run(self, call, *, running=ENDPOINT, values=None, service_id=SERVICE_ID):
        """Both ways of reaching an instance, recorded so a test can tell them apart.

        `find_running_endpoint` reads the local instances table and nothing else;
        `ensure_core_service_running` downloads and launches. Which one a code path
        takes is the property most of these tests are about.
        """
        calls = []

        def ensure(service, *, launch=True, envs=None, source_url=None):
            calls.append({"service": service, "launch": launch, "envs": envs})
            return running

        def find(service):
            calls.append({"service": service, "launch": False, "envs": None})
            return running

        config, service = _with(values, service_id)
        with config, service, mock.patch(
            "src.core_services.runtime.ensure_core_service_running", side_effect=ensure
        ), mock.patch(
            "src.core_services.runtime.find_running_endpoint", side_effect=find
        ):
            result = call()
        return result, calls

    def test_the_payment_path_attaches_and_never_launches(self):
        # A service download on the payment path holds the payment for as long as it
        # takes; the tick is where a node is allowed to wait.
        _chain, calls = self._run(node_service.backend)
        self.assertEqual([call["launch"] for call in calls], [False])
        self.assertIsNone(calls[0]["envs"], "no wallet is handed over just to attach")

    def test_attaching_reads_the_local_table_and_makes_no_network_call(self):
        """`launch=False` is not enough on its own, which is the subtlety here.

        `ensure_core_service_running(launch=False)` still tries to *download* the
        service before giving up -- a source-application round trip, per payment and per
        advertisement, for as long as the service happens to be down. The attach path
        goes straight to the instances table instead.
        """
        with mock.patch(
            "src.core_services.runtime.ensure_core_service_running"
        ) as ensure, mock.patch(
            "src.core_services.runtime.find_running_endpoint", return_value=ENDPOINT
        ) as find:
            config, service = _with()
            with config, service:
                node_service.backend()
        find.assert_called_once()
        ensure.assert_not_called()

    def test_prepare_launches_with_the_wallet(self):
        endpoint, calls = self._run(node_service.prepare)
        self.assertEqual(endpoint, ENDPOINT)
        self.assertEqual([call["launch"] for call in calls], [True])
        self.assertEqual(calls[0]["envs"]["BITCOIN_MNEMONIC"],
                         CONFIGURED["ledgers.bitcoin.WALLET_MNEMONIC"])

    def test_prepare_launches_nothing_without_a_wallet_to_give_it(self):
        endpoint, calls = self._run(
            node_service.prepare, values={"ledgers.bitcoin.WALLET_MNEMONIC": ""}
        )
        self.assertIsNone(endpoint)
        self.assertEqual(calls, [])

    def test_a_service_that_is_not_running_is_said_so_rather_than_started(self):
        with self.assertRaisesRegex(BackendUnavailable, "not running yet"):
            self._run(node_service.backend, running=None)

    def test_no_configured_service_id_is_a_different_message(self):
        # "You have not configured this" and "it is not up yet" are different problems
        # with different fixes.
        with self.assertRaisesRegex(BackendUnavailable, "core_services.bitcoin-node"):
            self._run(node_service.backend, service_id=None)

    def test_the_backend_authenticates_with_what_the_service_was_given(self):
        from base64 import b64encode

        chain, _calls = self._run(node_service.backend)
        expected = b64encode(b"nodo:hunter2").decode()
        # Read off the built client rather than re-derived: what matters is that the
        # header nodo sends is the credential the service was launched with.
        self.assertEqual(chain._auth_header, f"Basic {expected}")
        self.assertEqual(chain._url, ENDPOINT)
        self.assertEqual(chain._wallet, "nodo")

    def test_the_cookie_file_is_never_used_for_this_backend(self):
        # Core writes it inside the service, where nodo cannot read it -- and a stale
        # `~/.bitcoin/.cookie` from another node would authenticate against the wrong
        # wallet. Missing credentials must fail rather than fall back to it.
        with self.assertRaisesRegex(BackendUnavailable, "RPC_USER and RPC_PASSWORD"):
            self._run(node_service.backend,
                      values={"ledgers.bitcoin.RPC_PASSWORD": "",
                              "ledgers.bitcoin.RPC_COOKIE_PATH": "/tmp/.cookie"})

    def test_this_backend_can_pay(self):
        # The point of running a Core at all: it holds the keys, so a sweep to cold
        # storage and a donation payout can be signed.
        self.assertTrue(node_service.can_pay)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ConfigurationReasonTests(unittest.TestCase):
    """Config only: the registry asks this on the payment path and per advertisement."""

    def _reason(self, values=None, service_id=SERVICE_ID):
        config, service = _with(values, service_id)
        with config, service:
            return node_service.configuration_reason()

    def test_a_complete_configuration_has_no_reason(self):
        self.assertIsNone(self._reason())

    def test_an_unconfigured_service_id_says_which_key(self):
        self.assertIn("core_services.bitcoin-node", self._reason(service_id=None))

    def test_a_missing_value_is_named_by_its_config_key(self):
        reason = self._reason({"ledgers.bitcoin.WALLET_MNEMONIC": ""})
        self.assertIn("ledgers.bitcoin.WALLET_MNEMONIC", reason)

    def test_a_service_that_is_merely_not_running_is_not_a_reason(self):
        """Deliberately: the node brings it up itself.

        Answering "not running" here would drop Bitcoin from this node's advertisement
        between two ticks — making it unpayable for a reason its own operator cannot
        see, and un-advertising a contract that is about to work.
        """
        with mock.patch(
            "src.core_services.runtime.ensure_core_service_running", return_value=None
        ):
            self.assertIsNone(self._reason())


if __name__ == "__main__":
    unittest.main()
