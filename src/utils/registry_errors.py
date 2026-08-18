"""Why a service spec could not be handed back from the local registry.

``read_service_from_disk`` returns ``Optional[celaut.Service]``, and that ``None``
collapses two different situations: "this node does not store that spec" and
"this node could not load it right now". A caller that only reports a miss can
live with the collapse; a caller that makes an authorization decision cannot.
Reading a transient load failure as absence is what let Service.Network gating
degrade to allow-all under memory pressure (#269).

These types keep the two apart. They live in their own module, free of any
runtime dependency, so a caller can name the failure it cares about without
importing the registry loader's stack.
"""


class ServiceRegistryError(Exception):
    """A service spec was asked of the local registry and not returned."""


class ServiceNotInRegistry(ServiceRegistryError):
    """The spec is not stored on this node.

    Nothing to retry: it was never here. Whether that is legitimate depends on
    the caller -- a spec may simply never have been stored locally, but for a
    service this node itself launched it means the registry lost a spec it is
    supposed to hold.
    """


class ServiceSpecUnavailable(ServiceRegistryError):
    """The spec is stored here but was not loadable right now.

    Raised for the memory-lock timeout and for I/O errors: conditions that depend
    on how loaded the node is, not on what it holds, so a later attempt may well
    succeed. Until one does, the node knows nothing about the service -- which is
    not the same as the service declaring nothing.
    """
