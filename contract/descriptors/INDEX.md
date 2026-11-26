# Index of the descriptors

This file has two parts. The first part lists the descriptor files that exist. It is
generated: do not edit it. The second part lists the descriptors that nodo needs and
that have no file yet.

## 1. The descriptors that exist

<!-- BEGIN GENERATED: python -m src.utils.descriptor_file -->

The id is `<type>:<digest>` of the formal (REASON 6.3), with blake2b-256: the
algorithm that nodo uses. Another algorithm gives other ids for the same bytes.

| File | Tags | Id |
| --- | --- | --- |
| [`architectures/linux-amd64.descriptor`](architectures/linux-amd64.descriptor) | linux-amd64, linux/amd64, amd64, x86_64 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:7286c85c05460ba06509cc64b218a4d7470162983032d151fd47dbdb6cbef5d5` |
| [`architectures/linux-arm64.descriptor`](architectures/linux-arm64.descriptor) | linux-arm64, linux/arm64, arm64, aarch64 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:4d9137888704b0247a31c3705a875d4a70a54b80845bfc7332a7b39a58f93355` |
| [`contracts/payment/bitcoin-p2wpkh.descriptor`](contracts/payment/bitcoin-p2wpkh.descriptor) | bitcoin-p2wpkh | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:5495a6f5fc350871eb78b8251ca0b45addf48d385d96176911ba5da6c07cc35e` |
| [`contracts/payment/ergo-p2pk.descriptor`](contracts/payment/ergo-p2pk.descriptor) | ergo-p2pk | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:0e8ccfee22f3d3494097be8757c5fea6e1ca299d304823e8f79c391181dd88a6` |
| [`policies/api_slot/access.descriptor`](policies/api_slot/access.descriptor) | access | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:5344aac7355624c44adb896f24d835327efdb5fe6b0826a14a0b570b496cd37e` |
| [`policies/api_slot/tunnel_auth.descriptor`](policies/api_slot/tunnel_auth.descriptor) | tunnel_auth | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:0f8206ffef0314db0229f0cb367686a9631b8f85cfd62eb2b82932432df78043` |
| [`policies/api_slot/wake.descriptor`](policies/api_slot/wake.descriptor) | wake | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:2ff042caf1404c444e8aa8d48961e797cd061c176bee27fd33b7351c9a4eaa9a` |
| [`policies/container_init/guest-kernel.descriptor`](policies/container_init/guest-kernel.descriptor) | guest-kernel, kernel | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:9c03c66a3776b9ce1260b15aca7f0525ea6542092800cd4a23e07eda60cbeb97` |
| [`policies/container_init/notice.descriptor`](policies/container_init/notice.descriptor) | notice, stop notice, hibernate notice | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:6770fd4cbdc1ea323f0a26a2eaca3c3ad0631a94a6c8596d76b497416c746616` |
| [`policies/filesystem_entry/share.descriptor`](policies/filesystem_entry/share.descriptor) | share | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:1801e283b3be7286cc3445a2e128c3d921f75bddf1825e330c7b7553c45b9de2` |
| [`policies/filesystem_root/entry_metadata.descriptor`](policies/filesystem_root/entry_metadata.descriptor) | entry_metadata | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:3c06eea2a8fe2d1476af508118965a67971d8e178f87caded980e6d78107eb42` |
| [`policies/filesystem_root/read_mode.descriptor`](policies/filesystem_root/read_mode.descriptor) | read_mode | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:e36826fd30068ee263c73edeb8f698c5c4761771801f707d1f6abb6642a503ec` |
| [`quantities/resources/cpu.descriptor`](quantities/resources/cpu.descriptor) | cpu, vcpu, cores | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:330d3be7de50d6ce7e00e5fc13c69dea20d6f55fa8df64d5e3f1676a58a02722` |
| [`quantities/resources/device-memory.descriptor`](quantities/resources/device-memory.descriptor) | device-memory, vram | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:ee9cad6566f696f7f73602862c74af5ae19c2c88e2ffa58c6030c0609100ad01` |
| [`quantities/resources/disk.descriptor`](quantities/resources/disk.descriptor) | disk, disk space, root filesystem | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:80ad9800a8342caa9ea8bbba4b3ce8031ad59482aea81cc908568af13d6e7926` |
| [`quantities/resources/memory.descriptor`](quantities/resources/memory.descriptor) | memory, ram | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:dbc1dc07783ac6870cea3746ec3dbfe3632f5e7d076dccde917b46c6773656db` |

