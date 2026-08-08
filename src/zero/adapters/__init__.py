"""Adapters — external transports and SDKs.

This package depends on :mod:`zero.domain` and :mod:`zero.app`. It is
the outermost layer and is never imported by ``domain/``, ``app/``, or
``persistence/``.

Phase 1 has no real adapters yet. The HTTP boundary in
:mod:`zero.app.api` is technically an adapter, but it lives in ``app/``
because FastAPI's router is the application's primary entry surface.
Future external adapters (Telegram, Discord, providers) will live here.
"""
