
# Creación

## 1️⃣ Crear carpeta para la distro

```powershell
mkdir C:\WSL\nodo-wsl
mkdir C:\WSL\nodo-wsl\rootfs
cd C:\WSL\nodo-wsl
```

---

## 2️⃣ Descargar rootfs Debian minimal

```powershell
curl -L -o rootfs.tar.gz https://deb.debian.org/debian/dists/bookworm/main/installer-amd64/current/images/netboot/debian-installer/amd64/root.tar.gz
```

---

## 3️⃣ Extraer el rootfs

```powershell
tar -xzf rootfs.tar.gz -C rootfs
```

---

## 4️⃣ Crear `/boot` dentro del rootfs

```powershell
mkdir rootfs\boot
cd rootfs\boot
```

---

## 5️⃣ Descargar kernel (`vmlinuz`) e initramfs (`initrd.img`)

* Para ejemplo usamos kernel de Ubuntu mainline:

```powershell
curl -L -o vmlinuz https://kernel.ubuntu.com/~kernel-ppa/mainline/v6.6.1/amd64/linux-image-6.6.1-amd64
curl -L -o initrd.img https://kernel.ubuntu.com/~kernel-ppa/mainline/v6.6.1/amd64/linux-initrd.img-6.6.1-amd64
```

> Ahora tienes `/boot/vmlinuz` y `/boot/initrd.img` dentro de tu rootfs.

---

## 6️⃣ Empaquetar rootfs para WSL

```powershell
cd C:\WSL\nodo-wsl
tar -C rootfs -czf rootfs-wsl.tar .
```

---

## 7️⃣ Registrar la distro en WSL

```powershell
wsl --import nodo-wsl C:\WSL\nodo-wsl rootfs-wsl.tar
```

* `nodo-wsl` → nombre de la distro
* `C:\WSL\nodo-wsl` → carpeta de instalación
* `rootfs-wsl.tar` → archivo tar del rootfs

---

## 8️⃣ Entrar en la distro y crear usuario rootless

```powershell
wsl -d nodo-wsl
```

Dentro de la distro:

```bash
adduser --disabled-password --gecos '' nodo
usermod -aG sudo nodo
exit
```

---

## 9️⃣ Configurar la distro para iniciar como `nodo`

Crear archivo `wsl.conf` en la carpeta de instalación de la distro (`C:\WSL\nodo-wsl\wsl.conf`):

```ini
[user]
default=nodo
```

---

## 10️⃣ Verificar `/boot` y usuario

Volver a entrar:

```powershell
wsl -d nodo-wsl
```

Dentro:

```bash
whoami        # Debe mostrar: nodo
ls /boot      # Debe mostrar: vmlinuz  initrd.img
```

---

Con esto ya tienes:

* Una distro Debian llamada **nodo-wsl**
* Usuario **nodo**, rootless con sudo
* `/boot` con `vmlinuz` e `initrd.img` para usar con **Cloud-Hypervisor**

---



# Distribución

Perfecto, vamos a centrarnos en la **Opción A: crear un paquete `.appx` / `.msixbundle` de tu distro WSL**, que permite al usuario instalarla **con un doble clic**, sin usar la línea de comandos. Te doy la guía completa paso a paso.

---

## **Guía para crear un paquete Appx de una distro WSL**

### **1. Preparar tu distro WSL**

1. Instala y configura la distro base (por ejemplo Ubuntu) en WSL.
2. Personaliza lo que quieras: paquetes, scripts, configuraciones.
3. Exporta la distro a un archivo `.tar` (solo para ti, en tu máquina):

```powershell
wsl --export Ubuntu custom-distro.tar
```

* `Ubuntu` es el nombre de tu distro.
* `custom-distro.tar` es el archivo que usarás para crear el Appx.

---

### **2. Descargar WSL Distro Launcher**

* Microsoft proporciona una plantilla para crear paquetes WSL:
  [WSL Distro Launcher GitHub](https://github.com/microsoft/WSL-DistroLauncher)

1. Clona o descarga el repositorio.
2. Dentro encontrarás un proyecto de **Visual Studio** (`DistroLauncher.sln`) que sirve como base para tu paquete Appx.

---

### **3. Reemplazar la imagen de la distro**

1. Dentro del proyecto de Visual Studio hay una carpeta llamada `rootfs`.
2. Copia tu `custom-distro.tar` allí, reemplazando el ejemplo que viene con la plantilla.
3. Modifica `DistroLauncher.vcxproj` o los archivos de configuración si quieres cambiar:

   * Nombre de la distro.
   * ID del paquete.
   * Icono y metadatos.

> Esto controla cómo aparecerá la aplicación en el menú inicio y en la tienda si decides publicarla.

---

### **4. Compilar el paquete Appx**

1. Abre el proyecto `DistroLauncher.sln` en **Visual Studio 2019 o 2022**.
2. Cambia la configuración a **Release > x64** (u otra arquitectura según tu distro).
3. Ve a **Proyecto → Publicar → Crear paquete Appx**.
4. Marca **No, no quiero subir al Microsoft Store**.
5. Define la carpeta donde se guardará el paquete `.appx` o `.msixbundle`.
6. Visual Studio compilará el paquete listo para distribución.

---

### **5. Distribuir el paquete**

* El usuario solo necesita:

  1. Hacer doble clic en el `.appx` o `.msixbundle`.
  2. Windows instalará la distro WSL automáticamente.
  3. Aparecerá en el menú inicio como una aplicación normal (por ejemplo: “Custom Ubuntu”).

> Opción extra: puedes incluir un **icono personalizado** y nombre de la distro amigable en la interfaz de Windows.

---

### **6. Notas importantes**

* Esta forma funciona para **WSL2**.
* Si el usuario no tiene WSL habilitado, el paquete le indicará que lo habilite.
* Puedes distribuir el paquete por web, USB o red corporativa.
* No requiere scripts ni terminal para el usuario final.
