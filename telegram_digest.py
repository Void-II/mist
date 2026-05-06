import json
import os
import re
import sys
import time
import requests
from datetime import datetime
from zoneinfo import ZoneInfo
from github import Github, Auth

# ---------- Config ----------
BOT_TOKEN = os.environ['BOT_TOKEN']
GH_TOKEN = os.environ['GH_TOKEN']
REPO_NAME = os.environ['REPO']
CHANNEL_ID = int(os.environ['CHANNEL_ID'])  # Your channel ID (e.g., -1001234567890)
MESSAGE_ID_INPUT = os.environ.get('MESSAGE_ID', '').strip()

BASE_DIR = 'tg/files'
MAX_FILE_SIZE = 20 * 1024 * 1024    # 20 MB
MAX_TOTAL_SIZE = 400 * 1024 * 1024  # 400 MB
LAST_UPDATE_FILE = 'last_update_id.txt'
PROCESSED_FILE = 'processed_message_ids.json'
TEHRAN_TZ = ZoneInfo("Asia/Tehran")

# Create Github instance
auth = Auth.Token(GH_TOKEN)
g = Github(auth=auth)
repo = g.get_repo(REPO_NAME)

# ---------- Fetch updates from Telegram ----------
def get_updates(offset=None):
    """Fetch updates from Telegram bot"""
    url = f'https://api.telegram.org/bot{BOT_TOKEN}/getUpdates'
    params = {'timeout': 30}
    if offset:
        params['offset'] = offset
    
    response = requests.get(url, params=params, timeout=35)
    if response.status_code != 200:
        print(f"Error fetching updates: {response.text}")
        return []
    
    data = response.json()
    if not data.get('ok'):
        print(f"Telegram API error: {data}")
        return []
    
    return data.get('result', [])

def get_last_update_id():
    """Read the last processed update_id from file"""
    if os.path.exists(LAST_UPDATE_FILE):
        with open(LAST_UPDATE_FILE, 'r') as f:
            content = f.read().strip()
            if content:
                return int(content)
    return 0

def save_last_update_id(update_id):
    """Save the last processed update_id"""
    with open(LAST_UPDATE_FILE, 'w') as f:
        f.write(str(update_id))

# ---------- Load processed IDs ----------
if os.path.exists(PROCESSED_FILE):
    with open(PROCESSED_FILE, 'r') as f:
        try:
            processed_ids = json.load(f)
        except json.JSONDecodeError:
            processed_ids = []
else:
    processed_ids = []

# ---------- Get messages from Telegram ----------
print("Fetching updates from Telegram...")
last_update_id = get_last_update_id()

# If requesting a specific message, we need to fetch with offset - 1 to include it
if MESSAGE_ID_INPUT:
    # For specific message, we fetch a wide range and filter
    updates = get_updates()
else:
    # Normal mode: get only new updates
    updates = get_updates(offset=last_update_id + 1)

# Filter updates for our channel and extract messages
messages = []
for update in updates:
    # Save the update_id regardless
    update_id = update.get('update_id', 0)
    if update_id > last_update_id:
        last_update_id = update_id
    
    # Check if this update has a message from our channel
    message = update.get('message') or update.get('channel_post')
    if not message:
        continue
    
    # Filter by channel ID
    if message.get('chat', {}).get('id') != CHANNEL_ID:
        continue
    
    messages.append(message)

print(f"Found {len(messages)} messages from channel")

# If specific message ID requested, filter further
if MESSAGE_ID_INPUT:
    target_id = int(MESSAGE_ID_INPUT)
    messages = [m for m in messages if m.get('message_id') == target_id]
    if not messages:
        print(f"Message {target_id} not found in channel")
        sys.exit(1)
    print(f"Processing single message: {target_id}")

# ---------- Process messages ----------
if not messages:
    print("No new messages to process")
    # Still save the update_id
    save_last_update_id(last_update_id)
    sys.exit(0)

processed_count = 0
newly_processed = []

