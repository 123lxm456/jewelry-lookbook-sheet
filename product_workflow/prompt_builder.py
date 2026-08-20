from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from string import Template
from typing import Any

from .models import ProductSpec


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = ROOT / "prompts" / "base" / "product-panel.txt"


FALLBACK_SHOT_MATRIX = (
    {
        "camera_azimuth": "front_three_quarter", "camera_elevation": "eye_level", "shot_distance": "medium_tight",
        "composition": "right_weighted", "product_position": "lower_right", "scene_type": "interactive_use",
    },
    {
        "camera_azimuth": "strict_side", "camera_elevation": "product_level", "shot_distance": "medium",
        "composition": "centered", "product_position": "upper_center", "scene_type": "seamless_studio",
    },
    {
        "camera_azimuth": "front", "camera_elevation": "high_angle", "shot_distance": "medium_wide",
        "composition": "left_weighted", "product_position": "off_center_left", "scene_type": "tabletop_still_life",
    },
    {
        "camera_azimuth": "rear_three_quarter", "camera_elevation": "overhead", "shot_distance": "wide",
        "composition": "foreground_depth", "product_position": "lower_left", "scene_type": "lifestyle_environment",
    },
    {
        "camera_azimuth": "back", "camera_elevation": "low_angle", "shot_distance": "environmental_wide",
        "composition": "diagonal", "product_position": "center", "scene_type": "campaign_set",
    },
)


PANEL_SHOT_CONTRACTS: dict[str, dict[str, str]] = {
    "worn": {"camera_azimuth":"front_three_quarter","camera_elevation":"eye_level","shot_distance":"medium_tight","composition":"right_weighted","product_position":"lower_right","scene_type":"interactive_use"},
    "on_foot": {"camera_azimuth":"strict_side","camera_elevation":"product_level","shot_distance":"medium_tight","composition":"right_weighted","product_position":"lower_right","scene_type":"interactive_use"},
    "carried": {"camera_azimuth":"front_three_quarter","camera_elevation":"eye_level","shot_distance":"medium_tight","composition":"right_weighted","product_position":"lower_right","scene_type":"interactive_use"},
    "rolling": {"camera_azimuth":"strict_side","camera_elevation":"product_level","shot_distance":"medium_wide","composition":"right_weighted","product_position":"lower_right","scene_type":"interactive_use"},
    "detail": {"camera_azimuth":"front","camera_elevation":"high_angle","shot_distance":"medium_wide","composition":"left_weighted","product_position":"off_center_left","scene_type":"tabletop_still_life"},
    "still_life": {"camera_azimuth":"front_three_quarter","camera_elevation":"high_angle","shot_distance":"medium","composition":"diagonal","product_position":"lower_left","scene_type":"tabletop_still_life"},
    "gift": {"camera_azimuth":"front","camera_elevation":"high_angle","shot_distance":"wide","composition":"foreground_depth","product_position":"lower_right","scene_type":"campaign_set"},
    "mirror": {"camera_azimuth":"strict_side","camera_elevation":"eye_level","shot_distance":"environmental_wide","composition":"diagonal","product_position":"center","scene_type":"lifestyle_environment"},
}


FULL_PRODUCT_DISTANCE_MAP = {
    # Persisted plans used one value from each legacy distance. Map that
    # permutation one-to-one so diversity remains machine-checkable without
    # allowing a panel to crop the product.
    "extreme_closeup": "medium_tight",
    "closeup": "medium",
    "medium": "medium_wide",
    "medium_wide": "wide",
    "wide": "environmental_wide",
    "medium_tight": "medium_tight",
    "environmental_wide": "environmental_wide",
}
FULL_PRODUCT_DISTANCES = ("medium_tight", "medium", "medium_wide", "wide", "environmental_wide")


def _normalize_full_product_distances(concepts: list[dict[str, str]]) -> list[dict[str, str]]:
    if not any(concept.get("shot_distance") in {"extreme_closeup", "closeup"} for concept in concepts):
        return concepts
    normalized: list[dict[str, str]] = []
    used: set[str] = set()
    for concept in concepts:
        preferred = FULL_PRODUCT_DISTANCE_MAP.get(concept.get("shot_distance", ""), "medium_wide")
        distance = preferred if preferred not in used else next(
            candidate for candidate in FULL_PRODUCT_DISTANCES if candidate not in used
        )
        used.add(distance)
        normalized.append({**concept, "shot_distance": distance})
    return normalized


