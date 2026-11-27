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
| [`architectures/linux-amd64.descriptor`](architectures/linux-amd64.descriptor) | linux-amd64, linux/amd64, amd64, x86_64 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b6738d8de8558a50fc57a62c110dc339712c6331e92628db26803d20b7d2eefb` |
| [`architectures/linux-arm64-any-page.descriptor`](architectures/linux-arm64-any-page.descriptor) | linux-arm64-any-page, linux/arm64 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b384b3d121a8fa71e7a73d3c2bf50ab42a8948b6833a14130fc0bb8f1fb269a4` |
| [`architectures/linux-arm64.descriptor`](architectures/linux-arm64.descriptor) | linux-arm64, linux/arm64, arm64, aarch64 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:08b14c6766633d1400cdcc602126b59955ea7e229274a37dd6e7f9136dc5bbc7` |
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
| [`dependency_sources/service-file.descriptor`](dependency_sources/service-file.descriptor) | service-file, on_filesystem | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b941f5b9e824d666cf2a3b50aa25139bfb8ba108796e8cde0f2b4d4a60cb6c0a` |
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
| [`environment_formats/any.descriptor`](environment_formats/any.descriptor) | any, text | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:839fdff7feb387de47e5a5f6457da58dc0c882690f0f9f2d8ce77211aa40ef21` |
| [`facts/device-id.descriptor`](facts/device-id.descriptor) | device-id | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:6ce6c432403ebc0b978b7a38df1f7bc3b89229a42af71656dbfb8c7b26a70617` |
| [`facts/device-links.descriptor`](facts/device-links.descriptor) | device-links, nvlink | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:513329b19e532424e1702f2197e1bb43f94e230165db90b209e47c01a64f821d` |
| [`facts/device-models.descriptor`](facts/device-models.descriptor) | device-models | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:2b3dbd6cd94c023d70442b42b930ac5f86d86a286688844e1d5e65f5fcdccefa` |
| [`facts/device-place.descriptor`](facts/device-place.descriptor) | device-place, location | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:10026c1cb62e5e15d214e6efee25adbede6a0c4cdd7d0ffea3e8ee5a0ccdeb73` |
| [`facts/guest-kernel.descriptor`](facts/guest-kernel.descriptor) | guest-kernel, kernel | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:7c35a53bbc7fc46d07eebb7a5f82638c509dd90309cb30fe41e5368c52712376` |
| [`facts/pci-model.descriptor`](facts/pci-model.descriptor) | pci-model, pci | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:f77f111c3a3c9e36622b21845ab2e344fafb2b47f10ae9854b4d58649b751c28` |
| [`facts/usb-model.descriptor`](facts/usb-model.descriptor) | usb-model, usb | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b694a23c7e6ec4cb9f5678669a07835bf75753333db179f2ee8f518da62dfec3` |
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
| [`policies/container_init/guest-kernel.descriptor`](policies/container_init/guest-kernel.descriptor) | guest-kernel, kernel | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:68a47f3f2dd263abcf8c26232666aa913f308276005bcb842ccdce3c4f030390` |
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
| [`prices/node/cpu-time.descriptor`](prices/node/cpu-time.descriptor) | cpu-time, cpu_mu_per_vcpu_hour | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:d1a3203041517b71bcf7b562276a6a77eb3a5d2b8d19c5adb632dfcddfb58b7b` |
| [`prices/node/device-time.descriptor`](prices/node/device-time.descriptor) | device-time | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:12b7a755c0b492cf854eedd03fd532541067f8c5922e96ab3c1bdc8172cd3463` |
| [`prices/node/disk-time.descriptor`](prices/node/disk-time.descriptor) | disk-time, disk_mu_per_gib_hour | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a734a5b08ae158edf4c4efe45b68c3c561bd4890562a4557f4157674f6d40178` |
| [`prices/node/kept-state.descriptor`](prices/node/kept-state.descriptor) | kept-state, hibernated disk | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a68b49582a557e26c08ee2c571d1afd0425674139ba132f416e41a53e7c02ad5` |
| [`prices/node/memory-time.descriptor`](prices/node/memory-time.descriptor) | memory-time, ram_mu_per_gib_hour | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:c3f4993065e18ab1030e6a10f644f3413dd74091e7563b3b87087d7b3b36cdf8` |
| [`prices/node/modify-resources.descriptor`](prices/node/modify-resources.descriptor) | modify-resources, modify_resources_mu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:2ad7ac2b7c7e9edca157a6dac6ecae2fac0245e08697be40a054ab29ad0b3606` |
| [`prices/node/network-bytes.descriptor`](prices/node/network-bytes.descriptor) | network-bytes, net_mu_per_gib | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b33cf84a75cb195b769347a58745d53d20ac442ae2a311dfa3971274f669659f` |
| [`prices/node/statement-query.descriptor`](prices/node/statement-query.descriptor) | statement-query, statement_query_mu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:cab5714d8ac109135a43c40224e8091ec2042e15b932447a652955d1c1ad9c49` |
| [`prices/node/statement-row.descriptor`](prices/node/statement-row.descriptor) | statement-row, statement_row_mu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a4cbe4a4a3cb20d20b16fb2959552f012bbfcfe18384f5b3692ff95043eba66d` |
| [`prices/node/tunnel-open.descriptor`](prices/node/tunnel-open.descriptor) | tunnel-open, tunnel_open_mu | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a36a4346db273c99fd7123fbf7b4dcfddd82e8a4aa07ad8471b44d325e9a4185` |
| [`prices/service/call.descriptor`](prices/service/call.descriptor) | call, mu_per_call | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b9000d73c97bcb624709b4622dc70cf9289af193f83159705e013708152f229f` |
| [`proof_of_work/blake2b-trailing-zeros.descriptor`](proof_of_work/blake2b-trailing-zeros.descriptor) | blake2b-trailing-zeros, blake2b | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:ddadba5e7a58319c90723b600b33fff948b6c3ffd1984aab868024c0e1879632` |
| [`protocols/address/bee-rpc.descriptor`](protocols/address/bee-rpc.descriptor) | bee-rpc | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:98887b0edfb8fbc26c4e0ebdca0cf873d7a2e744700ac3939c1b05873c507057` |
| [`protocols/address/celaut-gateway.descriptor`](protocols/address/celaut-gateway.descriptor) | celaut-gateway | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:c805382e6dfbcb2642882c69e03d2249f08b8259616dd51bd5cb46504ce112cf` |
| [`protocols/address/ergo-p2p.descriptor`](protocols/address/ergo-p2p.descriptor) | ergo-p2p | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:685c1439fb2de07a90a07a7deb26afc3d8481b66df28f5adece70e3bb3347dfa` |
| [`protocols/address/ergo-rest.descriptor`](protocols/address/ergo-rest.descriptor) | ergo-rest | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:cba94914a638667c02a40344607fdcd203fd16c3148aad5c30429bd3e8d316e5` |
| [`protocols/address/grpc.descriptor`](protocols/address/grpc.descriptor) | grpc | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a318d9e22dfecbf06a11a00c08eb43b0a65ec886b50f0f5d83599bf891e0acf3` |
| [`protocols/address/http2.descriptor`](protocols/address/http2.descriptor) | http2 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:01575edd0b23255b9c1efa1727ba4fb635c150a14b4ab0576f739f87631c8acb` |
| [`protocols/address/tcp.descriptor`](protocols/address/tcp.descriptor) | tcp | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:db518cc9d8ba091449934c4a67fa56ae72c2823720e25355c092e25b823694ce` |
| [`protocols/address/tls.descriptor`](protocols/address/tls.descriptor) | tls | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:26ff6acdac9b95a18c334e65e676eb5c0fc1ba3c312d33c2bfafecc2c8aac5fe` |
| [`protocols/address/tor-onion-v3.descriptor`](protocols/address/tor-onion-v3.descriptor) | tor-onion-v3 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:81e06cee256629ffc232dc568d8b67da2ee1d207538c2054bd5257a5ec803484` |
| [`protocols/address/udp.descriptor`](protocols/address/udp.descriptor) | udp | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:296c06ccd189ac66e62d6c53ef0648b49ec3c86153fb60870d326cf04f5e4584` |
| [`quantities/benchmarks/float-ops.descriptor`](quantities/benchmarks/float-ops.descriptor) | float-ops, flt_ops_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:dbd7b4d1a356561cf95443b9ccb853f7ff91c64dbf2a3b0ff018522ae855996b` |
| [`quantities/benchmarks/int-ops.descriptor`](quantities/benchmarks/int-ops.descriptor) | int-ops, int_ops_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:2739f2cc9d954ee58e28c2c1bf1cf1b7ffa992e021b81714f007ffecc0e0e37c` |
| [`quantities/benchmarks/memory-bandwidth-1gib.descriptor`](quantities/benchmarks/memory-bandwidth-1gib.descriptor) | memory-bandwidth-1gib, mem_bandwidth_1gib_bytes_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:ac1520c46d26b19d3f401ceedc8dd6142cd57613c1e6de643c28ef0562762701` |
| [`quantities/benchmarks/memory-bandwidth-256mib.descriptor`](quantities/benchmarks/memory-bandwidth-256mib.descriptor) | memory-bandwidth-256mib, mem_bandwidth_256mib_bytes_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a32e64f812356332ba8e8449ba075d476089760bfc4e9d634df5dcc45dea5394` |
| [`quantities/benchmarks/memory-bandwidth-64mib.descriptor`](quantities/benchmarks/memory-bandwidth-64mib.descriptor) | memory-bandwidth-64mib, mem_bandwidth_64mib_bytes_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:055df939cbc03a38dc7951f035adf564f3e7d36cf36478b35a24c2ed6e488613` |
| [`quantities/benchmarks/sha256-hashes.descriptor`](quantities/benchmarks/sha256-hashes.descriptor) | sha256-hashes, sha256_hashes_per_sec | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:17e6e95ebecbb33b4da84432296860a113c4eb6ad8a756c9419ac8325529b510` |
| [`quantities/resources/cpu.descriptor`](quantities/resources/cpu.descriptor) | cpu, vcpu, cores | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:b40a7b0ac46be148fe6db636cde59c8a53bb5f172a97c6eaafd246b1b6e034fd` |
| [`quantities/resources/device-memory.descriptor`](quantities/resources/device-memory.descriptor) | device-memory, vram | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:ee9cad6566f696f7f73602862c74af5ae19c2c88e2ffa58c6030c0609100ad01` |
| [`quantities/resources/disk.descriptor`](quantities/resources/disk.descriptor) | disk, disk space, root filesystem | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:bd02506000ba59631e803c5346f375a4c5d1a2d61254fa3e7cec9383e3627e14` |
| [`quantities/resources/memory.descriptor`](quantities/resources/memory.descriptor) | memory, ram | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:93ff9c541ec5a949d10c752c3a12857edafd2acadfe636db698b18d0681a0263` |
| [`signatures/schemes/ed25519.descriptor`](signatures/schemes/ed25519.descriptor) | ed25519 | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:77465d8964bef14c447c879b0b8b55a44de49cb18db46c42148d724f2c349f38` |
| [`terms/conditions/on-close.descriptor`](terms/conditions/on-close.descriptor) | on-close, on_close | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:93bcef2d856121a56ed3573a77b498162c692e371d99bc84df1f0b917e6552e1` |
| [`terms/conditions/scarcity-curve.descriptor`](terms/conditions/scarcity-curve.descriptor) | scarcity-curve, scarcity_curve | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:2f8a2dc806b8dfe8247d0684c009c40693968c724acc93d9cf32c65113aed491` |
| [`terms/conditions/schedule.descriptor`](terms/conditions/schedule.descriptor) | schedule, activity window | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:59fdea41af046224bef39a2059df76f3779e44ba74aab00a623a3aea347207e1` |
| [`terms/promises/device-wipe.descriptor`](terms/promises/device-wipe.descriptor) | device-wipe | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:9e4de3477eab7c62295794b22d97b678c40a23d7d4fb1441757b3609daa58a4c` |
| [`terms/promises/free-below-load.descriptor`](terms/promises/free-below-load.descriptor) | free-below-load, free_while_scarcity_below | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:2e5c918c189f7da377c417a48b7e6df791c58caf1276bad2e92c12259e809256` |
| [`terms/promises/hibernated-grace.descriptor`](terms/promises/hibernated-grace.descriptor) | hibernated-grace, suspended_grace | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:dfa1da8c7762bd9609019641f679bf96e2a90f046ee217d37bd9b58c8a973de2` |
| [`terms/promises/launch-load.descriptor`](terms/promises/launch-load.descriptor) | launch-load, launch_scarcity | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:1450751a2c4ef5111faf471b299259cd5868645b54f668220deb6c822192305a` |
| [`terms/promises/new-client-credit.descriptor`](terms/promises/new-client-credit.descriptor) | new-client-credit, credit_mu_per_new_client | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:509d959c70450993a38022926e64fb2ad273d4a2fe6c0d9953d57ecb7d102ce8` |
| [`terms/promises/price-bound.descriptor`](terms/promises/price-bound.descriptor) | price-bound, price_bounded, price_volatility | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:a6425604541c20d33af9733ec63919d559d4877561e289d787b4dfd040614701` |
| [`terms/promises/scarcity-max.descriptor`](terms/promises/scarcity-max.descriptor) | scarcity-max, scarcity_max_multiplier | `0e5751c026e543b2e8ab2eb06099daa1d1e5df47778f7787faab45cdf12fe3a8:6696f94db21d675fbfe2ba3b321265fa1a326d6426aabc234361381cefab7e15` |
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
