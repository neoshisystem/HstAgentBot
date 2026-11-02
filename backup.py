import os
import sys
import time
import tarfile
import sqlite3
import shutil
import hashlib
from datetime import datetime, timezone

from config import DB_PATH, BACKUP_DIR, BACKUPS_TOPIC_ID

sys.path.append("/root/.hermes/scripts")
import backup_and_report

def run_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    temp_dir = f"/tmp/hst_bot_backup_{timestamp}"
    os.makedirs(temp_dir, exist_ok=True)
    
    try:
        # 1. Safe SQLite live backup
        temp_db = os.path.join(temp_dir, "agent_bot.db")
        if os.path.exists(DB_PATH):
            src = sqlite3.connect(DB_PATH)
            dst = sqlite3.connect(temp_db)
            with dst:
                src.backup(dst)
            dst.close()
            src.close()
        
        # 2. Copy all source codes, schema and registries
        code_dir = os.path.dirname(os.path.abspath(__file__))
        for item in sorted(os.listdir(code_dir)):
            if item.endswith(".py"):
                src_f = os.path.join(code_dir, item)
                shutil.copy2(src_f, temp_dir)
                
        # Copy data registry if present
        reg_p = os.path.join(code_dir, "data", "docs_registry.json")
        if os.path.exists(reg_p):
            shutil.copy2(reg_p, temp_dir)
                
        # 3. Create compressed archive
        archive_path = os.path.join(BACKUP_DIR, f"agent_bot_backup_{timestamp}.tar.gz")
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(temp_dir, arcname=f"backup_{timestamp}")
            
        size_kb = round(os.path.getsize(archive_path) / 1024, 2)
        print(f"✅ Backup created: {archive_path} ({size_kb} KB)")
        
        # Calculate SHA256
        hasher = hashlib.sha256()
        with open(archive_path, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        sha256_hash = hasher.hexdigest()
        
        # 4. Upload to configured backup topic
        caption = (
            f"📦 <b>بکاپ کامل دیتابیس و تنظیمات ربات (@HstAgent_bot)</b>\n\n"
            f"🏷 #Backup #HstAgentBot #3XUI #SQLite #D{datetime.now().strftime('%Y%m%d')}\n\n"
            f"📅 تاریخ: <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC</code>\n"
            f"📦 حجم آرشیو: <code>{size_kb} KB</code>\n"
            f"🔑 هش SHA256:\n<code>{sha256_hash}</code>\n\n"
            f"📁 <b>محتویات:</b> دیتابیس لایو (agent_bot.db)، تنظیمات مسیریابی (config.py)، ساختار دیتابیس و اسکیما (database.py)، موتور الگوها (nlp_matcher.py) و رجیستری مستندات.\n\n"
            f"🔄 <b>دستور استخراج:</b>\n"
            f"<pre>tar -xzf {os.path.basename(archive_path)} -C ./</pre>"
        )
        try:
            print(f"Uploading backup to Topic {BACKUPS_TOPIC_ID}...")
            upload_res = backup_and_report.send_telegram_document(BACKUPS_TOPIC_ID, archive_path, caption, parse_mode="HTML")
            print("Telegram upload result:", upload_res.get("ok"))
        except Exception as e:
            print(f"Failed to upload backup to Telegram: {e}")
        
        # 5. Retention policy: Keep last 30 backups, prune older
        all_backups = []
        for fname in os.listdir(BACKUP_DIR):
            if fname.startswith("agent_bot_backup_") and fname.endswith(".tar.gz"):
                full_p = os.path.join(BACKUP_DIR, fname)
                all_backups.append((os.path.getmtime(full_p), full_p))
                
        all_backups.sort(key=lambda x: x[0], reverse=True)
        if len(all_backups) > 30:
            for _, old_file in all_backups[30:]:
                try:
                    os.remove(old_file)
                    print(f"🗑 Pruned old backup: {old_file}")
                except Exception as e:
                    print(f"Error removing {old_file}: {e}")
                    
        return {"status": "ok", "archive": archive_path, "size_kb": size_kb, "sha256": sha256_hash}
        
    finally:
        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir, ignore_errors=True)

if __name__ == "__main__":
    res = run_backup()
    print("Backup completed:", res)
