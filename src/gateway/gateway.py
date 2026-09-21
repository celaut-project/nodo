from bee_rpc import client as bee
import grpc

from protos import celaut_pb2_grpc, celaut_pb2
from protos.gateway_bee import GenerateClient_output_indices
from src.gateway.iterables.estimated_cost_iterable import GetServiceEstimatedCostIterable
from src.gateway.iterables.get_service_iterable import GetServiceIterable
from src.gateway.iterables.observe_iterable import ObserveIterable
from src.gateway.iterables.resource_availability_iterable import GetResourceAvailabilityIterable
from src.gateway.iterables.start_service_iterable import StartServiceIterable
from src.utils.contract_xattrs import get_script, get_contract_type, get_token_id
from src.tunneling.rpc_tunnel import TunnelError, service_tunnel
from src.gateway.utils import generate_full_node_peer_info
from src.manager.manager import add_peer_instance, modify_deposit, stop_instance, generate_client_or_pow_required, get_internal_service_id_by_uri, spend_mu, \
    hotplug, get_sysresources
from src.manager.metrics import get_metrics
from src.manager.networks import NetworkRequestRejected, resolve_network_for_peer
from src.payment_system.payment_process import generate_deposit_token, validate_payment_process
from src.utils import logger as log
from src.utils.utils import from_amount, get_only_the_ip_from_context, to_amount
from src.utils.config import ConfigManager
from src.utils.network_policy import NetworkPolicyRejection
from src.utils.monetary import prices

env_manager = ConfigManager()


