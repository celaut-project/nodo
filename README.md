# Nodo: A Celaut node implementation

Nodo is a powerful framework designed to streamline communication, management, and orchestration of services
across a network of computers.

In a network where
services are specialized software components encapsulated within binary files, and nodes represent the computers in
the network responsible for discovering and establishing connections with other nodes
(aka [CELAUT](https://github.com/celaut-project/paradigm/blob/master/README.md)).

As is described in the [paradigm repository](https://github.com/celaut-project/paradigm/blob/master/README.md#node-responsabilities),
it's responsibilities are:

1. **Service Execution**: Handles service instance requests, balancing the load between running them
locally or on its peer nodes. This ensures an efficient distribution of tasks and resources across the network,
optimizing system performance.

2. **Communication Interface**: Provides a robust and flexible interface that enables the services that it executes
to communicate seamlessly with it, ensuring efficient data exchange and coordination.

3. **Address and Token Provisioning**: Offers a streamlined process for obtaining the communication address and
authentication token of a service required for interaction, enhancing security and accessibility.

4. **Dependency Management**: Takes care of ensuring that services have access to the addresses of their
dependencies, irrespective of the node on which they are executed, promoting a smooth and efficient service ecosystem.

5. **Service Packing**: Although it is not necessarily a Celaut node's responsibility, this implementation allows you to send
a Dockerfile along with a configuration file and get a specification for that service, making it a hassle-free process for users.


## Installation

### Linux

Choose one variant. They differ in the user that runs the daemon (see the notes below).

<details>
<summary><b>Service user</b> (best choice for a server)</summary>

```bash
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/stable/install.sh | sudo bash -s -- --service-user nodo
```

</details>

<details>
<summary><b>Without sudo</b></summary>

```bash
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/stable/install.sh | bash
```

The installer shows the few commands that need root and asks before it runs them. If you decline, run them yourself, then run the same command again.

</details>

<details>
<summary><b>Root</b> (the simplest)</summary>

```bash
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/stable/install.sh | sudo bash
```

</details>

<details>
<summary>Linux installation notes</summary>

- **Requirements**: `git`, `curl`, `sudo`, `iptables`, `bc`.

- **Supported distributions**: any Linux with `apt` or `dnf`. Tested on Ubuntu 22.04/24.04 and
  Fedora (including Fedora Asahi Remix on Apple Silicon). Other package managers need one entry
  added to `bash/lib_pkg.sh`.

- **Installation variants**: there are three ways to install Nodo. They differ in the user that
  runs the daemon. Python, Java, and `yq` runtimes are installed locally under `MAIN_DIR` in all
  three.

  | Variant | Command | Daemon user | Install directory |
  |---------|---------|-------------|-------------------|
  | Root (default) | `sudo bash` | `root` | `/nodo` |
  | Service user | `sudo bash -s -- --service-user nodo` | the system user you name | `/nodo` |
  | Without sudo | `bash` | you | `~/.local/share/nodo` |

  Advantages of each variant:

  | Variant | Advantages |
  |---------|------------|
  | Root | The simplest setup. The installer does all the steps. Running VMs continue when the daemon restarts. |
  | Service user | The daemon has `CAP_NET_ADMIN` only. The hypervisor and `virtiofsd` have no capabilities. The node has its own user and its own files. Best choice for a server. |
  | Without sudo | The daemon has `CAP_NET_ADMIN` only. Your own user owns all the files. Root is necessary only for a few steps, and you see each command before it runs. |

  The root-only steps of the installation without sudo are: missing packages, a `/etc/subuid`
  range, the systemd unit, and on hosts where AppArmor restricts user namespaces (Ubuntu 23.10+)
  an AppArmor profile for `virtiofsd`.

- **Root installation**: run the script with `sudo`. The script does the system-level setup itself
  and the daemon runs as root.

- **Installation with a service user**: run the script with `sudo` and the `--service-user` option:
  ```bash
  curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/stable/install.sh | sudo bash -s -- --service-user nodo
  ```
  The script creates the system user if it does not exist. The daemon runs as this user with
  `CAP_NET_ADMIN` only. `nodo update` keeps the service user. To go back to a root daemon, run
  the installer again with `--service-user root`.

- **Installation without sudo**: run the script as yourself:
  ```bash
  curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/stable/install.sh | bash
  ```
  It installs into `~/.local/share/nodo`, does everything that needs no root as you, and lists
  the few steps that do (missing packages, subordinate ids, the systemd unit) as exact commands.
  It saves them to `install-privileged.sh` and runs them with sudo only if you say so
  (`--no-input` only prints them). The daemon then runs as you, with `CAP_NET_ADMIN` only.
  The installer puts the shell completion in your home directory. Bash finds it by itself.
  For zsh, add `fpath=(~/.zsh/completions $fpath)` to `~/.zshrc`, before `compinit`.

- **Restart stops the VMs**: with a service user and in the installation without sudo, the VMs
  run inside the cgroup of the unit. A restart of `nodo.service` (also `nodo update` and
  `nodo daemon restart`) stops all running VMs. On a root installation, the VMs keep running.

  For details, see [`docs/ROOTLESS.md`](docs/ROOTLESS.md). For a fully manual installation,
  follow the [manual guide](docs/INSTALL.md).

- **Configurable binary paths**: Java, Python, and `yq` binaries can be customized in `config.yaml` under `dependencies.*`.
  
> Future versions of Nodo aim to progressively reduce the number of external system dependencies.

</details>

### Windows 11

Windows users should download and run the official installer:

[Nodo Windows Installer (.exe)](https://github.com/celaut-project/nodo/releases/download/wsl-exe/Nodo-Setup.exe)

<details>
<summary>Windows installation details</summary>

The installer automatically creates an isolated Linux distribution dedicated to **Nodo**, allowing the node to run separated from the rest of the operating system and user environment.

No manual Linux environment setup is required.

</details>

### Development

<details>
<summary>Developer installation (click to expand)</summary>

The options `--source-dir` and `--branch` work with all three variants. Add them to the command of the variant you want.

Local source installation (development workflow). Use a local checkout instead of a clone:
```bash
cd /home/user/Desktop/nodo

# Root
sudo ./install.sh --source-dir "$PWD"

# Service user
sudo ./install.sh --source-dir "$PWD" --service-user nodo

# Without sudo
./install.sh --source-dir "$PWD"
```

This mode keeps `nodo.service` pointed to your local checkout, so you can test code changes when you restart the service (no push/reinstall cycle needed). With a service user, make sure that this user can read the checkout.

Branch-based installation (default branch is `stable`):
```bash
# Root
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/dev/install.sh | sudo bash -s -- --branch dev

# Service user
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/dev/install.sh | sudo bash -s -- --branch dev --service-user nodo

# Without sudo
curl --proto '=https' --tlsv1.2 -sSf https://raw.githubusercontent.com/celaut-project/nodo/dev/install.sh | bash -s -- --branch dev
```

</details>


## Platform Compatibility

Below is a breakdown of **Nodo** feature support across different operating systems. Since the project is still under development, capability levels vary by platform.

| Functionality         | Linux     | Windows          | Mac              |
|---------------------- |---------- |------------------|------------------|
| Local execution       | 🟡 Alpha | 🟡 Alpha         | 🔴 Not supported |
| Packaging             | 🟡 Alpha | 🟡 Alpha         | 🔴 Not supported |
| Local network         | 🟡 Alpha | 🟡 Alpha         | 🔴 Not supported |
| Trustless network     | 🟡 Alpha | 🟡 Alpha         | 🔴 Not supported |

* 🟡 **Alpha**: Functionality under active development and subject to change.
* 🔴 **Not supported**: Functionality not available on this platform.


## Usage

For detailed usage instructions, please refer to the [User Guide](docs/USAGE.md).
Scripts and AI agents should use the CLI rather than `nodo tui`: every TUI page and
action has a non-interactive command, most with `--json` — see
[Scripting and AI agents](docs/USAGE.md#scripting-and-ai-agents-).

To package your own project into a Celaut service, see the [Packing Guide](docs/PACKING.md).
To remove Nodo, see the [Uninstallation Guide](docs/UNINSTALL.md).


## Know Your Assumptions

Before using **Nodo**, it's essential to understand the assumptions and risks involved. Please review the [Know Your Assumptions (KyA)](docs/KyA.md)
document to ensure you are fully aware of your responsibilities and the limitations of the software.


## About trust between peers

All payments, reputation submissions, and service remunerations are handled decentralized on the Ergo blockchain.
Check how and why Nodo uses [Ergo](docs/ERGO.md).
How a peer's score is computed when work is delegated: [Reputation](docs/REPUTATION.md).
How a request delegated from node to node is kept from looping back or growing without bound, and why a peer would honour that: [Recursion guard](docs/RECURSION_GUARD.md). Design documents and studies for specific issues are indexed in [docs/proposals](docs/proposals/README.md).
