from __future__ import annotations

from dataclasses import dataclass

from .registry import MethodRegistry


@dataclass(frozen=True)
class IndustryRoute:
    key: str
    label: str
    industry_method_id: str
    valuation_method_ids: tuple[str, ...]
    required_metrics: tuple[str, ...]
    warnings: tuple[str, ...]


class IndustryRouter:
    def __init__(self, registry: MethodRegistry) -> None:
        config = registry.industry_routes
        self._routes = config["routes"]
        self._aliases = {alias.lower(): key for alias, key in config["aliases"].items()}
        self._default = config["default_route"]

    def resolve(self, industry: str) -> IndustryRoute:
        normalized = industry.strip().lower()
        candidates = [normalized]
        for suffix in ("行业", "业"):
            if normalized.endswith(suffix) and len(normalized) > len(suffix):
                candidates.append(normalized[: -len(suffix)])
        key = next(
            (self._aliases[item] for item in candidates if item in self._aliases),
            normalized if normalized in self._routes else self._default,
        )
        data = self._routes[key]
        return IndustryRoute(
            key=key,
            label=data["label"],
            industry_method_id=data["industry_method_id"],
            valuation_method_ids=tuple(data["valuation_method_ids"]),
            required_metrics=tuple(data.get("required_metrics", [])),
            warnings=tuple(data.get("warnings", [])),
        )


def route_industry(industry: str, registry: MethodRegistry | None = None) -> IndustryRoute:
    return IndustryRouter(registry or MethodRegistry()).resolve(industry)
