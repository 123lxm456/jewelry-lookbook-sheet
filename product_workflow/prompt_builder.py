from __future__ import annotations

import hashlib
import json
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
        return [concept.model_dump() for concept in spec.dynamic_display_plan.concepts]
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
        shot = FALLBACK_SHOT_MATRIX[shot_index]
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
        "WORN ACTION — eye-level or body-location-level camera, oblique 25–40 degree view, asymmetric live gesture, with the complete product comfortably inside frame; never a tabletop arrangement.",
        "TRUE PROFILE — strict side-on or rear-three-quarter camera at product height, compressed clean studio framing, complete silhouette; never the oblique hero angle used elsewhere.",
        "FULL-PRODUCT DETAIL STUDY — elevated oblique camera and detail-led lighting, but the entire product and every outer edge remain visible with breathing room; never use a macro crop.",
        "ENVIRONMENTAL STORY — visibly high-angle or wide side-biased camera with foreground/background depth and a diagonal movement path, while the complete product stays unobstructed.",
        "CAMPAIGN HERO — low or straight-on camera, complete product silhouette, sculptural negative space and bold graphic balance; use an arrangement and camera elevation unlike panels 1–4.",
    )
    signature_by_panel_id = {
        "worn": default_signatures[0], "on_foot": default_signatures[0], "carried": default_signatures[0],
        "rolling": default_signatures[0], "usage": default_signatures[0],
        "profile": default_signatures[1], "structure": default_signatures[1],
        "detail": default_signatures[2], "hardware": default_signatures[2],
        "still_life": "ELEVATED STILL LIFE — 30–45 degree overhead camera, diagonal product-led arrangement, tactile foreground surface, no model; never a clean side-profile catalog shot.",
        "gift": "TOP-DOWN GIFT TABLEAU — near-overhead camera, complete product on an appropriate unbranded support with asymmetric fabric/ribbon flow; never reuse the still-life camera height or arrangement.",
        "open_structure": "OPEN CONSTRUCTION — elevated front-oblique camera clearly looking into the opened product, layered interior depth, no worn or rolling action.",
        "lifestyle": default_signatures[3], "travel_scene": default_signatures[3], "context": default_signatures[3],
        "mirror": "MIRROR PORTRAIT — side-biased eye-level camera showing model and one coherent reflection, distinct profile/head direction and hand action; never a tabletop or centered hero arrangement.",
        "hero": default_signatures[4],
    }
    assigned_signatures = []
    for index, (configured, concept) in enumerate(zip(strategy["panels"], concepts), start=1):
        panel = dict(configured)
        panel["number"] = index
        visual_signature = signature_by_panel_id.get(panel["id"], default_signatures[index - 1])
        assigned_signatures.append(visual_signature)
        other_labels = "、".join(label for label in panel_labels if label != panel["label"])
        shot_contract = (
            f"camera azimuth={concept['camera_azimuth']}; camera elevation={concept['camera_elevation']}; "
            f"shot distance={concept['shot_distance']}; composition={concept['composition']}; "
            f"product position={concept['product_position']}; scene type={concept['scene_type']}"
        )
        panel["shot_spec"] = concept
        panel["diversity_directive_en"] = (
            f"This is panel {index} of 5, the uniquely identifiable '{panel['label']}' panel. "
            f"Assigned visual signature: {visual_signature} Structured shot contract: {shot_contract}. "
            "The structured shot contract is mandatory and overrides generic scene or composition defaults if they conflict. "
            "Every assigned distance means a FULL-PRODUCT shot: keep every outer edge and identity-defining component inside "
            "the frame with visible breathing room. Detail emphasis must come from angle and light, never from cropping. "
            "Use Image 3 only as broad quality/layout inspiration, never to replace the assigned camera contract. "
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
    quantity_visibility = (
        f"Show exactly {spec.identity.item_count} visible complete item(s), matching the reference quantity. "
        "Every item must fit fully inside the image with all outer edges, extremities, handles, straps, wheels, brims, "
        "soles, or other identity-defining parts visible and unobstructed; do not crop, add, omit, or duplicate an item."
    )
    values = {
        "category": spec.identity.category_group,
        "subcategory": spec.identity.subcategory,
        "product_name": spec.identity.product_name,
        "item_count": str(spec.identity.item_count),
        "quantity_visibility_en": quantity_visibility,
        "subject_description_en": spec.prompt_facts.subject_description_en,
        "material_appearance_en": spec.prompt_facts.material_appearance_en,
        "structure": spec.physical.structure,
        "components": joined(spec.physical.components),
        "brand_elements_en": spec.prompt_facts.brand_elements_en.rstrip(".。"),
        "brand_rule_en": brand_rule,
        "important_details_en": joined(spec.details.important_details),
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
