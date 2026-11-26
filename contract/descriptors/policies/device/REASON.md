# Settings of a device

When the node attaches a device (at the start or on request), the notice that the service needs before the node detaches it, the other entries that the device must be linked to, a bitstream to load.

Class: `setting`.

## Places

`Device.policies`: a requirement. With no setting, the node attaches the device before the instance starts, and the instance has it until it stops or hibernates.

## Value

The formal of each setting gives the form of its value, its default when it is absent,
and its `place`. See `../REASON.md` for the rules of all the settings.

## Notes

A value that names another entry of `Container.devices` gives its position in the list.
