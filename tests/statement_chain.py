"""A ledger of one account, built the way a node builds it, to make statements from.

``FakeLedger`` chains its rows with the real hash (``ledger.movement_hash``) and gives
the statement that a node would answer for a range of them, so a test can say what a
peer shows and then change one thing: drop a row, change an amount, cut the history, as
the retention does.
"""
from typing import List, Optional

from protos import celaut_pb2
from src.database import ledger


class FakeLedger:

    def __init__(self, account: str, start_ts: int = 1000):
        self.account = account
        self.ref = ledger.account_ref(account)
        self.ts = start_ts
        self.rows: List[dict] = []
        self.first = 1  # the seq of the first row that the node still holds

    def add(self, amount: int, kind: str, ref: str = "", detail: str = "",
            ts: Optional[int] = None) -> dict:
        previous = self.rows[-1] if self.rows else None
        self.ts = self.ts + 10 if ts is None else ts
        seq = previous["seq"] + 1 if previous else 1
        balance = (int(previous["balance_after"]) if previous else 0) + amount
        prev_hash = previous["hash"] if previous else ledger.genesis_hash_of_ref(self.ref)
        row = {"seq": seq, "ts": self.ts, "amount": amount, "balance_after": balance,
               "kind": kind, "ref": ref, "detail": detail, "prev_hash": prev_hash}
        row["hash"] = ledger.movement_hash(
            prev_hash, seq, self.ts, amount, balance, kind, ref or None, detail or None)
        self.rows.append(row)
        return row

    def purge_before(self, seq: int) -> None:
        """The retention: the node deletes the rows before ``seq``."""
        self.first = seq

    @property
    def balance(self) -> int:
        return int(self.rows[-1]["balance_after"]) if self.rows else 0

    def mark_at(self, seq: int):
        """The reader's mark after checking up to ``seq`` (a ``statement_check.Mark``)."""
        from src.manager.statement_check import Mark
        row = self.rows[seq - 1]
        return Mark(seq, row["hash"], int(row["balance_after"]), row["ts"])

    def statement(self, from_seq: int = 1, to_seq: int = 0, limit: int = 1000,
                  ts: Optional[int] = None, retention_days: int = 30,
                  oldest: Optional[int] = None) -> celaut_pb2.BalanceStatement:
        head = self.rows[-1] if self.rows else None
        held = [row for row in self.rows if row["seq"] >= self.first]
        chosen = [] if from_seq <= 0 else [
            row for row in held
            if row["seq"] >= from_seq and (to_seq <= 0 or row["seq"] <= to_seq)
        ][:limit]
        out = celaut_pb2.BalanceStatement(
            account_ref=self.ref, from_seq=from_seq, to_seq=to_seq,
            balance=celaut_pb2.Amount(n=str(self.balance)),
            head_seq=head["seq"] if head else 0,
            head_hash=head["hash"] if head else ledger.genesis_hash_of_ref(self.ref),
            oldest_seq=(held[0]["seq"] if held else 0) if oldest is None else oldest,
            retention_days=retention_days,
            ts=self.ts + 1 if ts is None else ts,
        )
        for row in chosen:
            out.movements.add(
                seq=row["seq"], ts=row["ts"], kind=row["kind"], instance_ref=row["ref"],
                detail=row["detail"], prev_hash=row["prev_hash"],
                amount=celaut_pb2.Amount(n=str(row["amount"])),
                balance_after=celaut_pb2.Amount(n=str(row["balance_after"])),
            )
        return out
