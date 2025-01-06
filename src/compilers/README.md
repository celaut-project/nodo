# **Service Configuration JSON Documentation** (service.json)

### **Field Descriptions**

### **1. `api` (Optional)**
- **Type:** Array of Objects
- **Description:** Defines the network interfaces and protocols the service will use.
- **Keys:**
  - **`port` (Required):** The network port the service listens on.
    - **Type:** Integer
    - **Example:** `3030`, `5000`
  - **`protocol` (Required):** The protocols supported by the service.
    - **Type:** Array of Strings
    - **Allowed Values:** `"http"`, `"tls"`, or other network protocols.
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


### **Example Documentation**

#### **Example 1**
```json
{
    "api": [{"port": 3030, "protocol": ["http", "tls"]}],
    "architecture": "linux/arm64",
    "entrypoint": "/tiny-service",
    "envs": []
}
```

#### **Example 2**
```json
{
    "api": [{"port": 5000, "protocol": ["http"]}],
    "architecture": "aarch64",
    "entrypoint": "/service/start.py",
    "envs": []
}
```
