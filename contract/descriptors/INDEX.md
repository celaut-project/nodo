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
| [`dependency_sources/service-file.descriptor`](dependency_sources/service-file.descriptor) | service-file, on_filesystem | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b941f5b9e824d666cf2a3b50aa25139bfb8ba108796e8cde0f2b4d4a60cb6c0a` |
| [`environment_formats/any.descriptor`](environment_formats/any.descriptor) | any, text | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:839fdff7feb387de47e5a5f6457da58dc0c882690f0f9f2d8ce77211aa40ef21` |
| [`facts/device-id.descriptor`](facts/device-id.descriptor) | device-id | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:6ce6c432403ebc0b978b7a38df1f7bc3b89229a42af71656dbfb8c7b26a70617` |
| [`facts/device-links.descriptor`](facts/device-links.descriptor) | device-links, nvlink | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:513329b19e532424e1702f2197e1bb43f94e230165db90b209e47c01a64f821d` |
| [`facts/device-models.descriptor`](facts/device-models.descriptor) | device-models | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:ebd1ea752be8e0274e2857ed673f61d1cf3504e25e0251f230341d5d38e26024` |
| [`facts/device-place.descriptor`](facts/device-place.descriptor) | device-place, location | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:10026c1cb62e5e15d214e6efee25adbede6a0c4cdd7d0ffea3e8ee5a0ccdeb73` |
| [`facts/guest-kernel.descriptor`](facts/guest-kernel.descriptor) | guest-kernel, kernel | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:26509ebd9c5e14da0697ed1ed31f3986da202024645516a2d9c722e198b0c69e` |
| [`facts/pci-model.descriptor`](facts/pci-model.descriptor) | pci-model, pci | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:5d2ce65a6fa1e2a557a53196d610878b547bc1bc87ab8e63a7cabf5fd85caff1` |
| [`facts/usb-model.descriptor`](facts/usb-model.descriptor) | usb-model, usb | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:00fec13f609faabea95d819413049d3f2a92abfe46e4fc5298a66b5400d498f5` |
| [`ledgers/bitcoin-mainnet.descriptor`](ledgers/bitcoin-mainnet.descriptor) | bitcoin-mainnet | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:42c3dae582a538b4e29e9c246d11a88dd1d4097995c66a2e9622517c163424ba` |
| [`ledgers/ergo-mainnet.descriptor`](ledgers/ergo-mainnet.descriptor) | ergo-mainnet | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:003ec055b8a277e480cb04389b4a5c54eb7bf4e4596ce89959515d1707d98b7f` |
| [`limits/calls.descriptor`](limits/calls.descriptor) | calls, rate limit | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:0da197270dfadba911df7395d04d3a247aa94e5d6914adce75f6ace09df4bc18` |
| [`limits/chat-message-bytes.descriptor`](limits/chat-message-bytes.descriptor) | chat-message-bytes | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:02686ebd00a87e05830928cb9449c3c99201e5394a376c018e4def5c7f621d6c` |
| [`limits/chat-stored.descriptor`](limits/chat-stored.descriptor) | chat-stored | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:0435e5bb354a0536291faf1eef1c81a436726c2c071c0c86dbe09905bdb8333e` |
| [`limits/descendant-accounts.descriptor`](limits/descendant-accounts.descriptor) | descendant-accounts | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:0b4e3511f64b3e7cbd108288d68795745bad00c0d2440009ee2913ca49d89d79` |
| [`limits/descendant-depth.descriptor`](limits/descendant-depth.descriptor) | descendant-depth | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:7a90ef6ed4668e3817a21524e2e4a08d002d0de9c4ffd65ad4d61475e7c8cf0e` |
| [`limits/hibernated-disk.descriptor`](limits/hibernated-disk.descriptor) | hibernated-disk | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a167dd3763a1ff5a43d27a08b633aa6a813e6372268b895d6ee7dcae0956bebd` |
| [`limits/hibernated-instances.descriptor`](limits/hibernated-instances.descriptor) | hibernated-instances | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:50f25c5c57c35c319f9716a8d1129fe577bc6841a359f7eed061d8dac547ba77` |
| [`limits/network-bytes-per-day.descriptor`](limits/network-bytes-per-day.descriptor) | network-bytes-per-day | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:8e2e225278544843720898b52ff38d0332d6b744c5cd0849f87aca2d2c50cb3b` |
| [`limits/network-rate.descriptor`](limits/network-rate.descriptor) | network-rate | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:6605388849e23542b2bdf73e242867726082ef81a36cb3b40e6be3d683b9169d` |
| [`limits/recursion-hops.descriptor`](limits/recursion-hops.descriptor) | recursion-hops | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:6311094659448485673bdaae357c2648a48de03300fdcb9832cec3f70cd79817` |
| [`networks/all-addresses.descriptor`](networks/all-addresses.descriptor) | all-addresses, *, internet | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:54993b26f2e16a140f25aa6b364a8f3fc8e0f1640b858e1e1d903f91d269d634` |
| [`networks/celaut-instances.descriptor`](networks/celaut-instances.descriptor) | celaut-instances | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a82825a2cd5e7acf69279d824ebaf835f408ecf442b39fdf35dcbda19519f3bf` |
| [`networks/host-name.descriptor`](networks/host-name.descriptor) | host-name, domain, dns | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:6236bd35a58a755b506c50942fb49c83868712c8fe5cef9472a9d391b0075590` |
| [`networks/proof-of-work.descriptor`](networks/proof-of-work.descriptor) | proof-of-work, pow | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:ac37fb1f604bd95a4fc3f6a4195188cf1053d89681b04b27050b60f6d80907ec` |
| [`policies/api_slot/access.descriptor`](policies/api_slot/access.descriptor) | access | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:5344aac7355624c44adb896f24d835327efdb5fe6b0826a14a0b570b496cd37e` |
| [`policies/api_slot/tunnel_auth.descriptor`](policies/api_slot/tunnel_auth.descriptor) | tunnel_auth | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:0f8206ffef0314db0229f0cb367686a9631b8f85cfd62eb2b82932432df78043` |
| [`policies/api_slot/wake.descriptor`](policies/api_slot/wake.descriptor) | wake | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:2ff042caf1404c444e8aa8d48961e797cd061c176bee27fd33b7351c9a4eaa9a` |
| [`policies/configuration/device-selection.descriptor`](policies/configuration/device-selection.descriptor) | device-selection | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:84ca8ca8b4c4122d49fcc0681890e899e74388745443aa138f028b316cfa0733` |
| [`policies/configuration/instance-name.descriptor`](policies/configuration/instance-name.descriptor) | instance-name | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:667a8101b6e3ee7b61efe110e8cc299cff9b1d3b8dfcd71577b7e6881e22ae49` |
| [`policies/container_init/guest-kernel.descriptor`](policies/container_init/guest-kernel.descriptor) | guest-kernel, kernel | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:9c03c66a3776b9ce1260b15aca7f0525ea6542092800cd4a23e07eda60cbeb97` |
| [`policies/container_init/notice.descriptor`](policies/container_init/notice.descriptor) | notice, stop notice, hibernate notice | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:6770fd4cbdc1ea323f0a26a2eaca3c3ad0631a94a6c8596d76b497416c746616` |
| [`policies/device/attach.descriptor`](policies/device/attach.descriptor) | attach | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:75c8d0f3f707a640af0dde55be7300843afd674434b8a91b9eff9c58cf858252` |
| [`policies/device/bitstream.descriptor`](policies/device/bitstream.descriptor) | bitstream, fpga | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:fee77e45f7230ffa64d15d4819410335c2a33b48d751bf5f9364beaa1f2ff949` |
| [`policies/device/detach-notice.descriptor`](policies/device/detach-notice.descriptor) | detach-notice | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:4cdee552858ae5b85f0b500384c7cd61232ec766dcbffbd91b170ed9350993fc` |
| [`policies/device/link.descriptor`](policies/device/link.descriptor) | link | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:09d1ed910e580504bb708fe728db67dd8695c2d1c9a50ea21e45e2ebcde9f872` |
| [`policies/filesystem_entry/share.descriptor`](policies/filesystem_entry/share.descriptor) | share | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:1801e283b3be7286cc3445a2e128c3d921f75bddf1825e330c7b7553c45b9de2` |
| [`policies/filesystem_root/entry_metadata.descriptor`](policies/filesystem_root/entry_metadata.descriptor) | entry_metadata | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:3c06eea2a8fe2d1476af508118965a67971d8e178f87caded980e6d78107eb42` |
| [`policies/filesystem_root/read_mode.descriptor`](policies/filesystem_root/read_mode.descriptor) | read_mode | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:e36826fd30068ee263c73edeb8f698c5c4761771801f707d1f6abb6642a503ec` |
| [`policies/network/template.descriptor`](policies/network/template.descriptor) | template | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:1a9e77d2c08be6569abad3ab22ee37a13703d8a2ed44891d6a3850b157198333` |
| [`prices/node/build.descriptor`](prices/node/build.descriptor) | build, build_mu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:1c87e45cc18f22209bce096e9fa1f3f2a4d42097c39dc3033494bad3d25c64eb` |
| [`prices/node/cpu-time.descriptor`](prices/node/cpu-time.descriptor) | cpu-time, cpu_mu_per_vcpu_hour | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:854e2b606d2fd8be6f254c48ef2346ab856bbf291093dce362be21694a8197eb` |
| [`prices/node/device-time.descriptor`](prices/node/device-time.descriptor) | device-time | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:12b7a755c0b492cf854eedd03fd532541067f8c5922e96ab3c1bdc8172cd3463` |
| [`prices/node/disk-time.descriptor`](prices/node/disk-time.descriptor) | disk-time, disk_mu_per_gib_hour | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:1e4c4ac3fa129ef84904cf54c8ad8f32decaa638077401d61a4edf1f78366a8a` |
| [`prices/node/kept-state.descriptor`](prices/node/kept-state.descriptor) | kept-state, hibernated disk | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:c2eb9478fbac0c1a644d108b997181c8ac62ca80ee04556b6d4f19113513bc30` |
| [`prices/node/memory-time.descriptor`](prices/node/memory-time.descriptor) | memory-time, ram_mu_per_gib_hour | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:56a9e602597b6cd64f0540b2f81e50a33c8fc58d2c88923310661b7aeb23612d` |
| [`prices/node/modify-resources.descriptor`](prices/node/modify-resources.descriptor) | modify-resources, modify_resources_mu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:2ad7ac2b7c7e9edca157a6dac6ecae2fac0245e08697be40a054ab29ad0b3606` |
| [`prices/node/network-bytes.descriptor`](prices/node/network-bytes.descriptor) | network-bytes, net_mu_per_gib | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b33cf84a75cb195b769347a58745d53d20ac442ae2a311dfa3971274f669659f` |
| [`prices/node/statement-query.descriptor`](prices/node/statement-query.descriptor) | statement-query, statement_query_mu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:cab5714d8ac109135a43c40224e8091ec2042e15b932447a652955d1c1ad9c49` |
| [`prices/node/statement-row.descriptor`](prices/node/statement-row.descriptor) | statement-row, statement_row_mu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a4cbe4a4a3cb20d20b16fb2959552f012bbfcfe18384f5b3692ff95043eba66d` |
| [`prices/node/tunnel-open.descriptor`](prices/node/tunnel-open.descriptor) | tunnel-open, tunnel_open_mu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a36a4346db273c99fd7123fbf7b4dcfddd82e8a4aa07ad8471b44d325e9a4185` |
| [`prices/service/call.descriptor`](prices/service/call.descriptor) | call, mu_per_call | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b9000d73c97bcb624709b4622dc70cf9289af193f83159705e013708152f229f` |
| [`proof_of_work/blake2b-trailing-zeros.descriptor`](proof_of_work/blake2b-trailing-zeros.descriptor) | blake2b-trailing-zeros, blake2b | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:3c624c2dd31cdaecdbfb8824de75935dc896cdeb066620ee38dca9b8874710ba` |
| [`protocols/address/ergo-p2p.descriptor`](protocols/address/ergo-p2p.descriptor) | ergo-p2p | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:2f1778bc0e2c778894af5dbc32e1ffd5ded17bf4187f4ffa11015cd2e405f030` |
| [`protocols/address/ergo-rest.descriptor`](protocols/address/ergo-rest.descriptor) | ergo-rest | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:d31451f5766e271e17e1585945a667a428a36eb21e3eb0c406c65e4be0514648` |
| [`protocols/address/grpc.descriptor`](protocols/address/grpc.descriptor) | grpc | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a318d9e22dfecbf06a11a00c08eb43b0a65ec886b50f0f5d83599bf891e0acf3` |
| [`protocols/address/http2.descriptor`](protocols/address/http2.descriptor) | http2 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:01575edd0b23255b9c1efa1727ba4fb635c150a14b4ab0576f739f87631c8acb` |
| [`protocols/address/tcp.descriptor`](protocols/address/tcp.descriptor) | tcp | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:db518cc9d8ba091449934c4a67fa56ae72c2823720e25355c092e25b823694ce` |
| [`protocols/address/tls.descriptor`](protocols/address/tls.descriptor) | tls | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:26ff6acdac9b95a18c334e65e676eb5c0fc1ba3c312d33c2bfafecc2c8aac5fe` |
| [`protocols/address/tor-onion-v3.descriptor`](protocols/address/tor-onion-v3.descriptor) | tor-onion-v3 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:81e06cee256629ffc232dc568d8b67da2ee1d207538c2054bd5257a5ec803484` |
| [`protocols/address/udp.descriptor`](protocols/address/udp.descriptor) | udp | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:296c06ccd189ac66e62d6c53ef0648b49ec3c86153fb60870d326cf04f5e4584` |
| [`quantities/benchmarks/float-ops.descriptor`](quantities/benchmarks/float-ops.descriptor) | float-ops, flt_ops_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:1188efaabf8d5930cc17fffcfcdcf91018894628a9b7ab28dd49822e372c4756` |
| [`quantities/benchmarks/int-ops.descriptor`](quantities/benchmarks/int-ops.descriptor) | int-ops, int_ops_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:7c5ddc260dce1a6e46def9a0bd8f2a6a3f8d8891fc7efa968b27bd9d61f5dcdb` |
| [`quantities/benchmarks/memory-bandwidth-1gib.descriptor`](quantities/benchmarks/memory-bandwidth-1gib.descriptor) | memory-bandwidth-1gib, mem_bandwidth_1gib_bytes_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:06d94bc381a2f084b59fd7710589d7df319283e1fdf3a503ee523fb2bebda01e` |
| [`quantities/benchmarks/memory-bandwidth-256mib.descriptor`](quantities/benchmarks/memory-bandwidth-256mib.descriptor) | memory-bandwidth-256mib, mem_bandwidth_256mib_bytes_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:48aec407dc2777f1ffe50a4337f4183e2202a8289ad6eb4ef2a0bd5734195723` |
| [`quantities/benchmarks/memory-bandwidth-64mib.descriptor`](quantities/benchmarks/memory-bandwidth-64mib.descriptor) | memory-bandwidth-64mib, mem_bandwidth_64mib_bytes_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:746a045bebda93d63b2a105bf220056461ce9c9da1fb048bf3c28644bbaed710` |
| [`quantities/benchmarks/sha256-hashes.descriptor`](quantities/benchmarks/sha256-hashes.descriptor) | sha256-hashes, sha256_hashes_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:c94022a0c3cb3b961ea1312f1e2c65b37058a949dc0b020f0151598442608a04` |
| [`quantities/resources/cpu.descriptor`](quantities/resources/cpu.descriptor) | cpu, vcpu, cores | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:330d3be7de50d6ce7e00e5fc13c69dea20d6f55fa8df64d5e3f1676a58a02722` |
| [`quantities/resources/device-memory.descriptor`](quantities/resources/device-memory.descriptor) | device-memory, vram | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:ee9cad6566f696f7f73602862c74af5ae19c2c88e2ffa58c6030c0609100ad01` |
| [`quantities/resources/disk.descriptor`](quantities/resources/disk.descriptor) | disk, disk space, root filesystem | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:80ad9800a8342caa9ea8bbba4b3ce8031ad59482aea81cc908568af13d6e7926` |
| [`quantities/resources/memory.descriptor`](quantities/resources/memory.descriptor) | memory, ram | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:dbc1dc07783ac6870cea3746ec3dbfe3632f5e7d076dccde917b46c6773656db` |
| [`signatures/schemes/ed25519.descriptor`](signatures/schemes/ed25519.descriptor) | ed25519 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:77465d8964bef14c447c879b0b8b55a44de49cb18db46c42148d724f2c349f38` |
| [`terms/conditions/on-close.descriptor`](terms/conditions/on-close.descriptor) | on-close, on_close | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:93bcef2d856121a56ed3573a77b498162c692e371d99bc84df1f0b917e6552e1` |
| [`terms/conditions/scarcity-curve.descriptor`](terms/conditions/scarcity-curve.descriptor) | scarcity-curve, scarcity_curve | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:c11e404b8a315e6f4f6804ad877e75c77f00605837af63dfb361e3d3f388e459` |
| [`terms/conditions/schedule.descriptor`](terms/conditions/schedule.descriptor) | schedule, activity window | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:59fdea41af046224bef39a2059df76f3779e44ba74aab00a623a3aea347207e1` |
| [`terms/promises/device-wipe.descriptor`](terms/promises/device-wipe.descriptor) | device-wipe | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:9e4de3477eab7c62295794b22d97b678c40a23d7d4fb1441757b3609daa58a4c` |
| [`terms/promises/free-below-load.descriptor`](terms/promises/free-below-load.descriptor) | free-below-load, free_while_scarcity_below | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:e3a9428148156c40ac7bbf8069d054e1177ae2d9b9367528ced037d58eca014e` |
| [`terms/promises/hibernated-grace.descriptor`](terms/promises/hibernated-grace.descriptor) | hibernated-grace, suspended_grace | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:dfa1da8c7762bd9609019641f679bf96e2a90f046ee217d37bd9b58c8a973de2` |
| [`terms/promises/launch-load.descriptor`](terms/promises/launch-load.descriptor) | launch-load, launch_scarcity | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b8dde838c0f4b9c90af0bbd8fb616c98cfc91fdcca316b4c9e9ff4f315096ec6` |
| [`terms/promises/new-client-credit.descriptor`](terms/promises/new-client-credit.descriptor) | new-client-credit, credit_mu_per_new_client | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:509d959c70450993a38022926e64fb2ad273d4a2fe6c0d9953d57ecb7d102ce8` |
| [`terms/promises/price-bound.descriptor`](terms/promises/price-bound.descriptor) | price-bound, price_bounded, price_volatility | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:638680bb4c49655e93dec1fd28bae77c1975c40f32b8448e97f6135afafab0f3` |
| [`terms/promises/scarcity-max.descriptor`](terms/promises/scarcity-max.descriptor) | scarcity-max, scarcity_max_multiplier | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:8eef8c73033b29b971c667ba741bb553533507bd05b35b2048e464c2d834fbc9` |
| [`terms/promises/statement-retention.descriptor`](terms/promises/statement-retention.descriptor) | statement-retention, retention_days | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:351ab928fc9775f3ac367921288a8bfe60b21e40718c3a7d9f4ac656833f0582` |
| [`terms/promises/stop-notice.descriptor`](terms/promises/stop-notice.descriptor) | stop-notice, stop_grace | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:46ae720fdcffb1ba426d102fc06cecb7d6678958d6d079b290dfbb10d10558bf` |

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
| `share` | the id of a network share | the filesystem setting `share` with `role=network`; id = sha256 of length-prefixed fields (`src/utils/shared_filesystems.py:397-433`) | The formal fixes the id that `ShareLocationRequest.share_id` carries. |

