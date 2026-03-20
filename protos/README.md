```proto
// ====================================================================
// ESPECIFICACIÓN FORMAL DE SERVICE
// Versión elemental, orgánica y atemporal (cumple estrictamente los 4 mantras)
// ====================================================================

// ======================
// EXPLICACIÓN DE TÉRMINOS CLAVE
// (para que todos los desarrolladores y futuros lectores entiendan el lenguaje orgánico)
// ======================
/*
- Organismo: el Service completo. Es una entidad viva indivisible. (en el sentido de que es una unidad de información coherente, autónoma y expresable)
- Genoma: la parte que viaja 100% con el Service (filesystem + init + formas). Define todo su comportamiento observable.
- Sustrato: el host/nodo que ejecuta el Service. Solo recibe declaraciones (nunca se incorpora dentro del organismo).
- Nombre: la ruta en el árbol (ItemBranch.name).
- Forma: los atributos indivisibles del objeto (hoy solo xattrs; mañana lo que sea).
- Esencia: el contenido real (bytes) o referencia (link).
*/

// ======================
// LAS 4 REGLAS DE ORO (Mantras) — NO NEGOCIABLES
// ======================
/*
1. Mantra Orgánico
   El Service es un organismo vivo indivisible.
   Todo se reduce a: Nombre + Forma + Esencia.
   Cualquier cosa que afecte al comportamiento observable debe viajar con él.

2. Mantra Elemental (Aristotélica)
   No inventamos nada. Materializamos solo lo que ya existe en cualquier estructura jerárquica de objetos.
   La única vía de evolución futura es map<string, bytes> xattrs y tags-prose-formal.

3. Mantra de Separación Genoma / Sustrato
   Genoma → viaja dentro del Service (filesystem + form + init).
   Sustrato → solo se declara (nunca se incorpora).

4. Mantra Anti-Consenso
   El esquema debe estar a prueba de rotura de convenciones.
   Nunca se usan "cadenas mágicas" ni nombres que requieran consenso global.
   Única excepción permitida: patrón tags-prose-formal (o equivalente) cuando NO se puede materializar lo elemental.
*/

// ======================
// DEFINICIÓN DEL HASH INMUTABLE DEL SERVICE
// (en base al Mantra 4)
// ======================
/*
El hash inmutable se calcula como:
    H( serialized_canonical(Service) )
donde H es la función hash identificada de forma anti-consenso como:
      "la función hash tal que H(H(bytes vacíos)) = <digest canónico fijado en esta versión de la especificación>"
No se nombra nunca el algoritmo. Cada nodo verifica en runtime que su función satisface exactamente esa ecuación. Así nunca depende de convenciones de nombres.
*/

// ======================
// MENSAJES (anidados según dependencias reales + razonamiento orgánico de cada componente)
// ======================
message Service {
    // prose → Explicación en lenguaje natural del organismo (Mantra 1). Elemental y atemporal.
    string prose = 1;

    // El organismo completo (genoma).
    Container container = 2;

    // Declaración de API (cómo el mundo externo puede hablar con el organismo).
    Api api = 3;

    // Declaración de ámbitos externos requeridos (aislamiento por defecto).
    repeated Network network = 4;

    // Declaración de configuración inicial que el host debe inyectar (sustrato puro).
    ConfigDeclaration config_declaration = 5;
}

message Container {
    // Arquitectura del organismo (no materializable sin consenso → tags-prose-formal).
    Architecture architecture = 1;

    // Serialized rootfs completo (el árbol con Nombre + Forma + Esencia).
    bytes filesystem = 2;

    // Punto de activación inequívoco (ver razonamiento en Init).
    Init init = 3;

    // Requisitos de recursos (elementales, no necesitan tags-prose-formal).
    optional Resources resources = 4;

    // Protocol stack interno del nodo.
    repeated Api.Protocol node_protocol_stack = 5;

    // Carta de requisitos al sustrato (solo declaración).
    KernelInterface kernel_interface = 6;

    // =============================================================
    message Architecture {
        // Razonamiento: nunca podemos materializar "x86_64" o "wasm" sin romper Mantra 4.
        repeated string tags = 1;
        string prose = 2;
        bytes formal = 3; // descriptor binario real del genoma
    }

    message Init {
        // ¿Realmente lo más elemental es tener argv y working_directory?
        //
        // Respuesta elemental (Mantra 2): SÍ.
        //
        // Razones profundas:
        // 1. Un solo "path a ejecutable" sería demasiado restrictivo. En cualquier sistema (hoy o en 1000 años)
        //    la activación de un organismo casi siempre necesita parámetros (ej: /bin/sh -c "script", /app/server --port 8080).
        //    argv (lista de strings) es la forma más abstracta y universal de expresar eso.
        // 2. working_directory (cwd) es parte indivisible de la activación: muchos organismos usan rutas relativas
        //    dentro de su propio filesystem. Sin cwd explícito, el comportamiento dejaría de ser reproducible.
        // 3. Un solo path obligaría a meter args y cwd en xattrs (menos limpio y menos elemental).
        // 4. argv[0] + cwd + xattrs es la mínima tríada que cubre TODOS los casos conocidos y futuros sin asumir OS.
        //
        // Conclusión: argv + working_directory es MÁS elemental que un simple path.
        repeated string argv = 1;               // argv[0] debe existir dentro del filesystem
        optional string working_directory = 2;  // default = "/"
        map<string, bytes> xattrs = 3;          // cualquier parámetro extra futuro
    }

    message KernelInterface {
        // ¿No hay nada elemental que escribir aquí?
        //
        // Respuesta: NO. tags-prose-formal ES lo más elemental posible.
        //
        // Razones (Mantra 4 + Mantra 3):
        // - Cualquier campo concreto (abi_version, required_capabilities, etc.) sería una "cadena mágica"
        //   que rompería consenso entre hosts futuros.
        // - El organismo solo necesita declarar "necesito un sustrato que entienda esto".
        //   tags + prose + formal bytes es la única forma anti-consenso y eterna.
        // - Todo lo que el futuro traiga (cuántico, biológico, etc.) irá dentro del formal.
        //
        // No se puede simplificar más sin violar Mantra 4.
        repeated string tags = 1;
        string prose = 2;
        bytes formal = 3;
    }

    message Filesystem {
        message ItemBranch {
            message Link {
                string src = 1;
                string dst = 2;
            }

            string name = 1;

            oneof item {
                bytes file = 2;               // Esencia (contenido)
                Link link = 3;                // referencia por nombre (inode propio)
                Filesystem filesystem = 4;    // directorio anidado
            }

            // Forma → solo xattrs (Mantra 2). Todo lo demás va aquí.
            map<string, bytes> xattrs = 6;
        }

        repeated ItemBranch branch = 1;
    }
}

message ConfigDeclaration {
    // Razonamiento: el organismo declara qué configuración inicial necesita del host (Mantra 3).
    // El host la inyecta en la ruta indicada. Nunca viaja dentro del genoma.
    repeated string path = 1;
    DataFormat format = 2;
}

message Network {
    // Razonamiento elemental (Mantra 3 + aislamiento por defecto):
    // El organismo nace aislado. Cualquier conexión externa es un "sentido" que debe declarar explícitamente.
    // El host decide si lo concede y materializa el canal real (peers, DNS, túnel, etc.).
    // Nunca lleva direcciones concretas dentro del genoma.
    repeated string tags = 1;
    string prose = 2;
    bytes formal = 3;
    repeated Api.Protocol protocol_stack = 4;
}

// ======================
// Partes que ya eran elementales y no cambian
// ======================
message Api {
    message Protocol {
        repeated string tags = 1;
        string prose = 2;
        bytes formal = 3;
    }
    message Slot {
        int32 port = 1;  # Could be bytes at some point.
        repeated Protocol protocol_stack = 2;
        map<string, bytes> xattrs = 3;
    }
    map<string, DataFormat> environment_variables = 3;
    repeated Slot slot = 4;
    repeated GasPrice payment_contracts = 5; //  GasPrice es elemental porque no se refiere al “gas” de Ethereum, sino a unidades abstractas de recursos (computación, almacenamiento, tiempo, energía, etc.). El payment_contract declara mediante tags-prose-formal qué cosa se entrega (un contrato blockchain, una paloma mensajera, reputación, energía, o cualquier futuro equivalente) a cambio de X unidades de recursos. Luego, cada función de la API declara cuánto cuesta en esas mismas unidades. Así el organismo lleva su propio modelo económico completo sin asumir ninguna economía concreta ni requerir consenso global.
}

message Resources {
    Sysresources at_init = 1;
    Sysresources at_most = 2;
    optional int32 start_time_ms = 3;
}

// (Tipos externos: DataFormat, Sysresources, GasPrice se mantienen fuera o se anidan en futuras iteraciones.
// No afectan a los mantras actuales.)

*/

// ======================
// Resumen orgánico de por qué este es el esquema definitivo
// ======================
/*
- Todo lo que afecta al comportamiento observable viaja con el organismo (genoma).
- Todo lo del host solo se declara (sustrato).
- Default = aislamiento total (Network y ConfigDeclaration lo dejan explícito).
- No queda ni un campo que pueda romperse en 1000 años (xattrs puro).
- El hash sigue definido como H(H("")) → eterno y anti-consenso.
*/

```

