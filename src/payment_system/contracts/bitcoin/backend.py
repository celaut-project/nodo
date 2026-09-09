"""Bitcoin Core over JSON-RPC. The only thing in this package that touches a network.

An operator who wants to be paid in Bitcoin runs a Bitcoin node -- the same posture the
project takes everywhere else, and the reason this needs **no new heavyweight
dependency and no JVM**. Core signs, broadcasts, counts confirmations and keeps the
watch-only view of the receiving address; nothing here holds a key or builds a script.

The surface is deliberately narrow, and named for what the payment flow asks rather than
for Core's method names, so a read-only HTTP backend (Esplora) can be put behind the
same calls later for a node that only wants to *receive*. That is not implemented here;
the door is just left open.

Two rules this module keeps:

* **The RPC password never reaches a log.** Not in a URL, not in an error, not in a
  repr. Every failure is reported as the method that failed and the status, and the
  auth header is built where it cannot be interpolated into a message.
* **Every call has a timeout.** This is reached from the payment path and from the
  manager tick; a hung socket must not hold either for ever.
"""
from __future__ import annotations

import json
from base64 import b64encode
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from src.utils.config import ConfigManager
from src.utils.logger import LOGGER

# Long enough for a node under load to answer a wallet call, short enough that a hung
# socket does not hold the payment path or the manager tick.
TIMEOUT_SECONDS = 30


class BackendUnavailable(Exception):
    """The node could not be reached or would not answer.

    *Undetermined*, never a verdict: "no payment arrived" and "I could not look" are
    different answers, and treating the second as the first is how an honest payment
    gets rejected with the money already on-chain.
    """


