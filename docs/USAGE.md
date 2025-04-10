# Nodo: Guía de Usuario

Esta guía está diseñada para ayudarte a comprender y utilizar los comandos disponibles en Nodo, una herramienta de orquestación de servicios para redes distribuidas. A continuación se muestra una lista completa de los comandos, junto con ejemplos de uso.

## Comandos Básicos

Estos comandos son los más utilizados para tareas cotidianas:

- **execute `<service id | service tag | '.celaut' file path>`**  
  Ejecuta una instancia de servicio.  
  **Ejemplo:**  
  `nodo execute 1234567890abcdef`

- **remove `<service id>`**  
  Elimina un servicio del nodo utilizando su ID.  
  **Ejemplo:**  
  `nodo remove 1234567890abcdef`

- **stop `<instance id>`**  
  Detiene una instancia de servicio utilizando su ID.  
  **Ejemplo:**  
  `nodo stop abcdef1234567890`

- **increase_gas `<instance id> <gas to add>`**  
  Aumenta la cantidad de gas asignado a una instancia.  
  **Ejemplo:**  
  `nodo increase_gas abcdef1234567890 100`

- **decrease_gas `<instance id> <gas to retire>`**  
  Disminuye la cantidad de gas asignado a una instancia.  
  **Ejemplo:**  
  `nodo decrease_gas abcdef1234567890 50`

- **services**  
  Muestra una lista de todos los servicios disponibles en el nodo.  
  **Ejemplo:**  
  `nodo services`

- **connect `<ip:url>`**  
  Conecta manualmente a un nodo par especificando la IP y el puerto.  
  **Ejemplo:**  
  `nodo connect 192.168.1.10:4040`

- **pack `<project directory>`**  
  Empaqueta un proyecto para crear una especificación de servicio.  
  **Ejemplo:**  
  `nodo pack /ruta/al/proyecto`

- **config**  
  Configura variables de entorno y otros ajustes relacionados con la operación de Nodo.  
  **Ejemplo:**  
  `nodo config`

- **tui**  
  Lanza la interfaz de usuario en terminal para visualizar y gestionar el nodo y sus servicios.  
  **Ejemplo:**  
  `nodo tui`

- **info**  
  Muestra información sobre el estado del servicio, versión y configuración del nodo.  
  **Ejemplo:**  
  `nodo info`

- **logs**  
  Muestra los registros de la aplicación en tiempo real para monitoreo.  
  **Ejemplo:**  
  `nodo logs`

- **export `<service> <path>`**  
  Exporta un servicio a la ruta especificada.  
  **Ejemplo:**  
  `nodo export MiServicio /ruta/de/exportacion`

- **import `<path>`**  
  Importa un servicio desde la ruta especificada.  
  **Ejemplo:**  
  `nodo import /ruta/del/servicio`

## Comandos Adicionales

Además de los comandos básicos, Nodo incluye otros comandos que permiten gestionar y explorar funcionalidades avanzadas:

- **service `<service id | tag>`**  
  Muestra detalles o inspecciona un servicio específico.  
  **Ejemplo:**  
  `nodo service 1234567890abcdef`

- **tag `<service id | tag> <new tag>`**  
  Modifica la etiqueta asociada a un servicio.  
  **Ejemplo:**  
  `nodo tag 1234567890abcdef nuevo_etiqueta`

- **clients**  
  Lista los clientes conectados al nodo.  
  **Ejemplo:**  
  `nodo clients`

- **peers**  
  Muestra la lista de nodos pares (peers) conectados.  
  **Ejemplo:**  
  `nodo peers`

## Comandos Avanzados

Estos comandos están destinados para entornos de desarrollo o mantenimiento avanzado:

- **update**  
  Actualiza Nodo. Requiere privilegios de superusuario.  
  **Ejemplo:**  
  `sudo nodo update`

- **serve**  
  Inicia el servicio de Nodo en modo de desarrollo. Si el servicio ya está corriendo en segundo plano, se notifica que no se puede iniciar nuevamente.  
  **Ejemplo:**  
  `nodo serve`

- **migrate**  
  Actualiza el esquema de la base de datos.  
  **Ejemplo:**  
  `nodo migrate`

- **storage:prune_blocks**  
  Limpia el almacenamiento eliminando bloques innecesarios para reducir el uso de disco.  
  **Ejemplo:**  
  `nodo storage:prune_blocks`

- **test `<test name>`**  
  Ejecuta pruebas específicas para servicios o funcionalidades.  
  **Ejemplo:**  
  `nodo test test_nombre`

- **rundev `<repository path>`**  
  Ejecuta una versión de desarrollo del repositorio especificado.  
  **Ejemplo:**  
  `nodo rundev /ruta/al/repositorio`

- **submit_reputation**  
  Envía la información de reputación de forma forzada.  
  **Ejemplo:**  
  `nodo submit_reputation`

- **refresh_ergo_nodes**  
  Actualiza la lista de nodos Ergo, seleccionando uno para utilizarlo como proveedor.  
  **Ejemplo:**  
  `nodo refresh_ergo_nodes`

- **prune_containers**  
  Elimina contenedores innecesarios. Requiere privilegios de superusuario.  
  **Ejemplo:**  
  `sudo nodo prune_containers`

- **daemon**  
  Inicia Nodo en modo demonio para su ejecución en segundo plano.  
  **Ejemplo:**  
  `nodo daemon`

## Nota Importante sobre la Gestión de Servicios

### Ejecución Automática mediante systemd

Si Nodo se instaló con privilegios de superusuario, se configura automáticamente como un servicio `systemd` para funcionar en segundo plano sin intervención manual.

### Ejecución Manual en Desarrollo: nodo serve

Utiliza el comando `nodo serve` para ejecutar el servicio de Nodo en entornos de desarrollo o cuando no se quiera utilizar el modo de servicio en segundo plano.

## Interfaz TUI

La interfaz TUI (Terminal User Interface) ofrece una manera gráfica de monitorear y gestionar los nodos y servicios directamente desde la terminal. Algunas funciones incluyen:

- **Navegación:**  
  - Flechas Izquierda/Derecha: Cambiar entre secciones.
  - Flechas Arriba/Abajo: Moverse entre filas.
- **Comandos Rápidos:**  
  - `o` y `p`: Rotar vistas en una sección.
  - `m`: Cambiar el layout de vista en bloque.
  - `c`: Conectar directamente a un par.

## Obteniendo Ayuda

Para ver un resumen de todos los comandos disponibles, simplemente ejecuta:

```
nodo
```
