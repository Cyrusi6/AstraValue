"""Exercise the real immutable knowledge service through research readers."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from analysis.research.catalog import Catalog
from analysis.research.knowledge import Knowledge
from analysis.research.workspace import ResearchError
from test_core_service import approved, build, workspace
from test_bundle_integrity import acceptance_for, full_question_scope
from test_research_workspace import workspace as research_workspace


def configure(research_workspace, service):
    research_workspace.config.update(knowledge_catalog=str(service.catalog_path),
                                    knowledge_store=str(service.store_root))


def test_explicit_missing_catalog_is_a_configuration_error(tmp_path):
    workspace = SimpleNamespace(root=tmp_path, config={"knowledge_catalog": "missing/catalog.json"})
    with pytest.raises(ResearchError, match="knowledge_catalog_not_found"):
        Knowledge(workspace).available_methods()


def test_research_catalog_keeps_original_body_when_default_changes(workspace, research_workspace):
    service, first, _ = build(workspace, full_question_scope(workspace))
    service.publish_default(first["bundle_id"], acceptance_for(service, first))
    configure(research_workspace, service)
    knowledge = Knowledge(research_workspace)
    state = research_workspace.prepare_research("600519", "2025-01-01")
    catalog = Catalog(research_workspace)
    item = catalog.list_materials(state["research_id"], "knowledge")["items"][0]
    old = catalog.read_material(state["research_id"], item["material_id"])
    assert old["bundle_id"] == first["bundle_id"]

    (workspace[0] / "docs/methodology/knowledge/example.md").write_text(
        "# New synthetic method\nChanged interpretation after review.\n", encoding="utf-8")
    new_service, _ = approved(workspace, full_question_scope(workspace))
    second = new_service.build_candidate(bundle_id="candidate-2")
    new_service.publish_default(second["bundle_id"], acceptance_for(new_service, second))
    assert knowledge.search_knowledge(card_id="knowledge.roic")["bundle_id"] == second["bundle_id"]
    pinned = catalog.read_material(state["research_id"], item["material_id"])
    assert pinned["bundle_id"] == first["bundle_id"]
    assert pinned["content"] == old["content"]
    assert pinned["metadata"]["content_sha256"] == old["metadata"]["content_sha256"]

    new_service.record_change("source", "source-a", "Synthetic source correction")
    assert knowledge.available_methods()["items"] == []
    historical = catalog.read_material(state["research_id"], item["material_id"])
    assert historical["content"] == old["content"]
    assert historical["metadata"]["notices"]
    assert historical["metadata"]["available"] is False


def test_unversioned_material_never_reads_current_default(workspace, research_workspace):
    service, bundle, _ = build(workspace, full_question_scope(workspace))
    service.publish_default(bundle["bundle_id"], acceptance_for(service, bundle))
    configure(research_workspace, service)
    state = research_workspace.prepare_research("600519", "2025-01-01")
    catalog = Catalog(research_workspace)
    _, data = catalog._load(state["research_id"])
    item = next(row for row in data["items"] if row["reader"] == "knowledge")
    item["payload"].pop("bundle_id")
    with pytest.raises(ResearchError, match="knowledge_version_missing"):
        catalog._read_material(state["research_id"], item["material_id"], loaded=data)


def test_catalog_rejects_a_different_method_identity(workspace, research_workspace):
    service, bundle, _ = build(workspace, full_question_scope(workspace))
    service.publish_default(bundle["bundle_id"], acceptance_for(service, bundle))
    configure(research_workspace, service)
    state = research_workspace.prepare_research("600519", "2025-01-01")
    catalog = Catalog(research_workspace)
    _, data = catalog._load(state["research_id"])
    item = next(row for row in data["items"] if row["reader"] == "knowledge")
    item["payload"]["content_sha256"] = "0" * 64
    with pytest.raises(ResearchError, match="knowledge_material_identity_mismatch"):
        catalog._read_material(state["research_id"], item["material_id"], loaded=data)


def test_unknown_selected_bundle_does_not_fall_back(workspace):
    service, _, _ = build(workspace)
    workspace_view = SimpleNamespace(root=workspace[0], config={
        "knowledge_catalog": str(service.catalog_path), "knowledge_store": str(service.store_root)})
    with pytest.raises(KeyError, match="unknown version"):
        Knowledge(workspace_view).search_knowledge(card_id="knowledge.roic", bundle_id="missing-version")
