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
| Service declares `Service.Network{tags, prose, formal, protocol_stack}` (`environment_variable` removed, #396) | `protos/celaut.proto:261-277` |
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

> **Superseded in part by [#385](https://github.com/celaut-project/nodo/issues/385).**
> This section says resolution happens once, at launch, for *every* declared
> network. That is still true of every network whose `formal` carries no `${VAR}`
> template. A network that does carry one and whose variables the launcher did not
> answer is **not** resolved at launch at all: it is omitted from `__config__` and
> resolved later, on demand, over `Gateway.ResolveNetwork` (§2.5.1) — which is also
> the answer to the staleness this section goes on to describe, for that class of
> network. See [`NETWORKS.md`](../NETWORKS.md). Everything below still holds for the
> eager path.

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
  lossy — but the *guest* is handed every instance in its `__config__`, so the two
  would disagree the moment a resolution had more than one. A PoW network does. This
  is the one finding in §1 that §2 fixes rather than designs around (§2.8): the loop
  now writes a rule for every instance.
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
feature was, at the time of this audit, dead in the only path that reached it.
The guarantee would have been "`environment_variable` is honoured for peers whose
environment the node can read", and there were none.

**Removed in [#396](https://github.com/celaut-project/nodo/issues/396).** Never
reachable from `service.json` authoring, bypassed for remote peers, and always
inert in production for exactly the reason above. Its use case — several
instances of one service, only the matching ones as peers — is now expressed with
a `${VAR}` selection key in `formal` (§2.3, `docs/NETWORKS.md` "Partitioning by
instance"), the mechanism `formal` templating already made packer-validated and
enforced end to end (`request_fits_declaration`, `same_component`, the ancestor
chain), instead of a field with no operational path.

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

This is the one finding in §1 that §2 does not merely design around: `match_networks`
is changed (§2.8). Every network that declares no `formal` — which is every network
that exists today — compares exactly as it did.

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
| `environment_variable` filters DNS peers | **Removed** (#396) | Was never reachable in production; superseded by a `${VAR}` selection key in `formal` (§1.6). |
| `formal` constrains anything | **No** | Never parsed. `match_networks` is tag intersection (`networks.py:129-131`). |
| Ancestor chain limits what a child may reach | **Yes** | `filter_networks_with_ancestors` (`networks.py:133`); an unreadable ancestor spec aborts rather than grants (`networks.py:167-181`). **This is a real guarantee.** |
| Operator can refuse a domain | **Yes** | `service_networks` blacklist/whitelist, three enforcement points (`network_policy.py:44-52`). **This is a real guarantee.** |
| Ergo/PoW tags resolve to peers | **No** | `resolve_ergo_network` returns `[]` (`networks.py:56`). |

**Read the table as one sentence:** the controls that decide *whether* a guest may
reach a domain — the ancestor chain and the operator policy — are sound and
deliberate. The step that decides *what address that domain is* is a single
unvalidated `getaddrinfo` frozen at boot. Any new network kind should inherit the
first two and not imitate the third.

The table reads `dev` as it was. §2 leaves every row of it alone except two, both of
them rows where what the node *told* a guest and what it *did* had come apart:
"`formal` constrains anything" (§2.8) and the firewall's first-instance `break`
(§1.5). Nothing in the DNS lookup itself is touched.

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

## 2.3 `formal`: sorted `key=value` lines, `pow.`-prefixed

`Service.Network.formal` is bytes and carries the body every other `formal` in
`celaut.proto` carries: sorted `key=value` lines, UTF-8, built by
`node_identity.component_formal` and parsed by `parse_component_formal`. It is the
encoding `SignatureScheme.Protocol`, `Uri.Protocol` and `Contract.Ledger` already
use, and a PoW requirement is the same kind of statement about the same kind of
field.

```
pow.block_id=f35a8aa47ab6e950ba1a8cd10dc92bade42928dd985575d7fe46e759379690e0
pow.chain=ergo
pow.max_tip_age_s=3600
pow.min_cumulative_difficulty=2749889727692749668352
pow.min_height=1873000
publisher.note=extra metadata
```

Three properties, in order of weight:

1. **Canonical by construction.** Sorting the keys means two authors who declare the
   same parameters produce identical bytes whatever order they built them in. This
   field is compared byte for byte down the ancestor chain, so that is not a nicety.
   A protobuf map is explicitly *not* canonical across implementations — this repo's
   own `canonical_peer_content_digest` refuses `SerializeToString()` for exactly that
   reason — and neither is a JSON object.
2. **One convention for one field.** A second encoding for `formal` would make this
   the only place in the protocol with one, and every reader would have to know which
   descriptor it was holding before it could read the field.
3. **Readable with no dependency.** `celaut-project/skills` is TypeScript with no
   protobuf in it; a `split('\n')`/`indexOf('=')` reads this. It is also diffable by
   eye in a service spec, which the bytes of a map are not.

**The domain's keys are prefixed `pow.`** so the vocabulary this module reads is
namespaced from anything else the same body carries, and an old unprefixed `chain=`
is a *missing* key rather than one silently reinterpreted. Integers are nonnegative
decimal text (no floating-point conversion anywhere on the path). Required keys,
chain/tag agreement, hexadecimal block ID and typed values are validated as before.

**Unrecognized keys are preserved, not refused.** They are kept in
`PowRequirement.extensions` and re-emitted by `canonical_formal`, so a body that
travelled through this node says what it came in saying — and they are not treated as
constraints this node enforces. Refusing them would make each reader the ceiling on
what a descriptor may say, for no gain: a key nobody interprets grants nothing.
Validation of what *is* understood is a different thing and stays: a missing required
key, a malformed value, a line that is not a pair, or a key declared twice are each
refused.

**There is no version key.** A version belongs to the vocabulary being spoken, and
that is named by the network's `protocol_stack` descriptor (`pow/ergo-v1`), not
duplicated here where the two could disagree. Same reason `protocol` and
`peerDiscovery` are not keys of this body — see §2.3.1.

> **Historical note.** `7bd12f76` briefly made this a `NetworkFormal` protobuf
> `map<string, bytes>` (`protos/network_formal.proto`), on the reading that the map was
> what bought extensibility. It was not: what an unknown key needs is a *consumer* that
> does not refuse it, which is a policy in `pow_networks.py` and not a wire format. The
> map was reverted for the three reasons above, and the proto deleted; the decision is
> recorded here rather than erased, since the question will come up again.

### 2.3.1 What is not in `formal`

`protocol` and `peerDiscovery` are not flat parameters of the PoW ask. Each is a
tags/prose/formal descriptor in its own right — with its own version, its own prose
and its own determinate parameters — and `Service.Network.protocol_stack`
(`repeated Api.Protocol`) already models exactly that. Flattening either into one
value here would give the same fact two places to be stated and one way to disagree.

What nodo does with `protocol_stack` today is unchanged by this: `resolve_pow_network`
echoes the requester's stack onto the Instances it returns, exactly as the DNS path
does, because nothing here observed the peer's. Matching a published definition's
protocol stack against a service's ask is subsumption, not byte equality, and it is
out of scope for v1 along with the reputation-ledger source (open question 4).

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

A DNS network resolves a name. A `pow:` network has no name — "peers whose main chain
contains B and carries D work" is not something a lookup answers — so its addresses
have to be **found**. Four sources, in trust order:

| Source | How | Trust |
|---|---|---|
| **(a) the node's own configured ledger node** | `ledgers.ergo.NODE_URL` (`config.example.yaml:1021`), `ledgers.bitcoin.*` (`:1166`). | The operator chose it and the node already trusts it with reputation reads and payment proofs (`src/manager/ergo.py:14`). Verifying it is still worth doing — its *state* is a fact about the world, not about the operator's intent. |
| **(b) endpoints named by hand** | `service_networks.default_instances["pow:ergo"]`, a map of tag → uris. | The operator's own statement, for somebody running a node this one is not otherwise pointed at. Keyed by tag rather than flat because an endpoint means nothing on its own: an Ergo REST node has no business being asked about a bitcoin network. |
| **(c) other celaut nodes** | `Gateway.ResolveNetwork` (§2.5.1). | Peers this node holds a relationship with — it can pay them, rate them, and attribute a lie — but who staked nothing on *this* answer. |
| **(d) the Ergo peer crawl** | `ledgers.ergo.HTTP_PEERS_PATH`, populated by `get_refresh_peers()` (`src/manager/ergo.py:36-75`), which already filters on `genesisBlockId` matching `ledgers.ergo.GENESIS_BLOCK_ID`. It is also the **only** source that observes a peer's P2P address, which `/peers/connected` carries as `address` next to `restApiUrl`. | Untrusted strangers who paid nothing and were asked nothing. The crawl is recursive and unbounded (`ergo.py:68` recurses inside the loop), so resolution **reads the file** and never triggers a crawl. |

**The order is a latency decision, not a security one.** Every candidate goes through
the same verification (§2.6) whatever named it, so what the order decides is who is
asked first and therefore who fills the `MAX_PEERS` budget. That is the whole reason
it is safe to take addresses from strangers at all: the cost of a lie at this layer is
one wasted HTTP request, and the firewall rule is written afterwards, only for a peer
that answered the requirement.

What is *not* here: source (a) of the earlier draft — celaut instances that declare the
network, found through a network-membership index. There is still no such index
(`grep -rni network src/database/access_functions/` → nothing), and it stays out of
scope. Note that (d) reaches much of the same ground through a different door: a node
that resolved this domain for its own guests answers with what it found.

### 2.5.1 `Gateway.ResolveNetwork`

A new RPC: `Service.Network` → `ConfigurationFile.NetworkResolution`.

**Generic, not `pow:`-shaped.** A domain is declared the same way whatever resolves it
— tags, prose, formal — and an RPC per mechanism would make every caller decide in
advance which kind it was holding, which is the resolving it was trying to delegate. So
the RPC exposes `resolve_network` itself, and a node may answer for a DNS tag as
readily as for a `pow:` one.

Three properties, each a decision:

* **It is not a grant.** The reply is what the answering node believes. The caller
  opens nothing on it and verifies every address exactly as it verifies one from its
  own `config.yaml`. An `Instance` in the reply is a suggestion, which is why the
  client (`src/manager/network_discovery.py`) flattens them to bare addresses rather
  than carrying a claim nothing checked.
* **The operator's policy applies** (`service_networks`). Resolving a domain this node
  refuses to reach — handing over addresses it would not use itself — is reaching it by
  proxy; the same argument puts the policy check before the balancer in
  `launch_service` rather than after it. The rejection propagates as a rejection, so a
  caller can tell "not from this node" from "nobody is there".
* **The question is never relayed** (`resolve_network(..., ask_peers=False)`).
  Answering by asking our peers, who ask theirs, is a walk over a graph nobody has a
  view of — and two nodes that know each other are already a cycle of length two. Each
  node answers from what it knows locally; a caller wanting more breadth asks more
  nodes itself, which keeps the cost with whoever chose to spend it.

Two peers naming the same address have confirmed nothing — they may well have read it
off the same list — so answers are pooled, never voted on. Treating agreement as
evidence would be the one reading of this that *is* a trust decision.

> **A fourth property, added by
> [#385](https://github.com/celaut-project/nodo/issues/385).** As written above the
> RPC resolves *any* `Service.Network` handed to it, and nothing relates the request
> to what the caller declared. Once deferred resolution exists that is a hole: a
> guest whose spec pins `pow:ergo` to block B could ask here for `pow:ergo` pinned to
> nothing and be answered with peers on any Ergo-shaped chain. **When the caller is
> one of this node's own local instances, the request must now fit inside a network
> its service declares** — same tags, every key the declaration fixed present and
> unchanged, keys it left `${VAR}` free to fill, new keys free to add. Narrowing is
> granted; broadening is refused. A caller this node cannot identify as a local
> instance — *including another node asking as a peer, which is this section's own
> use of the RPC* — has no spec here to be measured against and is answered exactly
> as described above. The rule is tabulated in [`NETWORKS.md`](../NETWORKS.md).

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

**Ancestor chain** — this is where `formal` starts being read. `match_networks` was a
tag intersection with a `# TODO Could be more powerfull` on it (`networks.py:130`),
which was harmless while nothing put anything in `formal` and stopped being harmless
the moment a `pow:ergo` network carried its whole ask there: two services both tagged
`pow:ergo`, asking for different blocks and different amounts of work, were being
authorized as the same domain.

The rule is **the one every other tags/prose/formal descriptor in celaut is compared
by** — `node_identity.same_component`, which `Peer.SignatureScheme` in `celaut.proto`
states and which a signature scheme and a transport stack already use:

> `formal` decides whenever **both** sides declare one, byte for byte. Otherwise one
> shared tag is enough. `prose` is never compared.

A `Service.Network` is such a descriptor, so it gets that rule and no second one.
`same_component` became public for this; `same_component_stack` is not what is wanted,
because that pairs up a *stack* of descriptors and a Network is one.

An earlier draft of this document proposed a richer rule instead: compare the fields,
and let a child ask for a *narrower* domain than its father (`min_cumulative_difficulty`
≥ the father's, `max_tip_age_s` ≤ it, and so on). It is not obviously wrong, and it is
rejected anyway:

* It is a **second comparison semantics for one field**, known to exactly one module.
  Every other `formal` in celaut is compared as opaque bytes; a reader of
  `match_networks` would have to know that this one is not, and the next kind of
  network to use `formal` would need its own ladder or would silently fall back to
  bytes.
* "Narrower" is only orderable because *these particular* fields happen to be
  numeric thresholds. `block_id` already breaks it — the draft had to write "v1:
  require equal" — and anything non-scalar breaks it completely.
* It buys convenience, not safety. Both rules refuse the same dangerous case (a child
  asking for something weaker than its father granted).

What the simple rule costs, stated plainly: **a father that declares a `formal` grants
that exact ask and no other.** A child asking for strictly more work than its father
demanded is refused, even though it would have been safe. The way to grant a family of
asks is the way it already reads — declare the tag and leave `formal` empty, which is a
father saying "any `pow:ergo` my children care to specify". Nothing that declares no
`formal` anywhere changes behaviour at all.

This is why §2.3's canonical form matters: two nodes that mean the same requirement
must produce the same bytes, and `canonical_formal` using deterministic protobuf serialization is what makes that
true without anyone having to remember a rule.

**`environment_variable`** — removed (#396, §1.6); it was inert for PoW peers for
the same reason it was inert for DNS ones: a chain node is not a celaut instance
and has no environment to read. Partitioning among PoW peers, if ever needed, is
expressed the same way as everywhere else: a `${VAR}` selection key in `formal`.

**Firewall** — each qualifying peer becomes **its own `Instance`** with one
`Uri(ip, port)` naming that peer's **P2P endpoint**. They are separate operators,
separately verified, separately reachable and separately worth dropping, and one
`Instance` carrying N uris says the opposite — that shape means "one peer at several
addresses", which is what `resolve_domain` legitimately builds out of the A records of
a single name.

*The uri is the P2P endpoint, not the REST one.* Verification goes to `restApiUrl`
(§2.6) because only the REST API can answer "does your main chain contain B". But a
service that declared `pow:ergo` is asking for **chain peers** — something to sync
against — and the chain protocol is not spoken on the REST port. Emitting the REST
address granted egress that could not be used for what it was granted for, which is the
worst shape a firewall rule can take: useless *and* open. The REST uri is therefore not
also emitted alongside it. An `Instance.Uri` carries an ip and a port and nothing that
says which is which, so a second uri would be indistinguishable to the guest while the
firewall opened both; a guest that wants a REST API asks for one, and that ask is a
different network descriptor, not a second uri smuggled into this one.

*Where the port comes from, without assuming one.* **This node does not assume a port
for a network wherever it can observe one.** Ergo's `/peers/connected` entries carry
`address` (`/1.2.3.4:9030`, Java's `InetSocketAddress.toString()`) alongside
`restApiUrl`, so `get_refresh_peers()` keeps it as `p2pAddress` — an addition to the
entry, never a change of its shape, so a file written by an older nodo still reads. A
peer found that way is emitted at exactly the port it was seen on. That matters
concretely: of 58 peers on one live mainnet node's `/peers/connected`, 53 were on 9030
and five were not (9020, 9029, 9031, 1540).

A candidate whose P2P endpoint nobody observed — `ledgers.ergo.NODE_URL`,
`service_networks.default_instances`, a peer's `ResolveNetwork` answer, or a crawl entry
from before this field existed — reuses the REST **host** with Ergo mainnet's
conventional P2P port (`src/manager/ergo.py::MAINNET_P2P_PORT`, 9030), and the
assumption is logged with the address it was made for. Reusing the host is not an
assumption of the same kind: that is where the node which answered `/info` lives. Only
the port is a guess.

(This was briefly a config key, `pow_networks.ERGO_P2P_PORT`, overridable per the
reasoning above. It became a plain constant instead: nothing here can tell which peers
run on a non-conventional port to bias an operator's override towards, so the setting
could only ever be guessed at exactly as blindly as the default it replaced — one more
key to read past for no decision anybody was actually in a position to make.)

**Update: the REST uri is emitted after all — just never to a guest that did not ask
for it.** The paragraph above ("the REST uri is therefore not also emitted") described
the *guest-facing* grant correctly but conflated it with what `resolve_pow_network`
itself builds. The two turned out to need different answers once this node's own peer
discovery (`network_discovery.ask_peer`) needed a way to verify a *peer-suggested*
candidate's REST endpoint instead of guessing at Ergo mainnet's conventional REST port
(`src/manager/ergo.py::MAINNET_REST_PORT`, 9053) every time: guessing wrong meant every
`ask_peers`-sourced candidate failed verification silently, emptying that source.

So `resolve_pow_network` now builds **both** slots on every `Instance`, tagged apart
(`pow_networks.P2P_SLOT_TAG` / `REST_SLOT_TAG` in `Api.Slot.protocol_stack` — never the
requester's own `protocol_stack` echoed onto either, which is a different, identical-
for-every-candidate fact). A remote node asking as a peer gets the full picture, since
nothing it does opens a firewall on the strength of the answer; it is only what reaches
a **local guest's** own `NetworkResolution`, and therefore its firewall grant, that
still needs narrowing to preserve the original guarantee. `narrow_instances_for_local_
grant` does that narrowing, called from both `resolve_network_for_peer` (the deferred
RPC path this section is about) and `rootfs.build_network_resolution` (the eager,
launch-time one, which is where most guests actually go through this): the REST slot
is kept only when the guest's own declared `protocol_stack` explicitly names it, so a
bare `pow:ergo` tag reads exactly as before this existed.

*Checked: Ergo has no way to ask a node for its own P2P address.* `/info` reports
`restApiUrl` and nothing else addressable — verified live against a mainnet node, whose
28 `/info` keys include no P2P address, port or bind field. `/peers/all` carries the
same `address`/`restApiUrl` pairs as `/peers/connected` and is likewise about the
node's *peers*, not itself. So a peer's P2P port is learnable only from some **other**
node that connected to it, which is exactly the crawl, and is unavailable for a
candidate reached any other way. Hence the default.

That required a fix at the other end, which this proposal makes:

* `configure_guest_firewall_policy` (`network.py:559-569`) **stopped at the first peer
  instance for which a rule applied**. Nothing had noticed, because a resolution had
  never been more than one instance — but `ConfigurationFile.network_resolution` hands
  the *guest* every instance, so the guest was being given a list of addresses its own
  node's firewall would refuse. Which peers are reachable has to be the same question
  inside the guest and at the nftables rule. It now writes a rule for every instance,
  and one peer that cannot be opened no longer shuts the rest of the domain.
* A peer whose `restApiUrl` is a hostname needs a DNS lookup, which re-imports every
  §1.4 caveat. Prefer the numeric form when the crawl recorded one — which the crawl's
  `p2pAddress` almost always is, since it is the address a connection was made to.

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
| `src/manager/pow_networks.py` (new) | `PowRequirement`; `parse_pow_formal(formal, tag)` (`pow.`-prefixed keys validated, other keys carried as extensions and enforced by nothing, tag/chain agreement enforced, values parsed as exact `int`); `canonical_formal`; `candidate_urls` (the §2.5 sources); `ergo_peer_satisfies` (the §2.6 ladder); `resolve_pow_network`. Bitcoin parses and raises `NotImplementedError` with the §2.6 reason. |
| `src/identity/node_identity.py` | `parse_component_formal`, the inverse of `component_formal`, beside it because the two have to agree — the field is authored by hand as often as it is built. `_same_component` → `same_component`, made public for `match_networks` (§2.8). |
| `src/manager/networks.py` | One branch at the top of `resolve_network`'s tag loop (`if tag.startswith("pow:")`); `match_networks` now `same_component` (§2.8); `resolve_network_for_peer`, the decisions behind `Gateway.ResolveNetwork` kept out of its gRPC plumbing so they can be tested as decisions. |
| `src/manager/network_discovery.py` (new) | The client half of `Gateway.ResolveNetwork` (§2.5.1): `ask_peer`, `ask_peers`. Bare addresses out, never the sender's `Instance` grouping -- though each address keeps the protocol tags of the one slot it belonged to (e.g. `pow_networks.REST_SLOT_TAG`), which is a fact about that single, already-isolated address, not a claim about the sender's grouping. |
| `src/gateway/gateway.py`, `protos/celaut.proto`, `protos/celaut_pb2_grpc.py` | The `ResolveNetwork` RPC. **The gencode is hand-edited**, in the 1.56-era style the file is already in (it carries a hand-applied `from bee_rpc import buffer_pb2` fix): `bash/generate_protos.sh` needs `grpcio-tools==1.56.0` for the pinned protobuf 4.x, which has no wheel for current Pythons and does not build from source there. `celaut_pb2.py`'s embedded service descriptor is therefore one method out of date until someone regenerates it — nothing reads it (the grpc stub never imports `celaut_pb2`), and `tests/test_network_discovery.py` pins all four wiring points so a missed one fails in a test rather than in a handshake. |
| `src/virtualizers/microvm/network.py` | `configure_guest_firewall_policy` writes a rule for **every** peer instance, not the first that works (§2.8). |
| `config.example.yaml` | `pow_networks.TIMEOUT_SECONDS`, `.MAX_PEERS`, `.ASK_PEERS`; `service_networks.default_instances` (any tag → uris). **Not `networks:`** — that would sit one letter from the `network:` block, the same trap `service_networks` is named around. |
| `tests/` | `test_pow_networks.py` (the parser's accept/reject table, per-reason peer rejection, the instance-per-endpoint shape, the §2.5 source ordering, `match_networks`, `resolve_network_for_peer`), `test_network_discovery.py`, `identity/test_component_formal.py`, and two cases in `test_guest_policy_uses_the_right_hook.py`. No test touches the network or the clock. |
| `docs/NETWORKS.md` | Use Case 3 spelled out, linking here. |

**Since removed ([#396](https://github.com/celaut-project/nodo/issues/396)):**
`Service.Network.environment_variable` and `src/manager/network_env.py` (§1.6,
§2.8). Instance partitioning is expressed with a `${VAR}` selection key in
`formal` instead.

**Explicitly not in v1:** Bitcoin verification (the default backend is receive-only
Esplora, which exposes neither `chainwork` nor a peer list), reading *or* publishing
endpoint lists on the reputation ledger (open question 4), re-resolution,
cross-checking.

**Follow-ups, in order:** (1) endpoint lists on the reputation ledger, once the
on-chain formalization of a communication domain is settled with `skills`; (2) v2
cross-checking *k* of *n*; (3) `NetworkResolution.status`, so `[]` stops meaning three
different things (§2.9); (4) v3 header verification. Each is independently
revertible, which is the point of the order.

## 2.11 Open questions for Josemi

1. **Difficulty semantics** — §2.4 recommends cumulative work since genesis
   (`fullBlocksScore` / `chainwork`). Is "since the given block" the reading you
   had in mind instead? It is a different, also defensible ask.
2. **No qualifying peer** — `[]` (§2.9) or abort? `[]` is consistent with today;
   abort is arguably more honest to the service.
3. **What `match_networks` costs** — §2.8 takes the byte-comparison rule every other
   `formal` in celaut gets, which means a father declaring a `formal` grants that
   exact ask and nothing narrower. Is the family-grant idiom (declare the tag, leave
   `formal` empty) enough, or do you want the field-by-field ladder after all?
4. **Reading endpoints off the reputation ledger** — deliberately **out of scope for
   v1**. How a communication domain is formalized on-chain is still being settled with
   [`celaut-project/skills`](https://github.com/celaut-project/skills/issues/72): what
   identifies a network in R5, and whether `formal` carries celaut's sorted
   `key=value` body rather than a shape of its own. Wiring a reader against a schema
   that is about to change would bake in the version we are least sure of, so the
   source is left out entirely rather than shipped behind an unset type NFT. The
   `formal` half of that question is now settled in the same direction on both sides
   (§2.3); what identifies a network in R5, and matching a published definition to a
   concrete ask by subsumption rather than byte equality, are not.
5. **Regenerating the protos** — the `ResolveNetwork` gencode is hand-written
   (§2.10). Worth pinning a toolchain that still builds, or keeping the hand-edit and
   the wiring tests?
