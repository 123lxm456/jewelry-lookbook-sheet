from __future__ import annotations

import json
from pathlib import Path

from jewelry_workflow.product_spec import ProductSpec as LegacyProductSpec

from .models import (
    BrandingSpec,
    CopySection,
    DesignSpec,
    DetailSpec,
    IdentitySpec,
    IntegritySpec,
    MarketingCopy,
    PhysicalSpec,
    ProductSpec,
    PromptFacts,
    SourceMetadata,
    UsageSpec,
)


JEWELRY_PANEL_IDS = ("worn", "detail", "still_life", "gift", "mirror")


def convert_legacy_spec(legacy: LegacyProductSpec) -> ProductSpec:
    stones = [legacy.appearance.main_stone, *legacy.appearance.accent_stones]
    materials = [legacy.appearance.likely_metal, *(stone.visible_type for stone in stones)]
    textures = [*legacy.appearance.setting_methods, *(setting for stone in stones for setting in stone.settings)]
    important = [*legacy.design.visual_selling_points, *legacy.design.motifs]
    copies = [
        CopySection(panel_id=panel_id, **section.model_dump())
        for panel_id, section in zip(JEWELRY_PANEL_IDS, legacy.marketing_copy.sections)
    ]
    return ProductSpec(
        identity=IdentitySpec(
            category_group="jewelry",
            subcategory=legacy.identity.category,
            product_name=legacy.identity.product_name,
            item_count=legacy.identity.item_count,
            category_confidence=legacy.identity.category_confidence,
            support_status="supported",
        ),
        physical=PhysicalSpec(
            materials=materials,
            colors=[legacy.appearance.metal_color, *legacy.appearance.gem_colors],
            shape=legacy.design.shape_and_proportion,
            structure=legacy.design.structure,
            components=legacy.design.motifs or [legacy.identity.category],
            surface_texture=list(dict.fromkeys(textures)),
            shape_and_proportion=legacy.design.shape_and_proportion,
            size_cues="旧版规格未记录绝对尺寸，仅保留图片中的相对比例。",
        ),
        branding=BrandingSpec(visible=False, elements=[], placement="旧版规格未记录品牌元素", confidence=0),
        details=DetailSpec(
            important_details=important or [legacy.design.structure],
            construction_details=[legacy.design.structure],
            functional_details=[legacy.identity.wearing_location],
            observed_text=[],
        ),
        usage=UsageSpec(
            usage_scene=f"在{legacy.identity.wearing_location}自然佩戴",
            interaction_methods=[legacy.generation.wearing_instruction_en],
            recommended_display_methods=["佩戴", "结构微距", "静物", "礼赠", "镜面佩戴"],
        ),
        design=DesignSpec(
            style_keywords=legacy.design.style_keywords,
            visual_selling_points=legacy.design.visual_selling_points,
        ),
        integrity=IntegritySpec(
            must_preserve=legacy.generation.integrity_constraints_en,
            must_not_invent=legacy.generation.forbidden_additions_en,
            uncertain_attributes=[],
        ),
        prompt_facts=PromptFacts(
            subject_description_en=legacy.generation.subject_description_en,
            material_appearance_en=(
                f"Visible {legacy.appearance.metal_color} metal appearance with "
                f"{', '.join(legacy.appearance.gem_colors) or 'the observed surface details'}; exact identity may be unverified."
            ),
            brand_elements_en="Legacy analysis recorded no authoritative visible brand element.",
            integrity_constraints_en=legacy.generation.integrity_constraints_en,
            forbidden_changes_en=legacy.generation.forbidden_additions_en,
        ),
        strategy_id="jewelry-five-panel-v2",
        marketing_copy=MarketingCopy(sections=copies),
        source=SourceMetadata(
            image_file=legacy.source.image_file,
            image_sha256=legacy.source.image_sha256,
            vision_model=legacy.source.qwen_model,
            analyzed_at=legacy.source.analyzed_at,
        ),
    )


def load_product_spec(path: Path) -> ProductSpec:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    if data.get("schema_version") == "2.0":
        return ProductSpec.model_validate(data)
    return convert_legacy_spec(LegacyProductSpec.model_validate(data))
