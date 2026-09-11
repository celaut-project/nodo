"""Reaching Bitcoin over a public HTTP API, to be **paid** without running anything.

Ergo's posture is a remote public node plus a local key: `ledgers.ergo.NODE_URL`
defaults to somebody else's node and the wallet mnemonic lives in `config.yaml`. So an
operator runs no Ergo infrastructure at all. Bitcoin Core over RPC is the opposite --
Core signs, so it has to be a node you would hand your wallet to -- and requiring that
of anyone who merely wants to *receive* BTC is a heavier ask than this project makes
anywhere else.

This backend closes that gap for the receiving side. Selected as
`ledgers.bitcoin.BACKEND: explorer`, it implements the read half of the `ChainBackend`
surface against whatever `EXPLORER_URL` points at, which has to speak the Esplora HTTP
API: blockstream.info, mempool.space, or a self-hosted instance. It **refuses the
rest**: it holds no key, so it cannot sign, and it says so rather than failing
somewhere further in.

What that buys, and what it does not:

* A node can be **paid** in BTC with no bitcoind and no Bitcoin key anywhere. Being
  paid is the side that matters to a node earning money.
* It cannot **pay**. `can_pay` is False, so `check_sender_balance` answers no and the
  payer falls through to another payment system -- funding is the selection, and a
  wallet that cannot sign has no funding. Nothing is broadcast and nothing raises
  mid-payment.
* It cannot mint a receiving address either, and it holds no hot wallet it could
  sweep to cold later -- so it is paid into `COLD_WALLET` directly, which is the one
  Bitcoin address an operator running this backend has any reason to own. That is
  checked before the contract is offered.

Signing locally instead -- a seed in `config.yaml`, like Ergo's -- would need raw
segwit construction, BIP-143 sighashes and UTXO selection. Every line of that moves
money, and none of it is needed to be paid.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import requests

from src.payment_system.contracts.bitcoin.backend import BackendUnavailable
from src.utils.bitcoin_units import script_pubkey_from_address
from src.utils.config import ConfigManager

#: This backend holds no key. Read by the contract to decide where it is paid: a backend
#: that signs is paid into its own hot wallet, this one into the cold wallet.
CAN_SIGN = False
COLD_WALLET_KEY = "ledgers.bitcoin.payments.COLD_WALLET"

TIMEOUT_SECONDS = 30
# How many transactions one address page returns. Esplora's own page size; asking for
# fewer is not an option the API offers.
PAGE_SIZE = 25


class ExplorerBackend:
    """The read half of the chain, over HTTP. Holds no key and signs nothing."""

    #: No wallet, no signature: this backend cannot move money, and says so up front.
    can_pay = False

    def __init__(self, url: str):
        self._url = url.rstrip("/")

    # ------------------------------------------------------------------ transport
    def _get(self, path: str) -> Any:
        url = f"{self._url}{path}"
        try:
            response = requests.get(url, timeout=TIMEOUT_SECONDS)
        except requests.exceptions.RequestException as exc:
            raise BackendUnavailable(
                f"explorer {path} failed: {type(exc).__name__}"
            ) from None
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise BackendUnavailable(f"explorer {path}: HTTP {response.status_code}")
        text = response.text.strip()
        if not text:
            return None
        try:
            return response.json()
        except ValueError:
            # `/blocks/tip/height` and `/fee-estimates` answer plain text or JSON
            # depending on the deployment, so a non-JSON body is a value, not an error.
            return text

    def _tip_height(self) -> Optional[int]:
        value = self._get("/blocks/tip/height")
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _confirmations(status: Dict[str, Any], tip: Optional[int]) -> int:
        """Confirmations for a transaction's ``status`` block.

        Esplora reports the block a transaction landed in, not how deep it is, so the
        depth is derived against the tip. Unconfirmed is 0, and a missing tip is 0 too
        -- "I could not tell" must never read as "deep enough".
        """
        if not status or not status.get("confirmed"):
            return 0
        height = status.get("block_height")
        if tip is None or height is None:
            return 0
        return max(0, int(tip) - int(height) + 1)

    # ------------------------------------------------------------------ reads
    def get_balance(self, min_conf: int = 1) -> int:
        """Confirmed balance of the configured receiving address, in satoshi.

        The *address'* balance, not a wallet's: this backend has no wallet. Esplora's
        `chain_stats` counts only confirmed transactions, which is what `min_conf`
        asks for at its default; a deeper threshold is not something the API can
        express, so it is honoured as "confirmed" rather than approximated.
        """
        address = _receiving_address()
        stats = (self._get(f"/address/{address}") or {}).get("chain_stats") or {}
        funded = int(stats.get("funded_txo_sum") or 0)
        spent = int(stats.get("spent_txo_sum") or 0)
        return max(0, funded - spent)

    def estimate_fee_rate(self, target_conf: int) -> Optional[float]:
        """Fee rate in sat/vB for ``target_conf`` blocks, or None when unknown.

        Only ever used to *size* a deposit here, never to build a transaction: this
        backend cannot build one.
        """
        estimates = self._get("/fee-estimates")
        if not isinstance(estimates, dict):
            return None
        # Esplora keys its estimates by target in blocks, and not every target is
        # present. The nearest target at or above the one asked for is the honest
        # answer; a lower one would promise a confirmation nobody estimated.
        candidates = []
        for key, rate in estimates.items():
            try:
                candidates.append((int(key), float(rate)))
            except (TypeError, ValueError):
                continue
        for blocks, rate in sorted(candidates):
            if blocks >= int(target_conf):
                return rate
        return max((rate for _, rate in candidates), default=None)

    def list_received(self, address: str, min_conf: int) -> List[Dict[str, Any]]:
        """Transactions paying ``address`` with at least ``min_conf`` confirmations.

        Shaped like Core's ``listreceivedbyaddress`` so the contract reads one thing.

        Paginated to the end, because the caller is looking for one particular payment
        rather than browsing recent ones. ``/address/:addr/txs`` returns only the newest
        page, so on a node paid by several peers a deposit that has slipped past it
        would read as "no confirmed transaction carries the token" -- rejecting a
        payment already on-chain, with the money in this node's wallet. Continued with
        ``/txs/chain/:last_seen_txid``, which is Esplora's own way of walking back.
        """
        tip = self._tip_height()
        txids: List[str] = []
        seen: set = set()
        path = f"/address/{address}/txs"
        while True:
            page = self._get(path) or []
            fresh = [
                entry for entry in page
                if entry.get("txid") and str(entry["txid"]) not in seen
            ]
            if not fresh:
                break
            cursor = None
            for entry in fresh:
                tx_id = str(entry["txid"])
                seen.add(tx_id)
                status = entry.get("status") or {}
                if status.get("confirmed"):
                    # Only a confirmed transaction can be the cursor: the first page
                    # carries the mempool too, and `/txs/chain` walks the chain.
                    cursor = tx_id
                if self._confirmations(status, tip) >= int(min_conf):
                    txids.append(tx_id)
            # A short page is the last one. The first page also carries the mempool, so
            # it can be longer than PAGE_SIZE; only a *short* one ends the walk.
            if len(page) < PAGE_SIZE or cursor is None:
                break
            path = f"/address/{address}/txs/chain/{cursor}"
        return [{"txids": txids}] if txids else []

    def tx_status(self, txid: str) -> Dict[str, Any]:
        """``{"confirmations": n}`` for ``txid``.

        Never negative here: Esplora simply stops reporting a transaction that left the
        chain, and a 404 reads as zero confirmations -- which the payer's wait treats as
        "not yet", and its timeout bounds.
        """
        tip = self._tip_height()
        transaction = self._get(f"/tx/{txid}")
        if not transaction:
            return {"confirmations": 0}
        return {
            "confirmations": self._confirmations(transaction.get("status") or {}, tip)
        }

    def raw_transaction(self, txid: str) -> Dict[str, Any]:
        """The transaction's outputs, in the normalised shape the contract reads."""
        transaction = self._get(f"/tx/{txid}") or {}
        outputs: List[Dict[str, Any]] = []
        for output in transaction.get("vout") or []:
            script_hex = str(output.get("scriptpubkey") or "").lower()
            payload = None
            if str(output.get("scriptpubkey_type") or "") == "op_return":
                payload = _op_return_payload(script_hex)
            outputs.append({
                "script_hex": script_hex,
                # Esplora counts in satoshi already, which is what everything here uses.
                "value_sat": int(output.get("value") or 0),
                "op_return": payload,
            })
        return {"outputs": outputs}

    def list_transactions(self, limit: int) -> List[Dict[str, Any]]:
        """Recent transactions at the receiving address, newest first.

        Shaped like Core's ``listtransactions``, with one difference this backend
        cannot avoid: it sees an *address*, not a wallet, so it can only report what
        that address received. A payment this node made from elsewhere is not visible
        to it, and reporting a guess would be worse than reporting nothing.
        """
        address = _receiving_address()
        tip = self._tip_height()
        rows: List[Dict[str, Any]] = []
        for transaction in (self._get(f"/address/{address}/txs") or [])[: int(limit)]:
            received = sum(
                int(output.get("value") or 0)
                for output in transaction.get("vout") or []
                if str(output.get("scriptpubkey_address") or "") == address
            )
            if received <= 0:
                continue
            status = transaction.get("status") or {}
            rows.append({
                "txid": str(transaction.get("txid") or ""),
                "category": "receive",
                "amount": received / 100_000_000,
                "confirmations": self._confirmations(status, tip),
                "time": int(status.get("block_time") or 0),
                "address": address,
            })
        return rows

    # ------------------------------------------------------------------ refusals
    def _cannot_pay(self, what: str):
        return BackendUnavailable(
            f"the Bitcoin backend is read-only, so it cannot {what}. It holds no key "
            "and signs nothing. Configure ledgers.bitcoin.BACKEND: core with an RPC "
            "URL and credentials to pay out in BTC; see docs/BITCOIN.md."
        )

    def new_address(self, label: str = "nodo") -> str:
        raise self._cannot_pay("mint an address")

    def send_to(self, address: str, amount_sat: int, **_) -> str:
        raise self._cannot_pay("send")

    def send_many(self, outputs, **_) -> str:
        raise self._cannot_pay("send")


