```proto
// ====================================================================
// ESPECIFICACIÓN FORMAL DE SERVICE
// Versión elemental, orgánica y atemporal (cumple estrictamente los 4 mantras)
// ====================================================================

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
   La única vía de evolución futura es map<string, bytes> xattrs.

3. Mantra de Separación Genoma / Sustrato
   Genoma → viaja dentro del Service (filesystem + form + init).
   Sustrato → solo se declara (nunca se incorpora). Default = aislamiento total.

4. Mantra Anti-Consenso
   El esquema debe estar a prueba de rotura de convenciones.
   Nunca se usan cadenas mágicas ni nombres que requieran consenso global.
   Única excepción permitida: patrón tags-prose-formal (o equivalente) cuando NO se puede materializar lo elemental.
*/

// ======================
// DEFINICIÓN DEL HASH INMUTABLE DEL SERVICE
// (cumple Mantra 4 al 100%)
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
    // prose → Nombre humano del organismo (Mantra 1). Elemental y atemporal.
    string prose = 1;

    // El organismo completo (genoma).
    Container container = 2;

    // Declaración de API (cómo el mundo externo puede hablar con el organismo).
    Api api = 3;

    // Declaración de ámbitos externos requeridos (Mantra 3: aislamiento por defecto).
    repeated Network network = 4;

    // Declaración de configuración inicial que el host debe inyectar (sustrato puro).
    ConfigDeclaration config_declaration = 5;
}

message Container {
    // Arquitectura del organismo (no materializable sin consenso → tags-prose-formal).
    Architecture architecture = 1;

    // Serialized rootfs completo (el árbol con Nombre + Forma + Esencia).
    bytes filesystem = 2;

    // Punto de activación inequívoco (PID 1 o equivalente en cualquier sistema futuro).
    Init init = 3;

    // Requisitos de recursos (elementales, no necesitan tags-prose-formal).
    optional Resources resources = 4;
    Config config = 5;  // (mantener por compatibilidad histórica; no afecta mantras)

    // Protocol stack interno del nodo.
    repeated Api.Protocol node_protocol_stack = 6;

    // Carta de requisitos al sustrato (solo declaración).
    KernelInterface kernel_interface = 7;

    // =============================================================
    message Architecture {
        // Razonamiento: nunca podemos materializar "x86_64" o "wasm" sin romper Mantra 4.
        repeated string tags = 1;
        string prose = 2;
        bytes formal = 3;  // descriptor binario real del genoma
    }

    message Init {
        // Razonamiento: todo organismo necesita un punto de entrada inequívoco (Mantra 1).
        // argv + working_directory + xattrs es la forma más elemental posible.
        repeated string argv = 1;
        optional string working_directory = 2;  // default = "/"
        map<string, bytes> xattrs = 3;
    }

    message KernelInterface {
        // Razonamiento: requisitos mínimos al sustrato (Mantra 3). Nunca se incorpora.
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
                uint64 hardlink_inode = 5;    // referencia al mismo inode (identidad compartida)
            }

            // Forma materializada → solo xattrs (Mantra 2). Todo lo demás (permisos, timestamps, devices…) va aquí.
            // Razonamiento: en 1000 años no asumimos usuarios, tiempo lineal ni dispositivos. xattrs es eterno.
            optional ItemForm form = 10;
        }

        repeated ItemBranch branch = 1;
    }

    message ItemForm {
        // Versión más elemental y atemporal posible.
        map<string, bytes> xattrs = 1;
    }
}

message ConfigDeclaration {
    // Razonamiento: el organismo declara qué configuración inicial necesita del host (Mantra 3).
    // El host la inyecta en la ruta indicada. Nunca viaja dentro del genoma.
    repeated string path = 1;
    DataFormat format = 2;
    bytes expected_hash = 3;  // opcional, para reproducibilidad
}

message Network {
    // Razonamiento elemental (Mantra 3 + aislamiento por defecto):
    // El organismo nace aislado. Cualquier conexión externa es un "sentido" que debe declarar explícitamente.
    // El host decide si lo concede y materializa el canal real (peers, DNS, túnel, etc.).
    // Nunca lleva direcciones concretas dentro del genoma.

    // Identificador del ámbito externo requerido (ej: google, bitcoin-mainnet, etc.).
    repeated string tags = 1;

    // Explicación humana del ámbito.
    string prose = 2;

    // Descriptor binario que el host interpreta (semillas, versión de protocolo, etc.).
    bytes formal = 3;

    // Protocolos que el organismo usará dentro de ese ámbito.
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
        int32 port = 1;
        repeated Protocol protocol_stack = 2;
    }

    map<string, DataFormat> environment_variables = 3;
    repeated Slot slot = 4;
    repeated GasPrice payment_contracts = 5;
}

message Resources {
    Sysresources at_init = 1;
    Sysresources at_most = 2;
    optional int32 start_time_ms = 3;
}

// (Tipos externos: DataFormat, Sysresources, GasPrice, Config se mantienen fuera o se anidan en futuras iteraciones.
// No afectan a los mantras actuales.)

```

