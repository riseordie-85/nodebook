-- ComfyUI Prompt Tracker schema

CREATE TABLE IF NOT EXISTS images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,           -- stored filename on disk (uploads/)
    original_filename TEXT NOT NULL,  -- filename as uploaded
    thumbnail TEXT,                   -- stored filename on disk (thumbnails/)
    positive_prompt TEXT,
    negative_prompt TEXT,
    model_name TEXT,
    sampler_name TEXT,
    scheduler TEXT,
    seed TEXT,
    steps INTEGER,
    cfg REAL,
    denoise REAL,
    width INTEGER,
    height INTEGER,
    loras TEXT,                       -- JSON array of {name, strength_model, strength_clip}
    prompt_json TEXT,                 -- raw "prompt" (API format) chunk, as-is
    workflow_json TEXT,               -- raw "workflow" (UI graph format) chunk, as-is
    has_metadata INTEGER DEFAULT 0,   -- 0 if no ComfyUI metadata was found in the PNG
    is_favorite INTEGER DEFAULT 0,
    file_hash TEXT,                   -- SHA-256 of the raw file bytes, for duplicate detection
    saved_at TEXT,                    -- the file's own last-modified time (captured client-side at
                                       -- upload, when available) - distinct from uploaded_at, which is
                                       -- just whenever it happened to reach Nodebook. NULL when the
                                       -- upload path can't supply it (e.g. the API-key path used by
                                       -- HTTP Shortcuts/Apple Shortcuts), in which case sorting by
                                       -- "saved" falls back to uploaded_at for that row.
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The idx_images_file_hash index is created in db.py's migrate() instead of
-- here, specifically because it must run AFTER the ALTER TABLE that adds
-- the file_hash column on a pre-existing database - this file's CREATE
-- TABLE is a no-op there, so an index statement here would reference a
-- column that doesn't exist yet and crash init_db() before migrate() ever
-- gets a chance to add it. See the actual index creation in db.py.

CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    pinned INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS image_folders (
    image_id INTEGER NOT NULL REFERENCES images(id) ON DELETE CASCADE,
    folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
    PRIMARY KEY (image_id, folder_id)
);

CREATE INDEX IF NOT EXISTS idx_image_folders_folder ON image_folders(folder_id);
CREATE INDEX IF NOT EXISTS idx_image_folders_image ON image_folders(image_id);

-- Prompt builder (the Generator page): categories, each with an editable
-- list of items. Seeded once by seed_prompt_builder.py, then edited in the UI.
-- (Older databases may still hold the previous generator_* tables from the
-- retired random generator; nothing reads them any more and they're harmless.)
CREATE TABLE IF NOT EXISTS prompt_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS prompt_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL REFERENCES prompt_categories(id) ON DELETE CASCADE,
    text TEXT NOT NULL,
    sort_order INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_prompt_items_category ON prompt_items(category_id);

-- Reference images: a separate collection from AI generations - no prompt/
-- metadata fields at all, but a user-editable display_name (renaming isn't
-- a thing for the AI gallery, since original_filename already served that
-- role there, but a reference image's original filename is often useless).
CREATE TABLE IF NOT EXISTS reference_images (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    display_name TEXT,
    thumbnail TEXT,
    is_favorite INTEGER DEFAULT 0,
    file_hash TEXT,
    saved_at TEXT,                    -- same meaning as images.saved_at - see the comment there
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_reference_images_file_hash ON reference_images(file_hash);

CREATE TABLE IF NOT EXISTS reference_folders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    pinned INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reference_image_folders (
    reference_image_id INTEGER NOT NULL REFERENCES reference_images(id) ON DELETE CASCADE,
    folder_id INTEGER NOT NULL REFERENCES reference_folders(id) ON DELETE CASCADE,
    PRIMARY KEY (reference_image_id, folder_id)
);

CREATE INDEX IF NOT EXISTS idx_reference_image_folders_folder ON reference_image_folders(folder_id);
CREATE INDEX IF NOT EXISTS idx_reference_image_folders_image ON reference_image_folders(reference_image_id);

-- Notes: simple free-form text notes (title + body), independent of the
-- image galleries entirely - for jotting down art ideas, similar to Apple
-- Notes or Google Keep. Flat list, no folders of their own - pinned notes
-- just sort to the top.
CREATE TABLE IF NOT EXISTS notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    content TEXT NOT NULL DEFAULT '',
    pinned INTEGER DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_notes_pinned ON notes(pinned);

