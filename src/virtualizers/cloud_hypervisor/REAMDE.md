# Cloud Hypervisor como virtualizador (aislamiento fuerte)

## Objetivo

Integrar **Cloud Hypervisor (CH)** como backend de virtualización para ejecutar servicios con **aislamiento fuerte** (microVM sobre KVM), manteniendo el contrato actual expuesto por `src/virtualizers/interface.py`.

La motivación es elevar el aislamiento respecto a contenedores (Docker), reduciendo el “blast radius” ante escapes y endureciendo los límites entre servicios.

## Estado actual del código (por qué no es “plug & play”)

En el repositorio, `src/virtualizers/interface.py` actúa como *façade* sobre Docker (delegación directa a `src/virtualizers/docker/*`).

Además, existen puntos del sistema que **importan Docker directamente** (por ejemplo `execute`), lo que impide que un nuevo virtualizador sea seleccionable sin refactor. La primera corrección es que el resto del código pase por `src/virtualizers/interface.py` para **build/execute** (y luego para el resto de operaciones).

## Viabilidad por función de `src/virtualizers/interface.py`

### `is_built(service_hash) -> bool`

**Viable y simple.**

En Docker significa “existe una imagen `service_id.docker`”.
En CH debería significar “existe un artefacto cacheado/bundle listo para bootear”, por ejemplo:

- `rootfs.ext4` (o imagen de disco) generado desde `service.container.filesystem`
- metadatos del bundle (hash, arquitectura, etc.)
- (opcional) kernel/initramfs seleccionados

La implementación sería un check de ficheros/directorios bajo el cache configurado.

### `build(service, metadata, service_id) -> str`

**Viable, pero es el cambio más costoso.**

El build actual asume semántica de contenedor: filesystem arbitrario + `entrypoint`, y se materializa como imagen Docker `FROM scratch`.

Cloud Hypervisor necesita una VM booteable:

- **kernel** (por arquitectura)
- **initramfs / init** (o un rootfs con init) capaz de arrancar el proceso del servicio
- **rootfs** (imagen ext4, qcow2, o virtio-fs) que contenga el filesystem del servicio

Ruta mínima (recomendada para compatibilidad con el formato actual de servicio):

1) Generar un `rootfs.ext4` a partir del protobuf `service.container.filesystem`.
2) Usar un “guest base” común (kernel+initramfs minimal) que:
   - monte el rootfs (virtio-blk / virtio-fs),
   - lea `__config__` (ya existe el concepto en `set_container_config.py`),
   - ejecute el `entrypoint`.

Sin este “guest base”, `build()` no puede mapear 1:1 el concepto “contenedor FROM scratch” al concepto “microVM booteable”.

### `execute(...) -> (vmachine_id, vmachine_ip)`

**Viable, pero requiere definir el modelo de red y el estado de la VM.**

En Docker, `execute`:

- crea contenedor (imagen `service_id.docker`)
- injecta `__config__`
- arranca
- obtiene IP
- aplica firewall (iptables FORWARD) según IP del contenedor

En CH, `execute` debe:

- crear una microVM (proceso `cloud-hypervisor`) con:
  - API socket por VM,
  - vCPU/mem inicial,
  - device de disco/virtio-fs con el rootfs del servicio,
  - interfaz de red (tap).
- arrancar la VM, obtener/persistir:
  - `vmachine_id` (token) que el sistema use para referenciar la instancia,
  - `vmachine_ip` (si el modelo actual sigue usando IPs).

Decisiones críticas:

- **Red**:
  - TAP dedicado por VM + bridge (o NAT) en el host.
  - DHCP estático/host-managed o asignación determinista para poder recuperar IP sin “inspección Docker”.
- **Estado**:
  - El sistema necesita mapear `vmachine_id -> {pid, api_socket, tap, ip}` para `maintain/kill/firewall`.

### `hotplug(vmachine_id, system_requirements_range) -> bool`

**Viable parcialmente; requiere redefinir expectativas.**

El proto `Sysresources` mezcla campos típicos de cgroups (contenedores) con límites que no siempre aplican igual a VMs:

- `mem_limit`: puede ser soportable si CH/guest permiten hotplug (comúnmente más fácil aumentar que reducir).
- `cpu_period/cpu_quota`: en Docker mapean a CFS; en CH podría mapearse a:
  - número de vCPUs, o
  - límites cgroups sobre el proceso `cloud-hypervisor` (semántica distinta).
- `blkio_weight`: en VM no aplica igual; podría mapearse a cgroups del proceso, o ignorarse.
- `disk_space`: redimensionado en caliente suele ser complejo; puede declararse “no soportado” inicialmente.

Recomendación: definir una tabla de compatibilidad “campo -> comportamiento en CH” y que el contrato de `hotplug` sea explícito en fallos parciales (y loguee qué se aplicó).

### `kill(vmachine_id) -> bool`

**Viable y directo.**

Equivalentes:

- pedir shutdown por API (si se implementa),
- o terminar el proceso de la VM si no responde.

Requiere que `execute` haya persistido cómo localizar la VM (socket/PID).

### `maintain(vmachine_id, debug_mode, remove_and_penalize) -> None`

**Viable.**

En vez de consultar estado de contenedor, se consulta:

- si el proceso de CH sigue vivo,
- o si el API socket responde.

Si “no existe” o está “exited”, se invoca `remove_and_penalize`.

### `remove_firewall_rule(vmachine_id, ip, port, protocol) -> bool`

**Viable, pero cambia el punto de anclaje.**

Hoy el firewall se programa con iptables sobre el tráfico del contenedor basándose en su IP.

Con CH, lo más robusto es:

- aplicar reglas por **interfaz TAP** asociada a la VM (en host),
- o por IP si es estable y se persistió correctamente.

Esto exige que el virtualizador exponga/almacene el identificador de interfaz (y/o la IP) por VM.

## Recomendación de arquitectura (mínimo viable para aislamiento fuerte)

- **MicroVM por servicio**: un proceso CH por instancia.
- **Guest base** versionado por arquitectura:
  - kernel (vmlinuz) + initramfs minimal,
  - soporte de virtio-blk/virtio-fs,
  - init que ejecute `entrypoint`.
- **Rootfs por servicio**: imagen ext4 generada desde `service.container.filesystem`.
- **Red**: TAP por VM + bridge/NAT, con política “deny by default” y allow-lists.
- **Persistencia de estado de VM**: tabla/archivo/DB para `pid/socket/tap/ip` indexada por `vmachine_id`.

## Cambios de código necesarios (alto nivel)

1) Implementar un backend `src/virtualizers/cloud_hypervisor/*`.
2) Diseñar y fijar el contrato de:
   - `build` (bundle booteable),
   - `execute` (lifecycle + estado),
   - firewall (interfaz/ip).

## Riesgos y puntos a validar

- Disponibilidad de `/dev/kvm` y permisos del proceso.
- Modelo de red y obtención fiable de IP del guest.
- Semántica de `hotplug` con el proto actual.
- Riesgo de “deriva” entre la lógica de firewall Docker y la de CH si no se unifica el modelo.