**Resumen orgánico de por qué este es el esquema definitivo**

- Todo lo que afecta al comportamiento observable viaja con el organismo (genoma).  
- Todo lo del host solo se declara (sustrato).  
- Default = aislamiento total (Network y ConfigDeclaration lo dejan explícito).  
- No queda ni un campo que pueda romperse en 1000 años (ItemForm = solo xattrs).  
- Ambos tipos de enlaces (link + hardlink_inode) se mantienen porque son dos semánticas orgánicas distintas e indispensables.  
- El hash sigue definido como H(H("")) → eterno y anti-consenso.

Esta es la versión **más desnuda y elemental** que podemos tener hoy.


====
====

**CAMBIOS OBLIGATORIOS QUE DEBEMOS APLICAR**  
(para pasar de la especificación actual a la versión **elemental y orgánica** que cumple los 4 mantras)

A continuación tienes **el listado exacto y ordenado** de cambios estructurales, con el motivo (mantra), el impacto técnico y lo que los desarrolladores deben tener en cuenta.

### 1. Cambios estructurales en el mensaje raíz `Service`
| Cambio | Motivo (mantra) | Qué hacer |
|--------|------------------|-----------|
| Añadir `ConfigDeclaration config_declaration = 5;` | Separación Genoma/Sustrato (3) + aislamiento | El host inyecta el `__config__`. Nunca viaja dentro del genoma. |
| Mantener `prose`, `container`, `api`, `network` | Orgánico (1) | Sin cambios en orden. |

### 2. Cambios dentro de `Container`
| Campo actual | → Nuevo | Motivo | Acción para devs |
|--------------|---------|--------|------------------|
| `repeated string entrypoint = 3;` | `Init init = 3;` | Elemental (2) + PID 1 inequívoco | Reemplazar completamente. `Init` tiene `argv` + `working_directory` + `xattrs`. |
| `Architecture` (tags-prose-formal) | Sin cambio | Anti-consenso (4) | OK |
| `repeated Api.Protocol node_protocol_stack = 6;` | Sin cambio | — | OK |
| Añadir `KernelInterface kernel_interface = 7;` | Nuevo | Separación Genoma/Sustrato (3) | Obligatorio. Declara requisitos al host (ABI, capacidades, etc.). |
| `Config config = 5;` | Mantener por compatibilidad histórica | — | Se deja pero se desaconseja su uso futuro. |

### 3. Nuevo mensaje `Init` (reemplaza `entrypoint`)
```proto
message Init {
    repeated string argv = 1;               // argv[0] = ruta absoluta al binario PID 1
    optional string working_directory = 2;  // default = "/"
    map<string, bytes> xattrs = 3;
}
```
**Obligatorio** para todos los builders. El organismo ya no “tiene un entrypoint”, tiene un punto de activación inequívoco dentro de su propio filesystem.

### 4. Nuevo mensaje `KernelInterface`
```proto
message KernelInterface {
    repeated string tags = 1;
    string prose = 2;
    bytes formal = 3;
}
```
**Obligatorio**. Es la “carta de requisitos” al host. Nunca se incorpora dentro del Service.

### 5. Cambios en `Filesystem` (el más crítico)
```proto
message Filesystem {
    message ItemBranch {
        ...
        oneof item {
            bytes file = 2;
            Link link = 3;
            Filesystem filesystem = 4;
            uint64 hardlink_inode = 5;   // ← NUEVO
        }
        optional ItemForm form = 10;     // ← NUEVO
    }
}
```