class ChainBackend:
    """Bitcoin Core over JSON-RPC: the full surface, reads and writes.

    The surface itself -- ``get_balance``, ``list_received``, ``tx_status``,
    ``raw_transaction``, ``list_transactions``, ``estimate_fee_rate``, ``new_address``,
    ``send_to``, ``send_many`` -- is what the payment flow asks for. A different
    implementation of it is a different way to reach Bitcoin, not a different contract:
    see ``esplora.py``, which implements the read half and refuses the rest.
    """

    #: This backend holds a wallet, so it can sign and broadcast.
    can_pay = True

    def __init__(self, url: str, wallet: Optional[str] = None,
                 auth: Optional[str] = None):
        self._url = url.rstrip("/")
        self._wallet = wallet or ""
        # Pre-encoded, so a credential cannot be formatted into a log line by accident.
        self._auth_header = f"Basic {auth}" if auth else None
        self._id = 0

    # ------------------------------------------------------------------ transport
    def _call(self, method: str, params: Optional[List[Any]] = None,
              *, wallet_scoped: bool = True) -> Any:
        """One JSON-RPC call. Raises :class:`BackendUnavailable`, never leaks the auth."""
        self._id += 1
        url = self._url
        if wallet_scoped and self._wallet:
            url = f"{url}/wallet/{self._wallet}"
        headers = {"content-type": "application/json"}
        if self._auth_header:
            headers["authorization"] = self._auth_header

        payload = json.dumps(
            {"jsonrpc": "1.0", "id": self._id, "method": method, "params": params or []}
        )
        try:
            response = requests.post(
                url, data=payload, headers=headers, timeout=TIMEOUT_SECONDS
            )
        except requests.exceptions.RequestException as exc:
            # `exc` can carry the request URL, which carries no credential (the auth is
            # a header), but the method and the class are all a reader needs.
            raise BackendUnavailable(
                f"bitcoind {method} failed: {type(exc).__name__}"
            ) from None
        if response.status_code == 401:
            raise BackendUnavailable(
                f"bitcoind refused the credentials on {method} (HTTP 401). Check "
                "RPC_COOKIE_PATH, or RPC_USER and RPC_PASSWORD."
            )
        if response.status_code >= 400 and not response.content:
            raise BackendUnavailable(f"bitcoind {method}: HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise BackendUnavailable(
                f"bitcoind {method}: unreadable response (HTTP {response.status_code})"
            ) from None
        error = body.get("error")
        if error:
            raise BackendUnavailable(
                f"bitcoind {method}: {error.get('message', 'unknown error')} "
                f"(code {error.get('code')})"
            )
        return body.get("result")

    # ------------------------------------------------------------------ the surface
    def get_balance(self, min_conf: int = 1) -> int:
        """Confirmed wallet balance, in integer satoshi.

        Core answers in BTC as a JSON number. Parsed through ``Decimal`` on its string
        form rather than through the float it arrives as: a float is exactly the way to
        lose a satoshi on a large balance, and every arithmetic step after this is
        integers.
        """
        from decimal import Decimal

        btc = self._call("getbalance", ["*", int(min_conf)])
        return int((Decimal(str(btc)) * 100_000_000).to_integral_value())

    def new_address(self, label: str = "nodo") -> str:
        """A receiving address for this node's wallet.

        P2WPKH, named explicitly rather than left to Core's default, so the advertised
        contract and the fee/vsize estimate describe the same kind of output.
        """
        return str(self._call("getnewaddress", [label, "bech32"]))

    def estimate_fee_rate(self, target_conf: int) -> Optional[float]:
        """Fee rate in sat/vB for confirmation within ``target_conf`` blocks.

        ``None`` when Core will not estimate -- a fresh node, or regtest with no fee
        history. The caller decides what to do about it; guessing a rate here would put
        a made-up number on a transaction.
        """
        result = self._call("estimatesmartfee", [int(target_conf)], wallet_scoped=False)
        if not isinstance(result, dict):
            return None
        btc_per_kvb = result.get("feerate")
        if btc_per_kvb in (None, "", 0):
            return None
        # BTC/kvB -> sat/vB: 1e8 sat per BTC, 1000 vB per kvB.
        return float(btc_per_kvb) * 100_000_000 / 1000

    def send_to(self, address: str, amount_sat: int, *, op_return: Optional[bytes] = None,
                fee_rate_sat_vb: Optional[float] = None) -> str:
        """Pay one address, optionally carrying ``op_return``. Returns the txid."""
        return self.send_many(
            [(address, amount_sat)], op_return=op_return, fee_rate_sat_vb=fee_rate_sat_vb
        )

    def send_many(self, outputs: List[Tuple[str, int]], *,
                  op_return: Optional[bytes] = None,
                  fee_rate_sat_vb: Optional[float] = None) -> str:
        """Pay several addresses in one transaction, and return the txid.

        One transaction rather than one each, because the fee is per transaction: a
        donation split across three wallets should cost one fee, not three.

        Built, funded, signed and broadcast by Core in four calls rather than one
        ``sendmany``, because ``sendmany`` cannot attach an ``OP_RETURN`` -- and the
        ``OP_RETURN`` is what ties a payment to its deposit token, the way Ergo's
        register R4 does.
        """
        from decimal import Decimal

        if not outputs:
            raise BackendUnavailable("nothing to send: no outputs")
        # Core takes BTC as a decimal string, one mapping per output.
        core_outputs: List[Dict[str, Any]] = [
            {address: str(Decimal(int(amount_sat)) / 100_000_000)}
            for address, amount_sat in outputs
        ]
        if op_return:
            core_outputs.append({"data": op_return.hex()})

        raw = self._call("createrawtransaction", [[], core_outputs])
        # Change last, so the outputs the caller asked for keep the positions it chose.
        options: Dict[str, Any] = {"changePosition": len(core_outputs)}
        if fee_rate_sat_vb is not None:
            options["fee_rate"] = float(fee_rate_sat_vb)
        funded = self._call("fundrawtransaction", [raw, options])
        signed = self._call("signrawtransactionwithwallet", [funded["hex"]])
        if not signed.get("complete"):
            raise BackendUnavailable(
                "bitcoind could not fully sign the transaction; nothing was broadcast"
            )
        return str(self._call("sendrawtransaction", [signed["hex"]], wallet_scoped=False))

    def list_received(self, address: str, min_conf: int) -> List[Dict[str, Any]]:
        """Transactions paying ``address`` with at least ``min_conf`` confirmations.

        Watch-only included: the receiving address belongs to this node's wallet, and a
        node that imported it as watch-only is still being paid into it.
        """
        result = self._call(
            "listreceivedbyaddress", [int(min_conf), False, True, address]
        )
        return list(result or [])

    def list_transactions(self, limit: int) -> List[Dict[str, Any]]:
        """The wallet's most recent transactions, newest first.

        Core returns them oldest-first, which is the opposite of what a history page
        wants, so they are reversed here rather than in every caller.
        """
        result = self._call("listtransactions", ["*", int(limit), 0, True]) or []
        return list(reversed(list(result)))

    def tx_status(self, txid: str) -> Dict[str, Any]:
        """``gettransaction`` for ``txid``: its confirmations, and what it paid.

        ``confirmations`` can be **negative**, which is Core saying the transaction was
        replaced or reorged out. That is not "not yet confirmed" and must never be read
        as one.
        """
        return dict(self._call("gettransaction", [str(txid), True]) or {})

    def raw_transaction(self, txid: str) -> Dict[str, Any]:
        """The transaction's outputs, normalised (see :func:`normalise_outputs`)."""
        decoded = self._call("getrawtransaction", [str(txid), True], wallet_scoped=False)
        return {"outputs": _core_outputs(decoded or {})}


