"""파싱이 안 되는 deploy.yaml 원문을 줄 단위로 읽는다 — LLM 에 보내기 전 비밀 가리기, 원문 값 추출.

yaml.safe_load 가 실패한 원문에는 mask_spec 을 쓸 수 없고, 값 모양만 보는 secrets_pattern.redact() 는
`DB_PASSWORD: hunter2` 처럼 이름으로만 비밀인 값을 못 가린다. 그래서 텍스트에서 직접 찾는다.
- redact_lines: 이름이 비밀인 키의 값(블록 스타일·흐름 스타일·`KEY=값`·k8s 식 name/value 쌍·`|` 블록)과
  값 모양이 비밀인 문자열을 가린다. 판별 기준은 SEC-001 과 같다 (secrets_pattern)
- read_lines: 들여쓰기로 경로를 추적해 `키: 값` 줄의 말단 값을 읽는다(best-effort). 값을 못 읽은 줄은
  글자 그대로 raw 에 남긴다. 복구 게이트가 'LLM 이 원문 값을 바꿨나'를 볼 때 쓴다
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import yaml

from review_ai.secrets_pattern import MASK, is_secret_name, redact

Path = tuple[str | int, ...]

QUOTED_MASK = f'"{MASK}"'
_KEY = r"""(?:"[^"\n]*"|'[^'\n]*'|[^\s"'#\[\]{},:][^#:\n]*?)"""
KEY_LINE = re.compile(rf"^(?P<key>{_KEY})\s*:(?:\s+(?P<rest>.*))?$")
# 이름이 비밀인 키 — 블록(`KEY: v`)·흐름(`{KEY: v, ...}`)·셸(`KEY=v`) 어디든. 값은 따로 VALUE 로 잡는다:
# 값까지 한 정규식으로 먹으면 `cmd: run DB_PASSWORD=x` 처럼 앞 키의 값 안에 있는 비밀 이름을 건너뛴다
SECRET_NAME = re.compile(r"""(?:^|(?<=[\s{,\[-]))["']?(?P<name>[A-Za-z_][\w.-]*)["']?\s*(?::(?=\s|$)|=)\s*""")
VALUE = re.compile(r""""[^"\n]*"|'[^'\n]*'|[^\s,{}\[\]#][^,}\]#\n]*""")
# k8s 식 env 항목을 흐름 스타일로 쓴 것: {name: DB_PASSWORD, value: hunter2}
FLOW_NAME_VALUE = re.compile(r"""name\s*:\s*["']?(?P<name>[A-Za-z_]\w*)["']?\s*,\s*value\s*:\s*""")
BLOCK_SCALAR = re.compile(r"^[|>][-+0-9]*$")
# 명세 필드 이름 중 비밀 이름처럼 보이는 것 — `secrets:` 아래는 참조(이름·출처) 목록이지 값이 아니다
SCHEMA_KEYS = frozenset({"secrets"})


def _strip_comment(text: str) -> str:
    quote = ""
    for i, ch in enumerate(text):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'" and (i == 0 or text[i - 1] in " \t:,[{-"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i]
    return text


def _unquote(key: str) -> str:
    key = key.strip()
    return key[1:-1] if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'" else key


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _secret_span(line: str, start: int) -> tuple[int, bool]:
    """비밀 값의 끝과 '값이 다음 줄로 이어지는가'. 이어지는 값(빈 값·닫히지 않은 따옴표·괄호)은 줄 끝까지 가린다."""
    end = len(_strip_comment(line).rstrip())
    rest = line[start:end]
    if not rest.strip():
        return start, True                      # KEY: ⏎ 다음 줄들이 값
    if rest[0] in "{[":
        return end, rest.count(rest[0]) > rest.count("}" if rest[0] == "{" else "]")
    value = VALUE.match(line, start)
    text = value[0].rstrip() if value else ""
    if text[:1] in "\"'" and (len(text) < 2 or text[-1] != text[0]):
        return end, True                        # 닫히지 않은 따옴표 — 여러 줄 문자열
    return start + len(text), False


def _mask_pairs(line: str, pattern: re.Pattern[str]) -> tuple[str, bool]:
    """비밀 이름 뒤의 값을 가린다. 값이 다음 줄로 이어지면 True."""
    spans, continues = [], False
    for m in pattern.finditer(line):
        if not is_secret_name(m["name"]) or m["name"] in SCHEMA_KEYS:
            continue
        end, more = _secret_span(line, m.end())
        continues = continues or more
        if end > m.end():
            spans.append((m.end(), end))
    for start, end in reversed(spans):
        line = line[:start] + QUOTED_MASK + line[end:]
    return line, continues


def redact_lines(text: str) -> str:
    """원문 줄 구조는 그대로, 비밀 값만 "***MASKED***" 로. 줄 수가 같아 오류 줄 번호가 그대로 맞는다."""
    out: list[str] = []
    block_over: int | None = None      # 비밀 값이 이어지는 줄(| 블록·다음 줄 값·여러 줄 따옴표) — 더 깊은 줄은 전부 값
    name_col: int | None = None        # 비밀 이름을 가진 `name:` 의 칸 — 같은 칸의 `value:` 를 가린다
    for line in text.splitlines():
        body = line.replace("\t", "  ")
        indent = _indent(body)
        if block_over is not None:
            if not body.strip() or indent > block_over:
                out.append(" " * indent + QUOTED_MASK if body.strip() else line)
                continue
            block_over = None
        content = body.strip()
        key_col = indent + 2 if content.startswith("- ") else indent
        key_text = content[2:].lstrip() if content.startswith("- ") else content
        m = KEY_LINE.match(_strip_comment(key_text).rstrip())
        if name_col is not None and (key_col < name_col or content.startswith("- ") and key_col <= name_col):
            name_col = None
        if m and name_col == key_col and _unquote(m["key"]) == "value" and m["rest"]:
            line = body[:len(body) - len(key_text)] + key_text[:m.start("rest")] + QUOTED_MASK
        elif m and _unquote(m["key"]) == "name" and m["rest"] and is_secret_name(_unquote(m["rest"])):
            name_col = key_col
        if m and is_secret_name(_unquote(m["key"])) and BLOCK_SCALAR.match((m["rest"] or "").strip()):
            block_over = key_col
        line, flow_more = _mask_pairs(line, FLOW_NAME_VALUE)
        line, more = _mask_pairs(line, SECRET_NAME)
        if (flow_more or more) and block_over is None:
            block_over = indent
        out.append(redact(line))
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


