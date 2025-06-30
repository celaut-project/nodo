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
