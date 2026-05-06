import json
import os
import re
import sys
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from github import Github, Auth

# =============================================
# CONFIGURATION
# =============================================
BOT_TOKEN = os.environ['BOT_TOKEN']
GH_TOKEN = os.environ['GH_TOKEN']
CHANNEL_ID = int(os.environ['CHANNEL_ID'])
REPO_NAME = os.environ['REPO']
MESSAGE_ID_INPUT = os.environ.get('MESSAGE_ID', '').strip()

BASE_DIR = 'tg/files'
MAX_FILE_SIZE = 20 * 1024 * 1024              # 20 MB
MAX_TOTAL_SIZE = 400 * 1024 * 1024            # 400 MB
OFFSET_FILE = 'telegram_offset.txt'
PROCESSED_FILE = 'processed_message_ids.json'
TEHRAN_TZ = ZoneInfo("Asia/Tehran")
TELEGRAM_API = f'https://api.telegram.org/bot{BOT_TOKEN}'

# =============================================
# GITHUB AUTH
# =============================================
auth = Auth.Token(GH_TOKEN)
g = Github(auth=auth)
repo = g.get_repo(REPO_NAME)

# =============================================
# LOAD OFFSET
# =============================================
if os.path.exists(OFFSET_FILE):
    with open(OFFSET_FILE) as f:
        offset = int(f.read().strip())
else:
    offset = 0

# =============================================
# FETCH UPDATES
# =============================================
print("Fetching updates from Telegram...")
resp = requests.get(f'{TELEGRAM_API}/getUpdates', params={
    'offset': offset,
    'timeout': 30
})
resp.raise_for_status()
updates = resp.json()['result']

if not updates:
    print("No new messages.")
    sys.exit(0)

# Filter messages from our channel
messages = []
max_update_id = offset

for update in updates:
    max_update_id = max(max_update_id, update['update_id'])
    msg = update.get('channel_post') or update.get('message')
    if msg and msg.get('chat', {}).get('id') == CHANNEL_ID:
        messages.append(msg)

if not messages:
    print("No messages from target channel.")
    with open(OFFSET_FILE, 'w') as f:
        f.write(str(max_update_id + 1))
    sys.exit(0)

# Filter by message_id if provided
if MESSAGE_ID_INPUT:
    target_id = int(MESSAGE_ID_INPUT)
    messages = [m for m in messages if m['message_id'] == target_id]
    if not messages:
        print(f"Message {target_id} not found.")
        sys.exit(1)

# Load processed IDs
if os.path.exists(PROCESSED_FILE):
    with open(PROCESSED_FILE) as f:
        processed_ids = json.load(f)
else:
    processed_ids = []

# =============================================
# PROCESS EACH MESSAGE
# =============================================
for msg in messages:
    message_id = msg['message_id']
    
    if message_id in processed_ids:
        print(f"Skipping already processed message {message_id}")
        continue
    
    print(f"Processing message {message_id}...")
    
    msg_date = datetime.fromtimestamp(msg['date'], tz=TEHRAN_TZ)
    issue_title = msg_date.strftime('%d/%m/%Y')
    
    # Find/create daily issue
    issue = None
    for i in repo.get_issues(state='open'):
        if i.title == issue_title:
            issue = i
            break
    
    time_str = msg_date.strftime('%H:%M')
    sender = msg.get('from', {})
    sender_name = sender.get('first_name') or sender.get('username') or 'Unknown'
    text = msg.get('text') or msg.get('caption') or ''
    
    block = f"**{sender_name}** at {time_str}:\n"
    if text:
        block += f"{text}\n"
    
    # Handle files
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
        
        try:
            file_resp = requests.get(f'{TELEGRAM_API}/getFile', params={'file_id': file_id})
            file_resp.raise_for_status()
            file_data = file_resp.json()['result']
            file_path = file_data['file_path']
            file_size = file_data.get('file_size', 0)
            ext = file_path.split('.')[-1] if '.' in file_path else ''
            
            if file_size <= MAX_FILE_SIZE:
                download_url = f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}'
                safe_name = f"{message_id}_{file_unique_id}.{ext}" if ext else f"{message_id}_{file_unique_id}"
                local_dir = os.path.join(BASE_DIR, file_type)
                os.makedirs(local_dir, exist_ok=True)
                local_path = os.path.join(local_dir, safe_name)
                
                print(f"  Downloading {file_type}...")
                with requests.get(download_url, stream=True) as r:
                    r.raise_for_status()
                    with open(local_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                
                raw_url = f"https://github.com/{REPO_NAME}/raw/main/{local_path}"
                block += f"[📎 {file_type}]({raw_url})\n"
            else:
                block += f"[📁 Large {file_type} (>{MAX_FILE_SIZE//1024//1024}MB)](https://t.me/c/{str(CHANNEL_ID)[4:]}/{message_id})\n"
        except Exception as e:
            print(f"  Error: {e}")
            block += f"[⚠️ Error downloading {file_type}]\n"
    
    try:
        if issue:
            issue.edit(body=issue.body + "\n\n" + block if issue.body else block)
        else:
            repo.create_issue(title=issue_title, body=block)
        print(f"  Added to issue '{issue_title}'")
    except Exception as e:
        print(f"  Error updating issue: {e}")
    
    processed_ids.append(message_id)
    time.sleep(0.5)

# =============================================
# ENFORCE SIZE LIMIT
# =============================================
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
        print(f"Cleaning old files (total: {total_size})...")
        files_list = []
        for dirpath, _, filenames in os.walk(BASE_DIR):
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                files_list.append((extract_message_id(fname), full))
        files_list.sort(key=lambda x: x[0])
        
        for _, full_path in files_list:
            if get_total_size(BASE_DIR) <= MAX_TOTAL_SIZE:
                break
            os.remove(full_path)

# =============================================
# SAVE & COMMIT
# =============================================
with open(PROCESSED_FILE, 'w') as f:
    json.dump(processed_ids, f)

with open(OFFSET_FILE, 'w') as f:
    f.write(str(max_update_id + 1))

import subprocess
subprocess.run(['git', 'config', 'user.name', 'github-actions'], check=True)
subprocess.run(['git', 'config', 'user.email', 'actions@github.com'], check=True)
subprocess.run(['git', 'add', BASE_DIR, PROCESSED_FILE, OFFSET_FILE], check=True)
subprocess.run(['git', 'add', '-u', BASE_DIR], check=True)
subprocess.run(['git', 'commit', '-m', f'Process {len(messages)} messages'], check=True)
subprocess.run(['git', 'push'], check=True)

print(f"\nDone! Processed {len(messages)} message(s)")
