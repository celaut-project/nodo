# service.json
## Structure

```json
{
  "architecture": "linux/amd64",
  "init": {
    "entry_path": ["service", "start"],
    "xattrs": {
      "boot_mode": "prod"
    }
  },
  "config_declaration": {
    "path": ["config", "runtime", "node.pb"]
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

## Notes

- `init.entry_path` is serialized to `container.init.entry_path`.
- Legacy `entrypoint` is still accepted in `service.json` and is mapped to `container.init.entry_path`.
- If `service.json` provides slash-based input (for example `"/service/start"`), packer normalizes it to segmented form (`["service","start"]`).
- `init.xattrs` is serialized to `container.init.xattrs` (UTF-8 for text values).
- `config_declaration.path` is serialized to `container.config_declaration.path`.
- If `service.json` provides slash-based input (for example `"/config/runtime/node.pb"`), packer normalizes it to segmented form.
- `api[].gas_amount_per_call` is serialized to `api.slot[].gas_amount_per_call`.
- `resources.start_time_ms` no longer exists.
