"""GenerateClient costs work once a node has given away its free clients (issue #361).

The properties under test are the ones that make the mechanism a defence rather than a
formality: the node stores nothing between issuing a challenge and validating it, the
caller cannot lower the difficulty it was set, an already-taken `client_id` is refused
before a single hash is computed, and a challenge stays valid while the node's global
difficulty moves underneath it.

Difficulties here are kept at 1 or 2. A test is not a benchmark: each extra zero is 16x
the hashing, and the rule being checked is identical at every level.
"""
import unittest
from unittest.mock import patch
from uuid import uuid4

IMPORT_ERROR = None
try:
    from src.gateway import client_pow
    from src.gateway.client_pow import (
        POW_FORMAL,
        POW_PROSE,
        POW_TAGS,
        PoWError,
        current_difficulty,
        is_uuid4_hex,
        make_challenge,
        parse_and_verify_challenge,
        pow_digest,
        solve_pow,
        verify_solution,
    )
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc

SECRET = b"a server secret, known only here"
OTHER_SECRET = b"some other node's server secret."


def _client_id() -> str:
    return uuid4().hex


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DifficultyTests(unittest.TestCase):
    """The table in the issue: one step per MAX_WORK_FREE_CLIENTS_PER_DIFFICULTY."""

    def test_the_first_block_of_clients_is_free(self):
        self.assertEqual(current_difficulty(0, 500), 0)
        self.assertEqual(current_difficulty(499, 500), 0)

    def test_difficulty_steps_up_at_each_multiple(self):
        self.assertEqual(current_difficulty(500, 500), 1)
        self.assertEqual(current_difficulty(999, 500), 1)
        self.assertEqual(current_difficulty(1000, 500), 2)
        self.assertEqual(current_difficulty(1500, 500), 3)

    def test_the_step_size_is_configurable(self):
        self.assertEqual(current_difficulty(500, 1000), 0)
        self.assertEqual(current_difficulty(1000, 1000), 1)

    def test_a_step_of_zero_is_refused_rather_than_meaning_free_forever(self):
        # It is a divisor. Treating 0 as "unlimited" would be inventing a meaning.
        with self.assertRaises(ValueError):
            current_difficulty(1000, 0)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class StaticFieldsTests(unittest.TestCase):
    """`tags`, `prose` and `formal` are fixed strings the issue spells out exactly."""

    def test_tags(self):
        self.assertEqual(POW_TAGS, ["blake2b"])

    def test_prose(self):
        self.assertEqual(
            POW_PROSE,
            "Find a solution such that Blake2b(challenge + solution) ends with N zero "
            "characters, where N is the difficulty.",
        )

    def test_formal(self):
        self.assertEqual(
            POW_FORMAL,
            b'Blake2b(challenge || solution).hexdigest().endswith("0" * difficulty)',
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ChallengeHmacTests(unittest.TestCase):
    """Nothing in a challenge may be changed by the caller holding it."""

    def setUp(self):
        self.client_id = _client_id()
        self.challenge = make_challenge(
            client_id=self.client_id, difficulty=2, secret=SECRET, nonce="00" * 16
        )

    def test_a_challenge_this_node_issued_is_accepted_and_gives_its_fields_back(self):
        client_id, nonce, difficulty = parse_and_verify_challenge(self.challenge, SECRET)
        self.assertEqual(client_id, self.client_id)
        self.assertEqual(nonce, "00" * 16)
        self.assertEqual(difficulty, 2)

    def test_a_modified_client_id_invalidates_the_mac(self):
        _, nonce, difficulty, mac = self.challenge.split(":")
        forged = ":".join((_client_id(), nonce, difficulty, mac))
        with self.assertRaises(PoWError):
            parse_and_verify_challenge(forged, SECRET)

    def test_a_modified_nonce_invalidates_the_mac(self):
        client_id, _, difficulty, mac = self.challenge.split(":")
        forged = ":".join((client_id, "11" * 16, difficulty, mac))
        with self.assertRaises(PoWError):
            parse_and_verify_challenge(forged, SECRET)

    def test_a_lowered_difficulty_invalidates_the_mac(self):
        # The attack the HMAC exists for: solve difficulty 1 and claim it was asked for.
        client_id, nonce, _, mac = self.challenge.split(":")
        forged = ":".join((client_id, nonce, "1", mac))
        with self.assertRaises(PoWError):
            parse_and_verify_challenge(forged, SECRET)

    def test_a_modified_mac_is_rejected(self):
        client_id, nonce, difficulty, mac = self.challenge.split(":")
        flipped = ("0" if mac[0] != "0" else "1") + mac[1:]
        with self.assertRaises(PoWError):
            parse_and_verify_challenge(":".join((client_id, nonce, difficulty, flipped)), SECRET)

    def test_another_nodes_challenge_is_rejected(self):
        with self.assertRaises(PoWError):
            parse_and_verify_challenge(self.challenge, OTHER_SECRET)

    def test_a_malformed_challenge_is_rejected_rather_than_crashing(self):
        for bad in ("", "not-a-challenge", "a:b:c", "a:b:c:d:e", f"{self.client_id}:n:x:mac"):
            with self.subTest(bad=bad), self.assertRaises(PoWError):
                parse_and_verify_challenge(bad, SECRET)

    def test_two_challenges_for_the_same_client_differ(self):
        # The nonce is what makes them differ; a fixed one would make the work reusable.
        a = make_challenge(client_id=self.client_id, difficulty=1, secret=SECRET)
        b = make_challenge(client_id=self.client_id, difficulty=1, secret=SECRET)
        self.assertNotEqual(a, b)

    def test_a_challenge_cannot_be_issued_for_a_client_id_that_is_not_a_uuid4(self):
        with self.assertRaises(PoWError):
            make_challenge(client_id="not-a-uuid", difficulty=1, secret=SECRET)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class SolutionTests(unittest.TestCase):
    """The rule is exactly the one `POW_FORMAL` states."""

    def setUp(self):
        self.challenge = make_challenge(
            client_id=_client_id(), difficulty=2, secret=SECRET
        )

    def test_a_valid_solution_is_accepted(self):
        solution = solve_pow(self.challenge, 2)
        self.assertTrue(verify_solution(self.challenge, solution, 2))
        self.assertTrue(pow_digest(self.challenge, solution).endswith("00"))

    def test_an_invalid_solution_is_rejected(self):
        solution = solve_pow(self.challenge, 2)
        self.assertFalse(verify_solution(self.challenge, solution + "x", 2))

    def test_a_solution_for_a_lower_difficulty_is_rejected(self):
        easy = solve_pow(self.challenge, 1)
        self.assertTrue(verify_solution(self.challenge, easy, 1))
        # Only counts as difficulty 2 if it happened to earn a second zero.
        if not pow_digest(self.challenge, easy).endswith("00"):
            self.assertFalse(verify_solution(self.challenge, easy, 2))

    def test_a_solution_to_another_challenge_is_rejected(self):
        other = make_challenge(client_id=_client_id(), difficulty=2, secret=SECRET)
        self.assertFalse(verify_solution(self.challenge, solve_pow(other, 2), 2))

    def test_difficulty_zero_needs_no_solution(self):
        self.assertEqual(solve_pow(self.challenge, 0), "")
        self.assertTrue(verify_solution(self.challenge, "", 0))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ClientIdShapeTests(unittest.TestCase):
    def test_a_uuid4_hex_is_accepted(self):
        self.assertTrue(is_uuid4_hex(uuid4().hex))

    def test_anything_else_is_not(self):
        for bad in ("", "x" * 32, uuid4().hex[:31], uuid4().hex + "0", str(uuid4()),
                    uuid4().hex.upper(), None):
            with self.subTest(bad=bad):
                self.assertFalse(is_uuid4_hex(bad))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ServerSecretTests(unittest.TestCase):
    def test_the_secret_is_derived_from_the_node_identity_so_it_survives_a_restart(self):
        with patch.object(client_pow, "get_identity_mnemonic", return_value="a mnemonic"):
            first = client_pow.server_secret()
            second = client_pow.server_secret()
        self.assertEqual(first, second)
        self.assertEqual(len(first), 32)

    def test_a_different_identity_gives_a_different_secret(self):
        with patch.object(client_pow, "get_identity_mnemonic", return_value="a mnemonic"):
            first = client_pow.server_secret()
        with patch.object(client_pow, "get_identity_mnemonic", return_value="another one"):
            second = client_pow.server_secret()
        self.assertNotEqual(first, second)

    def test_a_node_with_no_identity_still_gets_a_secret(self):
        with patch.object(client_pow, "get_identity_mnemonic", return_value=None):
            secret = client_pow.server_secret()
        self.assertEqual(len(secret), 32)


# ----------------------------------------------------------------------------------
# The RPC itself, with the database and the client count stubbed. No row is ever
# inserted: 1500 clients is a number the difficulty is asked about, not a fixture.
# ----------------------------------------------------------------------------------

MANAGER_IMPORT_ERROR = None
try:
    # Before importing the manager: it builds a SQLConnection at import time, against
    # whatever `main.MAIN_DIR` says, which on a developer machine is not writable.
    from tests.config_bootstrap import load_example_config
    load_example_config()
    from protos import celaut_pb2
    from src.manager import manager
except Exception as import_exc:  # pragma: no cover - environment-dependent
    MANAGER_IMPORT_ERROR = import_exc


@unittest.skipIf(MANAGER_IMPORT_ERROR is not None,
                 f"Missing runtime dependencies: {MANAGER_IMPORT_ERROR}")
class GenerateClientTests(unittest.TestCase):
    def setUp(self):
        self.existing = set()
        self.client_count = 0
        self.created = []

        def add_client(client_id, balance_mu, last_usage, unmetered=False):
            # The real table has `id` as its primary key, so a duplicate raises rather
            # than overwriting a balance. Two racing requests for one id is what that
            # protects, and the stub has to behave the same way or the test is a lie.
            if client_id in self.existing:
                raise AssertionError(f"duplicate client id {client_id}")
            self.existing.add(client_id)
            self.created.append(client_id)

        patcher = patch.multiple(
            manager.sc,
            add_client=add_client,
            client_exists=lambda client_id: client_id in self.existing,
            count_clients=lambda: self.client_count,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        secret_patcher = patch.object(manager, "server_secret", return_value=SECRET)
        secret_patcher.start()
        self.addCleanup(secret_patcher.stop)

        step_patcher = patch.object(
            manager, "max_work_free_clients_per_difficulty", return_value=500
        )
        step_patcher.start()
        self.addCleanup(step_patcher.stop)

    # -- below the free limit ------------------------------------------------------

    def test_a_node_below_the_limit_creates_the_client_directly(self):
        self.client_count = 499
        client_id = _client_id()

        response = manager.generate_client_or_pow_required(client_id=client_id)

        self.assertIsInstance(response, celaut_pb2.Client)
        self.assertEqual(response.client_id, client_id)
        self.assertEqual(self.created, [client_id])

    def test_a_caller_that_sends_no_client_id_still_works_while_it_is_free(self):
        # Protocol compatibility: every caller written before this existed sends
        # nothing at all, and must keep getting a client.
        self.client_count = 0

        response = manager.generate_client_or_pow_required()

        self.assertIsInstance(response, celaut_pb2.Client)
        self.assertTrue(is_uuid4_hex(response.client_id))
        self.assertEqual(self.created, [response.client_id])

    def test_a_client_id_that_is_not_a_uuid4_is_refused_even_while_it_is_free(self):
        self.client_count = 0
        with self.assertRaises(PoWError):
            manager.generate_client_or_pow_required(client_id="../../etc/passwd")
        self.assertEqual(self.created, [])

    # -- at and above the limit ----------------------------------------------------

    def test_at_the_limit_the_node_asks_for_work_instead(self):
        self.client_count = 500
        client_id = _client_id()

        response = manager.generate_client_or_pow_required(client_id=client_id)

        self.assertIsInstance(response, celaut_pb2.PoWRequired)
        self.assertEqual(response.difficulty, 1)
        self.assertEqual(list(response.tags), POW_TAGS)
        self.assertEqual(response.prose, POW_PROSE)
        self.assertEqual(response.formal, POW_FORMAL)
        # Nothing was created, and nothing was written down to remember the request.
        self.assertEqual(self.created, [])
        self.assertEqual(self.existing, set())

    def test_the_challenge_is_bound_to_the_client_id_that_was_asked_for(self):
        self.client_count = 500
        client_id = _client_id()

        response = manager.generate_client_or_pow_required(client_id=client_id)

        self.assertEqual(parse_and_verify_challenge(response.challenge, SECRET)[0], client_id)

    def test_the_difficulty_follows_the_client_count(self):
        for count, expected in ((500, 1), (1000, 2), (1500, 3)):
            with self.subTest(clients=count):
                self.client_count = count
                response = manager.generate_client_or_pow_required(client_id=_client_id())
                self.assertEqual(response.difficulty, expected)
                # And the same number is what the challenge will be checked against.
                self.assertEqual(
                    parse_and_verify_challenge(response.challenge, SECRET)[2], expected
                )

    def test_a_request_without_a_client_id_is_refused_once_work_is_required(self):
        # There is nothing to bind a challenge to, and the node will not mint an id it
        # would then have to remember.
        self.client_count = 500
        with self.assertRaises(PoWError):
            manager.generate_client_or_pow_required()

    # -- the retry -----------------------------------------------------------------

    def _challenge_for(self, client_id, difficulty=1):
        return make_challenge(client_id=client_id, difficulty=difficulty, secret=SECRET)

    def test_a_solved_challenge_creates_the_client_from_the_challenge(self):
        self.client_count = 500
        client_id = _client_id()
        challenge = self._challenge_for(client_id)

        response = manager.generate_client_or_pow_required(
            client_id=client_id,
            challenge=challenge,
            solution=solve_pow(challenge, 1),
        )

        self.assertIsInstance(response, celaut_pb2.Client)
        self.assertEqual(response.client_id, client_id)
        self.assertEqual(self.created, [client_id])

    def test_the_client_id_created_comes_from_the_challenge_not_from_the_request(self):
        # A caller that solves a challenge for A and asks for B gets A: the id outside
        # the challenge is never consulted.
        self.client_count = 500
        authenticated_id, decoy = _client_id(), _client_id()
        challenge = self._challenge_for(authenticated_id)

        response = manager.generate_client_or_pow_required(
            client_id=decoy, challenge=challenge, solution=solve_pow(challenge, 1)
        )

        self.assertEqual(response.client_id, authenticated_id)
        self.assertEqual(self.created, [authenticated_id])

    def test_a_wrong_solution_is_refused(self):
        self.client_count = 500
        client_id = _client_id()
        challenge = self._challenge_for(client_id)

        with self.assertRaises(PoWError):
            manager.generate_client_or_pow_required(
                client_id=client_id, challenge=challenge, solution="definitely not it"
            )
        self.assertEqual(self.created, [])

    def test_a_forged_challenge_is_refused_before_any_hashing(self):
        self.client_count = 500
        client_id = _client_id()
        challenge = self._challenge_for(client_id, difficulty=3)
        # Claim it was only ever difficulty 1, keeping the MAC.
        cid, nonce, _, mac = challenge.split(":")
        forged = ":".join((cid, nonce, "1", mac))

        with patch.object(manager, "verify_solution") as verify:
            with self.assertRaises(PoWError):
                manager.generate_client_or_pow_required(
                    client_id=client_id, challenge=forged, solution=solve_pow(forged, 1)
                )
        verify.assert_not_called()
        self.assertEqual(self.created, [])

    def test_an_existing_client_id_is_refused_before_the_pow_is_validated(self):
        # The ordering the issue calls out (§8): the expensive check never runs for an
        # id that was never going to be created.
        self.client_count = 500
        client_id = _client_id()
        self.existing.add(client_id)
        challenge = self._challenge_for(client_id)
        solution = solve_pow(challenge, 1)

        with patch.object(manager, "verify_solution") as verify:
            with self.assertRaises(PoWError):
                manager.generate_client_or_pow_required(
                    client_id=client_id, challenge=challenge, solution=solution
                )

        verify.assert_not_called()
        self.assertEqual(self.created, [])

    def test_a_solution_cannot_be_spent_twice(self):
        # The replay defence, which falls out of the check above rather than out of
        # stored state: the first attempt creates the id, the second finds it taken.
        self.client_count = 500
        client_id = _client_id()
        challenge = self._challenge_for(client_id)
        solution = solve_pow(challenge, 1)

        manager.generate_client_or_pow_required(
            client_id=client_id, challenge=challenge, solution=solution
        )
        with self.assertRaises(PoWError):
            manager.generate_client_or_pow_required(
                client_id=client_id, challenge=challenge, solution=solution
            )
        self.assertEqual(self.created, [client_id])

    def test_nothing_is_written_between_the_challenge_and_the_retry(self):
        # §15: no temporary record of a pending client_id. The only write in the whole
        # exchange is the client itself, at the end.
        self.client_count = 500
        client_id = _client_id()

        challenge = manager.generate_client_or_pow_required(client_id=client_id).challenge
        self.assertEqual(self.created, [])
        self.assertEqual(self.existing, set())

        difficulty = parse_and_verify_challenge(challenge, SECRET)[2]
        manager.generate_client_or_pow_required(
            client_id=client_id,
            challenge=challenge,
            solution=solve_pow(challenge, difficulty),
        )
        self.assertEqual(self.created, [client_id])

    # -- concurrency ---------------------------------------------------------------

    def test_a_challenge_survives_the_global_difficulty_rising_under_it(self):
        # §16: the node asked for 1, other clients arrived, and the node now asks
        # newcomers for 2. The work already done is still the work that was asked for.
        self.client_count = 500
        client_id = _client_id()
        challenge = self._challenge_for(client_id, difficulty=1)
        solution = solve_pow(challenge, 1)

        self.client_count = 1000  # global difficulty is now 2

        response = manager.generate_client_or_pow_required(
            client_id=client_id, challenge=challenge, solution=solution
        )

        self.assertIsInstance(response, celaut_pb2.Client)
        self.assertEqual(response.client_id, client_id)

    def test_a_challenge_is_not_made_easier_by_the_global_difficulty_falling(self):
        # The other direction, which is the one that would be an exploit: a challenge
        # issued at 2 is still checked at 2 even if the node is currently giving
        # clients away for free.
        self.client_count = 1000
        client_id = _client_id()
        challenge = self._challenge_for(client_id, difficulty=2)
        too_easy = solve_pow(challenge, 1)

        self.client_count = 0  # global difficulty is now 0

        if not pow_digest(challenge, too_easy).endswith("00"):
            with self.assertRaises(PoWError):
                manager.generate_client_or_pow_required(
                    client_id=client_id, challenge=challenge, solution=too_easy
                )
            self.assertEqual(self.created, [])

    # -- the whole exchange --------------------------------------------------------

    def test_the_full_two_step_exchange(self):
        self.client_count = 1000
        client_id = uuid4().hex

        first = manager.generate_client_or_pow_required(client_id=client_id)
        self.assertIsInstance(first, celaut_pb2.PoWRequired)
        self.assertEqual(first.difficulty, 2)

        second = manager.generate_client_or_pow_required(
            client_id=client_id,
            challenge=first.challenge,
            solution=solve_pow(first.challenge, first.difficulty),
        )
        self.assertIsInstance(second, celaut_pb2.Client)
        self.assertEqual(second.client_id, client_id)
        self.assertEqual(self.created, [client_id])


@unittest.skipIf(MANAGER_IMPORT_ERROR is not None,
                 f"Missing runtime dependencies: {MANAGER_IMPORT_ERROR}")
class ConfiguredLimitTests(unittest.TestCase):
    def test_the_shipped_default_is_the_one_the_issue_specifies(self):
        self.assertEqual(manager.max_work_free_clients_per_difficulty(), 500)

    def test_a_nonsense_configured_value_falls_back_to_the_default(self):
        # Rather than dividing by zero inside an unauthenticated RPC. config_validation
        # refuses these at load time; this is the belt to that's braces.
        for bad in (0, -1, "", "many"):
            with self.subTest(bad=bad):
                with patch.object(manager.env_manager, "get", return_value=bad):
                    self.assertEqual(manager.max_work_free_clients_per_difficulty(), 500)


VALIDATION_IMPORT_ERROR = None
try:
    import yaml

    from src.utils.config_validation import ConfigValidationError, validate_pricing_config
except Exception as import_exc:  # pragma: no cover - environment-dependent
    VALIDATION_IMPORT_ERROR = import_exc


@unittest.skipIf(VALIDATION_IMPORT_ERROR is not None,
                 f"Missing runtime dependencies: {VALIDATION_IMPORT_ERROR}")
class ConfigValidationTests(unittest.TestCase):
    @staticmethod
    def _example_config():
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "config.example.yaml"), encoding="utf-8") as handle:
            return yaml.safe_load(handle)

    def test_the_shipped_example_config_declares_the_key_and_validates(self):
        config = self._example_config()
        self.assertEqual(config["free_tier"]["MAX_WORK_FREE_CLIENTS_PER_DIFFICULTY"], 500)
        validate_pricing_config(config, warn=lambda message: None)

    def test_a_value_that_cannot_size_a_difficulty_step_is_refused_at_load(self):
        # Caught here rather than as a ZeroDivisionError inside an unauthenticated RPC.
        for bad in (0, -1, "many", None):
            with self.subTest(bad=bad):
                config = self._example_config()
                config["free_tier"]["MAX_WORK_FREE_CLIENTS_PER_DIFFICULTY"] = bad
                with self.assertRaises(ConfigValidationError):
                    validate_pricing_config(config, warn=lambda message: None)


