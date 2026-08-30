"""Zero Develop — multi-agent control plane.

See ``docs/`` for the requirement ledger, current-state ledger,
dependency map, and architecture decision records.

``__version__`` is the single source of truth for the running version;
``pyproject.toml`` reads it dynamically so a release can never ship a
package version that disagrees with what ``/healthz`` and ``zero
--version`` report.
"""

__version__ = "0.8.5"
__all__ = ["__version__"]