def _display_concepts(spec: ProductSpec, strategy: dict[str, Any]) -> list[dict[str, str]]:
    if spec.dynamic_display_plan is not None:
        concepts = [concept.model_dump() for concept in spec.dynamic_display_plan.concepts]
        return [
            {**concept, **PANEL_SHOT_CONTRACTS.get(panel["id"], {}), "supported_interaction_en": "; ".join(spec.usage.interaction_methods) or "non-contact display only"}
            for panel, concept in zip(strategy["panels"], concepts)
        ]
    methods = spec.usage.recommended_display_methods
    interactions = spec.usage.interaction_methods
    preferred_shot = {
        "worn": 0, "on_foot": 0, "carried": 0, "rolling": 0, "usage": 0, "toy_identity": 0,
        "profile": 1, "structure": 1, "open_structure": 1, "toy_structure": 1,
        "detail": 2, "hardware": 2,
        "still_life": 1, "gift": 3, "lifestyle": 3, "travel_scene": 3, "context": 3,
        "toy_interaction": 3,
        "hero": 4, "mirror": 4,
    }
    available_shots = set(range(5))
    concepts: list[dict[str, str]] = []
    for index, panel in enumerate(strategy["panels"]):
        preferred = preferred_shot.get(panel["id"], index)
        shot_index = preferred if preferred in available_shots else min(available_shots)
        available_shots.remove(shot_index)
        shot = {**FALLBACK_SHOT_MATRIX[shot_index], **PANEL_SHOT_CONTRACTS.get(panel["id"], {})}
        method = methods[index] if index < len(methods) else panel["label"]
        concepts.append({
            **shot,
            "display_method_en": f"Adapt {method} to the exact observed product and the '{panel['label']}' purpose.",
            "scene_design_en": (
                f"Design a product-specific {shot['scene_type']} scene from the observed usage: {spec.usage.usage_scene}."
            ),
            "photography_style_en": f"Distinct commercial treatment for {panel['label']}",
            "feature_focus_en": spec.design.visual_selling_points[index % len(spec.design.visual_selling_points)],
            "supported_interaction_en": "; ".join(interactions) if interactions else "non-contact display only",
        })
    return concepts


def joined(values: list[str]) -> str:
    return "; ".join(values) if values else "none observed"


CONTEXT_MARKERS = (
    "非主体", "背景", "旁边", "周边", "道具", "比例参照", "尺寸参照", "仅作参照",
    "not part of the product", "not the primary", "background", "nearby", "scale reference", "prop",
)
VISIBILITY_MARKERS = (
    "延伸出画面", "超出画面", "未完全展示", "未展示", "被遮挡", "不可见", "仅露出", "局部可见",
    "extends out of frame", "cropped", "not fully shown", "not visible", "occluded", "partially visible",
)


def _has_marker(text: str, markers: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in markers)


def reference_prompt_evidence(spec: ProductSpec) -> dict[str, str]:
    """Use explicit v2 evidence, with a semantic migration for legacy jobs."""
    observation = spec.reference_observation
    details = list(spec.details.important_details)
    excluded = list(observation.excluded_context_elements)
    limitations = list(observation.visibility_limitations)
    structure_sentences = [part.strip() for part in re.split(r"(?<=[。.!?；;])", spec.physical.structure) if part.strip()]

    if not excluded:
        contextual = [text for text in details + structure_sentences if _has_marker(text, CONTEXT_MARKERS)]
        excluded.extend(contextual)
    if not limitations:
        evidence_pool = details + spec.details.construction_details + spec.details.functional_details + [
            spec.physical.structure, spec.physical.shape_and_proportion, spec.physical.size_cues,
        ]
        limitations.extend(text for text in evidence_pool if _has_marker(text, VISIBILITY_MARKERS))

    product_details = [text for text in details if text not in excluded]
    product_structure = " ".join(text for text in structure_sentences if text not in excluded).strip()
    if not product_structure:
        product_structure = spec.physical.shape
    topology = observation.topology_signature_en
    if topology == "Use the source pixels as the product authority.":
        topology = " ".join(filter(None, (
            spec.prompt_facts.subject_description_en,
            product_structure,
            joined(spec.physical.components),
            joined(spec.integrity.must_preserve),
        )))
    return {
        "structure": product_structure,
        "details": joined(product_details),
        "excluded": joined(list(dict.fromkeys(excluded))),
        "limitations": joined(list(dict.fromkeys(limitations))),
        "topology": topology,
    }