====
====

# PLAN DE IMPLEMENTACIÓN (ACTUALIZADO Y ATERRIZADO A CÓDIGO)

Este plan parte del estado real de `protos/celaut.proto` (actual) y detalla **todos los cambios de código** para llevar el repositorio al esquema orgánico definido arriba (`Init`, `ConfigDeclaration`, `KernelInterface`, `xattrs`, `Network.protocol_stack`).

## 0. Supuestos de migración

- Esta migración se considera **breaking** a nivel de contrato protobuf.
- Estrategia acordada: **hard cutover** (sin backward compatibility).
  - Lectura: aceptar solo esquema nuevo.
  - Escritura: emitir solo esquema nuevo.
- Todo payload antiguo debe considerarse inválido desde el momento del despliegue.

## 1. Fase de contrato protobuf (bloqueante)

### 1.1 Archivos de esquema a modificar

- `protos/celaut.proto`
  - `Service`: añadir `ConfigDeclaration config_declaration = 5;`.
  - `Service.Container`: reemplazar `entrypoint` por `Init`, eliminar `Config`, añadir `KernelInterface`.
  - `Service.Container.Filesystem.ItemBranch`: añadir `xattrs`.
  - `Service.Network`: renombrar `client_protocol_stack` -> `protocol_stack`.
  - `Service.Api.Slot`: añadir `map<string, bytes> xattrs = 3;`.
