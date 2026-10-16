from src.utils.utils import read_service_from_disk
from typing import Generator, Optional, Tuple

from protos import celaut_pb2
from protos.gateway_bee import rpc_output
from src.utils.tools.query_cache import QueryCache, canonical_key, quote_ttl, with_sorted_hashes
from src.virtualizers.architecture import UnsupportedArchitectureException, get_arch_tag
from src.gateway.iterables.abstract_input_service_iterable import AbstractInputServiceIterable, BreakIteration
from src.manager.manager import default_initial_balance
from src.utils.bee_client import BeeClient, Buffer
from src.utils.cost_functions.architecture_resources import NOT_EXECUTING_REASON, executes_locally
from src.utils.cost_functions.generate_estimated_cost import generate_estimated_cost
from src.utils.cost_functions.resource_availability import get_resource_availability
from src.utils import activity_window
from src.utils.network_policy import (
    HiddenModeRejection,
    enforce_hidden_mode,
    enforce_network_policy,
    tor_egress_unready_reason,
)
from src.balancers.estimated_cost_sorter.estimated_cost_sorter import estimated_cost_sorter
from src.balancers.execution_balancer.execution_balancer import quotes_from_peers
from src.utils.arch_guard import arch_from_tags
from src.utils.config import ConfigManager
from src.utils.cost_functions.delegated_quote import (
    delegation_time_budget,
    peer_round,
    remember_offer,
)
from src.utils.cost_functions.delegation_commission import with_commission
from src.utils.logger import LOGGER as logger
from src.utils.utils import from_amount, get_only_the_ip_from_context, to_amount


env_manager = ConfigManager()


