# Settings of a start, from the caller

What the caller of `StartService` sets for one instance: for example the name that it gives to the instance, or the device that the node gives for an entry of `Container.devices`.

Class: `setting`.

## Places

`Configuration.policies`: a requirement for the node. A node that does not know a setting, or does not understand its value, refuses the start.

## Value

The formal of each setting gives the form of its value, its default when it is absent,
and its `place`. See `../REASON.md` for the rules of all the settings.

## Notes

These settings are of one start, not of the service: they are not in the id of the service.
