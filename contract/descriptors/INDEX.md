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
| [`architectures/linux-amd64.descriptor`](architectures/linux-amd64.descriptor) | linux-amd64, linux/amd64, amd64, x86_64 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:5f2d2c0b95cdb4eebf62d148fd8a77d883736c8faa4435c1383451a2d3317404` |
| [`architectures/linux-arm64-any-page.descriptor`](architectures/linux-arm64-any-page.descriptor) | linux-arm64-any-page, linux/arm64 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:8d075cc95254b4da5f6a6c44aecb8e7e89e7be442829e9290a65fd722d12af58` |
| [`architectures/linux-arm64.descriptor`](architectures/linux-arm64.descriptor) | linux-arm64, linux/arm64, arm64, aarch64 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:2eaac983dad6b7a25b15fa2df04586680c21aa583e5d82b9cce2122b6b812577` |
| [`concepts/armv8.0-a.descriptor`](concepts/armv8.0-a.descriptor) | armv8.0-a, aarch64-baseline | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:cc71d5ea3cf371762ecd300d4fd07d81f1b02c4759237179520f686579366784` |
| [`concepts/block-device.descriptor`](concepts/block-device.descriptor) | block-device, disk | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:7969ba1ce70da693124cc3523c3837bb5cbcc58bf2c4f0e21d72aad5070c8aca` |
| [`concepts/core.descriptor`](concepts/core.descriptor) | core, virtual cpu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:74b75cc69cfa93ec7b96b07316fee2be3fe54afb24972ecd74002523e374c29a` |
| [`concepts/linux-kernel-image-arm64.descriptor`](concepts/linux-kernel-image-arm64.descriptor) | linux-kernel-image-arm64, image | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:3e307510f99ae9b6edf94a3bd23feab531b4a2fd40cb2877bfc57f1c64fb3f7a` |
| [`concepts/linux-kernel-image-x86-64.descriptor`](concepts/linux-kernel-image-x86-64.descriptor) | linux-kernel-image-x86-64, bzimage | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:61f23d11250c67409e545c0873505891cd4f3c1dbe6ece3a16f697fc27015e64` |
| [`concepts/linux-syscalls-aarch64.descriptor`](concepts/linux-syscalls-aarch64.descriptor) | linux-syscalls-aarch64 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:083aba63310970df80c746d7e626cc7e9275fe5a880ae1e9e54da65a55f521b2` |
| [`concepts/linux-syscalls-x86-64.descriptor`](concepts/linux-syscalls-x86-64.descriptor) | linux-syscalls-x86-64 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:e194b9acc027487b1d1cb37ce86f0b5e9fb6c9ec7a74d0ed6d192b4811f68a3c` |
| [`concepts/pci-function-id.descriptor`](concepts/pci-function-id.descriptor) | pci-function-id, pci-id | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:59ebc8f20dd7197562e96627df82788110e19aad218542cf1da9cea46c2fb7b6` |
| [`concepts/ram.descriptor`](concepts/ram.descriptor) | ram, memory | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:ad835e4d9e4461e67c03111368b7dc30aea7eea0e100f00b0cffb04e80a60a36` |
| [`concepts/usb-device-id.descriptor`](concepts/usb-device-id.descriptor) | usb-device-id, usb-id | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:3748c8d4b9be5a2716c486e9608d310908ea6388e53b0f64bfbed7e3257ba2be` |
| [`concepts/utc.descriptor`](concepts/utc.descriptor) | utc, coordinated universal time | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:76016d6a59ad50c6aed0620ea7d772e9f3ec634a40cac7a94785b23768dae900` |
| [`concepts/utf-8.descriptor`](concepts/utf-8.descriptor) | utf-8 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:24801bc1176a41ff5f2aa2b94f8a3186eb42a69b5d68437e6e849c83bb443895` |
| [`concepts/virtual-machine.descriptor`](concepts/virtual-machine.descriptor) | virtual-machine, guest | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b3469e385a533a222a75a1aeafb1a94b2795a9f689e302f034283e1d8cdceb38` |
| [`concepts/x86-64-v1.descriptor`](concepts/x86-64-v1.descriptor) | x86-64-v1 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:bacad9da2c43fb7a1edf0a3bd79143d019a754e3992591d3353fc058b9a03ab7` |
| [`contracts/payment/bitcoin-p2wpkh.descriptor`](contracts/payment/bitcoin-p2wpkh.descriptor) | bitcoin-p2wpkh | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:5495a6f5fc350871eb78b8251ca0b45addf48d385d96176911ba5da6c07cc35e` |
| [`contracts/payment/ergo-p2pk.descriptor`](contracts/payment/ergo-p2pk.descriptor) | ergo-p2pk | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:0e8ccfee22f3d3494097be8757c5fea6e1ca299d304823e8f79c391181dd88a6` |
| [`dependency_sources/service-file.descriptor`](dependency_sources/service-file.descriptor) | service-file, on_filesystem | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:fa180ce8a2cd7766aab16b05d9c7b355225259b111ad42b52176ed3b07e6d1f7` |
| [`documents/arm-ddi0487a-k.descriptor`](documents/arm-ddi0487a-k.descriptor) | arm-ddi0487a-k | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:48262ad73a0a6a11a1b38f11c56558f2282a37ddab6a68cdca64025a01186996` |
| [`documents/busybox-1.37.0-linux-amd64.descriptor`](documents/busybox-1.37.0-linux-amd64.descriptor) | busybox-1.37.0-linux-amd64 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:d4206562a06c19d044c16876f9da490211093cfa1816959ea4ec2c77459e33c1` |
| [`documents/busybox-1.37.0-linux-arm64.descriptor`](documents/busybox-1.37.0-linux-arm64.descriptor) | busybox-1.37.0-linux-arm64 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:6e44a4c142a0a543cd5e75dc52649178c83a4048542d32c73cb9b240d81733d2` |
| [`documents/busybox-1.37.0-source.descriptor`](documents/busybox-1.37.0-source.descriptor) | busybox-1.37.0-source | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:acc624a0fc8771fb3195112fff9dcb741781d414ac3fcf823aaa244757198abc` |
| [`documents/ergo-6.0.8-openapi.descriptor`](documents/ergo-6.0.8-openapi.descriptor) | ergo-6.0.8-openapi | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:ddaadd465ce8ee9a86714931579fe4e1f74077c2283df294e77dc4cfda9d5612` |
| [`documents/grpc-status-codes.descriptor`](documents/grpc-status-codes.descriptor) | grpc-status-codes | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:601d79f95bb7809d28f01f0a66f82f77d00ed850faa1b02627b0ac7a6f398345` |
| [`documents/intel-sdm-325462-093.descriptor`](documents/intel-sdm-325462-093.descriptor) | intel-sdm-325462-093 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a2bfc03a6f3090a3a1e65f38738e79bffa8a814d580cd272371828853b929397` |
| [`documents/itu-r-tf-460-6.descriptor`](documents/itu-r-tf-460-6.descriptor) | itu-r-tf-460-6 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:0bc8dd8897206669265b2d791131aaea18096f21080a1a9a7e9a84e005652595` |
| [`documents/linux-6.12-arm64-booting.descriptor`](documents/linux-6.12-arm64-booting.descriptor) | linux-6.12-arm64-booting | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:831edc9280c0fac8dacb932b51db5c29107a6edaf0daa55abb195b55305a4b16` |
| [`documents/linux-6.12-arm64-elf-hwcaps.descriptor`](documents/linux-6.12-arm64-elf-hwcaps.descriptor) | linux-6.12-arm64-elf-hwcaps | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:ab5616fcbaaf062b98ae731c1b8811c024621663499614dcdf6e85fe05c97f54` |
| [`documents/linux-6.12-arm64-syscall-abis.descriptor`](documents/linux-6.12-arm64-syscall-abis.descriptor) | linux-6.12-arm64-syscall-abis | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:4257487cc31b94c834b2a0853520e43f14017904a93139d9794fae66aad6f13f` |
| [`documents/linux-6.12-devices.descriptor`](documents/linux-6.12-devices.descriptor) | linux-6.12-devices | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:94a1742f92c7a0c425a9ec8977b4edd0889d54c339f1cdc72e338e1cab54dbae` |
| [`documents/linux-6.12-initramfs-buffer-format.descriptor`](documents/linux-6.12-initramfs-buffer-format.descriptor) | linux-6.12-initramfs-buffer-format | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:150c1da53572ebdeac3d2c69cba82acb72681406a610704674d21c38a3afdbd8` |
| [`documents/linux-6.12-syscall-64-table.descriptor`](documents/linux-6.12-syscall-64-table.descriptor) | linux-6.12-syscall-64-table | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:da80dd0e7e4809297615e93a242849a073bc3b47fdcc404dd44223d0b8ecb044` |
| [`documents/linux-6.12-syscall-table.descriptor`](documents/linux-6.12-syscall-table.descriptor) | linux-6.12-syscall-table | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:5fa3539f5ba4aabbb4945bbebd6a8d19234e91e10804b7b63ee6a6998b180246` |
| [`documents/linux-6.12-x86-boot-protocol.descriptor`](documents/linux-6.12-x86-boot-protocol.descriptor) | linux-6.12-x86-boot-protocol | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:56abcf25ee655f7698ebda6a79e5dd507b98222f4463ec8920a7293243480fcf` |
| [`documents/man-pages-6.10.descriptor`](documents/man-pages-6.10.descriptor) | man-pages-6.10 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:0547755fe776075c828d11f43fd868af1338cc669c8f0b4ac8a72dddcea42698` |
| [`documents/posix-2024-base-definitions-chapter-3.descriptor`](documents/posix-2024-base-definitions-chapter-3.descriptor) | posix-2024-base-definitions-chapter-3 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:5dd925be58ed836308859cb1796d9006a7ff6b32eaa1281fd0f28c4604773ed9` |
| [`documents/rfc-1034.descriptor`](documents/rfc-1034.descriptor) | rfc-1034 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:f0baa0620a4d5e430122c810a80d1884c7beca0146ae836e0ce9212282439bfe` |
| [`documents/rfc-1035.descriptor`](documents/rfc-1035.descriptor) | rfc-1035 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:f17c0393548b0632774637f18e10e7a92084f5403915b61ad03648fa14f2e25c` |
| [`documents/rfc-20.descriptor`](documents/rfc-20.descriptor) | rfc-20 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:c9e88ed9883dd55c81bb8a9ba3cb7ba61ba4878f02bccf5dbb4c376e92e97e9a` |
| [`documents/rfc-3629.descriptor`](documents/rfc-3629.descriptor) | rfc-3629 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:7f15b4d98ed9a91162f221517844fc0d10e3edf5dc72a42c6f2172b91ffd6bc4` |
| [`documents/rfc-768.descriptor`](documents/rfc-768.descriptor) | rfc-768 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:083a6588f875a29386e5aedfe5dd111ff4288a7d520cca210f12d577815074b1` |
| [`documents/rfc-791.descriptor`](documents/rfc-791.descriptor) | rfc-791 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:5efb1730c69ebce4f33d2d940d1e3e03f2fe3910fc0a49739f8c478c7f550bb7` |
| [`documents/rfc-8259.descriptor`](documents/rfc-8259.descriptor) | rfc-8259 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:f09a6cffec194529f501fb332784d4dd49e50b1f06443691f01a92bcdb191594` |
| [`documents/rfc-9110.descriptor`](documents/rfc-9110.descriptor) | rfc-9110 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:9a549a66fe1f6d129e79b532abb623bb875349c85b88c6de515323709853fc9d` |
| [`documents/rfc-9112.descriptor`](documents/rfc-9112.descriptor) | rfc-9112 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:fd890170d531f2a81eef01cfce1cf1e69f61735aed6330f5c79532782e64afdf` |
| [`documents/rfc-9293.descriptor`](documents/rfc-9293.descriptor) | rfc-9293 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:f8a0a6609035df7f645e13152b1a325cfb13c31a7983d53a47b33dd827d8708c` |
| [`documents/usb-2.0-specification.descriptor`](documents/usb-2.0-specification.descriptor) | usb-2.0-specification | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:df7aa17857def18540ddc43303dd5a34d6fe02516cae9527f7735b55c145154a` |
| [`documents/virtio-1.3.descriptor`](documents/virtio-1.3.descriptor) | virtio-1.3 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:9d8cb1d5c69354bcad0748d3e7f16adb0fb533f70cd5db412e5134410c01beb5` |
| [`environment_formats/any.descriptor`](environment_formats/any.descriptor) | any, text | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:3cf8c071bb277d71aeab2eb8a50a30f8dc5abcbc75e53a6b8731356b0a310ec2` |
| [`facts/device-id.descriptor`](facts/device-id.descriptor) | device-id | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:39363aeedc67ec903b169ac2d0c5c11974a7eec39c49eb908032d806f331a46d` |
| [`facts/device-links.descriptor`](facts/device-links.descriptor) | device-links, nvlink | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:5ba935ab15e64df333ee2e9482c71a79bd922e2b3a37d7eb86d11aa68838ff56` |
| [`facts/device-models.descriptor`](facts/device-models.descriptor) | device-models | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:2b3dbd6cd94c023d70442b42b930ac5f86d86a286688844e1d5e65f5fcdccefa` |
| [`facts/device-place.descriptor`](facts/device-place.descriptor) | device-place, location | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:fa09821fd6bb99faab3fca9351a51b7f03ad9bd7f64793ee40cd687c533bc745` |
| [`facts/guest-kernel.descriptor`](facts/guest-kernel.descriptor) | guest-kernel, kernel | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:c81b8fc20335a5c4da6e30db7911eb22e0312c2ee1f1eaea8d11bc403d7889d2` |
| [`facts/pci-model.descriptor`](facts/pci-model.descriptor) | pci-model, pci | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:f77f111c3a3c9e36622b21845ab2e344fafb2b47f10ae9854b4d58649b751c28` |
| [`facts/usb-model.descriptor`](facts/usb-model.descriptor) | usb-model, usb | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b694a23c7e6ec4cb9f5678669a07835bf75753333db179f2ee8f518da62dfec3` |
| [`ledgers/bitcoin-mainnet.descriptor`](ledgers/bitcoin-mainnet.descriptor) | bitcoin-mainnet | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:42c3dae582a538b4e29e9c246d11a88dd1d4097995c66a2e9622517c163424ba` |
| [`ledgers/ergo-mainnet.descriptor`](ledgers/ergo-mainnet.descriptor) | ergo-mainnet | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:003ec055b8a277e480cb04389b4a5c54eb7bf4e4596ce89959515d1707d98b7f` |
| [`limits/calls.descriptor`](limits/calls.descriptor) | calls, rate limit | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:9ffdaa1ee1832f99c7ebd0cdd2d2fbe62fc69dc0ae71f6f02ea40183acdc93f8` |
| [`limits/chat-message-bytes.descriptor`](limits/chat-message-bytes.descriptor) | chat-message-bytes | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:02686ebd00a87e05830928cb9449c3c99201e5394a376c018e4def5c7f621d6c` |
| [`limits/chat-stored.descriptor`](limits/chat-stored.descriptor) | chat-stored | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:0435e5bb354a0536291faf1eef1c81a436726c2c071c0c86dbe09905bdb8333e` |
| [`limits/descendant-accounts.descriptor`](limits/descendant-accounts.descriptor) | descendant-accounts | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:0b4e3511f64b3e7cbd108288d68795745bad00c0d2440009ee2913ca49d89d79` |
| [`limits/descendant-depth.descriptor`](limits/descendant-depth.descriptor) | descendant-depth | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:7a90ef6ed4668e3817a21524e2e4a08d002d0de9c4ffd65ad4d61475e7c8cf0e` |
| [`limits/hibernated-disk.descriptor`](limits/hibernated-disk.descriptor) | hibernated-disk | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a167dd3763a1ff5a43d27a08b633aa6a813e6372268b895d6ee7dcae0956bebd` |
| [`limits/hibernated-instances.descriptor`](limits/hibernated-instances.descriptor) | hibernated-instances | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:50f25c5c57c35c319f9716a8d1129fe577bc6841a359f7eed061d8dac547ba77` |
| [`limits/network-bytes-per-day.descriptor`](limits/network-bytes-per-day.descriptor) | network-bytes-per-day | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:c45463b0f8ddfac60038cd16d5f5af85de652ce8000771dfdd396677c0b3408b` |
| [`limits/network-rate.descriptor`](limits/network-rate.descriptor) | network-rate | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:6605388849e23542b2bdf73e242867726082ef81a36cb3b40e6be3d683b9169d` |
| [`limits/recursion-hops.descriptor`](limits/recursion-hops.descriptor) | recursion-hops | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:6311094659448485673bdaae357c2648a48de03300fdcb9832cec3f70cd79817` |
| [`networks/all-addresses.descriptor`](networks/all-addresses.descriptor) | all-addresses, *, internet | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:8fcb26a7dc0e2b82f946d0e3a1914df0f117050e9ead7dffaebeed435feaf171` |
| [`networks/celaut-instances.descriptor`](networks/celaut-instances.descriptor) | celaut-instances | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a82825a2cd5e7acf69279d824ebaf835f408ecf442b39fdf35dcbda19519f3bf` |
| [`networks/host-name.descriptor`](networks/host-name.descriptor) | host-name, domain, dns | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a0e82fda97e6b1e7c9525d982c9222c19339f7290f1551d03fab8e8c3f1ca373` |
| [`networks/proof-of-work.descriptor`](networks/proof-of-work.descriptor) | proof-of-work, pow | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:8dc359b381f04d8d5a71bb62fbcd0dd3c7a80b4e3f988d6e01be354d5a852c05` |
| [`policies/api_slot/access.descriptor`](policies/api_slot/access.descriptor) | access | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:5344aac7355624c44adb896f24d835327efdb5fe6b0826a14a0b570b496cd37e` |
| [`policies/api_slot/tunnel_auth.descriptor`](policies/api_slot/tunnel_auth.descriptor) | tunnel_auth | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:0f8206ffef0314db0229f0cb367686a9631b8f85cfd62eb2b82932432df78043` |
| [`policies/api_slot/wake.descriptor`](policies/api_slot/wake.descriptor) | wake | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:2ff042caf1404c444e8aa8d48961e797cd061c176bee27fd33b7351c9a4eaa9a` |
| [`policies/configuration/device-selection.descriptor`](policies/configuration/device-selection.descriptor) | device-selection | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:ef6ab5ac71b880d0bc7d0f2ab5ea453ad174dcbe841124959d5b32ecf8a702a2` |
| [`policies/configuration/instance-name.descriptor`](policies/configuration/instance-name.descriptor) | instance-name | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:11f0fb78b8a075a7abe021b7774f8d24ecab45ce7bf9a2f6b05a53887087ec33` |
| [`policies/container_init/guest-kernel.descriptor`](policies/container_init/guest-kernel.descriptor) | guest-kernel, kernel | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:f7d296919847535b01e49905681583dfe9df33650e39d4cf9d09705bd4f3c794` |
| [`policies/container_init/notice.descriptor`](policies/container_init/notice.descriptor) | notice, stop notice, hibernate notice | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:f7bb973c68c1d1101fb628905ad68ed46453bf1c94c374628655cba7391bd3a3` |
| [`policies/device/attach.descriptor`](policies/device/attach.descriptor) | attach | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:75c8d0f3f707a640af0dde55be7300843afd674434b8a91b9eff9c58cf858252` |
| [`policies/device/bitstream.descriptor`](policies/device/bitstream.descriptor) | bitstream, fpga | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:42bacc8f8e93cd92c38acfb29bc0af56dd3165ef5458eeebad25e889569b0104` |
| [`policies/device/detach-notice.descriptor`](policies/device/detach-notice.descriptor) | detach-notice | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:716b1fafb6ea9b31a338604cd90a416bbd405a476d1ae741617e162e93bad40c` |
| [`policies/device/link.descriptor`](policies/device/link.descriptor) | link | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:213a5a64cfc9b85eb43b3979608feff1e5c6a9f3c395c891522a0049a27fb01d` |
| [`policies/filesystem_entry/share.descriptor`](policies/filesystem_entry/share.descriptor) | share | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:1801e283b3be7286cc3445a2e128c3d921f75bddf1825e330c7b7553c45b9de2` |
| [`policies/filesystem_root/entry_metadata.descriptor`](policies/filesystem_root/entry_metadata.descriptor) | entry_metadata | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:3c06eea2a8fe2d1476af508118965a67971d8e178f87caded980e6d78107eb42` |
| [`policies/filesystem_root/read_mode.descriptor`](policies/filesystem_root/read_mode.descriptor) | read_mode | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:e36826fd30068ee263c73edeb8f698c5c4761771801f707d1f6abb6642a503ec` |
| [`policies/network/template.descriptor`](policies/network/template.descriptor) | template | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:9f84cb60dcc2eb4f9b18dde8f426fd8bbe955076b32057d15b5e4864edb1451d` |
| [`prices/node/build.descriptor`](prices/node/build.descriptor) | build, build_mu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:1c87e45cc18f22209bce096e9fa1f3f2a4d42097c39dc3033494bad3d25c64eb` |
| [`prices/node/cpu-time.descriptor`](prices/node/cpu-time.descriptor) | cpu-time, cpu_mu_per_vcpu_hour | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:eb9086769856092d7cbd243857ff120a3b28dd57d11ff3f849e8c7e2d9ee350c` |
| [`prices/node/device-time.descriptor`](prices/node/device-time.descriptor) | device-time | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:da9c9a398e39eb2d35c14b7312b5022be106e612f5922cf4f7d2ef5ea2a595c7` |
| [`prices/node/disk-time.descriptor`](prices/node/disk-time.descriptor) | disk-time, disk_mu_per_gib_hour | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:60cb199f7ffd54ef6d426a84d29aa81057d532dabc83a545120cf149c7adc83c` |
| [`prices/node/kept-state.descriptor`](prices/node/kept-state.descriptor) | kept-state, hibernated disk | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:ac4aac70753958622f6102a64096ad0e1b6bcd7510547b30aca020ee3e966e9e` |
| [`prices/node/memory-time.descriptor`](prices/node/memory-time.descriptor) | memory-time, ram_mu_per_gib_hour | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:20884829bef3f16afcc94fd6053364f6f1fc412fead42444e93e05e56c954f44` |
| [`prices/node/modify-resources.descriptor`](prices/node/modify-resources.descriptor) | modify-resources, modify_resources_mu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:2ad7ac2b7c7e9edca157a6dac6ecae2fac0245e08697be40a054ab29ad0b3606` |
| [`prices/node/network-bytes.descriptor`](prices/node/network-bytes.descriptor) | network-bytes, net_mu_per_gib | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b33cf84a75cb195b769347a58745d53d20ac442ae2a311dfa3971274f669659f` |
| [`prices/node/statement-query.descriptor`](prices/node/statement-query.descriptor) | statement-query, statement_query_mu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:cab5714d8ac109135a43c40224e8091ec2042e15b932447a652955d1c1ad9c49` |
| [`prices/node/statement-row.descriptor`](prices/node/statement-row.descriptor) | statement-row, statement_row_mu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a4cbe4a4a3cb20d20b16fb2959552f012bbfcfe18384f5b3692ff95043eba66d` |
| [`prices/node/tunnel-open.descriptor`](prices/node/tunnel-open.descriptor) | tunnel-open, tunnel_open_mu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a36a4346db273c99fd7123fbf7b4dcfddd82e8a4aa07ad8471b44d325e9a4185` |
| [`prices/service/call.descriptor`](prices/service/call.descriptor) | call, mu_per_call | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:6171b269e450e140a8627da3afc941ca14ac55e9f14c4f6cb89b35e071528ec8` |
| [`proof_of_work/blake2b-trailing-zeros.descriptor`](proof_of_work/blake2b-trailing-zeros.descriptor) | blake2b-trailing-zeros, blake2b | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:c78b41fb96299d6f9b29754bec518d7d343ee1997e901e82590ec328a0706c56` |
| [`protocols/address/bee-rpc.descriptor`](protocols/address/bee-rpc.descriptor) | bee-rpc | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:98887b0edfb8fbc26c4e0ebdca0cf873d7a2e744700ac3939c1b05873c507057` |
| [`protocols/address/celaut-gateway.descriptor`](protocols/address/celaut-gateway.descriptor) | celaut-gateway | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:c805382e6dfbcb2642882c69e03d2249f08b8259616dd51bd5cb46504ce112cf` |
| [`protocols/address/ergo-p2p.descriptor`](protocols/address/ergo-p2p.descriptor) | ergo-p2p | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:685c1439fb2de07a90a07a7deb26afc3d8481b66df28f5adece70e3bb3347dfa` |
| [`protocols/address/ergo-rest.descriptor`](protocols/address/ergo-rest.descriptor) | ergo-rest | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:316a704bdbc9aeb78565e81b288ee5160814539577e45fa07388cb1d9e42c556` |
| [`protocols/address/grpc.descriptor`](protocols/address/grpc.descriptor) | grpc | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a318d9e22dfecbf06a11a00c08eb43b0a65ec886b50f0f5d83599bf891e0acf3` |
| [`protocols/address/http2.descriptor`](protocols/address/http2.descriptor) | http2 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:01575edd0b23255b9c1efa1727ba4fb635c150a14b4ab0576f739f87631c8acb` |
| [`protocols/address/tcp.descriptor`](protocols/address/tcp.descriptor) | tcp | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:db518cc9d8ba091449934c4a67fa56ae72c2823720e25355c092e25b823694ce` |
| [`protocols/address/tls.descriptor`](protocols/address/tls.descriptor) | tls | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:26ff6acdac9b95a18c334e65e676eb5c0fc1ba3c312d33c2bfafecc2c8aac5fe` |
| [`protocols/address/tor-onion-v3.descriptor`](protocols/address/tor-onion-v3.descriptor) | tor-onion-v3 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:81e06cee256629ffc232dc568d8b67da2ee1d207538c2054bd5257a5ec803484` |
| [`protocols/address/udp.descriptor`](protocols/address/udp.descriptor) | udp | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:03756f66a60aef245d8c5b7d78e4d7b9b8ed9e41369a06205b87e0cd29cea469` |
| [`quantities/benchmarks/float-ops.descriptor`](quantities/benchmarks/float-ops.descriptor) | float-ops, flt_ops_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:11e3b93b8daa9c2ef1be06eb62e03177ecabb23ae8793f0a4182a326cfa00ee6` |
| [`quantities/benchmarks/int-ops.descriptor`](quantities/benchmarks/int-ops.descriptor) | int-ops, int_ops_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:08a7086a7343bdfe4bfb04c52e9096288e0ac96b7c072db226359bf9a2ee8030` |
| [`quantities/benchmarks/memory-bandwidth-1gib.descriptor`](quantities/benchmarks/memory-bandwidth-1gib.descriptor) | memory-bandwidth-1gib, mem_bandwidth_1gib_bytes_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:66c56edaaf68bbc1cf9406345a401c71df44b7aa917d58e775a68417fbe35654` |
| [`quantities/benchmarks/memory-bandwidth-256mib.descriptor`](quantities/benchmarks/memory-bandwidth-256mib.descriptor) | memory-bandwidth-256mib, mem_bandwidth_256mib_bytes_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:6dd2b79d1bce02beea943968b519d0838540037150736e53bb8c6085e9e8de0f` |
| [`quantities/benchmarks/memory-bandwidth-64mib.descriptor`](quantities/benchmarks/memory-bandwidth-64mib.descriptor) | memory-bandwidth-64mib, mem_bandwidth_64mib_bytes_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:4c0ad7a446a955c7c7c1396e5aeb7fb767e03e67a57b44185bef88e7266be640` |
| [`quantities/benchmarks/sha256-hashes.descriptor`](quantities/benchmarks/sha256-hashes.descriptor) | sha256-hashes, sha256_hashes_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:1491fe5c7fd768db3f374f5ca65dc4a035c1406038d4d7fae4971f05130e6f0a` |
| [`quantities/resources/cpu.descriptor`](quantities/resources/cpu.descriptor) | cpu, vcpu, cores | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b40a7b0ac46be148fe6db636cde59c8a53bb5f172a97c6eaafd246b1b6e034fd` |
| [`quantities/resources/device-memory.descriptor`](quantities/resources/device-memory.descriptor) | device-memory, vram | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:ee9cad6566f696f7f73602862c74af5ae19c2c88e2ffa58c6030c0609100ad01` |
| [`quantities/resources/disk.descriptor`](quantities/resources/disk.descriptor) | disk, disk space, root filesystem | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:bd02506000ba59631e803c5346f375a4c5d1a2d61254fa3e7cec9383e3627e14` |
| [`quantities/resources/memory.descriptor`](quantities/resources/memory.descriptor) | memory, ram | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:93ff9c541ec5a949d10c752c3a12857edafd2acadfe636db698b18d0681a0263` |
| [`signatures/schemes/ed25519.descriptor`](signatures/schemes/ed25519.descriptor) | ed25519 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:77465d8964bef14c447c879b0b8b55a44de49cb18db46c42148d724f2c349f38` |
| [`terms/conditions/on-close.descriptor`](terms/conditions/on-close.descriptor) | on-close, on_close | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:674a7c80a76352e026d79a9c1a51ec2a066660a8b379194032def14b1175d0bf` |
| [`terms/conditions/scarcity-curve.descriptor`](terms/conditions/scarcity-curve.descriptor) | scarcity-curve, scarcity_curve | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:5cf9f042946915fa21b4f168096ef5327b29933161f88f77206567f12368aaf8` |
| [`terms/conditions/schedule.descriptor`](terms/conditions/schedule.descriptor) | schedule, activity window | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:117f31a03d17d90b74ccf6467452127cb44d235539c0dacd365720cc8a6246e5` |
| [`terms/promises/device-wipe.descriptor`](terms/promises/device-wipe.descriptor) | device-wipe | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:9e4de3477eab7c62295794b22d97b678c40a23d7d4fb1441757b3609daa58a4c` |
| [`terms/promises/free-below-load.descriptor`](terms/promises/free-below-load.descriptor) | free-below-load, free_while_scarcity_below | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:2e5c918c189f7da377c417a48b7e6df791c58caf1276bad2e92c12259e809256` |
| [`terms/promises/hibernated-grace.descriptor`](terms/promises/hibernated-grace.descriptor) | hibernated-grace, suspended_grace | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:dfa1da8c7762bd9609019641f679bf96e2a90f046ee217d37bd9b58c8a973de2` |
| [`terms/promises/launch-load.descriptor`](terms/promises/launch-load.descriptor) | launch-load, launch_scarcity | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:1450751a2c4ef5111faf471b299259cd5868645b54f668220deb6c822192305a` |
| [`terms/promises/new-client-credit.descriptor`](terms/promises/new-client-credit.descriptor) | new-client-credit, credit_mu_per_new_client | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:509d959c70450993a38022926e64fb2ad273d4a2fe6c0d9953d57ecb7d102ce8` |
| [`terms/promises/price-bound.descriptor`](terms/promises/price-bound.descriptor) | price-bound, price_bounded, price_volatility | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a6425604541c20d33af9733ec63919d559d4877561e289d787b4dfd040614701` |
| [`terms/promises/scarcity-max.descriptor`](terms/promises/scarcity-max.descriptor) | scarcity-max, scarcity_max_multiplier | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:6696f94db21d675fbfe2ba3b321265fa1a326d6426aabc234361381cefab7e15` |
| [`terms/promises/statement-retention.descriptor`](terms/promises/statement-retention.descriptor) | statement-retention, retention_days | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:351ab928fc9775f3ac367921288a8bfe60b21e40718c3a7d9f4ac656833f0582` |
| [`terms/promises/stop-notice.descriptor`](terms/promises/stop-notice.descriptor) | stop-notice, stop_grace | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:322073777ed89f4ef64c505b9401720bb76ad10754dd900a2855d193488d36b0` |

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
| a family of GPUs, one for each compute capability | New. PR #649 (not merged) uses tags `pci:<vendor>:<device>+<alg>:<hash>`. `GetResourceAvailability` refuses every device (`src/gateway/iterables/resource_availability_iterable.py:52-66`). | One descriptor for each member of the family (for example one for each CUDA compute capability, or each ROCm `gfx` target). PR #649 writes the families that it can attach and test. |

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

### `policies/filesystem_entry/`

| Name | Value | Today | Notes |
| --- | --- | --- | --- |
| `share-v2` | as `share`, but with `role=network` the key `network` gives the position of the network in `Service.network` | `share` names the network by its tag, and its id is sha256 of length-prefixed fields with the tag (`src/utils/shared_filesystems.py:397-433`) | It replaces `share` (`replaces=`). The formal fixes the id that `ShareLocationRequest.share_id` carries: `<type>:<digest>` of a hash whose type the formal gives (rule 3.9), over the length-prefixed fields "network", the formal of the domain, the value of the domain after the settings of the network, the name, the env and its value. The id does not use the position: two services put one network in two positions. The setting stays on the directory. With the code, at stage 7. |

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
| `reputation-pull-calls`, `reputation-pull-bytes`, `reputation-push-calls`, `reputation-push-bytes` (one client); `reputation-pull-calls-all`, `reputation-push-calls-all` (all); `reputation-proofs-per-answer` | in the formal, as in `calls` | `reputation.PULL_*`, `PUSH_*`, `MAX_OFFCHAIN_*_PER_RESPONSE`. Follow `calls`: the formal gives what is counted and `per`; the scope is the window in seconds (empty for `reputation-proofs-per-answer`). Low priority. |

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
| the split of `celaut-gateway` | The file `celaut-gateway.descriptor` is the formal that nodo sends today (`transport_stack.py:418-720`). | A wire schema (REASON 2.5): split each rule that two nodes must agree on (hash types, signature formats, receipts, proof of work, schemas) into a formal of its own. It depends on the format of a schema. |
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
