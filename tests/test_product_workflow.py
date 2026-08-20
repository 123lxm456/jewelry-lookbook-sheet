from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
from PIL import Image, ImageDraw
from pydantic import ValidationError

from jewelry_workflow.product_spec import StoneCount
from jewelry_workflow.qwen_vision import (
    PRODUCT_AUDIT_PROMPT, SYSTEM_PROMPT, ProductGeometryAudit, analysis_image_data_urls,
    QwenVisionClient, apply_geometry_audit, extract_json, image_data_url, normalize_api_base_url,
    retryable_qwen_error,
    validate_product_analysis,
)
from product_workflow.compatibility import load_product_spec
from product_workflow.models import CopySection, ProductSpec
from product_workflow.prompt_builder import build_display_plan, render_panel_prompt
from product_workflow.registry import CategoryRegistry, StrategyRegistry, select_strategy
from scripts.assemble_long_image import build_risk_map, region_risk
from scripts.analyze_product import load_env
from scripts.assess_series_quality import (
    PanelResult, obvious_duplicate_pairs, panel_fails_gate, write_local_product_mask,
)
from scripts.compose_mirror_scene import compose
from scripts.prepare_product_reference import expanded_box
from scripts.prepare_style_reference import crop_bounds, panel_bounds


ROOT = Path(__file__).resolve().parents[1]
TEST_IMAGE = ROOT / "jewelry-test.jpg"


