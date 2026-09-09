from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from analysis.structured import DEFAULT_CONFIG_DIR, StructuredRegistryError, StructuredRegistryLoader


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate all structured-data v1 registries")
    parser.add_argument("--config-dir", type=Path, default=DEFAULT_CONFIG_DIR)
    args = parser.parse_args(argv)
    try:
        bundle = StructuredRegistryLoader(args.config_dir).load()
        invalid_page_sizes = [
            (item.dataset_id, item.request.page_size)
            for item in bundle.datasets.datasets
            if item.request.page_size is not None
            and (item.request.page_size < 1 or item.request.page_size > 500)
        ]
        if invalid_page_sizes:
            raise StructuredRegistryError(
                f"page_size必须为1..500的正整数: {invalid_page_sizes}"
            )
    except StructuredRegistryError as exc:
        print(f"STRUCTURED_REGISTRY_INVALID {exc}", file=sys.stderr)
        return 1
    summary = {
        "datasets": len(bundle.datasets.datasets),
        "field_positions": len(bundle.fields.fields),
        "unclassified_field_positions": bundle.fields.classification_summary[
            "unclassified_nature"
        ],
        "definition_unknown_field_positions": bundle.fields.classification_summary[
            "definition_unknown"
        ],
        "formula_eligible_field_positions": bundle.fields.classification_summary[
            "formula_eligible"
        ],
        "questions": len(bundle.research_requirements.questions),
        "requirements": len(bundle.research_requirements.requirements),
        "reading_rules": len(bundle.reading_rules.rules),
        "industry_profiles": len(bundle.industry_profiles.profiles),
        "peer_companies": len(bundle.peer_sets.peer_sets[0].companies),
        "content_hashes": bundle.content_hashes,
    }
    print("STRUCTURED_REGISTRY_OK " + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
