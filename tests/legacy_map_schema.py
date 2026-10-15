"""The schema as it was before ``map<>`` became ``repeated *KeyValue`` -- for tests.

Both schemas cannot be imported as ``celaut.proto`` into one protobuf pool, so the old
one is rebuilt here, in a private pool, *from the current descriptors*: every field of
type ``*KeyValue`` is turned back into the ``map<string, V>`` it replaced (same message,
same field number and name, protobuf's own ``map_entry`` nested type). What comes out
serializes byte-for-byte like the real ``celaut_pb2.py`` of origin/dev before the change
-- ``tests/test_keyvalue_wire.py`` also pins goldens produced by that real module, which
is what shows this reconstruction is faithful.

This module is not a test; nothing in the node imports it.
"""
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

from bee_rpc import buffer_pb2
from protos import celaut_pb2, pack_pb2

KV_TYPES = (celaut_pb2.BytesKeyValue, celaut_pb2.AmountKeyValue,
            celaut_pb2.DataFormatKeyValue, celaut_pb2.Uint64KeyValue)


def build():
    """``(pool, maps)``: the legacy pool and one ``(package, message, field, number)``
    per map field it restored."""
    pool = descriptor_pool.DescriptorPool()
    buf = descriptor_pb2.FileDescriptorProto()
    buffer_pb2.DESCRIPTOR.CopyToProto(buf)
    pool.Add(buf)

    kv = {}
    for cls in KV_TYPES:
        value = cls.DESCRIPTOR.fields_by_name["value"]
        kv["." + cls.DESCRIPTOR.full_name] = (
            value.type, "." + value.message_type.full_name if value.message_type else None)

    def remap(name):
        for old in ("celaut", "pack"):
            if name.startswith("." + old + "."):
                return ".legacy_" + old + name[len(old) + 1:]
        return name

    maps = []

    def walk(msg, package, path):
        here = path + [msg.name]
        for f in list(msg.field):
            if not f.type_name:
                continue
            if f.type_name in kv:
                value_type, value_type_name = kv[f.type_name]
                entry = msg.nested_type.add()
                entry.name = "".join(p.capitalize() for p in f.name.split("_")) + "Entry"
                entry.options.map_entry = True
                key = entry.field.add(); key.name = "key"; key.number = 1
                key.type = key.TYPE_STRING; key.label = key.LABEL_OPTIONAL
                val = entry.field.add(); val.name = "value"; val.number = 2
                val.type = value_type; val.label = val.LABEL_OPTIONAL
                if value_type_name:
                    val.type_name = remap(value_type_name)
                f.type_name = "." + package + "." + ".".join(here) + "." + entry.name
                f.label = f.LABEL_REPEATED
                maps.append((package, ".".join(here), f.name, f.number))
            else:
                f.type_name = remap(f.type_name)
        for nested in msg.nested_type:
            if not nested.options.map_entry:
                walk(nested, package, here)

    files = {}
    for mod in (celaut_pb2, pack_pb2):
        fdp = descriptor_pb2.FileDescriptorProto()
        mod.DESCRIPTOR.CopyToProto(fdp)
        original_package = fdp.package
        fdp.name = "legacy_" + fdp.name
        fdp.package = "legacy_" + original_package
        del fdp.service[:]
        fdp.dependency[:] = ["legacy_" + d if d in ("celaut.proto",) else d for d in fdp.dependency]
        drop = [m for m in fdp.message_type if "." + original_package + "." + m.name in kv]
        for m in drop:
            fdp.message_type.remove(m)
        for m in fdp.message_type:
            walk(m, fdp.package, [])
        files[original_package] = fdp
    pool.Add(files["celaut"])
    pool.Add(files["pack"])
    _retain_classes(pool, ("legacy_celaut.proto", "legacy_pack.proto"))
    return pool, maps


# Every message class of every pool `build` made. upb (protobuf 4.x) creates the class
# of a nested message type lazily, the first time a field of that type is touched, and
# holds it only weakly: the class sits in a reference cycle with nothing outside it, so
# the next garbage collection frees it while an instance of it is still alive -- and
# the first ListFields/MessageToJson that reaches that instance reads freed memory and
# segfaults. Pre-existing and latent; it surfaced deterministically once the schema
# grew enough for a collection to land inside a test (#459). Creating every class up
# front and keeping it here makes each one outlive the pool's instances.
_CLASSES = []


def _retain_classes(pool, file_names):
    def visit(descriptor):
        _CLASSES.append(message_factory.GetMessageClass(descriptor))
        for nested in descriptor.nested_types:
            visit(nested)

    for name in file_names:
        for descriptor in pool.FindFileByName(name).message_types_by_name.values():
            visit(descriptor)


def message_class(pool, full_name):
    return message_factory.GetMessageClass(pool.FindMessageTypeByName(full_name))
