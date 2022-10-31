import grpcbigbuffer as grpcbf
import docker as docker_lib
from hashlib import sha256
from src.builder import build
import grpc, os, subprocess

from src.gateway.utils import generate_gateway_instance
from src.gateway.service_balancer import service_balancer

from src.manager.manager import default_initial_cost, spend_gas, \
    generate_client_id_in_other_peer, start_service_cost, add_container
from src.manager.system_cache import SystemCache
from src.manager.metrics import gas_amount_on_other_peer
from src.manager.payment_process import increase_deposit_on_peer

from src.utils import utils, logger as l
from src.utils.env import HYCACHE, DOCKER_CLIENT, IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER, DOCKER_NETWORK, \
    DEFAULT_SYSTEM_RESOURCES, GAS_COST_FACTOR
from src.utils.recursion_guard import RecursionGuard

from protos import celaut_pb2 as celaut, gateway_pb2, gateway_pb2_grpc
from protos.gateway_pb2_grpcbf import StartService_input, StartService_input_partitions_v2

def set_config(container_id: str, config: celaut.Configuration, resources: celaut.Sysresources,
               api: celaut.Service.Container.Config):
    __config__ = celaut.ConfigurationFile()
    __config__.gateway.CopyFrom(generate_gateway_instance(network=DOCKER_NETWORK).instance)
    if config: __config__.config.CopyFrom(config)
    if resources: __config__.initial_sysresources.CopyFrom(resources)

    os.mkdir(HYCACHE + container_id)
    # TODO: Check if api.format is valid or make the serializer for it.

    with open(HYCACHE + container_id + '/__config__', 'wb') as file:
        file.write(__config__.SerializeToString())
    while 1:
        try:
            subprocess.run(
                '/usr/bin/docker cp ' + HYCACHE + container_id + '/__config__ ' + container_id + ':/' + '/'.join(
                    api.path),
                shell=True
            )
            break
        except subprocess.CalledProcessError as e:
            l.LOGGER(e.output)
    os.remove(HYCACHE + container_id + '/__config__')
    os.rmdir(HYCACHE + container_id)

def create_container(id: str, entrypoint: list, use_other_ports=None) -> docker_lib.models.containers.Container:
    try:
        result = DOCKER_CLIENT().containers.create(
            image=id + '.docker',  # https://github.com/moby/moby/issues/20972#issuecomment-193381422
            entrypoint=' '.join(entrypoint),
            ports=use_other_ports
        )
        return result
    except docker_lib.errors.ImageNotFound as e:
        l.LOGGER('CONTAINER IMAGE NOT FOUND')
        # TODO build(id) using agents model.
        raise e
    except Exception as e:
        l.LOGGER('DOCKER RUN ERROR -> ' + str(e))
        raise e

