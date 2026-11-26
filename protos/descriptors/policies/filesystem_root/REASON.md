# Settings of the root filesystem

How the node mounts and reads the root filesystem of the container (the one that `Container.filesystem` holds): is it writable, does the tree carry metadata in the xattrs of each entry.

Class: `setting`.

## Places

`Service.Container.Filesystem.policies` of the root filesystem: a requirement. A subdirectory has no policies.

## Value

The formal of each setting gives the form of its value, its default when it is absent,
and its `place`. See `../REASON.md` for the rules of all the settings.

## Notes

The keys of the `xattrs` of an entry (mode, uid, gid, mtime, device) are defined by the setting `entry_metadata.descriptor`. Without it, a node reads none of them, and refuses a tree where an entry carries one of the keys. A node reads no other setting from `xattrs`. The files here came before `../../REASON.md` (rule 6.4).
