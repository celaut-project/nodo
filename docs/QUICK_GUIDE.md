# FAST GUIDE: START USING NODO

+==============================================================+
| START HERE                                                   |
+==============================================================+
| `nodo config`                                                |
| Quick setup for the node and its runtime configuration.      |
|                                                              |
| `nodo download <url>`                                        |
| Fetch a published service from a manifest URL and import it. |
|                                                              |
| `nodo pack <folder>`                                         |
| Turn a local project folder into a service package.          |
|                                                              |
| `nodo publish <service id|service tag>`                      |
| Publish a local service so other nodes can download it.      |
|                                                              |
| `nodo execute <service>`                                     |
| Run a service from a local id, tag, or packaged file.        |
|                                                              |
| `nodo import <path>`                                         |
| Import a packaged service file from disk into this node.     |
|                                                              |
| `nodo export <service> <path>`                               |
| Export a packaged artifact for sharing or moving around.     |
+==============================================================+

AT A GLANCE

1. Configure the node
   `nodo config`

2. Bring in a service
   `nodo download <url>`
   or
   `nodo import <path>`

3. Build your own service package
   `nodo pack <folder>`

4. Share it with the network
   `nodo publish <service id|service tag>`

5. Run it
   `nodo execute <service>`

COMMAND FOCUS

`nodo config`
Set up the node quickly before working with services.

`nodo download <url>`
Use this when the service already exists online and you want it locally.

`nodo pack <folder>`
Use this when you have a local service project and want to package it.

`nodo publish <service id|service tag>`
Use this after packaging or importing a service you want to distribute.

`nodo execute <service>`
Run a service using a service id, a service tag, or a local packaged file.

`nodo import <path>`
Load a packaged service file from disk into the local node registry.

`nodo export <service> <path>`
Write a packaged service artifact to a directory on disk.

IMPORTANT FLAGS

`nodo execute --remote <service>`
Use `--remote` when the node is being reached through SSH or another remote
host-facing setup. It tells Nodo to advertise the external reachable address
instead of the internal VM/container address.

`nodo export <service> <path> --raw`
Use `--raw` when you want the raw `.celaut` file so you can inspect it or
verify its hash manually.

MENTAL MODEL

- `config` prepares the node.
- `download` or `import` brings a service in.
- `pack` prepares your own service.
- `publish` shares it.
- `execute` runs it.
- `export` takes it back out.
