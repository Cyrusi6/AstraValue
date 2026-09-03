from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import re
from pathlib import Path
from typing import Iterable

from .models import MethodBundle, MethodSpec


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config" / "methods"


class MethodRegistryError(RuntimeError):
    pass


class MethodRegistry:
    def __init__(self, config_dir: Path | str = DEFAULT_CONFIG_DIR) -> None:
        self.config_dir = Path(config_dir)
        self._methods: dict[str, dict[str, MethodSpec]] = {}
        self._industry_routes: dict = {}
        self.reload()

    def reload(self) -> None:
        specs_path = self.config_dir / "method_specs.json"
        routes_path = self.config_dir / "industry_routes.json"
        if not specs_path.exists():
            raise MethodRegistryError(f"缺少方法配置: {specs_path}")
        raw_specs = json.loads(specs_path.read_text(encoding="utf-8"))
        self._methods.clear()
        for item in raw_specs["methods"]:
            spec = MethodSpec.model_validate(item)
            versions = self._methods.setdefault(spec.method_id, {})
            if spec.version in versions:
                raise MethodRegistryError(f"重复方法版本: {spec.ref}")
            versions[spec.version] = spec
        for method_id, versions in self._methods.items():
            active = [item for item in versions.values() if item.status == "active"]
            if len(active) != 1:
                raise MethodRegistryError(f"{method_id} 必须且只能有一个active版本，当前为{len(active)}个")
        self._industry_routes = json.loads(routes_path.read_text(encoding="utf-8"))

    @property
    def industry_routes(self) -> dict:
        return self._industry_routes

    def list_methods(self, active_only: bool = True) -> list[MethodSpec]:
        result = [spec for versions in self._methods.values() for spec in versions.values()]
        if active_only:
            result = [spec for spec in result if spec.status == "active"]
        return sorted(result, key=lambda item: (item.method_id, item.version))

    def get(self, method_id: str, version: str | None = None) -> MethodSpec:
        try:
            versions = self._methods[method_id]
        except KeyError as exc:
            raise MethodRegistryError(f"未知方法: {method_id}") from exc
        if version is not None:
            try:
                return versions[version]
            except KeyError as exc:
                raise MethodRegistryError(f"未知方法版本: {method_id}@{version}") from exc
        active = [spec for spec in versions.values() if spec.status == "active"]
        if len(active) != 1:
            raise MethodRegistryError(f"{method_id} 必须且只能有一个active版本")
        return active[0]

    def create_bundle(self, method_ids: Iterable[str]) -> MethodBundle:
        specs = [self.get(method_id) for method_id in sorted(set(method_ids))]
        method_hashes = {spec.ref: self._method_hash(spec) for spec in specs}
        canonical = json.dumps(
            {
                "methods": [spec.model_dump(mode="json") for spec in specs],
                "method_hashes": method_hashes,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        return MethodBundle(method_bundle_id=f"mb-{digest[:20]}", methods=specs, method_hashes=method_hashes)

    def get_document(self, method_id: str, version: str | None = None) -> str:
        spec = self.get(method_id, version)
        path = PROJECT_ROOT / spec.doc_path
        if not path.exists():
            raise MethodRegistryError(f"缺少方法文档: {spec.doc_path}")
        return path.read_text(encoding="utf-8")

    def versions(self, method_id: str) -> list[MethodSpec]:
        if method_id not in self._methods:
            raise MethodRegistryError(f"未知方法: {method_id}")
        return sorted(self._methods[method_id].values(), key=lambda item: item.version)

    @staticmethod
    def _method_hash(spec: MethodSpec) -> str:
        doc_path = PROJECT_ROOT / spec.doc_path
        document = doc_path.read_bytes() if doc_path.exists() else b"<missing-document>"
        try:
            implementation = inspect.getsource(_resolve_symbol(spec.implementation_ref)).encode("utf-8")
        except (ImportError, AttributeError, OSError, TypeError, ValueError):
            implementation = b"<missing-implementation>"
        payload = json.dumps(spec.model_dump(mode="json"), ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(payload + b"\0" + document + b"\0" + implementation).hexdigest()

    def validate_library(self, project_root: Path | str = PROJECT_ROOT) -> list[str]:
        root = Path(project_root)
        errors: list[str] = []
        semver = re.compile(r"^\d+\.\d+\.\d+$")
        seen_docs: dict[Path, str] = {}
        configured_refs: set[str] = set()
        required_headings = {
            "分析目标与经济含义",
            "输入字段",
            "公式、单位与期间口径",
            "数据源与双源核验",
            "适用范围与强制停止条件",
            "缺失、异常与冲突处理",
            "输出与报告展示",
            "案例",
            "实现、配置与测试",
            "已知局限与变更日志",
        }
        for spec in self.list_methods(active_only=False):
            configured_refs.add(spec.ref)
            if not semver.match(spec.version):
                errors.append(f"{spec.ref}: version不是语义化版本")
            doc = root / spec.doc_path
            if not doc.exists():
                errors.append(f"{spec.ref}: 缺少文档 {spec.doc_path}")
            else:
                text = doc.read_text(encoding="utf-8")
                doc_method = _front_matter_value(text, "method_id")
                doc_version = _front_matter_value(text, "version")
                if doc_method != spec.method_id:
                    errors.append(f"{spec.ref}: 文档method_id为 {doc_method!r}")
                if doc_version != spec.version:
                    errors.append(f"{spec.ref}: 文档version为 {doc_version!r}")
                if doc in seen_docs:
                    errors.append(f"{spec.ref}: 与 {seen_docs[doc]} 共用文档，要求一方法一文档")
                seen_docs[doc] = spec.ref
                headings = {match.group(1).strip() for match in re.finditer(r"^##\s+(.+)$", text, re.M)}
                missing_headings = sorted(required_headings - headings)
                if missing_headings:
                    errors.append(f"{spec.ref}: 文档缺少章节 {', '.join(missing_headings)}")
            test_path = root / spec.test_ref.split("::", 1)[0]
            if not test_path.exists():
                errors.append(f"{spec.ref}: 缺少测试引用 {spec.test_ref}")
            elif "::" in spec.test_ref:
                test_name = spec.test_ref.split("::", 1)[1]
                test_text = test_path.read_text(encoding="utf-8")
                if not re.search(rf"^def\s+{re.escape(test_name)}\s*\(", test_text, re.M):
                    errors.append(f"{spec.ref}: 测试函数不存在 {spec.test_ref}")
            try:
                _resolve_symbol(spec.implementation_ref)
            except (ImportError, AttributeError, ValueError) as exc:
                errors.append(f"{spec.ref}: 实现引用无效 {spec.implementation_ref}: {exc}")
        methodology_root = root / "docs" / "methodology"
        if methodology_root.exists():
            for doc in methodology_root.rglob("*.md"):
                text = doc.read_text(encoding="utf-8")
                method_id = _front_matter_value(text, "method_id")
                version = _front_matter_value(text, "version")
                if method_id and version and f"{method_id}@{version}" not in configured_refs:
                    errors.append(f"孤立方法文档: {doc.relative_to(root)} ({method_id}@{version})")
        return errors


def _front_matter_value(text: str, key: str) -> str | None:
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    for line in text[3:end].splitlines():
        if line.strip().startswith(f"{key}:"):
            return line.split(":", 1)[1].strip().strip('"\'')
    return None


def _resolve_symbol(reference: str):
    module_name, symbol_name = reference.split(":", 1)
    module = importlib.import_module(module_name)
    return getattr(module, symbol_name)