def build_display_plan(spec: ProductSpec, strategy: dict[str, Any]) -> dict[str, Any]:
    panels = []
    concepts = _normalize_full_product_distances(_display_concepts(spec, strategy))
    panel_labels = [str(panel["label"]) for panel in strategy["panels"]]
    identity_options = (
        "an adult East Asian model with an oval face, straight dark shoulder-length hair, and understated makeup",
        "an adult East Asian model with a softly angular face, dark hair in a low clean bun, and understated makeup",
        "an adult East Asian model with a round-oval face, short straight dark hair, and understated makeup",
        "an adult East Asian model with a long oval face, dark collarbone-length hair, and understated makeup",
    )
    identity_index = int(spec.source.image_sha256[:8], 16) % len(identity_options)
    model_identity = identity_options[identity_index]
    default_signatures = (
        "WORN ACTION — eye-level or body-location-level camera, oblique 25–40 degree view, asymmetric live gesture, with all observed product structure comfortably inside frame; never a tabletop arrangement.",
        "TRUE PROFILE — strict side-on or rear-three-quarter camera at product height, compressed clean studio framing, observed silhouette intact; never the oblique hero angle used elsewhere.",
        "IDENTITY DETAIL STUDY — elevated oblique camera and detail-led lighting, with every observed edge visible with breathing room; never use a macro crop or invent hidden structure.",
        "ENVIRONMENTAL STORY — visibly high-angle or wide side-biased camera with foreground/background depth and a diagonal movement path, while all observed product structure stays unobstructed.",
        "CAMPAIGN HERO — low or straight-on camera, observed product silhouette intact, sculptural negative space and bold graphic balance; use an arrangement and camera elevation unlike panels 1–4.",
    )
    signature_by_panel_id = {
        "worn": default_signatures[0], "on_foot": default_signatures[0], "carried": default_signatures[0],
        "rolling": default_signatures[0], "usage": default_signatures[0],
        "profile": default_signatures[1], "structure": default_signatures[1],
        "detail": default_signatures[2], "hardware": default_signatures[2],
        "still_life": "AMBER EDITORIAL STILL LIFE — high three-quarter camera, diagonal product path across a warm tactile surface, book-and-glass depth, no model; never a pale catalog surface or gift box.",
        "gift": "OPEN-BOX GIFT TABLEAU — high three-quarter camera, complete product in one open unbranded box with ivory textile depth and ceremonial negative space; never reuse panel 2's bare-surface arrangement or panel 3's amber tabletop.",
        "open_structure": "OPEN CONSTRUCTION — elevated front-oblique camera clearly looking into the opened product, layered interior depth, no worn or rolling action.",
        "lifestyle": default_signatures[3], "travel_scene": default_signatures[3], "context": default_signatures[3],
        "mirror": "GEOMETRY-LOCKED MIRROR PORTRAIT — side-profile model in the right source zone and an empty equal left zone for deterministic reflection composition; never generate a reflection or second person in this stage.",
        "hero": default_signatures[4],
    }
    assigned_signatures = []
    has_visibility_limits = reference_prompt_evidence(spec)["limitations"] != "none observed"
    for index, (configured, concept) in enumerate(zip(strategy["panels"], concepts), start=1):
        panel = dict(configured)
        panel["number"] = index
        panel["style_reference_en"] = panel.get(
            "style_reference_en",
            "Use this category's product-specific strategy; do not apply the jewelry lookbook template.",
        )
        visual_signature = signature_by_panel_id.get(panel["id"], default_signatures[index - 1])
        assigned_signatures.append(visual_signature)
        other_labels = "、".join(label for label in panel_labels if label != panel["label"])
        shot_contract = (
            f"camera azimuth={concept['camera_azimuth']}; camera elevation={concept['camera_elevation']}; "
            f"shot distance={concept['shot_distance']}; composition={concept['composition']}; "
            f"product position={concept['product_position']}; scene type={concept['scene_type']}"
        )
        panel["shot_spec"] = concept
        extent_directive = (
            "Keep every observed edge and identity-defining component inside the frame, but keep source-hidden or source-cropped structure outside the chosen view. "
            if has_visibility_limits else
            "Keep every outer edge and identity-defining component inside the frame with visible breathing room. "
        )
        panel["diversity_directive_en"] = (
            f"This is panel {index} of 5, the uniquely identifiable '{panel['label']}' panel. "
            f"Assigned visual signature: {visual_signature} Structured shot contract: {shot_contract}. "
            "The structured shot contract is mandatory and overrides generic scene or composition defaults if they conflict. "
            f"Every assigned distance is an identity-preserving shot: {extent_directive}"
            "Detail emphasis must come from angle and light, never from additional cropping. "
            "If an optional style image is supplied, use it only as broad quality/layout inspiration, never to replace the assigned camera contract. "
            f"The other panels are {other_labels}; this result must remain instantly distinguishable from all of them "
            "even after removing typography and backgrounds. Camera elevation, azimuth, distance, lens feel, product "
            "arrangement, framing, and human action are all uniqueness dimensions, not optional suggestions. Do not fall "
            "back to a generic paired front three-quarter catalog pose."
        )
        panel["model_identity_en"] = (
            f"Whenever a person appears, use the same recurring identity across this five-image task: {model_identity}. "
            "Keep facial structure, apparent age, hair, and skin tone consistent; clothing may vary only in plain neutral styling."
        )
        panel["dynamic_display_guidance_en"] = (
            f"Product-specific display method: {concept['display_method_en']} "
            f"Scene design: {concept['scene_design_en']} Photography style: {concept['photography_style_en']}. "
            f"Feature focus: {concept['feature_focus_en']}. Observed plausible usage: {spec.usage.usage_scene}. "
            f"Observed interaction evidence: {joined(spec.usage.interaction_methods)}. "
            "Do not perform an interaction or expose a function not supported by those observed facts."
        )
        panels.append(panel)
    return {
        "version": 2,
        "strategy_id": strategy["id"],
        "category_group": spec.identity.category_group,
        "subcategory": spec.identity.subcategory,
        "panels": panels,
        "model_identity_en": model_identity,
        "shot_matrix": assigned_signatures,
        "structured_shot_matrix": [panel["shot_spec"] for panel in panels],
    }


