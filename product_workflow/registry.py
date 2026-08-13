from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONFIG_ROOT = ROOT / "configs"


@dataclass(frozen=True)
class CategoryDefinition:
    id: str
    label: str
    supported: bool
    strategy_id: str | None
    subcategories: tuple[str, ...]
    aliases: tuple[str, ...]


class CategoryRegistry:
    def __init__(self, config_dir: Path = CONFIG_ROOT / "categories") -> None:
        self.definitions = tuple(
            CategoryDefinition(
                id=data["id"], label=data["label"], supported=bool(data["supported"]),
                strategy_id=data.get("strategy_id"), subcategories=tuple(data.get("subcategories", [])),
                aliases=tuple(data.get("aliases", [])),
            )
            for path in sorted(config_dir.glob("*.json"))
            for data in (json.loads(path.read_text(encoding="utf-8")),)
        )
        if not self.definitions:
            raise RuntimeError(f"No category definitions found in {config_dir}")

    def catalog_for_prompt(self) -> str:
        strategies = StrategyRegistry()
        lines = []
        for item in self.definitions:
            panel_brief = ""
            if item.supported and item.strategy_id:
                strategy = strategies.get(item.strategy_id)
                panel_brief = "; five concept purposes in required order=" + ", ".join(
                    f"{panel['id']}({panel['copy_purpose']})" for panel in strategy["panels"]
                )
            lines.append(
                f"- {item.id} ({item.label}): {', '.join(item.subcategories)}; "
                f"status={'supported' if item.supported else 'unsupported'}{panel_brief}"
            )
        return "\n".join(lines)

    def resolve(self, category_group: str, subcategory: str = "") -> CategoryDefinition:
        needle = category_group.strip().casefold()
        sub = subcategory.strip().casefold()
        for item in self.definitions:
            names = {item.id.casefold(), item.label.casefold(), *(value.casefold() for value in item.aliases)}
            if needle in names:
                return item
        for item in self.definitions:
            if sub and sub in {value.casefold() for value in item.subcategories}:
                return item
        return next(item for item in self.definitions if item.id == "other_non_apparel")


class StrategyRegistry:
    def __init__(self, config_dir: Path = CONFIG_ROOT / "strategies") -> None:
        self.strategies: dict[str, dict[str, Any]] = {}
        for path in sorted(config_dir.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.strategies[data["id"]] = data
        if not self.strategies:
            raise RuntimeError(f"No display strategies found in {config_dir}")

    def get(self, strategy_id: str) -> dict[str, Any]:
        try:
            return self.strategies[strategy_id]
        except KeyError as exc:
            raise ValueError(f"Unknown display strategy: {strategy_id}") from exc


def select_strategy(analysis: Any, categories: CategoryRegistry | None = None) -> tuple[CategoryDefinition, dict[str, Any]]:
    category_registry = categories or CategoryRegistry()
    category = category_registry.resolve(analysis.identity.category_group, analysis.identity.subcategory)
    if not category.supported or analysis.identity.support_status == "unsupported":
        reason = analysis.identity.rejection_reason or f"暂不支持{category.label}商品"
        raise ValueError(f"UNSUPPORTED_PRODUCT: {reason}")
    if category.strategy_id is None:
        raise ValueError(f"Category {category.id} has no display strategy")
    return category, StrategyRegistry().get(category.strategy_id)
