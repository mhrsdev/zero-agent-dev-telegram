#!/usr/bin/env python3
"""Validate installable Zero Develop release artifacts.

The validator checks exact package paths, archive member types, nested content,
and raw credential-shaped bytes. It is intentionally independent of the source
checkout's ignored state; the build workflow is responsible for establishing
source provenance before invoking this gate.
"""

from __future__ import annotations

import argparse
import json
import re
import stat
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath

EXPECTED_MIGRATIONS = frozenset(
    {
        "0001_initial",
        "0002_identity_authorization_tools_audit",
        "0003_plan_lifecycle",
        "0004_execution_graph",
        "0005_worktrees_and_repositories",
        "0006_agent_types",
        "0007_artifacts_rag_context",
        "0008_provider_adapters",
        "0009_integration_merge",
        "0010_interface_adapters",
        "0011_security_hardening",
        "0012_f03_f04_f05_f06_f18_f19_security_state_integrity",
        "0012_interface_delivery",
        "0012_remediation_integration_evidence",
        "0013_provider_usage_nullable_dedup",
        "0014_interface_binding_scoped_claims",
        "0015_task_artifact_immutability",
        "0016_artifact_provenance",
        "0017_cross_project_lineage",
        "0018_provider_request_identity",
        "0019_provider_lineage",
        "0020_interface_claim_fencing",
        "0021_combined_test_evidence",
        "0022_result_deliveries",
        "0023_result_delivery_outcome_fencing",
        "0024_provider_request_lease_fencing",
        "0025_project_lineage_hardening",
        "0026_project_ownership_and_legacy_provider_recovery",
        "0027_remaining_project_lineage",
        "0028_secret_key_versioning",
    }
)

REQUIRED_MODULES = frozenset(
    {
        "zero/main.py",
        "zero/config.py",
        "zero/app/api.py",
        "zero/app/planner_service.py",
        "zero/app/result_delivery_service.py",
        "zero/app/scheduler_service.py",
        "zero/persistence/connection.py",
        "zero/persistence/migrations.py",
    }
)

REQUIRED_SDIST_FILES = frozenset(
    {
        "scripts/run_dev.sh",
        "scripts/validate_release_artifacts.py",
        "tests/test_execution_isolation_boundary.py",
        "tests/test_project_lineage_sql.py",
        "tests/test_release_validator.py",
    }
)