def render_panel_prompt(spec: ProductSpec, panel: dict[str, Any], template_path: Path = DEFAULT_TEMPLATE) -> tuple[str, str]:
    source = template_path.read_text(encoding="utf-8")
    brand_rule = (
        "Preserve every visible brand mark, monogram, emblem, label, and its exact placement, scale, color, and orientation."
        if spec.branding.visible
        else "Do not invent a brand, logo, monogram, label, or readable product text."
    )
    evidence = reference_prompt_evidence(spec)
    has_visibility_limits = evidence["limitations"] != "none observed"
    if has_visibility_limits:
        quantity_visibility = (
            f"Show exactly {spec.identity.item_count} primary item(s), matching the reference quantity. Preserve every "
            "visible product relationship and identity-defining part, while keeping source-hidden or source-cropped "
            "structure outside the chosen view; do not add, complete, omit, or duplicate an item."
        )
    else:
        quantity_visibility = (
            f"Show exactly {spec.identity.item_count} visible complete item(s), matching the reference quantity. "
            "Every item must fit fully inside the image with all outer edges and identity-defining parts visible and "
            "unobstructed; do not crop, add, omit, or duplicate an item."
        )
    values = {
        "category": spec.identity.category_group,
        "subcategory": spec.identity.subcategory,
        "product_name": spec.identity.product_name,
        "item_count": str(spec.identity.item_count),
        "quantity_visibility_en": quantity_visibility,
        "subject_description_en": spec.prompt_facts.subject_description_en,
        "material_appearance_en": spec.prompt_facts.material_appearance_en,
        "structure": evidence["structure"],
        "components": joined(spec.physical.components),
        "brand_elements_en": spec.prompt_facts.brand_elements_en.rstrip(".。"),
        "brand_rule_en": brand_rule,
        "important_details_en": evidence["details"],
        "topology_signature_en": evidence["topology"],
        "excluded_context_en": evidence["excluded"],
        "visibility_limitations_en": evidence["limitations"],
        "framing_avoid_en": (
            "inventing or completing source-hidden/source-cropped structure; additionally cropping, hiding, or obstructing any observed product feature"
            if has_visibility_limits else
            "any close-up, macro crop, partial-product composition, clipped outer edge, hidden core structure, or product extending beyond the frame"
        ),
        "style_reference_policy_en": (
            "Jewelry-only Image1 policy: use the panel-specific visual grammar below only for scene, composition, camera "
            "angle, model pose, display method, light direction, and mood. Never copy, infer, or recreate any jewelry, "
            "pearl, stone, chain, charm, metal color, text, UI, logo, or accessory from Image1; all product content must "
            "come from the uploaded product authority image."
            if spec.identity.category_group == "jewelry"
            else "No jewelry lookbook template applies to this category; follow this product category's own shot contract."
        ),
        "panel_style_reference_en": panel["style_reference_en"],
        "integrity_constraints_en": joined(spec.prompt_facts.integrity_constraints_en),
        "forbidden_changes_en": joined(spec.prompt_facts.forbidden_changes_en),
        "scene_prompt_en": panel["scene_prompt_en"],
        "backdrop_en": panel["backdrop_en"],
        "composition_en": panel["composition_en"],
        "lighting_en": panel["lighting_en"],
        "diversity_directive_en": panel["diversity_directive_en"],
        "dynamic_display_guidance_en": panel["dynamic_display_guidance_en"],
        "model_identity_en": panel["model_identity_en"],
    }
    return Template(source).substitute(values).rstrip() + "\n", hashlib.sha256(source.encode()).hexdigest()


def canonical_hash(data: Any) -> str:
    encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
