# Pending: the redesign of celaut.proto

This file records what is still to do for the redesign of `celaut.proto`. The proto is
the reference for what is decided: its comments give each rule, and `contract/REASON.md`
gives the reasons. The history of each decision is in git. The code (Python, Rust TUI),
the generated `*_pb2.py` files, the tests and `compat.json` do not follow the proto
yet. Remove this file when the code follows the proto.

## 1. Decisions that are still open

- **`config-file`.** The proto removed `Container.config_declaration`, and the node
  always writes `/__config__`. Decide if a setting of `Init` is necessary, or if the
  path is a part of the architecture.
- **The Ergo reputation ledger.** Its formal holds the R4 type ids of the
  configuration of the node (`src/utils/ledger_descriptors.py:184-224`). Decide if the
  ids are a part of the ledger, or values.
- **The ledger tag `bitcoin` as a network with no addresses**
  (`src/manager/networks.py:149`). Decide if it is still necessary.

- **How the reader of the test of the probe reads a document** (REASON 2.9, rule 3.13
  of `contract/descriptors/REASON.md`). The reader finds the document in `uses` by its
  position. But a document names other documents by their public names (for example
  "IEEE 754"), and uses terms that it does not define. Two proposals: (a) the formal
  that names a document also gives a glossary, which joins each name and term of the
  part that it uses to an id; (b) the base of the reader of a document is the language
  of the document (for example English), not only ASD-STE100. Also, the formal must
  say how to read the part that applies, not only where it is.

## 2. The descriptors to write

The rules are in `contract/descriptors/REASON.md` and in the `REASON.md` of each folder.
The list of the descriptors that nodo needs, with where nodo implements each one
today, is the second part of `contract/descriptors/INDEX.md`. It is the work for the
agents that write the descriptors.

These descriptors do not pass the test of the probe yet (REASON 2.9, rule 3.11 of
`contract/descriptors/REASON.md`). Each one needs a document, with its license notice
(rule 3.13):

- **The instruction sets** (`concepts/x86-64-v1`, `concepts/armv8.0-a`). The bits of
  CPUID and of AT_HWCAP identify a processor, but do not define its instructions. The
  manuals of the makers define them.
- **The behavior of the system calls** (`concepts/linux-syscalls-x86-64`,
  `concepts/linux-syscalls-aarch64`). The line `behavior=` names "the behavior of Linux
  6.12". The source of Linux (GPL-2.0) can be a document, but it is large: find the
  smallest document that defines the behavior.
- **The tool of the benchmarks** (the six files of `quantities/benchmarks/`, the set
  `NOT_IN_USES` of `tests/test_descriptor_files.py`). They name a container image of
  busybox by the digest of its image index, and no file holds those bytes. Name a
  static binary of busybox (GPL-2.0) as a document.
- **The identity of a PCI or a USB model** (`concepts/pci-function-id`,
  `concepts/usb-device-id`). "Configuration space" and "device descriptor" are terms of
  the specifications of PCI-SIG and USB-IF.

## 3. Facts of nodo today that the implementation must change

The inventory of the code (November 2026) found these. Each one is a change of the
code, not of the contract.

- **Formal bytes.** Outside the layers of an address, the ledgers, the signature
  scheme and the proof of work, the code has formal bytes only for the six settings of
  `src/utils/policies.py`. The architectures, the resources, the benchmarks, the
  rates, the terms, the networks and the facts are tags, keys of `service.json` or
  keys of the configuration.
- **The legacy descriptor files** (the `LEGACY` set of
  `tests/test_descriptor_files.py`: settings, payment protocols, layers of an address,
  ledgers, the signature scheme) need the lines `class=` and `name=`. Their bytes are
  on the wire today, so this change goes with the code. The code must then read the
  tags and the prose from the files (for example `policies.make`), not make them.
- **`scarcity_max_multiplier` is sent in `Peer.rates`.** It is a promise, not a
  price.
- **The announced rates are truncated.** The configuration is in MU for each hour,
  the announcement is in MU for each second (`hour // 3600`), and the charge uses the
  price for each hour. Select units for the rate formals that give exact integers.