# ----------------------------------------------------------------------------------
# The wire. GenerateClient can now answer with either of two messages, which only
# works if both ends agree on an index for each -- bee-rpc numbers a lone message 1 by
# itself, so a Client and a PoWRequired would collide there and the caller could not
# tell them apart.
# ----------------------------------------------------------------------------------

WIRE_IMPORT_ERROR = None
try:
    from bee_rpc import buffer_pb2 as bee_buffer_pb2
    from bee_rpc import client as bee
    from protos.gateway_bee import GenerateClient_output_indices
except Exception as import_exc:  # pragma: no cover - environment-dependent
    WIRE_IMPORT_ERROR = import_exc


@unittest.skipIf(WIRE_IMPORT_ERROR is not None or MANAGER_IMPORT_ERROR is not None,
                 f"Missing runtime dependencies: {WIRE_IMPORT_ERROR or MANAGER_IMPORT_ERROR}")
class GenerateClientWireTests(unittest.TestCase):
    @staticmethod
    def _round_trip(message, indices):
        buffers = list(bee.serialize_to_buffer(message_iterator=message, indices=dict(indices)))
        return next(bee.parse_from_buffer(
            request_iterator=iter(buffers),
            indices=dict(indices),
            partitions_message_mode=True,
        ), None)

    def test_the_two_responses_stay_distinguishable(self):
        client = celaut_pb2.Client(client_id=uuid4().hex)
        pow_required = celaut_pb2.PoWRequired(
            tags=POW_TAGS, prose=POW_PROSE, formal=POW_FORMAL,
            challenge="a:b:1:c", difficulty=1,
        )

        for sent in (client, pow_required):
            with self.subTest(message=type(sent).__name__):
                received = self._round_trip(sent, GenerateClient_output_indices)
                self.assertIsInstance(received, type(sent))
                self.assertEqual(received, sent)

    def test_a_request_carrying_a_challenge_survives_the_round_trip(self):
        sent = celaut_pb2.Client(
            client_id=uuid4().hex, challenge="a:b:1:c", pow_solution="7"
        )
        received = self._round_trip(sent, {1: celaut_pb2.Client})
        self.assertEqual(received, sent)

    def test_a_caller_that_sends_nothing_is_read_as_no_request(self):
        # Protocol compatibility: bee.client_grpc with no input sends an Empty, which
        # parses to nothing here. The handler treats that as the first attempt with no
        # proposed id -- which on a node below its free limit is the whole exchange.
        buffers = list(bee.serialize_to_buffer(
            message_iterator=bee_buffer_pb2.Empty(), indices={}
        ))
        received = next(bee.parse_from_buffer(
            request_iterator=iter(buffers),
            indices=celaut_pb2.Client,
            partitions_message_mode=True,
        ), None)
        self.assertIsNone(received)


if __name__ == "__main__":
    unittest.main()
