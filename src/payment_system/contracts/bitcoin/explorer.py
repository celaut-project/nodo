"""Reaching Bitcoin over a public HTTP API, and signing for it locally.

This is Ergo's posture, on Bitcoin: ``ledgers.ergo.NODE_URL`` defaults to somebody
else's public node and the wallet mnemonic lives in ``config.yaml``, so an operator runs
no Ergo infrastructure at all. Here ``EXPLORER_URL`` points at any server that speaks the
Esplora HTTP API -- blockstream.info, mempool.space, or one you host -- and the key is
derived from ``ledgers.bitcoin.WALLET_MNEMONIC`` (see ``signer.py``). The explorer
answers questions about the chain; it never holds a key and never signs.

* **To be paid**, the wallet's single BIP-84 address is advertised, and incoming
  payments are read back off the explorer.
* **To pay**, the wallet's confirmed outputs are listed, a transaction is built and
  signed here, and the explorer is asked only to relay it. The transaction id it
  answers with is checked against the one computed locally.

The explorer is trusted for what it *reports*, and for nothing else: BIP-143 signs each
input's amount, so an output it misreports gives a transaction the network rejects, never
one that moves more than the wallet holds. Choose one you trust to tell the truth, and to
broadcast; a hosted instance of your own is the strictest answer.

The one address is a hot wallet like any other signing backend's: payments land in it and
the excess is swept to ``payments.COLD_WALLET`` when that is set.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import requests

from src.payment_system.contracts.bitcoin import signer
from src.payment_system.contracts.bitcoin.backend import BackendUnavailable
from src.utils.bitcoin_units import script_pubkey_for_address
from src.utils.config import ConfigManager

TIMEOUT_SECONDS = 30
# How many transactions one address page returns. Esplora's own page size; asking for
# fewer is not an option the API offers.
PAGE_SIZE = 25
# Spent outputs must have at least this many confirmations. Unconfirmed change is never
# spent: a chain of unconfirmed transactions is how one stuck payment becomes three.
SPEND_MIN_CONFIRMATIONS = 1
# How much of an explorer's refusal is repeated in an error. It is prose from a server
# this node does not control, so it is bounded.
ERROR_EXCERPT = 200


class ExplorerBackend:
    """The chain over HTTP, and the one wallet key that spends on it."""

    #: This backend holds a key, so it can sign and broadcast.
    can_pay = True

    def __init__(self, url: str, wallet: signer.WalletKey):
        self._url = url.rstrip("/")
        self._wallet = wallet

    @property
    def address(self) -> str:
        return self._wallet.address

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

    def _post(self, path: str, body: str) -> str:
        """POST ``body`` as plain text and return the plain-text answer."""
        try:
            response = requests.post(
                f"{self._url}{path}", data=body, timeout=TIMEOUT_SECONDS,
                headers={"content-type": "text/plain"},
            )
        except requests.exceptions.RequestException as exc:
            raise BackendUnavailable(
                f"explorer {path} failed: {type(exc).__name__}"
            ) from None
        text = response.text.strip()
        if response.status_code != 200:
            raise BackendUnavailable(
                f"explorer {path}: HTTP {response.status_code} {text[:ERROR_EXCERPT]}".rstrip()
            )
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
    def utxos(self, min_conf: int = SPEND_MIN_CONFIRMATIONS) -> List[signer.Utxo]:
        """The wallet's unspent outputs with at least ``min_conf`` confirmations."""
        entries = self._get(f"/address/{self.address}/utxo") or []
        tip = self._tip_height() if int(min_conf) > 0 else None
        found = []
        for entry in entries:
            if self._confirmations(entry.get("status") or {}, tip) < int(min_conf):
                continue
            found.append(signer.Utxo(
                txid=str(entry["txid"]), vout=int(entry["vout"]), value=int(entry["value"])
            ))
        return found

    def get_balance(self, min_conf: int = 1) -> int:
        """Spendable balance of the wallet's address, in satoshi.

        Counted from its outputs rather than from the address' totals, so ``min_conf``
        means what it says: a deeper threshold than "confirmed" is honoured, where the
        totals could only have offered the one.
        """
        return sum(u.value for u in self.utxos(min_conf))

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
        """Recent transactions of the wallet's address, newest first.

        Shaped like Core's ``listtransactions``: a transaction that spends one of the
        wallet's outputs is a ``send`` for what went to *other* addresses (the fee is
        not part of it, nor is change), and one that only pays the address is a
        ``receive``. Both are read off the one address, which is the whole wallet.
        """
        address = self.address
        tip = self._tip_height()
        rows: List[Dict[str, Any]] = []
        for transaction in (self._get(f"/address/{address}/txs") or [])[: int(limit)]:
            status = transaction.get("status") or {}
            received = 0
            sent: List[Tuple[str, int]] = []
            for output in transaction.get("vout") or []:
                to = str(output.get("scriptpubkey_address") or "")
                value = int(output.get("value") or 0)
                if to == address:
                    received += value
                elif to:
                    sent.append((to, value))
            spends_ours = any(
                str((vin.get("prevout") or {}).get("scriptpubkey_address") or "") == address
                for vin in transaction.get("vin") or []
            )
            if spends_ours and sent:
                category, amount, counterparty = "send", -sum(v for _, v in sent), sent[0][0]
            elif not spends_ours and received > 0:
                category, amount, counterparty = "receive", received, address
            else:
                continue
            rows.append({
                "txid": str(transaction.get("txid") or ""),
                "category": category,
                "amount": amount / 100_000_000,
                "confirmations": self._confirmations(status, tip),
                "time": int(status.get("block_time") or 0),
                "address": counterparty,
            })
        return rows

    # ------------------------------------------------------------------ the wallet
    def receive_address(self, label: str = "nodo") -> str:
        """The address this node is paid at: derived, so the same on every call."""
        return self.address

    def new_address(self, label: str = "nodo") -> str:
        """Nothing is minted: the wallet has the one address, and this is it.

        Present because the contract's ``init()`` asks a signing backend for an address
        the first time. Another call would be another derivation index, and the
        advertised ``script`` is one fixed ``scriptPubKey`` by design.
        """
        return self.address

    # ------------------------------------------------------------------ writes
    def send_to(self, address: str, amount_sat: int, *, op_return: Optional[bytes] = None,
                fee_rate_sat_vb: float,
                subtract_fee_from_amount: bool = False) -> str:
        """Pay one address, optionally carrying ``op_return``. Returns the txid."""
        return self.send_many(
            [(address, amount_sat)], op_return=op_return,
            fee_rate_sat_vb=fee_rate_sat_vb,
            subtract_fee_from_outputs=[0] if subtract_fee_from_amount else None,
        )

    def send_many(self, outputs: Sequence[Tuple[str, int]], *,
                  op_return: Optional[bytes] = None,
                  fee_rate_sat_vb: float,
                  subtract_fee_from_outputs: Optional[Sequence[int]] = None) -> str:
        """Build, sign and relay one transaction paying every output. Returns the txid.

        One transaction, so a donation split across wallets costs one fee. Nothing is
        sent unless the whole transaction was built and every signature verified, and
        the explorer is believed about the txid only if it agrees with the one computed
        here.
        """
        if not outputs:
            raise BackendUnavailable("nothing to send: no outputs")
        network = self._wallet.network
        scripts = []
        for address, amount_sat in outputs:
            script = script_pubkey_for_address(address, network=network)
            if script is None:
                raise BackendUnavailable(
                    f"{address!r} is not a valid {network} address; nothing was sent"
                )
            scripts.append((script, int(amount_sat)))
        try:
            built = signer.build_transaction(
                self._wallet, self.utxos(SPEND_MIN_CONFIRMATIONS), scripts,
                fee_rate=fee_rate_sat_vb, op_return=op_return,
                subtract_fee_from=subtract_fee_from_outputs,
            )
        except signer.InsufficientFunds as exc:
            raise BackendUnavailable(f"insufficient funds: {exc}; nothing was sent") from None
        except ValueError as exc:
            raise BackendUnavailable(f"could not build the transaction: {exc}") from None

        relayed = self._post("/tx", built.hex)
        if relayed.lower() != built.txid:
            # The transaction may well be on its way, which is exactly why this is not a
            # quiet success: what this node recorded is not what the explorer says.
            raise BackendUnavailable(
                f"the explorer answered {relayed[:ERROR_EXCERPT]!r} to a broadcast of "
                f"{built.txid}"
            )
        return built.txid


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


