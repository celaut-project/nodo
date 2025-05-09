Okay, I understand. The `prose` field should provide a formal explanation of the *purpose* or *functionality* of the network access identified by the tags, rather than a detailed technical specification like ports or IPs.

Here is the updated documentation with this refinement:

# **Service Configuration JSON Documentation** (service.json)

### **Field Descriptions**

### **1. `api` (Optional)**
- **Type:** Array of Objects
- **Description:** Defines the network interfaces and protocols the service will use for *incoming* connections.
- **Keys:**
  - **`port` (Required):** The network port the service listens on.
    - **Type:** Integer
    - **Example:** `3030`, `5000`
  - **`protocol` (Required):** The protocols supported by the service.
    - **Type:** Array of Strings
    - **Allowed Values:** `"http"`, `"tls`", or other network protocols.
    - **Example:** `["http", "tls"]`

### **2. `architecture` (Required)**
- **Type:** String
- **Description:** Specifies the target CPU architecture for the service.
- **Some Allowed Values:**
  - ARM64-related: `"linux/arm64"`, `"aarch64"`
  - x86_64-related: `"linux/amd64"`, `"x86_64"`
- **Example:** `"linux/arm64"`, `"aarch64"`

### **3. `entrypoint` (Required)**
- **Type:** String
- **Description:** The command or path to the executable used to start the service.
- **Example:**
  - For a binary: `"/tiny-service"`
  - For a script: `"/service/start.py"`

### **4. `envs` (Optional)**
- **Type:** Array of Strings
- **Description:** Specifies environment variables required by the service. Each variable should have a corresponding `.field` file containing its value.
- **Default:** `[]` (No environment variables).
- **Example:** `["DATABASE_URL", "API_KEY"]`

### **5. `network` (Optional)**
- **Type:** Array of Objects
- **Description:** Configures the network access controls for *outgoing* connections initiated by the service. Each object in the array specifies a set of network permissions. If this array is empty or the field is omitted, the service is fully isolated and cannot make any outgoing network connections.
- **Default:** `[]` (No outgoing network access allowed).
- **Keys within each object:**
  - **`tags` (Required):** A list of strings representing identifiers or labels for this network configuration. These tags are used by the underlying network system to grant specific outgoing access.
    - **Type:** Array of Strings
    - **Example:** `["Bitcoin", "Bittorrent"]`
  - **`prose` (Required):** A formal explanation describing the *purpose* or *protocol functionality* associated with the network tags. This clarifies *what* the network access is used for at a high level.
    - **Type:** String
    - **Example:** `"Formal explanation detailing the interaction with the Bitcoin network for block propagation."`

### **Example Documentation**

#### **Example 1 (No outgoing network access)**
```json
{
    "api": [{"port": 3030, "protocol": ["http", "tls"]}],
    "architecture": "linux/arm64",
    "entrypoint": "/tiny-service",
    "envs": [],
    "network": []
}
```
*Explanation: The empty `network` array means the service has no defined outgoing network permissions and is fully isolated.*

#### **Example 2 (Allowing outgoing connections via defined network configurations)**
```json
{
    "api": [{"port": 5000, "protocol": ["http"]}],
    "architecture": "aarch64",
    "entrypoint": "/service/start.py",
    "envs": [],
    "network": [
        {
            "tags": ["Bitcoin"],
            "prose": "Formal explanation: Interaction with the Bitcoin peer-to-peer network for broadcasting transactions and receiving block data."
        },
        {
            "tags": ["Bittorrent"],
            "prose": "Formal explanation: Participation in Bittorrent swarms for downloading and uploading data chunks."
        }
    ]
}
```
*Explanation: This service is configured with two sets of outgoing network permissions. The prose formally describes the high-level purpose of connecting to the Bitcoin and Bittorrent networks.*

#### **Example 3 (Omitting the 'network' field - same as Example 1)**
```json
{
    "api": [{"port": 3030, "protocol": ["http"]}],
    "architecture": "linux/amd64",
    "entrypoint": "/worker/process.sh",
    "envs": ["QUEUE_TOPIC"]
}
```
*Explanation: Omitting the `network` field is equivalent to providing an empty array `[]`, resulting in a fully isolated service.*