"""Tests for the prompt builder's Excel/CSV import and export. Run from the
project root:
    python3 -m unittest discover -s tests -v
"""
import io
import os
import sys
import tempfile
import unittest

from openpyxl import Workbook, load_workbook

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

ENV_KEYS = ("DATABASE_PATH", "UPLOAD_FOLDER", "THUMBNAIL_FOLDER",
            "REFERENCE_UPLOAD_FOLDER", "REFERENCE_THUMBNAIL_FOLDER")


def xlsx(rows, sheet="Prompts"):
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


class PromptImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = self.tmp.name
        self._old_env = {k: os.environ.get(k) for k in ENV_KEYS}
        os.environ.update(
            DATABASE_PATH=os.path.join(d, "t.db"), UPLOAD_FOLDER=os.path.join(d, "u"),
            THUMBNAIL_FOLDER=os.path.join(d, "t"), REFERENCE_UPLOAD_FOLDER=os.path.join(d, "ru"),
            REFERENCE_THUMBNAIL_FOLDER=os.path.join(d, "rt"))
        for mod in ("config", "app"):
            sys.modules.pop(mod, None)
        import app as app_module
        self.app = app_module.create_app()
        self.app.config["TESTING"] = True
        self.c = self.app.test_client()
        with self.c.session_transaction() as s:
            s["logged_in"] = True

    def tearDown(self):
        self.tmp.cleanup()
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    # helpers
    def items(self, key):
        import db
        with self.app.app_context():
            return [r["text"] for r in db.get_db().execute(
                "SELECT i.text FROM prompt_items i JOIN prompt_categories c ON c.id = i.category_id "
                "WHERE c.key = ? ORDER BY i.sort_order, i.id", (key,)).fetchall()]

    def preview(self, data, name="p.xlsx"):
        return self.c.post("/api/generator/import/preview",
                           data={"file": (io.BytesIO(data), name)},
                           content_type="multipart/form-data")

    def by_label(self, preview_json):
        return {c["label"]: c for c in preview_json["categories"]}

    # tests
    def test_export_round_trips(self):
        r = self.c.get("/generator/export.xlsx")
        self.assertEqual(r.status_code, 200)
        wb = load_workbook(io.BytesIO(r.data))
        ws = wb["Prompts"]
        header = [c.value for c in ws[1]]
        self.assertEqual(header, ["Style", "Camera & Framing", "Subject", "Wardrobe",
                                  "Pose", "Environment", "Details"])
        self.assertEqual(ws.cell(2, 1).value, self.items("style")[0])
        # Re-importing the export finds nothing new
        p = self.preview(r.data).get_json()
        self.assertEqual(len(p["categories"]), 7)
        self.assertTrue(all(c["new"] == 0 and c["in_file"] == 50 for c in p["categories"]))

    def test_preview_matches_key_or_label_and_counts_new(self):
        existing = self.items("style")[0]
        data = xlsx([
            ["STYLE", "camera", "Mystery"],
            [existing.upper(), "A new camera angle", "x"],
            ["1. A brand new style", None, "y"],
            ["A brand new style", None, None],  # duplicate within the file
        ])
        r = self.preview(data)
        self.assertEqual(r.status_code, 200)
        p = r.get_json()
        cats = self.by_label(p)
        self.assertEqual(cats["Style"]["items"], [existing.upper(), "A brand new style"])
        self.assertEqual((cats["Style"]["new"], cats["Style"]["already"]), (1, 1))
        self.assertEqual(cats["Camera & Framing"]["in_file"], 1)
        self.assertEqual(p["unmatched"], ["Mystery"])
        self.assertEqual(self.items("style")[-1] != "A brand new style", True)  # nothing saved yet

    def test_import_add_skips_existing(self):
        before = self.items("pose")
        r = self.c.post("/api/generator/import", json={"categories": [
            {"category_id": self.cat_id("pose"), "mode": "add",
             "items": [before[0], "Brand new pose", "brand NEW pose"]}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["categories"][0]["added"], 1)
        self.assertEqual(self.items("pose"), before + ["Brand new pose"])

    def test_import_replace(self):
        r = self.c.post("/api/generator/import", json={"categories": [
            {"category_id": self.cat_id("details"), "mode": "replace", "items": ["One", "Two"]},
            {"category_id": self.cat_id("style"), "mode": "skip", "items": ["Ignored"]}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.items("details"), ["One", "Two"])
        self.assertNotIn("Ignored", self.items("style"))

    def test_import_rejects_bad_category_and_rolls_back(self):
        before = self.items("details")
        r = self.c.post("/api/generator/import", json={"categories": [
            {"category_id": self.cat_id("details"), "mode": "replace", "items": ["One"]},
            {"category_id": 99999, "mode": "add", "items": ["x"]}]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.items("details"), before)

    def test_long_layout_and_csv(self):
        csv_data = "Category,Prompt\nPose,Long layout pose\nDetails,Long layout detail\nNope,x\n".encode()
        p = self.preview(csv_data, "list.csv").get_json()
        cats = self.by_label(p)
        self.assertEqual(cats["Pose"]["items"], ["Long layout pose"])
        self.assertEqual(cats["Details"]["items"], ["Long layout detail"])
        self.assertEqual(p["unmatched"], ["Nope"])

    def test_reads_prompts_sheet_not_first_sheet(self):
        wb = Workbook()
        wb.active.title = "How to use"
        wb.active.append(["Style"])
        wb.active.append(["should be ignored"])
        ws = wb.create_sheet("Prompts")
        ws.append(["Wardrobe"])
        ws.append(["From the Prompts sheet"])
        buf = io.BytesIO()
        wb.save(buf)
        cats = self.by_label(self.preview(buf.getvalue()).get_json())
        self.assertEqual(list(cats), ["Wardrobe"])

    def test_bad_files(self):
        self.assertEqual(self.preview(b"not a zip").status_code, 400)
        self.assertEqual(self.preview(b"hello", "notes.txt").status_code, 400)
        r = self.preview(xlsx([["Foo", "Bar"], ["a", "b"]]))
        self.assertEqual(r.status_code, 400)
        self.assertIn("No columns matched", r.get_json()["error"])

    def test_requires_login(self):
        c = self.app.test_client()
        self.assertEqual(c.get("/generator/export.xlsx").status_code, 302)
        self.assertEqual(c.post("/api/generator/import", json={}).status_code, 302)

    def cat_id(self, key):
        import db
        with self.app.app_context():
            return db.get_db().execute("SELECT id FROM prompt_categories WHERE key = ?", (key,)).fetchone()["id"]


if __name__ == "__main__":
    unittest.main()
