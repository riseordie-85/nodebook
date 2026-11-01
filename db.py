import hashlib
import sqlite3
import click
from flask import current_app, g


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(
            current_app.config["DATABASE"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = get_db()
    with current_app.open_resource("schema.sql") as f:
        db.executescript(f.read().decode("utf8"))


def hash_file(filepath):
    """SHA-256 of a file's raw bytes, read in chunks so large images don't
    need to be loaded into memory all at once."""
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


def migrate():
    """Applies schema changes that CREATE TABLE IF NOT EXISTS can't handle -
    new columns on a table that already exists. Safe to run on every
    startup: each change checks first and only applies if missing. Kept
    schema-only and fast on purpose - no file I/O here, since this runs
    synchronously at boot for every gunicorn worker."""
    conn = get_db()
    folder_columns = {row["name"] for row in conn.execute("PRAGMA table_info(folders)").fetchall()}
    if "pinned" not in folder_columns:
        conn.execute("ALTER TABLE folders ADD COLUMN pinned INTEGER DEFAULT 0")
        conn.commit()

    image_columns = {row["name"] for row in conn.execute("PRAGMA table_info(images)").fetchall()}
    if "is_favorite" not in image_columns:
        conn.execute("ALTER TABLE images ADD COLUMN is_favorite INTEGER DEFAULT 0")
        conn.commit()

    if "file_hash" not in image_columns:
        conn.execute("ALTER TABLE images ADD COLUMN file_hash TEXT")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_images_file_hash ON images(file_hash)")
    conn.commit()

    if "saved_at" not in image_columns:
        conn.execute("ALTER TABLE images ADD COLUMN saved_at TEXT")
        conn.commit()

    reference_columns = {row["name"] for row in conn.execute("PRAGMA table_info(reference_images)").fetchall()}
    if "saved_at" not in reference_columns:
        conn.execute("ALTER TABLE reference_images ADD COLUMN saved_at TEXT")
        conn.commit()


@click.command("init-db")
def init_db_command():
    """Create tables if they don't already exist."""
    init_db()
    click.echo("Database initialized.")


def init_app(app):
    app.teardown_appcontext(close_db)
    app.cli.add_command(init_db_command)
