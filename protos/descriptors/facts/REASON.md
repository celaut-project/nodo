# Facts

What a node attests about a device or about a start: the model of a device, a stable
id that a caller can select, where the device is, the devices that it is linked to,
the guest kernel that an instance boots.

Class: `fact`.

## Places

- `ArchitectureResources.facts` (`Peer.resources`, `Peer.devices`, `AttachedDevice`):
  an announcement.
- `StartReceipt.facts`: an announcement that the node signs.

A reader keeps a fact that it does not know, and does not use it.

## Value

The formal gives the form. A group of members is one fact and a value (for example
the PCI model `10de:2330`), not one descriptor for each member. A list of `Policy`
has no repeated formal, so many members of one start (the models of two devices) are
one fact whose value is the list.

## The formal must give

`place`, and the form of the value.
