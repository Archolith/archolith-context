#!/usr/bin/env python3
"""Verify archolith-proxy boots in the expected plugin mode.

Modes:
  core   -> both audit and filter report "not_installed"
  filter -> filter is NOT "not_installed"; audit IS "not_installed"
  audit  -> audit is NOT "not_installed"; filter IS "not_installed"
  full   -> neither audit nor filter is "not_installed"

Exit 0 on success (mode matches), 1 on failure.
"""

import sys


def main() -> int:
    """Verify proxy boots with expected plugin configuration."""
    if len(sys.argv) != 2:
        print("Usage: python check_plugin_mode.py <mode>", file=sys.stderr)
        print("  mode: core | filter | audit | full", file=sys.stderr)
        return 1

    expected_mode = sys.argv[1]
    valid_modes = {"core", "filter", "audit", "full"}
    if expected_mode not in valid_modes:
        print(f"Invalid mode: {expected_mode}. Must be one of {valid_modes}", file=sys.stderr)
        return 1

    # Import proxy main to verify it boots
    try:
        import archolith_proxy.main  # noqa: F401
    except Exception as exc:
        print(f"FAIL: Failed to import archolith_proxy.main: {exc}", file=sys.stderr)
        return 1

    # Instantiate plugins and check versions
    try:
        from archolith_proxy.plugins.audit_plugin import AuditPlugin
        from archolith_proxy.plugins.filter_plugin import FilterPlugin

        audit = AuditPlugin()
        filter_plugin = FilterPlugin()

        audit_ver = audit.plugin_version
        filter_ver = filter_plugin.plugin_version
    except Exception as exc:
        print(f"FAIL: Failed to instantiate plugins: {exc}", file=sys.stderr)
        return 1

    # Validate against expected mode
    audit_installed = audit_ver != "not_installed"
    filter_installed = filter_ver != "not_installed"

    valid = False
    if expected_mode == "core":
        valid = not audit_installed and not filter_installed
    elif expected_mode == "filter":
        valid = filter_installed and not audit_installed
    elif expected_mode == "audit":
        valid = audit_installed and not filter_installed
    elif expected_mode == "full":
        valid = audit_installed and filter_installed

    if valid:
        print(f"PASS: mode={expected_mode} audit={audit_ver} filter={filter_ver}")
        return 0
    else:
        print(
            f"FAIL: Expected mode={expected_mode}, but audit={audit_ver} filter={filter_ver}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
