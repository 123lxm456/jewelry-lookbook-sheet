from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)


class StoneCount(StrictModel):
    mode: Literal["exact", "range", "approximate", "unknown"]
    value: int | None = Field(default=None, ge=0)
    minimum: int | None = Field(default=None, ge=0)
    maximum: int | None = Field(default=None, ge=0)
    description: str = Field(min_length=1, max_length=80)

    @model_validator(mode="before")
    @classmethod
    def normalize_numbers_from_description(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        numbers = [int(value) for value in re.findall(r"\d+", str(normalized.get("description", "")))]
        if normalized.get("mode") == "exact" and normalized.get("value") is None and numbers:
            normalized["value"] = numbers[0]
        if normalized.get("mode") == "range" and len(numbers) >= 2:
            normalized.setdefault("minimum", numbers[0])
            normalized.setdefault("maximum", numbers[1])
        return normalized

    @model_validator(mode="after")
    def validate_count_shape(self) -> "StoneCount":
        if self.mode == "exact" and self.value is None:
            raise ValueError("exact stone counts require value")
        if self.mode == "range":
            if self.minimum is None or self.maximum is None:
                raise ValueError("range stone counts require minimum and maximum")
            if self.minimum > self.maximum:
                raise ValueError("stone count minimum cannot exceed maximum")
        return self


class StoneSpec(StrictModel):
    visible_type: str = Field(
        min_length=1,
        max_length=60,
        description="Observed or likely stone type; use unknown when the image is insufficient.",
    )
    colors: list[str] = Field(min_length=1, max_length=8)
    count: StoneCount
    shapes_or_cuts: list[str] = Field(default_factory=list, max_length=10)
    settings: list[str] = Field(default_factory=list, max_length=10)
    confidence: float = Field(ge=0, le=1)
    evidence: str = Field(min_length=1, max_length=180)


class IdentitySpec(StrictModel):
    category: str = Field(min_length=1, max_length=40)
    product_name: str = Field(min_length=2, max_length=40)
    item_count: int = Field(ge=1, le=1000)
    wearing_location: str = Field(min_length=1, max_length=80)
    category_confidence: float = Field(ge=0, le=1)


class AppearanceSpec(StrictModel):
    main_stone: StoneSpec
    accent_stones: list[StoneSpec] = Field(default_factory=list, max_length=6)
    gem_colors: list[str] = Field(default_factory=list, max_length=10)
    setting_methods: list[str] = Field(default_factory=list, max_length=10)
    metal_color: str = Field(min_length=1, max_length=60)
    likely_metal: str = Field(min_length=1, max_length=60)
    metal_confidence: float = Field(ge=0, le=1)


class DesignSpec(StrictModel):
    style_keywords: list[str] = Field(min_length=1, max_length=8)
    motifs: list[str] = Field(default_factory=list, max_length=8)
    structure: str = Field(min_length=1, max_length=300)
    shape_and_proportion: str = Field(min_length=1, max_length=240)
    visual_selling_points: list[str] = Field(min_length=1, max_length=8)


class GenerationSpec(StrictModel):
    subject_description_en: str = Field(min_length=20, max_length=900)
    wearing_instruction_en: str = Field(min_length=10, max_length=500)
    macro_focus_en: str = Field(min_length=10, max_length=500)
    gift_presentation_en: str = Field(min_length=10, max_length=500)
    integrity_constraints_en: list[str] = Field(min_length=2, max_length=16)
    forbidden_additions_en: list[str] = Field(min_length=2, max_length=16)


class CopySection(StrictModel):
    eyebrow: str = Field(min_length=2, max_length=32)
    title: str = Field(min_length=2, max_length=16)
    body: str = Field(min_length=2, max_length=49)

    @field_validator("eyebrow")
    @classmethod
    def eyebrow_must_be_single_line(cls, value: str) -> str:
        if "\n" in value:
            raise ValueError("eyebrow must be a single line")
        if not re.fullmatch(r"[A-Z0-9 &'\-]+", value):
            raise ValueError("eyebrow must contain short uppercase English text")
        return value

    @field_validator("title")
    @classmethod
    def title_must_be_single_line(cls, value: str) -> str:
        if "\n" in value:
            raise ValueError("title must be a single line")
        if not re.search(r"[\u4e00-\u9fff]", value):
            raise ValueError("title must contain Simplified Chinese copy")
        return value

    @field_validator("body")
    @classmethod
    def body_must_fit_layout(cls, value: str) -> str:
        lines = value.splitlines()
        if len(lines) != 2:
            raise ValueError("body must contain exactly two lines")
        if any(not line or len(line) > 24 for line in lines):
            raise ValueError("each body line must contain 1 to 24 characters")
        if any(not re.search(r"[\u4e00-\u9fff]", line) for line in lines):
            raise ValueError("each body line must contain Simplified Chinese copy")
        return value


class MarketingCopy(StrictModel):
    sections: list[CopySection] = Field(min_length=5, max_length=5)


class ProductAnalysis(StrictModel):
    identity: IdentitySpec
    appearance: AppearanceSpec
    design: DesignSpec
    generation: GenerationSpec
    marketing_copy: MarketingCopy = Field(alias="copy")

    @model_validator(mode="after")
    def prevent_unverified_material_claims(self) -> "ProductAnalysis":
        marketing_text = " ".join(
            [
                self.identity.product_name,
                *(value for section in self.marketing_copy.sections for value in (section.title, section.body)),
            ]
        )
        if self.appearance.main_stone.confidence < 0.95 and re.search(
            r"钻石|真钻|天然钻|diamond", marketing_text, re.I
        ):
            raise ValueError("low-confidence stone identification cannot be marketed as diamond")
        if self.appearance.metal_confidence < 0.95 and re.search(
            r"铂金|白金|黄金|足金|纯银|925|18K|14K|platinum|sterling",
            marketing_text,
            re.I,
        ):
            raise ValueError("low-confidence metal identification cannot use a verified material claim")
        return self


class SourceMetadata(StrictModel):
    image_file: str = Field(min_length=1)
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    qwen_model: str = Field(min_length=1)
    analyzed_at: datetime


class ProductSpec(ProductAnalysis):
    schema_version: Literal["1.0"] = "1.0"
    source: SourceMetadata