- **`Peer.limits` is never written.** nodo enforces its limits with keys of its
  configuration only. A caller cannot know them.
- **The facts `kernel` and `devices` of the start receipt are always empty**
  (`src/gateway/launcher/local_execution/local_execution.py:614`).
- **`node_protocol_stack`.** The node checks only the tag `celaut-gateway`
  (`src/virtualizers/microvm/rootfs.py:445-459`). The subset check is a TODO there.
  The packer writes the full gateway stack when `service.json` has `uses_gateway`.
- **Architectures** are matched by tag (`src/virtualizers/architecture.py:45-65`),
  and the configuration flag `TRUST_METADATA_ARCHITECTURE` reads tags from `Metadata`,
  which the proto removed. The node must announce each architecture that it can run,
  also the more general ones (for example `linux-arm64` and `linux-arm64-any-page`),
  and the packer must write the most general one that the binaries meet
  (`contract/descriptors/architectures/REASON.md`).
- **Benchmarks** are one run at the start of the node, with no statistic. The rule
  "use the score of the next larger working set" is removed: a node that did not
  measure a working set refuses, or measures it. A node measures again at a period
  that the operator sets, and announces a percentile only with enough measures (low
  priority, REASON 1.7).
- **CPU** is two numbers today (`cpu_quota`, `cpu_period`). The formal is one
  integer: thousandths of a core.
- **PR #649** (devices with VFIO, attach at the start only). Its tags
  `pci:<vendor>:<device>+<alg>:<hash>` become an architecture formal (the family that
  the software needs), a model fact, and the setting of the guest kernel. The price
  and the receipt read the model fact. A device benchmark that the node did not
  measure refuses. The receipt fields of its proto diff become `facts`. The PR writes
  the formals of `architectures/devices/` for the families that it can attach and
  test: one for each member (for example one for each CUDA compute capability).
- **`Descriptor.uses`.** The code sends no `uses` and does not check them. A writer must
  build them from the ids of the formal (`uses` of `src/utils/descriptor_file.py`
  gives the files), and a reader must refuse a descriptor whose `uses` do not match its
  formal (REASON 2.9).
- **Schemas.** `src/identity/protocol_schema.py` writes a schema as lines of
  structure (`schema.<name>=<num>:<cardinality>:<type>,...`), and the formal of
  `celaut-gateway` holds them. A schema names the `.proto` files as documents
  (REASON 1.8). The lines of structure can stay only as the check of a reader that
  decides that two schemas agree (REASON 2.5).
- **`TunnelRequest.slot`** is still a decimal text: it is not signed.
- **The proof of work of `GenerateClient`** sends a line of pseudo-code as its formal
  (`src/gateway/client_pow.py:52`). It must send the formal of
  `proof_of_work/blake2b-trailing-zeros.descriptor`.
- **A network share** names its network by its tag, and its id is sha256 over the tag
  (`src/utils/shared_filesystems.py:397-433`). The code follows `share-v2` (second part
  of `INDEX.md`): the position of the network in `Service.network`, and an id over
  the bytes of the domain, with the hash that the formal names by its type.
- **The packer writes the setting `notice` by default** (`src/utils/notice.py:120`).
  It must write it only when the author asks for it: a setting fails closed, and a
  service works with no notice.
- **A proof-of-work network keeps the `pow.*` keys that it does not know**, and does
  not enforce them (`src/manager/pow_networks.py`). It must refuse them, as the formal
  `networks/proof-of-work` says.

## 4. Known state of the branch

- `celaut_pb2.py` is not generated from the new proto, so the code builds the old
  messages.
- `tests/identity/test_protocol_doc.py` fails: `celaut_doc.binpb` is not generated
  from the new proto.
- Ten other tests fail on this branch for reasons outside this work (install, help,
  pricing, observe, update manifest).

## 5. Implementation stages

The contract is complete before nodo implements all of it (REASON 1.7).

1. Proto (draft done), and the core descriptors in `contract/descriptors/`.
2. The `Signed` envelope in nodo, the TUI and the Peer on the ledger. Remove the
   digest keys from the layer formal.