CREDENTIAL_PATTERNS = {
    "private_key_header": re.compile(rb"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "github_token": re.compile(rb"(?:gh[pors]|github_pat)_[A-Za-z0-9_]{20,}"),
    "aws_access_key": re.compile(rb"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "slack_token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "openai_like_key": re.compile(rb"\bsk-[A-Za-z0-9]{20,}\b"),
    "google_api_key": re.compile(rb"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "telegram_bot_token": re.compile(rb"\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b"),
    "bearer_token": re.compile(rb"\bBearer\s+[A-Za-z0-9._-]{8,}\b"),
}


def _validate_names(names: Iterable[str]) -> list[str]:
    unsafe: list[str] = []
    for name in names:
        path = PurePosixPath(name)
        if "\\" in name or "\x00" in name or path.is_absolute() or ".." in path.parts:
            unsafe.append(name)
    return unsafe


def _relative_parts(name: str) -> tuple[str, ...]:
    return PurePosixPath(name).parts


def _migration_ids(names: Iterable[str], *, artifact_format: str = "wheel") -> set[str]:
    result: set[str] = set()
    for name in names:
        parts = _relative_parts(name)
        if artifact_format == "wheel":
            if len(parts) != 4 or parts[:3] != ("zero", "persistence", "migrations"):
                continue
            filename = parts[3]
        else:
            marker = ("src", "zero", "persistence", "migrations")
            if len(parts) != len(marker) + 2 or parts[-5:-1] != marker:
                continue
            filename = parts[-1]
        if filename.endswith(".sql"):
            result.add(filename[:-4])
    return result


def _required_modules_present(names: Iterable[str], *, artifact_format: str = "wheel") -> set[str]:
    result: set[str] = set()
    for name in names:
        parts = _relative_parts(name)
        for module in REQUIRED_MODULES:
            module_parts = PurePosixPath(module).parts
            if artifact_format == "wheel":
                matches = parts == module_parts
            else:
                matches = (
                    len(parts) >= len(module_parts) and parts[-len(module_parts) :] == module_parts
                )
            if matches:
                result.add(module)
    return result


def _required_sdist_files_present(names: Iterable[str]) -> set[str]:
    result: set[str] = set()
    for name in names:
        parts = _relative_parts(name)
        for relative_path in REQUIRED_SDIST_FILES:
            required_parts = PurePosixPath(relative_path).parts
            if (
                len(parts) >= len(required_parts)
                and parts[-len(required_parts) :] == required_parts
            ):
                result.add(relative_path)
    return result


def _validate_zip_members(archive: zipfile.ZipFile, names: list[str]) -> list[str]:
    issues = _validate_names(names)
    if len(names) != len(set(names)):
        issues.append("duplicate member names")
    for info in archive.infolist():
        mode = (info.external_attr >> 16) & 0o170000
        if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
            issues.append(f"non-regular zip member: {info.filename}")
    return issues


def _validate_tar_members(members: list[tarfile.TarInfo]) -> list[str]:
    names = [member.name for member in members]
    issues = _validate_names(names)
    if len(names) != len(set(names)):
        issues.append("duplicate member names")
    for member in members:
        if not (member.isdir() or member.isfile()):
            issues.append(f"non-regular tar member: {member.name}")
    return issues


def _generated_members(names: Iterable[str]) -> list[str]:
    return sorted(
        name
        for name in names
        if any(part.endswith(".egg-info") for part in PurePosixPath(name).parts)
        or name.endswith((".pyc", ".pyo"))
    )


def _scan_bytes(label: str, data: bytes, findings: dict[str, int]) -> None:
    for category, pattern in CREDENTIAL_PATTERNS.items():
        count = len(pattern.findall(data))
        if count:
            findings[f"{label}:{category}"] = findings.get(f"{label}:{category}", 0) + count


def _inspect_wheel(path: Path) -> dict[str, object]:
    findings: dict[str, int] = {}
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        archive_errors = _validate_zip_members(archive, names)
        for name in names:
            if name.endswith("/"):
                continue
            _scan_bytes(f"{path.name}:{name}", archive.read(name), findings)
    migration_ids = _migration_ids(names, artifact_format="wheel")
    modules = _required_modules_present(names, artifact_format="wheel")
    return {
        "artifact": str(path),
        "format": "wheel",
        "unsafe_paths": _validate_names(names),
        "archive_errors": archive_errors,
        "generated_members": _generated_members(names),
        "credential_matches": findings,
        "migration_count": len(migration_ids),
        "missing_migrations": sorted(EXPECTED_MIGRATIONS - migration_ids),
        "unexpected_migrations": sorted(migration_ids - EXPECTED_MIGRATIONS),
        "missing_modules": sorted(REQUIRED_MODULES - modules),
    }


def _inspect_sdist(path: Path) -> dict[str, object]:
    findings: dict[str, int] = {}
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        archive_errors = _validate_tar_members(members)
        names = [member.name for member in members]
        for member in members:
            if member.isfile():
                handle = archive.extractfile(member)
                if handle is not None:
                    _scan_bytes(f"{path.name}:{member.name}", handle.read(), findings)
    migration_ids = _migration_ids(names, artifact_format="sdist")
    modules = _required_modules_present(names, artifact_format="sdist")
    source_files = _required_sdist_files_present(names)
    return {
        "artifact": str(path),
        "format": "sdist",
        "unsafe_paths": _validate_names(names),
        "archive_errors": archive_errors,
        "generated_members": _generated_members(names),
        "credential_matches": findings,
        "migration_count": len(migration_ids),
        "missing_migrations": sorted(EXPECTED_MIGRATIONS - migration_ids),
        "unexpected_migrations": sorted(migration_ids - EXPECTED_MIGRATIONS),
        "missing_modules": sorted(REQUIRED_MODULES - modules),
        "missing_source_files": sorted(REQUIRED_SDIST_FILES - source_files),
    }


def _assert_clean(report: dict[str, object]) -> None:
    failures = {
        key: value
        for key in (
            "unsafe_paths",
            "archive_errors",
            "generated_members",
            "credential_matches",
            "missing_migrations",
            "unexpected_migrations",
            "missing_modules",
            "missing_source_files",
        )
        if (value := report.get(key))
    }
    if report["migration_count"] != len(EXPECTED_MIGRATIONS):
        failures["migration_count"] = report["migration_count"]
    if failures:
        raise SystemExit(json.dumps({"status": "failed", **report, "failures": failures}, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("dist", nargs="?", default="dist")
    args = parser.parse_args()
    dist = Path(args.dist)
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise SystemExit(
            json.dumps(
                {
                    "status": "failed",
                    "reason": "expected exactly one wheel and one sdist",
                    "wheels": [str(path) for path in wheels],
                    "sdists": [str(path) for path in sdists],
                },
                indent=2,
            )
        )
    reports = [_inspect_wheel(wheels[0]), _inspect_sdist(sdists[0])]
    for report in reports:
        _assert_clean(report)
    print(
        json.dumps(
            {"status": "ok", "expected_migrations": len(EXPECTED_MIGRATIONS), "artifacts": reports},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
