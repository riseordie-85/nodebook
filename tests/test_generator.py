"""Tests for the prompt builder (Generator page) routes. Uses a throwaway
database/upload folder via environment variables. Run from the project root:
    python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


class GeneratorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        d = cls.tmp.name
        cls._old_env = {k: os.environ.get(k) for k in (
            "DATABASE_PATH", "UPLOAD_FOLDER", "THUMBNAIL_FOLDER",
            "REFERENCE_UPLOAD_FOLDER", "REFERENCE_THUMBNAIL_FOLDER")}
        os.environ.update(
            DATABASE_PATH=os.path.join(d, "t.db"), UPLOAD_FOLDER=os.path.join(d, "u"),
            THUMBNAIL_FOLDER=os.path.join(d, "t"), REFERENCE_UPLOAD_FOLDER=os.path.join(d, "ru"),
            REFERENCE_THUMBNAIL_FOLDER=os.path.join(d, "rt"))
        for mod in ("config", "app"):
            sys.modules.pop(mod, None)
        import app as app_module
        cls.app = app_module.create_app()
        cls.app.config["TESTING"] = True

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()
        for k, v in cls._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def client(self, logged_in=True):
        c = self.app.test_client()
        if logged_in:
            with c.session_transaction() as s:
                s["logged_in"] = True
        return c

    def category(self, key):
        import db
        with self.app.app_context():
            return dict(db.get_db().execute("SELECT * FROM prompt_categories WHERE key = ?", (key,)).fetchone())

    def test_seeded_with_seven_categories_of_fifty(self):
        import db
        with self.app.app_context():
            rows = db.get_db().execute(
                "SELECT c.key, COUNT(i.id) AS n FROM prompt_categories c "
                "LEFT JOIN prompt_items i ON i.category_id = c.id GROUP BY c.id ORDER BY c.sort_order").fetchall()
        self.assertEqual([r["key"] for r in rows],
                         ["style", "camera", "subject", "wardrobe", "pose", "environment", "details"])
        self.assertTrue(all(r["n"] == 50 for r in rows))

    def test_seed_does_not_run_twice(self):
        import db, seed_prompt_builder
        with self.app.app_context():
            seed_prompt_builder.seed_if_empty(db.get_db())
            n = db.get_db().execute("SELECT COUNT(*) AS n FROM prompt_categories").fetchone()["n"]
        self.assertEqual(n, 7)

    def test_page_renders_with_data(self):
        r = self.client().get("/generator")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("builder-data", html)
        self.assertIn("Camera &amp; Framing", html.replace("\\u0026", "&amp;"))

    def test_requires_login(self):
        c = self.client(logged_in=False)
        self.assertEqual(c.get("/generator").status_code, 302)
        r = c.post("/api/generator/items", json={"category_id": 1, "text": "x"})
        self.assertEqual(r.status_code, 302)

    def test_add_edit_delete_roundtrip(self):
        c = self.client()
        cat = self.category("pose")
        r = c.post("/api/generator/items", json={"category_id": cat["id"], "text": "  Doing a handstand  "})
        self.assertEqual(r.status_code, 200)
        item = r.get_json()
        self.assertEqual(item["text"], "Doing a handstand")

        r = c.post(f"/api/generator/items/{item['id']}/update", json={"text": "Doing a cartwheel"})
        self.assertEqual(r.get_json()["text"], "Doing a cartwheel")

        r = c.post(f"/api/generator/items/{item['id']}/delete")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(c.post(f"/api/generator/items/{item['id']}/delete").status_code, 404)

    def test_duplicates_and_blanks_rejected(self):
        c = self.client()
        cat = self.category("style")
        first = c.post("/api/generator/items", json={"category_id": cat["id"], "text": "Unique style X"}).get_json()
        dup = c.post("/api/generator/items", json={"category_id": cat["id"], "text": "unique STYLE x"})
        self.assertEqual(dup.status_code, 409)
        self.assertEqual(c.post("/api/generator/items", json={"category_id": cat["id"], "text": "   "}).status_code, 400)
        self.assertEqual(c.post("/api/generator/items", json={"category_id": 99999, "text": "a"}).status_code, 404)
        self.assertEqual(c.post("/api/generator/items", json={"text": "a"}).status_code, 404)
        # editing into an existing item's text is also a clash
        other = c.post("/api/generator/items", json={"category_id": cat["id"], "text": "Another style Y"}).get_json()
        self.assertEqual(c.post(f"/api/generator/items/{other['id']}/update", json={"text": "Unique style X"}).status_code, 409)
        # same text in a *different* category is fine
        cam = self.category("camera")
        in_camera = c.post("/api/generator/items", json={"category_id": cam["id"], "text": "Unique style X"})
        self.assertEqual(in_camera.status_code, 200)
        c.post(f"/api/generator/items/{in_camera.get_json()['id']}/delete")
        c.post(f"/api/generator/items/{first['id']}/delete")
        c.post(f"/api/generator/items/{other['id']}/delete")

    def test_can_empty_a_category_and_page_still_renders(self):
        import db
        c = self.client()
        cat = self.category("details")
        with self.app.app_context():
            conn = db.get_db()
            saved = conn.execute("SELECT text, sort_order FROM prompt_items WHERE category_id = ?", (cat["id"],)).fetchall()
            conn.execute("DELETE FROM prompt_items WHERE category_id = ?", (cat["id"],))
            conn.commit()
        try:
            self.assertEqual(c.get("/generator").status_code, 200)
        finally:
            with self.app.app_context():
                conn = db.get_db()
                conn.executemany("INSERT INTO prompt_items (category_id, text, sort_order) VALUES (?,?,?)",
                                 [(cat["id"], r["text"], r["sort_order"]) for r in saved])
                conn.commit()

    # ---------- bulk editor ----------

    def count(self, cat_id):
        import db
        with self.app.app_context():
            return db.get_db().execute(
                "SELECT COUNT(*) AS n FROM prompt_items WHERE category_id = ?", (cat_id,)).fetchone()["n"]

    def test_edit_page_requires_login_and_renders(self):
        self.assertIn(self.client(False).get("/generator/edit").status_code, (301, 302, 401))
        r = self.client().get("/generator/edit")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"edit-data", r.data)
        self.assertIn(b"generator-edit.js", r.data)
        self.assertIn(b"/generator/edit", self.client().get("/generator").data)  # link from the builder

    def test_bulk_add_strips_numbers_dedupes_and_skips_existing(self):
        c = self.client()
        cat = self.category("pose")
        before = self.count(cat["id"])
        r = c.post("/api/generator/items/bulk-add", json={
            "category_id": cat["id"],
            "text": "1. bulk pose alpha\n2) Bulk Pose Alpha\n\n   bulk   pose beta  \n"})
        self.assertEqual(r.status_code, 200)
        added = r.get_json()["added"]
        self.assertEqual([a["text"] for a in added], ["bulk pose alpha", "bulk pose beta"])
        self.assertEqual(self.count(cat["id"]), before + 2)
        # Same again: everything already there (case-insensitive)
        r = c.post("/api/generator/items/bulk-add", json={"category_id": cat["id"], "text": "BULK POSE ALPHA"})
        self.assertEqual(r.get_json()["added"], [])
        self.assertEqual(r.get_json()["skipped"], 1)
        c.post("/api/generator/items/bulk-delete", json={"ids": [a["id"] for a in added]})
        self.assertEqual(self.count(cat["id"]), before)

    def test_bulk_add_rejects_bad_input(self):
        c = self.client()
        cat = self.category("pose")
        self.assertEqual(c.post("/api/generator/items/bulk-add", json={"category_id": 99999, "text": "x"}).status_code, 404)
        self.assertEqual(c.post("/api/generator/items/bulk-add", json={"category_id": "1", "text": "x"}).status_code, 404)
        self.assertEqual(c.post("/api/generator/items/bulk-add", json={"category_id": cat["id"], "text": "  \n "}).status_code, 400)
        self.assertEqual(c.post("/api/generator/items/bulk-add", json={"category_id": cat["id"]}).status_code, 400)
        self.assertIn(self.client(False).post("/api/generator/items/bulk-add",
                      json={"category_id": cat["id"], "text": "x"}).status_code, (301, 302, 401))

    def test_bulk_delete_counts_and_validation(self):
        c = self.client()
        cat = self.category("details")
        before = self.count(cat["id"])
        ids = [c.post("/api/generator/items", json={"category_id": cat["id"], "text": "bulkdel %d" % i}).get_json()["id"]
               for i in range(3)]
        r = c.post("/api/generator/items/bulk-delete", json={"ids": ids + [987654]})
        self.assertEqual(r.get_json()["deleted"], 3)  # unknown id is ignored, not an error
        self.assertEqual(self.count(cat["id"]), before)
        for bad in ({}, {"ids": []}, {"ids": "1"}, {"ids": [1, "2"]}, {"ids": [True]}, {"ids": [1.5]}):
            self.assertEqual(c.post("/api/generator/items/bulk-delete", json=bad).status_code, 400, bad)
        self.assertIn(self.client(False).post("/api/generator/items/bulk-delete", json={"ids": [1]}).status_code,
                      (301, 302, 401))


if __name__ == "__main__":
    unittest.main()
