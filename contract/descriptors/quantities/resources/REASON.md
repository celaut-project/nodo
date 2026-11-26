# Resources

A thing that an instance consumes: memory, disk, CPU, memory of a device.

Class: `resource`.

## Places

- `Resources.at_init` and `Resources.at_most` of a container or of a device, the
  resources of a request (`ModifyServiceSystemResourcesInput`, `DeviceChange`,
  `GetResourceAvailability`): requirements. A node refuses an unknown resource.
- `ArchitectureResources.resources` (`Peer.resources`, `Peer.devices`,
  `AttachedDevice`), `StartReceipt.reserved`, `InstanceState.resources`: announcements
  or reports.

## Value

An integer in the unit of the formal. Select a unit that makes the values integers
(for example a thousandth of a core). A quantity with no value says nothing: it is not
0.

## The formal must give

`unit`, and how the node counts and enforces the resource (`measure`): a total for the
instance or per core, a peak or an average.
