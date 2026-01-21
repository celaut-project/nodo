## 1. The Rootless network model (Docker/Podman)

In an unprivileged (Rootless) environment, the container engine cannot create real network interfaces (`veth pairs`) or manipulate the kernel bridge (`bridge`).

* **Mechanism:** It uses `slirp4netns` or `pasta`. These act as a **Layer 2/3 proxy in user space**.
* **Point of failure:** The host kernel does not see each container’s traffic individually. From the operating system’s perspective, all outgoing traffic belongs to the **PID of the network engine** (a single user-space process).
* **Consequence:** It is impossible to apply `iptables` or `nftables` rules on the host that distinguish between containers, because the source IP is the same for all of them in the eyes of the kernel.

---

## 2. Evaluated approaches and their limitations

### A. Environment variables (`HTTP_PROXY`)

* **Technique:** Inject `HTTP_PROXY` or `HTTPS_PROXY` to redirect traffic to the Gateway.
* **Limitation:** It only works with applications that respect these variables (Layer 7). It does not capture direct TCP/UDP traffic, binary protocols, or applications that ignore the proxy. It does not provide true socket-level interception.

### B. Internal redirection (DNAT in the Namespace)

* **Technique:** Use `iptables` inside the container (where root privileges exist) to redirect traffic to the host IP (`10.0.2.2`).
* **Limitation:** The **original destination IP** and the **container identity** are lost. When the traffic reaches the Gateway on the host, it receives a local connection and cannot know which external IP the container originally tried to reach, breaking transparency.

### C. User-space tunnels (`tun2socks`)

* **Technique:** Create a `TUN` device inside the container and convert all IP traffic into SOCKS5 traffic toward the Gateway.
* **Limitation:** High CPU overhead due to double packet translation (IP → SOCKS → IP) and extreme complexity when managing DNS resolution transparently without the container “knowing” it is being filtered.

### D. Podman with `pasta`

* **Technique:** Uses file descriptor passing to move packets between namespaces without copying data (faster than `slirp`).
* **Limitation:** Although more efficient, it still operates under the constraints of an unprivileged user: it cannot inject rules into the host kernel’s network stack.

---

## 3. Technical Conclusion

Granular network control (L3/L4) and full transparency require, by definition, the **ability to manipulate the kernel network stack**.

Any architecture that removes the use of `sudo` (network privileges) is forced to move the logic to **Layer 7 (Proxies)** or to **User Space**, which breaks the transparency of generic TCP/UDP protocols and the ability to natively identify individual data flows.
