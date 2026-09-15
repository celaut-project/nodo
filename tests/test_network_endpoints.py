"""Reading endpoints for a communication domain off the reputation ledger (issue #78).

A `pow:<chain>` network has no name to look up, so its addresses are published rather
than resolved: an ordinary reputation box whose R4 says "this is an endpoint list",
R5 names the domain, R8 says for or against, and R9 carries the addresses.

Two things these tests are about. **The digest**, because it is the name two nodes
have to agree on without talking to each other -- a digest that moved when somebody
reordered a tag or reworded the prose would leave them looking in different places
and reporting nothing, silently. And **what a box is worth**, because the contract is
open: anybody can write anything into R9, so the reader must survive every shape of
rubbish and must not let a free claim outrank one somebody burned ERG on.

Nothing here reaches the network: the explorer seam is replaced with a table of boxes.
"""
import json
import unittest
from unittest.mock import MagicMock, patch

IMPORT_ERROR = None
try:
    from tests.config_bootstrap import load_example_config
    load_example_config()

    from protos import celaut_pb2 as celaut
    from src.reputation_system import network_endpoints as ne
    from src.reputation_system.envs import REPUTATION_PROOF_ERGO_TREE
except Exception as import_exc:  # pragma: no cover - environment-dependent
    IMPORT_ERROR = import_exc
    celaut = None  # type: ignore[assignment]
    ne = None  # type: ignore[assignment]
    REPUTATION_PROOF_ERGO_TREE = ""  # type: ignore[assignment]

TYPE_NFT = "a" * 64
PROOF = "b" * 64


def _box(uris, positive=True, proof=PROOF, amount=1, tree=None):
    """One reputation box publishing an endpoint list, as the explorer renders one."""
    payload = json.dumps({"uris": list(uris)}).encode("utf-8").hex()
    return {
        "boxId": f"box-{'-'.join(uris) or 'empty'}-{positive}",
        "ergoTree": tree if tree is not None else REPUTATION_PROOF_ERGO_TREE,
        "assets": [{"tokenId": proof, "amount": amount}],
        "additionalRegisters": {
            "R8": {"renderedValue": "true" if positive else "false"},
            "R9": {"renderedValue": payload},
        },
    }


def _standing(assigned=100, burned=10_000_000):
    standing = MagicMock()
    standing.assigned_amount = assigned
    standing.burned_nanoerg = burned
    return standing


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class DescriptorDigestTests(unittest.TestCase):
    """R5 is the name of a domain, and both ends have to derive it the same way."""

    def test_the_order_the_tags_were_listed_in_does_not_change_the_name(self):
        forwards = celaut.Service.Network(tags=["pow:ergo", "mainnet"], formal=b"chain=ergo")
        backwards = celaut.Service.Network(tags=["mainnet", "pow:ergo"], formal=b"chain=ergo")

        self.assertEqual(
            ne.network_descriptor_digest(forwards),
            ne.network_descriptor_digest(backwards),
        )

    def test_rewording_the_prose_does_not_change_the_name(self):
        """It is human text, `same_component` never compares it, and the only symptom
        of folding it in would be an empty answer nobody could explain."""
        a = celaut.Service.Network(tags=["pow:ergo"], prose="peers on Ergo")
        b = celaut.Service.Network(tags=["pow:ergo"], prose="Ergo peers, rewritten")

        self.assertEqual(ne.network_descriptor_digest(a), ne.network_descriptor_digest(b))

    def test_a_different_ask_is_a_different_name(self):
        a = celaut.Service.Network(tags=["pow:ergo"], formal=b"min_cumulative_difficulty=1")
        b = celaut.Service.Network(tags=["pow:ergo"], formal=b"min_cumulative_difficulty=2")

        self.assertNotEqual(ne.network_descriptor_digest(a), ne.network_descriptor_digest(b))

    def test_the_tagless_family_descriptor_is_its_own_name(self):
        """How a publisher covers "any pow:ergo" rather than one exact ask."""
        family = celaut.Service.Network(tags=["pow:ergo"])
        exact = celaut.Service.Network(tags=["pow:ergo"], formal=b"chain=ergo")

        self.assertNotEqual(
            ne.network_descriptor_digest(family), ne.network_descriptor_digest(exact)
        )

    def test_the_digest_is_a_32_byte_hex_string(self):
        digest = ne.network_descriptor_digest(celaut.Service.Network(tags=["pow:ergo"]))

        self.assertEqual(len(digest), 64)
        bytes.fromhex(digest)