def _network() -> str:
    return str(ConfigManager().get("ledgers.bitcoin.NETWORK") or "mainnet").strip()


def _wallet_key() -> signer.WalletKey:
    """The key for the configured mnemonic. ``BackendUnavailable`` when it cannot be."""
    config = ConfigManager()
    mnemonic = str(config.get("ledgers.bitcoin.WALLET_MNEMONIC") or "").strip()
    if not mnemonic:
        raise BackendUnavailable(
            "ledgers.bitcoin.WALLET_MNEMONIC is empty. The node generates one on load "
            "unless BACKEND is core, so this is a config that was edited by hand"
        )
    passphrase = str(config.get("ledgers.bitcoin.WALLET_PASSPHRASE") or "")
    try:
        return signer.derive_wallet_key(mnemonic, passphrase, _network())
    except ValueError as exc:
        # The message never carries the words: see `signer.derive_wallet_key`.
        raise BackendUnavailable(f"ledgers.bitcoin.WALLET_MNEMONIC: {exc}") from None


def configuration_reason() -> Optional[str]:
    """Why this backend cannot be used, or ``None`` when it can be tried.

    Config only -- no socket -- because the registry asks on the payment path.
    """
    if not str(ConfigManager().get("ledgers.bitcoin.EXPLORER_URL") or "").strip():
        return "ledgers.bitcoin.EXPLORER_URL is not set"
    try:
        _wallet_key()
    except BackendUnavailable as exc:
        return str(exc)
    return None


def backend() -> ExplorerBackend:
    url = str(ConfigManager().get("ledgers.bitcoin.EXPLORER_URL") or "").strip()
    if not url:
        raise BackendUnavailable("ledgers.bitcoin.EXPLORER_URL is not set")
    return ExplorerBackend(url=url, wallet=_wallet_key())