- `protos/pack.proto`
  - Sincronizar con el nuevo contrato de `celaut.Service` (al menos: `init`, `config_declaration`, `kernel_interface`, `protocol_stack`).
  - Revisar coherencia entre `pack.Service.Container` y `celaut.Service.Container` para evitar divergencia semántica.
- `src/commands/tui/protos/celaut.proto`
  - Mantenerlo idéntico al `protos/celaut.proto` principal (evitar drift de Rust/TUI).

### 1.2 Artefactos generados

- Regenerar:
  - `protos/celaut_pb2.py`
  - `protos/celaut_pb2_grpc.py`
  - `protos/pack_pb2.py`
- Validar script:
  - `bash/generate_protos.sh`
- Recompilar TUI (usa `build.rs` + `prost`):
  - `src/commands/tui/build.rs` ya recompila con cambios de proto; verificar build tras sincronizar `src/commands/tui/protos/celaut.proto`.

### 1.3 Decisión obligatoria sobre numeración de campos

- Antes de implementar, fijar y documentar si se reutilizan números de campo o se reservan.
- Recomendación técnica para minimizar corrupción wire-format: **no reutilizar números de campos eliminados** y usar `reserved` para nombres/números legacy.

## 2. Fase de builders/packers (serialización del Service)

### 2.1 Archivos a tocar

- `src/packers/zip_with_dockerfile.py`
- `src/packers/zip_with_dockerfile_fractal.py`
- `src/packers/README.md`

### 2.2 Cambios funcionales

- Migrar `entrypoint` de `service.json` a estructura `init`:
  - `container.init.argv`
  - `container.init.working_directory`
  - `container.init.xattrs`
- Migrar `container.config` a `service.config_declaration`.
- Añadir parsing opcional de:
  - `kernel_interface` (tags/prose/formal)
  - `api[].xattrs`
- Filesystem:
  - Orden determinista de `branch` (`sorted(...)`), evitando `os.listdir` no determinista.
  - Captura de metadatos en `xattrs` (ej: `posix.mode`, `posix.uid`, `posix.gid`, `posix.mtime_ns`, etc.).

## 3. Fase de runtimes/virtualizadores

