"""Persistence repositories.

Each repository maps between canonical domain types and database
rows. Repositories never import from :mod:`zero.app` or
:mod:`zero.adapters`. They depend on :mod:`zero.domain` types and on
the :class:`Database` connection wrapper.

All repositories enforce project-scoping at the query level (per
``zero-project-isolation-evidence`` §"Scope begins before access"):
queries always filter by ``project_id`` before content is loaded.
"""
