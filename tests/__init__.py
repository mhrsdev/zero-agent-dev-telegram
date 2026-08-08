"""Zero Develop — test suite.

Tests use :func:`zero.config.Settings.load_for_test` to construct a
forced-test :class:`Settings` instance, then call
:func:`zero.app.api.create_app` to wire the real ASGI app. The smoke
test starts the real ASGI app through httpx's ASGI transport (no
network port needed), proving the same executable path intended for
later milestones.
"""
