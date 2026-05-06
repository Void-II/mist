import json
import os
import re
import sys
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from github import Github

# ---------- Config ----------
BOT_TOKEN = os.environ['BOT_TOKEN']
GH_TOKEN = os.environ['GH_TOKEN']
REPO_NAME = os.environ['REPO']
MESSAGE_ID_INPUT = os.environ.get('MESSAGE_ID', '')  # may be empty or integer string

BASE_DIR = 'tg/files'
MAX_FILE_SIZE = 20 * 1024 * 1024    # 20 MB
MAX_TOTAL_SIZE = 400 * 1024 * 1024  # 400 MB
PENDING_FILE = 'pending_messages.json'
PROCESSED_FILE = 'processed_message_ids.json'
TEHRAN_TZ = ZoneInfo("Asia/Tehran")

g = Github(GH_TOKEN)
repo = g.get_repo(REPO_NAME)

# ---------- Load pending messages ----------
if not os.path.exists(PENDING_FILE):
    print("No pending messages file. Exiting.")
    sys.exit(0)

with open(PENDING_FILE) as f:
    pending = json.load(f)

if not pending:
    print("No pending messages.")
    sys.exit(0)

# ---------- Filter by message_id if provided ----------
if MESSAGE_ID_INPUT:
    target_id = int(MESSAGE_ID_INPUT)
    pending = [m for m in pending if m['message_id'] == target_id]
    if not pending:
        print(f"Message {target_id} not found in pending list.")
        sys.exit(1)

# ---------- Load processed IDs ----------
if os.path.exists(PROCESSED_FILE):
    with open(PROCESSED_FILE) as f:
        processed_ids = json.load(f)
else:
    processed_ids = []

# ---------- Process each message ----------
new_pending = []   # messages that remain pending (none after successful processing)
for msg in pending:
    message_id = msg['message_id']

    # Skip already processed (shouldn't happen if we clean up, but safety)
    if message_id in processed_ids:
        continue

    # Date and issue
    msg_date = datetime.fromtimestamp(msg['date'], tz=TEHRAN_TZ)
    issue_title = msg_date.strftime('%d/%m/%Y')

    # Find or create daily issue
    issue = None
    for i in repo.get_issues(state='open'):
        if i.title == issue_title:
            issue = i
            break

    # Build message block
    time_str = msg_date.strftime('%H:%M')
    sender = msg.get('from', {})
    sender_name = sender.get('first_name') or sender.get('username') or 'Unknown'
    text = msg.get('text') or msg.get('caption') or ''

    block = f"**{sender_name}** at {time_str}:\n"
    if text:
        block += f"{text}\n"

    # Handle file attachment
    file_type = None
    file_info = None
    if 'photo' in msg:
        file_type = 'photo'
        file_info = msg['photo'][-1]
    elif 'document' in msg:
        file_type = 'document'
        file_info = msg['document']
    elif 'video' in msg:
        file_type = 'video'
        file_info = msg['video']
    elif 'audio' in msg:
        file_type = 'audio'
        file_info = msg['audio']
    elif 'voice' in msg:
        file_type = 'voice'
        file_info = msg['voice']
    elif 'video_note' in msg:
        file_type = 'video_note'
        file_info = msg['video_note']
    elif 'sticker' in msg:
        file_type = 'sticker'
        file_info = msg['sticker']

    if file_info and 'file_id' in file_info:
        file_id = file_info['file_id']
        file_unique_id = file_info.get('file_unique_id', file_id)

        tg_resp = requests.get(
            f'https://api.telegram.org/bot{BOT_TOKEN}/getFile',
            params={'file_id': file_id}
        )
        tg_resp.raise_for_status()
        file_data = tg_resp.json()['result']
        file_path = file_data['file_path']
        file_size = file_data.get('file_size', 0)
        ext = file_path.split('.')[-1] if '.' in file_path else ''

        if file_size <= MAX_FILE_SIZE:
            download_url = f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}'
            safe_name = f"{message_id}_{file_unique_id}.{ext}" if ext else f"{message_id}_{file_unique_id}"
            local_dir = os.path.join(BASE_DIR, file_type)
            os.makedirs(local_dir, exist_ok=True)
            local_path = os.path.join(local_dir, safe_name)

            with requests.get(download_url, stream=True) as r:
                r.raise_for_status()
                with open(local_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        f.write(chunk)

            raw_url = f"https://github.com/{REPO_NAME}/raw/main/{local_path}"
            block += f"[📎 Download {file_type}]({raw_url})\n"
        else:
            direct_link = f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}'
            block += f"[📁 Large {file_type} (>{MAX_FILE_SIZE//1024//1024} MB)]({direct_link})\n"

    # Update issue
    if issue:
        new_body = issue.body + "\n\n" + block if issue.body else block
        issue.edit(body=new_body)
    else:
        repo.create_issue(title=issue_title, body=block)

    # Update processed IDs
    processed_ids.append(message_id)

# ---------- Write updated processed IDs ----------
with open(PROCESSED_FILE, 'w') as f:
    json.dump(processed_ids, f)

# ---------- Clean up pending file: remove processed messages ----------
if MESSAGE_ID_INPUT:
    # Remove only the processed message(s)
    remaining = [m for m in json.load(open(PENDING_FILE)) if m['message_id'] != int(MESSAGE_ID_INPUT)]
else:
    remaining = []  # all processed
with open(PENDING_FILE, 'w') as f:
    json.dump(remaining, f)

# ---------- Enforce total file size limit ----------
def get_total_size(directory):
    total = 0
    for dirpath, _, filenames in os.walk(directory):
        for fname in filenames:
            total += os.path.getsize(os.path.join(dirpath, fname))
    return total

def extract_message_id(filename):
    match = re.match(r'^(\d+)_.*', filename)
    return int(match.group(1)) if match else 0

if os.path.exists(BASE_DIR):
    total_size = get_total_size(BASE_DIR)
    if total_size > MAX_TOTAL_SIZE:
        files_list = []
        for dirpath, _, filenames in os.walk(BASE_DIR):
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                mid = extract_message_id(fname)
                files_list.append((mid, full))
        files_list.sort(key=lambda x: x[0])   # oldest first

        for _, full_path in files_list:
            if get_total_size(BASE_DIR) <= MAX_TOTAL_SIZE:
                break
            os.remove(full_path)
            print(f"Deleted old file: {full_path}")

# ---------- Commit and push ----------
import subprocess
subprocess.run(['git', 'config', 'user.name', 'github-actions'], check=True)
subprocess.run(['git', 'config', 'user.email', 'actions@github.com'], check=True)
subprocess.run(['git', 'add', BASE_DIR, PROCESSED_FILE, PENDING_FILE], check=True)
subprocess.run(['git', 'add', '-u', BASE_DIR], check=True)   # deleted files
subprocess.run(['git', 'commit', '-m', f'Processed messages from pending list'], check=True)
subprocess.run(['git', 'push'], check=True)

print("Processing complete.")
