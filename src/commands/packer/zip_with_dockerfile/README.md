# Packer — zip_with_dockerfile

> **Moved.** The full packing / service-configuration guide now lives at
> [`docs/PACKING.md`](../../../../docs/PACKING.md) so it is discoverable next to
> the rest of the Nodo documentation.
>
> It covers the project layout, `pack_config.json`, `service.json`, the
> `Dockerfile` rules (no `CMD` / `ENTRYPOINT` / `EXPOSE` — the entrypoint is read
> from `service.json → init.entry_path`), and the end-to-end preparation process.

This directory holds the `zip_with_dockerfile` packer implementation. For how to
author a service and what `nodo pack` expects as input, read
[`docs/PACKING.md`](../../../../docs/PACKING.md).