def _op_return_payload(script_hex: str) -> Optional[bytes]:
    """The data an `OP_RETURN` script carries, from the raw script.

    `6a` is OP_RETURN. What follows is a push: a length byte below 0x4c, or `4c`/`4d`
    with an explicit length. Anything else is some other script's business.
    """
    try:
        script = bytes.fromhex(script_hex)
    except ValueError:
        return None
    if len(script) < 2 or script[0] != 0x6A:
        return None
    opcode = script[1]
    if opcode < 0x4C:
        return script[2:2 + opcode] or None
    if opcode == 0x4C and len(script) >= 3:
        return script[3:3 + script[2]] or None
    if opcode == 0x4D and len(script) >= 4:
        length = int.from_bytes(script[2:4], "little")
        return script[4:4 + length] or None
    return None


def receiving_address() -> str:
    """The address this node is paid at on a read-only backend: the cold wallet.

    A backend that signs is paid into a hot wallet and sweeps the excess to cold. This
    one holds no key, so there is no hot wallet to be paid into and nothing that could
    ever move a coin out of one -- which leaves the cold wallet as the address payers
    should be sent to in the first place, and leaves the operator one address to own
    rather than two.

    ``""`` when it is unset; the caller decides how loudly that matters.
    """
    return str(ConfigManager().get(COLD_WALLET_KEY) or "").strip()


