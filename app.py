import json
import os
import uuid
import hashlib
import secrets
from functools import wraps

from flask import (
    Flask, render_template, request, redirect, url_for, session,
    send_from_directory, flash, abort, Response, jsonify, current_app
)
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from PIL import Image

import db
from config import Config
from workflow_parser import extract_metadata, get_full_workflow_json
import seed_prompt_builder
import prompt_io


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    os.makedirs(os.path.dirname(app.config["DATABASE"]), exist_ok=True)
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["THUMBNAIL_FOLDER"], exist_ok=True)

    db.init_app(app)

    with app.app_context():
        # Always safe to run - schema.sql only uses CREATE TABLE/INDEX IF NOT
        # EXISTS, so this picks up new tables on an existing database too
        # (rather than only on a brand new one).
        db.init_db()
        db.migrate()
        seed_prompt_builder.seed_if_empty(db.get_db())

    register_routes(app)
    return app


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def api_key_or_login_required(view):
    """Like login_required, but also accepts a static API key via the
    X-API-Key header - for non-browser clients (automation tools, HTTP
    Shortcuts, etc.) that can't carry a session cookie. Deliberately used
    on only one route rather than folded into login_required everywhere,
    so adding this doesn't widen the whole app's attack surface - just this
    one upload path."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        configured_key = current_app.config.get("API_KEY", "")
        provided_key = request.headers.get("X-API-Key", "")
        if configured_key and provided_key and secrets.compare_digest(provided_key, configured_key):
            return view(*args, **kwargs)
        if session.get("logged_in"):
            return view(*args, **kwargs)
        return redirect(url_for("login", next=request.path))
    return wrapped


def allowed_file(filename, app, extensions=None):
    extensions = extensions if extensions is not None else app.config["ALLOWED_EXTENSIONS"]
    return "." in filename and filename.rsplit(".", 1)[1].lower() in extensions


def make_thumbnail(src_path, dest_path, size=(400, 400)):
    with Image.open(src_path) as im:
        im = im.convert("RGB")
        im.thumbnail(size)
        im.save(dest_path, "JPEG", quality=85)


def parse_view_param(request):
    """The gallery's ?folder= param has three possible meanings:
      - absent entirely -> default view, images not filed into any folder
      - "all" -> every image regardless of folder
      - a number -> that specific folder
    Returns (mode, folder_id) where mode is 'unfiled' | 'all' | 'folder'."""
    raw = request.args.get("folder")
    if raw is None:
        return "unfiled", None
    if raw == "all":
        return "all", None
    try:
        return "folder", int(raw)
    except (TypeError, ValueError):
        return "unfiled", None


SORT_MODES = {
    # key -> (SQL expression TEMPLATE to sort by, direction). {t} is a
    # table-name prefix (e.g. "images.") for queries that JOIN more than
    # one table, where a bare column name would be ambiguous - empty
    # string for single-table queries. Templated rather than a plain
    # string specifically because COALESCE(saved_at, uploaded_at) has TWO
    # column names that each need the prefix; a table alias can only ever
    # go in front of a column, never in front of a whole function call
    # like "images.COALESCE(...)", which is a syntax error.
    "new_saved": ("COALESCE({t}saved_at, {t}uploaded_at)", "DESC"),
    "old_saved": ("COALESCE({t}saved_at, {t}uploaded_at)", "ASC"),
    "new_upload": ("{t}uploaded_at", "DESC"),
    "old_upload": ("{t}uploaded_at", "ASC"),
}
DEFAULT_SORT = "new_saved"


def parse_sort_param(request, table_prefix=""):
    """The gallery's ?sort= param picks which date field to order by and
    which direction. Defaults to newest-first by save date (the file's own
    last-modified time, captured at upload when available) - falling back
    to upload date for any row where that wasn't captured (e.g. anything
    that came in through the API-key/share-target path). Pass table_prefix
    (e.g. "images.") for a query that JOINs more than one table, so the
    column references in the expression aren't ambiguous. Returns
    (sort_key, sql_expr, direction); sort_key is always one of SORT_MODES'
    keys, safe to interpolate since it's validated against that dict
    first - never the raw querystring value."""
    raw = request.args.get("sort", DEFAULT_SORT)
    if raw not in SORT_MODES:
        raw = DEFAULT_SORT
    template, direction = SORT_MODES[raw]
    return raw, template.format(t=table_prefix), direction


def parse_saved_at(raw_value):
    """Converts a JS File.lastModified value (milliseconds since epoch, as a
    string) into the same datetime string format SQLite's datetime('now')
    produces, so the two sort together correctly. Returns None for
    anything missing or unparseable, rather than raising - a bad or absent
    timestamp just means this row falls back to uploaded_at when sorting."""
    if not raw_value:
        return None
    try:
        from datetime import datetime, timezone
        ms = float(raw_value)
        if ms <= 0:
            return None
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return None


def process_reference_uploads(files, app, saved_at_values=None):
    """Shared by the normal upload form, the PWA share-target endpoint, and
    the API-key upload path (HTTP Shortcuts etc.) - all need to
    hash/dedupe/thumbnail/save incoming files exactly the same way. Flashes
    its own summary messages (harmless no-op for non-browser callers) and
    also returns the summary as a dict, for callers that want to respond
    with JSON instead of a redirect. saved_at_values is optional - only the
    browser upload form can supply it (via each file's own lastModified
    time); the share-target/API-key paths have no way to capture this, so
    those rows just fall back to uploaded_at when sorting."""
    conn = db.get_db()
    saved_at_values = saved_at_values or []
    saved = 0
    skipped = []
    failed = []
    duplicates = []

    for idx, file in enumerate(files):
        if not file or file.filename == "":
            continue
        if not allowed_file(file.filename, app, app.config["REFERENCE_ALLOWED_EXTENSIONS"]):
            skipped.append(file.filename)
            continue

        try:
            file_bytes = file.read()
            file_hash = hashlib.sha256(file_bytes).hexdigest()
            file.seek(0)

            existing = conn.execute(
                "SELECT id FROM reference_images WHERE file_hash = ?", (file_hash,)
            ).fetchone()
            if existing:
                duplicates.append(file.filename)
                continue

            original_name = secure_filename(file.filename)
            stored_name = f"{uuid.uuid4().hex}_{original_name}"
            filepath = os.path.join(app.config["REFERENCE_UPLOAD_FOLDER"], stored_name)
            file.save(filepath)

            saved_at = parse_saved_at(saved_at_values[idx]) if idx < len(saved_at_values) else None

            thumb_name = f"{uuid.uuid4().hex}.jpg"
            thumb_path = os.path.join(app.config["REFERENCE_THUMBNAIL_FOLDER"], thumb_name)
            try:
                make_thumbnail(filepath, thumb_path)
            except Exception:
                thumb_name = None

            conn.execute(
                "INSERT INTO reference_images (filename, original_filename, thumbnail, file_hash, saved_at) VALUES (?,?,?,?,?)",
                (stored_name, original_name, thumb_name, file_hash, saved_at),
            )
            conn.commit()
            saved += 1
        except Exception:
            conn.rollback()
            failed.append(file.filename)

    if saved:
        flash(f"Uploaded {saved} image(s).")
    if duplicates:
        flash(f"Skipped {len(duplicates)} duplicate(s) already in your library: {', '.join(duplicates)}")
    if skipped:
        flash(f"Skipped (unsupported type): {', '.join(skipped)}")
    if failed:
        flash(f"Failed to process: {', '.join(failed)} - check the file(s) and try re-uploading just those.")

    return {
        "saved": saved,
        "duplicates": duplicates,
        "skipped": skipped,
        "failed": failed,
    }


def register_routes(app):

    # ---------- Auth ----------

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            password = request.form.get("password", "")
            expected_hash = app.config["APP_PASSWORD_HASH"]
            if expected_hash and check_password_hash(expected_hash, password):
                session.permanent = True
                session["logged_in"] = True
                next_url = request.args.get("next") or url_for("gallery")
                return redirect(next_url)
            flash("Incorrect password.")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---------- Gallery ----------

    @app.route("/")
    @login_required
    def gallery():
        conn = db.get_db()
        view_mode, folder_id = parse_view_param(request)
        sort_key, sort_expr, sort_dir = parse_sort_param(request)
        page = max(1, request.args.get("page", 1, type=int))
        per_page = 60
        offset = (page - 1) * per_page

        if view_mode == "folder":
            # This branch JOINs two tables, so bare column names in the sort
            # expression would be ambiguous - re-derive it with the images.
            # prefix rather than reusing the unprefixed sort_expr above.
            _, sort_expr_joined, _ = parse_sort_param(request, table_prefix="images.")
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM image_folders WHERE folder_id = ?", (folder_id,)
            ).fetchone()["c"]
            images = conn.execute(
                f"""SELECT images.* FROM images
                   JOIN image_folders ON image_folders.image_id = images.id
                   WHERE image_folders.folder_id = ?
                   ORDER BY images.is_favorite DESC, {sort_expr_joined} {sort_dir}, images.id {sort_dir}
                   LIMIT ? OFFSET ?""",
                (folder_id, per_page, offset),
            ).fetchall()
            current_folder = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
            folder_qs = f"&folder={folder_id}&sort={sort_key}"
        elif view_mode == "all":
            total = conn.execute("SELECT COUNT(*) AS c FROM images").fetchone()["c"]
            images = conn.execute(
                f"SELECT * FROM images ORDER BY is_favorite DESC, {sort_expr} {sort_dir}, id {sort_dir} LIMIT ? OFFSET ?",
                (per_page, offset),
            ).fetchall()
            current_folder = None
            folder_qs = f"&folder=all&sort={sort_key}"
        else:  # unfiled - the default view, so organizing by drag-and-drop
            # visibly empties this list as things get filed away.
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM images WHERE id NOT IN (SELECT image_id FROM image_folders)"
            ).fetchone()["c"]
            images = conn.execute(
                f"""SELECT * FROM images WHERE id NOT IN (SELECT image_id FROM image_folders)
                   ORDER BY is_favorite DESC, {sort_expr} {sort_dir}, id {sort_dir} LIMIT ? OFFSET ?""",
                (per_page, offset),
            ).fetchall()
            current_folder = None
            folder_qs = f"&sort={sort_key}"

        total_pages = max(1, (total + per_page - 1) // per_page)
        folders = conn.execute("SELECT * FROM folders ORDER BY pinned DESC, name").fetchall()
        return render_template(
            "gallery.html", images=images, folders=folders, current_folder=current_folder,
            view_mode=view_mode, folder_qs=folder_qs, sort_key=sort_key,
            page=page, total_pages=total_pages, total=total,
        )

    # ---------- Prompt builder (Generator) ----------

    def _builder_item_json(row):
        return {"id": row["id"], "text": row["text"]}

    def _builder_data():
        conn = db.get_db()
        categories = conn.execute("SELECT * FROM prompt_categories ORDER BY sort_order, id").fetchall()
        data = []
        for cat in categories:
            items = conn.execute(
                "SELECT id, text FROM prompt_items WHERE category_id = ? ORDER BY sort_order, id",
                (cat["id"],),
            ).fetchall()
            data.append({
                "id": cat["id"], "key": cat["key"], "label": cat["label"],
                "items": [_builder_item_json(i) for i in items],
            })
        return data

    @app.route("/generator")
    @login_required
    def generator():
        return render_template("generator.html", builder_data=_builder_data())

    @app.route("/generator/edit")
    @login_required
    def generator_edit():
        return render_template("generator_edit.html", builder_data=_builder_data())

    @app.route("/api/generator/items", methods=["POST"])
    @login_required
    def api_builder_item_add():
        data = request.get_json(silent=True) or {}
        category_id = data.get("category_id")
        text = (data.get("text") or "").strip()
        conn = db.get_db()
        if not isinstance(category_id, int) or not conn.execute(
                "SELECT 1 FROM prompt_categories WHERE id = ?", (category_id,)).fetchone():
            return jsonify({"error": "Category not found"}), 404
        if not text:
            return jsonify({"error": "Text can't be empty"}), 400
        existing = conn.execute(
            "SELECT id FROM prompt_items WHERE category_id = ? AND lower(text) = lower(?)",
            (category_id, text),
        ).fetchone()
        if existing:
            return jsonify({"error": "That item is already in this category", "id": existing["id"]}), 409
        next_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM prompt_items WHERE category_id = ?",
            (category_id,),
        ).fetchone()["n"]
        cur = conn.execute(
            "INSERT INTO prompt_items (category_id, text, sort_order) VALUES (?,?,?)",
            (category_id, text, next_order),
        )
        conn.commit()
        return jsonify({"id": cur.lastrowid, "text": text})

    @app.route("/api/generator/items/<int:item_id>/update", methods=["POST"])
    @login_required
    def api_builder_item_update(item_id):
        data = request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        conn = db.get_db()
        item = conn.execute("SELECT * FROM prompt_items WHERE id = ?", (item_id,)).fetchone()
        if item is None:
            return jsonify({"error": "Item not found"}), 404
        if not text:
            return jsonify({"error": "Text can't be empty"}), 400
        clash = conn.execute(
            "SELECT id FROM prompt_items WHERE category_id = ? AND lower(text) = lower(?) AND id != ?",
            (item["category_id"], text, item_id),
        ).fetchone()
        if clash:
            return jsonify({"error": "That item is already in this category"}), 409
        conn.execute("UPDATE prompt_items SET text = ? WHERE id = ?", (text, item_id))
        conn.commit()
        return jsonify({"id": item_id, "text": text})

    @app.route("/api/generator/items/<int:item_id>/delete", methods=["POST"])
    @login_required
    def api_builder_item_delete(item_id):
        conn = db.get_db()
        cur = conn.execute("DELETE FROM prompt_items WHERE id = ?", (item_id,))
        conn.commit()
        if cur.rowcount == 0:
            return jsonify({"error": "Item not found"}), 404
        return jsonify({"ok": True})

    @app.route("/api/generator/items/bulk-add", methods=["POST"])
    @login_required
    def api_builder_items_bulk_add():
        """Add many prompts at once from pasted text, one per line. Leading
        list numbers ("12. ") are stripped, blanks and duplicates (including
        ones already in the category, ignoring case) are skipped."""
        data = request.get_json(silent=True) or {}
        category_id = data.get("category_id")
        text = data.get("text")
        conn = db.get_db()
        if not isinstance(category_id, int) or not conn.execute(
                "SELECT 1 FROM prompt_categories WHERE id = ?", (category_id,)).fetchone():
            return jsonify({"error": "Category not found"}), 404
        if not isinstance(text, str) or not text.strip():
            return jsonify({"error": "Paste at least one prompt"}), 400
        if len(text) > 500_000:
            return jsonify({"error": "That's too much text to add at once"}), 400
        lines = prompt_io.dedupe(text.splitlines())
        existing = {r["text"].lower() for r in _existing_items(conn, category_id)}
        fresh = [t for t in lines if t.lower() not in existing]
        start = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM prompt_items WHERE category_id = ?",
            (category_id,),
        ).fetchone()["n"]
        added = []
        for i, t in enumerate(fresh):
            cur = conn.execute(
                "INSERT INTO prompt_items (category_id, text, sort_order) VALUES (?,?,?)",
                (category_id, t, start + i),
            )
            added.append({"id": cur.lastrowid, "text": t})
        conn.commit()
        return jsonify({"added": added, "skipped": len(text.splitlines()) - len(added)})

    @app.route("/api/generator/items/bulk-delete", methods=["POST"])
    @login_required
    def api_builder_items_bulk_delete():
        data = request.get_json(silent=True) or {}
        ids = data.get("ids")
        if (not isinstance(ids, list) or not ids or len(ids) > 5000
                or not all(isinstance(i, int) and not isinstance(i, bool) for i in ids)):
            return jsonify({"error": "Nothing selected"}), 400
        conn = db.get_db()
        deleted = 0
        for start in range(0, len(ids), 500):  # stay under SQLite's variable limit
            chunk = ids[start:start + 500]
            cur = conn.execute(
                "DELETE FROM prompt_items WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk)
            deleted += cur.rowcount
        conn.commit()
        return jsonify({"deleted": deleted})

    # ---------- Prompt builder: Excel import / export ----------

    def _builder_categories():
        return db.get_db().execute(
            "SELECT id, key, label FROM prompt_categories ORDER BY sort_order, id").fetchall()

    def _existing_items(conn, category_id):
        return conn.execute(
            "SELECT text FROM prompt_items WHERE category_id = ? ORDER BY sort_order, id",
            (category_id,),
        ).fetchall()

    @app.route("/generator/export.xlsx")
    @login_required
    def generator_export():
        conn = db.get_db()
        data = [
            {"label": c["label"], "items": [r["text"] for r in _existing_items(conn, c["id"])]}
            for c in _builder_categories()
        ]
        return Response(
            prompt_io.build_workbook(data),
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=nodebook_prompts.xlsx"},
        )

    @app.route("/api/generator/import/preview", methods=["POST"])
    @login_required
    def api_builder_import_preview():
        """Read an uploaded file and report, per category, what an import
        would do. Nothing is saved; the browser sends the items back to
        /api/generator/import with the chosen mode for each category."""
        f = request.files.get("file")
        if f is None or not f.filename:
            return jsonify({"error": "Choose a file to import"}), 400
        categories = _builder_categories()
        try:
            parsed = prompt_io.parse(f.filename, f.read(), categories)
        except prompt_io.ImportError_ as e:
            return jsonify({"error": str(e)}), 400
        conn = db.get_db()
        out = []
        for c in categories:
            items = parsed["categories"].get(c["id"])
            if not items:
                continue
            existing = {r["text"].lower() for r in _existing_items(conn, c["id"])}
            new = [t for t in items if t.lower() not in existing]
            out.append({
                "category_id": c["id"], "label": c["label"], "items": items,
                "in_file": len(items), "new": len(new), "already": len(items) - len(new),
                "current": len(existing),
            })
        return jsonify({"categories": out, "unmatched": parsed["unmatched"]})

    @app.route("/api/generator/import", methods=["POST"])
    @login_required
    def api_builder_import():
        data = request.get_json(silent=True) or {}
        plan = data.get("categories")
        if not isinstance(plan, list) or not plan:
            return jsonify({"error": "Nothing to import"}), 400
        conn = db.get_db()
        valid_ids = {c["id"] for c in _builder_categories()}
        summary = []
        try:
            for entry in plan:
                if not isinstance(entry, dict):
                    continue
                cid, mode = entry.get("category_id"), entry.get("mode")
                if mode == "skip":
                    continue
                if cid not in valid_ids or mode not in ("add", "replace"):
                    raise ValueError("Bad import request")
                items = prompt_io.dedupe(entry.get("items") or [])
                if not items:
                    continue
                if mode == "replace":
                    conn.execute("DELETE FROM prompt_items WHERE category_id = ?", (cid,))
                    existing = set()
                else:
                    existing = {r["text"].lower() for r in _existing_items(conn, cid)}
                to_add = [t for t in items if t.lower() not in existing]
                start = conn.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 AS n FROM prompt_items WHERE category_id = ?",
                    (cid,),
                ).fetchone()["n"]
                conn.executemany(
                    "INSERT INTO prompt_items (category_id, text, sort_order) VALUES (?,?,?)",
                    [(cid, t, start + i) for i, t in enumerate(to_add)],
                )
                summary.append({"category_id": cid, "mode": mode, "added": len(to_add)})
            conn.commit()
        except ValueError as e:
            conn.rollback()
            return jsonify({"error": str(e)}), 400
        return jsonify({"ok": True, "categories": summary})

    # ---------- Upload ----------

    @app.route("/setup")
    @login_required
    def mobile_setup():
        # Live-rendered so it never goes stale: the URL reflects however
        # this request actually reached the server (Tailscale hostname,
        # or anything else later), and the API key is read straight from
        # config - if it's ever rotated, this page updates on its own.
        share_target_url = request.host_url.rstrip("/") + url_for("reference_share_target")
        return render_template(
            "mobile_setup.html",
            share_target_url=share_target_url,
            api_key=app.config.get("API_KEY", ""),
        )

    @app.route("/duplicates")
    @login_required
    def duplicates():
        conn = db.get_db()
        dup_hashes = conn.execute(
            """SELECT file_hash FROM images
               WHERE file_hash IS NOT NULL
               GROUP BY file_hash HAVING COUNT(*) > 1"""
        ).fetchall()

        groups = []
        for row in dup_hashes:
            images = conn.execute(
                "SELECT * FROM images WHERE file_hash = ? ORDER BY uploaded_at ASC, id ASC",
                (row["file_hash"],),
            ).fetchall()
            groups.append(images)

        folders = conn.execute("SELECT * FROM folders ORDER BY pinned DESC, name").fetchall()
        return render_template("duplicates.html", groups=groups, folders=folders)

    @app.route("/upload", methods=["GET", "POST"])
    @login_required
    def upload():
        if request.method == "POST":
            files = request.files.getlist("images")
            saved_at_values = request.form.getlist("saved_at")
            conn = db.get_db()
            saved = 0
            skipped = []
            failed = []
            duplicates = []

            for idx, file in enumerate(files):
                if not file or file.filename == "":
                    continue
                if not allowed_file(file.filename, app):
                    skipped.append(file.filename)
                    continue

                try:
                    file_bytes = file.read()
                    file_hash = hashlib.sha256(file_bytes).hexdigest()
                    file.seek(0)

                    existing = conn.execute(
                        "SELECT id FROM images WHERE file_hash = ?", (file_hash,)
                    ).fetchone()
                    if existing:
                        duplicates.append(file.filename)
                        continue

                    original_name = secure_filename(file.filename)
                    stored_name = f"{uuid.uuid4().hex}_{original_name}"
                    filepath = os.path.join(app.config["UPLOAD_FOLDER"], stored_name)
                    file.save(filepath)

                    meta = extract_metadata(filepath)
                    saved_at = parse_saved_at(saved_at_values[idx]) if idx < len(saved_at_values) else None

                    thumb_name = f"{uuid.uuid4().hex}.jpg"
                    thumb_path = os.path.join(app.config["THUMBNAIL_FOLDER"], thumb_name)
                    try:
                        make_thumbnail(filepath, thumb_path)
                    except Exception:
                        thumb_name = None

                    conn.execute(
                        """INSERT INTO images
                           (filename, original_filename, thumbnail, positive_prompt, negative_prompt,
                            model_name, sampler_name, scheduler, seed, steps, cfg, denoise,
                            width, height, loras, prompt_json, workflow_json, has_metadata, file_hash, saved_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            stored_name, original_name, thumb_name,
                            meta["positive_prompt"], meta["negative_prompt"],
                            meta["model_name"], meta["sampler_name"], meta["scheduler"],
                            str(meta["seed"]) if meta["seed"] is not None else None,
                            meta["steps"], meta["cfg"], meta["denoise"],
                            meta["width"], meta["height"],
                            json.dumps(meta["loras"]) if meta["loras"] else None,
                            meta["prompt_json"], meta["workflow_json"],
                            1 if meta["has_metadata"] else 0,
                            file_hash, saved_at,
                        ),
                    )
                    # Commit per-file rather than once at the end: in a large
                    # batch, one bad file no longer takes the whole upload
                    # down with it, and everything processed so far is
                    # already safely persisted if something later does fail.
                    # This also means a duplicate LATER in the same batch is
                    # correctly caught against a duplicate EARLIER in it.
                    conn.commit()
                    saved += 1
                except Exception:
                    conn.rollback()
                    failed.append(file.filename)

            if saved:
                flash(f"Uploaded {saved} image(s).")
            if duplicates:
                flash(f"Skipped {len(duplicates)} duplicate(s) already in your library: {', '.join(duplicates)}")
            if skipped:
                flash(f"Skipped (not PNG): {', '.join(skipped)}")
            if failed:
                flash(f"Failed to process: {', '.join(failed)} - check the file(s) and try re-uploading just those.")
            return redirect(url_for("gallery"))

        return render_template("upload.html")

    # ---------- Image detail ----------

    @app.route("/image/<int:image_id>")
    @login_required
    def image_detail(image_id):
        conn = db.get_db()
        image = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if not image:
            abort(404)

        # Figure out prev/next within the same context the user arrived from
        # (unfiled, a specific folder, or everything) so browsing stays consistent.
        view_mode, folder_id = parse_view_param(request)
        sort_key, sort_expr, sort_dir = parse_sort_param(request)
        if view_mode == "folder":
            _, sort_expr_joined, _ = parse_sort_param(request, table_prefix="images.")
            ordered_ids = [
                row["id"] for row in conn.execute(
                    f"""SELECT images.id FROM images
                       JOIN image_folders ON image_folders.image_id = images.id
                       WHERE image_folders.folder_id = ?
                       ORDER BY images.is_favorite DESC, {sort_expr_joined} {sort_dir}, images.id {sort_dir}""",
                    (folder_id,),
                ).fetchall()
            ]
            folder_param = str(folder_id)
        elif view_mode == "all":
            ordered_ids = [
                row["id"] for row in
                conn.execute(f"SELECT id FROM images ORDER BY is_favorite DESC, {sort_expr} {sort_dir}, id {sort_dir}").fetchall()
            ]
            folder_param = "all"
        else:
            ordered_ids = [
                row["id"] for row in conn.execute(
                    f"""SELECT id FROM images WHERE id NOT IN (SELECT image_id FROM image_folders)
                       ORDER BY is_favorite DESC, {sort_expr} {sort_dir}, id {sort_dir}"""
                ).fetchall()
            ]
            folder_param = None

        prev_id = next_id = None
        if image_id in ordered_ids:
            idx = ordered_ids.index(image_id)
            # Gallery grid reads newest-first; "next" continues forward through
            # the grid (older), "previous" goes back toward the top (newer).
            if idx > 0:
                prev_id = ordered_ids[idx - 1]
            if idx < len(ordered_ids) - 1:
                next_id = ordered_ids[idx + 1]

        all_folders = conn.execute("SELECT * FROM folders ORDER BY name").fetchall()
        assigned_folder_ids = {
            row["folder_id"] for row in
            conn.execute("SELECT folder_id FROM image_folders WHERE image_id = ?", (image_id,)).fetchall()
        }
        loras = json.loads(image["loras"]) if image["loras"] else []
        return render_template(
            "image_detail.html", image=image, all_folders=all_folders,
            assigned_folder_ids=assigned_folder_ids, loras=loras,
            folder_param=folder_param, sort_key=sort_key, prev_id=prev_id, next_id=next_id,
        )

    @app.route("/image/<int:image_id>/delete", methods=["POST"])
    @login_required
    def delete_image(image_id):
        conn = db.get_db()
        image = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if image:
            for folder, fname in (("UPLOAD_FOLDER", image["filename"]), ("THUMBNAIL_FOLDER", image["thumbnail"])):
                if fname:
                    path = os.path.join(app.config[folder], fname)
                    if os.path.exists(path):
                        os.remove(path)
            conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
            conn.commit()
            flash("Image deleted.")
        return redirect(url_for("gallery"))

    @app.route("/image/<int:image_id>/download-workflow")
    @login_required
    def download_workflow(image_id):
        conn = db.get_db()
        image = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
        if not image:
            abort(404)
        workflow = get_full_workflow_json(image)
        if not workflow:
            abort(404)
        base_name = os.path.splitext(image["original_filename"])[0]
        return Response(
            workflow,
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename={base_name}_workflow.json"},
        )

    @app.route("/image/<int:image_id>/folders", methods=["POST"])
    @login_required
    def update_image_folders(image_id):
        conn = db.get_db()
        selected_ids = request.form.getlist("folder_ids", type=int)
        conn.execute("DELETE FROM image_folders WHERE image_id = ?", (image_id,))
        for fid in selected_ids:
            conn.execute("INSERT INTO image_folders (image_id, folder_id) VALUES (?, ?)", (image_id, fid))
        conn.commit()
        flash("Folders updated.")
        return redirect(url_for("image_detail", image_id=image_id))

    # ---------- Folders ----------

    @app.route("/folders", methods=["GET", "POST"])
    @login_required
    def folders():
        conn = db.get_db()
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            if name:
                try:
                    conn.execute("INSERT INTO folders (name, description) VALUES (?, ?)", (name, description))
                    conn.commit()
                    flash(f'Folder "{name}" created.')
                except Exception:
                    flash(f'A folder named "{name}" already exists.')
            return redirect(url_for("folders"))

        all_folders = conn.execute(
            """SELECT folders.*, COUNT(image_folders.image_id) AS image_count
               FROM folders LEFT JOIN image_folders ON image_folders.folder_id = folders.id
               GROUP BY folders.id ORDER BY folders.pinned DESC, folders.name"""
        ).fetchall()
        return render_template("folders.html", folders=all_folders)

    @app.route("/folders/<int:folder_id>/delete", methods=["POST"])
    @login_required
    def delete_folder(folder_id):
        conn = db.get_db()
        conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        conn.commit()
        flash("Folder deleted.")
        return redirect(url_for("folders"))

    # ---------- Folder / image management API (used by the gallery's inline file-manager UI) ----------

    @app.route("/api/folders", methods=["POST"])
    @login_required
    def api_create_folder():
        conn = db.get_db()
        data = request.get_json(silent=True) or request.form
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Name required"}), 400
        try:
            cur = conn.execute("INSERT INTO folders (name) VALUES (?)", (name,))
            conn.commit()
        except Exception:
            return jsonify({"error": f'A folder named "{name}" already exists.'}), 409
        return jsonify({"id": cur.lastrowid, "name": name, "pinned": 0})

    @app.route("/api/folders/<int:folder_id>/rename", methods=["POST"])
    @login_required
    def api_rename_folder(folder_id):
        conn = db.get_db()
        data = request.get_json(silent=True) or request.form
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Name required"}), 400
        if not conn.execute("SELECT id FROM folders WHERE id = ?", (folder_id,)).fetchone():
            return jsonify({"error": "Folder not found"}), 404
        try:
            conn.execute("UPDATE folders SET name = ? WHERE id = ?", (name, folder_id))
            conn.commit()
        except Exception:
            return jsonify({"error": f'A folder named "{name}" already exists.'}), 409
        return jsonify({"id": folder_id, "name": name})

    @app.route("/api/folders/<int:folder_id>/delete", methods=["POST"])
    @login_required
    def api_delete_folder(folder_id):
        conn = db.get_db()
        conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
        conn.commit()
        return jsonify({"deleted": folder_id})

    @app.route("/api/folders/<int:folder_id>/pin", methods=["POST"])
    @login_required
    def api_pin_folder(folder_id):
        conn = db.get_db()
        row = conn.execute("SELECT pinned FROM folders WHERE id = ?", (folder_id,)).fetchone()
        if not row:
            return jsonify({"error": "Folder not found"}), 404
        new_state = 0 if row["pinned"] else 1
        conn.execute("UPDATE folders SET pinned = ? WHERE id = ?", (new_state, folder_id))
        conn.commit()
        return jsonify({"id": folder_id, "pinned": new_state})

    @app.route("/api/images/move", methods=["POST"])
    @login_required
    def api_move_images():
        conn = db.get_db()
        data = request.get_json(silent=True) or {}
        image_ids = [int(i) for i in data.get("image_ids", []) if str(i).isdigit()]
        folder_id = data.get("folder_id")
        folder_id = int(folder_id) if folder_id not in (None, "", "null") else None

        if not image_ids:
            return jsonify({"error": "No images specified"}), 400
        if folder_id is not None and not conn.execute("SELECT id FROM folders WHERE id = ?", (folder_id,)).fetchone():
            return jsonify({"error": "Folder not found"}), 404

        for image_id in image_ids:
            # "Move" is single-location: clear any existing assignment first,
            # then assign the new one (or leave unassigned if folder_id is None,
            # i.e. moving back to "All images").
            conn.execute("DELETE FROM image_folders WHERE image_id = ?", (image_id,))
            if folder_id is not None:
                conn.execute(
                    "INSERT INTO image_folders (image_id, folder_id) VALUES (?, ?)",
                    (image_id, folder_id),
                )
        conn.commit()
        return jsonify({"moved": len(image_ids), "folder_id": folder_id})

    @app.route("/api/images/delete", methods=["POST"])
    @login_required
    def api_delete_images():
        conn = db.get_db()
        data = request.get_json(silent=True) or {}
        image_ids = [int(i) for i in data.get("image_ids", []) if str(i).isdigit()]
        if not image_ids:
            return jsonify({"error": "No images specified"}), 400

        deleted = 0
        for image_id in image_ids:
            image = conn.execute("SELECT * FROM images WHERE id = ?", (image_id,)).fetchone()
            if not image:
                continue
            for folder_key, fname in (("UPLOAD_FOLDER", image["filename"]), ("THUMBNAIL_FOLDER", image["thumbnail"])):
                if fname:
                    path = os.path.join(app.config[folder_key], fname)
                    if os.path.exists(path):
                        os.remove(path)
            conn.execute("DELETE FROM images WHERE id = ?", (image_id,))
            deleted += 1
        conn.commit()
        return jsonify({"deleted": deleted})

    @app.route("/api/images/<int:image_id>/favorite", methods=["POST"])
    @login_required
    def api_toggle_favorite(image_id):
        conn = db.get_db()
        row = conn.execute("SELECT is_favorite FROM images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            return jsonify({"error": "Image not found"}), 404
        new_state = 0 if row["is_favorite"] else 1
        conn.execute("UPDATE images SET is_favorite = ? WHERE id = ?", (new_state, image_id))
        conn.commit()
        return jsonify({"id": image_id, "is_favorite": new_state})

    # ---------- Reference images (separate collection - no prompt/metadata,
    # accepts jpg/png/gif/webp, and supports renaming) ----------

    @app.route("/reference")
    @login_required
    def reference_gallery():
        conn = db.get_db()
        view_mode, folder_id = parse_view_param(request)
        sort_key, sort_expr, sort_dir = parse_sort_param(request)
        page = max(1, request.args.get("page", 1, type=int))
        per_page = 60
        offset = (page - 1) * per_page

        if view_mode == "folder":
            _, sort_expr_joined, _ = parse_sort_param(request, table_prefix="reference_images.")
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM reference_image_folders WHERE folder_id = ?", (folder_id,)
            ).fetchone()["c"]
            images = conn.execute(
                f"""SELECT reference_images.* FROM reference_images
                   JOIN reference_image_folders ON reference_image_folders.reference_image_id = reference_images.id
                   WHERE reference_image_folders.folder_id = ?
                   ORDER BY reference_images.is_favorite DESC, {sort_expr_joined} {sort_dir}, reference_images.id {sort_dir}
                   LIMIT ? OFFSET ?""",
                (folder_id, per_page, offset),
            ).fetchall()
            current_folder = conn.execute("SELECT * FROM reference_folders WHERE id = ?", (folder_id,)).fetchone()
            folder_qs = f"&folder={folder_id}&sort={sort_key}"
        elif view_mode == "all":
            total = conn.execute("SELECT COUNT(*) AS c FROM reference_images").fetchone()["c"]
            images = conn.execute(
                f"SELECT * FROM reference_images ORDER BY is_favorite DESC, {sort_expr} {sort_dir}, id {sort_dir} LIMIT ? OFFSET ?",
                (per_page, offset),
            ).fetchall()
            current_folder = None
            folder_qs = f"&folder=all&sort={sort_key}"
        else:
            total = conn.execute(
                """SELECT COUNT(*) AS c FROM reference_images
                   WHERE id NOT IN (SELECT reference_image_id FROM reference_image_folders)"""
            ).fetchone()["c"]
            images = conn.execute(
                f"""SELECT * FROM reference_images WHERE id NOT IN (SELECT reference_image_id FROM reference_image_folders)
                   ORDER BY is_favorite DESC, {sort_expr} {sort_dir}, id {sort_dir} LIMIT ? OFFSET ?""",
                (per_page, offset),
            ).fetchall()
            current_folder = None
            folder_qs = f"&sort={sort_key}"

        total_pages = max(1, (total + per_page - 1) // per_page)
        folders = conn.execute("SELECT * FROM reference_folders ORDER BY pinned DESC, name").fetchall()
        return render_template(
            "reference_gallery.html", images=images, folders=folders, current_folder=current_folder,
            view_mode=view_mode, folder_qs=folder_qs, sort_key=sort_key,
            page=page, total_pages=total_pages, total=total,
        )

    @app.route("/reference/upload", methods=["GET", "POST"])
    @login_required
    def reference_upload():
        if request.method == "POST":
            process_reference_uploads(
                request.files.getlist("images"), app, request.form.getlist("saved_at")
            )
            return redirect(url_for("reference_gallery"))

        return render_template("reference_upload.html")

    @app.route("/reference/share-target", methods=["POST"])
    @api_key_or_login_required
    def reference_share_target():
        # Destination for the OS "Share to..." sheet, registered via
        # static/manifest.json's share_target (Android/Chrome only - iOS
        # Safari doesn't implement the Web Share Target API), AND for
        # non-browser clients like HTTP Shortcuts that authenticate with
        # the X-API-Key header instead of a session cookie.
        summary = process_reference_uploads(request.files.getlist("images"), app)
        if not session.get("logged_in"):
            # Authenticated via API key, not a real browser session - a
            # redirect to a login-gated HTML page is useless to a tool like
            # HTTP Shortcuts, so give it a clean JSON result instead.
            return jsonify(summary)
        return redirect(url_for("reference_gallery"))

    @app.route("/reference/image/<int:image_id>")
    @login_required
    def reference_image_detail(image_id):
        conn = db.get_db()
        image = conn.execute("SELECT * FROM reference_images WHERE id = ?", (image_id,)).fetchone()
        if not image:
            abort(404)

        view_mode, folder_id = parse_view_param(request)
        sort_key, sort_expr, sort_dir = parse_sort_param(request)
        if view_mode == "folder":
            _, sort_expr_joined, _ = parse_sort_param(request, table_prefix="reference_images.")
            ordered_ids = [
                row["id"] for row in conn.execute(
                    f"""SELECT reference_images.id FROM reference_images
                       JOIN reference_image_folders ON reference_image_folders.reference_image_id = reference_images.id
                       WHERE reference_image_folders.folder_id = ?
                       ORDER BY reference_images.is_favorite DESC, {sort_expr_joined} {sort_dir}, reference_images.id {sort_dir}""",
                    (folder_id,),
                ).fetchall()
            ]
            folder_param = str(folder_id)
        elif view_mode == "all":
            ordered_ids = [
                row["id"] for row in conn.execute(
                    f"SELECT id FROM reference_images ORDER BY is_favorite DESC, {sort_expr} {sort_dir}, id {sort_dir}"
                ).fetchall()
            ]
            folder_param = "all"
        else:
            ordered_ids = [
                row["id"] for row in conn.execute(
                    f"""SELECT id FROM reference_images WHERE id NOT IN (SELECT reference_image_id FROM reference_image_folders)
                       ORDER BY is_favorite DESC, {sort_expr} {sort_dir}, id {sort_dir}"""
                ).fetchall()
            ]
            folder_param = None

        prev_id = next_id = None
        if image_id in ordered_ids:
            idx = ordered_ids.index(image_id)
            if idx > 0:
                prev_id = ordered_ids[idx - 1]
            if idx < len(ordered_ids) - 1:
                next_id = ordered_ids[idx + 1]

        all_folders = conn.execute("SELECT * FROM reference_folders ORDER BY name").fetchall()
        assigned_folder_ids = {
            row["folder_id"] for row in
            conn.execute(
                "SELECT folder_id FROM reference_image_folders WHERE reference_image_id = ?", (image_id,)
            ).fetchall()
        }
        return render_template(
            "reference_image_detail.html", image=image, all_folders=all_folders,
            assigned_folder_ids=assigned_folder_ids, sort_key=sort_key,
            folder_param=folder_param, prev_id=prev_id, next_id=next_id,
        )

    @app.route("/reference/image/<int:image_id>/delete", methods=["POST"])
    @login_required
    def reference_delete_image(image_id):
        conn = db.get_db()
        image = conn.execute("SELECT * FROM reference_images WHERE id = ?", (image_id,)).fetchone()
        if image:
            for folder_key, fname in (("REFERENCE_UPLOAD_FOLDER", image["filename"]), ("REFERENCE_THUMBNAIL_FOLDER", image["thumbnail"])):
                if fname:
                    path = os.path.join(app.config[folder_key], fname)
                    if os.path.exists(path):
                        os.remove(path)
            conn.execute("DELETE FROM reference_images WHERE id = ?", (image_id,))
            conn.commit()
            flash("Image deleted.")
        return redirect(url_for("reference_gallery"))

    @app.route("/reference/image/<int:image_id>/folders", methods=["POST"])
    @login_required
    def reference_update_image_folders(image_id):
        conn = db.get_db()
        selected_ids = request.form.getlist("folder_ids", type=int)
        conn.execute("DELETE FROM reference_image_folders WHERE reference_image_id = ?", (image_id,))
        for fid in selected_ids:
            conn.execute(
                "INSERT INTO reference_image_folders (reference_image_id, folder_id) VALUES (?, ?)", (image_id, fid)
            )
        conn.commit()
        flash("Folder updated.")
        return redirect(url_for("reference_image_detail", image_id=image_id))

    @app.route("/reference/image/<int:image_id>/rename", methods=["POST"])
    @login_required
    def reference_rename_image(image_id):
        conn = db.get_db()
        new_name = request.form.get("display_name", "").strip()
        conn.execute("UPDATE reference_images SET display_name = ? WHERE id = ?", (new_name or None, image_id))
        conn.commit()
        flash("Renamed.")
        return redirect(url_for("reference_image_detail", image_id=image_id))

    @app.route("/reference/folders", methods=["GET", "POST"])
    @login_required
    def reference_folders():
        conn = db.get_db()
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            description = request.form.get("description", "").strip()
            if name:
                try:
                    conn.execute("INSERT INTO reference_folders (name, description) VALUES (?, ?)", (name, description))
                    conn.commit()
                    flash(f'Folder "{name}" created.')
                except Exception:
                    flash(f'A folder named "{name}" already exists.')
            return redirect(url_for("reference_folders"))

        all_folders = conn.execute(
            """SELECT reference_folders.*, COUNT(reference_image_folders.reference_image_id) AS image_count
               FROM reference_folders LEFT JOIN reference_image_folders ON reference_image_folders.folder_id = reference_folders.id
               GROUP BY reference_folders.id ORDER BY reference_folders.pinned DESC, reference_folders.name"""
        ).fetchall()
        return render_template("reference_folders.html", folders=all_folders)

    @app.route("/reference/folders/<int:folder_id>/delete", methods=["POST"])
    @login_required
    def reference_delete_folder(folder_id):
        conn = db.get_db()
        conn.execute("DELETE FROM reference_folders WHERE id = ?", (folder_id,))
        conn.commit()
        flash("Folder deleted.")
        return redirect(url_for("reference_folders"))

    # ---------- Reference API (same shapes as the AI-gallery API, targeting
    # the reference_* tables instead) ----------

    @app.route("/api/reference/folders", methods=["POST"])
    @login_required
    def api_create_reference_folder():
        conn = db.get_db()
        data = request.get_json(silent=True) or request.form
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Name required"}), 400
        try:
            cur = conn.execute("INSERT INTO reference_folders (name) VALUES (?)", (name,))
            conn.commit()
        except Exception:
            return jsonify({"error": f'A folder named "{name}" already exists.'}), 409
        return jsonify({"id": cur.lastrowid, "name": name, "pinned": 0})

    @app.route("/api/reference/folders/<int:folder_id>/rename", methods=["POST"])
    @login_required
    def api_rename_reference_folder(folder_id):
        conn = db.get_db()
        data = request.get_json(silent=True) or request.form
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "Name required"}), 400
        if not conn.execute("SELECT id FROM reference_folders WHERE id = ?", (folder_id,)).fetchone():
            return jsonify({"error": "Folder not found"}), 404
        try:
            conn.execute("UPDATE reference_folders SET name = ? WHERE id = ?", (name, folder_id))
            conn.commit()
        except Exception:
            return jsonify({"error": f'A folder named "{name}" already exists.'}), 409
        return jsonify({"id": folder_id, "name": name})

    @app.route("/api/reference/folders/<int:folder_id>/delete", methods=["POST"])
    @login_required
    def api_delete_reference_folder(folder_id):
        conn = db.get_db()
        conn.execute("DELETE FROM reference_folders WHERE id = ?", (folder_id,))
        conn.commit()
        return jsonify({"deleted": folder_id})

    @app.route("/api/reference/folders/<int:folder_id>/pin", methods=["POST"])
    @login_required
    def api_pin_reference_folder(folder_id):
        conn = db.get_db()
        row = conn.execute("SELECT pinned FROM reference_folders WHERE id = ?", (folder_id,)).fetchone()
        if not row:
            return jsonify({"error": "Folder not found"}), 404
        new_state = 0 if row["pinned"] else 1
        conn.execute("UPDATE reference_folders SET pinned = ? WHERE id = ?", (new_state, folder_id))
        conn.commit()
        return jsonify({"id": folder_id, "pinned": new_state})

    @app.route("/api/reference/images/move", methods=["POST"])
    @login_required
    def api_move_reference_images():
        conn = db.get_db()
        data = request.get_json(silent=True) or {}
        image_ids = [int(i) for i in data.get("image_ids", []) if str(i).isdigit()]
        folder_id = data.get("folder_id")
        folder_id = int(folder_id) if folder_id not in (None, "", "null") else None

        if not image_ids:
            return jsonify({"error": "No images specified"}), 400
        if folder_id is not None and not conn.execute("SELECT id FROM reference_folders WHERE id = ?", (folder_id,)).fetchone():
            return jsonify({"error": "Folder not found"}), 404

        for image_id in image_ids:
            conn.execute("DELETE FROM reference_image_folders WHERE reference_image_id = ?", (image_id,))
            if folder_id is not None:
                conn.execute(
                    "INSERT INTO reference_image_folders (reference_image_id, folder_id) VALUES (?, ?)",
                    (image_id, folder_id),
                )
        conn.commit()
        return jsonify({"moved": len(image_ids), "folder_id": folder_id})

    @app.route("/api/reference/images/delete", methods=["POST"])
    @login_required
    def api_delete_reference_images():
        conn = db.get_db()
        data = request.get_json(silent=True) or {}
        image_ids = [int(i) for i in data.get("image_ids", []) if str(i).isdigit()]
        if not image_ids:
            return jsonify({"error": "No images specified"}), 400

        deleted = 0
        for image_id in image_ids:
            image = conn.execute("SELECT * FROM reference_images WHERE id = ?", (image_id,)).fetchone()
            if not image:
                continue
            for folder_key, fname in (("REFERENCE_UPLOAD_FOLDER", image["filename"]), ("REFERENCE_THUMBNAIL_FOLDER", image["thumbnail"])):
                if fname:
                    path = os.path.join(app.config[folder_key], fname)
                    if os.path.exists(path):
                        os.remove(path)
            conn.execute("DELETE FROM reference_images WHERE id = ?", (image_id,))
            deleted += 1
        conn.commit()
        return jsonify({"deleted": deleted})

    @app.route("/api/reference/images/<int:image_id>/favorite", methods=["POST"])
    @login_required
    def api_toggle_reference_favorite(image_id):
        conn = db.get_db()
        row = conn.execute("SELECT is_favorite FROM reference_images WHERE id = ?", (image_id,)).fetchone()
        if not row:
            return jsonify({"error": "Image not found"}), 404
        new_state = 0 if row["is_favorite"] else 1
        conn.execute("UPDATE reference_images SET is_favorite = ? WHERE id = ?", (new_state, image_id))
        conn.commit()
        return jsonify({"id": image_id, "is_favorite": new_state})

    @app.route("/api/reference/images/<int:image_id>/rename", methods=["POST"])
    @login_required
    def api_rename_reference_image(image_id):
        conn = db.get_db()
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        if not conn.execute("SELECT id FROM reference_images WHERE id = ?", (image_id,)).fetchone():
            return jsonify({"error": "Image not found"}), 404
        conn.execute("UPDATE reference_images SET display_name = ? WHERE id = ?", (name or None, image_id))
        conn.commit()
        return jsonify({"id": image_id, "display_name": name or None})

    # ---------- File serving ----------

    @app.route("/media/uploads/<path:filename>")
    @login_required
    def serve_upload(filename):
        return send_from_directory(app.config["UPLOAD_FOLDER"], filename)

    @app.route("/media/thumbnails/<path:filename>")
    @login_required
    def serve_thumbnail(filename):
        return send_from_directory(app.config["THUMBNAIL_FOLDER"], filename)

    @app.route("/reference/media/uploads/<path:filename>")
    @login_required
    def serve_reference_upload(filename):
        return send_from_directory(app.config["REFERENCE_UPLOAD_FOLDER"], filename)

    @app.route("/reference/media/thumbnails/<path:filename>")
    @login_required
    def serve_reference_thumbnail(filename):
        return send_from_directory(app.config["REFERENCE_THUMBNAIL_FOLDER"], filename)

    # ---------- Notes: simple text notes, independent of the galleries ----------

    @app.route("/notes")
    @login_required
    def notes():
        conn = db.get_db()
        all_notes = conn.execute(
            "SELECT * FROM notes ORDER BY pinned DESC, updated_at DESC"
        ).fetchall()
        return render_template("notes.html", notes=all_notes)

    @app.route("/notes/new", methods=["POST"])
    @login_required
    def notes_new():
        conn = db.get_db()
        cur = conn.execute("INSERT INTO notes (title, content) VALUES (NULL, '')")
        conn.commit()
        return redirect(url_for("note_detail", note_id=cur.lastrowid))

    @app.route("/notes/<int:note_id>")
    @login_required
    def note_detail(note_id):
        conn = db.get_db()
        note = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if note is None:
            abort(404)
        return render_template("note_detail.html", note=note)

    @app.route("/api/notes/<int:note_id>/update", methods=["POST"])
    @login_required
    def api_update_note(note_id):
        data = request.get_json(silent=True) or {}
        title = (data.get("title") or "").strip()
        content = data.get("content") or ""
        conn = db.get_db()
        if not conn.execute("SELECT id FROM notes WHERE id = ?", (note_id,)).fetchone():
            return jsonify({"error": "Note not found"}), 404
        conn.execute(
            "UPDATE notes SET title = ?, content = ?, updated_at = datetime('now') WHERE id = ?",
            (title or None, content, note_id),
        )
        conn.commit()
        return jsonify({"ok": True})

    @app.route("/api/notes/<int:note_id>/duplicate", methods=["POST"])
    @login_required
    def api_duplicate_note(note_id):
        conn = db.get_db()
        note = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
        if note is None:
            return jsonify({"error": "Note not found"}), 404
        new_title = f"{note['title']} (copy)" if note["title"] else None
        cur = conn.execute(
            "INSERT INTO notes (title, content) VALUES (?, ?)",
            (new_title, note["content"]),
        )
        conn.commit()
        return jsonify({"id": cur.lastrowid})

    @app.route("/api/notes/<int:note_id>/pin", methods=["POST"])
    @login_required
    def api_pin_note(note_id):
        conn = db.get_db()
        row = conn.execute("SELECT pinned FROM notes WHERE id = ?", (note_id,)).fetchone()
        if not row:
            return jsonify({"error": "Note not found"}), 404
        new_state = 0 if row["pinned"] else 1
        conn.execute("UPDATE notes SET pinned = ? WHERE id = ?", (new_state, note_id))
        conn.commit()
        return jsonify({"id": note_id, "pinned": new_state})

    @app.route("/api/notes/<int:note_id>/delete", methods=["POST"])
    @login_required
    def api_delete_note(note_id):
        conn = db.get_db()
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()
        return jsonify({"deleted": note_id})

    @app.route("/service-worker.js")
    def service_worker():
        # Served from the site root (not /static/) specifically so its
        # default scope covers the whole app, not just /static/ - a service
        # worker can only control paths at or below where it's served from.
        response = send_from_directory(app.static_folder, "service-worker.js")
        response.headers["Content-Type"] = "application/javascript"
        return response


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