class ProductWorkflowTests(unittest.TestCase):
    def test_packaging_and_job_state_sync_use_current_generic_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for number in range(1, 6):
                Image.new("RGB", (64, 96), f"#{number}{number}{number}{number}{number}{number}").save(
                    output / f"panel-{number:02d}.png"
                )
            Image.new("RGB", (64, 192), "#cccccc").save(output / "product-long.png")
            spec = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
            spec["identity"]["product_name"] = "图示测试商品"
            (output / "product-spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            (output / "display-plan.json").write_text("{}", encoding="utf-8")
            (output / "job-state.json").write_text(
                json.dumps({"product_name": "旧名称", "steps": {}}, ensure_ascii=False), encoding="utf-8"
            )
            subprocess.run([sys.executable, str(ROOT / "scripts/package_product_images.py"), str(output)], check=True)
            subprocess.run([sys.executable, str(ROOT / "scripts/sync_job_state.py"), str(output)], check=True)
            with zipfile.ZipFile(output / "product-images.zip") as archive:
                self.assertEqual(len(archive.namelist()), 6)
                self.assertIsNone(archive.testzip())
            state = json.loads((output / "job-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["product_name"], "图示测试商品")
            self.assertEqual(state["status"], "completed")

    def test_product_audit_rules_are_geometry_first_and_not_sample_specific(self) -> None:
        audit_rules = (SYSTEM_PROMPT + PRODUCT_AUDIT_PROMPT).lower()
        self.assertIn("topology graph", audit_rules)
        self.assertIn("neutral geometric", audit_rules)
        self.assertIn("do not classify, name, identify", audit_rules)
        self.assertNotIn("semantic anchoring such as", audit_rules)

    def test_geometry_audit_replaces_semantic_identity_for_any_product(self) -> None:
        spec = load_product_spec(ROOT / "product.example.json")
        audit = ProductGeometryAudit(
            primary_product_bbox=(0.2, 0.1, 0.8, 0.9),
            outer_contour_en="One irregular closed outer boundary with two nested boundary levels.",
            component_inventory_en=["component A: one thin path", "component B: one compact nested region"],
            repeated_region_groups_en=["seven visible angular regions in one linear group"],
            negative_spaces_en=["six narrow gaps between adjacent angular regions"],
            attachment_graph_en=["component A contacts component B at two horizontally separated boundary points"],
            symmetry_en="Approximate vertical symmetry; no horizontal symmetry proven.",
            proportion_evidence_en=["component B spans about one quarter of the visible path width"],
            excluded_context_elements=["background surface"],
            visibility_limitations=["both path ends extend outside the source frame"],
            source_quality_limitations=["specular highlights obscure some internal boundaries"],
        )
        result = apply_geometry_audit(spec, audit)
        self.assertEqual(result.identity.product_name, "图示项链")
        self.assertIn("two horizontally separated", result.reference_observation.topology_signature_en)
        self.assertNotEqual(result.physical.shape, spec.physical.shape)

    def test_series_gate_does_not_regenerate_for_positive_or_minor_notes(self) -> None:
        result = PanelResult(
            panel_number=3,
            product_fidelity=0.90,
            composition_match=0.85,
            style_match=0.90,
            product_identity_exact=True,
            product_bbox=(0.2, 0.2, 0.8, 0.8),
            physically_valid=True,
            mirror_consistent=True,
            issues=["Lighting matches the assigned style.", "Silhouette is only slightly irregular."],
        )
        self.assertFalse(panel_fails_gate(result, 0.90, 0.75))
        result.product_fidelity = 0.89
        self.assertTrue(panel_fails_gate(result, 0.90, 0.75))
        result.product_fidelity = 0.95
        result.product_identity_exact = False
        self.assertTrue(panel_fails_gate(result, 0.90, 0.75))

    def test_local_duplicate_gate_flags_only_near_identical_panels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = [root / f"panel-{number}.png" for number in range(3)]
            Image.new("RGB", (80, 120), "white").save(paths[0])
            Image.new("RGB", (80, 120), "white").save(paths[1])
            Image.new("RGB", (80, 120), "black").save(paths[2])
            pairs = obvious_duplicate_pairs(paths)
        self.assertEqual([pair.panels for pair in pairs], [(1, 2)])

    def test_product_lock_mask_edits_only_an_expanded_product_region(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            image_path = Path(temporary) / "panel.png"
            mask_path = Path(temporary) / "mask.png"
            Image.new("RGB", (1000, 1500), "white").save(image_path)
            write_local_product_mask(image_path, (0.4, 0.4, 0.6, 0.6), mask_path)
            with Image.open(mask_path) as mask:
                self.assertEqual(mask.getpixel((500, 750))[3], 0)
                self.assertEqual(mask.getpixel((20, 20))[3], 255)

    def test_dynamic_plan_allows_one_shared_dimension_when_pairs_remain_distinct(self) -> None:
        data = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
        for key in ("schema_version", "strategy_id", "copy", "source"):
            data.pop(key, None)
        data["dynamic_display_plan"] = {"concepts": [
            {"display_method_en":"Complete product identity view","camera_azimuth":"front","camera_elevation":"eye_level","shot_distance":"medium","composition":"centered","product_position":"center","scene_type":"seamless_studio","scene_design_en":"Clean product-specific studio background","photography_style_en":"commercial product photo","feature_focus_en":"complete silhouette"},
            {"display_method_en":"Alternate construction view","camera_azimuth":"front","camera_elevation":"product_level","shot_distance":"medium_wide","composition":"left_weighted","product_position":"upper_center","scene_type":"tabletop_still_life","scene_design_en":"Sparse product-specific tabletop setting","photography_style_en":"construction study","feature_focus_en":"exact structure"},
            {"display_method_en":"Authentic surface detail study","camera_azimuth":"front_three_quarter","camera_elevation":"high_angle","shot_distance":"extreme_closeup","composition":"right_weighted","product_position":"off_center_left","scene_type":"campaign_set","scene_design_en":"Tonal set from observed product colors","photography_style_en":"material macro photo","feature_focus_en":"surface detail"},
            {"display_method_en":"Contextual product presentation","camera_azimuth":"rear_three_quarter","camera_elevation":"overhead","shot_distance":"wide","composition":"foreground_depth","product_position":"lower_right","scene_type":"lifestyle_environment","scene_design_en":"Sparse context derived from observed usage","photography_style_en":"environmental advertising","feature_focus_en":"scale and presence"},
            {"display_method_en":"Premium campaign presentation","camera_azimuth":"back","camera_elevation":"low_angle","shot_distance":"closeup","composition":"diagonal","product_position":"lower_left","scene_type":"interactive_use","scene_design_en":"Product-specific campaign arrangement","photography_style_en":"bold campaign photo","feature_focus_en":"original appearance"},
        ]}
        analysis = validate_product_analysis(json.dumps(data, ensure_ascii=False))
        self.assertIsNotNone(analysis.dynamic_display_plan)

    def test_invalid_optional_reference_evidence_does_not_discard_core_analysis(self) -> None:
        data = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
        for key in ("schema_version", "strategy_id", "copy", "source"):
            data.pop(key, None)
        data["reference_observation"] = {
            "primary_product_bbox": [0.5, 0.2, 0.5, 0.8],
            "primary_product_elements": [], "excluded_context_elements": [],
            "visibility_limitations": [], "topology_signature_en": "invalid zero-width box",
        }
        analysis = validate_product_analysis(json.dumps(data, ensure_ascii=False))
        self.assertEqual(analysis.reference_observation.primary_product_bbox, (0.0, 0.0, 1.0, 1.0))
        self.assertEqual(analysis.identity.product_name, data["identity"]["product_name"])

    def test_qwen_transport_errors_are_retryable(self) -> None:
        self.assertTrue(retryable_qwen_error(httpx.ConnectError("temporary connection failure")))
        self.assertFalse(retryable_qwen_error(ValueError("invalid schema")))

    def test_qwen_timeout_is_configurable_and_defaults_to_ninety_seconds(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            client = QwenVisionClient("key", "https://example.invalid/v1", model="test-model")
            self.assertEqual(client.client.timeout, 90.0)
        with patch.dict(os.environ, {"QWEN_TIMEOUT_SECONDS": "420"}, clear=True):
            client = QwenVisionClient("key", "https://example.invalid/v1", model="test-model")
            self.assertEqual(client.client.timeout, 420.0)

    def test_transient_geometry_audit_failure_preserves_valid_primary_analysis(self) -> None:
        spec = load_product_spec(ROOT / "product.example.json")
        primary = spec.model_dump(mode="json")
        for key in ("schema_version", "strategy_id", "marketing_copy", "copy", "source"):
            primary.pop(key, None)
        client = QwenVisionClient("key", "https://example.invalid/v1", model="test-model")
        client._completion = MagicMock(return_value=json.dumps(primary, ensure_ascii=False))
        client.audit_product = MagicMock(side_effect=httpx.ReadTimeout("slow audit"))
        with patch.dict(
            os.environ,
            {
                "QWEN_RESPONSE_FORMAT": "json_schema", "QWEN_GEOMETRY_AUDIT": "true",
                "QWEN_AUDIT_REQUIRED": "false",
            },
            clear=False,
        ):
            result = client.analyze(TEST_IMAGE)
        self.assertEqual(result.identity.subcategory, spec.identity.subcategory)
        client.audit_product.assert_called_once()

    def test_required_geometry_audit_still_propagates_transient_failure(self) -> None:
        spec = load_product_spec(ROOT / "product.example.json")
        primary = spec.model_dump(mode="json")
        for key in ("schema_version", "strategy_id", "marketing_copy", "copy", "source"):
            primary.pop(key, None)
        client = QwenVisionClient("key", "https://example.invalid/v1", model="test-model")
        client._completion = MagicMock(return_value=json.dumps(primary, ensure_ascii=False))
        client.audit_product = MagicMock(side_effect=httpx.ReadTimeout("slow audit"))
        with patch.dict(
            os.environ,
            {"QWEN_RESPONSE_FORMAT": "json_schema", "QWEN_AUDIT_REQUIRED": "true"},
            clear=False,
        ):
            with self.assertRaises(httpx.ReadTimeout):
                client.analyze(TEST_IMAGE)

    def test_env_file_replaces_empty_but_not_nonempty_process_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text("QWEN_MODEL=file-model\nQWEN_BASE_URL=https://file.example/v1\n", encoding="utf-8")
            with patch.dict(os.environ, {"QWEN_MODEL": "", "QWEN_BASE_URL": "https://process.example/v1"}, clear=False):
                load_env(env_file)
                self.assertEqual(os.environ["QWEN_MODEL"], "file-model")
                self.assertEqual(os.environ["QWEN_BASE_URL"], "https://process.example/v1")

    def test_example_spec_is_valid_and_has_five_sections(self) -> None:
        spec = load_product_spec(ROOT / "product.example.json")
        self.assertEqual(len(spec.marketing_copy.sections), 5)
        self.assertEqual(spec.schema_version, "2.0")
        self.assertEqual(spec.identity.category_group, "jewelry")

    def test_copy_rejects_non_chinese_title(self) -> None:
        with self.assertRaises(ValidationError):
            CopySection(panel_id="detail", eyebrow="DETAILS", title="English title", body="第一行中文\n第二行中文")

    def test_copy_timeout_fallback_produces_publishable_two_line_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            spec_path = root / "product-spec.json"
            page_path = root / "page.json"
            shutil.copy(ROOT / "product.example.json", spec_path)
            environment = os.environ.copy()
            environment.pop("QWEN_API_KEY", None)
            environment.pop("QWEN_BASE_URL", None)
            environment["QWEN_ANALYSIS_CACHE_DIR"] = str(root / "analysis-cache")
            subprocess.run([
                sys.executable, str(ROOT / "scripts/generate_marketing_copy.py"),
                str(spec_path), str(page_path), "--env-file", str(root / "missing.env"),
            ], cwd=ROOT, env=environment, check=True, capture_output=True, text=True)
            page = json.loads(page_path.read_text(encoding="utf-8"))
            self.assertEqual(len(page["sections"]), 5)
            self.assertTrue(all("商品视觉信息正在生成" not in section["body"] for section in page["sections"]))

    def test_extract_json_accepts_fenced_response(self) -> None:
        self.assertEqual(extract_json("```json\n{\"ok\": true}\n```"), {"ok": True})

    def test_all_supported_categories_have_five_panel_strategies(self) -> None:
        categories = CategoryRegistry()
        strategies = StrategyRegistry()
        supported = [item for item in categories.definitions if item.supported]
        self.assertEqual(
            {item.id for item in supported},
            {"jewelry", "bags", "luggage", "shoes", "hats", "toys", "other_non_apparel"},
        )
        base = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
        for category in supported:
            strategy = strategies.get(category.strategy_id)
            self.assertEqual(len(strategy["panels"]), 5)
            data = json.loads(json.dumps(base, ensure_ascii=False))
            data["identity"]["category_group"] = category.id
            data["identity"]["subcategory"] = category.subcategories[0]
            data["strategy_id"] = strategy["id"]
            for section, panel in zip(data["copy"]["sections"], strategy["panels"]):
                section["panel_id"] = panel["id"]
            spec = ProductSpec.model_validate(data)
            plan = build_display_plan(spec, strategy)
            for panel in plan["panels"]:
                prompt, _ = render_panel_prompt(spec, panel)
                self.assertNotIn("$", prompt)
                self.assertIn("Show all product structure that is actually evidenced", prompt)
                self.assertNotIn("may intentionally crop", prompt)
            matrix = plan["structured_shot_matrix"]
            dimensions = ("camera_azimuth", "camera_elevation", "shot_distance", "composition", "product_position", "scene_type")
            for left in range(5):
                for right in range(left + 1, 5):
                    self.assertGreaterEqual(sum(matrix[left][key] != matrix[right][key] for key in dimensions), 4)

    def test_toy_category_uses_dynamic_five_panel_plan_and_preserves_identity(self) -> None:
        data = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
        strategy = StrategyRegistry().get("toy-five-panel-v1")
        data["identity"].update({
            "category_group": "toys", "subcategory": "毛绒玩具", "product_name": "蓝色毛绒玩具",
        })
        data["physical"].update({
            "materials": ["短绒面料，具体纤维未知"], "colors": ["蓝色", "白色"],
            "structure": "圆润头身、两只短耳和四肢缝合结构，比例以输入图为准。",
        })
        data["usage"] = {
            "usage_scene": "儿童房或收藏架中的非接触陈列",
            "interaction_methods": ["桌面陈列"],
            "recommended_display_methods": ["完整正面", "侧后结构", "绒毛微距", "空间陈列", "广告主视觉"],
        }
        data["strategy_id"] = strategy["id"]
        data["dynamic_display_plan"] = {"concepts": [
            {"display_method_en":"Complete identity view on a suitable support","camera_azimuth":"front","camera_elevation":"eye_level","shot_distance":"medium","composition":"centered","product_position":"center","scene_type":"seamless_studio","scene_design_en":"Clean scale-aware studio made for this plush toy","photography_style_en":"e-commerce product photography","feature_focus_en":"exact face and overall silhouette"},
            {"display_method_en":"Alternate construction view without invented parts","camera_azimuth":"strict_side","camera_elevation":"product_level","shot_distance":"medium_wide","composition":"left_weighted","product_position":"upper_center","scene_type":"tabletop_still_life","scene_design_en":"Sparse tabletop support showing authentic sewn structure","photography_style_en":"precise construction study","feature_focus_en":"ear and limb proportions"},
            {"display_method_en":"Authentic surface detail close study","camera_azimuth":"front_three_quarter","camera_elevation":"high_angle","shot_distance":"extreme_closeup","composition":"right_weighted","product_position":"off_center_left","scene_type":"campaign_set","scene_design_en":"Tonal set derived from the observed blue and white palette","photography_style_en":"material macro photography","feature_focus_en":"short-pile texture and seam"},
            {"display_method_en":"Non-contact contextual room display","camera_azimuth":"rear_three_quarter","camera_elevation":"overhead","shot_distance":"wide","composition":"foreground_depth","product_position":"lower_right","scene_type":"lifestyle_environment","scene_design_en":"Safe sparse room context with no additional toys","photography_style_en":"environmental advertising photography","feature_focus_en":"recognizable scale and spatial presence"},
            {"display_method_en":"Premium product-specific campaign presentation","camera_azimuth":"back","camera_elevation":"low_angle","shot_distance":"closeup","composition":"diagonal","product_position":"lower_left","scene_type":"interactive_use","scene_design_en":"Non-contact playful display without invented behavior","photography_style_en":"bold commercial campaign","feature_focus_en":"original color blocks and rear silhouette"}
        ]}
        for section, panel in zip(data["copy"]["sections"], strategy["panels"]):
            section["panel_id"] = panel["id"]
        spec = ProductSpec.model_validate(data)
        plan = build_display_plan(spec, strategy)
        self.assertEqual([panel["id"] for panel in plan["panels"]], [
            "toy_identity", "toy_structure", "detail", "toy_interaction", "hero",
        ])
        prompts = [render_panel_prompt(spec, panel)[0] for panel in plan["panels"]]
        self.assertTrue(all("Structured shot contract" in prompt for prompt in prompts))
        self.assertTrue(all("sole product-design authority" in prompt for prompt in prompts))
        self.assertIn("short-pile texture and seam", prompts[2])

    def test_apparel_is_rejected_before_generation(self) -> None:
        data = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
        data["identity"].update({
            "category_group": "apparel", "subcategory": "衬衫",
            "support_status": "unsupported", "rejection_reason": "暂不支持服装类商品",
        })
        spec = ProductSpec.model_validate(data)
        with self.assertRaisesRegex(ValueError, "UNSUPPORTED_PRODUCT"):
            select_strategy(spec)

    def test_visible_brand_is_preserved_not_globally_forbidden(self) -> None:
        data = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
        data["branding"] = {"visible": True, "elements": ["正面金属字母标志"], "placement": "包体正面中央", "confidence": 0.9}
        data["prompt_facts"]["brand_elements_en"] = "A metal letter mark is visible at the front center."
        spec = ProductSpec.model_validate(data)
        strategy = StrategyRegistry().get(spec.strategy_id)
        prompt, _ = render_panel_prompt(spec, build_display_plan(spec, strategy)["panels"][0])
        self.assertIn("Preserve every visible brand mark", prompt)
        self.assertIn("Never add, remove, replace", prompt)
        self.assertNotIn("any visible branding", prompt)

    def test_legacy_analysis_moves_declared_background_objects_out_of_product_identity(self) -> None:
        job_spec = ROOT / "outputs/wechat-5-2181a03261443029cc75/job-20260817-151417-860467-b8236473/product-spec.json"
        if not job_spec.is_file():
            self.skipTest("runtime sample is not present")
        spec = load_product_spec(job_spec)
        strategy = StrategyRegistry().get(spec.strategy_id)
        prompt, _ = render_panel_prompt(spec, build_display_plan(spec, strategy)["panels"][0])
        excluded_line = next(line for line in prompt.splitlines() if line.startswith("Context objects"))
        detail_line = next(line for line in prompt.splitlines() if line.startswith("Distinctive details"))
        self.assertIn("非主体", excluded_line)
        self.assertNotIn("两颗白色珍珠", detail_line)
        self.assertIn("source-hidden or source-cropped", prompt)

    def test_bag_strategy_uses_carry_scenes_without_mirror_postprocess(self) -> None:
        data = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
        strategy = StrategyRegistry().get("bag-five-panel-v1")
        data["identity"].update({"category_group": "bags", "subcategory": "手提包", "product_name": "结构化手提包"})
        data["strategy_id"] = strategy["id"]
        for section, panel in zip(data["copy"]["sections"], strategy["panels"]):
            section["panel_id"] = panel["id"]
        spec = ProductSpec.model_validate(data)
        plan = build_display_plan(spec, strategy)
        self.assertEqual([panel["id"] for panel in plan["panels"]], ["carried", "structure", "detail", "lifestyle", "hero"])
        self.assertFalse(any(panel.get("postprocess") for panel in plan["panels"]))
        prompt, _ = render_panel_prompt(spec, plan["panels"][0])
        self.assertIn("carrying the exact bag", prompt)
        self.assertNotIn("mirror", prompt.lower())

    def test_visual_signatures_follow_panel_semantics_not_position(self) -> None:
        spec = load_product_spec(ROOT / "product.example.json")
        jewelry = build_display_plan(spec, StrategyRegistry().get("jewelry-five-panel-v2"))
        signatures = {panel["id"]: panel["diversity_directive_en"] for panel in jewelry["panels"]}
        self.assertIn("IDENTITY DETAIL STUDY", signatures["detail"])
        self.assertIn("AMBER EDITORIAL STILL LIFE", signatures["still_life"])
        self.assertIn("OPEN-BOX GIFT TABLEAU", signatures["gift"])
        self.assertIn("GEOMETRY-LOCKED MIRROR PORTRAIT", signatures["mirror"])
        detail_prompt, _ = render_panel_prompt(
            spec, next(panel for panel in jewelry["panels"] if panel["id"] == "detail")
        )
        worn_prompt, _ = render_panel_prompt(
            spec, next(panel for panel in jewelry["panels"] if panel["id"] == "worn")
        )
        self.assertIn(f"Show exactly {spec.identity.item_count} visible complete item", detail_prompt)
        self.assertNotIn("may intentionally crop", detail_prompt)
        self.assertIn(f"Show exactly {spec.identity.item_count} visible complete item", worn_prompt)

    def test_exact_count_is_normalized_from_description(self) -> None:
        count = StoneCount.model_validate({"mode": "exact", "description": "清晰可见2颗主石"})
        self.assertEqual(count.value, 2)

    def test_qwen_image_is_compressed(self) -> None:
        payload = image_data_url(TEST_IMAGE)
        self.assertTrue(payload.startswith("data:image/jpeg;base64,"))
        self.assertLessEqual(len(payload), 180_000)
        self.assertLessEqual(len(image_data_url(TEST_IMAGE, 80_000)), 80_000)

    def test_analysis_views_use_generic_aspect_ratio_tiles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            portrait = Path(temporary) / "portrait.jpg"
            square = Path(temporary) / "square.jpg"
            Image.new("RGB", (600, 1200), "#76695d").save(portrait)
            Image.new("RGB", (800, 800), "#76695d").save(square)
            self.assertEqual(len(analysis_image_data_urls(portrait, 50_000)), 7)
            self.assertEqual(len(analysis_image_data_urls(square, 50_000)), 5)

    def test_qwen_base_url_gets_https_when_scheme_is_omitted(self) -> None:
        self.assertEqual(normalize_api_base_url("naiccc.com/v1/"), "https://naiccc.com/v1")
        self.assertEqual(normalize_api_base_url(" http://localhost:8000/v1/ "), "http://localhost:8000/v1")

    def test_qwen_base_url_rejects_empty_value(self) -> None:
        with self.assertRaises(ValueError):
            normalize_api_base_url("  ")

    def test_layout_risk_prefers_low_detail_whitespace(self) -> None:
        image = Image.new("RGB", (1256, 1000), "#eee8df")
        draw = ImageDraw.Draw(image)
        for offset in range(0, 560, 18):
            draw.line((offset, 0, 560 - offset, 1000), fill="#171717", width=7)
        risk_map = build_risk_map(image)
        detailed = region_risk(image, risk_map, (30, 100, 500, 900))
        whitespace = region_risk(image, risk_map, (730, 100, 1200, 900))
        self.assertGreater(detailed, whitespace)

    def test_primary_product_crop_uses_normalized_evidence_with_padding(self) -> None:
        self.assertEqual(expanded_box((0.25, 0.25, 0.75, 0.75), 1000, 800), (190, 152, 810, 648))

    def test_mirror_compositor_preserves_real_plate_and_exact_geometry(self) -> None:
        source = Image.new("RGB", (1000, 1500), "#e8dfd3")
        draw = ImageDraw.Draw(source)
        draw.rectangle((540, 200, 900, 1380), fill="#243b55")
        draw.ellipse((620, 260, 760, 400), fill="#d8a88e")
        result = compose(source)
        self.assertEqual(result.size, source.size)
        # The real zone remains untouched, while the left mirror contains the
        # horizontally reversed source geometry rather than a model redraw.
        self.assertEqual(result.getpixel((700, 700)), source.getpixel((700, 700)))
        self.assertNotEqual(result.getpixel((250, 700)), source.getpixel((250, 700)))

    def test_style_reference_keeps_all_panels_in_composite_image(self) -> None:
        with Image.open(ROOT / "image1.jpg") as image:
            top, bottom, method = crop_bounds(image)
            regions = panel_bounds(image)
        self.assertEqual(method, "detected separator bands")
        self.assertLessEqual(top, 200)
        self.assertGreaterEqual(bottom, 6100)
        self.assertEqual(len(regions), 5)
        self.assertTrue(all(panel_bottom > panel_top for panel_top, panel_bottom in regions))

    def test_style_reference_falls_back_for_regular_image(self) -> None:
        image = Image.new("RGB", (800, 1200), "#d8cbbb")
        top, bottom, method = crop_bounds(image)
        self.assertEqual(method, "percentage fallback")
        self.assertEqual((top, bottom), (330, 1122))

    def test_rendered_assets_have_no_template_placeholders(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            subprocess.run(
                [
                    "python3",
                    str(ROOT / "scripts/render_product_assets.py"),
                    str(ROOT / "product.example.json"),
                    str(TEST_IMAGE),
                    str(ROOT / "image1.jpg"),
                    str(output),
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            page = json.loads((output / "page.json").read_text(encoding="utf-8"))
            self.assertEqual(len(page["sections"]), 5)
            manifest = json.loads((output / "generation-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["workflow_version"], 8)
            self.assertEqual(manifest["schema_version"], "2.0")
            display_plan = json.loads((output / "display-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(display_plan["strategy_id"], "jewelry-five-panel-v2")
            prompts = sorted((output / "prompts").glob("*.txt"))
            self.assertEqual([prompt.name for prompt in prompts], [f"panel-{number:02d}.txt" for number in range(1, 6)])
            for prompt in prompts:
                self.assertNotIn("$", prompt.read_text(encoding="utf-8"))
            mirror_prompt = (output / "work/panel-05-mirror_compose.txt").read_text(encoding="utf-8")
            self.assertIn("reverse it horizontally", mirror_prompt)
            self.assertIn("product structure", mirror_prompt)

    def test_five_image_jobs_start_in_parallel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "parallel"
            output.mkdir()
            spec = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
            spec["source"]["image_file"] = TEST_IMAGE.name
            spec["source"]["image_sha256"] = hashlib.sha256(TEST_IMAGE.read_bytes()).hexdigest()
            (output / "product-spec.json").write_text(
                json.dumps(spec, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment.update(
                {
                    "IMAGE_GEN_CLI": str(ROOT / "tests/fake_image_gen.py"),
                    "IMAGE2_PARALLELISM": "5",
                    "OPENAI_API_KEY": "test-key",
                }
            )
            subprocess.run(
                [
                    str(ROOT / "run_workflow.sh"),
                    "--output-dir",
                    str(output),
                    str(TEST_IMAGE),
                ],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            starts = [float(path.read_text()) for path in output.glob("panel-*.started")]
            self.assertEqual(len(starts), 5)
            self.assertLess(max(starts) - min(starts), 0.5)
            self.assertEqual(
                [path.read_text() for path in sorted(output.glob("panel-*.images"))],
                ["3"] * 5,
            )
            self.assertEqual(
                sorted(path.name for path in output.glob("panel-0?.png")),
                [f"panel-{number:02d}.png" for number in range(1, 6)],
            )
            self.assertTrue((output / "product-long.png").is_file())
            self.assertFalse((output / "jewelry-long.png").exists())
            with Image.open(output / "product-long.png") as result:
                self.assertEqual(result.width, 1256)
                self.assertGreaterEqual(result.height, 7600)
            self.assertFalse((output / "product-long-layout.json").exists())
            self.assertFalse((output / "work").exists())
            self.assertFalse((output / "logs").exists())
            self.assertEqual(
                sorted(path.name for path in (output / "prompts").glob("panel-*.txt")),
                [f"panel-{number:02d}.txt" for number in range(1, 6)],
            )

    def test_resume_reuses_verified_completed_panels(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "resume"
            output.mkdir()
            spec = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
            spec["source"]["image_file"] = TEST_IMAGE.name
            spec["source"]["image_sha256"] = hashlib.sha256(TEST_IMAGE.read_bytes()).hexdigest()
            (output / "product-spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            product_reference = output / "work/product-authority-reference.png"
            subprocess.run([
                sys.executable, str(ROOT / "scripts/prepare_product_reference.py"),
                str(TEST_IMAGE), str(output / "product-spec.json"), str(product_reference),
            ], cwd=ROOT, check=True, capture_output=True, text=True)
            subprocess.run([
                sys.executable, str(ROOT / "scripts/render_product_assets.py"),
                str(output / "product-spec.json"), str(product_reference), str(ROOT / "image1.jpg"), str(output),
            ], cwd=ROOT, check=True, capture_output=True, text=True)
            for number in range(1, 4):
                Image.new("RGB", (1024, 1536), "#c8b8a8").save(output / f"panel-{number:02d}.png")
            environment = os.environ.copy()
            environment.update({
                "IMAGE_GEN_CLI": str(ROOT / "tests/fake_image_gen.py"),
                "IMAGE2_PARALLELISM": "5", "OPENAI_API_KEY": "test-key",
            })
            completed = subprocess.run(
                [str(ROOT / "run_workflow.sh"), "--output-dir", str(output), str(TEST_IMAGE)],
                cwd=ROOT, env=environment, capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertFalse((output / "panel-01.started").exists())
            self.assertFalse((output / "panel-02.started").exists())
            self.assertFalse((output / "panel-03.started").exists())
            self.assertTrue((output / "panel-04.started").exists())
            self.assertTrue((output / "panel-05.started").exists())
            self.assertEqual(completed.stderr.count("Skipping completed panel:"), 3)

    def test_retryable_503_retries_and_keeps_the_exact_failed_panel(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "retry-503"
            output.mkdir()
            spec = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
            spec["source"]["image_file"] = TEST_IMAGE.name
            spec["source"]["image_sha256"] = hashlib.sha256(TEST_IMAGE.read_bytes()).hexdigest()
            (output / "product-spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            environment = os.environ.copy()
            environment.update({
                "IMAGE_GEN_CLI": str(ROOT / "tests/fake_image_gen.py"),
                "IMAGE2_PARALLELISM": "5", "IMAGE2_MAX_ATTEMPTS": "3",
                "IMAGE2_RETRY_DELAY": "0", "OPENAI_API_KEY": "test-key",
                "FAKE_IMAGE_ERROR_04": "503",
            })
            completed = subprocess.run(
                [str(ROOT / "run_workflow.sh"), "--output-dir", str(output), str(TEST_IMAGE)],
                cwd=ROOT, env=environment, capture_output=True, text=True,
            )
            combined = completed.stdout + completed.stderr
            self.assertEqual(completed.returncode, 1, combined)
            self.assertEqual(len(list((output / "logs").glob("panel-04-attempt-*.log"))), 3)
            self.assertIn("::workflow::panel_error::04::", combined)
            self.assertIn("stage=商品展示图片生成：第 04 张/共 5 张::status=1", combined)
            self.assertNotIn("stage=图片输入与风格参考预处理::status=1", combined)

    def test_bag_workflow_runs_without_jewelry_postprocess(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "bag"
            output.mkdir()
            spec = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
            strategy = StrategyRegistry().get("bag-five-panel-v1")
            spec["identity"].update({
                "category_group": "bags", "subcategory": "手提包", "product_name": "结构化手提包",
            })
            spec["strategy_id"] = strategy["id"]
            spec["source"]["image_file"] = TEST_IMAGE.name
            spec["source"]["image_sha256"] = hashlib.sha256(TEST_IMAGE.read_bytes()).hexdigest()
            for section, panel in zip(spec["copy"]["sections"], strategy["panels"]):
                section["panel_id"] = panel["id"]
            (output / "product-spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            environment = os.environ.copy()
            environment.update({
                "IMAGE_GEN_CLI": str(ROOT / "tests/fake_image_gen.py"),
                "IMAGE2_PARALLELISM": "5", "OPENAI_API_KEY": "test-key",
            })
            completed = subprocess.run(
                [str(ROOT / "run_workflow.sh"), "--output-dir", str(output), str(TEST_IMAGE)],
                cwd=ROOT, env=environment, check=True, capture_output=True, text=True,
            )
            self.assertTrue((output / "product-long.png").is_file())
            plan = json.loads((output / "display-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["postprocessors"], {})
            self.assertNotIn("图片后处理", completed.stdout)
            self.assertFalse((output / "work").exists())

    def test_rolling_pool_refills_and_mirror_repair_starts_early(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "rolling"
            output.mkdir()
            spec = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
            spec["source"]["image_file"] = TEST_IMAGE.name
            spec["source"]["image_sha256"] = hashlib.sha256(TEST_IMAGE.read_bytes()).hexdigest()
            (output / "product-spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            environment = os.environ.copy()
            environment.update({
                "IMAGE_GEN_CLI": str(ROOT / "tests/fake_image_gen.py"),
                "IMAGE2_PARALLELISM": "2", "OPENAI_API_KEY": "test-key",
                "FAKE_IMAGE_DELAY_01": "0.05", "FAKE_IMAGE_DELAY_02": "0.8",
                "FAKE_IMAGE_DELAY_03": "0.05", "FAKE_IMAGE_DELAY_04": "0.05",
                "FAKE_IMAGE_DELAY_05": "0.05", "FAKE_IMAGE_DELAY_POSTPROCESS": "0.05",
            })
            subprocess.run(
                [str(ROOT / "run_workflow.sh"), "--output-dir", str(output), str(TEST_IMAGE)],
                cwd=ROOT, env=environment, check=True, capture_output=True, text=True,
            )
            panel_02_finished = float((output / "panel-02.finished").read_text())
            panel_03_started = float((output / "panel-03.started").read_text())
            self.assertLess(panel_03_started, panel_02_finished)
            self.assertFalse((output / "work").exists())

    def test_mirror_uses_deterministic_compositor_not_expensive_image_edit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "quality-gate"
            output.mkdir()
            spec = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
            spec["source"]["image_file"] = TEST_IMAGE.name
            spec["source"]["image_sha256"] = hashlib.sha256(TEST_IMAGE.read_bytes()).hexdigest()
            (output / "product-spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            environment = os.environ.copy()
            environment.update({
                "IMAGE_GEN_CLI": str(ROOT / "tests/fake_image_gen.py"),
                "POSTPROCESS_QUALITY_CLI": str(ROOT / "tests/fake_quality_check.py"),
                "FAKE_QUALITY_STATUS": "0", "IMAGE2_PARALLELISM": "5", "OPENAI_API_KEY": "test-key",
            })
            completed = subprocess.run(
                [str(ROOT / "run_workflow.sh"), "--output-dir", str(output), str(TEST_IMAGE)],
                cwd=ROOT, env=environment, check=True, capture_output=True, text=True,
            )
            self.assertIn("::workflow::postprocess_ready::05::mirror_compose", completed.stdout)
            self.assertFalse((output / "work").exists())
            self.assertGreaterEqual(completed.stdout.count("::workflow::layout_ready::"), 5)

    def test_series_quality_status_three_triggers_regeneration_instead_of_err_trap(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "series-retry"
            output.mkdir()
            spec = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
            spec["source"]["image_file"] = TEST_IMAGE.name
            spec["source"]["image_sha256"] = hashlib.sha256(TEST_IMAGE.read_bytes()).hexdigest()
            (output / "product-spec.json").write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
            environment = os.environ.copy()
            environment.update({
                "IMAGE_GEN_CLI": str(ROOT / "tests/fake_image_gen.py"),
                "SERIES_QUALITY_CLI": str(ROOT / "tests/fake_series_quality.py"),
                "IMAGE2_PARALLELISM": "5", "OPENAI_API_KEY": "test-key",
                "ENV_FILE": str(output / "missing.env"),
            })
            completed = subprocess.run(
                [str(ROOT / "run_workflow.sh"), "--output-dir", str(output), str(TEST_IMAGE)],
                cwd=ROOT, env=environment, capture_output=True, text=True,
            )
            combined = completed.stdout + completed.stderr
            self.assertEqual(completed.returncode, 0, combined)
            self.assertIn("质量校验自动重生成：第 04 张", combined)
            self.assertNotIn("stage=五图商品一致性与重复度校验：第 1 次::status=3", combined)
            self.assertFalse((output / "logs").exists())
            self.assertTrue((output / "product-images.zip").is_file())



if __name__ == "__main__":
    unittest.main()