def _core_outputs(decoded: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Core's ``getrawtransaction`` verbose output, in the shape the contract reads.

    The contract must not know any backend's JSON: Core reports a value in BTC as a
    float and an `OP_RETURN` as an `asm` string, while an Esplora API reports satoshi
    integers and a hex script. Normalising here is what lets a second way of reaching
    the chain be a different backend rather than a second contract.
    """
    from decimal import Decimal

    outputs: List[Dict[str, Any]] = []
    for output in decoded.get("vout") or []:
        script = output.get("scriptPubKey") or {}
        try:
            value_sat = int(
                (Decimal(str(output.get("value") or 0)) * 100_000_000).to_integral_value()
            )
        except Exception:
            value_sat = 0
        payload = None
        if script.get("type") == "nulldata":
            parts = str(script.get("asm") or "").split()
            if len(parts) >= 2:
                try:
                    payload = bytes.fromhex(parts[1])
                except ValueError:
                    payload = None
        outputs.append({
            "script_hex": str(script.get("hex") or "").lower(),
            "value_sat": value_sat,
            "op_return": payload,
        })
    return outputs


def _auth_from_config() -> Optional[str]:
    """Base64 ``user:password``, from a cookie file or from the two config keys.

    The cookie is preferred and is what Core writes for a local node, so the ordinary
    setup keeps no credential in `config.yaml` at all. Read per call rather than cached:
    Core rewrites the cookie on every restart.
    """
    config = ConfigManager()
    cookie_path = config.get("ledgers.bitcoin.RPC_COOKIE_PATH")
    if cookie_path:
        try:
            cookie = Path(str(cookie_path)).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            LOGGER(
                f"Could not read the bitcoind cookie at {cookie_path}: "
                f"{type(exc).__name__}. Falling back to RPC_USER/RPC_PASSWORD."
            )
        else:
            if cookie:
                return b64encode(cookie.encode("utf-8")).decode("ascii")

    user = config.get("ledgers.bitcoin.RPC_USER")
    password = config.get("ledgers.bitcoin.RPC_PASSWORD")
    if user and password:
        return b64encode(f"{user}:{password}".encode("utf-8")).decode("ascii")
    return None


def backend() -> ChainBackend:
    """The configured backend. Built per call, holding no connection of its own."""
    config = ConfigManager()
    url = str(config.get("ledgers.bitcoin.RPC_URL") or "").strip()
    if not url:
        raise BackendUnavailable("ledgers.bitcoin.RPC_URL is not set")
    return ChainBackend(
        url=url,
        wallet=str(config.get("ledgers.bitcoin.WALLET_NAME") or ""),
        auth=_auth_from_config(),
    )


def configuration_reason() -> Optional[str]:
    """Why this node cannot talk to a Bitcoin node, or ``None`` when it can try.

    Config only -- no socket. The registry asks this on the payment path and on every
    advertisement, so it may look at the config and the filesystem and nothing else.
    """
    config = ConfigManager()
    if not str(config.get("ledgers.bitcoin.RPC_URL") or "").strip():
        return "ledgers.bitcoin.RPC_URL is not set"
    if _auth_from_config() is None:
        return (
            "no bitcoind credentials: set ledgers.bitcoin.RPC_COOKIE_PATH to Core's "
            ".cookie file, or RPC_USER and RPC_PASSWORD"
        )
    return None
