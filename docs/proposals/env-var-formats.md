# A limited set of values for an environment variable

Design note. It records an idea for the future. The node does **not** check the value of
an environment variable today. The structure to describe the allowed values exists in the
protocol, but nothing defines its content.

## The problem

A service declares the environment variables that it needs, and the caller gives the
values (`nodo execute -e <name> <value>`). Some variables accept only some values:

- only an integer, or only a number;
- a string that matches a regular expression;
- one value from a short list (a discrete set).

Today the service must check its own variables, and the caller learns the rule only from
the prose of the service, if there is one. A wrong value is found late, after the
instance starts.

## What exists now

- `Service.Container.environment_variables` is `repeated DataFormatKeyValue`
  (`contract/celaut.proto`). Each variable has a name and a `DataFormat`.
- `DataFormat` has three fields: `tags` (names, for a reader), `prose` (a text for a
  reader) and `formal` (the format "in a form that a program can read").
- The packer writes the names from `envs` in `service.json`. It sets an **empty**
  `DataFormat` for each name (`src/packers/zip_with_dockerfile.py`). The text of
  [`PACKING.md`](../PACKING.md#envs) says that per-variable descriptors are not embedded.
- The caller gives the values in `Configuration.environment_variables`. The node passes
  them to the instance as they are. It does not read the `formal` of a variable.
  `nodo service_envs` shows only `tags` and `prose`.
- The `environment_variables` of the container is part of the specification, so it is
  part of the service id. A change of a rule gives a new id.

The place for the rule exists: the field `formal`. The content of the field does not.
The protocol does not say what a program must find in `formal`, so two implementations
cannot agree.

## Proposal

Define the content of `formal` for an environment variable. Do not change
`celaut.proto`.

The repository already has the pattern. For a `Policy` and for a payment protocol, a
setting is identified **only** by the exact bytes of its `formal`. A published `formal`
never changes. A new meaning is a new `formal`. See [`policies/`](../policies/README.md)
and [`payment_protocols/`](../payment_protocols/README.md).

Use the same pattern here. Each kind of value is one file `<name>.formal` in a new
folder, and the node holds the same bytes. A test pins the bytes to the files.

| Kind | Allowed value | Parameters |
|---|---|---|
| `integer` | `-?[0-9]+` | optional `min` and `max` |
| `number` | a decimal number | optional `min` and `max` |
| `regex` | a string that matches the expression | `pattern` |
| `enum` | one value of a list | `values`, sorted |

The `tags` give the name of the kind, for a reader. The `prose` explains the rule. The
parameters are `key=value` lines, sorted by key, in UTF-8. This is the same form as the
other `formal` fields of the protocol.

An example for a variable `RETRIES`: `tags` is `["integer"]`, and the parameters are
`max=10` and `min=0`.

## Why no change of the protocol

The three fields of `DataFormat` are enough. A new kind of value is a new `formal`, not a
new field. This also follows the rule in `celaut.proto` that a new primitive must not need
a change of the wire format.

A change of the protocol is only needed for a structural item that `DataFormat` cannot
hold. Examples: a default value, or a flag that says the variable is required. These are
a separate decision.

## Open questions

Each question must have an answer before the node enforces any of this.

- **A node that does not know the `formal`.** For a `Policy` the node does not run the
  service. This is the safe rule: an old node never opens what the author closed. The
  other option is to ignore the rule and trust the service. Decide which one applies to a
  variable.
- **Where to check.** The options are the client (`nodo execute`), the node at launch,
  or both. A check only in the client does not stop a caller that uses gRPC directly.
- **The dialect of the regular expression.** Choose one (for example RE2, which has no
  backtracking). Set a limit on the size of the pattern and on the time to match. A
  pattern that hangs the node is a risk.
- **The value of a variable in the child services.** A parent can give values to a child.
  The check must apply on this path too.
- **Values with a secret.** A rule that shows the allowed values (`enum`) is public,
  because it is in the specification. A rule must not be used to hide a secret.
- **Names with a special shape.** The node passes a name to the process only if it has a
  safe shape (`src/utils/guest_env.py`). The rule of the value does not change this
  filter.
- **The packer.** `service.json` needs a way to write the rule for each variable. Decide
  the syntax, and decide whether the old list `envs` stays valid.

## Work for the future

1. Define the `formal` of each kind and put the files in `docs/`.
2. Add the code that reads and checks a `formal`, with a test that pins the bytes.
3. Let the packer write the rule of each variable.
4. Check the value in `nodo execute`, then in the node at launch.
5. Show the rule in `nodo service_envs`.
6. Write the user documentation in [`PACKING.md`](../PACKING.md) and link it here.
