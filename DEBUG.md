- packer-service se ha descargado como archivo, no tiene bloques, pero si que posee archivos Block(), por lo que o se almaceno en storage de forma incorrecta o se construyo de forma incorrecta.
- Por tanto, no es un problema del build.

- Integrity lee reader.read_multiblock y considera la hash correcta.

- Download no puede ser el problema porque Snake Game si que funciona y tiene bloques.
- Debe ser celaut-basics/packer-service