<!-- END GENERATED -->

## 2. The descriptors to write

Each item is a descriptor that nodo needs: a thing that it implements today and must
expose in its interface or check in a service, or a thing that the new contract needs.
"Today" gives where nodo implements it now (with the old `celaut_pb2`), and how it
names it: a tag, a key of the configuration, a key of `service.json`, or a formal that
the code builds with `component_formal` and that has no file yet. "New" is a thing that
nodo does not implement yet. The column "Name" is a proposal. When you write a file,
remove its row here.

### `architectures/devices/`

| Name | Today | Notes |
| --- | --- | --- |
| a family of GPUs, one for each compute capability | New. PR #649 (not merged) uses tags `pci:<vendor>:<device>+<alg>:<hash>`. `GetResourceAvailability` refuses every device (`src/gateway/iterables/resource_availability_iterable.py:52-66`). | One descriptor for each member of the family. |

### `quantities/resources/`

| Name | Value / unit | Today | Notes |
| --- | --- | --- | --- |
| (`blkio_weight`) | weight | packed, not enforced (`hotplug.py:107`) | Do not write it: nodo does not enforce it. |

### `quantities/benchmarks/`

| Name | Value / unit | Today | Notes |
| --- | --- | --- | --- |
| `int-ops` | operations per second of one core | key `int_ops_per_sec` (`src/utils/benchmark.py:65-70`) | Today one run at the start of the node, with no statistic (`src/core_services/benchmark.py`). The formal must give `stat` and `method`. |
| `float-ops` | operations per second of one core | key `flt_ops_per_sec` | As above. |
| `sha256-hashes` | hashes per second of one core | key `sha256_hashes_per_sec` | As above. |
| `memory-bandwidth-64mib`, `-256mib`, `-1gib` | bytes per second, for a working set | keys `mem_bandwidth_<size>_bytes_per_sec` (`benchmark.py:46`) | One descriptor for each working set. Today a service can ask any size and the node compares with the next larger size: a formal cannot do that, so the service asks one of the three. |
| the percentiles to support | | New | For a score that must be stable. |

### `policies/container_init/`

| Name | Value | Today | Notes |
| --- | --- | --- | --- |
| `resume` | how a process continues after a wake | No setting: a wake starts the entrypoint on the kept disk, with no memory snapshot (`src/manager/manager.py:1376-1399`) | Q1.2. The default needs no setting. Write one only for another way. |
| `config-file` | the path of the file where the node writes the configuration | old field `Container.config_declaration`; the node always writes `/__config__` (`src/virtualizers/microvm/rootfs.py:41-44`) | The redesign removed the field. Decide if a setting is necessary, or if `/__config__` is a part of the architecture. |

### `policies/filesystem_root/`, `policies/filesystem_entry/`, `policies/api_slot/`

The six settings that nodo has today have their files (part 1). They need the lines
`class=` and `name=` at stage 7 (`REASON.md`, rule 6.4).

### `policies/network/`

| Name | Value | Today | Notes |
| --- | --- | --- | --- |
| `template` | the keys of the domain that the node fills at a start | a grammar inside the values of the network formal: `${VAR}`, the full value only (`src/manager/network_templates.py:68,191`) | Absent: the value of the domain is fixed. |
| `share` | the id of a network share | the filesystem setting `share` with `role=network`; id = sha256 of length-prefixed fields (`src/utils/shared_filesystems.py:397-433`) | The formal fixes the id that `ShareLocationRequest.share_id` carries. |

### `policies/device/`

| Name | Value | Today | Notes |
| --- | --- | --- | --- |
| `attach` | the ordered list of the accepted modes (at the start, on request) | old enum `Attach` in the pb2 only | Absent: at the start. |
| `detach-notice` | seconds | New | |
| `link` | the positions of the other entries | New | For example NVLink. |
| `bitstream` | a path in the filesystem | New | For an FPGA. |