### `prices/node/`

All are keys of the old `Peer.rates` today (`src/utils/cost_functions/general_cost_functions.py:23-43`), with no
formal. The configuration is in MU for each hour, and the announcement for each second
(`hour // 3600`, so the announced figure is truncated).

| Name | Of / base | Today |
| --- | --- | --- |
| `connection-open` | one connection | New: listed before, but no code |

### `limits/`

nodo enforces these today with keys of its configuration, and announces none
(`Peer.limits` is never written). Write a descriptor only for a limit that a caller
must know.

| Name | Per | Today |
| --- | --- | --- |
| `reputation-pull`, `reputation-push` | one client, and all | `reputation.PULL_*`, `PUSH_*`, `MAX_OFFCHAIN_*_PER_RESPONSE` |

### `networks/`

| Name | Value | Today | Notes |
| --- | --- | --- | --- |
| (ledger with no addresses) | | tag `bitcoin` (`networks.py:149`) | It resolves to no peers: check if it is still necessary. |

### `dependency_sources/`

| Name | Value | Today |
| --- | --- | --- |
| `registry` | the location of a registry | New. Today the node gets a service from its registry, from `StartService`, from its peers (`GetService`) and from the source-application core service (`src/core_services/source_application.py`); none is a source of a dependency. |

### `protocols/address/`

The code builds these formals with `component_formal` (`src/identity/transport_stack.py`).
Copy the bytes into a file, then add `class=` and `name=` at stage 7 (they are on the
wire today).

| Name | Today | Notes |
| --- | --- | --- |
| `bee-rpc` | `transport_stack.py:266-351` | Its keys hold the schema of `buffer.Buffer`. |
| `celaut-gateway` | `transport_stack.py:418-720` | A wire schema (REASON 2.5): split each rule that two nodes must agree on (hash types, signature formats, receipts, proof of work, schemas) into a formal of its own. |
| `celaut-observe`, `celaut-chat` | Today Chat and Observe are RPCs inside the gateway layer | Each one is a branch of the tree with its own formal (REASON 2.8). |

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
| `ergo-mainnet` (reputation) | `ledger_descriptors.py:184-224` | The formal holds the R4 type ids of the configuration. Decide if these ids are a part of the ledger or values. |

### `signatures/schemes/`

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

### `proof_of_work/`

The proof of work of `ReputationProof.pow_nonce` (`src/reputation_system/offchain_proof.py:139-221`) has no
descriptor: its rule is a part of the gateway layer.
