# Node Uninstallation

To uninstall the node and remove all associated files and services, follow these steps.

## Automatic Uninstallation

An `uninstall.sh` script is provided to facilitate the process. This script will:

1.  Stop and disable the `nodo.service`.
2.  Remove Docker containers created by the node (using the internal database to identify them).
3.  Remove project files and configuration.
4.  Remove the wrapper script `/usr/local/bin/nodo`.

**Note:** This script **WILL NOT** remove system dependencies such as Docker, Python, Java, etc., as they may be needed for other applications.

### Execution

Run the following command in the terminal from the project directory:

```bash
sudo chmod +x uninstall.sh
sudo ./uninstall.sh
```

## Manual Uninstallation

If you prefer to do it manually:

1.  **Stop the service:**
    ```bash
    sudo systemctl stop nodo.service
    sudo systemctl disable nodo.service
    sudo rm /etc/systemd/system/nodo.service
    sudo systemctl daemon-reload
    ```

2.  **Clean up containers (Optional):**
    If you wish to remove the containers created by the node, you must identify and remove them manually using `docker rm -f <container_id>`.

3.  **Remove files:**
    ```bash
    sudo rm -rf /nodo
    sudo rm /usr/local/bin/nodo
    ```
