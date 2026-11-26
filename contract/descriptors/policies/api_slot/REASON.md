# Settings of a slot of the API

How a third party reaches one port of the service, how the tunnel finds the instance, and if a connection wakes a hibernated instance.

Class: `setting`.

## Places

`Service.Api.Slot.policies`: a requirement. A slot that does not set a setting has its default.

## Value

The formal of each setting gives the form of its value, its default when it is absent,
and its `place`. See `../REASON.md` for the rules of all the settings.

## Notes

A node refuses a service that has `wake=on_tunnel` or `tunnel_auth=address` on a slot with `access=direct`. How the tunnel uses them is in `docs/TUNNELING.md`. The files here came before `../../REASON.md` (rule 6.4).
