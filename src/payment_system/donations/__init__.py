"""Donations: paying a share of earnings, and reading other nodes' share off the chain.

A donation is how a node operator funds the development of the software they run, and
the reason it is worth making is that *other* nodes weigh it when they choose who to
delegate work to. That is the whole design constraint. `nodo` is open source, so
anything a node grants itself for donating can be patched out and kept; only credit
granted by other nodes, computed from the chain and never from what the donor claims,
survives being forked.

Hence the split across this package:

* :mod:`.config` -- the two wallet lists, per ledger. Who we *fund* is a prospective
  bet that costs money; whose contributions we *count* is a retrospective judgement
  that is free. They are separate lists on purpose and are never merged.
* :mod:`.split` -- pure arithmetic: weight normalisation and what a payout looks like.
* :mod:`.accrual` -- a share of each incoming payment, owed in the asset it arrived in.
* :mod:`.indexer` -- what other nodes donated, read off each chain independently.
* :mod:`.credit` -- that index turned into one bounded number per peer, for the balancer.

Two things this package deliberately does not do: it never sends or receives a donation
figure over the protocol (self-declared, therefore worthless), and it never lets a
donation lookup fail into a routing decision (see :mod:`.credit`).
"""
