"""What the network stakes on a node, and how that is read off the chain.

Three things are pinned here, because each of them is a way to report a reputation
that is not the one the ecosystem would compute:

* the arithmetic -- a stake is a *share* of the proof that published it, positives and
  negatives are kept apart, and one proof counts once however many boxes it splits into;
* the register reading -- a box only counts when it is on the canonical contract, of the
  node type, addressed to this node, and declares a polarity;
* the periods -- a window contains the opinions *published* in it, and the standing
  total contains every opinion whenever it was published.

No network: the box shapes are the ones the explorer really returns (see the R8 note in
``decode_bool_register``), and the two chain lookups an opinion needs are stubbed.
"""

import unittest
from unittest import mock

from src.commands.reputation import DAY_SECONDS, report
from src.reputation_system.contracts.ergo import opinions as ergo_opinions
from src.reputation_system.contracts.ergo.utils import (
    MAINNET_EXPLORER,
    ProofStanding,
    MAINNET_GENESIS_BLOCK_ID,
    TESTNET_EXPLORER,
    decode_bool_register,
    ergo_tree_template,
    ergo_tree_template_hash,
    explorer_api_url,
)
from src.reputation_system.envs import REPUTATION_PROOF_ERGO_TREE
from src.utils.config import ConfigManager
from src.reputation_system.opinions import (
    NodeReputation,
    Opinion,
    burned_by_proof,
    by_proof,
    split_own,
    totals,
)

NODE_ID = "ed6df5dfbea1f0932dc7fdd25d0f0543f6086ef110fc888f1acd5c89af4c84b8"
NODE_TYPE_NFT = "64060577c3393e0e3cf8938ec8e6a2002ded27ece17750aa5add7d5c3e1227ba"
PROOF = "d4e7c77ca41e7a950cb6c46fcc5da4a91ae4021aceb718ba651f74b750ff4b2a"
OWN_PROOF = "aa" * 32
NOW = 1_800_000_000


def opinion(proof=PROOF, amount=1, assigned=4, positive=True, at=NOW, box="box", burned=0):
    return Opinion(
        ledger="ergo",
        proof_id=proof,
        owner="0008cd" + "02" * 33,
        amount=amount,
        assigned_amount=assigned,
        positive=positive,
        published_at=at,
        box_id=box,
        burned_nanoerg=burned,
    )


