# Nodebook

A personal ComfyUI prompt/image tracker: upload PNGs, and it reads the embedded
ComfyUI workflow metadata automatically - positive/negative prompts, sampler
settings, model, LoRAs - plus lets you copy or download the full workflow
JSON to reload in ComfyUI later. Images can be tagged into multiple folders
(by character, theme, etc).

## Structure
- `app.py` - routes and app setup
- `workflow_parser.py` - reads ComfyUI metadata out of PNG files
- `db.py` / `schema.sql` - SQLite storage
- `templates/`, `static/` - the UI
- `Dockerfile`, `docker-compose.yml` - deployment (built for CasaOS)

## Local setup (before deploying)
```
pip install -r requirements.txt
python3 generate_password.py        # creates your login password hash
cp .env.example .env                # paste the hash + a random SECRET_KEY in here
export $(cat .env | xargs)          # or use docker-compose, which reads .env automatically
flask --app app run --debug
```

Deployment to CasaOS is the next step - this is ready to hand off for that.

## Backups
`backup_to_drive.sh` takes a safe snapshot of the live database (via SQLite's
`VACUUM INTO`, which works correctly even while the app has the file open)
plus `.env`, and copies that and all four media folders to Google Drive via
rclone. It runs on the NUC HOST, not inside the container - it reads the
same bind-mounted `data/` folder the container itself uses.

One-time setup on the NUC:
1. Install rclone: `curl https://rclone.org/install.sh | sudo bash`
2. `rclone config` → `n` (new remote) → name it `gdrive` → type `drive` →
   follow the prompts (it'll open a browser link to authorize your Google
   account) → accept the defaults for everything else.
3. Make sure `sqlite3` is installed: `sudo apt-get install -y sqlite3`
4. Make the script executable: `chmod +x backup_to_drive.sh`
5. Test it once by hand: `./backup_to_drive.sh` then check `backup.log` and
   confirm a `NodebookBackup` folder shows up in Google Drive.
6. Schedule it nightly: `crontab -e`, then add:
   `0 3 * * * /mnt/ext-storage/AI/comfyui-tracker/backup_to_drive.sh`

It uses `rclone copy`, not `sync` - on purpose, so deleting something on the
NUC never deletes it from the backup too. Database snapshots are kept dated
(`tracker-YYYY-MM-DD.db`) rather than overwritten, so a few days of history
survive even if a bad write goes unnoticed for a bit.

**To restore:** stop the container (`docker compose down`), copy the files
back from the `NodebookBackup` folder in Drive into their matching `data/`
subfolders (the database snapshot goes to `data/instance/tracker.db`), then
`docker compose up -d` again.