### `policies/configuration/`

| Name | Value | Today | Notes |
| --- | --- | --- | --- |
| `instance-name` | lowercase `[a-z0-9]+(-[a-z0-9]+)*`, 63 characters at most | the reserved environment variable `__nodo_instance_name` (`src/utils/instance_names.py:8-10,81-109`) | |
| `device-selection` | the position of an entry and the stable id of a device | New | |

### `facts/`

| Name | Value | Today | Notes |
| --- | --- | --- | --- |
| `guest-kernel` | the hash of the kernel that the instance boots | field `kernel` of the old start receipt, always empty (`src/identity/execution_receipt.py:390-431`, `src/gateway/launcher/local_execution/local_execution.py:614`) | |
| `device-models` | the list of the models of the attached devices | field `devices` of the old start receipt, always empty | One fact whose value is the list. |
| `pci-model`, `usb-model` | `vendor:device` | New | In `Peer.devices`, and the scope of the rate of a device. |
| `device-id` | a stable id that the node selects | New | |
| `device-place` | a text for a person | New | |
| `device-links` | the stable ids of the linked devices | New | |

### `terms/conditions/`

| Name | Value | Today | Notes |
| --- | --- | --- | --- |
| `schedule` | the spans of activity, in minutes of UTC | `activity_window.WINDOWS` (`src/utils/activity_window.py:108`), old `Peer.activity_window` and offer `closes_at` | |
| `on-close` | what the node does at the close: refuse, stop or hibernate | `activity_window.ON_CLOSE`, old offer field `on_close` (`src/utils/on_close.py:9-13`) | |
| `scarcity-curve` | how the price grows with the load | `pricing.SCARCITY_CURVE` (`src/utils/monetary.py:416`), not announced, a float | It is not exact: write it as integers. |

### `terms/promises/`

| Name | Value | Today | Notes |
| --- | --- | --- | --- |
| `scarcity-max` | the most that the multiplier can be | `pricing.SCARCITY_MAX_MULTIPLIER` (`monetary.py:411`), sent today as a key of `Peer.rates` | It is a term, not a price. |
| `price-bound` | the growth bound v for each period T | old offer fields `price_bounded`, `price_volatility`, `volatility_period_seconds` (`src/utils/price_bound.py:171`) | A ratio is two integers. |
| `launch-load` | the load at which the price was computed | old offer field `launch_scarcity`, in ppm (`src/utils/signed_offer.py:29-47`) | |
| `hibernated-grace` | seconds that the node keeps a hibernated instance that cannot pay | `instances.SUSPENDED_GRACE_SECONDS`, not announced (`src/utils/dead_instances.py:91`) | Q1.1. |
| `stop-notice` | seconds between the notice and the stop | `instances.STOP_GRACE_SECONDS` (`notice.py:493`) | |
| `free-below-load` | the load under which the node does not charge | `free_tier.FREE_WHILE_SCARCITY_BELOW`, not announced (`monetary.py:334`) | |
| `new-client-credit` | MU | `free_tier.CREDIT_MU_PER_NEW_CLIENT` (`manager.py:904`) | |
| `statement-retention` | days | `ledger.RETENTION_DAYS`, old `BalanceStatement.retention_days` | |
| `device-wipe` | none | New | The node wipes a device between two clients. |

### `prices/node/`

All are keys of the old `Peer.rates` today (`src/utils/cost_functions/general_cost_functions.py:23-43`), with no
formal. The configuration is in MU for each hour, and the announcement for each second
(`hour // 3600`, so the announced figure is truncated).

| Name | Of / base | Today |
| --- | --- | --- |
| `memory-time` | `memory`, one GiB for one second | `ram_mu_per_gib_second`; also `ram_mu_per_gib_second:<arch>` (the scope is the architecture) |
| `cpu-time` | `cpu`, one core for one second | `cpu_mu_per_vcpu_second` |
| `disk-time` | `disk`, one GiB for one second | `disk_mu_per_gib_second` |
| `network-bytes` | one GiB of relayed traffic | `net_mu_per_gib` |
| `build` | one build | `build_mu` |
| `tunnel-open` | one `ServiceTunnel` | `tunnel_open_mu` |
| `modify-resources` | one `ModifyServiceSystemResources` | `modify_resources_mu` |
| `statement-query` | one `GetBalanceStatement` | `statement_query_mu` |
| `statement-row` | one movement in a statement | `statement_row_mu` |
| `kept-state` | the kept state of a hibernated instance | No key: the disk rate on `disk_space` (`src/manager/maintain.py:917-940`) |
| `device-time` | one device for one second, scope = the model fact | New |
| `connection-open` | one connection | New: listed before, but no code |

