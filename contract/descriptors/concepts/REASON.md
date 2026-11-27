# Concepts

A term that other formals use, and that has variants or that more than one descriptor
uses: for example a level of an instruction set, a system call interface, a size of a
memory page, a kind of kernel. A concept is a part of other descriptors (rule 3.12 of
`../REASON.md`). Its variants are siblings: a formal that changes from one variant to
another changes one id.

Class: `concept`.

## Places

None in `celaut.proto`. A concept is only in the formal of another descriptor, by its
id.

## Value

None.

## The formal must give

What the term is, with only the terms of the base (rule 3.11 of `../REASON.md`): words
of ASD-STE100, names of `celaut.proto`, numbers with a unit of the base, and the ids of
other descriptors and documents. Give the variant in the formal (for example the level
"v1" of an instruction set), so that two variants have two formals.

A concept must pass the test of the probe (rule 3.11 of `../REASON.md`): a reader with
no network must be able to build what the concept names. A test that a machine passes
(for example the bits that CPUID reports) identifies a machine, but does not define it.
So the concept also names the document that defines the thing (for example the manual
of an instruction set), by its id.
