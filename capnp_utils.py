import capnp
import mmap


def pointer_generator(
        file: str,
        capnp_class: capnp.lib.capnp._StructModule
    ) -> capnp.lib.capnp._DynamicStructReader:

    f = open(file, 'r+b')
    buf = mmap.mmap(
        f.fileno(),
        length=0,
        access=mmap.ACCESS_READ
    )
    return lambda: capnp_class.from_bytes(buf)


def test():
    from protos import service_capnp
    file_path = '/mnt/c/Users/josem/Documents/HyperNode/abbb77d3152baa967765be495f14b04f4f74c4b4bf281a1431690eb1ccf29ba0'
    return pointer_generator(file=file_path, capnp_class=service_capnp.ServiceWithMeta)