### `prices/service/`

| Name | Of / base | Today |
| --- | --- | --- |
| `call` | one call, scope = the name of the method | old `Slot.mu_per_call`, from `service.json` `api[].mu_per_call` (`src/packers/zip_with_dockerfile.py:950-963`) |

### `limits/`

nodo enforces these today with keys of its configuration, and announces none
(`Peer.limits` is never written). Write a descriptor only for a limit that a caller
must know.

| Name | Per | Today |
| --- | --- | --- |
| `calls` | one client, for a window | `communication.CLIENT_RATE_LIMIT_WINDOW_SECONDS`, `MAX_CALLS`, `QUARANTINE_SECONDS` (`src/gateway/client_gate.py:72-80`) |
| `reputation-pull`, `reputation-push` | one client, and all | `reputation.PULL_*`, `PUSH_*`, `MAX_OFFCHAIN_*_PER_RESPONSE` |
| `chat-message-bytes`, `chat-stored` | one client | `chat.MAX_MESSAGE_BYTES`, `MAX_STORED_MESSAGES_PER_CLIENT` |
| `hibernated-instances`, `hibernated-disk` | one parent | `instances.MAX_SUSPENDED_PER_FATHER`, `MAX_SUSPENDED_DISK_GIB_PER_FATHER` |
| `descendant-depth`, `descendant-accounts` | one account | `ledger.MAX_DESCENDANT_DEPTH`, `MAX_DESCENDANT_ACCOUNTS` |
| `recursion-hops` | one request | `network.RECURSION_MAX_HOPS` |
| `network-bytes-per-day`, `network-rate` | all | `host_limits.MAX_NET_GIB_PER_DAY`, `MAX_NET_MIB_PER_SECOND` (`src/utils/host_limits.py:361-384`) |

### `networks/`

| Name | Value | Today | Notes |
| --- | --- | --- | --- |
| `all-addresses` | empty | tag `*` (`src/utils/network_policy.py:106`) | |
| `host-name` | the name and its ports | a lowercase tag with a dot; the formal key `port`, default ports 80 and 443 (`src/manager/networks.py:51-100,528`) | IPv4 only today. |
| `celaut-instances` | the keys of the domain | any other tag; the formal is free `key=value` lines, compared by bytes (`networks.py:912`) | |
| `proof-of-work` | the chain and the keys `pow.block_id`, `pow.min_cumulative_difficulty`, `pow.min_height`, `pow.max_tip_age_s` | tag `pow:<chain>`, formal `pow.*` (`src/manager/pow_networks.py:74-94`) | One descriptor, the chain in the value. Only Ergo resolves. |
| (ledger with no addresses) | | tag `bitcoin` (`networks.py:149`) | It resolves to no peers: check if it is still necessary. |

### `dependency_sources/`

| Name | Value | Today |
| --- | --- | --- |
| `service-file` | a path in the filesystem of the service | old field `on_filesystem`, never read by the node (`src/packers/service_json.py:123-147`) |
| `registry` | the location of a registry | New. Today the node gets a service from its registry, from `StartService`, from its peers (`GetService`) and from the source-application core service (`src/core_services/source_application.py`); none is a source of a dependency. |

### `protocols/address/`

The code builds these formals with `component_formal` (`src/identity/transport_stack.py`).
Copy the bytes into a file, then add `class=` and `name=` at stage 7 (they are on the
wire today).