class Gateway(celaut_pb2_grpc.Gateway):

    def GetServiceEstimatedCost(self, request_iterator, context, **kwargs):
        print("DEBUG. GET SERVICE ESTIMATED COST")
        log.LOGGER("DEBUG. GET SERVICE ESTIMATED COST")
        yield from GetServiceEstimatedCostIterable(request_iterator, context)

    def GetResourceAvailability(self, request_iterator, context, **kwargs):
        yield from GetResourceAvailabilityIterable(request_iterator, context)

    def StartService(self, request_iterator, context, **kwargs):
        yield from StartServiceIterable(request_iterator, context)

    def StopService(self, request_iterator, context, **kwargs):
        try:
            log.LOGGER('Stopping instance.')
            token = next(bee.parse_from_buffer(
                                request_iterator=request_iterator,
                                indices=celaut_pb2.TokenMessage,
                                partitions_message_mode=True
                            ), 0).token
            log.LOGGER(f'    with id {token}')
            refunded_amount = stop_instance(token=token)
            if not refunded_amount: refunded_amount = 0
            
            log.LOGGER(f'Stopped instance {token}.')
            yield from bee.serialize_to_buffer(
                    message_iterator=celaut_pb2.Refund(
                        amount=to_amount(refunded_amount)
                    )
            )
        except Exception as e:
            raise Exception('Was imposible stop the service. ' + str(e))

    def ModifyDeposit(self, request_iterator, context, **kwargs):
        try:
            log.LOGGER('Modifying deposit on service.')

            _input = next(bee.parse_from_buffer(
                                request_iterator=request_iterator,
                                indices=celaut_pb2.ModifyDepositInput,
                                partitions_message_mode=True
                            ), 0)

            success, message = modify_deposit(
                        amount_mu=from_amount(_input.difference),
                        service_token=_input.service_token
                    )

            log.LOGGER(f"Message on modify deposit: {message}")

            yield from bee.serialize_to_buffer(
                    message_iterator=celaut_pb2.ModifyDepositOutput(
                        success=success,
                        message=message
                    )
            )
        except Exception as e:
            raise Exception('Was imposible stop the service. ' + str(e))

    def GetPeerInfo(self, request_iterator, context, **kwargs):
        log.LOGGER(f'Request for instance by {context.peer()}')
        gateway_instance = generate_full_node_peer_info()
        yield from bee.serialize_to_buffer(gateway_instance)

    def ResolveNetwork(self, request_iterator, context, **kwargs):
        """Answer with the peers this node knows in the communication domain asked for.

        Generic on purpose. A caller holds a ``Service.Network`` and wants it turned
        into addresses; which mechanism does the turning -- a DNS lookup, a chain
        crawl, a published endpoint list -- is this node's business, not the caller's,
        and an RPC per mechanism would have every caller decide in advance which one it
        was holding. ``resolve_network`` already dispatches on the tag, so this exposes
        exactly the function the node runs for its own guests (issue #78).

        **Not a grant, for a peer.** To another node asking as a peer -- the RPC's
        older use, no guest and no firewall of ours involved -- the reply is only
        what this node believes; that caller opens nothing on the strength of it and
        verifies each address the way it verifies one from its own config. A lying
        answer therefore costs the caller a wasted request, which is what makes it
        safe to ask a stranger at all.

        **Bounded by what the caller declared, and a grant, when the caller is one of
        this node's own guests** (issue #385, and the firewall side of it, #404). A
        local instance is identified by its address the same way
        ``ModifyServiceSystemResources`` identifies one, its service spec is read, and
        the requested network has to fit inside a network that spec declares: same
        tags, every key the declaration fixed present and unchanged, keys it left as
        ``${VAR}`` free to fill, new keys free to add. Without that, deferred
        resolution would be a way around the declaration it exists to complete -- a
        guest that declared ``pow:ergo`` pinned to block B could ask here for
        ``pow:ergo`` pinned to nothing. A request that fits is, this time, a grant:
        the addresses returned are also opened in that guest's firewall
        (``networks.grant_resolved_network``), the same rules launch itself would have
        written had this network resolved by then -- deferred resolution otherwise
        told a guest where to go and left it unable to get there. A caller this node
        cannot identify as a local instance has no spec here to be measured against,
        nothing granted, and is answered as before.

        The operator's policy and the no-relaying rule live in
        ``networks.resolve_network_for_peer``, with the reasoning for each -- this is the
        gRPC plumbing around them, and a decision buried in a handler is a decision
        nobody can test.
        """
        network = next(bee.parse_from_buffer(
            request_iterator=request_iterator,
            indices=celaut_pb2.Service.Network,
            partitions_message_mode=True
        ), None)

        if network is None:
            raise Exception("ResolveNetwork needs a Service.Network to resolve.")

        log.LOGGER(
            f"Resolve network request by {context.peer()} for tags {list(network.tags)}"
        )

        try:
            resolution = resolve_network_for_peer(
                network,
                subject=f"peer {context.peer()}",
                caller_ip=get_only_the_ip_from_context(context_peer=context.peer()),
            )
        except NetworkPolicyRejection as e:
            raise Exception(f"This node does not reach that network. {e}")
        except NetworkRequestRejected as e:
            # Raised as an Exception like every other refusal in this file, so the
            # caller reads it off the gRPC status the same way it reads a policy
            # rejection. The two are separate sentences because they are separate
            # facts: the operator refuses the domain to anyone, versus this request
            # asks for more than the asking instance declared.
            raise Exception(
                f"That request does not fit what the asking instance declared. {e}"
            )

        yield from bee.serialize_to_buffer(resolution)

    def IntroducePeer(self, request_iterator, context, **kwargs):
        # TODO DDOS protection.   ¿?
        log.LOGGER('Introduce peer method.')
        peer_id = add_peer_instance(
                peer=next(bee.parse_from_buffer(
                request_iterator=request_iterator,
                indices=celaut_pb2.Peer,
                partitions_message_mode=True
            ), None)
        )

        # Answer with the id the peer was stored under, or REFUSED when it was not.
        # Refusal is a normal outcome now that an unverifiable announcement is turned
        # down (issue #236) -- it used to be impossible, since the uuid4 fallback always
        # succeeded, which is why "OK" was unconditional. A blanket "OK" would now tell a
        # node self-announcing through connect's SELF_ANNOUNCE_TO_CONNECTING_PEERS that it
        # is registered here while nothing was stored.
        yield from bee.serialize_to_buffer(celaut_pb2.RecursionGuard(token=peer_id or "REFUSED"))  # Recursion guard shouldn't be used here, another message should be used. TODO

    def GenerateClient(self, request_iterator, context, **kwargs):
        # The DoS protection this used to only have a TODO for (issue #361): the first
        # clients are free, after which the caller proposes its own UUID4 and pays for
        # it in Blake2b. The request is optional -- absent, or a bare Client with no
        # challenge, is the first attempt, and on a node still below the free limit that
        # is the whole exchange, exactly as before.
        request = next(bee.parse_from_buffer(
            request_iterator=request_iterator,
            indices=celaut_pb2.Client,
            partitions_message_mode=True
        ), None)

        yield from bee.serialize_to_buffer(
                message_iterator=generate_client_or_pow_required(
                    client_id=request.client_id if request else "",
                    challenge=request.challenge if request else "",
                    solution=request.pow_solution if request else "",
                ),
                # A copy: bee-rpc adds its own `0: bytes` entry to whatever it is
                # handed, and this one is a module-level constant shared with the
                # calling side.
                indices=dict(GenerateClient_output_indices)
        )

    def ModifyServiceSystemResources(self, request_iterator, context, **kwargs):
        log.LOGGER('Request for modify service system resources.')
        caller_ip = get_only_the_ip_from_context(context_peer=context.peer())
        token = get_internal_service_id_by_uri(uri=caller_ip)
        # Two different failures used to share one message. Charging an unknown
        # caller fails because there is nobody to charge, and reporting that as
        # "Error charging" pointed at the price of the call instead of at the fact
        # that the node did not recognise the address it came from -- which is what
        # the caller and the operator need to know, and what they were told for a
        # guest that called in before its instance was registered.
        if not token:
            raise Exception(
                f'No local instance is registered at {caller_ip}, so this node cannot tell '
                f'which instance is asking to change its resources ({context.peer()}).'
            )
        refund_container = []
        if not spend_mu(
                id=token,
                amount_mu=prices().modify_resources_mu,
                refund_function_container=refund_container
        ): raise Exception('Error charging for the resource change of ' + context.peer())
        if not hotplug(
                vmachine_id=token,
                system_requeriments_range=next(bee.parse_from_buffer(
                    request_iterator=request_iterator,
                    indices=celaut_pb2.ModifyServiceSystemResourcesInput,
                    partitions_message_mode=True
                ), None)
        ):
            try:
                refund_container.pop()()
            except IndexError:
                pass
            raise Exception('Exception on service modify method.')

        yield from bee.serialize_to_buffer(
                message_iterator=get_sysresources(id=token)
        )

    def GetService(self, request_iterator, context, **kwargs):
        yield from GetServiceIterable(request_iterator, context)

    def GenerateDepositToken(self, request_iterator, context, *kwargs):
        yield from bee.serialize_to_buffer(
                message_iterator=celaut_pb2.TokenMessage(
                    token=generate_deposit_token(
                        client_id=next(bee.parse_from_buffer(
                            request_iterator=request_iterator,
                            indices=celaut_pb2.Client,
                            partitions_message_mode=True
                        ), 0).client_id
                    )
                )
        )

    def Payable(self, request_iterator, context, **kwargs):
        log.LOGGER('Request for payment.')
        payment = next(bee.parse_from_buffer(
            request_iterator=request_iterator,
            indices=celaut_pb2.Payment,
            partitions_message_mode=True
        ), None)
        raw_script = get_script(payment.contract)
        # Select the payment validator by the stable, wallet-independent contract_type; the
        # raw ErgoTree/propositionBytes travels as ``script`` (never a textual address).
        contract_type = get_contract_type(payment.contract) or raw_script
        if not validate_payment_process(
                amount=from_amount(payment.amount),
                ledger=payment.contract.ledger,
                contract=contract_type,
                script=raw_script,
                token=payment.deposit_token,
                # Which asset this payment settles in is on the wire and used to be
                # dropped here. It is what says whose debt a donation accrues against:
                # one contract can be paid in a chain's native unit and in a token, and
                # inferring the asset from the ledger cannot tell those apart.
                asset=get_token_id(payment.contract),
        ):
            raise Exception('Error: payment not valid.')
        log.LOGGER('Payment is valid.')
        for b in bee.serialize_to_buffer(): yield b

    def GetMetrics(self, request_iterator, context, **kwargs):
        yield from bee.serialize_to_buffer(
                message_iterator=get_metrics(
                    token=next(bee.parse_from_buffer(
                        request_iterator=request_iterator,
                        indices=celaut_pb2.TokenMessage,
                        partitions_message_mode=True
                    ), None).token
                ),
                indices=celaut_pb2.Metrics,
        )

    def ServiceTunnel(self, request_iterator, context, **kwargs):
        try:
            # The stream carries two message types: the leading TokenMessage
            # handshake (index 1) and the raw payload (index 0). Both are parsed
            # in memory — `partitions_message_mode=False` would spill every
            # payload chunk to a temporary file, which no byte pipe can afford.
            conn, relay = service_tunnel(
                iterator=bee.parse_from_buffer(
                    request_iterator=request_iterator,
                    indices={1: celaut_pb2.TokenMessage, 0: bytes},
                    partitions_message_mode={1: True, 0: True}
                ),
                is_active=context.is_active,
            )
        except TunnelError as e:
            log.LOGGER(f'Tunnel refused: {e}')
            context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(e))
            return

        try:
            yield from bee.serialize_to_buffer(
                    message_iterator=relay,
                    # Mirrors the input map. Declaring a second index also keeps
                    # bee_rpc from inferring the index off the first message, which
                    # it does by calling next() unguarded — a service that closes
                    # without replying would surface as a RuntimeError instead of an
                    # empty stream.
                    indices={1: celaut_pb2.TokenMessage, 0: bytes},
            )
        finally:
            # The socket is opened eagerly inside service_tunnel; guarantee it is
            # released even if serialize_to_buffer bails before the relay
            # generator is ever iterated (its own finally would not run then).
            relay.close()
            try:
                conn.close()
            except OSError:
                pass

    def Observe(self, request_iterator, context, **kwargs):
        yield from ObserveIterable(request_iterator, context)
