# Benchmarks

A score that a node measures on an architecture or a device: integer operations per
second of one core, memory bandwidth, hashes per second.

Class: `benchmark`.

## Places

- `Resources.benchmarks` of a container or of a device: a requirement. The node
  compares each score at each placement: the start, the wake, a move.
- `ArchitectureResources.benchmarks`: an announcement of what the node measured. A
  score that the node did not measure is absent, never 0.

## Value

An integer in the unit of the formal. More is better: write a latency as its inverse.

## The formal must give

`unit`, the statistic (`stat`: a mean, or a percentile with its window, for a score
that must be stable), and how the node measures it (`method`), so that two nodes
measure the same thing.