| Name | Today | Notes |
| --- | --- | --- |
| `tcp` | `transport_stack.py:136-152` | |
| `udp` | tag only (`src/routing/registry.py:43`, `src/virtualizers/firewall.py:15-25`) | No formal yet. |
| `tor-onion-v3` | `transport_stack.py:117-133` | |
| `tls` | `transport_stack.py:159-206` | |
| `http2` | `transport_stack.py:213-223` | |
| `grpc` | `transport_stack.py:230-259` | |
| `bee-rpc` | `transport_stack.py:266-351` | Its keys hold the schema of `buffer.Buffer`. |
| `celaut-gateway` | `transport_stack.py:418-720` | A wire schema (REASON 2.5): split each rule that two nodes must agree on (hash types, signature formats, receipts, proof of work, schemas) into a formal of its own. |
| `celaut-observe`, `celaut-chat` | Today Chat and Observe are RPCs inside the gateway layer | Each one is a branch of the tree with its own formal (REASON 2.8). |
| `ergo-p2p`, `ergo-rest` | tags only, on the members of a proof-of-work network | |

### `protocols/node_gateway/`

| Name | Today | Notes |
| --- | --- | --- |
| one formal for each group of RPCs that a service uses (resources, dependencies, statements, ...) | The packer writes the full gateway stack if `service.json` has `uses_gateway: true` (`src/packers/zip_with_dockerfile.py:635-648`). The node checks the tag `celaut-gateway` only (`src/virtualizers/microvm/rootfs.py:445-459`). | S6.5. The subset check is a TODO in `rootfs.py:451-457`. |

### `contracts/payment/`

| Name | Today | Notes |
| --- | --- | --- |
| `simulated` | `src/payment_system/contracts/simulator/interface.py:11-24` | For tests only. Write it only if a test needs the file. |

### `ledgers/`

| Name | Today | Notes |
| --- | --- | --- |
| `ergo-mainnet` (payment) | `src/utils/ledger_descriptors.py:123-155` | The formal depends on the configuration (`GENESIS_BLOCK_ID`): one file for each network. |
| `ergo-mainnet` (reputation) | `ledger_descriptors.py:184-224` | The formal holds the R4 type ids of the configuration. Decide if these ids are a part of the ledger or values. |
| `bitcoin-mainnet` | `ledger_descriptors.py:285-320` | As above. |

### `signatures/schemes/`

| Name | Today | Notes |
| --- | --- | --- |
| `ed25519` | `SIGNATURE_SCHEME_COMPONENTS` (`src/identity/node_identity.py:212-239`) | The scheme of the identity of a node. |

Low priority: the API of a verifier of signatures, only for a node that verifies a peer
through a service (REASON 1.7). A client that uses a registry gets the API from the
objective.

### `schemas/`

| Name | Today | Notes |
| --- | --- | --- |
| the format of a schema | `src/identity/protocol_schema.py:109`: `schema.<name>=<num>:<cardinality>:<type>,...`, with no names of fields | To decide first: add the name of each field. |
| `service` | New | For `Service.schema`. |
| `peer`, `estimated-cost`, `start-receipt`, `tunnel-checkpoint`, `balance-statement`, `reputation-proof`, `client`, `observe-checkpoint` | Today each signed type has a text prefix (`celaut-price-offer:`, `celaut-execution-receipt:`, ...; `node_identity.py:63-91`) | One for each signed type, for `Signed.Body.context`. |
| chat entry, ledger attestation | prefixes `celaut-chat-signature:`, `celaut-ledger-attestation:` | The new contract has no type for them yet. |

### `environment_formats/`

| Name | Today | Notes |
| --- | --- | --- |
| `text` (any value) | the packer writes an empty format for each name of `service.json` `envs` (`zip_with_dockerfile.py:897-904`); no format is checked | The node checks only the name and the value for Linux (`src/utils/guest_env.py:30-59`). Start with one format. |

### `proof_of_work/`

| Name | Today | Notes |
| --- | --- | --- |
| `blake2b-trailing-zeros` | `src/gateway/client_pow.py:47-73`: tags `blake2b`, a formal in pseudo-code: the hex of blake2b-512 of challenge and solution ends with d zeros, d at most 64 | Rewrite the formal as `key=value` lines. |

The proof of work of `ReputationProof.pow_nonce` (`src/reputation_system/offchain_proof.py:139-221`) has no
descriptor: its rule is a part of the gateway layer.
