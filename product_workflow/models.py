from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)


class IdentitySpec(StrictModel):
    category_group: str = Field(min_length=2, max_length=40)
    subcategory: str = Field(min_length=1, max_length=60)
    product_name: str = Field(min_length=2, max_length=60)
    item_count: int = Field(ge=1, le=1000)
    category_confidence: float = Field(ge=0, le=1)
    support_status: Literal["supported", "unsupported"] = "supported"
    rejection_reason: str | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def unsupported_requires_reason(self) -> "IdentitySpec":
        if self.support_status == "unsupported" and not self.rejection_reason:
            raise ValueError("unsupported products require rejection_reason")
        return self


class PhysicalSpec(StrictModel):
    materials: list[str] = Field(default_factory=list, max_length=12)
    colors: list[str] = Field(min_length=1, max_length=12)
    shape: str = Field(min_length=1, max_length=160)
    structure: str = Field(min_length=1, max_length=500)
    components: list[str] = Field(default_factory=list, max_length=20)
    surface_texture: list[str] = Field(default_factory=list, max_length=12)
    shape_and_proportion: str = Field(min_length=1, max_length=320)
    size_cues: str = Field(min_length=1, max_length=240)


class BrandingSpec(StrictModel):
    visible: bool
    elements: list[str] = Field(default_factory=list, max_length=10)
    placement: str = Field(default="未观察到", max_length=200)
    confidence: float = Field(ge=0, le=1)


class DetailSpec(StrictModel):
    important_details: list[str] = Field(min_length=1, max_length=20)
    construction_details: list[str] = Field(default_factory=list, max_length=20)
    functional_details: list[str] = Field(default_factory=list, max_length=20)
    observed_text: list[str] = Field(default_factory=list, max_length=10)


class UsageSpec(StrictModel):
    usage_scene: str = Field(min_length=1, max_length=240)
    interaction_methods: list[str] = Field(default_factory=list, max_length=10)
    recommended_display_methods: list[str] = Field(default_factory=list, max_length=10)


class DisplayConcept(StrictModel):
    """One model-planned shot with machine-checkable visual differences."""

    display_method_en: str = Field(min_length=8, max_length=320)
    camera_azimuth: Literal[
        "front", "front_three_quarter", "strict_side", "rear_three_quarter", "back"
    ]
    camera_elevation: Literal["eye_level", "product_level", "high_angle", "overhead", "low_angle"]
    # Legacy close-up values remain readable for persisted v2 analyses. New
    # analyses use the five full-product-safe distances below; prompt building
    # normalizes either vocabulary before image generation.
    shot_distance: Literal[
        "medium_tight", "medium", "medium_wide", "wide", "environmental_wide",
        "extreme_closeup", "closeup",
    ]
    composition: Literal["centered", "left_weighted", "right_weighted", "diagonal", "foreground_depth"]
    product_position: Literal["center", "upper_center", "lower_left", "lower_right", "off_center_left"]
    scene_type: Literal[
        "seamless_studio", "tabletop_still_life", "lifestyle_environment", "interactive_use", "campaign_set"
    ]
    scene_design_en: str = Field(min_length=8, max_length=320)
    photography_style_en: str = Field(min_length=4, max_length=200)
    feature_focus_en: str = Field(min_length=4, max_length=240)


class DynamicDisplayPlan(StrictModel):
    concepts: list[DisplayConcept] = Field(min_length=5, max_length=5)

    @model_validator(mode="after")
    def concepts_must_be_visually_distinct(self) -> "DynamicDisplayPlan":
        dimensions = (
            "camera_azimuth", "camera_elevation", "shot_distance", "composition",
            "product_position", "scene_type",
        )
        for field_name in dimensions:
            values = [getattr(concept, field_name) for concept in self.concepts]
            if len(set(values)) != len(values):
                raise ValueError(f"all five display concepts require distinct {field_name} values")
        return self


class DesignSpec(StrictModel):
    style_keywords: list[str] = Field(min_length=1, max_length=10)
    visual_selling_points: list[str] = Field(min_length=1, max_length=10)


class IntegritySpec(StrictModel):
    must_preserve: list[str] = Field(min_length=2, max_length=20)
    must_not_invent: list[str] = Field(min_length=2, max_length=20)
    uncertain_attributes: list[str] = Field(default_factory=list, max_length=12)


class PromptFacts(StrictModel):
    subject_description_en: str = Field(min_length=20, max_length=1200)
    material_appearance_en: str = Field(min_length=10, max_length=600)
    brand_elements_en: str = Field(min_length=2, max_length=500)
    integrity_constraints_en: list[str] = Field(min_length=2, max_length=20)
    forbidden_changes_en: list[str] = Field(min_length=2, max_length=20)


class CopySection(StrictModel):
    panel_id: str = Field(min_length=2, max_length=40)
    eyebrow: str = Field(min_length=2, max_length=32)
    title: str = Field(min_length=2, max_length=16)
    body: str = Field(min_length=2, max_length=49)

    @field_validator("eyebrow")
    @classmethod
    def eyebrow_must_be_single_line(cls, value: str) -> str:
        if "\n" in value or not re.fullmatch(r"[A-Z0-9 &'\-]+", value):
            raise ValueError("eyebrow must contain short uppercase English text")
        return value

    @field_validator("title")
    @classmethod
    def title_must_be_single_line_chinese(cls, value: str) -> str:
        if "\n" in value or not re.search(r"[\u4e00-\u9fff]", value):
            raise ValueError("title must be one line of Simplified Chinese copy")
        return value

    @field_validator("body")
    @classmethod
    def body_must_fit_layout(cls, value: str) -> str:
        lines = value.splitlines()
        if len(lines) != 2 or any(not line or len(line) > 24 for line in lines):
            raise ValueError("body must contain exactly two lines of at most 24 characters")
        if any(not re.search(r"[\u4e00-\u9fff]", line) for line in lines):
            raise ValueError("each body line must contain Simplified Chinese copy")
        return value


class MarketingCopy(StrictModel):
    sections: list[CopySection] = Field(min_length=5, max_length=5)


class ProductAnalysis(StrictModel):
    identity: IdentitySpec
    physical: PhysicalSpec
    branding: BrandingSpec
    details: DetailSpec
    usage: UsageSpec
    dynamic_display_plan: DynamicDisplayPlan | None = None
    design: DesignSpec
    integrity: IntegritySpec
    prompt_facts: PromptFacts


class SourceMetadata(StrictModel):
    image_file: str = Field(min_length=1)
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    vision_model: str = Field(min_length=1)
    analyzed_at: datetime


class ProductSpec(ProductAnalysis):
    schema_version: Literal["2.0"] = "2.0"
    strategy_id: str = Field(min_length=2, max_length=80)
    marketing_copy: MarketingCopy = Field(alias="copy")
    source: SourceMetadata
