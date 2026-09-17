"""`pow:<chain>` network resolution: reading `formal`, and picking peers from it.

No test touches the network or the clock. `requests.get` is replaced with a table
of canned answers keyed by path, so what is asserted is which requests would go
out, how each answer is read, and -- the part that matters -- which *shape* of peer
comes back, since the firewall's behaviour depends on it (one Instance per endpoint).

Two halves, matching the module: the parser, whose whole job is to refuse things;
and the resolver, whose whole job is to refuse peers.

Issue #78.
"""
import importlib.util
import json
import sys
import types
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.manager import pow_networks
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    pow_networks = None  # type: ignore[assignment]


def _load_networks_module():
    """Load ``src/manager/networks.py`` from its file, with its db seam stubbed.

    Importing it by name pulls in ``src.database.sql_connection`` and therefore
    ``bee_rpc``/grpc, which these tests never touch and a minimal checkout does not
    have. Same device as ``tests/test_networks_ancestors.py``, and the stubs are
    removed immediately so no other test module sees them.
    """
    stubbed = ("src.database.sql_connection", "src.utils.utils")
    saved = {name: sys.modules.get(name) for name in stubbed}

    sql_stub = types.ModuleType("src.database.sql_connection")
    sql_stub.SQLConnection = type("SQLConnection", (), {})
    utils_stub = types.ModuleType("src.utils.utils")
    utils_stub.load_service_from_disk = lambda service_hash: None
    sys.modules[stubbed[0]] = sql_stub
    sys.modules[stubbed[1]] = utils_stub
    try:
        path = Path(__file__).resolve().parents[1] / "src" / "manager" / "networks.py"
        spec = importlib.util.spec_from_file_location("networks_under_pow_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # pragma: no cover - environment-dependent
        return None
    finally:
        for name, previous in saved.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


GENESIS = "b0244dfc267baca974a4caee06120321562784303a8a688976ae56170e4d175b"
BLOCK = "f35a8aa47ab6e950ba1a8cd10dc92bade42928dd985575d7fe46e759379690e0"
HEIGHT = 1000001
SCORE = 2749889727692749668352


def _formal(**overrides):
    """A ``formal`` in the shape every celaut component declares one: key=value lines.

    The four keys this module reads are written unprefixed for legibility and get the
    ``pow.`` here; every other keyword is passed through verbatim, which is how a test
    states a key from somebody else's vocabulary.
    """
    shorthand = ("chain", "block_id", "min_cumulative_difficulty", "min_height", "max_tip_age_s")
    document = {
        "pow.chain": "ergo",
        "pow.block_id": BLOCK,
        "pow.min_cumulative_difficulty": "1000",
    }
    document.update(
        {(f"pow.{key}" if key in shorthand else key): value
         for key, value in overrides.items()}
    )
    for key, value in list(document.items()):
        if value is None:
            del document[key]
    return _lines(document)


def _lines(document):
    """``key=value`` lines, unsorted, so the parser is never handed its own output."""
    return "\n".join(f"{key}={value}" for key, value in document.items()).encode("utf-8")


def _network(formal=None, tags=("pow:ergo",)):
    return celaut.Service.Network(tags=list(tags), formal=formal if formal is not None else _formal())


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class PowFormalParsingTests(unittest.TestCase):
    def test_a_well_formed_formal_parses_into_its_fields(self):
        requirement = pow_networks.parse_pow_formal(
            _formal(min_cumulative_difficulty="2749889727692749668352", min_height=1873000),
            tag="pow:ergo",
        )

        self.assertEqual(requirement.chain, "ergo")
        self.assertEqual(requirement.block_id, BLOCK)
        self.assertEqual(requirement.min_cumulative_difficulty, 2749889727692749668352)
        self.assertEqual(requirement.min_height, 1873000)
        self.assertIsNone(requirement.max_tip_age_s)

    def test_cumulative_work_survives_being_larger_than_a_double(self):
        """The value Ergo reports today already loses precision as a float.

        Carried as a decimal string and compared as an exact int, so a requirement
        and a peer's score one unit apart are one unit apart here too.
        """
        big = str(2 ** 71 + 1)
        requirement = pow_networks.parse_pow_formal(
            _formal(min_cumulative_difficulty=big), tag="pow:ergo"
        )

        self.assertEqual(requirement.min_cumulative_difficulty, 2 ** 71 + 1)
        self.assertNotEqual(float(big), requirement.min_cumulative_difficulty)

    def test_an_empty_formal_is_refused_rather_than_read_as_no_constraints(self):
        with self.assertRaises(pow_networks.PowFormalError):
            pow_networks.parse_pow_formal(b"", tag="pow:ergo")

    def test_an_unknown_key_is_preserved_rather_than_refused(self):
        """This node enforces none of them, so there is nothing it grants by carrying one.

        The reverse rule -- refuse what you do not understand -- belongs where a
        misread means granting more than was asked (`network_policy.py`). Here a key
        outside the `pow.` vocabulary constrains nothing, and refusing it would make
        this reader the ceiling on what a descriptor is allowed to say.
        """
        requirement = pow_networks.parse_pow_formal(
            _formal(**{"publisher.note": "extra metadata", "pow.min_miners": "3"}),
            tag="pow:ergo",
        )

        # `pow.min_miners` is inside this module's namespace and still not one of its
        # keys: unknown all the same, so carried and not enforced.
        self.assertEqual(
            requirement.extensions,
            {"publisher.note": "extra metadata", "pow.min_miners": "3"},
        )
        self.assertEqual(requirement.chain, "ergo")

    def test_an_unknown_key_survives_the_round_trip(self):
        """A body that travelled through this node says what it came in saying."""
        requirement = pow_networks.parse_pow_formal(
            _formal(**{"publisher.note": "extra metadata"}), tag="pow:ergo"
        )
        canonical = pow_networks.canonical_formal(requirement)

        self.assertIn(b"publisher.note=extra metadata", canonical)
        self.assertEqual(
            pow_networks.parse_pow_formal(canonical, tag="pow:ergo"), requirement
        )

    def test_there_is_no_version_key_to_get_wrong(self):
        """A version belongs to the vocabulary, which `protocol_stack` names.

        So `v` is neither required nor meaningful: a formal without one parses, and
        one carrying it is carrying somebody else's key, not a claim this node reads.
        """
        without = pow_networks.parse_pow_formal(_formal(), tag="pow:ergo")
        self.assertEqual(without.chain, "ergo")
        self.assertEqual(without.extensions, {})

        withV = pow_networks.parse_pow_formal(_formal(**{"v": "2"}), tag="pow:ergo")
        self.assertEqual(withV.extensions, {"v": "2"})
        self.assertEqual(withV.chain, "ergo")

    def test_the_unprefixed_keys_of_the_old_shape_are_not_this_vocabulary(self):
        """`chain=ergo` is somebody else's `chain`, not this module's.

        The prefix is what makes that decidable, so an old unprefixed body is a body
        missing every required key rather than one silently reinterpreted.
        """
        formal = _lines(
            {"chain": "ergo", "block_id": BLOCK, "min_cumulative_difficulty": "1000"}
        )

        with self.assertRaises(pow_networks.PowFormalError) as raised:
            pow_networks.parse_pow_formal(formal, tag="pow:ergo")

        self.assertIn("pow.chain", str(raised.exception))

    def test_protocol_and_peer_discovery_are_not_keys_of_this_body(self):
        """They are descriptors of their own, in `Service.Network.protocol_stack`.

        Carried like any other foreign key rather than read: a protocol is a
        tags/prose/formal thing with its own version, and flattening it to one value
        here would be a second place for it to be stated and disagree.
        """
        requirement = pow_networks.parse_pow_formal(
            _formal(**{"protocol": "pow/ergo-v1", "peerDiscovery": "environment_variable"}),
            tag="pow:ergo",
        )

        self.assertEqual(
            requirement.extensions,
            {"protocol": "pow/ergo-v1", "peerDiscovery": "environment_variable"},
        )

    def test_a_tag_and_a_chain_that_disagree_are_a_malformed_spec(self):
        """The tag is what the operator's policy vetted, so the two have to agree."""
        with self.assertRaises(pow_networks.PowFormalError) as raised:
            pow_networks.parse_pow_formal(_formal(chain="bitcoin"), tag="pow:ergo")

        self.assertIn("pow:ergo", str(raised.exception))

    def test_a_missing_required_field_is_named(self):
        formal = _lines({"pow.chain": "ergo", "pow.block_id": BLOCK})

        with self.assertRaises(pow_networks.PowFormalError) as raised:
            pow_networks.parse_pow_formal(formal, tag="pow:ergo")

        self.assertIn("pow.min_cumulative_difficulty", str(raised.exception))

    def test_a_non_hex_block_id_is_refused(self):
        with self.assertRaises(pow_networks.PowFormalError):
            pow_networks.parse_pow_formal(_formal(block_id="not-a-block"), tag="pow:ergo")

    def test_a_negative_threshold_is_refused(self):
        with self.assertRaises(pow_networks.PowFormalError):
            pow_networks.parse_pow_formal(
                _formal(min_cumulative_difficulty="-1"), tag="pow:ergo"
            )

    def test_a_word_is_not_an_integer(self):
        with self.assertRaises(pow_networks.PowFormalError):
            pow_networks.parse_pow_formal(_formal(min_height="true"), tag="pow:ergo")

    def test_garbage_bytes_are_refused_rather_than_read(self):
        with self.assertRaises(pow_networks.PowFormalError):
            pow_networks.parse_pow_formal(b"\xff\xfe not a formal", tag="pow:ergo")

    def test_a_line_that_is_not_a_pair_is_refused(self):
        """The body is key=value lines, and a line that is not one is not ignorable.

        Malformed text is where `ComponentFormalError` is raised, and it surfaces here
        as `PowFormalError`: a caller of this module handles one exception type, not
        the parser's as well.
        """
        with self.assertRaises(pow_networks.PowFormalError) as raised:
            pow_networks.parse_pow_formal(b"pow.chain=ergo\nwhatever", tag="pow:ergo")

        self.assertIn("key=value", str(raised.exception))

    def test_a_key_declared_twice_is_refused_rather_than_resolved(self):
        """Which of the two was meant is not something a parser gets to decide."""
        formal = _formal() + b"\npow.chain=bitcoin"

        with self.assertRaises(pow_networks.PowFormalError) as raised:
            pow_networks.parse_pow_formal(formal, tag="pow:ergo")

        self.assertIn("twice", str(raised.exception))

    def test_a_hand_written_formal_may_end_in_a_newline(self):
        """Whitespace around the document is ignored; nothing inside it is."""
        requirement = pow_networks.parse_pow_formal(_formal() + b"\n", tag="pow:ergo")

        self.assertEqual(requirement.chain, "ergo")

    def test_an_extension_may_not_shadow_one_of_this_modules_own_keys(self):
        """Unreachable through the parser, which partitions by the same set.

        Reachable from a hand-built requirement, where taking it would mean the
        serializer picking one of two values for a key on the author's behalf.
        """
        requirement = pow_networks.PowRequirement(
            chain="ergo",
            block_id=BLOCK,
            min_cumulative_difficulty=1000,
            extensions={"pow.chain": "bitcoin"},
        )

        with self.assertRaises(pow_networks.PowFormalError) as raised:
            pow_networks.canonical_formal(requirement)

        self.assertIn("pow.chain", str(raised.exception))

    def test_an_unknown_chain_is_refused(self):
        with self.assertRaises(pow_networks.PowFormalError):
            pow_networks.parse_pow_formal(_formal(chain="dogecoin"), tag="pow:dogecoin")

    def test_the_canonical_form_round_trips_and_is_sorted(self):
        requirement = pow_networks.parse_pow_formal(
            _formal(min_cumulative_difficulty=str(SCORE), max_tip_age_s="3600"), tag="pow:ergo"
        )
        canonical = pow_networks.canonical_formal(requirement)

        self.assertEqual(pow_networks.parse_pow_formal(canonical, tag="pow:ergo"), requirement)
        self.assertEqual(canonical, pow_networks.canonical_formal(requirement))
        keys = [line.split("=", 1)[0] for line in canonical.decode("utf-8").split("\n")]
        self.assertEqual(keys, sorted(keys))

    def test_the_canonical_form_does_not_depend_on_the_order_it_was_written_in(self):
        """It is what `match_networks` compares byte for byte down the ancestor chain."""
        forwards = pow_networks.parse_pow_formal(
            _lines({
                "pow.chain": "ergo",
                "pow.block_id": BLOCK,
                "pow.min_cumulative_difficulty": "1000",
                "publisher.note": "extra metadata",
            }),
            tag="pow:ergo",
        )
        backwards = pow_networks.parse_pow_formal(
            _lines({
                "publisher.note": "extra metadata",
                "pow.min_cumulative_difficulty": "1000",
                "pow.block_id": BLOCK,
                "pow.chain": "ergo",
            }),
            tag="pow:ergo",
        )

        self.assertEqual(
            pow_networks.canonical_formal(forwards),
            pow_networks.canonical_formal(backwards),
        )

    def test_the_threshold_survives_the_round_trip_as_an_exact_integer(self):
        """Every value is text, so nothing on this path can round it to a double."""
        requirement = pow_networks.parse_pow_formal(
            _formal(min_cumulative_difficulty=str(2 ** 71 + 1)), tag="pow:ergo"
        )

        self.assertIn(
            f"pow.min_cumulative_difficulty={2 ** 71 + 1}",
            pow_networks.canonical_formal(requirement).decode("utf-8"),
        )


def _answers(**overrides):
    """The canned REST answers of a peer that satisfies the default requirement."""
    table = {
        "/info": {
            "genesisBlockId": GENESIS,
            "fullBlocksScore": str(SCORE),
            "fullHeight": 1873681,
            "headersHeight": 1873682,
        },
        f"/blocks/{BLOCK}/header": {"height": HEIGHT, "id": BLOCK, "timestamp": 1683634664810},
        f"/blocks/at/{HEIGHT}": [BLOCK],
        "/blocks/lastHeaders/1": [{"timestamp": 1683634664810}],
    }
    table.update(overrides)
    return table


def _response(payload, status=200):
    answer = MagicMock()
    answer.status_code = status
    answer.json = MagicMock(return_value=payload)
    return answer


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ErgoPeerVerificationTests(unittest.TestCase):
    """What disqualifies a candidate, one reason at a time."""

    def _ask(self, requirement=None, answers=None, url="http://peer.test:9053", **kwargs):
        answers = _answers() if answers is None else answers
        requirement = requirement or pow_networks.parse_pow_formal(_formal(), tag="pow:ergo")
        calls = []

        def fake_get(full_url, timeout=None):
            calls.append(full_url)
            path = full_url[len(url):]
            if path not in answers:
                return _response(None, status=404)
            return _response(answers[path])

        with patch.object(pow_networks, "env_manager") as env, patch.object(
            pow_networks.requests, "get", side_effect=fake_get
        ):
            env.get.side_effect = lambda key, default=None: (
                GENESIS if key == "ledgers.ergo.GENESIS_BLOCK_ID" else default
            )
            verdict = pow_networks.ergo_peer_satisfies(url, requirement, timeout=1, **kwargs)
        return verdict, calls

    def test_a_synced_peer_holding_the_block_qualifies(self):
        verdict, calls = self._ask()

        self.assertTrue(verdict)
        self.assertIn(f"http://peer.test:9053/blocks/at/{HEIGHT}", calls)

    def test_a_peer_on_another_chain_is_refused_before_anything_else(self):
        """A testnet node is not a peer on this network, whatever it holds."""
        verdict, calls = self._ask(
            answers=_answers(**{"/info": {"genesisBlockId": "00" * 32, "fullBlocksScore": str(SCORE)}})
        )

        self.assertFalse(verdict)
        self.assertEqual(calls, ["http://peer.test:9053/info"])

    def test_a_chain_carrying_too_little_work_is_refused(self):
        requirement = pow_networks.parse_pow_formal(
            _formal(min_cumulative_difficulty=str(SCORE + 1)), tag="pow:ergo"
        )
        verdict, calls = self._ask(requirement=requirement)

        self.assertFalse(verdict)
        self.assertEqual(calls, ["http://peer.test:9053/info"])

    def test_work_exactly_at_the_threshold_qualifies(self):
        requirement = pow_networks.parse_pow_formal(
            _formal(min_cumulative_difficulty=str(SCORE)), tag="pow:ergo"
        )

        self.assertTrue(self._ask(requirement=requirement)[0])

    def test_headers_score_is_not_accepted_in_place_of_full_blocks_score(self):
        """A header-only chain is not one this peer can serve blocks from."""
        verdict, _ = self._ask(
            answers=_answers(
                **{"/info": {"genesisBlockId": GENESIS, "headersScore": str(SCORE)}}
            )
        )

        self.assertFalse(verdict)

    def test_a_peer_below_min_height_is_refused(self):
        requirement = pow_networks.parse_pow_formal(
            _formal(min_height=9_000_000), tag="pow:ergo"
        )

        self.assertFalse(self._ask(requirement=requirement)[0])

    def test_a_peer_that_does_not_have_the_block_is_refused(self):
        answers = _answers()
        del answers[f"/blocks/{BLOCK}/header"]

        self.assertFalse(self._ask(answers=answers)[0])

    def test_a_block_the_peer_holds_only_as_an_orphan_is_refused(self):
        """Containment means *main chain*. Storing an orphan is not containing it."""
        verdict, calls = self._ask(answers=_answers(**{f"/blocks/at/{HEIGHT}": ["ab" * 32]}))

        self.assertFalse(verdict)
        self.assertIn(f"http://peer.test:9053/blocks/at/{HEIGHT}", calls)

    def test_a_stalled_peer_is_refused_against_our_clock_not_its_own(self):
        requirement = pow_networks.parse_pow_formal(_formal(max_tip_age_s=600), tag="pow:ergo")
        far_later = 1683634664810 + 3600 * 1000

        self.assertFalse(self._ask(requirement=requirement, now_ms=far_later)[0])
        self.assertTrue(
            self._ask(requirement=requirement, now_ms=1683634664810 + 60 * 1000)[0]
        )

    def test_an_unreachable_peer_is_a_no_rather_than_an_exception(self):
        with patch.object(pow_networks, "env_manager") as env, patch.object(
            pow_networks.requests,
            "get",
            side_effect=pow_networks.requests.exceptions.ConnectionError("no route"),
        ):
            env.get.side_effect = lambda key, default=None: default
            requirement = pow_networks.parse_pow_formal(_formal(), tag="pow:ergo")

            self.assertFalse(pow_networks.ergo_peer_satisfies("http://x:9053", requirement, timeout=1))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResolvePowNetworkTests(unittest.TestCase):
    def _resolve(self, network=None, qualifying=("http://a.test:9053", "http://b.test:9053"),
                 candidates=("http://a.test:9053", "http://b.test:9053")):
        network = network or _network()
        addresses = {
            "http://a.test:9053": ("203.0.113.10", 9053),
            "http://b.test:9053": ("203.0.113.11", 9053),
        }
        with patch.object(
            pow_networks, "candidate_urls", return_value=list(candidates)
        ), patch.object(
            pow_networks, "ergo_peer_satisfies", side_effect=lambda url, *a, **k: url in qualifying
        ), patch.object(
            pow_networks, "_uri_for", side_effect=lambda url: addresses.get(url)
        ):
            return pow_networks.resolve_pow_network(network, tag="pow:ergo")

    def test_every_qualifying_peer_is_its_own_instance(self):
        """They are separate operators, separately verified and separately reachable.

        One Instance with N uris is the shape of "one peer at several addresses",
        which is what `resolve_domain` builds out of the A records of one name. These
        are not that, and the guest is handed the same list the firewall is.
        """
        peers = self._resolve()

        self.assertEqual(len(peers), 2)
        self.assertEqual(
            [(u.ip, u.port) for peer in peers for u in peer.uri_slot[0].uri],
            [("203.0.113.10", 9053), ("203.0.113.11", 9053)],
        )
        for peer in peers:
            self.assertEqual(len(peer.uri_slot), 1)
            self.assertEqual(len(peer.uri_slot[0].uri), 1)

    def test_a_peer_that_fails_verification_is_left_out(self):
        peers = self._resolve(qualifying=("http://b.test:9053",))

        self.assertEqual(
            [(u.ip, u.port) for peer in peers for u in peer.uri_slot[0].uri],
            [("203.0.113.11", 9053)],
        )

    def test_no_qualifying_peer_resolves_to_nothing_rather_than_aborting(self):
        """"Nobody meets D right now" is a statement about the world, and transient.

        A policy rejection and an unreadable ancestor spec do abort; this does not.
        """
        self.assertEqual(self._resolve(qualifying=()), [])

    def test_the_declared_protocol_stack_is_carried_onto_the_peer(self):
        network = _network()
        network.protocol_stack.append(celaut.Service.Api.Protocol(tags=["http"]))

        peers = self._resolve(network=network)

        self.assertEqual(list(peers[0].api.slot[0].protocol_stack[0].tags), ["http"])
        self.assertEqual(list(peers[0].api.slot[0].transport.tags), ["tcp"])

    def test_a_malformed_formal_stops_the_resolution_rather_than_returning_peers(self):
        with self.assertRaises(pow_networks.PowFormalError):
            self._resolve(network=_network(formal=b"chain=ergo"))

    def test_bitcoin_parses_and_says_it_does_not_resolve_yet(self):
        network = celaut.Service.Network(
            tags=["pow:bitcoin"], formal=_formal(chain="bitcoin")
        )

        with self.assertRaises(NotImplementedError) as raised:
            pow_networks.resolve_pow_network(network, tag="pow:bitcoin")

        self.assertIn("ergo", str(raised.exception))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class CandidateSourceTests(unittest.TestCase):
    def test_the_crawl_file_is_read_as_urls_and_never_split_on_a_colon(self):
        """Its keys are `restApiUrl` values, which the old stub would have crashed on.

        `src/manager/ergo.py` writes full URLs; the unreachable code in
        `resolve_ergo_network` did `ip, port = uri.split(":")` on them.
        """
        import tempfile, os

        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "peers.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"https://node.sigmaspace.io": {}, "http://10.0.0.5:9053": {}}, handle)

            settings = {
                "ledgers.ergo.NODE_URL": "https://configured.test",
                "ledgers.ergo.HTTP_PEERS_PATH": path,
                "service_networks.default_instances": {
                    "pow:ergo": ["http://mine.test:9053"],
                    "pow:bitcoin": ["http://not-for-this-tag.test:8332"],
                },
            }
            urls = self._candidates(settings)

        # Configured node first, then the operator's own for THIS tag, then strangers.
        # The bitcoin entry is not an Ergo endpoint and is never asked.
        self.assertEqual(
            urls,
            [
                "https://configured.test",
                "http://mine.test:9053",
                "https://node.sigmaspace.io",
                "http://10.0.0.5:9053",
            ],
        )

    def test_an_unreadable_crawl_file_leaves_the_other_sources_alone(self):
        settings = {
            "ledgers.ergo.NODE_URL": "https://configured.test",
            "ledgers.ergo.HTTP_PEERS_PATH": "/nonexistent/peers.json",
        }

        self.assertEqual(self._candidates(settings), ["https://configured.test"])

    def test_endpoints_that_are_not_a_mapping_of_tag_to_uris_are_ignored(self):
        """A typo in one config block does not abort a launch with other sources."""
        settings = {
            "ledgers.ergo.NODE_URL": "https://configured.test",
            "service_networks.default_instances": [
                "http://flat-list.test:9053"
            ],
        }

        self.assertEqual(self._candidates(settings), ["https://configured.test"])

    def test_a_lone_uri_is_accepted_where_a_list_was_meant(self):
        settings = {
            "service_networks.default_instances": {
                "pow:ergo": "http://alone.test:9053"
            },
        }

        self.assertEqual(self._candidates(settings), ["http://alone.test:9053"])

    def test_the_operator_is_asked_before_the_strangers(self):
        """Trust order: the node the operator configured, then what they wrote down."""
        settings = {
            "ledgers.ergo.NODE_URL": "https://configured.test",
            "service_networks.default_instances": {
                "pow:ergo": ["http://mine.test:9053", "http://also-mine.test:9053"]
            },
        }

        self.assertEqual(
            self._candidates(settings),
            [
                "https://configured.test",
                "http://mine.test:9053",
                "http://also-mine.test:9053",
            ],
        )

    def test_what_peers_suggest_is_asked_last_and_only_when_relaying_is_allowed(self):
        """A node answering ResolveNetwork passes ask_peers=False, so it cannot relay."""
        with patch.object(pow_networks, "_peer_suggested_endpoints") as suggested:
            suggested.return_value = ["http://from-a-peer.test:9053"]

            asking = self._candidates({}, patch_peers=False)
            answering = self._candidates({}, patch_peers=False, ask_peers=False)

        self.assertEqual(asking, ["http://from-a-peer.test:9053"])
        self.assertEqual(answering, [])
        suggested.assert_called_once()

    def test_the_same_endpoint_named_by_two_sources_is_asked_once(self):
        settings = {
            "ledgers.ergo.NODE_URL": "http://shared.test:9053/",
            "service_networks.default_instances": {
                "pow:ergo": ["http://shared.test:9053"]
            },
        }

        self.assertEqual(self._candidates(settings), ["http://shared.test:9053"])

    def _candidates(self, settings, patch_peers=True, ask_peers=True):
        """`candidate_urls` with config stubbed and the peer-ask source controlled."""
        with ExitStack() as stack:
            env = stack.enter_context(patch.object(pow_networks, "env_manager"))
            if patch_peers:
                stack.enter_context(
                    patch.object(pow_networks, "_peer_suggested_endpoints", return_value=[])
                )
            env.get.side_effect = lambda key, default=None: settings.get(key, default)
            return pow_networks.candidate_urls(_network(), "pow:ergo", ask_peers=ask_peers)

    def test_a_url_is_pinned_to_an_address_and_a_port(self):
        with patch.object(
            pow_networks.socket,
            "getaddrinfo",
            return_value=[(2, 1, 6, "", ("203.0.113.9", 9053))],
        ):
            self.assertEqual(
                pow_networks._uri_for("http://node.test:9053"), ("203.0.113.9", 9053)
            )
            self.assertEqual(pow_networks._uri_for("https://node.test"), ("203.0.113.9", 443))
            # No scheme, no port: the conventional Ergo REST port.
            self.assertEqual(pow_networks._uri_for("node.test"), ("203.0.113.9", 9053))

    def test_a_name_that_does_not_resolve_is_dropped_rather_than_raising(self):
        with patch.object(
            pow_networks.socket, "getaddrinfo", side_effect=pow_networks.socket.gaierror("nx")
        ):
            self.assertIsNone(pow_networks._uri_for("http://nowhere.test:9053"))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResolveNetworkDispatchTests(unittest.TestCase):
    """`resolve_network` routes a `pow:` tag here, and changes nothing else."""

    def setUp(self):
        networks = _load_networks_module()
        if networks is None:  # pragma: no cover - environment-dependent
            self.skipTest("src/manager/networks.py could not be loaded")
        self.networks = networks

    def test_a_pow_tag_reaches_the_pow_resolver(self):
        peer = celaut.Instance(uri_slot=[celaut.Instance.Uri_Slot(internal_port=1)])
        with patch.object(
            self.networks, "resolve_pow_network", return_value=[peer]
        ) as resolver, patch.object(self.networks, "resolve_domain") as dns:
            result = self.networks.resolve_network(_network())

        self.assertEqual(len(result), 1)
        resolver.assert_called_once()
        dns.assert_not_called()

    def test_a_pow_tag_never_reaches_the_dns_heuristic(self):
        """`pow:ergo` has no dot, so it could not have -- asserted so it stays true."""
        with patch.object(
            self.networks, "resolve_pow_network", return_value=[]
        ), patch.object(self.networks, "resolve_domain") as dns:
            self.assertEqual(self.networks.resolve_network(_network()), [])

        dns.assert_not_called()

    def test_a_dns_tag_alongside_a_pow_tag_still_resolves_when_the_pow_one_finds_nobody(self):
        network = _network(tags=("pow:ergo", "example.test"))
        with patch.object(
            self.networks, "resolve_pow_network", return_value=[]
        ), patch.object(
            self.networks,
            "resolve_domain",
            return_value=[celaut.Instance.Uri(ip="203.0.113.1", port=80)],
        ) as dns:
            result = self.networks.resolve_network(network)

        dns.assert_called_once_with("example.test")
        self.assertEqual(
            [(u.ip, u.port) for u in result[0].uri_slot[0].uri], [("203.0.113.1", 80)]
        )

    def test_an_ordinary_dns_network_is_untouched_by_the_new_branch(self):
        with patch.object(
            self.networks,
            "resolve_domain",
            return_value=[celaut.Instance.Uri(ip="203.0.113.2", port=443)],
        ):
            result = self.networks.resolve_network(
                celaut.Service.Network(tags=["example.test"])
            )

        self.assertEqual(
            [(u.ip, u.port) for u in result[0].uri_slot[0].uri], [("203.0.113.2", 443)]
        )


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class MatchNetworksTests(unittest.TestCase):
    """`match_networks` reads `formal`, which is what the ancestor chain authorizes on.

    The rule is `node_identity.same_component`'s, the one every other tags/prose/formal
    descriptor in celaut is compared by: formal decides when both sides declare one, a
    shared tag otherwise.
    """

    def setUp(self):
        networks = _load_networks_module()
        if networks is None:  # pragma: no cover - environment-dependent
            self.skipTest("src/manager/networks.py could not be loaded")
        self.match = networks.match_networks

    def test_two_pow_networks_asking_for_different_work_do_not_match(self):
        """The case the tag intersection got wrong: same domain name, different ask."""
        a = _network(formal=_formal(min_cumulative_difficulty="1000"))
        b = _network(formal=_formal(min_cumulative_difficulty="9000"))

        self.assertFalse(self.match(a, b))

    def test_the_same_ask_written_in_a_different_order_still_matches(self):
        """Only if both sides canonicalise -- which is why `canonical_formal` sorts."""
        requirement = pow_networks.parse_pow_formal(_formal(), tag="pow:ergo")
        canonical = pow_networks.canonical_formal(requirement)

        self.assertTrue(
            self.match(_network(formal=canonical), _network(formal=canonical))
        )

    def test_a_parent_declaring_no_formal_still_grants_the_whole_tag(self):
        """How a parent says "any pow:ergo my children care to specify"."""
        parent = celaut.Service.Network(tags=["pow:ergo"])
        child = _network(formal=_formal(min_cumulative_difficulty="9000"))

        self.assertTrue(self.match(parent, child))
        self.assertTrue(self.match(child, parent))

    def test_a_shared_tag_still_decides_when_neither_side_declares_a_formal(self):
        """Every network that predates `formal` behaves exactly as it did."""
        self.assertTrue(
            self.match(
                celaut.Service.Network(tags=["a.example", "b.example"]),
                celaut.Service.Network(tags=["b.example"]),
            )
        )
        self.assertFalse(
            self.match(
                celaut.Service.Network(tags=["a.example"]),
                celaut.Service.Network(tags=["c.example"]),
            )
        )

    def test_a_network_that_names_nothing_matches_nothing_not_even_itself(self):
        """Prose alone states nothing a comparison can act on."""
        empty = celaut.Service.Network(prose="a domain, described")

        self.assertFalse(self.match(empty, empty))

    def test_the_protocol_stack_is_not_what_decides(self):
        """It says what the peers speak, not which domain this is."""
        a = celaut.Service.Network(tags=["pow:ergo"])
        b = celaut.Service.Network(tags=["pow:ergo"])
        b.protocol_stack.append(celaut.Service.Api.Protocol(tags=["http"]))

        self.assertTrue(self.match(a, b))


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class ResolveForPeerTests(unittest.TestCase):
    """What a node answers when another one asks it to resolve a domain (issue #78).

    The decisions behind `Gateway.ResolveNetwork`, which is why they live in
    `networks.resolve_network_for_peer` and not in the handler: a decision buried in
    gRPC plumbing is a decision nobody can test.
    """

    def setUp(self):
        networks = _load_networks_module()
        if networks is None:  # pragma: no cover - environment-dependent
            self.skipTest("src/manager/networks.py could not be loaded")
        self.networks = networks

    def test_the_question_is_never_relayed_to_our_own_peers(self):
        """Two nodes that know each other are a cycle; relaying makes one ask a flood."""
        with patch.object(self.networks, "enforce_network_policy"), \
             patch.object(self.networks, "resolve_pow_network", return_value=[]) as resolver:
            self.networks.resolve_network_for_peer(_network())

        self.assertIs(resolver.call_args.kwargs["ask_peers"], False)

    def test_the_answer_carries_the_tags_it_was_asked_about(self):
        peer = celaut.Instance(uri_slot=[celaut.Instance.Uri_Slot(internal_port=1)])
        with patch.object(self.networks, "enforce_network_policy"), \
             patch.object(self.networks, "resolve_pow_network", return_value=[peer]):
            resolution = self.networks.resolve_network_for_peer(_network())

        self.assertEqual(list(resolution.tags), ["pow:ergo"])
        self.assertEqual(len(resolution.peer_instances), 1)

    def test_a_domain_the_operator_refuses_to_reach_is_refused_to_a_peer_too(self):
        """Handing over the addresses this node would not use itself is reaching it
        by proxy -- the argument that puts the check before the balancer in
        `launch_service` rather than after it."""
        from src.utils.network_policy import NetworkPolicy, NetworkPolicyRejection

        rejection = NetworkPolicy(blacklist=("pow:*",)).check(
            networks=[_network()], subject="peer ipv4:203.0.113.5:1234"
        )

        def _refuse(networks, subject=""):
            raise NetworkPolicyRejection(rejection)

        with patch.object(self.networks, "enforce_network_policy", side_effect=_refuse), \
             patch.object(self.networks, "resolve_pow_network") as resolver:
            with self.assertRaises(NetworkPolicyRejection):
                self.networks.resolve_network_for_peer(_network())

        # Refused before anything was resolved: a rejection is not an empty answer.
        resolver.assert_not_called()

    def test_an_ordinary_dns_domain_is_answerable_too(self):
        """Nothing about this is proof of work; the RPC is generic on purpose."""
        with patch.object(self.networks, "enforce_network_policy"), \
             patch.object(
                 self.networks, "resolve_domain",
                 return_value=[celaut.Instance.Uri(ip="203.0.113.2", port=443)],
             ):
            resolution = self.networks.resolve_network_for_peer(
                celaut.Service.Network(tags=["example.test"])
            )

        self.assertEqual(list(resolution.tags), ["example.test"])
        self.assertEqual(
            [(u.ip, u.port) for u in resolution.peer_instances[0].uri_slot[0].uri],
            [("203.0.113.2", 443)],
        )


if __name__ == "__main__":
    unittest.main()
