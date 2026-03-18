# Cloud Hypervisor como virtualizador (aislamiento fuerte)

## Objetivo y contexto

Integrar **Cloud Hypervisor (CH)** como backend de virtualización para ejecutar servicios con **aislamiento fuerte** (microVM sobre KVM), manteniendo el contrato actual expuesto por `src/virtualizers/interface.py`.

La motivación es elevar el aislamiento frente a contenedores, reducir el blast radius ante escapes y endurecer límites entre servicios sin cambiar la API consumida por el resto del sistema.

## Estado actual y bloqueos

Aunque existe una fachada en `src/virtualizers/interface.py`, hoy el acoplamiento con Docker sigue presente en varios puntos, lo que impide seleccionar otro virtualizador sin refactor.

Puntos críticos a desacoplar:

- `src/balancers/execution_balancer/execution_balancer.py` usa `src/virtualizers.docker.build` de forma directa.
- `src/gateway/iterables/*` importan `src/virtualizers.docker.build` directamente.
- `src/gateway/launcher/launch_service.py` importa `TransportProtocol` y `allow_connection` desde Docker.
- `src/manager/manager.py` usa `TransportProtocol` de Docker.
- `src/virtualizers/interface.py` solo delega a Docker y solo acepta `docker` en `_is_supported_virtualizer`.

Consecuencia: aunque se implemente un backend CH, no podrá usarse sin mover estos puntos a la interfaz común.

## Arquitectura propuesta (mínimo viable)

Componentes esenciales para ejecutar un servicio en CH:

- **Guest base** por arquitectura: kernel (`vmlinuz`) + `initramfs` minimal.
- **Rootfs por servicio**: `rootfs.ext4` generado desde `service.container.filesystem`.
- **Inyección de `__config__`**: equivalente a `set_container_config.py` pero para el rootfs (pre‑boot o via virtio-fs).
- **Red**: interfaz TAP por VM + bridge del host, con política “deny by default”.
- **Persistencia de estado**: índice `vmachine_id -> {pid, api_socket, tap, ip, rootfs}` para `kill/maintain/firewall`.

## Plan por fases con hitos y entregables

1. **Fase 0 – Desacoplar Docker y preparar interfaz**. Entregables: llamadas a `src/virtualizers/interface.py` desde el resto del sistema; `TransportProtocol` movido a una capa neutral; registro del virtualizer por instancia en DB; claves de configuración para CH.
2. **Fase 1 – Build CH**. Entregables: pipeline para generar `rootfs.ext4` desde `service.container.filesystem`; selección de kernel/initramfs por arquitectura; layout de cache de artefactos; `is_built` basado en bundle CH.
3. **Fase 2 – Execute CH**. Entregables: creación de microVM con API socket; montaje de rootfs; inyección de `__config__`; arranque y obtención de IP; persistencia de estado de VM.
4. **Fase 3 – Red y firewall**. Entregables: reglas por interfaz TAP o IP estable; allowlist a gateway y peers; compatibilidad con `allow_connection_*` desde la capa común.
5. **Fase 4 – Lifecycle y hotplug**. Entregables: mapeo de `mem_limit/cpu` a CH o cgroups del proceso; semántica explícita de “no soportado” por campo; `kill/maintain/remove` para VM.
6. **Fase 5 – Observabilidad y operación**. Entregables: logs mínimos por VM; métricas básicas (PID, uptime, mem); limpieza de recursos; rollback a Docker por flag.

## Viabilidad técnica y operativa

Mapa por función de `src/virtualizers/interface.py`:

- `is_built(service_hash)`: Viable. Debe comprobar la existencia del bundle CH (por ejemplo `rootfs.ext4` + metadatos + kernel/initramfs seleccionados).
- `build(service, metadata, service_id)`: Viable con coste alto. Requiere transformar el filesystem del servicio en un rootfs booteable y asociarlo a un guest base por arquitectura.
- `execute(...) -> (vmachine_id, vmachine_ip)`: Viable. Debe crear una microVM, configurar red, arrancar, y persistir `pid/socket/tap/ip`.
- `hotplug(vmachine_id, system_requirements_range)`: Parcialmente viable. `mem_limit` y `cpu` pueden mapearse a CH o a cgroups del proceso; otros campos pueden declararse no soportados inicialmente.
- `kill(vmachine_id)`: Viable. Apagar por API si está disponible, o terminar el proceso si no responde.
- `maintain(vmachine_id, debug_mode, remove_and_penalize)`: Viable. Comprobar proceso/socket y penalizar si no existe.
- `remove_firewall_rule(vmachine_id, ip, port, protocol)`: Viable. Debe operar sobre TAP o IP persistida de la VM.

Requisitos operativos del host:

- Soporte KVM (`/dev/kvm`) y permisos de acceso.
- Módulos de red (TAP/bridge) y política iptables/nftables compatible.
- Capacidad de ejecutar procesos `cloud-hypervisor` y mantener sockets de control.

## Cambios públicos/Interfaces a documentar

