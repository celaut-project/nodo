import os
from typing import Optional, Generator, Set, Tuple

from grpcbigbuffer import client as grpcbf, buffer_pb2
from grpcbigbuffer.block_driver import WITHOUT_BLOCK_POINTERS_FILE_NAME

from protos import celaut_pb2 as celaut
from protos import gateway_pb2
from protos.gateway_pb2_grpcbf import StartService_input_indices, \
    StartService_input_message_mode
from src.gateway.launch_service import launch_service
from src.gateway.utils import save_service
from src.manager.manager import could_ve_this_sysreq
from src.manager.resources_manager import mem_manager
from src.utils import logger as l
from src.utils.env import SHA3_256_ID, \
    REGISTRY
from src.utils.utils import from_gas_amount, get_only_the_ip_from_context, read_file

CONFIGURATION_REQUIRED = False  # TODO añadir como variable de entorno. Por si el nodo debe de ser mas estricto.


def get_from_registry(service_hash: str) -> gateway_pb2.ServiceWithMeta:
    l.LOGGER('Getting ' + service_hash + ' service from the local registry.')
    filename: str = REGISTRY + service_hash
    if not os.path.exists(filename):
        return None

    if os.path.isdir(filename):
        filename = filename + '/' + WITHOUT_BLOCK_POINTERS_FILE_NAME
    try:
        with mem_manager(2 * os.path.getsize(filename)) as iolock:
            service_with_meta = gateway_pb2.ServiceWithMeta()
            service_with_meta.ParseFromString(read_file(filename=filename))
            return service_with_meta
    except (IOError, FileNotFoundError):
        l.LOGGER('The service was not on registry.')
        return None


def find_service_hash(_hash: gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash) \
        -> Tuple[Optional[gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash], bool]:
    return _hash, _hash.value.hex() in [s for s in os.listdir(REGISTRY)] if SHA3_256_ID == _hash.type else (None, False)


class Hash:
    def __init__(self, _hash: gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash):
        self.type = _hash.type
        self.value = _hash.value

    def __hash__(self):
        return hash((self.type, self.value))

    def __eq__(self, other):
        return (self.type, self.value) == (other.type, other.value)

    def proto(self) -> gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash:
        return gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash(
            type=self.type,
            value=self.value
        )


def start_service(request_iterator, context) -> Generator[buffer_pb2.Buffer, None, None]:
    l.LOGGER('Starting service by ' + str(context.peer()) + ' ...')
    configuration: Optional[celaut.Configuration] = None
    system_requeriments = None
    initial_gas_amount = None
    max_sysreq = None

    client_id = None
    recursion_guard_token = None

    service_hash: Optional[gateway_pb2.celaut__pb2.Any.Metadata.HashTag.Hash] = None
    service_saved = False

    hashes: Set[Hash] = set()
    metadata: Optional[gateway_pb2.celaut__pb2.Any.Metadata] = None

    parser_generator = grpcbf.parse_from_buffer(
        request_iterator=request_iterator,
        indices=StartService_input_indices,
        partitions_message_mode=StartService_input_message_mode
    )
    while True:
        try:
            r = next(parser_generator)
            l.LOGGER('parse generator next -> ' + str(type(r)) + ': ' + str(r))
        except StopIteration:
            break

        match type(r):
            case gateway_pb2.Client:
                client_id = r.client_id

            case gateway_pb2.RecursionGuard:
                recursion_guard_token = r.token

            case gateway_pb2.Configuration:
                configuration = r.config

                if r.HasField('max_sysreq') and not could_ve_this_sysreq(sysreq=r.max_sysreq):
                    raise Exception("The node can't execute the service with this requeriments.")
                else:
                    max_sysreq = r.max_sysreq

                if r.HasField('min_sysreq'):
                    system_requeriments = r.min_sysreq

                if r.HasField('initial_gas_amount'):
                    initial_gas_amount = from_gas_amount(r.initial_gas_amount)

            case celaut.Any.Metadata.HashTag.Hash:
                hashes.add(Hash(r))
                if not service_hash:
                    service_hash, service_saved = find_service_hash(_hash=r)

            case celaut.Any.Metadata:

                if service_hash:
                    for _hash in metadata.Any.HashTag.Hash:  # TODO nos podríamos ahorrar esta iteración
                        if not service_hash:
                            service_hash, service_saved = find_service_hash(_hash=_hash)
                        # TODO se podría realizar junto con la iteració siguiente:

                metadata = r
                hashes: Set[hashes] = hashes.union({
                    Hash(_e) for _e in metadata.Any.HashTag.Hash
                })
                del metadata.Any.HashTag.Hash
                metadata.Any.HashTag.Hash.extend([_e.proto() for _e in hashes])
                hashes.clear()

            case grpcbf.Dir:
                if r.type != gateway_pb2.celaut__pb2.Service:
                    raise Exception('Incorrect service message.')

                # NO TIENE SENTIDO USAR AQUI EL DUPLICATE GRABBER YA QUE AHORA NO RETORNAMOS PRIMERO EL TIPO, SI NO QUE
                #  RETORNAMOS EL TIPO JUNTO CON EL DIRECTORIO DEL SERVICIO YA DESCARGADO. EL DUPLICATE GRABBER DEBERIA
                #  USARSE ANTES.

                l.LOGGER('Save service on disk')
                # Take it from metadata.
                if not service_hash:
                    # TODO  compute the hash of r.dir.
                    raise Exception("Not registry hash.")

                service_saved = save_service(
                    service_with_meta_dir=r.dir,
                    service_hash=service_hash
                )

        if service_saved:
            if CONFIGURATION_REQUIRED and not configuration:
                raise Exception("Client or configuration ")

            l.LOGGER('Launch service with configuration')
            yield from grpcbf.serialize_to_buffer(
                indices={},
                message_iterator=launch_service(
                    service=get_from_registry(service_hash=service_hash),
                    metadata=metadata,
                    config=configuration,
                    system_requirements=system_requeriments,
                    max_sysreq=max_sysreq,
                    initial_gas_amount=initial_gas_amount,
                    service_id=service_hash,
                    father_ip=get_only_the_ip_from_context(context_peer=context.peer()),
                    father_id=client_id,
                    recursion_guard_token=recursion_guard_token
                )
            )
            return

    l.LOGGER('The service is not in the registry and the request does not have the definition.' \
             + str([(h.type.hex(), h.value.hex()) for h in hashes]))
