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
- Esencia: el contenido real (bytes) o referencia (link / shared_identity).
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
                uint64 shared_identity = 5;   // ← TODO RESUELTO
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
- Ambos tipos de referencias (link + shared_identity) se mantienen porque son dos semánticas orgánicas distintas e indispensables.
- El hash sigue definido como H(H("")) → eterno y anti-consenso.
*/

```

====
====

# PLAN DE IMPLEMENTACIÓN.

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
| Sustituir `repeated string entrypoint = 3;` | `Init init = 3;` | Elemental (2) + PID 1 inequívoco | Reemplazar completamente. `Init` tiene `argv` + `working_directory` + `xattrs`. |
| `Architecture` (tags-prose-formal) | Sin cambio | Anti-consenso (4) | OK |
| `repeated Api.Protocol node_protocol_stack ;` | Sin cambio | — | OK |
| Añadir `KernelInterface kernel_interface;` | Nuevo | Separación Genoma/Sustrato (3) | Obligatorio. Declara requisitos al host (ABI, capacidades, etc.). |
| Eliminado `Config config = 5;` | sustituido por ConfigDeclaration fuera de Container |

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
            uint64 shared_identity = 5;   // ← NUEVO
        }
        map<string, bytes> xattrs = 6;     // ← NUEVO
    }
}
```

**Acciones para los desarrolladores del builder:**
- **Hardlinks**: durante la serialización asignar IDs internos secuenciales (64-bit) y usar `shared_identity` cuando dos nombres compartan el mismo inode. Nunca duplicar bytes.
- **Symlinks**: siguen usando `Link` (referencia por nombre).
- **Metadata**: todo lo que antes era `mode`, `uid`, `gid`, `mtime`, `device` **DEBE** ir en `xattrs` con claves libres (ej: "posix.mode", "posix.uid", "selinux.label", "future.quantum.integrity", etc.).
- **Atomicidad**: el `bytes filesystem` debe serializarse de forma canónica (orden de branches determinista + hardlink IDs asignados antes de escribir).

### 6. Nuevo mensaje `ConfigDeclaration` (hermano de `container`)
```proto
message ConfigDeclaration {
    repeated string path = 1;
    DataFormat format = 2;
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
   - Interpretar `xattrs` de ItemBranch según lo declarado en `kernel_interface`.

4. **Extensibilidad futura**  
   - **Nunca** añadir campos nuevos fuera de `xattrs` o `tags-prose-formal`.  
   - Todo lo que aparezca en 1000 años (cuántico, biológico, etc.) va dentro de `xattrs` o `formal`.

5. **Regla de oro práctica para cualquier cambio**  
   Pregunta antes de tocar nada:  
   “¿Esto hace al organismo más completo sin contaminarlo con el host?”  
   Si la respuesta no es sí → fuera.

Esta es la lista **completa y accionable**.  
Una vez aplicados estos cambios, `celaut.proto` queda **100 % elemental, atemporal y alineado con los 4 mantras**.