def launch_service(
        service_buffer: bytes,
        metadata: celaut.Any.Metadata,
        father_ip: str,
        father_id: str = None,
        service_id: str = None,
        system_requirements: celaut.Sysresources = None,
        max_sysreq=None,
        config: celaut.Configuration = None,
        initial_gas_amount: int = None,
        recursion_guard_token: str = None,
) -> gateway_pb2.Instance:
    l.LOGGER('Go to launch a service. ')
    if service_buffer is None: raise Exception("Service object can't be None")

    with RecursionGuard(
            token=recursion_guard_token,
            generate=bool(father_id)  # Use only if is from outside.
    ) as recursion_guard_token:

        if not father_id:
            father_id = father_ip
        if father_ip == father_id and utils.get_network_name(father_ip) != DOCKER_NETWORK:
            raise Exception('Client id not provided.')

        initial_gas_amount: int = initial_gas_amount if initial_gas_amount else default_initial_cost(
            father_id=father_id)
        getting_container = False  # Here it asks the balancer if it should assign the job to a peer.

        while True:
            abort_it = True
            for peer, cost in service_balancer(
                    service_buffer=service_buffer,
                    metadata=metadata,
                    ignore_network=utils.get_network_name(
                        ip_or_uri=father_ip
                    ) if IGNORE_FATHER_NETWORK_ON_SERVICE_BALANCER else None,
                    initial_gas_amount=initial_gas_amount,
                    recursion_guard_token=recursion_guard_token
            ).items():
                if abort_it: abort_it = False
                l.LOGGER('Balancer select peer ' + str(peer) + ' with cost ' + str(cost))

                # Delegate the service instance execution.
                if peer != 'local':
                    try:
                        l.LOGGER('El servicio se lanza en el nodo ' + str(peer))
                        refound_gas = []

                        if not spend_gas(
                                token_or_container_ip=father_id,
                                gas_to_spend=cost,
                                refund_gas_function_container=refound_gas
                        ): raise Exception('Launch service error spending gas for ' + father_id)

                        if gas_amount_on_other_peer(
                                peer_id=peer,
                        ) <= cost and not increase_deposit_on_peer(
                            peer_id=peer,
                            amount=cost
                        ):  raise Exception(
                            'Launch service error increasing deposit on ' + peer + ' when it didn\'t have enough gas.')

                        l.LOGGER('Spended gas, go to launch the service on ' + str(peer))
                        service_instance = next(grpcbf.client_grpc(
                            method=gateway_pb2_grpc.GatewayStub(
                                grpc.insecure_channel(
                                    next(utils.generate_uris_by_peer_id(peer))
                                )
                            ).StartService,  # TODO se debe hacer que al pedir un servicio exista un timeout.
                            partitions_message_mode_parser=True,
                            indices_serializer=StartService_input,
                            partitions_serializer=StartService_input_partitions_v2,
                            indices_parser=gateway_pb2.Instance,
                            input=utils.service_extended(
                                service_buffer=service_buffer,
                                metadata=metadata,
                                config=config,
                                min_sysreq=system_requirements,
                                max_sysreq=max_sysreq,
                                initial_gas_amount=initial_gas_amount,
                                client_id=generate_client_id_in_other_peer(peer_id=peer),
                                recursion_guard_token=recursion_guard_token
                            )
                        ))
                        encrypted_external_token: str = sha256(service_instance.token.encode('utf-8')).hexdigest()
                        SystemCache().set_external_on_cache(
                            agent_id=father_id,
                            peer_id=peer,  # Add node_uri.
                            encrypted_external_token=encrypted_external_token,  # Add token.
                            external_token=service_instance.token
                        )
                        service_instance.token = father_id + '##' + peer + '##' + encrypted_external_token  # TODO adapt for ipv6 too.
                        return service_instance
                    except Exception as e:
                        l.LOGGER('Failed starting a service on peer, occurs the error: ' + str(e))
                        try:
                            refound_gas.pop()()
                        except IndexError:
                            pass

                #  The node launches the service locally.
                if getting_container: l.LOGGER('El nodo lanza el servicio localmente.')
                if not system_requirements: system_requirements = DEFAULT_SYSTEM_RESOURCES
                refound_gas = []
                if not spend_gas(
                        token_or_container_ip=father_id,
                        gas_to_spend=start_service_cost(
                            initial_gas_amount=initial_gas_amount if initial_gas_amount else default_initial_cost(),
                            service_buffer=service_buffer,
                            metadata=metadata
                        ) * GAS_COST_FACTOR,
                ): raise Exception('Launch service error spending gas for ' + father_id)
                try:
                    service_id = build.build(
                        service_buffer=service_buffer,
                        metadata=metadata,
                        service_id=service_id,
                        get_it=not getting_container
                    )  # If the container is not built, build it.
                except build.UnsupportedArquitectureException as e:
                    try:
                        refound_gas.pop()()
                    except IndexError:
                        pass
                    raise e
                except build.WaitBuildException:
                    # If it does not have the container, it takes it from another node in the background and requests
                    #  the instance from another node as well.
                    try:
                        refound_gas.pop()()  # Refund the gas.
                    except IndexError:
                        pass
                    getting_container = True
                    continue
                except Exception as e:
                    l.LOGGER('Error building the container: ' + str(e))
                    try:
                        refound_gas.pop()()  # Refund the gas.
                    except IndexError:
                        l.LOGGER('Error refunding the gas.')
                    l.LOGGER(str(e))
                    raise e

                # Now serialize the part of the service that is needed.
                service = celaut.Service()
                service.ParseFromString(service_buffer)
                # If the request is made by a local service.
                if father_id == father_ip:
                    container = create_container(
                        id=service_id,
                        entrypoint=service.container.entrypoint
                    )

                    set_config(container_id=container.id, config=config, resources=system_requirements,
                               api=service.container.config)

                    # The container must be started after adding the configuration file and
                    #  before requiring its IP address, since docker assigns it at startup.

                    try:
                        container.start()
                    except docker_lib.errors.APIError as e:
                        l.LOGGER('ERROR ON CONTAINER ' + str(container.id) + ' ' + str(
                            e))  # TODO LOS ERRORES DEBERIAN LANZAR ALGUN TIPO DE EXCEPCION QUE LLEGUE HASTA EL GRPC.

                    # Reload this object from the server again and update attrs with the new data.
                    container.reload()

                    for slot in service.api.slot:
                        uri_slot = celaut.Instance.Uri_Slot()
                        uri_slot.internal_port = slot.port

                        # Since it is internal, we know that it will only have one possible address per slot.
                        uri = celaut.Instance.Uri()
                        uri.ip = container.attrs['NetworkSettings']['IPAddress']
                        uri.port = slot.port
                        uri_slot.uri.append(uri)

                # Si hace la peticion un servicio de otro nodo.
                else:
                    assigment_ports = {slot.port: utils.get_free_port() for slot in service.api.slot}
                    container = create_container(
                        use_other_ports=assigment_ports,
                        id=service_id,
                        entrypoint=service.container.entrypoint
                    )
                    set_config(container_id=container.id, config=config, resources=system_requirements,
                               api=service.container.config)
                    try:
                        container.start()
                    except docker_lib.errors.APIError as e:
                        # TODO LOS ERRORES DEBERÍAN LANZAR UNA EXCEPCION QUE LLEGUE HASTA EL GRPC.
                        l.LOGGER('ERROR ON CONTAINER ' + str(container.id) + ' ' + str(e))

                        # Reload this object from the server again and update attrs with the new data.
                    container.reload()

                    for port in assigment_ports:
                        uri_slot = celaut.Instance.Uri_Slot()
                        uri_slot.internal_port = port

                        # for host_ip in host_ip_list:
                        uri = celaut.Instance.Uri()
                        uri.ip = utils.get_local_ip_from_network(
                            network=utils.get_network_name(ip_or_uri=father_ip)
                        )
                        uri.port = assigment_ports[port]
                        uri_slot.uri.append(uri)

                l.LOGGER('Thrown out a new instance by ' + father_id + ' of the container_id ' + container.id)
                return gateway_pb2.Instance(
                    token=add_container(
                        father_id=father_id,
                        container=container,
                        initial_gas_amount=initial_gas_amount if initial_gas_amount else default_initial_cost(
                            father_id=father_id),
                        system_requeriments_range=gateway_pb2.ModifyServiceSystemResourcesInput(
                            min_sysreq=system_requirements, max_sysreq=system_requirements)
                    ),
                    instance=celaut.Instance(
                        api=service.api,
                        uri_slot=[uri_slot]
                    )
                )

            if abort_it:
                l.LOGGER("Can't launch this service " + service_id)
                raise Exception("Can't launch this service " + service_id)