for msg in messages:
    message_id = msg.get('message_id')
    if not message_id:
        print("Message without ID, skipping")
        continue
    
    if message_id in processed_ids:
        print(f"Message {message_id} already processed, skipping")
        continue
    
    try:
        # Iran time (UTC+3:30)
        msg_date = datetime.fromtimestamp(msg['date'], tz=TEHRAN_TZ)
        issue_title = msg_date.strftime('%d/%m/%Y')
        
        # Find or create the daily issue
        issue = None
        for i in repo.get_issues(state='open'):
            if i.title == issue_title:
                issue = i
                break
        
        if issue is None:
            issue = repo.create_issue(
                title=issue_title,
                body="Daily digest from Telegram channel – messages as comments below"
            )
            print(f"Created issue #{issue.number}: {issue_title}")
        else:
            print(f"Using existing issue #{issue.number}")
        
        # Check for duplicate comment
        comment_marker = f'<!-- msg_{message_id} -->'
        already_commented = False
        for comment in issue.get_comments():
            if comment.body and comment_marker in comment.body:
                already_commented = True
                break
        
        if already_commented:
            print(f"Message {message_id} already commented")
            processed_ids.append(message_id)
            newly_processed.append(message_id)
            continue
        
        # Build comment body
        time_str = msg_date.strftime('%H:%M')
        sender = msg.get('from', {})
        sender_name = sender.get('first_name') or sender.get('username') or 'Unknown'
        text = msg.get('text') or msg.get('caption') or ''
        
        comment_body = f"**{sender_name}** at {time_str}\n"
        if text:
            comment_body += f"\n{text}\n"
        
        # Handle file attachments
        file_type = None
        file_info = None
        
        if 'photo' in msg:
            file_type = 'photo'
            file_info = msg['photo'][-1]  # Largest size
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
        
        if file_info and file_info.get('file_id'):
            file_id = file_info['file_id']
            file_unique_id = file_info.get('file_unique_id', file_id)
            
            try:
                # Get file info from Telegram
                tg_resp = requests.get(
                    f'https://api.telegram.org/bot{BOT_TOKEN}/getFile',
                    params={'file_id': file_id},
                    timeout=10
                )
                
                if tg_resp.status_code == 200 and tg_resp.json().get('ok'):
                    file_data = tg_resp.json()['result']
                    file_path = file_data.get('file_path', '')
                    
                    if file_path:
                        file_size = file_data.get('file_size', 0)
                        ext = file_path.split('.')[-1] if '.' in file_path else ''
                        
                        if file_size <= MAX_FILE_SIZE:
                            download_url = f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}'
                            safe_name = f"{message_id}_{file_unique_id}"
                            if ext:
                                safe_name += f".{ext}"
                            
                            local_dir = os.path.join(BASE_DIR, file_type)
                            os.makedirs(local_dir, exist_ok=True)
                            local_path = os.path.join(local_dir, safe_name)
                            
                            # Download file
                            file_resp = requests.get(download_url, timeout=30)
                            if file_resp.status_code == 200:
                                with open(local_path, 'wb') as f:
                                    f.write(file_resp.content)
                                
                                raw_url = f"https://github.com/{REPO_NAME}/raw/main/{local_path}"
                                comment_body += f"\n📎 [{file_type}]({raw_url})"
                                print(f"Downloaded {file_type} ({file_size} bytes)")
                            else:
                                comment_body += f"\n⚠️ Download failed"
                        else:
                            direct_link = f'https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}'
                            size_mb = file_size // 1024 // 1024
                            comment_body += f"\n📁 [{file_type} ({size_mb} MB)]({direct_link})"
                else:
                    comment_body += f"\n⚠️ File info unavailable"
                    
            except Exception as e:
                print(f"Error with file: {e}")
                comment_body += f"\n⚠️ File error"
        
        # Add marker and post comment
        comment_body += f"\n\n{comment_marker}"
        issue.create_comment(comment_body)
        print(f"✅ Posted comment for message {message_id}")
        
        processed_count += 1
        newly_processed.append(message_id)
        processed_ids.append(message_id)
        
    except Exception as e:
        print(f"❌ Error processing message {message_id}: {e}")
        import traceback
        traceback.print_exc()
        continue

# ---------- Save state ----------
save_last_update_id(last_update_id)

# Keep processed IDs manageable
if len(processed_ids) > 10000:
    processed_ids = processed_ids[-10000:]

with open(PROCESSED_FILE, 'w') as f:
    json.dump(processed_ids, f)

print(f"Processed {processed_count} messages")
print(f"Last update_id: {last_update_id}")

# ---------- Enforce file size limit ----------
def get_total_size(directory):
    total = 0
    if not os.path.exists(directory):
        return total
    for dirpath, _, filenames in os.walk(directory):
        for fname in filenames:
            fp = os.path.join(dirpath, fname)
            if os.path.exists(fp):
                total += os.path.getsize(fp)
    return total

def extract_message_id(filename):
    match = re.match(r'^(\d+)_.*', filename)
    return int(match.group(1)) if match else 0

if os.path.exists(BASE_DIR):
    total_size = get_total_size(BASE_DIR)
    print(f"Current files: {total_size / 1024 / 1024:.2f} MB")
    
    if total_size > MAX_TOTAL_SIZE:
        files_list = []
        for dirpath, _, filenames in os.walk(BASE_DIR):
            for fname in filenames:
                full = os.path.join(dirpath, fname)
                mid = extract_message_id(fname)
                files_list.append((mid, full))
        
        files_list.sort(key=lambda x: x[0])
        
        deleted = 0
        for _, full_path in files_list:
            if get_total_size(BASE_DIR) <= MAX_TOTAL_SIZE * 0.9:
                break
            if os.path.exists(full_path):
                os.remove(full_path)
                deleted += 1
        
        if deleted:
            print(f"Deleted {deleted} old files")

# ---------- Commit changes ----------
import subprocess

subprocess.run(['git', 'config', 'user.name', 'github-actions'], check=True)
subprocess.run(['git', 'config', 'user.email', 'actions@github.com'], check=True)

if os.path.exists(BASE_DIR):
    subprocess.run(['git', 'add', BASE_DIR], check=True)

for file in [PROCESSED_FILE, LAST_UPDATE_FILE]:
    if os.path.exists(file):
        subprocess.run(['git', 'add', file], check=True)

result = subprocess.run(['git', 'status', '--porcelain'], capture_output=True, text=True)
if result.stdout.strip():
    subprocess.run(['git', 'commit', '-m', f'Processed {processed_count} messages'], check=True)
    subprocess.run(['git', 'push'], check=True)
    print("✅ Changes pushed")
else:
    print("No changes to commit")

print("🎉 Done!")
