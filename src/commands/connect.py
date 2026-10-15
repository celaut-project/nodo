from src.manager.manager import add_peer_instance, verified_peer_public_key, \
    associate_client_id_on_channel, get_client_id_on_other_peer, mint_client_id_on_channel
from src.database.sql_connection import SQLConnection
from src.utils.bee_client import BeeClient
from src.utils.config import ConfigManager
from src.identity.grpc_transport import channel_and_peer_id, node_channel

env_manager = ConfigManager()
SELF_ANNOUNCE_TO_CONNECTING_PEERS = env_manager.get("SELF_ANNOUNCE_TO_CONNECTING_PEERS")

sc = SQLConnection()

def connect(peer: str):
    print('Connecting to peer ->', peer)

    # A known peer is re-handshaked rather than skipped: that is how a payment
    # contract it only started advertising later gets registered locally.
    if sc.uri_exists(uri=peer):
        print(f"Peer {peer} is already registered; refreshing what it advertises.")

    try:
        # First contact, so there is no id to expect yet -- the certificate carries the
        # peer's identity key and proves it (issue #257), so this learns *and* verifies
        # who holds the address in one step, with no trust-on-first-use.
        channel, certificate_peer_id = channel_and_peer_id(peer)
        try:
            # GetPeerInfo now requires a client_id like every other RPC but
            # GenerateClient (issue #428), and this is the first RPC ever called on a
            # peer -- nothing about it is stored yet, so get_client_id_on_other_peer
            # (which needs a stored address to open its own channel from) cannot be
            # used here; mint straight over the channel already open for this call.
            client_id = mint_client_id_on_channel(channel)
            if not client_id:
                print(f"Could not mint a client_id at {peer}; it may refuse this node.")
            peer_info = BeeClient.get_peer_info(channel, client_id=client_id)
        finally:
            channel.close()

        if peer_info.public_key and peer_info.public_key != certificate_peer_id:
            # The address is provably held by certificate_peer_id, so an advertisement
            # for a *different* node is somebody relaying (or replaying) a signed Peer
            # that is not theirs. Registering it would file this address under the wrong
            # id, which is exactly what the certificate is here to prevent.
            print(
                f"Refused peer {peer}: the address is held by {certificate_peer_id} but "
                f"it advertised the identity {peer_info.public_key}."
            )
            return
        
        peer_id = add_peer_instance(peer_info)
        if not peer_id:
            if not verified_peer_public_key(peer_info):
                # Distinguish a policy refusal from a failure: this peer answered, it
                # just did not prove an identity, and no amount of retrying will change
                # that. Asking the same question add_peer_instance asked, rather than
                # only checking whether the fields are empty: a peer running the older
                # ECDSA scheme *does* send both, and it is refused all the same -- so
                # keying off emptiness would send it down the retry-implying branch.
                # A node signs with the identity key derived from its wallet mnemonic,
                # so one that cannot be verified is running code that predates it.
                print(
                    f"Refused peer {peer}: it did not prove an identity, so there is no "
                    "key to register it under (see the log for which check failed)."
                )
            else:
                print("Failed to add a peer.")
        else:
            print(f'Added peer {peer} with id {peer_id}')
            # Dialled by hand, so chosen: if gossip had already filed this peer, it is
            # now eligible for the automatic refill (issue #427).
            sc.mark_peer_chosen(peer_id=peer_id)
            # We dialled this address and it answered with an identity we verified, so
            # it is this peer's and nobody else's. Any other peer still holding it is
            # stale (the usual cause: the same host regenerated its mnemonic, so its
            # peer_id changed) and would otherwise keep being tried first.
            for previous_id in sc.claim_uri(uri=peer, peer_id=peer_id):
                print(
                    f"Endpoint {peer} was registered under peer {previous_id}; removed it "
                    "from that peer, which now answers at whatever other addresses it "
                    "announced (`nodo disconnect` it if there are none)."
                )
            if not peer_info.payment_contracts:
                print(
                    f"Note: peer {peer} advertises no payment contract, so it cannot "
                    "be paid yet (its ledger interface may not be initialised)."
                )

        if SELF_ANNOUNCE_TO_CONNECTING_PEERS:
            from src.gateway.utils import generate_full_node_peer_info
            print(f'Sending instance to peer: {peer}')

            try:
                gateway_instance = generate_full_node_peer_info()
            except Exception as e:
                print(f"Error generating instance for peer {peer}. {e}")
                return
            
            try:
                channel = node_channel(peer, expected_peer_id=certificate_peer_id)
                try:
                    # IntroducePeer requires a client_id too (issue #428). By this
                    # point add_peer_instance above may already have registered this
                    # peer's address, so get_client_id_on_other_peer's cache/PoW path
                    # can be used when we have a peer_id for it; otherwise mint fresh
                    # on this channel, the same as the pre-registration GetPeerInfo
                    # call above.
                    announce_client_id = (
                        get_client_id_on_other_peer(peer_id=peer_id) if peer_id
                        else mint_client_id_on_channel(channel)
                    )
                    if not announce_client_id:
                        print(f"Could not obtain a client_id at {peer}; announcing without one.")
                    _result = BeeClient.introduce_peer(
                        channel, gateway_instance, client_id=announce_client_id or ""
                    )  # Recursion guard shouldn't be used here, another message should be used. TODO
                    if _result is None:
                        # No answer at all -- not the same as a well-formed refusal
                        # ("REFUSED"), which is why this isn't just left to default
                        # into an empty RecursionGuard: an empty token would read as
                        # accepted below.
                        raise Exception(f"Peer {peer} sent no answer to IntroducePeer.")

                    if _result.token != "REFUSED" and announce_client_id:
                        # Only reachable now that IntroducePeer just registered this
                        # node as a peer there -- the earlier mint's own binding
                        # attempt (inside get_client_id_on_other_peer /
                        # mint_client_id_on_channel) could not have succeeded yet,
                        # since this node was not a known peer there at that point.
                        # AssociateClient is the deferred half that can (issue #428).
                        if associate_client_id_on_channel(channel, announce_client_id):
                            print(f"Bound this node's client_id at peer {peer} to its identity.")
                finally:
                    channel.close()

                if _result.token == "REFUSED":
                    # The peer stored nothing: it could not verify this node's identity
                    # signature. Worth saying out loud -- the announcement is what makes
                    # this node reachable, and the log line for it is on the remote side.
                    print(
                        f"Peer {peer} refused this node's announcement; it could not "
                        "verify our identity signature."
                    )
                else:
                    print(f"Peer {peer} accepted this node's announcement: {_result.token}")

            except Exception as e:
                print(f"Error sending instance to peer {peer}. {e}")
            
    except Exception as e:
        print(f"Error connecting to peer {peer}. {e}")
