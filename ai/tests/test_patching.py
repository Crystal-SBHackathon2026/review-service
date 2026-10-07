from __future__ import annotations

import copy

import pytest

from review_ai.patching import PatchError, apply_ops
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
