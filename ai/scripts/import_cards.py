"""oneaction 리허설 지식 카드(K-*.yaml)를 knowledge/incidents/*.md 로 옮긴다. 한 번 돌리고 결과를 커밋한다.

    .venv/bin/python scripts/import_cards.py ../../../oneaction/knowledge/cards
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "knowledge" / "incidents"

# 카드 → 명세 규칙. 명세로 판정할 수 없는 카드(코드·인프라 문제)는 빈 목록 — 의미 검색으로만 찾힌다
RELATED_RULES = {
    "K-002": ["DB-003"],
    "K-009": ["DB-005", "DB-003"],
    "K-011": ["SEC-001"],
    "K-012": ["RUN-001"],
    "K-013": ["RUN-004"],
}
SECTIONS = (("symptom", "증상"), ("root_cause", "원인"), ("fix", "고치는 법"), ("verify", "확인"))


def convert(card: dict) -> str:
    front = {
        "doc_type": "incident",
        "provider": "any",
        "title": card["title"],
        "card_id": card["id"],
        "related_rules": RELATED_RULES.get(card["id"], []),
        "observed": card.get("observed", ""),
    }
    body = [f"## {label}\n{str(card[key]).strip()}\n" for key, label in SECTIONS if card.get(key)]
    return "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False) + "---\n" + "\n".join(body)


def main(cards_dir: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    paths = sorted(Path(cards_dir).glob("K-*.yaml"))
    for path in paths:
        card = yaml.safe_load(path.read_text(encoding="utf-8"))
        (OUT / f"{path.stem}.md").write_text(convert(card), encoding="utf-8")
    print(f"카드 {len(paths)}장 → {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main(sys.argv[1])
