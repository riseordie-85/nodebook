"""
Re-parses images already in the database using the current workflow_parser
logic, so existing images pick up a parser fix without being re-uploaded.

Two passes:
  1. Images with a stored API-format "prompt" graph are re-parsed from that
     stored JSON (no file access needed).
  2. Images with NO prompt yet and no stored graph (e.g. ones saved by a node
     that only writes an A1111-style "parameters" text block, which isn't
     kept in the database) are re-read from the original PNG on disk. This
     pass only ever fills in images whose prompt is still empty, so it can't
     overwrite anything already there.

Usage (from inside the container):
    docker compose exec prompt-log python3 reprocess.py
"""
import json
import os
import sqlite3

from config import Config
from workflow_parser import extract_metadata, parse_prompt_graph


def main():
    conn = sqlite3.connect(Config.DATABASE)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("SELECT * FROM images WHERE prompt_json IS NOT NULL").fetchall()
    print(f"Found {len(rows)} image(s) with stored workflow data.")

    updated = 0
    for row in rows:
        try:
            graph = json.loads(row["prompt_json"])
        except (json.JSONDecodeError, TypeError):
            print(f"  [{row['id']}] {row['original_filename']}: couldn't parse stored JSON, skipping")
            continue

        extracted = parse_prompt_graph(graph)

        conn.execute(
            """UPDATE images SET
                 positive_prompt=?, negative_prompt=?, model_name=?, sampler_name=?,
                 scheduler=?, seed=?, steps=?, cfg=?, denoise=?, width=?, height=?, loras=?
               WHERE id=?""",
            (
                extracted["positive_prompt"], extracted["negative_prompt"],
                extracted["model_name"], extracted["sampler_name"], extracted["scheduler"],
                str(extracted["seed"]) if extracted["seed"] is not None else None,
                extracted["steps"], extracted["cfg"], extracted["denoise"],
                extracted["width"], extracted["height"],
                json.dumps(extracted["loras"]) if extracted["loras"] else None,
                row["id"],
            ),
        )
        updated += 1
        print(f"  [{row['id']}] {row['original_filename']}: reprocessed "
              f"(positive: {'found' if extracted['positive_prompt'] else 'still missing'})")

    # Pass 2: re-read the PNG itself for images that never got a prompt.
    blank_rows = conn.execute(
        "SELECT * FROM images WHERE prompt_json IS NULL AND positive_prompt IS NULL"
    ).fetchall()
    print(f"Found {len(blank_rows)} image(s) with no prompt and no stored graph - checking the files.")

    filled = 0
    for row in blank_rows:
        path = os.path.join(Config.UPLOAD_FOLDER, row["filename"])
        if not os.path.isfile(path):
            print(f"  [{row['id']}] {row['original_filename']}: file not found on disk, skipping")
            continue

        meta = extract_metadata(path)
        if meta["positive_prompt"] is None and meta["model_name"] is None:
            continue  # nothing new to learn from this file

        conn.execute(
            """UPDATE images SET
                 positive_prompt=?, negative_prompt=?, model_name=?, sampler_name=?,
                 scheduler=?, seed=?, steps=?, cfg=?, denoise=?, width=?, height=?, loras=?,
                 has_metadata=1
               WHERE id=?""",
            (
                meta["positive_prompt"], meta["negative_prompt"],
                meta["model_name"], meta["sampler_name"], meta["scheduler"],
                str(meta["seed"]) if meta["seed"] is not None else None,
                meta["steps"], meta["cfg"], meta["denoise"],
                meta["width"], meta["height"],
                json.dumps(meta["loras"]) if meta["loras"] else None,
                row["id"],
            ),
        )
        filled += 1
        print(f"  [{row['id']}] {row['original_filename']}: filled in from the file")

    conn.commit()
    conn.close()
    print(f"Done. Re-parsed {updated} stored graph(s), filled in {filled} image(s) from their files.")


if __name__ == "__main__":
    main()
