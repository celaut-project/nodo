# service.json (Hard Cutover)

Este packer acepta **solo** el esquema nuevo. Si detecta claves legacy (`entrypoint`, `config`, `resources.start_time_ms`) falla explícitamente.

## Estructura

```json
{
  "architecture": "linux/amd64",
  "init": {
    "entry_path": ["/service/start"],
    "xattrs": {
      "boot_mode": "prod"
    }
  },
  "config_declaration": {
    "path": ["__config__"]
  },
  "api": [
    {
      "port": 8080,
      "protocol": ["http"],
      "gas_amount_per_call": {
        "health": "1",
        "infer": "50"
      }
    }
  ],
  "resources": {
    "at_init": {
      "mem_limit": 10000000,
      "disk_space": 2000000000
    },
    "at_most": {
      "mem_limit": 50000000,
      "disk_space": 4000000000
    }
  },
  "network": [
    {
      "tags": ["example.com"],
      "prose": "Outbound access to example.com APIs."
    }
  ],
  "envs": ["API_KEY"]
}
```

## Notas

- `init.entry_path` se serializa en `container.init.entry_path`.
- `init.xattrs` se serializa en `container.init.xattrs` (UTF-8 para valores de texto).
- `config_declaration.path` se serializa en `container.config_declaration.path`.
- `api[].gas_amount_per_call` se serializa en `api.slot[].gas_amount_per_call`.
- `resources.start_time_ms` ya no existe.
