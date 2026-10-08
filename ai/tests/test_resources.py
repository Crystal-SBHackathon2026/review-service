from __future__ import annotations

from pathlib import Path

import pytest

from review_ai import resources


def test_data_dir_prefers_copy_inside_installed_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "site-packages" / "review_ai"
    (package / "_data" / "catalog").mkdir(parents=True)
    (tmp_path / "site-packages" / "catalog").mkdir()
    monkeypatch.setattr(resources, "_PACKAGE_DIR", package)

    assert resources.data_dir("catalog") == package / "_data" / "catalog"


def test_data_dir_falls_back_to_source_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = tmp_path / "ai" / "review_ai"
    package.mkdir(parents=True)
    (tmp_path / "ai" / "knowledge").mkdir()
    monkeypatch.setattr(resources, "_PACKAGE_DIR", package)

    assert resources.data_dir("knowledge") == tmp_path / "ai" / "knowledge"


def test_data_dir_fails_loudly_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resources, "_PACKAGE_DIR", tmp_path / "review_ai")

    with pytest.raises(FileNotFoundError, match="catalog/"):
        resources.data_dir("catalog")
