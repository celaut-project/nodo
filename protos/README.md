# Plan de Implementación de Migración de Protos (Ordenado)

## 1. Objetivo
Migrar el repositorio al modelo protobuf elemental y consistente, con **hard cutover** (sin compatibilidad hacia atrás), manteniendo sincronizados:

- `protos/celaut.proto`
- `protos/pack.proto`
- `src/commands/tui/protos/celaut.proto`
- código Python/Rust consumidor de estos contratos

## 2. Decisiones Cerradas
- La migración es **breaking**.
- Estrategia: **hard cutover**.
  - Lectura: solo esquema nuevo.
  - Escritura: solo esquema nuevo.
- `ConfigDeclaration` vive dentro de `Container` (`container.config_declaration`).
- `Service.Api.Slot` reemplaza `xattrs` por:
  - `map<string, GasAmount> gas_amount_per_call = 3`
  - Cada key representa un método declarado en `protocol_stack`.
- `Contract` se simplifica a:
  - `Ledger ledger = 1`
  - `map<string, bytes> xattrs = 2`
- Convención canónica para `Contract.xattrs` (snake_case plano):
  - `script`, `token_id`, `address`, `reputation_key`
- `Resources.start_time_ms` queda eliminado.
- Las pruebas en vivo (arranque, ejecución, regresión end-to-end) quedan fuera de este plan y las ejecuta el usuario.

## 3. Modelo Objetivo (Resumen)

```proto
message Contract {
    message Ledger {
        repeated string tags = 1;
        string prose = 2;
        bytes formal = 3;
    }

    Ledger ledger = 1;
    // (script, token_id, dirección, clave de reputación, etc.)
    map<string, bytes> xattrs = 2;
}

message Service {
    string prose = 1;
    Container container = 2;
    Api api = 3;
    repeated Network network = 4;
}

message Container {
    // ...
    ConfigDeclaration config_declaration = 6;

    message ConfigDeclaration {
        repeated string path = 1;
        DataFormat format = 2;
    }
}

message Service.Api.Slot {
    int32 port = 1;
    repeated Protocol protocol_stack = 2;
    map<string, GasAmount> gas_amount_per_call = 3;
}
```

## 4. Fase 1: Contrato Protobuf (Bloqueante)

### 4.1 Cambios en `celaut.proto`
- `Service.Container`
  - Reemplazar `entrypoint` por `Init`.
  - Mover `ConfigDeclaration` dentro de `Container`.
  - Exponer `container.config_declaration`.
- `Service.Container.Init`
  - `repeated string entry_path = 1`
  - `map<string, bytes> xattrs = 2`
- `Service.Container.Filesystem.ItemBranch`
  - Añadir `map<string, bytes> xattrs`.
- `Service.Network`
  - Renombrar `client_protocol_stack` -> `protocol_stack`.
- `Service.Api.Slot`
  - `map<string, GasAmount> gas_amount_per_call = 3`.
- `Contract`
  - Eliminar `ScriptTemplate`, `template`, `script`, `token_id`.
  - Mantener solo `ledger` + `xattrs`.
- `Resources`
  - Eliminar `start_time_ms`.

### 4.2 Cambios en `pack.proto`
- Sincronizar semántica con `celaut.Service`:
  - `init`
  - `container.config_declaration`
  - `architecture`
  - `network.protocol_stack`
  - `api.slot.gas_amount_per_call`

### 4.3 Copia TUI
- Mantener `src/commands/tui/protos/celaut.proto` idéntico al proto principal.

### 4.4 Numeración y reservas
- Mantener índices ordenados y compactos, reutilizando índices de campos eliminados cuando aplique.
- No introducir `reserved` por defecto; usarlo solo si aparece un riesgo concreto de ambigüedad en wire-format.

## 5. Fase 2: Codegen y Build
- Regenerar artefactos:
  - `protos/celaut_pb2.py`
  - `protos/celaut_pb2_grpc.py`
  - `protos/pack_pb2.py`
