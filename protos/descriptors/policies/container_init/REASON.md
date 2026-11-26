# Settings of the start of a process

How the node starts a process of the service, how it continues one on a state that it kept (after a hibernation), where the service reads the notices of the node, and the guest kernel that the instance boots.

Class: `setting`.

## Places

`Service.Container.Init.policies`: a requirement for the node that runs the service.

## Value

The formal of each setting gives the form of its value, its default when it is absent,
and its `place`. See `../REASON.md` for the rules of all the settings.

## Notes

No file yet. Until a setting is defined here, a node refuses any policy in this place.