class GetServiceEstimatedCostIterable(AbstractInputServiceIterable):
    RPC = "GetServiceEstimatedCost"
    
    # https://github.com/celaut-project/nodo/issues/70
    
    # With network.DELEGATE_EXECUTION this call also asks the peers (#551): this node
    # delegates StartService, so its quote offers the best of the local price and a
    # peer's price plus commission. It does not suppose that the client knows the same
    # peers. The time the caller gives (its gRPC deadline) limits how long the peers
    # are asked, and each hop gives its own peers less: see
    # `src/utils/cost_functions/delegated_quote.py`.
    #
    # The price depends only on what was asked, so it is remembered by the content of
    # the question (#456) rather than guarded by a token: the same quote asked again
    # within `network.QUERY_CACHE_TTL_SECONDS` is priced from memory, and one asked while
    # it is being computed waits for that answer. Whether there is room for it right now
    # is not remembered: it is checked on every answer, so a full node never serves a
    # remembered "can run" and a node that has freed room never serves a remembered
    # "no". See `src/utils/tools/query_cache.py`.

    cost: Optional[int] = None

    def start(self):
        logger('Request for the cost of a service.')
        return super().start()

    def generate(self) -> Generator[Buffer, None, None]:
        try:

            if not self.configuration:
                self.configuration = celaut_pb2.Configuration()

            if not self.service_hash:
                raise Exception("No service hash provided.")

            # A price is an offer, and a closed node has nothing to offer: quoting
            # while outside `activity_window` only gets the asking peer's balancer
            # to select us and then fail at launch, the same reasoning the network
            # policy below is enforced here for (#280).
            #
            # No dev-client exemption on this path, unlike `launch_service`: this
            # RPC is only ever reached by a peer over the wire, so every request
            # arriving here is outside work by construction. The operator's own
            # launches price themselves in-process through
            # `execution_balancer`/`generate_estimated_cost` and never come past
            # here at all.
            #
            # Before the cache, so a closed node never serves a quote it remembers.
            if not activity_window.is_open():
                raise Exception(activity_window.closed_reason())

            # A node that runs nothing itself and delegates nothing has nothing to offer:
            # refused before the service is read or priced.
            if not executes_locally() and not env_manager.get("network.DELEGATE_EXECUTION", True):
                raise Exception(NOT_EXECUTING_REASON)

            service = read_service_from_disk(service_hash=self.service_hash)
            if not service:
                raise Exception(f"Service {self.service_hash} not on local registry.")
            # The lists of the operator refuse the domain itself, on this node and on
            # any peer it would delegate to (#280). So they are not a reason to ask
            # the peers: they end the quote. Hidden mode is different (see below).
            enforce_network_policy(networks=service.network, subject=f"service {self.service_hash}")

            local_price, local_refusal = self._local_offer()
            price = self._best_offer(service, local_price)
            if price is None:
                # No peer offers it either: the client gets the reason of this node.
                raise local_refusal
            self._remember_offer(price)

            yield from BeeClient.respond(
                message_iterator=price,
                indices=rpc_output("GetServiceEstimatedCost")
            )

        except UnsupportedArchitectureException as e:
            raise e

        finally:
            # raise BreakIteration
            yield Buffer(signal=True)

    def _local_offer(self) -> Tuple[Optional[celaut_pb2.EstimatedCost], Optional[Exception]]:
        """The price of running the service here, or None and why it cannot run here.

        A refusal here is not the end of the quote: a peer may still run the service, and
        this node can offer that (``_best_offer``, #551). It is raised only if no peer
        offers anything.
        """
        # A node that runs nothing itself has no room to quote. The price below is
        # the price of running the service HERE, and the room check after it is
        # this machine's: answering would tell the asking peer's balancer that this
        # node can take the work, and `launch_service` would then have no local
        # candidate to run it on. Before the cache, so a node that is not executing
        # prices nothing.
        if not executes_locally():
            return None, Exception(NOT_EXECUTING_REASON)

        # The question as received: the configuration before `_quote` fills in the
        # default `initial_mu`, and the metadata with its hashes in a fixed order
        # (the parser collects them in a set).
        key = canonical_key(
            "GetServiceEstimatedCost",
            self.service_hash,
            self.configuration,
            with_sorted_hashes(self.metadata),
        )
        try:
            price, resources, arch, networks = QueryCache().get_or_compute(
                key,
                quote_ttl(),
                self._quote,
            )
        except (HiddenModeRejection, UnsupportedArchitectureException) as e:
            # A hidden node runs nothing whose traffic leaves this host directly, and a
            # node does not run an architecture it lacks. A peer can do both.
            return None, e
        # A hidden node whose services' egress goes through Tor does not offer what
        # its launcher would then skip while Tor is not ready: the asking peer's
        # balancer would select this node, pay it, and get the service from a third
        # node. Outside the cache, like the room check below: Tor's state changes, the
        # price does not.
        tor_unready = tor_egress_unready_reason(networks)
        if tor_unready:
            return None, Exception(f"This node cannot run the service right now: {tor_unready}.")
        # No room, no offer (#280): the same gate `generate_estimated_cost` applies
        # when it is not split from its price. Checked on every request, outside
        # the cache: the cache keeps the price, never the refusal, so a quote
        # refused for lack of room is offered again as soon as room frees.
        availability = get_resource_availability(resources=resources, arch=arch)
        if not availability["can_execute"]:
            # Handing None to respond() used to fail inside the serializer, so the
            # caller got an UNKNOWN status naming a KeyError rather than the reason.
            return None, Exception(
                "This node cannot run the service right now: "
                f"{availability.get('reason') or 'not enough resources.'}"
            )
        return price, None

    def _best_offer(
            self,
            service: celaut_pb2.Service,
            local_price: Optional[celaut_pb2.EstimatedCost],
    ) -> Optional[celaut_pb2.EstimatedCost]:
        """The best offer of this node: the local price, or a peer's price and commission.

        This node delegates ``StartService`` (``network.DELEGATE_EXECUTION``), so its
        quote says so too (#551). It does not suppose that the client knows the same
        peers. The peers are asked within the time the caller gives
        (``delegation_time_budget``), and the client gets the total only.
        """
        if not env_manager.get("network.DELEGATE_EXECUTION", True):
            return local_price
        budget = delegation_time_budget(getattr(self, "context", None))
        if budget is None:
            return local_price
        initial_mu = self._initial_mu(service)
        if initial_mu is None:
            return local_price
        configuration = celaut_pb2.Configuration()
        configuration.CopyFrom(self.configuration)
        configuration.initial_mu.CopyFrom(to_amount(initial_mu))

        peer_arch = arch_from_tags(service.container.architecture.tags)
        quotes = peer_round(
            self.service_hash, configuration,
            ask=lambda: quotes_from_peers(
                resources=service.container.resources, metadata=self.metadata,
                configuration=configuration, peer_arch=peer_arch, timeout=budget,
            ),
        )
        if quotes is None:
            # This node is already asking its peers this question: the question came
            # back to it through a loop, or two clients asked at once. Answer with what
            # is known here, without a second round of questions.
            return local_price
        if not quotes:
            # No peer answered in time: the same answer as a node that does not delegate.
            return local_price
        candidates = {peer: with_commission(cost, initial_mu) for peer, cost in quotes.items()}
        if local_price is not None:
            candidates["local"] = local_price
        if not candidates:
            return None
        for _, best in estimated_cost_sorter(estimated_costs=candidates):
            return best
        return None

    def _initial_mu(self, service: celaut_pb2.Service) -> Optional[int]:
        """The deposit of the instance, in local MU: the one asked for, or this node's default.

        The commission does not apply to it (``delegation_commission``), so it has to be
        known before a peer's price can be offered. None if it cannot be known.
        """
        if self.configuration.HasField("initial_mu") and self.configuration.initial_mu.n:
            return int(self.configuration.initial_mu.n)
        try:
            arch = get_arch_tag(service=service, metadata=self.metadata)
        except UnsupportedArchitectureException:
            arch = None
        try:
            return int(default_initial_balance(
                system_resources=service.container.resources.at_init,
                service_hash=self.service_hash,
                arch=arch,
            ))
        except Exception as e:
            logger(f"No default deposit for {self.service_hash}, so no peer price is offered: {e}")
            return None

    def _remember_offer(self, price: celaut_pb2.EstimatedCost) -> None:
        """Keep the offer, so that the launch does not charge more than it (#551)."""
        client_id = getattr(self, "client_id", None)
        if client_id:
            remember_offer(client_id, self.service_hash, int(price.cost.n or 0))

    def _quote(self) -> Tuple[
        celaut_pb2.EstimatedCost, celaut_pb2.Service.Container.Resources, Optional[str],
        Tuple[celaut_pb2.Service.Network, ...],
    ]:
        """The price, whatever the load, with what `generate` checks the room and Tor for."""
        service = read_service_from_disk(service_hash=self.service_hash)

        if not service:
            raise Exception(f"Service {self.service_hash} not on local registry.")

        # This node does not quote a service it would then refuse to run:
        # a price is an offer, and answering one for a service whose
        # networks the policy rejects only gets the peer's balancer to
        # select us and fail at launch (#280).
        enforce_network_policy(
            networks=service.network,
            subject=f"service {self.service_hash}",
        )
        # A hidden node quotes only what it would run itself, and it runs nothing whose
        # traffic leaves this host directly: that traffic shows its IP address.
        enforce_hidden_mode(
            networks=service.network,
            subject=f"service {self.service_hash}",
        )

        # The service's architecture, which selects the memory price when the
        # operator has set one per arch. Read before the service is dropped:
        # the quote and the charge have to come from the same rate, or this
        # node offers one price and bills another.
        service_arch = get_arch_tag(service=service, metadata=self.metadata)

        resources = service.container.resources
        # Kept for the same reason `service_arch` is read here: disk is
        # priced differently for a `read_mode=ro` service (it holds its
        # image and no writable capacity), and the quote and the charge
        # have to agree. The spec is dropped after the quote instead of
        # before it.
        priced_service = service
        del service

        # Needs the requested resources to price, so it happens here rather than
        # before the service is read.
        if not self.configuration.HasField('initial_mu') or not self.configuration.initial_mu:
            try:
                self.configuration.initial_mu.CopyFrom(
                    to_amount(default_initial_balance(
                        system_resources=resources.at_init,
                        service_hash=self.service_hash,
                        arch=service_arch,
                    ))
                )
            except Exception as e:
                logger(f"Failed to set initial_mu: {e}")
                raise Exception(f"Failed to set initial_mu: {e}")

        price = generate_estimated_cost(
            metadata=self.metadata,
            config=self.configuration,
            resources=resources,
            arch=service_arch,
            service=priced_service,
            check_availability=False,
        )
        networks = tuple(celaut_pb2.Service.Network.FromString(network.SerializeToString())
                         for network in priced_service.network)
        return price, resources, service_arch, networks

    def final(self):
        logger('End request for the cost of a service.')
        return super().final()
