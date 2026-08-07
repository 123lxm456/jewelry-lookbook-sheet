from __future__ import annotations

import json
import hashlib
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw
from pydantic import ValidationError

from jewelry_workflow.product_spec import CopySection, ProductSpec, StoneCount
from jewelry_workflow.qwen_vision import extract_json, image_data_url, normalize_api_base_url
from scripts.assemble_long_image import build_risk_map, region_risk
from scripts.prepare_style_reference import crop_bounds


ROOT = Path(__file__).resolve().parents[1]
TEST_IMAGE = ROOT / "jewelry-test.jpg"


class ProductWorkflowTests(unittest.TestCase):
    def test_example_spec_is_valid_and_has_five_sections(self) -> None:
        spec = ProductSpec.model_validate_json((ROOT / "product.example.json").read_text(encoding="utf-8"))
        self.assertEqual(len(spec.marketing_copy.sections), 5)

    def test_copy_rejects_non_chinese_title(self) -> None:
        with self.assertRaises(ValidationError):
            CopySection(eyebrow="DETAILS", title="English title", body="第一行中文\n第二行中文")

    def test_extract_json_accepts_fenced_response(self) -> None:
        self.assertEqual(extract_json("```json\n{\"ok\": true}\n```"), {"ok": True})

    def test_low_confidence_stone_cannot_be_marketed_as_diamond(self) -> None:
        data = json.loads((ROOT / "product.example.json").read_text(encoding="utf-8"))
        data["copy"]["sections"][0]["body"] = "钻石闪耀夺目光彩\n双层花簇凝聚视线"
        with self.assertRaises(ValidationError):
            ProductSpec.model_validate(data)

    def test_exact_count_is_normalized_from_description(self) -> None:
        count = StoneCount.model_validate({"mode": "exact", "description": "清晰可见2颗主石"})
        self.assertEqual(count.value, 2)

    def test_qwen_image_is_compressed(self) -> None:
        payload = image_data_url(TEST_IMAGE)
        self.assertTrue(payload.startswith("data:image/jpeg;base64,"))
        self.assertLessEqual(len(payload), 180_000)
        self.assertLessEqual(len(image_data_url(TEST_IMAGE, 80_000)), 80_000)

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

    def test_style_reference_keeps_all_panels_in_composite_image(self) -> None:
        with Image.open(ROOT / "image1.jpg") as image:
            top, bottom, method = crop_bounds(image)
        self.assertEqual(method, "detected separator bands")
        self.assertLessEqual(top, 200)
        self.assertGreaterEqual(bottom, 6100)

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
            self.assertEqual(manifest["workflow_version"], 4)
            prompts = sorted((output / "prompts").glob("*.txt"))
            self.assertEqual([prompt.name for prompt in prompts], [f"panel-{number:02d}.txt" for number in range(1, 6)])
            for prompt in prompts:
                self.assertNotIn("$", prompt.read_text(encoding="utf-8"))
            mirror_prompt = (output / "prompts/panel-05.txt").read_text(encoding="utf-8")
            self.assertIn("every visible real hand has one complete reflected hand", mirror_prompt)
            self.assertIn("correct reversed left-right orientation", mirror_prompt)

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
                sorted(path.name for path in output.glob("panel-0?.png")),
                [f"panel-{number:02d}.png" for number in range(1, 6)],
            )
            self.assertTrue((output / "jewelry-long.png").is_file())
            with Image.open(output / "jewelry-long.png") as result:
                self.assertEqual(result.width, 1256)
                self.assertGreaterEqual(result.height, 7600)
            layout_plan = json.loads((output / "jewelry-long-layout.json").read_text(encoding="utf-8"))
            layouts = [panel["layout"] for panel in layout_plan["panels"]]
            self.assertEqual(len(layouts), 5)
            self.assertEqual(len(set(layouts)), 5)
            self.assertIn(
                sum(layout in {"vertical_left", "vertical_right"} for layout in layouts),
                {1, 2},
            )
            self.assertTrue(all(panel["decoration"] in {"none", "line", "tint"} for panel in layout_plan["panels"]))
            self.assertLessEqual(
                sum(layout in {"left_sidebar", "right_sidebar", "bottom_description"} for layout in layouts),
                3,
            )


if __name__ == "__main__":
    unittest.main()
