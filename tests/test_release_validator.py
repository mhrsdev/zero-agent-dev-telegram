from __future__ import annotations

import importlib.util
import stat
import tarfile
import warnings
import zipfile
from pathlib import Path

_SCRIPT = Path(__file__).parents[1] / "scripts" / "validate_release_artifacts.py"
_SPEC = importlib.util.spec_from_file_location("release_validator", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_VALIDATOR = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_VALIDATOR)


def test_archive_name_validation_rejects_posix_and_windows_traversal() -> None:
    unsafe = _VALIDATOR._validate_names(["../escape", r"..\escape", "/absolute", "safe/file"])
    assert unsafe == ["../escape", r"..\escape", "/absolute"]


def test_migration_and_module_detection_require_exact_package_components() -> None:
    wheel_names = [
        "zero/persistence/migrations/0001_initial.sql",
        "notzero/persistence/migrations/0002_wrong.sql",
        "evil/zero/persistence/migrations/0003_wrong.sql",
        "zero/app/planner_service.py",
        "notzero/app/result_delivery_service.py",
    ]
    assert _VALIDATOR._migration_ids(wheel_names, artifact_format="wheel") == {"0001_initial"}
    assert _VALIDATOR._required_modules_present(wheel_names, artifact_format="wheel") == {
        "zero/app/planner_service.py"
    }


def test_zip_member_validation_rejects_duplicates_and_symlinks(tmp_path) -> None:
    path = tmp_path / "probe.whl"
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("zero/__init__.py", "")
            archive.writestr("zero/__init__.py", "duplicate")
            info = zipfile.ZipInfo("zero/link")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "target")

    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert _VALIDATOR._validate_zip_members(archive, names)


def test_sdist_inspection_rejects_generated_egg_info(tmp_path) -> None:
    path = tmp_path / "probe.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        payload = b"generated"
        info = tarfile.TarInfo("probe/src/probe.egg-info/SOURCES.txt")
        info.size = len(payload)
        archive.addfile(info, __import__("io").BytesIO(payload))

    report = _VALIDATOR._inspect_sdist(path)
    assert report["generated_members"] == ["probe/src/probe.egg-info/SOURCES.txt"]


def test_raw_credential_scan_reports_category_without_value() -> None:
    findings: dict[str, int] = {}
    _VALIDATOR._scan_bytes(
        "probe",
        b"Authorization: " + b"Bearer " + b"synthetic-" + b"token",
        findings,
    )
    assert findings == {"probe:bearer_token": 1}


def test_expected_migrations_match_source_tree() -> None:
    """The gate must track the real migrations directory.

    Guards against both regressions that broke releases before: a
    hand-maintained frozen list drifting out of sync, and silent
    path-resolution breakage that would make the expected set empty
    (an empty gate passes anything).
    """
    migrations_dir = Path(__file__).parents[1] / "src" / "zero" / "persistence" / "migrations"
    from_disk = {path.stem for path in migrations_dir.glob("*.sql")}
    assert _VALIDATOR.EXPECTED_MIGRATIONS == from_disk
    assert len(_VALIDATOR.EXPECTED_MIGRATIONS) >= 30
