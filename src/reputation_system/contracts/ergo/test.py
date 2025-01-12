import hashlib
import base58

# Dirección Ergo inicial
ergo_address = "9hV5btmWVVr8sAfp3im1A8Zf8Akd2HsJtRkUiVCvEwrEUuo38V7"

# Llave pública comprimida extraída del registro R7
public_key_hex = "038696f0bfa01ecf1244ae08579cbe486cf755d892de754cb674179bb3293b79c0"

# Paso 1: Convertir la llave pública comprimida en el formato base de la dirección Ergo
# Agregar el byte de red (0x00 para mainnet)
network_byte = b"\x00"
public_key_bytes = bytes.fromhex(public_key_hex)
address_bytes = network_byte + public_key_bytes

# Paso 2: Calcular el checksum
# El checksum es los primeros 4 bytes del doble SHA256
checksum = hashlib.sha256(hashlib.sha256(address_bytes).digest()).digest()[:4]

# Paso 3: Combinar los datos para formar la dirección
full_address_bytes = address_bytes + checksum

# Codificar en Base58
generated_address = base58.b58encode(full_address_bytes).decode()

# Comparar la dirección generada con la original
print("Dirección generada:", generated_address)
print("Dirección original:", ergo_address)
print("¿Corresponde?", generated_address == ergo_address)




""".ropeproject\/

En Appkit. Estoy agregando una dirección publica tal que "9hV5btmWVVr8sAfp3im1A8Zf8Akd2HsJtRkUiVCvEwrEUuo38V7" en el registro R7 de una caja. Para ello hago: sender_address: Address = "9hV5btmWVVr8sAfp3im1A8Zf8Akd2HsJtRkUiVCvEwrEUuo38V7" p2pkAddres = sender_address.asP2PK() sender_address_proposition = p2pkAddres.pubkey() Despues de subir la transacción, en el registro R7 tengo el valor 038696f0bfa01ecf1244ae08579cbe486cf755d892de754cb674179bb3293b79c0 Me gustaría poder verificar que la dirección 9hV5btmWVVr8sAfp3im1A8Zf8Akd2HsJtRkUiVCvEwrEUuo38V7 es la que se encuentra en el registro. ¿Que transformaciones ocurren para pasar de un valor a otro?EditTe ayudo a entender las transformaciones que ocurren entre la dirección P2PK de Ergo y la llave pública almacenada en el registro.
Conversión de direcciones ErgoClick to open code
Las transformaciones que ocurren son:

La dirección Ergo 9hV5btmWVVr8sAfp3im1A8Zf8Akd2HsJtRkUiVCvEwrEUuo38V7 está codificada en Base58 e incluye:

Un byte de red (0x00 para mainnet)
La llave pública comprimida (33 bytes)
Un checksum (4 bytes)


Cuando llamas a asP2PK(), se decodifica el Base58 y se extrae la llave pública.
Al llamar a pubkey(), obtienes el ProveDlog que contiene la llave pública en formato hexadecimal.
El valor que ves en R7 (038696f0bfa01ecf1244ae08579cbe486cf755d892de754cb674179bb3293b79c0) es la representación hexadecimal de la llave pública comprimida (33 bytes).

Para verificar que corresponden a la misma dirección, puedes:

Tomar el valor del registro R7
Agregarle el byte de red (0x00) al principio
Calcular el checksum (doble SHA256 de los primeros 34 bytes y tomar los primeros 4 bytes del resultado)
Codificar todo en Base58

¿Te gustaría que te muestre cómo hacer esta verificación en código?


En python. Ayudame a entender todo esto
ChatGPT Plus
ChatGPT
Claro, te voy a guiar paso a paso para que entiendas todo el proceso en Python. Vamos a realizar todas las transformaciones necesarias para verificar que la llave pública almacenada en el registro R7 corresponde a la dirección Ergo inicial.

Resumen de pasos
Convertir el registro R7 al formato base de una dirección Ergo:

Extraer la llave pública comprimida del registro R7.
Agregar el byte de red (0x00 para mainnet) al inicio.
Calcular el checksum.
Codificar en Base58.
Comparar la dirección generada con la dirección inicial.

Código en Python
Primero, necesitamos importar las bibliotecas necesarias:

python
Copiar código
import hashlib
import base58
Aquí está el código completo:

python
Copiar código
# Dirección Ergo inicial
ergo_address = "9hV5btmWVVr8sAfp3im1A8Zf8Akd2HsJtRkUiVCvEwrEUuo38V7"

# Llave pública comprimida extraída del registro R7
public_key_hex = "038696f0bfa01ecf1244ae08579cbe486cf755d892de754cb674179bb3293b79c0"

# Paso 1: Convertir la llave pública comprimida en el formato base de la dirección Ergo
# Agregar el byte de red (0x00 para mainnet)
network_byte = b"\x00"
public_key_bytes = bytes.fromhex(public_key_hex)
address_bytes = network_byte + public_key_bytes

# Paso 2: Calcular el checksum
# El checksum es los primeros 4 bytes del doble SHA256
checksum = hashlib.sha256(hashlib.sha256(address_bytes).digest()).digest()[:4]

# Paso 3: Combinar los datos para formar la dirección
full_address_bytes = address_bytes + checksum

# Codificar en Base58
generated_address = base58.b58encode(full_address_bytes).decode()

# Comparar la dirección generada con la original
print("Dirección generada:", generated_address)
print("Dirección original:", ergo_address)
print("¿Corresponde?", generated_address == ergo_address)
Explicación paso a paso
Llave pública comprimida:

La llave pública comprimida almacenada en el registro R7 es 038696f0bfa01ecf1244ae08579cbe486cf755d892de754cb674179bb3293b79c0.
Byte de red:

Para mainnet, el byte de red es 0x00. Esto se concatena al inicio de la llave pública comprimida.
Checksum:

Se calcula el doble SHA256 de los primeros 34 bytes (network_byte + public_key_bytes).
Luego, se toman los primeros 4 bytes del resultado para el checksum.
Codificación en Base58:

Los 34 bytes (byte de red + llave pública comprimida) más los 4 bytes del checksum se codifican en Base58 para obtener la dirección Ergo.

"""