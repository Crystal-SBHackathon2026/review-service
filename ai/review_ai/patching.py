"""deploy_spec 에 JSON Patch(RFC 6902 의 add·replace·remove)를 적용한다. 입력은 바꾸지 않는다."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

from review_ai.state import PatchOp

SUPPORTED_OPS = frozenset({"add", "replace", "remove"})


class PatchError(ValueError):
    pass


def parse_pointer(path: str) -> list[str]:
    if not path.startswith("/"):
        raise PatchError(f"JSON Pointer 는 / 로 시작해야 한다: {path!r}")
    return [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]


def _index(container: list[Any], token: str, *, allow_end: bool) -> int:
    if allow_end and token == "-":
        return len(container)
    if not token.isdigit():
        raise PatchError(f"리스트 인덱스가 아니다: {token!r}")
    idx = int(token)
    limit = len(container) if allow_end else len(container) - 1
    if idx > limit:
        raise PatchError(f"인덱스 범위 밖: {idx}")
    return idx


def _parent(doc: Any, tokens: list[str]) -> Any:
    node = doc
    for token in tokens[:-1]:
        if isinstance(node, dict) and token in node:
            node = node[token]
        elif isinstance(node, list):
            node = node[_index(node, token, allow_end=False)]
        else:
            raise PatchError(f"경로가 없다: /{'/'.join(tokens)}")
    return node


def _apply(doc: Any, op: PatchOp) -> None:
    kind = op.get("op")
    if kind not in SUPPORTED_OPS:
        raise PatchError(f"지원하지 않는 op: {kind!r}")
    tokens = parse_pointer(op["path"])
    parent, last = _parent(doc, tokens), tokens[-1]
    if isinstance(parent, dict):
        if kind != "add" and last not in parent:
            raise PatchError(f"경로가 없다: {op['path']}")
        if kind == "remove":
            del parent[last]
        else:
            parent[last] = copy.deepcopy(op.get("value"))
        return
    if not isinstance(parent, list):
        raise PatchError(f"경로가 없다: {op['path']}")
    idx = _index(parent, last, allow_end=kind == "add")
    if kind == "add":
        parent.insert(idx, copy.deepcopy(op.get("value")))
    elif kind == "replace":
        parent[idx] = copy.deepcopy(op.get("value"))
    else:
        del parent[idx]


def apply_ops(spec: dict[str, Any], ops: Sequence[PatchOp]) -> dict[str, Any]:
    doc = copy.deepcopy(spec)
    for op in ops:
        _apply(doc, op)
    return doc
