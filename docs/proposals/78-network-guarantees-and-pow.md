# Network resolution: what DNS guarantees today, and a Proof-of-Work network

Working document for [#78](https://github.com/celaut-project/nodo/issues/78).
Two halves, and they are different kinds of writing: §1 is an **audit** of code
that exists, every claim carrying a `file:line` and verified against it; §2 is a
**design**, argued rather than reported — its v1 ships alongside this document
(§2.10), the rest does not.

Companion to [`NETWORKS.md`](../NETWORKS.md) (the model), [`FIREWALL.md`](../FIREWALL.md)
(what a resolved tag turns into) and [`ERGO.md`](../ERGO.md) (how this node already
talks to Ergo).

---

# 1. What the DNS network guarantees today

## 1.1 The path, end to end

| Step | Where |
|---|---|
| Service declares `Service.Network{tags, prose, formal, protocol_stack, environment_variable}` | `protos/celaut.proto:261-277` |
| Operator policy over the declared tags | `src/utils/network_policy.py`, called from `src/gateway/launcher/launch_service.py:213` |
| Ancestor chain intersects the request | `filter_networks_with_ancestors`, `src/manager/networks.py:133` |
| Policy again, on what survived | `src/virtualizers/microvm/rootfs.py:124` |
| Tags → peer `Instance`s | `resolve_network`, `src/manager/networks.py:74` |
| Peer `Instance`s → guest `__config__` | `rootfs.py:130-137`, `rootfs.py:140-158` |
| Peer `Instance`s → nftables allows | `src/virtualizers/microvm/network.py:556-574` |

`resolve_network` is the only step that turns a *name* into an *address*, and it is
26 lines long. Everything below is about those 26 lines.

## 1.2 What `resolve_domain` actually does

```python
ips = list({
    info[4][0]
    for info in socket.getaddrinfo(domain, None)
    if info[0] == socket.AF_INET
})
return [
    celaut.Instance.Uri(ip=ip, port=port)
    for ip in ips
    for port in [80, 443]
]
```
— `src/manager/networks.py:27-42`

Three things are hardcoded there and one is inferred:

* **IPv4 only.** `info[0] == socket.AF_INET` drops every AAAA answer. A
  v6-only destination resolves to nothing, and `resolve_network` then falls
  through to `return []` (`networks.py:99-102`) — the same answer it gives for
  `*`, so the guest cannot tell "open internet" from "your name has no A record".
* **Ports 80 and 443, always.** Two `Uri`s per address, whatever the service
  asked for. The code says so itself: `# Ports should be based on the protocol
  stack ¿?` (`networks.py:41`). A service declaring `protocol_stack` = postgres
  gets 80/443 opened and 5432 closed.
* **The tag heuristic**: `if not tag.islower() or '.' not in tag: continue`
  (`networks.py:92`). Verified behaviour of that predicate:

  | tag | treated as a DNS name? | why |
  |---|---|---|
  | `example.com` | yes | |
  | `WWW.google.com` | **no** | `islower()` is False |
  | `192.168.1.1` | **no** | `islower()` is False for an all-digit string |
  | `localhost` | no | no `.` |
  | `pow:ergo` | no | no `.` |
  | `*` | no | neither |

  So the case-insensitivity DNS itself guarantees is not honoured here, and the
  operator policy that *is* case-insensitive (`network_policy.py:239-241`
  lowercases both sides) can whitelist a tag the resolver will then ignore.

* **A failure is an exception, not an empty list.** `socket.gaierror` becomes
  `ValueError` (`networks.py:44-45`), which nothing between `resolve_network` and
  `ch/execute.py:570` catches. Verified: `resolve_network(Network(tags=["nx.invalid"]))`
  raises. So one unresolvable name **aborts the launch**, while `*` and `pow:ergo`
  resolve quietly to `[]`. Two different answers to "I could not give you this
  destination", chosen by which branch of an `if` the tag fell into rather than on
  purpose.

* **First tag wins, the rest are dropped.** The loop `break`s on the first tag that
  resolves (`networks.py:96-97`), and there is exactly one `uris` list. Verified:
  `Network(tags=["example.com", "example.net"])` returns only `example.com`'s
  addresses. But the *whole* tag list is copied into
  `NetworkResolution.tags` (`rootfs.py:132`) and the firewall walks it looking for
  `*` (`network.py:549`). The guest is told it is in a domain named by N tags and
  given the addresses of one.

## 1.3 When resolution happens

Once, at launch, inside `build_network_resolution` (`rootfs.py:108`), called from
`ch/execute.py:236` and `qemu/execute.py:420`. The result is serialized into the
guest's `__config__` and written into the offline rootfs image with `debugfs`
(`ch/execute.py:258-262`), before the VM is booted.

There is **no path that updates it afterwards.** `debugfs_write` operates on an
offline image; the only callers are the two `execute.py` launch paths and
`shares.py:125`. So:

* A DNS TTL of 60s is honoured for exactly as long as the instance's first second.
* A CDN that rotates addresses, an A record that fails over, a load balancer that
  drains a backend — the guest keeps the addresses resolved at boot, and the
  firewall keeps the rules written for them, for the life of the instance.
* The inverse: the *new* addresses are not opened. A long-running instance whose
  destination moved is firewalled off from it, and nothing in the node notices.

This is the one guarantee the code comments already claim is better than it is:
`network.py:481-482` says the `NetworkResolution` "is not frozen at launch the
way a resolved A record is". As delivered today it is exactly as frozen — the
richer *shape* survives, the refresh does not exist.

## 1.4 What is guaranteed about the peer's identity

Nothing.

* **No DNSSEC, no validation.** `getaddrinfo` uses the *host's* resolver
  (`/etc/resolv.conf` of the machine running nodo). Whatever that resolver says is
  taken as fact. A hijacked resolver, a poisoned cache, a captive-portal DNS, or a
  LAN with a lying DHCP-supplied nameserver all produce addresses nodo opens.
* **No proof the peer speaks `protocol_stack`.** `resolve_network` copies
  `network.protocol_stack` into the synthesized `Instance.api.slot[0]`
  (`networks.py:104-113`). That is the *requester's* declaration echoed back, not
  an observation: nothing connects, nothing handshakes, nothing checks. The guest
  reads its `__config__` and sees a peer that claims the stack it asked for.
* **No TLS anything.** No pinning, no certificate check, no SNI knowledge. That is
  the guest's job and correctly so — but it means the node's contribution to
  "you are talking to `api.example.com`" is one unauthenticated A lookup.
* **No liveness check.** An address that is dark is opened like any other.

What nodo guarantees for a DNS network is therefore: *some host's resolver returned
these A records at launch time, and the guest may send TCP to them on 80 and 443.*
That is a useful statement. It is not an identity statement.

## 1.5 Interplay with the firewall

`configure_guest_firewall_policy` (`network.py:495`) writes the rules:

* `*` anywhere in any tag list → `allow_all_egress` (`network.py:549-554`), one
  accept for the whole VM. This is checked across **all** `NetworkResolution`
  entries, so a single `*` tag in one network opens everything for every other.
* Otherwise, per `NetworkResolution`, it iterates `peer_instances` and **stops at
  the first instance for which a rule was applied** (`network.py:559-569`).
  `resolve_network` returns at most one `Instance` today, so this is not currently
  lossy — but an implementation returning several peers (a PoW network will) would
  have only its first honoured. This is a real constraint on §2.
* Inside one instance, *every* `uri` gets a rule
  (`microvm/firewall.py:177-190`), so all resolved IPs × {80, 443} are opened.
* A tag nothing could be applied for is a **log line**, not an error
  (`network.py:571-574`). The guest boots with less egress than it declared and is
  never told.
* Rules are IPv4-shaped by construction: `validate_address`
  (`utils/firewall/policy.py:47-64`) accepts v6 literals, but `resolve_domain`
  never produces one.

**TOCTOU.** `build_network_resolution` runs at `ch/execute.py:236`;
`configure_guest_firewall_policy` at `ch/execute.py:308`. Between them the rootfs is
copied, the config is serialized and `debugfs`-injected, the entrypoint is written,
shares are materialized and a tap is created. Seconds, on a real image. The
addresses opened are the ones resolved before all that — which is the *correct*
ordering for the firewall (the comment at `ch/execute.py:295-307` explains why the
policy must precede the guest), but it does mean the config the guest reads and the
rules the host enforces are both snapshots of a lookup that is by then measurably
old. They agree with each other, which is what matters; neither agrees with DNS.

## 1.6 `environment_variable` and DNS peers

`filter_peers_by_environment` (`network_env.py:44`) is called with
`peer_env_lookup=None` from every real caller — `rootfs.py:133` passes only
`requester_env_values`, and `resolve_network`'s parameter defaults to `None`
(`networks.py:77`). Verified: nothing in `src/` or `tests/` ever passes a lookup.
The function's first line then returns every peer unchanged
(`network_env.py:56-57`).

This is right for DNS — an A record has no celaut environment — but it means the
feature is, today, dead in the only path that reaches it. The guarantee is
"`environment_variable` is honoured for peers whose environment the node can read",
and there are none.

## 1.7 `formal` is not read anywhere

`match_networks` is the whole of network comparison:

```python
def match_networks(a, b) -> bool:
    # TODO Could be more powerfull
    return bool(set(a.tags) & set(b.tags))
```
— `src/manager/networks.py:129-131`

Tag-set intersection. Consequences, all verified by reading the callers:

* `formal` is **never parsed, compared or validated** anywhere in `src/`. `grep`
  finds it only in the proto and the packer, which does not even copy it
  (`packers/zip_with_dockerfile.py:564-570` sets `tags` and `prose` only).
* `prose` likewise.
* `protocol_stack` is copied into the synthesized peer but never used to *select*
  one.
* So the ancestor chain authorizes on tags alone: a father declaring `pow:ergo`
  authorizes a child asking for `pow:ergo` **whatever the two `formal`s say**.
  For DNS that is harmless. For any network whose tag is a class and whose `formal`
  is the actual ask, it is the entire control surface being ignored.

## 1.8 `resolve_ergo_network` is a stub, and the dead code behind it is wrong

```python
def resolve_ergo_network() -> List[celaut.Instance.Uri]:
    return []
    # TODO Needs to get the ip and port from the data, actually is the restApiUrl.
    ...
        for uri in ergo_peers.keys():
            ip, port = uri.split(":")
```
— `src/manager/networks.py:55-72`

The `return []` on the first line makes the rest unreachable, and the unreachable
part would not work: the keys of `ledgers.ergo.HTTP_PEERS_PATH` are `restApiUrl`
values written by `src/manager/ergo.py:60-64`, i.e. strings like
`https://node.sigmaspace.io` — so `uri.split(":")` yields `("https", "//node…")`
and `int(port)` raises, into a bare `except: return []`. The TODO on line 58 says
exactly this. Worth recording because §2 wants that peer list and must parse it as
URLs.

Note also `if "ergo" in tag` (`networks.py:87`) — a substring test, so
`my-ergo-thing.example.com` takes the ergo branch first, gets `[]`, falls through to
the DNS branch and resolves anyway. Harmless while the stub returns `[]`; a trap
the moment it does not.

## 1.9 The guarantee table

| Guarantee | Provided today? | Notes |
|---|---|---|
| Tag resolves to addresses of that DNS name | **Partly** | A records only, IPv4 only (`networks.py:30-31`). No AAAA, no CNAME awareness beyond what `getaddrinfo` flattens. |
| Case-insensitive names, as DNS specifies | **No** | `tag.islower()` (`networks.py:92`) rejects `WWW.google.com`. |
| Ports match the declared `protocol_stack` | **No** | Hardcoded 80/443 (`networks.py:41`), with a TODO admitting it. |
| Every declared tag is resolved | **No** | First success `break`s (`networks.py:96-97`); the rest are silently dropped while all tags still reach `NetworkResolution.tags` (`rootfs.py:132`). |
| Resolution tracks DNS TTL / rotation | **No** | Once at launch (`rootfs.py:108`), injected into an offline image (`ch/execute.py:258`). No refresh path exists. |
| Peer authenticity (DNSSEC, pinning) | **No** | Host resolver, taken as fact. No validation of any kind. |
| Peer actually speaks `protocol_stack` | **No** | Echoed from the requester's own declaration (`networks.py:104-113`). Never probed. |
| TLS identity | **No**, by design | The guest's job. Nodo opens a socket path, nothing more. |
| Only resolved addresses are reachable | **Yes**, on the forward hook | `block_all` + per-uri allows (`firewall.py:177-190`). Not on the input hook — see `FIREWALL.md` "Known limits". |
| `*` is confined | **No**, by design | `allow_all_egress` (`network.py:549-554`); one `*` in any tag list opens the whole VM. |
| A tag that could not be opened fails the launch | **No** | Logged only (`network.py:571-574`). But an unresolvable *DNS* tag raises `ValueError` and aborts (`networks.py:45`). Two policies, unintentionally. |
| Config and firewall agree on the addresses | **Yes** | Both read the same `network_resolution` object (`ch/execute.py:236`, `:308`). |
| `environment_variable` filters DNS peers | **No**, correctly | No `peer_env_lookup` is ever passed; DNS peers have no celaut environment (`network_env.py:56-57`). |
| `formal` constrains anything | **No** | Never parsed. `match_networks` is tag intersection (`networks.py:129-131`). |
| Ancestor chain limits what a child may reach | **Yes** | `filter_networks_with_ancestors` (`networks.py:133`); an unreadable ancestor spec aborts rather than grants (`networks.py:167-181`). **This is a real guarantee.** |
| Operator can refuse a domain | **Yes** | `service_networks` blacklist/whitelist, three enforcement points (`network_policy.py:44-52`). **This is a real guarantee.** |
| Ergo/PoW tags resolve to peers | **No** | `resolve_ergo_network` returns `[]` (`networks.py:56`). |

**Read the table as one sentence:** the controls that decide *whether* a guest may
reach a domain — the ancestor chain and the operator policy — are sound and
deliberate. The step that decides *what address that domain is* is a single
unvalidated `getaddrinfo` frozen at boot. Any new network kind should inherit the
first two and not imitate the third.

---

# 2. Proposal: Proof-of-Work networks

> "Give me peers on the PoW chain that contain block `B` and whose chain meets a
> minimum difficulty `D`."

## 2.1 Why a tag alone cannot say it

`pow:ergo` names a *class* of domain. "Ergo mainnet, containing block B, at least
D work" is an *instance* of that class, and two services on the same chain with
different asks are not in the same communication domain. That is what `formal`
is for (`celaut.proto:264`) — and §1.7 established that nothing reads it today.
So a PoW network is the first network kind that needs `formal` to mean something,
and the first that needs `match_networks` to compare more than tags.

## 2.2 Tag scheme

`pow:<chain>`, lowercase, with `<chain>` a chain slug: `pow:ergo`, `pow:bitcoin`.

Consistent with what the docs already use as the example whitelist entry
(`NETWORKS.md` `service_networks` block, `config.example.yaml:393` and `:424`),
and with `network:<slug>` in celaut-project/skills
(`src/lib/strictDefinition.ts`, `ConceptKind` → `<kind>:<slug>`). It survives the
existing policy globs unchanged: `pow:*`, `pow:ergo`, `!pow:bitcoin` all work as
written today, because the policy globs the tag and nothing else
(`network_policy.py:239-253`).

It must **not** contain a `.`, so it can never fall into the DNS heuristic
(`networks.py:92`); `pow:ergo` verified above as not matching it.

## 2.3 `formal`: JSON, not a proto message

`formal` is `bytes` (`celaut.proto:264`), so the encoding is ours to choose.

| | JSON (utf-8) | new proto message |
|---|---|---|
| Round-trips across packers | yes — `service.json` is already JSON (`docs/PACKING.md:422-427`) | needs a codegen step in every packer |
| Canonical form for comparison | needs a rule (see below) | free |
| Readable in an issue / a log | yes | no |
| Cost to add a chain | a field | a proto change + regeneration |
| Precedent in celaut | `strictDefinition.ts` puts JSON in R9 | `Architecture.formal`, `Protocol.formal` are also unstructured `bytes` today |

**Recommendation: JSON.** The deciding argument is that `formal` is `bytes` in a
message that is authored by hand, packed from `service.json`, published to a
reputation-system box and read by three languages. A proto message inside `bytes`
is a schema nobody can see from the outside; the skills repo already chose JSON for
exactly this reason. The cost — needing a canonicalisation rule — is one paragraph:

> **Canonical form.** UTF-8 JSON object, keys sorted, no insignificant whitespace
> (`json.dumps(obj, sort_keys=True, separators=(",", ":"))`). Comparison is over the
> *parsed* object, never the bytes, so a non-canonical `formal` is accepted on
> input; canonicalisation exists so that a `formal` can be hashed or logged
> reproducibly, not as a validity condition. Unknown keys are **rejected**, not
> ignored: a node that silently drops a constraint it does not understand grants
> more than was asked for. (Same reasoning as `network_policy.py`'s "a list the
> node failed to read is not a list that allowed everything".)

```json
{
  "v": 1,
  "chain": "ergo",
  "block_id": "f35a8aa47ab6e950ba1a8cd10dc92bade42928dd985575d7fe46e759379690e0",
  "min_cumulative_difficulty": "2749889727692749668352",
  "min_height": 1873000,
  "max_tip_age_s": 3600
}
```

| Field | Type | Req. | Meaning |
|---|---|---|---|
| `v` | int | yes | Format version. A peer that does not know `v` refuses rather than guesses. |
| `chain` | string | yes | `ergo`, `bitcoin`. Must equal the tag's suffix — a `pow:ergo` tag with `"chain":"bitcoin"` is a malformed spec, not a cross-chain ask. |
| `block_id` | hex string | yes | The block that must be **on the peer's main chain**. |
| `min_cumulative_difficulty` | decimal **string** | yes | See §2.4. String because the value exceeds 2⁶⁴ (Ergo's `fullBlocksScore` is ~2.7e21 today) and JSON numbers are doubles. |
| `min_height` | int | no | Peer's main-chain height must be ≥ this. Cheap liveness floor. |
| `max_tip_age_s` | int | no | Peer's tip timestamp must be within this many seconds of now. Catches a synced-but-stalled node. |

`protocol_stack` stays in the proto field where it belongs
(`celaut.proto:267`), not in `formal`. `pow:ergo` peers speak the Ergo node REST
API (`:9053` by convention, `restApiUrl` in practice); `pow:bitcoin` peers speak
JSON-RPC (`:8332`) or P2P (`:8333`). §2.8 is where that turns into a port.

## 2.4 "Minimum difficulty", precisely

Three candidate readings, and they are not close to equivalent:

| Reading | Ergo source | Bitcoin source | Comparable across peers? |
|---|---|---|---|
| Difficulty of the tip block | `/info.difficulty` | `getblockchaininfo.difficulty` | **No.** Retargets every epoch; a value valid today is wrong next week, and it says nothing about history. |
| Cumulative work since genesis | `/info.fullBlocksScore` | `getblockchaininfo.chainwork` | **Yes.** Monotone non-decreasing, total order, directly comparable. |
| Cumulative work since `block_id` | derived: `score(tip) − score(block_id)` | derived | Yes, but needs two reads and is a *confirmation-depth* question wearing a difficulty costume. |

**Recommendation: cumulative work since genesis** — `fullBlocksScore` on Ergo,
`chainwork` on Bitcoin.

Reasons, in order of weight:

1. **It is the thing the chain's own fork-choice rule maximises.** "Meets minimum
   difficulty D" then means the same thing to nodo as it does to the network: this
   peer follows a chain that at least D work has been spent on. A tip-difficulty
   threshold is satisfiable by one lucky block on a fork nobody else has.
2. **Monotone.** A `formal` written today stays valid tomorrow and gets *stricter*
   relative to the chain only if the author meant it to. A tip-difficulty threshold
   flips validity at every retarget.
3. **One field answers both halves of the ask.** Depth-since-`block_id` is
   expressible as `min_cumulative_difficulty` ≥ the score at the block the author
   cares about plus whatever margin they want; the reverse is not true.
4. **Both chains expose it on the paths we already use.** Verified live against
   `https://node.sigmaspace.io/info` (the shipped `ledgers.ergo.NODE_URL`,
   `config.example.yaml:1021`): `fullBlocksScore: 2749889727692749668352`,
   `headersScore`, `difficulty`, `fullHeight`, `headersHeight`, `genesisBlockId`
   are all in one response.

Comparison is exact integer arithmetic on the decimal string — Python `int()`,
never a float. `headersScore` is **not** used: a header-only chain is not a chain
this peer can serve blocks from.

## 2.5 Where candidate peers come from

| Source | How | Trust |
|---|---|---|
| **(a) celaut instances declaring the network** | The normal indexing path in `NETWORKS.md` "Network Instance Indexing": an instance that declares `pow:ergo` *and* exposes the `protocol_stack` in its `Service.Api`. | Same as any peer: unknown, must be verified. But it is a peer this node can also *pay* and *rate*, so a lying one is attributable — the only source where that is true. **Not implementable today**: there is no network-membership index in the database (`grep -rni network src/database/access_functions/` → nothing). Out of v1 scope. |
| **(b) the node's own configured ledger node** | `ledgers.ergo.NODE_URL` (`config.example.yaml:1021`), `ledgers.bitcoin.*` (`:1166`). | The operator chose it and the node already trusts it with reputation reads and payment proofs (`src/manager/ergo.py:14`). Verifying it is still worth doing — its *state* is a fact about the world, not about the operator's intent — but its honesty is already assumed elsewhere. |
| **(c) the Ergo peer crawl** | `ledgers.ergo.HTTP_PEERS_PATH`, populated by `get_refresh_peers()` (`src/manager/ergo.py:36-75`), which already filters on `genesisBlockId` matching `ledgers.ergo.GENESIS_BLOCK_ID` (`ergo.py:22-30`). | Untrusted strangers. Each must be verified independently, and the genesis check is a floor not a ceiling. Note the crawl is recursive and unbounded (`ergo.py:68` recurses inside the loop) — resolution must **read the file**, never trigger a crawl. |

v1 uses **(b) + (c)**, because both exist. (a) is the interesting one and is
deferred to whenever network-membership indexing lands.

## 2.6 Verification, per candidate

For `chain: ergo`, against the candidate's `restApiUrl`:

1. `GET /info` → `genesisBlockId` must equal `ledgers.ergo.GENESIS_BLOCK_ID`
   (same check `manager/ergo.py:22` already makes; a testnet node is not a peer on
   this network). Keep `fullHeight`, `fullBlocksScore`, and the tip id.
2. `int(fullBlocksScore) >= int(min_cumulative_difficulty)`.
3. `fullHeight >= min_height`, when given.
4. `GET /blocks/{block_id}/header` → 200 means the peer has the block. Verified
   live: a real id returns the header with `height`, `difficulty`, `nBits`,
   `powSolutions`; an unknown id returns HTTP 404 with `{"error":404,"reason":"not-found"}`.
5. **Main chain, not merely known.** Take `height` from step 4, then
   `GET /blocks/at/{height}` → a list of block ids at that height; `block_id` must
   be in it. Verified live: `/blocks/at/1000001` →
   `["f35a8aa47ab6e950ba1a8cd10dc92bade42928dd985575d7fe46e759379690e0"]`.
   Without step 5 a peer that merely *stored* an orphan passes.
6. `max_tip_age_s`, when given: `currentTime` from `/info` is the peer's own clock,
   so compare the *tip header's* `timestamp` (`/blocks/lastHeaders/1`) against
   **our** clock. A stalled node reporting a fresh clock is precisely what this
   check exists to catch.

For `chain: bitcoin`: `getblockchaininfo` → `chainwork` (hex, 32 bytes) and
`blocks`; `getblockheader <block_id>` → `confirmations` **> 0** is exactly the
main-chain test (Core reports `-1` for a block on a side branch), which makes it
one call rather than two. Nodo's Bitcoin posture is a receive-only Esplora backend
by default (`payment_system/contracts/bitcoin/explorer.py:1-32`), and Esplora
exposes neither `chainwork` nor a peer list — so Bitcoin gets the parser in v1 and
a documented `NotImplementedError`, not a half-verification.

### What a peer can and cannot lie about

A REST answer is a **claim**. Everything in steps 1-6 is self-reported, and
fabricating all of it costs a peer one `if` statement in a proxy.

What it cannot cheaply fake is **work**. The headers are self-authenticating: an
Ergo header carries `nBits` and `powSolutions` (`pk`, `w`, `n`, `d`) and its
Autolykos solution can be checked without trusting anyone; a Bitcoin header's
SHA256d must be below target. So the honest ladder is:

| Stage | Check | Cost to us | What it stops |
|---|---|---|---|
| **v1** | REST self-report (1-6) | 2 HTTP calls per peer | An out-of-sync, wrong-network, stalled or pruned node — the overwhelmingly common case. A deliberate liar passes. |
| **v2** | Cross-check: require *k* of *n* independent candidates to agree on the id at `block_id`'s height | k× the calls, no new code paths | A single liar, without any cryptography. Cheap and worth doing before v3. |
| **v3** | Fetch the header chain and verify the PoW solutions | a library (`ergo-lib`/Autolykos) and O(chain) work, or O(log n) with NiPoPoWs | A liar with a fabricated chain. Also the only thing that makes the *difficulty* number trustworthy rather than merely cross-witnessed. |

**Recommendation: ship v1, document it as self-reported, and say so in the guest's
own terms.** The reason to be unembarrassed about that: the alternative today is
`resolve_ergo_network() → []`, and a service that wants a stronger guarantee can
get it from the chain itself — it is going to talk to these peers over a protocol
that already carries headers. Nodo's job is *selection*, not consensus. v2 is a
small enough step that it should be the first follow-up, and NiPoPoWs are the
right shape for v3 (there is prior art in this ecosystem).

## 2.7 Re-resolution

Unlike a DNS name, the *answer* to a PoW ask changes continuously: a peer that met
D at boot still meets it an hour later (work is monotone), but a peer that was in
sync may stall, and peers that did not qualify later do.

**Can the guest's config be updated after boot? No.** §1.3: `__config__` is written
into the offline ext4 image with `debugfs` before the VM starts
(`ch/execute.py:244-264`), and there is no online write path — `debugfs_write`'s
only callers are the two launch paths and `shares.py`. Adding one is a much larger
change than this proposal (it needs a channel into a running guest, which today is
the gateway and a service-level protocol).

So: **v1 resolves once at launch, exactly like DNS**, and the doc says so. Two
things make that acceptable and one makes it better later:

* `min_cumulative_difficulty` is monotone, so the property that was checked cannot
  become *false* by the chain advancing — only the tip-freshness checks can go stale.
* A service that needs live peer discovery already has the right tool: it reaches
  the node over the gateway. A `node_controller` call that re-runs resolution is
  the correct shape for a refresh, and it is additive.
* The firewall is the real constraint on any future refresh: new peers need new
  allow rules, and rules are written per-VM at launch (`network.py:495`). A refresh
  that hands the guest addresses it cannot reach is worse than no refresh.

## 2.8 Composing with the existing controls

**Operator policy** — nothing to do. `pow:ergo` is a tag; `service_networks`
globs tags (`network_policy.py:239-253`). `whitelist: ["pow:*"]` and
`blacklist: ["pow:bitcoin"]` already work, and are already the documented example
(`config.example.yaml:424`). Note the policy judges the **tag only**: it cannot
express "PoW networks, but not with `min_height` below X". That is fine — the
policy is about domains, not about their contents — but it should be stated so
nobody assumes otherwise.

**Ancestor chain** — this is where `formal` has to start being read. Proposed rule
for `match_networks`, keeping today's behaviour for every existing network:

```
match(child, father):
  1. tags must intersect                          (unchanged, networks.py:130)
  2. if neither side's matched tag is "pow:*"     -> match (unchanged)
  3. if exactly one side carries a pow formal     -> NO match
  4. both carry one: match iff the child's ask is NO WEAKER than the father's:
       chain                       equal
       block_id                    equal, OR the child's block is a descendant
                                   of the father's — v1: require equal
       min_cumulative_difficulty   child >= father
       min_height                  child >= father   (absent on child = father's)
       max_tip_age_s               child <= father   (absent on child = father's)
  5. a formal that does not parse  -> NO match
```

Rule 4 is "a child may ask for a *narrower* domain than its father, never a wider
one", which is the same induction the chain already runs on tags, applied to the
contents. A father asking `≥ D1` and a child asking `≥ D2 ≥ D1` is fine: every peer
the child accepts, the father would have accepted too. Rule 3 is the conservative
direction — a father that asked for `pow:ergo` with no constraints at all is asking
for something this scheme cannot compare, so it grants nothing rather than
everything. Rule 5 follows `network_policy.py`'s stated principle verbatim.

**`environment_variable`** — no change, and it stays inert for PoW peers for the
same reason it is inert for DNS ones (§1.6): a chain node is not a celaut instance
and has no environment to read. It becomes meaningful only for peer source (a).

**Firewall** — each qualifying peer becomes one `Instance` with one
`Uri(ip, port)`, where ip/port come from parsing the peer's `restApiUrl`
(`https://host:9053` → resolve `host`, port 9053; default 443/80 by scheme when
absent). Two consequences to respect:

* `network.py:559-569` **breaks after the first instance for which a rule applied**.
  So a PoW resolution returning N peers must return them as **one `Instance` with
  N `uri`s in one `Uri_Slot`** — which `allow_connection_to_instance` walks
  completely (`firewall.py:177-190`) — and not as N `Instance`s. This mirrors what
  `resolve_domain` already does with multiple A records.
* A peer whose `restApiUrl` is a hostname needs a DNS lookup, which re-imports every
  §1.4 caveat. Prefer the numeric form when the crawl recorded one.

## 2.9 No peer qualifies

| Option | Argument |
|---|---|
| Return `[]` | Consistent with `*` and with every unresolved tag today (`networks.py:99-102`). The guest boots with no peers for that network and can say so itself. |
| Abort the launch | Consistent with `resolve_domain`'s `ValueError` and with `enforce_network_policy` at `rootfs.py:124`. A guest that cannot reach its chain is usually useless. |

**Recommendation: return `[]`, and log it at the same place the firewall already
logs an unopenable tag** (`network.py:571-574`).

Three reasons. First, "no peer meets D right now" is a statement about the *world*,
not about this service's request — unlike a policy rejection, which is a statement
about intent, or an unreadable ancestor spec, which is a statement about this
node's integrity. Second, it is transient by nature: a launch refused because the
crawl file was stale is a launch that would have succeeded a minute later, and
nodo's own failure taxonomy already distinguishes those (`networks.py:167-181`
raises for the *permanent* kind). Third, `[]` is what the guest gets for `*` today,
so the guest-side handling exists.

The honest caveat: `[]` today is indistinguishable from "wildcard" and from "IPv6
only" (§1.2). That ambiguity is a pre-existing defect and this proposal should not
deepen it — v1 should log a distinguishable line, and a future
`NetworkResolution.status` field would fix it properly for every network kind at
once.

## 2.10 Implementation

**v1 ships with this document**, on the same branch:

| File | Change |
|---|---|
| `src/manager/pow_networks.py` (new) | `PowRequirement`; `parse_pow_formal(formal, tag)` (strict: unknown keys refused, `v` checked, tag/chain agreement enforced, decimal strings parsed as exact `int`); `canonical_formal`; `ergo_candidate_urls`; `ergo_peer_satisfies` (the §2.6 ladder); `resolve_pow_network`. Bitcoin parses and raises `NotImplementedError` with the §2.6 reason. |
| `src/manager/networks.py` | One branch at the top of `resolve_network`'s tag loop: `if tag.startswith("pow:")`. No existing branch is touched, and a `pow:` tag could not have reached the DNS heuristic anyway (no `.`) — the ordering is for the reader. |
| `config.example.yaml` | `pow_networks.TIMEOUT_SECONDS`, `.MAX_PEERS`, `.EXTRA_PEERS`. **Not `networks:`** — that would sit one letter from the `network:` block, the same trap `service_networks` is named around. Chain endpoints are read from `ledgers.ergo.*` rather than duplicated. |
| `tests/test_pow_networks.py` (new) | 37 tests, no network and no clock: the parser's accept/reject table; per-reason peer rejection (wrong genesis, work below threshold, `headersScore` not accepted for `fullBlocksScore`, below `min_height`, block absent, block present but orphan at that height, stalled tip against *our* clock, unreachable); the one-Instance-N-uris shape; candidate ordering and that the crawl file is read as URLs; and that the dispatch leaves DNS resolution untouched. |
| `docs/NETWORKS.md` | Use Case 3 spelled out, linking here. |

**Explicitly not in v1:** `match_networks` formal comparison (§2.8 — a behaviour
change to an authorization control, so it deserves its own PR and its own tests),
peer source (a), Bitcoin verification, re-resolution, cross-checking.

**Follow-ups, in order:** (1) `match_networks` formal comparison; (2) v2
cross-checking *k* of *n*; (3) `NetworkResolution.status`, so `[]` stops meaning
three different things (§2.9); (4) v3 header verification. Each is independently
revertible, which is the point of the order.

## 2.11 Open questions for Josemi

1. **Difficulty semantics** — §2.4 recommends cumulative work since genesis
   (`fullBlocksScore` / `chainwork`). Is "since the given block" the reading you
   had in mind instead? It is a different, also defensible ask.
2. **`formal` encoding** — JSON (§2.3), or a proto message? JSON is my
   recommendation and matches celaut-project/skills, but `formal` is `bytes`
   project-wide and you may want one answer for `Architecture.formal`,
   `Protocol.formal` and `Network.formal` together rather than three.
3. **No qualifying peer** — `[]` (§2.9) or abort? `[]` is consistent with today;
   abort is arguably more honest to the service.
4. **`match_networks`** — is tightening it to compare `formal` (§2.8) in scope for
   #78, or does it belong with the strict-definition work in
   celaut-project/skills? It is a behaviour change to an authorization control, so
   it should not ride along on a resolver PR either way.
5. **Peer source (a)** — is network-membership indexing (`NETWORKS.md` "Network
   Instance Indexing") planned? It is the only source where a lying peer is
   attributable, which changes the whole trust argument in §2.6.