def flatten(value: Any, path: Path = ()) -> Iterator[tuple[Path, Any]]:
    """중첩 dict·list → (경로, 말단 값). 빈 dict·list 도 말단이다."""
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from flatten(child, (*path, str(key)))
    elif isinstance(value, list | tuple) and value:
        for i, child in enumerate(value):
            yield from flatten(child, (*path, i))
    else:
        yield path, value


@dataclass
class LineRead:
    values: dict[Path, Any] = field(default_factory=dict)    # 읽은 말단 값
    line_of: dict[Path, int] = field(default_factory=dict)   # 그 값이 있던 줄 (1부터)
    raw: dict[Path, str] = field(default_factory=dict)       # 값을 못 읽은 줄의 글자 — 경로는 그 키(또는 부모)
    raw_line: dict[Path, int] = field(default_factory=dict)
    ambiguous: set[Path] = field(default_factory=set)        # 같은 경로가 두 번 나왔다

    def put(self, path: Path, value: Any, line: int) -> None:
        for leaf, v in flatten(value, path):
            if leaf in self.values and self.values[leaf] != v:
                self.ambiguous.add(leaf)
            self.values[leaf], self.line_of[leaf] = v, line

    def put_raw(self, path: Path, text: str, line: int) -> None:
        self.raw[path] = f"{self.raw[path]} {text}" if path in self.raw else text
        self.raw_line.setdefault(path, line)


def _scalar(rest: str) -> tuple[bool, Any]:
    try:
        return True, yaml.safe_load(rest)
    except yaml.YAMLError:
        # 따옴표·괄호로 시작하지 않으면 평문 문자열로 본다 (예: `a: b: c`). 열린 괄호는 못 읽은 값
        return (False, None) if rest[:1] in "[{\"'&*!|>%@`" else (True, rest)


MAX_FLOW_LINES = 30


def _value(rest: str, lines: list[str], no: int) -> tuple[bool, Any, int]:
    """값과 그 값이 끝난 줄 번호. 여러 줄에 걸친 흐름 값({…}·[…])은 다음 줄을 붙여 읽힐 때까지 이어 본다."""
    ok, value = _scalar(rest)
    if ok or rest[:1] not in "{[":
        return ok, value, no
    joined = rest
    for nxt in range(no, min(no + MAX_FLOW_LINES, len(lines))):
        joined += " " + _strip_comment(lines[nxt].replace("\t", "  ")).strip()
        try:
            return True, yaml.safe_load(joined), nxt + 1
        except yaml.YAMLError:
            continue
    return False, None, no


def read_lines(text: str) -> LineRead:
    """들여쓰기로 경로를 추적한다. 리스트 '-' 는 부모 키와 같은 칸이어도 자식이다(칸 + 0.5 로 비교)."""
    read = LineRead()
    stack: list[tuple[float, Path]] = [(-1, ())]
    counters: dict[Path, int] = {}
    block_over: int | None = None
    lines = text.splitlines()
    done = 0  # 여러 줄 흐름 값으로 이미 읽은 마지막 줄
    for no, line in enumerate(lines, 1):
        if no <= done:
            continue
        body = _strip_comment(line.replace("\t", "  ")).rstrip()
        if not body.strip() or body.strip() in ("---", "..."):
            continue
        indent = _indent(body)
        if block_over is not None and indent > block_over:
            continue
        block_over = None
        content, col = body.strip(), indent
        if content == "-" or content.startswith("- "):
            while stack[-1][0] >= col + 0.5:
                stack.pop()
            parent = stack[-1][1]
            counters[parent] = counters.get(parent, -1) + 1
            item = (*parent, counters[parent])
            stack.append((col + 0.5, item))
            after = content[1:]
            content, col = after.strip(), col + 1 + _indent(after)
            if not content:
                continue
            if not KEY_LINE.match(content):
                ok, value, done = _value(content, lines, no)
                read.put(item, value, no) if ok else read.put_raw(item, content, no)
                continue
        m = KEY_LINE.match(content)
        while stack[-1][0] >= col:
            stack.pop()
        if m is None:  # 콜론이 없는 줄·여러 줄에 걸친 흐름 값 — 부모 경로에 글자로 남긴다
            read.put_raw(stack[-1][1], content, no)
            continue
        path = (*stack[-1][1], _unquote(m["key"]))
        rest = (m["rest"] or "").strip()
        if not rest:
            stack.append((col, path))
            continue
        if BLOCK_SCALAR.match(rest):
            block_over = col
            read.put_raw(path, rest, no)
            continue
        ok, value, done = _value(rest, lines, no)
        read.put(path, value, no) if ok else read.put_raw(path, rest, no)
    return read
