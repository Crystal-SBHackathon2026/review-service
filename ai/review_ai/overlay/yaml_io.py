from __future__ import annotations

from typing import Any

import yaml


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)


def dump(doc: Any) -> str:
    return yaml.dump(doc, Dumper=_Dumper, sort_keys=False, allow_unicode=True, default_flow_style=False)


def dump_with_header(doc: Any, header: str) -> str:
    return "".join(f"# {line}\n" for line in header.splitlines()) + dump(doc)