**ItemForm** (nuevo y obligatorio):
```proto
message ItemForm {
    map<string, bytes> xattrs = 1;   // ÚNICA vía de extensión. Todo lo demás (mode, uid, gid, mtime, device…) va aquí.
}
```

**Acciones para los desarrolladores del builder:**
- **Hardlinks**: durante la serialización asignar IDs internos secuenciales (64-bit) y usar `hardlink_inode` cuando dos nombres compartan el mismo inode. Nunca duplicar bytes.
- **Symlinks**: siguen usando `Link` (referencia por nombre).
- **Metadata**: todo lo que antes era `mode`, `uid`, `gid`, `mtime`, `device` **DEBE** ir en `xattrs` con claves libres (ej: "posix.mode", "posix.uid", "selinux.label", "future.quantum.integrity", etc.).
- **Atomicidad**: el `bytes filesystem` debe serializarse de forma canónica (orden de branches determinista + hardlink IDs asignados antes de escribir).

### 6. Nuevo mensaje `ConfigDeclaration` (hermano de `container`)
```proto
message ConfigDeclaration {
    repeated string path = 1;
    DataFormat format = 2;
    bytes expected_hash = 3;   // opcional
}
```
El host debe inyectar el archivo en la ruta indicada **antes** de arrancar el organismo.

### 7. Cambios en `Network` (para reflejar aislamiento por defecto)
```proto
message Network {
    repeated string tags = 1;
    string prose = 2;
    bytes formal = 3;
    repeated Api.Protocol protocol_stack = 4;   // ← cambiado de client_protocol_stack
}
```
**Importante**:
- Default = **cero acceso externo**.
- El organismo declara “necesito este ámbito” (google, bitcoin-mainnet, etc.).
- El host decide si concede y materializa el canal real.
- `client_protocol_stack` → ahora `protocol_stack` (más claro y elemental).

### 8. Añadir las 4 reglas de oro + definición de hash (al inicio del archivo)
Obligatorio poner el bloque de comentarios con los 4 mantras y la definición del hash inmutable:

```proto
// El hash inmutable se calcula como H(serialized_canonical(Service))
// donde H es "la función hash tal que H(H(bytes vacíos)) = <digest canónico>"
```

### COSAS QUE LOS DESARROLLADORES DEBEN TENER SIEMPRE EN CUENTA

1. **Backward compatibility**  
   - La nueva versión **rompe** `entrypoint` y `Config` (se convierten en `Init` y `ConfigDeclaration`).  
   - Durante la transición: los builders deben soportar ambos durante 1 versión y emitir warning.

2. **Serialización del filesystem**  
   - Debe ser **canónica** (orden de campos determinista).  
   - Hardlink IDs se asignan en una pasada previa (DFS o similar) antes de escribir bytes.  
   - El hash del Service entero incluye el `filesystem` serializado → cualquier cambio en orden o IDs invalida el hash.

3. **Hosts / runtimes**  
   - Deben leer `KernelInterface` y `Network` **antes** de arrancar.  
   - Si no pueden satisfacer `formal`, rechazar el Service.  
   - Inyectar `__config__` según `ConfigDeclaration`.  
   - Interpretar `xattrs` del `ItemForm` según lo declarado en `kernel_interface`.

4. **Extensibilidad futura**  
   - **Nunca** añadir campos nuevos fuera de `xattrs` o `tags-prose-formal`.  
   - Todo lo que aparezca en 1000 años (cuántico, biológico, etc.) va dentro de `xattrs` o `formal`.

5. **Regla de oro práctica para cualquier cambio**  
   Pregunta antes de tocar nada:  
   “¿Esto hace al organismo más completo sin contaminarlo con el host?”  
   Si la respuesta no es sí → fuera.

Esta es la lista **completa y accionable**.  
Una vez aplicados estos cambios, el `Service.proto` queda **100 % elemental, atemporal y alineado con los 4 mantras**.

¿Quieres que te genere el diff exacto en formato patch, o el archivo completo ya actualizado listo para copiar? Dime y lo entrego en un segundo.