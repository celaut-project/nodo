# Device architectures

What the software of a service programs against in a device: for example a family of
GPUs with one compute capability, a USB video class camera, one model of robot arm.
The model of a device that the software does not need is a fact (`../../facts/`), not
a part of the architecture.

Class: `architecture`.

## Places

- `Device.architecture`: a requirement. A node attaches a device whose architecture
  has the same formal.
- `ArchitectureResources.architecture` in `Peer.devices` and in `AttachedDevice`: an
  announcement of the node, one entry for each device as the guest sees it.

## Value

None: an architecture is a `Descriptor`. One descriptor for each member of a family.

## Notes

- A device is a thing of the node that the node gives to one instance at a time,
  whatever the bus (PCI, USB, a network of the node). A thing whose access the node
  does not control is a network (`../../networks/`).