### 3.1 Docker runtime

Archivos:
- `src/virtualizers/docker/execute.py`
- `src/virtualizers/docker/set_container_config.py`
- `src/virtualizers/docker/build.py`

Cambios:
- `execute.py`: usar `service.container.init.argv` en lugar de `service.container.entrypoint`.
- `set_container_config.py`: recibir `service.config_declaration` en vez de `service.container.config`.
- Aplicar `xattrs` del filesystem al materializar rootfs (al menos permisos/mode inicialmente).

### 3.2 Cloud Hypervisor runtime

Archivos:
- `src/virtualizers/cloud_hypervisor/build.py`
- `src/virtualizers/cloud_hypervisor/execute.py`
- `bash/build_ch_initramfs.sh`

Cambios:
- Reemplazar validación de `entrypoint` por validación de `init`:
  - `argv` no vacío
  - `argv[0]` absoluto
  - `working_directory` válida
- Cambiar inyección de metadata del arranque:
  - de `/.__nodo_entrypoint` a un payload de init (por ejemplo JSON/binario con `argv`, `cwd`, `xattrs`).
- `build_ch_initramfs.sh`:
  - leer la nueva metadata de init
  - hacer `cd` al `working_directory`
  - ejecutar con argv completo (`switch_root ... "$argv0" "$@"` equivalente)
- Inyección de config:
  - usar `service.config_declaration.path` (ahora está en `Service`, no en `Container`).
- Materialización de filesystem:
  - añadir soporte de `xattrs` igual que en Docker.

## 4. Fase de red, manager y superficie CLI

### 4.1 Red y resolución de peers

Archivo:
- `src/manager/networks.py`

Cambio:
- `network.client_protocol_stack` -> `network.protocol_stack`.

### 4.2 Inspección y UX de CLI

Archivo:
- `src/commands/inspect.py`

Cambios:
- Mostrar `container.init` en lugar de `container.entrypoint`.
- Mostrar `service.config_declaration` en lugar de `container.config`.
- Mostrar `kernel_interface` cuando exista.

## 5. Migración de datos (sin compatibilidad temporal)

### 5.1 Migración persistente (registry/cache)

- Añadir script de migración de servicios serializados en registry/cache:
  - Reescribir servicios antiguos al nuevo contrato.
  - Recalcular hash/ID si cambia serialización canónica.
- Ejecutar migración **antes** del despliegue del nuevo runtime.
- Si existen artefactos no migrables, fallar de forma explícita y no arrancar.

## 6. Plan de pruebas (obligatorio)

### 6.1 Unit tests a actualizar

- `tests/test_cloud_hypervisor_execute_helpers.py`
  - reemplazar tests de `_validate_entrypoint_strict` por tests de validación de `init`.
  - adaptar test de config target a `config_declaration`.

### 6.2 Nuevos tests a añadir

- Packer:
  - serialización canónica (`branch` ordenado determinísticamente).
  - metadatos en `xattrs`.
- Runtime:
  - Docker y CH ejecutan `init.argv` completo con `working_directory`.
  - inyección de `__config__` desde `config_declaration`.
- Red:
  - `protocol_stack` en `Service.Network` sin referencias legacy.

### 6.3 Smoke/regresión

- Empaquetar servicio de ejemplo con esquema nuevo.
- Arrancar en Docker y Cloud Hypervisor.
- Verificar conectividad esperada y aislamiento por defecto.

## 7. Secuencia recomendada de ejecución

1. Actualizar protos (`celaut.proto`, `pack.proto`, copia TUI).
2. Regenerar stubs Python y recompilar TUI.
3. Adaptar packers (emisión y lectura solo del esquema nuevo).
4. Adaptar runtimes Docker/CH + script initramfs.
5. Adaptar manager/CLI (`networks.py`, `inspect.py`).
6. Añadir/actualizar tests.
7. Ejecutar migración de datos y activar escritura estricta nueva.

## 8. Checklist de cierre

- No quedan referencias en código a:
  - `container.entrypoint`
  - `container.config`
  - `network.client_protocol_stack`
- No existe lógica de fallback/mapeo desde esquema legacy.
- Protos principal/TUI/pack están sincronizados.
- Tests unitarios + smoke en ambos virtualizadores en verde.
- Documentación de packers y runtime actualizada con el modelo `init/config_declaration`.