- `_is_supported_virtualizer` debe aceptar `cloud_hypervisor`.
- `TransportProtocol` debe moverse a una capa neutral (no en `src/virtualizers/docker/*`).
- Nuevas claves de configuración bajo `virtualizers.cloud_hypervisor.*`.

Claves sugeridas:

```
virtualizers.cloud_hypervisor.KERNEL_PATH
virtualizers.cloud_hypervisor.INITRAMFS_PATH
virtualizers.cloud_hypervisor.BINARY_PATH
virtualizers.cloud_hypervisor.CACHE_DIR
virtualizers.cloud_hypervisor.API_SOCKETS_DIR
virtualizers.cloud_hypervisor.NETWORK_MODE
virtualizers.cloud_hypervisor.NETWORK_BRIDGE_NAME
virtualizers.cloud_hypervisor.NETWORK_SUBNET
virtualizers.cloud_hypervisor.NETWORK_GATEWAY_IP
```

## Riesgos y mitigaciones

- IP no determinista o difícil de recuperar. Mitigación: asignación determinista y persistencia de estado por `vmachine_id`.
- Hotplug parcial con semántica distinta a Docker. Mitigación: tabla de compatibilidad por campo y retorno explícito de “no soportado”.
- Permisos KVM y red en host. Mitigación: checks de preflight y fallback a Docker si falta soporte.
- Coste de build del rootfs y caching. Mitigación: cache por `service_id` y reutilización de guest base.
- Divergencia de firewall respecto a Docker. Mitigación: capa común de firewall basada en interfaz o IP estable.

## Plan de pruebas

- Build: generar `rootfs.ext4` para un servicio simple y validar `is_built`.
- Execute: boot de microVM, ejecución de entrypoint, obtención de IP.
- Red/Firewall: bloqueo por defecto y allowlist a gateway/peers.
- Lifecycle: `kill/maintain` con VM caída y hotplug parcial.
- Compatibilidad: fallback a Docker con flag y arquitectura no soportada.

## Criterios de éxito

- Servicios ejecutan con aislamiento fuerte sin cambios en el API del sistema.
- `interface.py` soporta `docker` y `cloud_hypervisor` de forma conmutable por configuración.
- Red y firewall mantienen el comportamiento funcional existente.
- El sistema puede operar sin Docker cuando CH está habilitado.

## Decisiones resueltas

- Red: se adopta TAP + bridge con IP determinista por `vmachine_id`.
- Hotplug: solo `mem_limit` garantizado; el resto aplica si es posible y, si no, retorna “no soportado”.
- Inyección de `__config__`: pre‑boot (rootfs), no en runtime.

## Diseño de red (decisión cerrada)

### Modelo de red

Se descarta NAT (user-mode/slirp) y se adopta **TAP atado a un bridge en el host (`br-ch`)**. Esto permite mantener el contrato actual de red y aplicar reglas de firewall por IP de la VM.

### Motivos

- Aislamiento y control en capa 2, compatible con `allow_connection`.
- Comunicación P2P entre peers con reglas explícitas.
- Menor overhead al evitar NAT por paquete.

### Asignación de IP determinista

La IP y la MAC se derivan del `vmachine_id`, sin DHCP, para garantizar estabilidad y permitir reinicios sin perder reglas.

Algoritmo recomendado:

1. Tomar `vmachine_id` y calcular hash (SHA‑256 o MD5).
2. Derivar MAC local con prefijo `02:42:ac` y bytes del hash.
3. Calcular offset por módulo del tamaño de la subred.
4. Sumar el offset a la IP base de la red.

### Flujo de implementación

**Preflight del host (una vez):**

1. Crear bridge: `ip link add br-ch type bridge`.
2. Asignar gateway: `ip addr add 192.168.200.1/24 dev br-ch`.
3. Habilitar forwarding: `sysctl net.ipv4.ip_forward=1`.
4. Aplicar deny‑by‑default en `FORWARD` para `br-ch`.

**Ejecución de la microVM (`execute`):**

1. Calcular IP y MAC deterministas.
2. Crear TAP, levantarla y asociarla a `br-ch`.
3. Lanzar `cloud-hypervisor` con `--net-tap` y cmdline con IP estática.
4. Persistir `{tap, ip}` en el estado de la VM.

**Integración con firewall:**

1. Consultar IP estable desde el estado de VM.
2. Insertar regla `FORWARD` para `<IP_VM> -> <IP_PEER>` con puerto y protocolo.
3. Eliminar reglas usando los mismos parámetros.

### Configuración requerida

```yaml
virtualizers.cloud_hypervisor:
  NETWORK_MODE: "tap_bridge"
  NETWORK_BRIDGE_NAME: "br-ch"
  NETWORK_SUBNET: "192.168.200.0/24"
  NETWORK_GATEWAY_IP: "192.168.200.1"
```

### Resumen de red

Este diseño elimina dependencias de la red de Docker, mantiene el modelo deny‑by‑default y garantiza IPs estables para el ciclo de vida completo de cada servicio.
