from pathlib import Path

from setuptools import setup
from setuptools.command.sdist import sdist as _sdist


class CleanSdist(_sdist):
    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        filtered = [
            path
            for path in files
            if not any(part.endswith(".egg-info") for part in Path(path).parts)
        ]
        super().make_release_tree(base_dir, filtered)


setup(cmdclass={"sdist": CleanSdist})
