import os
import sqlite3
import json
import requests
from datetime import datetime

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
DB_PATH = "conversations.db"
BACKUP_PATH = "db_backup.json"

def backup_database():
    if not os.path.exists(DB_PATH):
        print("Database not found")
        return
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    backup_data = {"timestamp": datetime.now().isoformat(), "users": [], "history": []}
    
    cursor.execute("SELECT * FROM users")
    columns = [desc[0] for desc in cursor.description]
    for row in cursor.fetchall():
        backup_data["users"].append(dict(zip(columns, row)))
    
    cursor.execute("SELECT * FROM history")
    columns = [desc[0] for desc in cursor.description]
    for row in cursor.fetchall():
        backup_data["history"].append(dict(zip(columns, row)))
    
    conn.close()
    
    with open(BACKUP_PATH, 'w', encoding='utf-8') as f:
        json.dump(backup_data, f, ensure_ascii=False, indent=2)
    
    print(f"Backup created: {BACKUP_PATH}")
    upload_to_github()

def upload_to_github():
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("GitHub credentials not set")
        return
    
    with open(BACKUP_PATH, 'r', encoding='utf-8') as f:
        content = f.read()
    
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{BACKUP_PATH}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    
    response = requests.get(url, headers=headers)
    sha = response.json()["sha"] if response.status_code == 200 else None
    
    data = {"message": f"DB backup - {datetime.now().isoformat()}", "content": content.encode('utf-8').hex(), "encoding": "hex"}
    if sha:
        data["sha"] = sha
    
    response = requests.put(url, headers=headers, json=data)
    print("Backup uploaded" if response.status_code in [200, 201] else f"Upload failed: {response.status_code}")

def restore_database():
    if not os.path.exists(BACKUP_PATH):
        print("No backup found")
        return
    
    with open(BACKUP_PATH, 'r', encoding='utf-8') as f:
        backup_data = json.load(f)
    
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    cursor.execute("""CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        questions_today INTEGER DEFAULT 0, last_reset_date TEXT,
        is_premium INTEGER DEFAULT 0, selected_sages TEXT DEFAULT 'all', created_at TEXT)""")
    
    cursor.execute("""CREATE TABLE IF NOT EXISTS history (
        user_id INTEGER PRIMARY KEY, messages TEXT)""")
    
    for user in backup_data.get("users", []):
        cursor.execute("INSERT OR REPLACE INTO users VALUES (?,?,?,?,?,?,?,?)",
            (user["user_id"], user["username"], user["first_name"], user["questions_today"],
             user["last_reset_date"], user["is_premium"], user["selected_sages"], user["created_at"]))
    
    for hist in backup_data.get("history", []):
        cursor.execute("INSERT OR REPLACE INTO history VALUES (?,?)", (hist["user_id"], hist["messages"]))
    
    conn.commit()
    conn.close()
    print(f"Database restored from {backup_data.get('timestamp', 'unknown')}")

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "restore":
        restore_database()
    else:
        backup_database()