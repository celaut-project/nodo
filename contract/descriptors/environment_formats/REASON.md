# Formats of environment variables

The form of the value of an environment variable that a service declares: for example
a decimal integer, a host and a port, a JSON text.

Class: `format`.

## Places

`DescriptorKeyValue.value` in `Service.Container.environment_variables`. A caller gives
the values in `Configuration.environment_variables`.

## Value

None: a format is a `Descriptor`. The formal says how a node or a caller checks a
value.
