# archolith-proxy Plugin System

Plugins extend archolith-proxy through a standard lifecycle contract.
The proxy starts regardless of plugin state — a broken plugin never prevents startup.

## Protocol

Implement all six members of `ProxyPlugin`:

```python
from archolith_proxy.plugins import ProxyPlugin, get_plugin_registry

class MyPlugin:
    @property
    def plugin_id(self) -> str:
        return "my-plugin"          # unique ID used in metrics and admin surface

    @property
    def plugin_version(self) -> str:
        return "1.0.0"

    async def activate(self) -> bool:
        # Called once at proxy startup.
        # Return True if ready, False if degraded.
        # Raise to report an error (proxy still starts).
        return True

    async def deactivate(self) -> None:
        # Called at proxy shutdown. Best-effort — exceptions are swallowed.
        pass

    async def healthcheck(self) -> dict:
        # Must return {"status": "ok"|"degraded"|"unavailable", ...}.
        return {"status": "ok"}

    def contribute_metrics(self) -> dict[str, int | float]:
        # Flat dict of counters. Must not block.
        return {"requests_handled": self._count}
```

## Registration

Register during module init or at proxy startup:

```python
get_plugin_registry().register(MyPlugin())
```

## Configuration

Control which plugins activate via environment variables:

```env
# Only activate these plugins (comma-separated IDs). Empty = activate all.
PLUGINS_ENABLED=filter,memory

# Always block these plugins, even if listed in PLUGINS_ENABLED.
PLUGINS_DISABLED=audit
```

## Admin Surface

- `GET /plugins` — list all plugins with status and counts
- `GET /plugins/{id}` — single plugin detail + live health + metrics
- `GET /metrics` → `plugins` key — aggregated plugin metrics grouped by plugin ID