def _receiving_address() -> str:
    address = receiving_address()
    if not address:
        raise BackendUnavailable(
            f"{COLD_WALLET_KEY} is not set. A read-only backend cannot ask a node for "
            "an address, and it is paid into the cold wallet directly."
        )
    return address


def configuration_reason() -> Optional[str]:
    """Why this backend cannot be used, or ``None`` when it can be tried.

    Config only -- no socket -- because the registry asks on the payment path.
    """
    config = ConfigManager()
    if not str(config.get("ledgers.bitcoin.EXPLORER_URL") or "").strip():
        return "ledgers.bitcoin.EXPLORER_URL is not set"
    cold_wallet = receiving_address()
    if not cold_wallet:
        return (
            f"{COLD_WALLET_KEY} is not set, and a read-only backend cannot ask a node "
            "for an address -- it is paid into the cold wallet directly, so set the "
            "address you want to be paid at"
        )
    network = str(config.get("ledgers.bitcoin.NETWORK") or "mainnet").strip()
    if script_pubkey_from_address(cold_wallet, network=network) is None:
        return (
            f"{COLD_WALLET_KEY}={cold_wallet!r} is not a segwit {network} address, and "
            "it is what payers are advertised as a scriptPubKey on this backend"
        )
    return None


def backend() -> ExplorerBackend:
    url = str(ConfigManager().get("ledgers.bitcoin.EXPLORER_URL") or "").strip()
    if not url:
        raise BackendUnavailable("ledgers.bitcoin.EXPLORER_URL is not set")
    return ExplorerBackend(url=url)
