"""Tests for workflow_parser, especially the A1111-style "parameters" fallback.

Builds tiny PNGs on the fly (no binary fixtures to keep around). Run from the
project root with:  python3 -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import unittest

from PIL import Image
from PIL.PngImagePlugin import PngInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import workflow_parser as wp  # noqa: E402

# A realistic text block, in the shape the LoRA Manager / Civitai-style savers write.
PARAMS = (
    'A fox in a forest \u2014 "golden hour", cinematic, ta86_inkriot\n'
    "<lora:Afterlight_v1:0.25> <lora:Elegance_Niji_KR2:1.0>\n"
    "Negative prompt: blurry, low quality\n"
    "Steps: 8, Sampler: Euler simple, CFG scale: 1.0, Seed: 29776804209934, "
    "Size: 1256x1672, Clip skip: 1, Model hash: EB4DD8C612, "
    "Model: krea2TurboOfficialComfy_krea2TurboFp8, "
    'Hashes: {"model":"EB4DD8C612","LORA:Afterlight_v1":"41459BE6C4"}, '
    "Version: ComfyUI"
)

API_GRAPH = {
    "3": {"class_type": "KSampler", "inputs": {
        "seed": 123, "steps": 20, "cfg": 7, "sampler_name": "euler", "scheduler": "normal",
        "denoise": 1, "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
        "latent_image": ["5", 0]}},
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "model.safetensors"}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 768, "batch_size": 1}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a red fox", "clip": ["4", 1]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry", "clip": ["4", 1]}},
}


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def png(self, **chunks):
        info = PngInfo()
        for key, value in chunks.items():
            info.add_text(key, value)
        path = os.path.join(self.dir.name, f"t{len(os.listdir(self.dir.name))}.png")
        Image.new("RGB", (8, 8)).save(path, pnginfo=info)
        return path

    # --- the new fallback -------------------------------------------------
    def test_parameters_fallback_extracts_everything(self):
        r = wp.extract_metadata(self.png(parameters=PARAMS))
        self.assertTrue(r["has_metadata"])
        self.assertEqual(r["positive_prompt"], 'A fox in a forest \u2014 "golden hour", cinematic, ta86_inkriot')
        self.assertEqual(r["negative_prompt"], "blurry, low quality")
        self.assertEqual(r["model_name"], "krea2TurboOfficialComfy_krea2TurboFp8")
        self.assertEqual((r["sampler_name"], r["scheduler"]), ("Euler", "simple"))
        self.assertEqual((r["steps"], r["cfg"], r["seed"]), (8, 1.0, 29776804209934))
        self.assertEqual((r["width"], r["height"]), (1256, 1672))

    def test_lora_tags_become_loras_and_leave_the_prompt(self):
        r = wp.extract_metadata(self.png(parameters=PARAMS))
        self.assertNotIn("<lora:", r["positive_prompt"])
        self.assertEqual(
            [(l["name"], l["strength_model"], l["strength_clip"]) for l in r["loras"]],
            [("Afterlight_v1", 0.25, 0.25), ("Elegance_Niji_KR2", 1.0, 1.0)])

    def test_separate_clip_weight_tag(self):
        r = wp.parse_parameters_text("x <lora:foo:0.8:0.6>\nSteps: 5, Sampler: Euler")
        self.assertEqual((r["loras"][0]["strength_model"], r["loras"][0]["strength_clip"]), (0.8, 0.6))

    def test_no_tags_falls_back_to_hash_names_with_unknown_weight(self):
        text = PARAMS.replace("<lora:Afterlight_v1:0.25> <lora:Elegance_Niji_KR2:1.0>\n", "")
        r = wp.parse_parameters_text(text)
        self.assertEqual([l["name"] for l in r["loras"]], ["Afterlight_v1"])
        self.assertIsNone(r["loras"][0]["strength_model"])

    def test_workflow_is_still_kept_for_copy_and_download(self):
        r = wp.extract_metadata(self.png(parameters=PARAMS, workflow='{"nodes": []}'))
        self.assertEqual(r["workflow_json"], '{"nodes": []}')
        self.assertTrue(r["has_metadata"])

    def test_no_negative_prompt_marker(self):
        r = wp.parse_parameters_text("just a prompt\nSteps: 20, Sampler: DPM++ 2M Karras, Seed: 5")
        self.assertEqual(r["positive_prompt"], "just a prompt")
        self.assertIsNone(r["negative_prompt"])
        self.assertEqual((r["sampler_name"], r["scheduler"]), ("DPM++ 2M", "karras"))

    def test_schedule_type_field_wins(self):
        r = wp.parse_parameters_text("p\nSteps: 20, Sampler: Euler a, Schedule type: SGM Uniform, Denoising strength: 0.55")
        self.assertEqual((r["sampler_name"], r["scheduler"], r["denoise"]), ("Euler a", "sgm_uniform", 0.55))

    def test_multiline_prompt_and_steps_word_inside_prompt(self):
        text = "line one\nwe took Steps: forward, then more\nline three\nNegative prompt: bad\nSteps: 9, Seed: 7"
        r = wp.parse_parameters_text(text)
        self.assertIn("Steps: forward", r["positive_prompt"])
        self.assertEqual((r["steps"], r["seed"], r["negative_prompt"]), (9, 7, "bad"))

    def test_hires_steps_is_not_mistaken_for_steps(self):
        r = wp.parse_parameters_text("p\nSteps: 20, Hires steps: 12, Seed: 1")
        self.assertEqual(r["steps"], 20)

    def test_unrecognised_text_is_ignored(self):
        self.assertIsNone(wp.parse_parameters_text("Hello, this is just a comment"))
        for junk in (None, "", "   ", 42, b"bytes"):
            self.assertIsNone(wp.parse_parameters_text(junk))

    # --- regressions: existing behaviour must not change ------------------
    def test_standard_prompt_chunk_still_uses_the_graph(self):
        # Even with a conflicting parameters block present, the graph wins.
        r = wp.extract_metadata(self.png(prompt=json.dumps(API_GRAPH), parameters=PARAMS))
        self.assertEqual((r["positive_prompt"], r["negative_prompt"]), ("a red fox", "blurry"))
        self.assertEqual((r["seed"], r["steps"], r["model_name"]), (123, 20, "model.safetensors"))
        self.assertEqual((r["width"], r["height"]), (512, 768))
        self.assertIsNotNone(r["prompt_json"])

    def test_no_metadata_at_all(self):
        r = wp.extract_metadata(self.png())
        self.assertFalse(r["has_metadata"])
        self.assertIsNone(r["positive_prompt"])

    def test_workflow_only_behaves_as_before(self):
        r = wp.extract_metadata(self.png(workflow='{"nodes": []}'))
        self.assertTrue(r["has_metadata"])
        self.assertIsNone(r["positive_prompt"])

    def test_garbage_parameters_changes_nothing(self):
        self.assertFalse(wp.extract_metadata(self.png(parameters="not generation data"))["has_metadata"])
        r = wp.extract_metadata(self.png(parameters="not generation data", workflow="{}"))
        self.assertTrue(r["has_metadata"])
        self.assertIsNone(r["positive_prompt"])

    # --- empty prompt in "parameters": fall back to the workflow ------------
    EMPTY_PROMPT_PARAMS = (
        "<lora:Elegance_Niji_KR2:0.5> <lora:Beautiful_V2:1.0>\n"
        "Steps: 8, Sampler: Euler simple, CFG scale: 1.0, Seed: 19552489790971, "
        "Size: 1256x1672, Model: krea2TurboFp8"
    )
    LONG_PROMPT = "She kneels on a plush white rug wearing a light-blue hoodie, silver hair in waves."

    def lora_manager_workflow(self, **extra):
        nodes = [
            # the subgraph instance: its widget values hold what was actually typed
            {"id": 30, "type": "b0e5ca93-2731-42b9-8e0a-d28ea851ff81",
             "widgets_values": [self.LONG_PROMPT, False, 512, 1024, "qwen_image_vae.safetensors"]},
            {"id": 47, "type": "MarkdownNote",
             "widgets_values": ["Download the style LoRAs used in this workflow from the link below."]},
            {"id": 54, "type": "UNETLoader", "widgets_values": ["krea2TurboOfficialComfy_krea2TurboFp8.safetensors", "default"]},
            {"id": 55, "type": "Lora Loader (LoraManager)",
             "widgets_values": [{}, "<lora:Elegance_Niji_KR2:0.50> <lora:Beautiful_V2:1.00> and more text here"]},
            {"id": 56, "type": "TriggerWord Toggle (LoraManager)",
             "widgets_values": [True, [{"text": "ArsMJStyle", "active": True}], "ArsMJStyle, colored pencil,"]},
        ]
        nodes.extend(extra.get("nodes", []))
        return json.dumps({
            "nodes": nodes,
            # definitions hold stale defaults + a system prompt - must never be read
            "definitions": {"subgraphs": [{"nodes": [
                {"id": 18, "type": "PrimitiveStringMultiline",
                 "widgets_values": ["You are an expert prompt engineer for text-to-image models. " * 20]}]}]},
        })

    def test_empty_parameters_prompt_filled_from_workflow(self):
        r = wp.extract_metadata(self.png(parameters=self.EMPTY_PROMPT_PARAMS,
                                         workflow=self.lora_manager_workflow()))
        self.assertEqual(r["positive_prompt"], self.LONG_PROMPT + ", ArsMJStyle, colored pencil,")
        # settings still come from the parameters text
        self.assertEqual(r["seed"], 19552489790971)
        self.assertEqual([l["name"] for l in r["loras"]], ["Elegance_Niji_KR2", "Beautiful_V2"])

    def test_workflow_fallback_ignores_notes_loras_and_subgraph_definitions(self):
        r = wp.extract_metadata(self.png(parameters=self.EMPTY_PROMPT_PARAMS,
                                         workflow=self.lora_manager_workflow()))
        self.assertNotIn("expert prompt engineer", r["positive_prompt"])
        self.assertNotIn("Download the style", r["positive_prompt"])
        self.assertNotIn("<lora:", r["positive_prompt"])

    def test_workflow_fallback_skips_muted_trigger_words(self):
        wf = json.loads(self.lora_manager_workflow())
        wf["nodes"][4]["mode"] = 4  # bypassed
        r = wp.extract_metadata(self.png(parameters=self.EMPTY_PROMPT_PARAMS, workflow=json.dumps(wf)))
        self.assertEqual(r["positive_prompt"], self.LONG_PROMPT)

    def test_parameters_prompt_wins_over_workflow(self):
        r = wp.extract_metadata(self.png(parameters=PARAMS, workflow=self.lora_manager_workflow()))
        self.assertTrue(r["positive_prompt"].startswith("A fox in a forest"))

    def test_workflow_fallback_with_bad_workflow_json(self):
        r = wp.extract_metadata(self.png(parameters=self.EMPTY_PROMPT_PARAMS, workflow="{not json"))
        self.assertTrue(r["has_metadata"])
        self.assertIsNone(r["positive_prompt"])
        self.assertEqual(r["steps"], 8)

    def test_extract_never_raises_on_non_png(self):
        path = os.path.join(self.dir.name, "fake.png")
        with open(path, "wb") as f:
            f.write(b"this is not an image")
        self.assertFalse(wp.extract_metadata(path)["has_metadata"])


if __name__ == "__main__":
    unittest.main()