3. The tree of layers: announce it, find a path in it by exact bytes, and the subset
   check of `node_protocol_stack`. Move Chat and Observe to their own gRPC services.
4. Terms: pricing, schedule, receipts, balance movements.
5. Resources: cgroups and the virtual machine from the formals.
6. Placement: availability, benchmarks, devices.
7. CLI, TUI, docs, `compat.json`, and the `class=` and `name=` lines of the legacy
   descriptor files.

## 6. Work on the `.proto` files before the freeze

### 6.1. Move the reasons out of the comments (REASON 5.4)

**Why.** A schema names the `.proto` files as documents (REASON 1.8). Each byte of
these files, also each byte of a comment, is in the identity of every `Service` and of
every signed message. A comment that only explains why the design is so does not change
what a node does, but a change to it changes the id of every service. So the comments
must hold only rules, and the reasons must go to `contract/REASON.md`, which is in no
schema.

**When.** One time, just before the freeze of `celaut.proto`, together with the check
of REASON 7.4. Do not do it before: until the freeze the files change, and no schema
is published.

**Files.** `contract/celaut.proto`, `contract/buffer.proto` and the files of
`contract/extensions/`.

**Steps.** For each comment, sentence by sentence:

1. Classify the sentence with the test of REASON 5.4: "if this sentence changes, can
   two parties act in two ways?"
   - **A rule** (keep it in the comment): what the field or the message holds, its
     form, its unit if the contract fixes one, what a reader or a node does with it
     and when, what an absent value or an empty list means, if the place is a
     requirement or an announcement.
   - **An example** (keep it only if the rule is less exact without it): a sentence
     with "for example". It must obey REASON 5.1.
   - **A reason** (move it): why the design is so, the alternatives that were refused,
     the history ("before the redesign", "today the code..."), a comparison with
     another system, a consequence for later designs.
   - **A pointer to an implementation or to a rule number** (remove it, or move it):
     a path or a module of nodo (for example `src/utils/keyvalue.py`, `utils.py`),
     or "rule 2.7 of REASON". A pointer to a file of the contract (for example
     `contract/descriptors/`) can stay only if a reader needs it to apply the rule.
2. If one sentence holds a rule and a reason, split it.
3. Move each reason to `contract/REASON.md`: add it to the rule that it supports, or
   write a new rule in the correct section (REASON section 8). Keep the name of the
   field or of the message in the text, so that a reader finds it. Write it in
   ASD-STE100.
4. Do not change a rule. Do not change a field, a number, a name or a type. If you
   cannot decide if a sentence is a rule or a reason, keep it in the comment: a lost
   rule is worse than a reason that stays. List each such sentence in the PR.

**Known cases.** These are examples, not the full list:

- `celaut.proto`: the block "Why the owner attestation of a reputation proof is in
  xattrs" (above the reputation messages), the block "Why a string and not an
  integer" (above `Amount`), the long text above the key-value messages, and the
  pointers to `src/identity/protocol_doc.py`, `src/utils/keyvalue.py` and
  `src/utils/contract_xattrs.py`, and to `contract/REASON.md`.
- `buffer.proto`: "See StreamControl in utils.py".
- `extensions/observe.proto`: the pointer to `src/commands/observe.py`.

To find more, search the comments for: `Why`, `because`, `so that`, `the reason`,
`before`, `today`, `nodo`, `src/`, `.py`, `REASON`.

**Check.** Only comments change, so the compiled descriptors without the source
information must be equal, byte for byte, before and after:

    python -m grpc_tools.protoc -I contract -I contract/extensions \
        --descriptor_set_out=before.binpb contract/celaut.proto \
        contract/extensions/observe.proto contract/extensions/chat.proto
    # ... make the change ...
    python -m grpc_tools.protoc -I contract -I contract/extensions \
        --descriptor_set_out=after.binpb contract/celaut.proto \
        contract/extensions/observe.proto contract/extensions/chat.proto
    cmp before.binpb after.binpb

Do not use `--include_source_info`: the comments are in the source information. If
`cmp` finds a difference, a change is not only in a comment: find it and undo it.

**Result.** In the PR, give for each moved text the place where it was and the rule of
`contract/REASON.md` where it is now.

