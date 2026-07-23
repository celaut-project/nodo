# Nodo operations console

`nodo tui` opens a terminal operations console for a running nodo installation. It reads the
same `config.yaml`, SQLite database, registries, Cloud Hypervisor cgroups, logs, and wallet
status used by the node. Paths are resolved from `config.yaml`; they are not hard-coded to a
particular installation directory.

## Pages

| Page | Purpose |
|---|---|
| **Overview** | Node status/version/address, host CPU and RAM, current and reserved instance resources, disk usage, nodo storage size, peer/client counts, service count, reputation proof, and Ergo wallet balances. |
| **Instances** | Running instances with service, endpoint, virtualizer, current cgroup memory, configured RAM/disk limits, and gas. |
| **Services** | Locally available services, metadata tag, content ID, stored size, and execution action. |
| **Network** | Connected peers/endpoints/reputation and known clients. The obsolete tunnels page was removed. |
| **Config** | Every scalar or empty collection in `config.yaml`, including values inside lists. Values retain their YAML type when edited. |
| **Logs** | Tail of `storage/app.log` beside commands/actions launched from the TUI. |

Ergo information is refreshed asynchronously through `nodo info` every 60 seconds so JVM or
explorer latency cannot freeze the interface. Local database/system data refreshes every two
seconds; the recursive storage scan is limited to every 30 seconds.

## Controls

| Key | Action |
|---|---|
| `←` / `→` | Previous/next page |
| `↑` / `↓` | Select table row |
| `r` | Force a refresh |
| `Tab` | Switch peer/client focus on Network |
| `c` | Connect a peer from Network |
| `e` | Execute the selected service, or edit the selected Config value |
| `/` | Filter Config paths/values |
| `x` | Clear the Config filter |
| `Enter` / `Esc` | Save/cancel a modal |
| `Ctrl+U` | Clear modal input |
| `q` or `Ctrl+C` | Exit |

## Configuration editor

The Config page operates on the full YAML tree instead of a small hard-coded allowlist. For
example, list values appear as `core_services[1].id` and nested values as
`virtualizers.ch.MIN_MEM_MIB`.

- Input is parsed as YAML, so `true`, `5000`, `1.5`, `null`, `[]`, and quoted strings retain
  the expected type.
- The update is performed with nodo's configured `yq` binary, preserving comments and the
  rest of the file layout.
- Before every write, the previous file is copied to `config.yaml.tui.bak`.
- Paths containing `mnemonic`, `password`, `secret`, `private_key`, `token`, or `api_key` are
  masked in tables and modal input. Leaving a secret editor blank keeps the existing value;
  enter `""` explicitly to clear it.
- A saved value is immediately visible in the TUI, but a running nodo process may require a
  restart before it observes the change.

## Development

The protobuf compiler is vendored through `protoc-bin-vendored`; no system `protoc` is needed.

```bash
cd src/commands/tui
cargo test
cargo clippy --all-targets -- -D warnings
cargo run
```

Render tests cover every page at 80×24 and 140×40, and a dedicated regression test verifies
that plaintext secrets never appear in the terminal buffer.
