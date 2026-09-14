from __future__ import annotations

import re
from pathlib import Path

import yaml

from .workspace import ResearchError, ResearchWorkspace, sha


class Knowledge:
    def __init__(self, workspace: ResearchWorkspace):
        self.w = workspace

    def _cards(self):
        cards = []
        for path in sorted((self.w.root / "docs/methodology/research-cards").glob("*.md")):
            parts = path.read_text(encoding="utf-8").split("---", 2)
            if len(parts) != 3:
                continue
            meta = yaml.safe_load(parts[1])
            if not isinstance(meta, dict) or meta.get("content_status") != "verified" or not all(meta.get(k) for k in (
                "id", "title", "author", "work", "version", "content_nature", "source_url", "locator", "source_sha256")):
                continue
            if not all(section in parts[2] for section in ("## 核心概念与公式", "## 适用商业场景", "## 研判触发指标", "## 反例与失效边界")):
                continue
            cards.append((path, meta, parts[2].strip()))
        return cards

    def search_knowledge(self, question: str = "", card_id: str | None = None, page: int = 1, page_size: int = 5):
        """Search verified theory cards; supply a returned card_id to read bounded text. Optional for research."""
        if page < 1 or not 1 <= page_size <= 10:
            raise ResearchError("invalid_pagination")
        cards = self._cards()
        if card_id:
            found = next((c for c in cards if c[1]["id"] == card_id), None)
            if not found:
                return {"status": "knowledge_gap", "card_id": card_id}
            path, meta, body = found
            from analysis.structured.research_lite import _split_utf8
            chunks = _split_utf8(body, 4000)
            if page > len(chunks):
                raise ResearchError("knowledge_page_out_of_range")
            return {"status": "ready", "metadata": meta, "content": chunks[page-1], "card_sha256": sha(path),
                    "next_page": page+1 if page < len(chunks) else None}
        tokens = [x for x in re.split(r"\W+", question.lower()) if x]
        ranked = []
        for path, meta, body in cards:
            text = (meta["title"] + " " + " ".join(meta.get("topics", [])) + " " + body).lower()
            score = sum(t in text for t in tokens)
            score += sum(t.lower() in question.lower() for t in meta.get("topics", []))
            if not question or score:
                ranked.append((score, meta))
        ranked.sort(key=lambda x: (-x[0], x[1]["id"]))
        start = (page-1)*page_size
        return {"status": "ready" if ranked else "knowledge_gap", "total": len(ranked),
                "items": [{k: m[k] for k in ("id", "title", "source_url", "locator", "content_status")} for _,m in ranked[start:start+page_size]],
                "next_page": page+1 if start+page_size < len(ranked) else None}
