- packer-service se ha descargado como archivo, no tiene bloques, pero si que posee archivos Block(), por lo que o se almaceno en storage de forma incorrecta o se construyo de forma incorrecta.
- Por tanto, no es un problema del build.

- Integrity lee reader.read_multiblock y considera la hash correcta.

- Download no puede ser el problema porque Snake Game si que funciona y tiene bloques.
- Debe ser celaut-basics/packer-service,  aunque viendo el bloque:
"""
        def _blocks():
            yield Dir(dir=meta_path, _type=celaut_pb2.Metadata)
            yield Dir(dir=service_dir, _type=celaut_pb2.Service)

        out_file = write_to_file(
            path=work,
            file_name="service",
            extension="celaut.bee",
            input=_blocks(),
            indices={
                1: celaut_pb2.Metadata,
                2: celaut_pb2.Service,
            },
        )
        with open(out_file, "rb") as f:
            return f.read()
"""
debería estar agregado todos los bloques (no sus punteros), ¿porque termina aparentemente agregandonos los punteros en lugar del contenido de los bloques?

Repito: por mis pruebas, he visto que el problema no está en celaut-project/nodo si no en celaut-basics/packer-service o bien en la libreria bee-rpc que este utiliza.