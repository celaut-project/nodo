# Price and limits for the access to a DNS network

Design note. It records an idea for the future. The node does **not** charge or limit
the access to a DNS network today. Only the announcement exists in the protocol, in
`Peer.rates` and `Peer.limits` (`protos/celaut.proto`).

## The problem

A service can declare a DNS name as a network, for example `finance.yahoo.com` or
`api.openai.com` ([`NETWORKS.md`](../NETWORKS.md)). The node then opens its firewall
for that name. The service uses the IP address of the node to reach a server that is
**outside Celaut**. We call such a server a *central server*.

The access of the node to a central server is a limited supply:

- The central server can block the IP address of the node if the traffic is too high.
  Then every service on the node loses that network.
- Many scrapers and bots want the same few networks. Demand is more than supply.
- Today the node can only accept or refuse a network (`service_networks.whitelist` and
  `service_networks.blacklist`). It has no price for it and no limit on its use.

When supply is limited and demand is high, the node must be able to ask for a price.

## What the protocol has now

The change of the protocol is already in `celaut.proto`. No node reads it yet.

| Item | Where | Example |
|---|---|---|
| Price of the traffic to one network | `Peer.rates`, key `net_mu_per_gib:<network>` | `net_mu_per_gib:api.openai.com` |
| Price of one connection to one network | `Peer.rates`, key `connection_open_mu:<network>` | `connection_open_mu:api.openai.com` |
| Limit for one client | `Peer.limits`, key `<limit>:<network>` | `max_concurrent_connections:api.openai.com` |
| Limit for all clients together | `Peer.limits`, key `global_<limit>:<network>` | `global_max_gib_per_day:finance.yahoo.com` |

The limits are `max_concurrent_connections`, `max_connections_per_day`,
`max_connections_per_month`, `max_gib_per_day` and `max_gib_per_month`. The network
`*` means every network that has no key of its own. A key without a network has the
general rate of the node.

Only a DNS name has these keys. A `pow:` network is a set of peers. It does not use
the access of this node to a central server.

The network is a plain string, not a tag-prose-formal descriptor. The `formal` field
of a network only matters for `pow:`, where it carries selection keys.

## Case: a node prices `api.openai.com`

The mechanism is the same as for `finance.yahoo.com`. Three points are different.

1. **The node sells the access, not the service.** The caller uses its own API key.
   The node charges only for the route to `api.openai.com`. The node does not pay
   OpenAI and does not resell it.
2. **The node cannot see inside the traffic.** The traffic is TLS. The node can count
   connections and bytes. It cannot count requests or tokens. An API call is small, so
   a price by GiB is almost zero. For this case `connection_open_mu` and
   `max_concurrent_connections` are the useful keys.
3. **To sell the API itself, make a Celaut service.** A service that holds the key and
   exposes `api.openai.com` as a slot can set a price for each method in
   `Service.Api.Slot.mu_per_call`. That is a different thing: it is a service, with its
   own price, and the network access is hidden behind it.

| | Network access (this note) | Celaut service that wraps the API |
|---|---|---|
| Who holds the API key | The caller | The service |
| What the node sells | The route out | Calls to a method |
| What can be counted | Connections, bytes | Calls, by method |
| Where the price is | `Peer.rates` with `:<network>` | `Slot.mu_per_call` |

## Open questions

Each question must have an answer before the node enforces any of this.

- **The network `*`.** A service that declares `*` can reach `api.openai.com` without
  the specific price. The node can refuse `*` when a priced network exists, or it can
  charge the highest price to `*`. This is not decided.
- **One IP address for many names.** The firewall works with IP addresses. A central
  server behind a CDN shares its address with other names. The node cannot always know
  which name a connection is for. The node must count by the name that the service
  declared, or use a resolver that it controls.
- **Subdomains.** The keys match one exact name. `api.openai.com` does not match
  `eu.api.openai.com`. `service_networks` uses globs. Decide if prices use them.
- **Where to count.** In the normal mode the egress leaves by the firewall of the
  guest bridge, and the node does not see it. In hidden mode it goes through the Tor
  TransPort. The node needs one point where every connection passes.
- **When the price is charged.** A service can open a connection at any time. The
  node can charge the balance of the instance at run time, and stop the instance when
  the balance is empty. The quote (`GetServiceEstimatedCost`) must show the price of
  the declared networks before the start.
- **Descendants and delegation.** A child service has a part of what its parent
  declared. The price and the limit must follow the same chain, and a peer that runs
  the service must apply its own price.
- **The terms of service of the central server.** The operator is responsible for the
  traffic that leaves from its IP address. A limit protects the node, and a price does
  not remove that responsibility.

## Work for the future

1. Add a block to `service_networks` in `config.yaml` that sets the prices and the
   limits for each name.
2. Count connections and bytes for each client and each network.
3. Refuse a new connection above a limit. Charge the price from the balance.
4. Write the prices and the limits in `Peer.rates` and `Peer.limits`.
5. Include the price in the quote of `GetServiceEstimatedCost`.
6. Write the user documentation in [`NETWORKS.md`](../NETWORKS.md) and
   [`PRICING.md`](../PRICING.md), and link it here.