@unittest.skipIf(IMPORT_ERROR is not None, f"Missing runtime dependencies: {IMPORT_ERROR}")
class EndpointsForTests(unittest.TestCase):
    def _endpoints(self, boxes, type_nft=TYPE_NFT, standings=None, search=None):
        config = MagicMock()
        config.return_value.get.side_effect = lambda key, default=None: (
            type_nft if key == ne.TYPE_NFT_KEY else default
        )
        searcher = search if search is not None else MagicMock(return_value=iter(boxes))

        with patch.object(ne, "ConfigManager", config), \
             patch.object(ne, "explorer_api_url", return_value="https://explorer.test"), \
             patch.object(ne, "ergo_tree_template_hash", return_value="template"), \
             patch.object(ne, "iter_unspent_boxes_by_registers", searcher), \
             patch.object(
                 ne, "proof_standing",
                 side_effect=lambda api, token: (standings or {}).get(token, _standing()),
             ):
            result = ne.endpoints_for(celaut.Service.Network(tags=["pow:ergo"]))
        self.searcher = searcher
        return result

    def test_a_published_list_comes_back(self):
        self.assertEqual(
            self._endpoints([_box(["http://a.test:9053", "http://b.test:9053"])]),
            ["http://a.test:9053", "http://b.test:9053"],
        )

    def test_the_search_is_filtered_on_both_the_type_and_the_domain(self):
        """An endpoint list and an opinion about a node differ only by R4: reading
        without the filter would take every opinion ever published for an address."""
        self._endpoints([])

        registers = self.searcher.call_args.args[2]
        self.assertEqual(registers["R4"], TYPE_NFT)
        self.assertEqual(
            registers["R5"],
            ne.network_descriptor_digest(celaut.Service.Network(tags=["pow:ergo"])),
        )

    def test_nothing_is_read_at_all_when_the_type_nft_is_unset(self):
        searcher = MagicMock(return_value=iter([_box(["http://a.test:9053"])]))

        self.assertEqual(self._endpoints([], type_nft="", search=searcher), [])
        searcher.assert_not_called()

    def test_the_better_backed_claim_is_asked_first(self):
        """A share of what the proof assigned, times what it burned -- so minting a
        larger token supply, which costs nothing, buys no place in the queue."""
        endpoints = self._endpoints(
            [
                _box(["http://cheap.test:9053"], proof="c" * 64),
                _box(["http://dear.test:9053"], proof="d" * 64),
            ],
            standings={
                "c" * 64: _standing(assigned=1, burned=1_000),
                "d" * 64: _standing(assigned=1, burned=9_000_000),
            },
        )

        self.assertEqual(endpoints, ["http://dear.test:9053", "http://cheap.test:9053"])

    def test_a_claim_against_an_endpoint_takes_it_out(self):
        """Withdrawing an endpoint is something the network can do."""
        endpoints = self._endpoints(
            [
                _box(["http://gone.test:9053"], proof="c" * 64),
                _box(["http://gone.test:9053"], positive=False, proof="d" * 64),
            ],
            standings={
                "c" * 64: _standing(burned=1_000),
                "d" * 64: _standing(burned=9_000_000),
            },
        )

        self.assertEqual(endpoints, [])

    def test_a_claim_against_does_not_outweigh_a_better_backed_one_for(self):
        endpoints = self._endpoints(
            [
                _box(["http://stays.test:9053"], proof="c" * 64),
                _box(["http://stays.test:9053"], positive=False, proof="d" * 64),
            ],
            standings={
                "c" * 64: _standing(burned=9_000_000),
                "d" * 64: _standing(burned=1_000),
            },
        )

        self.assertEqual(endpoints, ["http://stays.test:9053"])

    def test_repeating_an_endpoint_in_one_box_does_not_make_it_unrejectable(self):
        """Weight comes from the proof behind a box, not from how often it says it."""
        endpoints = self._endpoints(
            [
                _box(["http://spam.test:9053"] * 5, proof="c" * 64),
                _box(["http://spam.test:9053"], positive=False, proof="d" * 64),
            ],
            standings={
                "c" * 64: _standing(burned=1_000),
                "d" * 64: _standing(burned=9_000_000),
            },
        )

        self.assertEqual(endpoints, [])

    def test_a_box_that_declares_no_polarity_is_skipped_rather_than_read_either_way(self):
        box = _box(["http://mute.test:9053"])
        box["additionalRegisters"]["R8"] = {"renderedValue": "maybe"}

        self.assertEqual(self._endpoints([box]), [])

    def test_a_box_on_another_contract_is_not_read(self):
        """The template hash identifies the contract's code, not the exact tree."""
        self.assertEqual(
            self._endpoints([_box(["http://elsewhere.test:9053"], tree="00" * 8)]), []
        )

    def test_an_endpoint_whose_proof_cannot_be_priced_is_still_offered(self):
        """Otherwise one explorer hiccup empties the list without saying so."""
        def _raise(api, token):
            raise RuntimeError("explorer down")

        config = MagicMock()
        config.return_value.get.side_effect = lambda key, default=None: (
            TYPE_NFT if key == ne.TYPE_NFT_KEY else default
        )
        with patch.object(ne, "ConfigManager", config), \
             patch.object(ne, "explorer_api_url", return_value="https://explorer.test"), \
             patch.object(ne, "ergo_tree_template_hash", return_value="template"), \
             patch.object(
                 ne, "iter_unspent_boxes_by_registers",
                 return_value=iter([_box(["http://unpriced.test:9053"])]),
             ), \
             patch.object(ne, "proof_standing", side_effect=_raise):
            endpoints = ne.endpoints_for(celaut.Service.Network(tags=["pow:ergo"]))

        self.assertEqual(endpoints, ["http://unpriced.test:9053"])

    def test_one_box_may_not_contribute_an_unbounded_number_of_endpoints(self):
        """Untrusted input that turns into outbound requests during a launch."""
        many = [f"http://h{n}.test:9053" for n in range(ne.MAX_URIS_PER_BOX + 20)]

        self.assertEqual(len(self._endpoints([_box(many)])), ne.MAX_URIS_PER_BOX)

    def test_rubbish_in_r9_is_not_an_endpoint_list_and_not_an_exception(self):
        """Anybody can write anything there; one bad box must not stop a launch."""
        good = _box(["http://good.test:9053"])
        cases = [
            {"renderedValue": "not hex at all"},
            {"renderedValue": b"[1,2,3]".hex()},
            {"renderedValue": json.dumps({"uris": "not-a-list"}).encode().hex()},
            {"renderedValue": json.dumps({"uris": [1, 2, None]}).encode().hex()},
            {"renderedValue": ""},
        ]
        for register in cases:
            with self.subTest(register=register):
                bad = _box(["http://ignored.test:9053"])
                bad["additionalRegisters"]["R9"] = register

                self.assertEqual(self._endpoints([bad, good]), ["http://good.test:9053"])

    def test_an_unreachable_explorer_is_one_fewer_source_not_a_failed_launch(self):
        def _raise(*args, **kwargs):
            raise RuntimeError("explorer down")

        self.assertEqual(self._endpoints([], search=MagicMock(side_effect=_raise)), [])


if __name__ == "__main__":
    unittest.main()
