from pathlib import Path
from types import SimpleNamespace

from analysis.research.knowledge import Knowledge


def test_verified_card_search_read_and_skeleton_exclusion(tmp_path):
    root = Path(__file__).resolve().parents[1]
    card = (root / "docs/methodology/research-cards/cashflow-definition.md").read_text(encoding="utf-8")
    directory = tmp_path / "docs/methodology/research-cards"
    directory.mkdir(parents=True)
    (directory / "verified.md").write_text(card, encoding="utf-8")
    (directory / "skeleton.md").write_text(card.replace("content_status: verified", "content_status: skeleton").replace("id: cashflow-definition", "id: skeleton"), encoding="utf-8")
    knowledge = Knowledge(SimpleNamespace(root=tmp_path))
    index = knowledge.search_knowledge("现金流")
    assert index["total"] == 1
    assert "content" not in index
    read = knowledge.search_knowledge(card_id=index["items"][0]["id"])
    assert read["metadata"]["locator"] == "PDF pages 17 and 94"
    assert "FCFF" in read["content"]
    assert knowledge.search_knowledge(card_id="skeleton")["status"] == "knowledge_gap"
    assert knowledge.search_knowledge("不存在的知识主题")["status"] == "knowledge_gap"


def test_long_card_preserves_conditions_across_pages(tmp_path):
    root = Path(__file__).resolve().parents[1]
    card = (root / "docs/methodology/research-cards/cashflow-definition.md").read_text(encoding="utf-8")
    directory = tmp_path / "docs/methodology/research-cards"
    directory.mkdir(parents=True)
    (directory / "long.md").write_text(card + "\n" + "必要条件。" * 2000 + "末尾失效边界", encoding="utf-8")
    knowledge = Knowledge(SimpleNamespace(root=tmp_path))
    parts = []
    page = 1
    while page:
        row = knowledge.search_knowledge(card_id="cashflow-definition", page=page)
        parts.append(row["content"])
        page = row["next_page"]
    assert len(parts) > 1
    assert "末尾失效边界" in "".join(parts)
