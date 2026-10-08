from __future__ import annotations

import copy

import pytest

from review_ai.patching import PatchError, apply_ops, changed_paths, path_under
from tests.conftest import load_sample_dict


def test_replace_returns_new_dict_and_keeps_input() -> None:
    spec = load_sample_dict("03-fix-sqlite-replicas-gcp.yaml")
    before = copy.deepcopy(spec)
    out = apply_ops(spec, [{"op": "replace", "path": "/runtime/replicas", "value": 1}])
    assert out["runtime"]["replicas"] == 1
    assert spec == before


def test_add_to_dict_and_list_end() -> None:
    spec = load_sample_dict("02-pass-local-sqlite.yaml")
    vol = {"name": "cache", "mount_path": "/cache", "size": "1Gi", "persistent": False}
    out = apply_ops(spec, [
        {"op": "add", "path": "/runtime/env", "value": {}},
        {"op": "add", "path": "/runtime/env/LOG_LEVEL", "value": "info"},
        {"op": "add", "path": "/storage/volumes/-", "value": vol},
    ])
    assert out["runtime"]["env"] == {"LOG_LEVEL": "info"}
    assert out["storage"]["volumes"][-1] == vol


def test_remove_and_escaped_pointer() -> None:
    spec = {"a": {"x/y": 1, "b": [1, 2, 3]}}
    out = apply_ops(spec, [{"op": "remove", "path": "/a/x~1y"}, {"op": "remove", "path": "/a/b/1"}])
    assert out == {"a": {"b": [1, 3]}}


@pytest.mark.parametrize(
    "op",
    [
        {"op": "replace", "path": "/runtime/nope", "value": 1},
        {"op": "remove", "path": "/missing"},
        {"op": "replace", "path": "/storage/volumes/9", "value": {}},
        {"op": "move", "path": "/runtime", "value": 1},
        {"op": "replace", "path": "runtime", "value": 1},
    ],
)
def test_invalid_ops_raise(op: dict) -> None:
    with pytest.raises(PatchError):
        apply_ops(load_sample_dict("02-pass-local-sqlite.yaml"), [op])


def test_changed_paths_sees_leaves_not_op_paths() -> None:
    before = {"db": {"engine": "mysql", "version": "8.0"}, "buckets": [{"name": "a", "public": True}], "env": {}}
    after = {"db": {"engine": "mysql", "version": "16"}, "buckets": [], "env": {"X": "1"}}
    assert changed_paths(before, after) == {
        "/db/version", "/buckets", "/buckets/0/name", "/buckets/0/public", "/env", "/env/X",
    }
    assert changed_paths(before, copy.deepcopy(before)) == set()


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("/storage/buckets/0/public", "/storage/buckets/*/public", True),
        ("/storage/buckets/0/name", "/storage/buckets/*/public", False),
        ("/storage/buckets", "/storage/buckets/*/public", False),
        ("/database/engine", "/database/engine", True),
        ("/database/engineering", "/database/engine", False),
        ("/storage/volumes/1/size", "/storage/volumes", True),
    ],
)
def test_path_under_matches_wildcard_index(path: str, pattern: str, expected: bool) -> None:
    assert path_under(path, pattern) is expected
