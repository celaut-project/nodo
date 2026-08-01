# End-to-End Walkthrough

A single worked path from a project directory to a running, reachable service:
**pack → estimate → execute → get address + token → call → observe → kill.** It
also serves as the example-output reference for the commands in
[`USAGE.md`](USAGE.md) (which documents every flag but shows no output).

> The output blocks below are **illustrative**. Hashes, ids, ports, and exact
> formatting will differ on your node and between versions; treat them as shape,
> not literal contract. The authoritative field lists live in
> [`USAGE.md`](USAGE.md) and [`PACKING.md`](PACKING.md).

Prerequisites: Nodo installed ([`INSTALL.md`](INSTALL.md)), the KyA accepted, and
— for default-mode packing — a packer core service configured and running
([`CONFIG.md`](CONFIG.md)).

## 1. Pack the project

Author the project per [`PACKING.md`](PACKING.md) (a `Dockerfile` with **no**
`CMD`/`ENTRYPOINT`/`EXPOSE`, a `service.json` declaring `architecture`, `init`,
`api`, and any `envs`), then:

```bash
nodo pack ./my-solver
```

```
Preparing ./my-solver …
Building filesystem via packer-service (instance 9f2c… @ 192.168.200.7:8080) …
Parsed filesystem → celaut.Service specification
Imported service.
service id: 7b8f4e2a9c1d6f03a5e8b7c4d2f1a0e9b6c3d8f5a2e1b4c7d0f9a6e3b8c5d2f1
tags: my-solver
```

Keep the printed **service id** (or the `my-solver` tag) — it identifies the
specification for every command below.

## 2. Inspect what it declares

Never guess environment variables or ports — read them from the service.

```bash
nodo inspect my-solver
```

```
Service 7b8f4e2a…d2f1  (tag: my-solver)
architecture:      linux/amd64
init.entry_path:   /app/start.sh
resources:
  at_init:  mem_limit=104857600 (100 MB)  disk_space=2147483648 (2 GB)
  at_most:  mem_limit=524288000 (500 MB)  disk_space=10737418240 (10 GB)
api:
  - port 50051  transport=tcp  protocol=[grpc]
    gas_amount_per_call: { Solve: 100 }
envs (declared):   WORKERS, TIMEOUT
network:           [ipv4, public]
```

The `-e` values you may pass to `execute` are exactly the declared `envs`
(`WORKERS`, `TIMEOUT` here). The reachable port/protocol come from the `api`
block. See [Concepts → Address and token provisioning](CONCEPTS.md).

## 3. Estimate feasibility and cost

```bash
nodo estimate my-solver
```

```
Execution feasible: YES
Estimated gas:
  initial:      1.20e+08
  maintenance:  3.00e+06 / iteration
Resource check (required vs available):
  RAM:   100 MB required   |  6.2 GB available   / 15.6 GB total
  CPU:   0.5 core required  |  7.3 cores available / 8 cores total
  Disk:  2.0 GB required    |  210 GB available   / 512 GB total
```

If it prints `Execution feasible: NO`, the `reason` line names the failing guard
(usually `resources.at_most.mem_limit` vs the node's memory pool). Fix the service
or free resources before executing.

## 4. Execute — creates a running instance and provisions its address + token

```bash
# Pass only declared envs; --remote advertises the host-facing IP.
nodo execute --remote -e WORKERS 8 -e TIMEOUT 20 my-solver
```

```
Launching my-solver in a Cloud Hypervisor microVM …
instance id:   c92ae2ff1b7d4a0e8f36c5a1d9e0…      (abbreviates to: c92ae2ff)
status:        running   virtualizer: ch
address:       203.0.113.10:50051         (grpc/tcp, from service.json api)
token:         eyJhbGciOiJ… (auth token — required on every call)
```

`execute` returns the **instance id**, the **communication address**
(`ip:port` for the declared API), and the **authentication token**. Providing
these three is a core node responsibility (project [`README.md`](../README.md) →
*Address and Token Provisioning*). You need the address **and** the token to make
a call.

Confirm it is running:

```bash
nodo instances
```

```
INSTANCE ID   SERVICE (TAG)   STATUS    VIRTUALIZER   GAS
c92ae2ff      my-solver       running   ch            1.20e+08
```

## 5. Call the service

Use the address + token with the protocol the service declares (`grpc` here). For
example, with `grpcurl` and the token as metadata:

```bash
grpcurl -H "authorization: <token>" -d '{"input": "..."}' \
  203.0.113.10:50051 my.solver.Solver/Solve
```

Each call is metered per `service.json → api.gas_amount_per_call` (here `Solve`
costs 100 gas). Top up a running instance with `nodo increase_gas c92ae2ff 100`.

## 6. Observe live metrics and network activity

```bash
nodo observe c92ae2ff --save ./captures
```

```
my-solver  c92ae2ff   [ch]                        CPU 12.4% (peak 41.0%)   MEM 88.2 MB (peak 130.1 MB)
Network (newest first):
17:15:41  OUT → instance c92ae2ff [gateway] (parent)     TCP     142 pkts    38.4 KB
17:15:41  IN  ← 203.0.113.55:54210                       TCP      37 pkts     9.1 KB
… writing ./captures/my-solver_c92ae2ff/{metrics.jsonl,capture.pcap}
```

`observe` answers "what is my instance doing right now." With `--save` it writes
`metrics.jsonl` (one CPU/memory sample per second) and, on a Linux/KVM host with
`CAP_NET_RAW`, `capture.pcap` (every frame, openable in Wireshark). `Ctrl-C`
exits. Full behaviour: [`USAGE.md`](USAGE.md) → `observe`.

## 7. Kill the instance

```bash
nodo kill c92ae2ff
```

```
Stopping instance c92ae2ff … stopped.
```

The specification stays in the local registry (`nodo services`); only the running
instance is gone. To remove the specification too: `nodo remove my-solver`.
