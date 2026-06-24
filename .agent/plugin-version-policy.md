# Plugin Version Compatibility Policy

## Minimum Version Enforcement

Plugin minimum compatible versions are defined in `archolith_proxy/plugins/registry.py`:

```python
MIN_PLUGIN_VERSIONS: dict[str, str] = {
    "filter": "0.1.0",
    "audit": "0.1.0",
    "memory": "0.1.0",
}
```

## Version Bump Policy

When a plugin (e.g. archolith-filter, archolith-audit) bumps its **MINOR version** with new contract surface that archolith-proxy depends on:

1. Increment the corresponding entry in `MIN_PLUGIN_VERSIONS` in `registry.py`
2. Add a changelog entry documenting the new minimum
3. Forward compatibility: an older proxy can still load an older plugin (fail-open contract is unchanged)
4. Backward compatibility: a newer proxy requires the minimum version or marks the plugin as 'error'

## Plugin Activation and Below-Minimum Detection

At proxy startup, `PluginRegistry.activate_all()` checks each plugin's installed version against `MIN_PLUGIN_VERSIONS`:

- **Below minimum**: plugin status set to 'error', log `plugin_version_incompatible` with hint `pip install archolith-proxy[<plugin_id>]`
- **At or above minimum**: plugin activation proceeds normally
- **"not_installed"**: plugin is absent; marked 'degraded', proxy continues (fail-open)
- **"unknown"**: version detection failed; activation proceeds (benefit of doubt)

The proxy **always starts** — version mismatch is non-fatal.

## Testing Coverage

- **Positive path (correct versions)**: covered by existing CI via successful plugin activation in the `test` job
- **Negative path (below-minimum)**: covered by `tests/test_plugins/test_registry.py::test_version_below_minimum_marks_error`, which runs in the `test` job
- **Plugin matrix (core/filter/audit/full modes)**: covered by the `plugin-matrix` job in `.github/workflows/ci.yml`, which exercises all install combinations and verifies boot success for each mode