def coll_byte(payload_hex: str) -> str:
    """Serialize a hex payload as a Coll[Byte] register: 0e + VLQ(len) + payload."""
    return "0e" + format(len(payload_hex) // 2, "02x") + payload_hex


def box(
    r4=NODE_TYPE_NFT,
    r5=NODE_ID,
    r8="0101",
    token=PROOF,
    amount=1,
    ergo_tree=REPUTATION_PROOF_ERGO_TREE,
    block="block",
    assets=None,
):
    return {
        "boxId": "b0",
        "blockId": block,
        "ergoTree": ergo_tree,
        "assets": [{"tokenId": token, "amount": amount}] if assets is None else assets,
        "additionalRegisters": {
            "R4": coll_byte(r4),
            "R5": coll_byte(r5),
            "R7": coll_byte("0008cd" + "02" * 33),
            "R8": r8,
        },
    }


class StubChain(ergo_opinions._Chain):
    """The explorer lookups an opinion needs, answered without a network."""

    def __init__(self, assigned=4, timestamp=NOW, burned=2 * 10 ** 9):
        super().__init__("http://explorer.invalid")
        self._standing = ProofStanding(
            assigned_amount=assigned, reserved_amount=0, burned_nanoerg=burned
        )
        self._timestamp = timestamp

    def standing(self, token_id):
        return self._standing

    def block_time(self, block_id):
        return self._timestamp


class OpinionArithmeticTests(unittest.TestCase):
    def test_a_stake_is_a_share_of_what_its_proof_has_assigned(self):
        # A quarter of a four-token proof and a quarter of a four-billion-token one are
        # the same opinion: the raw counts are not comparable, the shares are.
        self.assertEqual(opinion(amount=1, assigned=4).weight, 0.25)
        self.assertEqual(opinion(amount=1_000_000_000, assigned=4_000_000_000).weight, 0.25)

    def test_the_denominator_excludes_the_reserve_a_proof_holds_in_itself(self):
        # Measured on mainnet: every live profile parks ~99,999,9xx of its 99,999,999
        # tokens in one self-pointing box and spends a single token per opinion. Against
        # the minted supply that opinion reads 0.000001% — six orders of magnitude off,
        # and the same figure for every opinion anyone has ever published. Against what
        # the proof has actually assigned it reads what it means.
        real = opinion(amount=1, assigned=95)
        self.assertAlmostEqual(real.weight * 100, 1.0526, places=4)
        against_minted = 1 / (95 + 99_999_904) * 100
        self.assertLess(against_minted, 0.00001)

    def test_a_proof_that_has_assigned_nothing_reads_as_no_share(self):
        # Rather than a division by zero. It also cannot arise from a real opinion: a
        # proof with nothing assigned has published none.
        self.assertEqual(opinion(amount=1, assigned=0).weight, 0.0)

    def test_an_unreadable_supply_is_a_zero_share_not_a_crash(self):
        self.assertEqual(opinion(amount=5, assigned=0).weight, 0.0)

    def test_a_proof_counts_once_however_many_boxes_it_splits_into(self):
        split = [opinion(amount=1, assigned=4), opinion(amount=1, assigned=4)]
        self.assertEqual(by_proof(split), {PROOF: 0.5})
        self.assertEqual(totals(split).positive_proofs, 1)

    def test_for_and_against_are_reported_apart(self):
        figures = totals([
            opinion(proof="for", amount=2, assigned=4),
            opinion(proof="against", amount=1, assigned=4, positive=False),
        ])
        self.assertEqual(figures.positive, 0.5)
        self.assertEqual(figures.negative, 0.25)
        self.assertEqual(figures.net, 0.25)
        self.assertEqual((figures.positive_proofs, figures.negative_proofs), (1, 1))

    def test_a_proof_that_cancels_itself_out_is_neither_supporter_nor_detractor(self):
        figures = totals([
            opinion(amount=1, assigned=4),
            opinion(amount=1, assigned=4, positive=False),
        ])
        self.assertEqual((figures.positive, figures.negative), (0.0, 0.0))
        self.assertEqual(figures.proofs, 0)

    def test_our_own_proof_is_split_off_rather_than_counted(self):
        theirs, ours = split_own(
            [opinion(proof=PROOF), opinion(proof=OWN_PROOF)], OWN_PROOF
        )
        self.assertEqual([item.proof_id for item in theirs], [PROOF])
        self.assertEqual([item.proof_id for item in ours], [OWN_PROOF])

    def test_with_no_proof_of_our_own_nothing_is_split_off(self):
        theirs, ours = split_own([opinion()], "")
        self.assertEqual(len(theirs), 1)
        self.assertEqual(ours, [])

    def test_a_share_is_weighed_against_what_its_proof_had_to_give_up(self):
        # Minting a proof is free, so the share alone cannot separate a costly opinion
        # from a fabricated one. The reputation contract makes the ERG behind a proof
        # unrecoverable (`nativeErgIsPreserved` holds on the owner's own spending path),
        # which is what turns it into a cost worth reading.
        costly = opinion(amount=1, assigned=2, burned=10 * 10 ** 9)   # half of a 10 ERG proof
        cheap = opinion(amount=2, assigned=2, burned=10 ** 6)         # all of a min-box proof
        self.assertEqual(costly.backed_nanoerg, 5 * 10 ** 9)
        self.assertEqual(cheap.backed_nanoerg, 10 ** 6)
        # The cheap one commits a larger *share* and yet stands for a thousandth as
        # much sunk value — the whole reason this figure exists.
        self.assertGreater(cheap.weight, costly.weight)
        self.assertGreater(costly.backed_nanoerg, cheap.backed_nanoerg * 1000)

    def test_backing_multiplies_the_share_and_not_the_raw_token_count(self):
        # `token_amount * burned` (as the reference web app's profile score computes it)
        # rewards minting a bigger supply, which costs nothing: these two proofs put the
        # same sacrifice behind the same fraction of themselves, so they must weigh the
        # same however many tokens they chose to mint.
        small = opinion(proof="small", amount=1, assigned=2, burned=10 ** 9)
        huge = opinion(proof="huge", amount=10 ** 15, assigned=2 * 10 ** 15, burned=10 ** 9)
        self.assertEqual(small.backed_nanoerg, huge.backed_nanoerg)

    def test_a_proof_backing_counts_once_however_many_boxes_it_spreads_over(self):
        # The sacrifice belongs to the proof, not to each of its boxes; summing it per
        # box would multiply one sacrifice by however many opinions it is split into.
        split = [
            opinion(amount=1, assigned=4, burned=4 * 10 ** 9),
            opinion(amount=1, assigned=4, burned=4 * 10 ** 9),
        ]
        self.assertEqual(burned_by_proof(split), {PROOF: 4 * 10 ** 9})
        # Half the proof is committed here, so half its sacrifice stands behind us.
        self.assertEqual(totals(split).positive_backing, 2 * 10 ** 9)

    def test_backing_is_reported_for_and_against_and_cancels_with_the_shares(self):
        figures = totals([
            opinion(proof="for", amount=2, assigned=4, burned=10 ** 9),
            opinion(proof="against", amount=1, assigned=4, positive=False, burned=8 * 10 ** 9),
        ])
        self.assertEqual(figures.positive_backing, 0.5 * 10 ** 9)
        self.assertEqual(figures.negative_backing, 2 * 10 ** 9)
        # A well-funded detractor outweighs a cheap supporter, which a share-only net
        # (+0.25 here) would hide entirely.
        self.assertGreater(figures.net, 0)
        self.assertLess(figures.net_backing, 0)

    def test_a_proof_that_cancels_itself_out_takes_its_backing_with_it(self):
        figures = totals([
            opinion(amount=1, assigned=4, burned=10 ** 9),
            opinion(amount=1, assigned=4, positive=False, burned=10 ** 9),
        ])
        self.assertEqual((figures.positive_backing, figures.negative_backing), (0.0, 0.0))

    def test_an_unreadable_backing_understates_rather_than_invents(self):
        # `_Chain.burned` reports 0 when the explorer cannot be asked. Zero backing is
        # the safe direction: it never credits a proof with a sacrifice it may not have
        # made.
        self.assertEqual(opinion(burned=0).backed_nanoerg, 0.0)

    def test_an_undated_opinion_is_still_reputation_the_node_holds(self):
        # Nothing is aggregated by date, so a box whose block could not be read costs
        # only the age shown beside it.
        self.assertEqual(totals([opinion(at=None)]).positive, 0.25)


class RegisterReadingTests(unittest.TestCase):
    def test_polarity_is_read_from_both_forms_the_sources_render(self):
        # The explorer's serializedValue (and the node) give the SBoolean encoding; the
        # rendered form gives the word. Reading only the word made every real box read
        # as negative.
        self.assertIs(decode_bool_register("0101"), True)
        self.assertIs(decode_bool_register("0100"), False)
        self.assertIs(decode_bool_register("true"), True)
        self.assertIs(decode_bool_register("False"), False)

    def test_no_declared_polarity_is_not_a_polarity(self):
        for value in ("", "0e20aa", "maybe"):
            self.assertIsNone(decode_bool_register(value), value)

    def test_a_canonical_box_addressed_to_us_is_an_opinion(self):
        self.assertTrue(ergo_opinions._is_opinion_about(box(), NODE_TYPE_NFT, NODE_ID))

    def test_a_box_addressed_to_somebody_else_is_not(self):
        self.assertFalse(
            ergo_opinions._is_opinion_about(box(r5="bb" * 32), NODE_TYPE_NFT, NODE_ID)
        )

    def test_another_object_type_is_not_an_opinion_about_a_node(self):
        # A plain-text note or a user profile pointed at the same bytes is not a
        # judgement on a node, and counting it would put somebody's comment in the
        # node's reputation.
        self.assertFalse(
            ergo_opinions._is_opinion_about(box(r4="cc" * 32), NODE_TYPE_NFT, NODE_ID)
        )

    def test_a_box_off_the_canonical_contract_is_not_counted(self):
        # It sits at the same address but under a different compiled contract, so no
        # other reader in the ecosystem sees it. Crediting it would report reputation
        # only this node can see.
        self.assertFalse(
            ergo_opinions._is_opinion_about(box(ergo_tree="19aa"), NODE_TYPE_NFT, NODE_ID)
        )

    def test_an_opinion_carries_the_stake_the_supply_the_date_and_the_sacrifice(self):
        read = ergo_opinions._opinion(box(amount=3), StubChain(assigned=4, timestamp=NOW))
        self.assertEqual((read.amount, read.assigned_amount, read.weight), (3, 4, 0.75))
        self.assertEqual(read.burned_nanoerg, 2 * 10 ** 9)
        self.assertEqual(read.backed_nanoerg, 1.5 * 10 ** 9)
        self.assertIs(read.positive, True)
        self.assertEqual(read.published_at, NOW)
        self.assertEqual(read.proof_id, PROOF)

    def test_a_proofs_own_reserve_box_is_not_an_opinion_about_anybody(self):
        # R5 == the box's own token id is how a proof declares itself and parks the
        # supply it has not assigned. Counting it would read a proof's unspent reserve
        # as reputation held on somebody — and old nodo minted exactly this shape, with
        # the proof id in R5 instead of the target's identity key.
        reserve = box(r5=PROOF, token=PROOF)
        self.assertFalse(ergo_opinions._is_opinion_about(reserve, NODE_TYPE_NFT, PROOF))

    def test_a_box_with_no_polarity_or_no_token_is_not_an_opinion(self):
        self.assertIsNone(ergo_opinions._opinion(box(r8=""), StubChain()))
        self.assertIsNone(ergo_opinions._opinion(box(assets=[]), StubChain()))


class ExplorerFilterTests(unittest.TestCase):
    """The reputation contract is shared by the whole ecosystem, so the question has to
    be asked as a filter and not as a download of it.

    Two values make that filter work, and getting either wrong returns *nothing* rather
    than an error — which reads as "nobody has an opinion about this node". So both are
    pinned: the template hash the search is keyed by, and the fact that the endpoint
    matches rendered register values.
    """

    # Verified against api.ergoplatform.com: with this as `ergoTreeTemplateHash`, an
    # unfiltered search returns the reputation contract's boxes, and adding
    # `{"R4": <node type NFT>, "R5": <identity key>}` narrows it to the one box that
    # names that node. Derived below rather than trusted, so it cannot drift from the
    # ErgoTree it belongs to.
    TEMPLATE_HASH = "e84b95d84a30df33aa258fe2b9d24c3e75e27a67c6453983c19703029112d147"

    def test_the_template_hash_the_search_needs_is_derived_from_the_pinned_tree(self):
        self.assertEqual(
            ergo_tree_template_hash(REPUTATION_PROOF_ERGO_TREE), self.TEMPLATE_HASH
        )

    def test_the_template_is_the_root_expression_with_the_constants_stripped(self):
        template = ergo_tree_template(REPUTATION_PROOF_ERGO_TREE)
        tree = bytes.fromhex(REPUTATION_PROOF_ERGO_TREE)
        # The tail of the tree, and shorter than it by the header, the size, the
        # constant count and the 28 constants.
        self.assertTrue(tree.endswith(template))
        self.assertLess(len(template), len(tree))
        # `d8` opens a BlockValue: the root of this contract, not a constant.
        self.assertEqual(template[:1].hex(), "d8")

    def test_a_tree_without_constant_segregation_is_its_own_template(self):
        # Header 0x00: no size field, no constants. Everything after it is the root.
        self.assertEqual(ergo_tree_template("00d19373"), bytes.fromhex("d19373"))

    def test_a_constant_of_unknown_length_raises_rather_than_hashing_the_wrong_bytes(self):
        # 0x10 segregation | 0x01 version, one constant of type 0x63 — a shape this
        # reader cannot measure. Guessing would produce a valid-looking hash that
        # matches no box on the chain, and a search that quietly finds nothing.
        with self.assertRaises(ValueError):
            ergo_tree_template("110163ff")

    def test_the_explorer_is_named_by_the_configured_genesis_block(self):
        # Local, so reputation stays readable while the Ergo node is unreachable: the
        # two are separate services and the reputation read only needs the explorer.
        self.assertEqual(explorer_api_url(), MAINNET_EXPLORER)
        with mock.patch.object(ConfigManager, "get", return_value="not-mainnet"):
            self.assertEqual(explorer_api_url(), TESTNET_EXPLORER)

    def test_the_mainnet_genesis_is_the_one_the_config_ships(self):
        # `manager.ergo` refuses an Ergo node whose `/info` reports a different genesis
        # than this key, which is what makes the key trustworthy as a network name.
        self.assertEqual(
            ConfigManager().get("ledgers.ergo.GENESIS_BLOCK_ID"), MAINNET_GENESIS_BLOCK_ID
        )

    def test_a_search_that_cannot_be_used_falls_back_to_scanning_the_contract(self):
        # The fallback exists so a search that never ran cannot read as "nobody has an
        # opinion about this node".
        def unusable(*_args, **_kwargs):
            raise ValueError("HTTP 400")
            yield  # pragma: no cover - makes this a generator, as the real one is

        with mock.patch.object(ergo_opinions, "iter_unspent_boxes_by_registers", unusable), \
             mock.patch.object(
                 ergo_opinions, "iter_unspent_boxes_by_address",
                 lambda *a, **k: iter([box()]),
             ):
            found = list(ergo_opinions._opinion_boxes("http://explorer.invalid", {}))
        self.assertEqual(len(found), 1)

    def test_a_search_that_fails_part_way_raises_rather_than_counting_twice(self):
        # Restarting on the address scan here would re-emit the box already yielded,
        # and the caller sums these — so the node's own reputation would come out
        # inflated. Half an answer has to be an error.
        def half_a_page(*_args, **_kwargs):
            yield box()
            raise ValueError("connection reset mid-scan")

        scanned = []
        with mock.patch.object(ergo_opinions, "iter_unspent_boxes_by_registers", half_a_page), \
             mock.patch.object(
                 ergo_opinions, "iter_unspent_boxes_by_address",
                 lambda *a, **k: iter(scanned.append("scanned") or [box()]),
             ):
            with self.assertRaises(ValueError):
                list(ergo_opinions._opinion_boxes("http://explorer.invalid", {}))
        self.assertEqual(scanned, [], "fell back after emitting, so a box was counted twice")

    def test_no_opinions_is_an_answer_and_not_a_reason_to_rescan(self):
        scanned = []
        with mock.patch.object(
            ergo_opinions, "iter_unspent_boxes_by_registers", lambda *a, **k: iter([])
        ), mock.patch.object(
            ergo_opinions, "iter_unspent_boxes_by_address",
            lambda *a, **k: iter(scanned.append("scanned") or []),
        ):
            self.assertEqual(list(ergo_opinions._opinion_boxes("http://x", {})), [])
        self.assertEqual(scanned, [])

    def test_a_look_alike_contract_is_rejected_even_though_the_search_returns_it(self):
        # A template hash names a contract's *code*: constant segregation makes it
        # shared by every tree with the same code and different constants, and such a
        # box is invisible to every other reader of the chain.
        look_alike = box(ergo_tree="19" + "ff" * 8)
        self.assertFalse(
            ergo_opinions._is_opinion_about(look_alike, NODE_TYPE_NFT, NODE_ID)
        )


class ReportTests(unittest.TestCase):
    def reputation(self, opinions, own=()):
        return NodeReputation(
            node_id=NODE_ID,
            own_proof_id=OWN_PROOF,
            opinions=list(opinions),
            own=list(own),
            errors={},
        )

    def test_the_standing_total_holds_every_opinion_whenever_it_arrived(self):
        old = opinion(proof="old", amount=1, assigned=4, at=NOW - 200 * DAY_SECONDS)
        data = report(self.reputation([old]), now=NOW)
        self.assertEqual(data["standing"]["positive"], 0.25)

    def test_the_report_offers_no_windows_to_read_reputation_over(self):
        # Not an omission. Revising an opinion spends its box and writes a new one, and
        # `submit_to_ledger` re-splits a node's whole supply on every submission, so
        # every date on a proof resets together. A window over them would report how
        # often the publisher republishes, dressed up as reputation earned that week --
        # a figure that would sit next to real money flows and read like one.
        data = report(self.reputation([opinion()]), now=NOW)
        self.assertNotIn("periods", data)
        self.assertIn("standing", data)
        # The per-opinion date survives, as the age of that box and nothing more.
        self.assertEqual(data["opinions"][0]["published_at"], NOW)

    def test_the_report_names_the_node_and_carries_the_opinions_behind_it(self):
        data = report(self.reputation([opinion()], own=[opinion(proof=OWN_PROOF)]), now=NOW)
        self.assertEqual(data["node_id"], NODE_ID)
        self.assertEqual(data["own_proof_id"], OWN_PROOF)
        self.assertEqual([item["proof_id"] for item in data["opinions"]], [PROOF])
        self.assertEqual([item["proof_id"] for item in data["own"]], [OWN_PROOF])
        self.assertEqual(data["read_at"], NOW)


if __name__ == "__main__":
    unittest.main()
