"""
Computes and stores the file_hash for every image already in the database
that doesn't have one yet - i.e. anything uploaded before duplicate
detection existed. Reads each file directly off disk; no re-upload needed.

Deliberately NOT run automatically at app startup: with a large library,
hashing every file synchronously at boot can take long enough to make
gunicorn think the worker never came up, which is exactly what happened
the first time this shipped. Run it once, manually, instead:

    docker compose exec prompt-log python3 backfill_hashes.py
"""
import os
import sqlite3

from config import Config
from db import hash_file


def main():
    conn = sqlite3.connect(Config.DATABASE)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("SELECT id, filename, original_filename FROM images WHERE file_hash IS NULL").fetchall()
    print(f"Found {len(rows)} image(s) without a stored hash.")

    updated = 0
    missing = 0
    for row in rows:
        filepath = os.path.join(Config.UPLOAD_FOLDER, row["filename"])
        if not os.path.exists(filepath):
            print(f"  [{row['id']}] {row['original_filename']}: file missing on disk, skipping")
            missing += 1
            continue
        try:
            file_hash = hash_file(filepath)
        except Exception as e:
            print(f"  [{row['id']}] {row['original_filename']}: couldn't hash ({e}), skipping")
            continue

        conn.execute("UPDATE images SET file_hash = ? WHERE id = ?", (file_hash, row["id"]))
        updated += 1
        if updated % 25 == 0:
            conn.commit()
            print(f"  ...{updated} done so far")

    conn.commit()
    conn.close()
    print(f"Done. Hashed {updated} image(s), {missing} file(s) missing on disk.")
    print("Visit /duplicates to review anything that turned out to be a duplicate.")


if __name__ == "__main__":
    main()
