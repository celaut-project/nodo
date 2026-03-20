# Migración Hard Cutover de Protos CELAUT

Estado: **implementado en esquema protobuf** y adaptado en consumidores Python principales.

## Esquema actual

- `Contract`
  - `ledger = 1`
  - `xattrs = 2` (`script`, `address`, `token_id`, `reputation_key` opcional)
- `Service.Container`
  - `init` (`entry_path`, `xattrs`)
  - `config_declaration`
- `Service.Api.Slot`
  - `gas_amount_per_call`
- `Service.Network`
  - `protocol_stack`
- `Resources.start_time_ms` eliminado.

## Archivos sincronizados

- `protos/celaut.proto`
- `protos/pack.proto`
- `src/commands/tui/protos/celaut.proto` (idéntico al principal)

## Codegen

`bash/generate_protos.sh` ahora soporta dos modos:

1. `grpc_tools.protoc` (si está disponible)
2. `protoc` nativo (fallback para generar `*_pb2.py`)

## Notas operativas

- La migración es breaking: no hay fallback legacy.
- Los packers fallan explícitamente si reciben claves legacy (`entrypoint`, `config`, `resources.start_time_ms`).