- Validar `bash/generate_protos.sh`.
- Recompilar TUI (prost/build.rs) tras sincronizar protos.

## 6. Fase 3: Adaptación de Código por Subsistema

### 6.1 Packers
Archivos principales:
- `src/packers/zip_with_dockerfile.py`
- `src/packers/zip_with_dockerfile_fractal.py`
- `src/packers/README.md`

Cambios:
- Emitir `container.init.entry_path` y `container.init.xattrs`.
- Emitir `container.config_declaration`.
- Emitir/leer `api.slot[].gas_amount_per_call`.
- Mantener orden determinista de filesystem (`branch` ordenado).
- Capturar metadatos POSIX en `xattrs`.

### 6.2 Runtimes (Docker / Cloud Hypervisor)
Docker:
- `src/virtualizers/docker/execute.py`
- `src/virtualizers/docker/set_container_config.py`
- `src/virtualizers/docker/build.py`

Cloud Hypervisor:
- `src/virtualizers/cloud_hypervisor/build.py`
- `src/virtualizers/cloud_hypervisor/execute.py`
- `bash/build_ch_initramfs.sh`

Cambios comunes:
- Usar `container.init.entry_path` en lugar de `entrypoint`.
- Inyectar config desde `container.config_declaration`.
- Materializar/aplicar `xattrs` del filesystem.
- Actualizar metadata de arranque a payload de `init`.

### 6.3 Manager / Red / CLI
- `src/manager/networks.py`
  - `network.client_protocol_stack` -> `network.protocol_stack`.
- `src/commands/inspect.py`
  - Mostrar `container.init` y `container.config_declaration`.
  - Mostrar `container.architecture`.
  - Mostrar contratos usando `contract.ledger` + `contract.xattrs`.

### 6.4 Pago, reputación y base de datos (impacto de `Contract.xattrs`)
Áreas afectadas:
- `src/payment_system/*`
- `src/reputation_system/*`
- `src/gateway/gateway.py`
- `src/database/sql_connection.py`

Cambios requeridos:
- Reemplazar lecturas/escrituras de:
  - `contract.template`
  - `contract.script`
  - `contract.token_id`
  por `contract.xattrs[...]`.
- Normalizar codificación de valores textuales en `xattrs` a UTF-8 en bytes.
- Ajustar hashing/lookup de contratos para que use el nuevo shape del `Contract`.
- Actualizar persistencia de instancias de contrato en DB para dejar de depender de campos eliminados.

## 7. Fase 4: Datos y Migración Operativa
- Invalidar payloads legacy al desplegar.
- Ejecutar migración de datos persistidos si existen contratos antiguos en DB.
- Verificar que no haya fallback silencioso desde formato viejo.

## 8. Pruebas

### 8.1 Unit tests
Actualizar y/o añadir pruebas para:
- Validación de `init` en lugar de `entrypoint`.
- Uso de `container.config_declaration`.
- `network.protocol_stack`.
- `api.slot.gas_amount_per_call`.
- `Contract` con `ledger + xattrs`.

### 8.2 Pruebas en vivo (fuera de alcance de este plan)
- Arranque, ejecución y regresión end-to-end las ejecuta el usuario.
- Este plan se limita a dejar listas las validaciones unitarias y la migración de código.

## 9. Secuencia Recomendada de Ejecución
1. Actualizar protos (`celaut.proto`, `pack.proto`, copia TUI).
2. Regenerar stubs y recompilar TUI.
3. Adaptar packers.
4. Adaptar runtimes.
5. Adaptar manager/CLI/red.
6. Adaptar pago/reputación/DB por `Contract.xattrs`.
7. Actualizar tests.
8. Entregar para validación en vivo por el usuario y desplegar hard cutover.

## 10. Checklist de Cierre
- Protos principal/pack/TUI sincronizados.
- Sin referencias legacy en código.
- Sin fallback legacy activo.
- Unit tests actualizados en verde.
- Pruebas en vivo validadas por el usuario.
- Documentación actualizada y consistente con el esquema final.
