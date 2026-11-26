# Terms

The terms of an offer and of a node: what the node and the caller agree to for one
instance. This folder only holds two folders, one for each class, because the caller
acts in two ways with no need to read the formal:

- `conditions/`: a term that binds the caller. A caller that does not know it does not
  accept the offer.
- `promises/`: a term that binds the node. A caller that does not know it ignores it.

The test: if the caller is better off when the term is absent, it is a condition. If
the caller is equal or worse off when it is absent, it is a promise (REASON 3.2).
