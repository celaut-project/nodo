# Sources of a dependency

Where a party can get the full service of a dependency: for example a file in the
filesystem of the service that declares it, or a registry.

Class: `source`.

## Places

`Service.Workload.Dependency.sources`: an announcement. A node that does not know a
source ignores it, and it can get the service from a source that is not in the list.
A source never changes the service that the hashes name.

## Value

The location, in the form that the formal defines (for example a path in the
filesystem of the service).
