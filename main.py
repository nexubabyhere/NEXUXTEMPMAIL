from flask import Flask, render_template_string, jsonify, request, send_file
import requests
import json
from uuid import uuid4
import time
from datetime import datetime, timedelta
import re
from dateutil import parser
import hashlib
import os
import csv
import io
import sqlite3
from contextlib import closing
import base64

app = Flask(__name__)

# Database setup
def init_db():
    with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                last_activity TIMESTAMP NOT NULL,
                is_active INTEGER DEFAULT 1
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                sender TEXT,
                recipient TEXT,
                subject TEXT,
                body_preview TEXT,
                full_content TEXT,
                received_at TIMESTAMP NOT NULL,
                is_read INTEGER DEFAULT 0,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id)
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS archives (
                archive_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                archive_name TEXT,
                created_at TIMESTAMP NOT NULL,
                file_path TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions (session_id)
            )
        ''')
        conn.commit()

init_db()

# Store active sessions in memory for quick access
active_sessions = {}
message_cache = {}

class TempMailSession:
    def __init__(self, email, session_id):
        self.email = email
        self.session_id = session_id
        self.created_at = datetime.utcnow()
        self.last_check = datetime.utcnow()
        self.message_count = 0
        self.is_active = True
        self.custom_alias = None
        
        # Store in database
        with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
            conn.execute(
                'INSERT OR REPLACE INTO sessions (session_id, email, created_at, last_activity, is_active) VALUES (?, ?, ?, ?, ?)',
                (session_id, email, self.created_at, self.created_at, 1)
            )
            conn.commit()
        
        active_sessions[session_id] = self
    
    def update_activity(self):
        self.last_check = datetime.utcnow()
        with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
            conn.execute(
                'UPDATE sessions SET last_activity = ? WHERE session_id = ?',
                (self.last_check, self.session_id)
            )
            conn.commit()
    
    def deactivate(self):
        self.is_active = False
        with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
            conn.execute(
                'UPDATE sessions SET is_active = 0 WHERE session_id = ?',
                (self.session_id,)
            )
            conn.commit()
        if self.session_id in active_sessions:
            del active_sessions[self.session_id]

def generate_email(use_custom_domain=False, custom_prefix=None):
    """Generate a temporary email address with optional customization"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Content-Type': 'application/json',
        'Origin': 'https://www.emailnator.com',
        'Referer': 'https://www.emailnator.com/',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site'
    }
    
    try:
        # Try multiple email generation strategies
        strategies = [
            {"ids": [1, 2, 3]},  # Gmail-like, Random, Dot Gmail
            {"ids": [2, 3]},     # Random + Dot Gmail
            {"ids": [1]}         # Gmail-like only
        ]
        
        email = None
        for strategy in strategies:
            try:
                response = requests.post(
                    "https://api.emailnator.com/api/email/generate",
                    data=json.dumps(strategy),
                    headers=headers,
                    timeout=15
                )
                if response.status_code == 200:
                    data = response.json()
                    if 'email' in data:
                        email = data['email']
                        break
            except:
                continue
        
        if not email:
            # Fallback: generate a random email
            random_part = hashlib.md5(str(uuid4()).encode()).hexdigest()[:10]
            email = f"{random_part}@tempmail.tmp"
        
        # Apply custom prefix if requested
        if custom_prefix and '@' in email:
            local_part, domain = email.split('@', 1)
            email = f"{custom_prefix}_{local_part}@{domain}"
        
        # Generate session ID
        session_id = hashlib.sha256(f"{email}{datetime.utcnow().isoformat()}".encode()).hexdigest()[:12]
        
        # Create session object
        session = TempMailSession(email, session_id)
        
        return {
            'email': email,
            'session_id': session_id,
            'custom': custom_prefix is not None
        }
    except Exception as e:
        print(f"Error generating email: {e}")
        # Ultimate fallback
        fallback_email = f"temp_{int(time.time())}_{uuid4().hex[:6]}@fallback.tmp"
        session_id = hashlib.md5(fallback_email.encode()).hexdigest()[:10]
        session = TempMailSession(fallback_email, session_id)
        return {
            'email': fallback_email,
            'session_id': session_id,
            'custom': False,
            'fallback': True
        }

def get_inbox(email, session_id):
    """Get inbox messages with enhanced parsing"""
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Origin': 'https://www.emailnator.com',
        'Referer': 'https://www.emailnator.com/'
    }
    
    try:
        response = requests.post(
            "https://api.emailnator.com/api/email/inbox",
            data=json.dumps({"email": email}),
            headers=headers,
            timeout=20
        )
        
        if response.status_code != 200:
            return {'messages': [], 'status': 'error', 'code': response.status_code}
        
        data = response.json()
        
        # Store messages in database
        if 'messages' in data and data['messages']:
            store_messages_in_db(data['messages'], session_id, email)
        
        return data
    except Exception as e:
        print(f"Error fetching inbox: {e}")
        return {'messages': [], 'status': 'error', 'error': str(e)}

def store_messages_in_db(messages, session_id, recipient_email):
    """Store messages in SQLite database"""
    with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
        for msg in messages:
            try:
                # Extract message data
                sender = 'Unknown'
                subject = 'No Subject'
                body_preview = 'No preview'
                full_content = str(msg)
                received_time = datetime.utcnow()
                
                if isinstance(msg, str):
                    lines = msg.strip().split('\n')
                    if len(lines) >= 3:
                        sender = lines[0].replace('NEW', '').strip()
                        subject = lines[2].strip() if len(lines) > 2 else 'No Subject'
                        body_preview = lines[3].strip() if len(lines) > 3 else 'No preview'
                elif isinstance(msg, dict):
                    sender = msg.get('sender', 'Unknown')
                    subject = msg.get('subject', 'No Subject')
                    body_preview = msg.get('preview', 'No preview')
                    if 'text' in msg and isinstance(msg['text'], str):
                        full_content = msg['text']
                
                # Generate message ID
                msg_id = hashlib.sha256(f"{sender}{subject}{body_preview}{received_time.isoformat()}".encode()).hexdigest()[:16]
                
                # Check if message already exists
                cursor = conn.execute('SELECT message_id FROM messages WHERE message_id = ?', (msg_id,))
                if not cursor.fetchone():
                    conn.execute('''
                        INSERT INTO messages 
                        (message_id, session_id, sender, recipient, subject, body_preview, full_content, received_at, is_read)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (msg_id, session_id, sender, recipient_email, subject, body_preview, full_content, received_time, 0))
                
            except Exception as e:
                print(f"Error storing message: {e}")
                continue
        
        conn.commit()

def get_session_messages(session_id, limit=50, offset=0, unread_only=False):
    """Retrieve messages for a session from database"""
    with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
        query = '''
            SELECT message_id, sender, recipient, subject, body_preview, 
                   full_content, received_at, is_read
            FROM messages 
            WHERE session_id = ?
        '''
        params = [session_id]
        
        if unread_only:
            query += ' AND is_read = 0'
        
        query += ' ORDER BY received_at DESC LIMIT ? OFFSET ?'
        params.extend([limit, offset])
        
        cursor = conn.execute(query, params)
        rows = cursor.fetchall()
        
        messages = []
        for row in rows:
            messages.append({
                'id': row[0],
                'sender': row[1],
                'recipient': row[2],
                'subject': row[3],
                'preview': row[4],
                'content': row[5],
                'time': row[6],
                'is_read': bool(row[7])
            })
        
        # Get counts
        cursor = conn.execute('SELECT COUNT(*) FROM messages WHERE session_id = ?', (session_id,))
        total = cursor.fetchone()[0]
        
        cursor = conn.execute('SELECT COUNT(*) FROM messages WHERE session_id = ? AND is_read = 0', (session_id,))
        unread = cursor.fetchone()[0]
        
        return {
            'messages': messages,
            'total': total,
            'unread': unread,
            'has_more': total > (offset + limit)
        }

def mark_as_read(message_id, session_id):
    """Mark a message as read"""
    with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
        conn.execute(
            'UPDATE messages SET is_read = 1 WHERE message_id = ? AND session_id = ?',
            (message_id, session_id)
        )
        conn.commit()
        return True

def delete_message(message_id, session_id):
    """Delete a specific message"""
    with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
        conn.execute(
            'DELETE FROM messages WHERE message_id = ? AND session_id = ?',
            (message_id, session_id)
        )
        conn.commit()
        return True

def export_messages(session_id, format_type='json'):
    """Export messages in specified format"""
    messages_data = get_session_messages(session_id, limit=1000, offset=0)
    messages = messages_data['messages']
    
    if format_type == 'json':
        return json.dumps(messages, indent=2, default=str)
    elif format_type == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['Sender', 'Recipient', 'Subject', 'Preview', 'Received At', 'Read'])
        for msg in messages:
            writer.writerow([
                msg['sender'],
                msg['recipient'],
                msg['subject'],
                msg['preview'],
                msg['time'],
                'Yes' if msg['is_read'] else 'No'
            ])
        return output.getvalue()
    elif format_type == 'txt':
        lines = []
        for msg in messages:
            lines.append(f"From: {msg['sender']}")
            lines.append(f"To: {msg['recipient']}")
            lines.append(f"Subject: {msg['subject']}")
            lines.append(f"Time: {msg['time']}")
            lines.append(f"Preview: {msg['preview']}")
            lines.append("-" * 50)
        return "\n".join(lines)
    
    return ""

def get_session_stats(session_id):
    """Get statistics for a session"""
    with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
        # Message stats
        cursor = conn.execute('''
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN is_read = 0 THEN 1 ELSE 0 END) as unread,
                MIN(received_at) as first_msg,
                MAX(received_at) as last_msg
            FROM messages 
            WHERE session_id = ?
        ''', (session_id,))
        
        stats = cursor.fetchone()
        
        # Session info
        cursor = conn.execute('SELECT email, created_at FROM sessions WHERE session_id = ?', (session_id,))
        session_info = cursor.fetchone()
        
        return {
            'total_messages': stats[0] if stats[0] else 0,
            'unread_messages': stats[1] if stats[1] else 0,
            'first_message': stats[2],
            'last_message': stats[3],
            'email': session_info[0] if session_info else 'Unknown',
            'created_at': session_info[1] if session_info else None,
            'active': session_id in active_sessions
        }

# HTML Template with enhanced UI
HTML_TEMPLATE = '''
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>⚡ TempMail Control Center</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/sweetalert2@11"></script>
    <style>
        :root {
            --primary: #6366f1;
            --primary-dark: #4f46e5;
            --secondary: #10b981;
            --danger: #ef4444;
            --warning: #f59e0b;
            --dark: #1f2937;
            --light: #f9fafb;
            --gray: #6b7280;
            --gray-light: #e5e7eb;
            --sidebar-width: 280px;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Inter', sans-serif;
        }
        
        body {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            color: var(--dark);
        }
        
        .app-container {
            display: flex;
            min-height: 100vh;
            background: var(--light);
            border-radius: 20px;
            margin: 20px;
            overflow: hidden;
            box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
        }
        
        /* Sidebar Navigation */
        .sidebar {
            width: var(--sidebar-width);
            background: var(--dark);
            color: white;
            padding: 30px 20px;
            display: flex;
            flex-direction: column;
        }
        
        .logo {
            display: flex;
            align-items: center;
            gap: 15px;
            margin-bottom: 40px;
            padding-bottom: 20px;
            border-bottom: 2px solid var(--primary);
        }
        
        .logo-icon {
            font-size: 2.5rem;
            color: var(--secondary);
        }
        
        .logo-text h1 {
            font-size: 1.5rem;
            font-weight: 700;
            background: linear-gradient(90deg, #6366f1, #10b981);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        
        .logo-text p {
            font-size: 0.85rem;
            opacity: 0.7;
        }
        
        .nav-menu {
            list-style: none;
            flex-grow: 1;
        }
        
        .nav-item {
            margin-bottom: 10px;
        }
        
        .nav-link {
            display: flex;
            align-items: center;
            gap: 15px;
            padding: 15px 20px;
            color: white;
            text-decoration: none;
            border-radius: 12px;
            transition: all 0.3s ease;
            font-weight: 500;
        }
        
        .nav-link:hover {
            background: rgba(255, 255, 255, 0.1);
            transform: translateX(5px);
        }
        
        .nav-link.active {
            background: var(--primary);
            box-shadow: 0 4px 12px rgba(99, 102, 241, 0.3);
        }
        
        .nav-link i {
            font-size: 1.2rem;
            width: 24px;
            text-align: center;
        }
        
        .session-info {
            background: rgba(255, 255, 255, 0.1);
            padding: 20px;
            border-radius: 12px;
            margin-top: 20px;
        }
        
        .session-info h3 {
            font-size: 0.9rem;
            opacity: 0.8;
            margin-bottom: 10px;
        }
        
        .session-email {
            font-size: 0.9rem;
            word-break: break-all;
            background: rgba(0, 0, 0, 0.3);
            padding: 10px;
            border-radius: 8px;
            margin-bottom: 15px;
        }
        
        .session-stats {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 10px;
            font-size: 0.8rem;
        }
        
        .stat-item {
            text-align: center;
            padding: 8px;
            background: rgba(0, 0, 0, 0.2);
            border-radius: 6px;
        }
        
        .stat-value {
            font-weight: 700;
            color: var(--secondary);
        }
        
        /* Main Content */
        .main-content {
            flex-grow: 1;
            padding: 30px;
            overflow-y: auto;
            background: white;
        }
        
        .content-section {
            display: none;
            animation: fadeIn 0.3s ease;
        }
        
        .content-section.active {
            display: block;
        }
        
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        .section-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 2px solid var(--gray-light);
        }
        
        .section-title {
            display: flex;
            align-items: center;
            gap: 15px;
            font-size: 1.8rem;
            font-weight: 700;
            color: var(--dark);
        }
        
        .section-title i {
            color: var(--primary);
            font-size: 2rem;
        }
        
        /* Dashboard */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));
            gap: 25px;
            margin-bottom: 40px;
        }
        
        .stat-card {
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-dark) 100%);
            color: white;
            padding: 25px;
            border-radius: 16px;
            box-shadow: 0 10px 20px rgba(99, 102, 241, 0.2);
        }
        
        .stat-card.secondary {
            background: linear-gradient(135deg, var(--secondary) 0%, #059669 100%);
        }
        
        .stat-card.warning {
            background: linear-gradient(135deg, var(--warning) 0%, #d97706 100%);
        }
        
        .stat-card.danger {
            background: linear-gradient(135deg, var(--danger) 0%, #dc2626 100%);
        }
        
        .stat-icon {
            font-size: 2.5rem;
            margin-bottom: 15px;
            opacity: 0.9;
        }
        
        .stat-value-lg {
            font-size: 2.5rem;
            font-weight: 800;
            margin-bottom: 5px;
        }
        
        .stat-label {
            font-size: 0.9rem;
            opacity: 0.9;
        }
        
        .quick-actions {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 40px;
        }
        
        .action-btn {
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
            padding: 30px 20px;
            background: white;
            border: 2px solid var(--gray-light);
            border-radius: 16px;
            cursor: pointer;
            transition: all 0.3s ease;
            text-decoration: none;
            color: var(--dark);
        }
        
        .action-btn:hover {
            transform: translateY(-5px);
            border-color: var(--primary);
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.1);
        }
        
        .action-btn i {
            font-size: 2.5rem;
            margin-bottom: 15px;
            color: var(--primary);
        }
        
        .action-btn span {
            font-weight: 600;
            font-size: 1.1rem;
        }
        
        /* Email Generator */
        .generator-card {
            background: white;
            border-radius: 16px;
            padding: 40px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.08);
            margin-bottom: 30px;
        }
        
        .email-display-large {
            background: linear-gradient(135deg, #f0f4ff 0%, #e0e7ff 100%);
            border-radius: 12px;
            padding: 30px;
            text-align: center;
            margin-bottom: 30px;
            border: 3px dashed #c7d2fe;
        }
        
        .current-email {
            font-size: 2.2rem;
            font-weight: 800;
            color: var(--primary-dark);
            margin: 20px 0;
            word-break: break-all;
            padding: 20px;
            background: white;
            border-radius: 12px;
            border: 2px solid #e0e7ff;
        }
        
        .controls-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-top: 30px;
        }
        
        .control-btn {
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 25px 15px;
            background: white;
            border: 2px solid var(--gray-light);
            border-radius: 12px;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        
        .control-btn:hover {
            transform: translateY(-3px);
            box-shadow: 0 8px 20px rgba(0, 0, 0, 0.1);
        }
        
        .control-btn.primary {
            border-color: var(--primary);
            background: linear-gradient(135deg, #e0e7ff 0%, #c7d2fe 100%);
        }
        
        .control-btn.secondary {
            border-color: var(--secondary);
            background: linear-gradient(135deg, #d1fae5 0%, #a7f3d0 100%);
        }
        
        .control-btn.warning {
            border-color: var(--warning);
            background: linear-gradient(135deg, #fef3c7 0%, #fde68a 100%);
        }
        
        .control-btn i {
            font-size: 2rem;
            margin-bottom: 15px;
        }
        
        .control-btn.primary i { color: var(--primary); }
        .control-btn.secondary i { color: var(--secondary); }
        .control-btn.warning i { color: var(--warning); }
        .control-btn.danger i { color: var(--danger); }
        
        .control-label {
            font-weight: 600;
            font-size: 1.1rem;
            margin-bottom: 5px;
        }
        
        .control-desc {
            font-size: 0.85rem;
            opacity: 0.7;
            text-align: center;
        }
        
        /* Inbox */
        .inbox-controls {
            display: flex;
            gap: 15px;
            margin-bottom: 25px;
            flex-wrap: wrap;
        }
        
        .inbox-btn {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 12px 24px;
            background: white;
            border: 2px solid var(--gray-light);
            border-radius: 10px;
            cursor: pointer;
            font-weight: 600;
            transition: all 0.3s ease;
        }
        
        .inbox-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 5px 15px rgba(0, 0, 0, 0.1);
        }
        
        .inbox-btn.refresh {
            border-color: var(--primary);
            color: var(--primary);
        }
        
        .inbox-btn.mark-all {
            border-color: var(--secondary);
            color: var(--secondary);
        }
        
        .inbox-btn.export {
            border-color: var(--warning);
            color: var(--warning);
        }
        
        .inbox-btn.delete-all {
            border-color: var(--danger);
            color: var(--danger);
        }
        
        .messages-list {
            max-height: 600px;
            overflow-y: auto;
            border-radius: 12px;
            border: 2px solid var(--gray-light);
        }
        
        .message-item {
            padding: 25px;
            border-bottom: 1px solid var(--gray-light);
            background: white;
            transition: all 0.3s ease;
            position: relative;
        }
        
        .message-item:hover {
            background: #f9fafb;
            transform: translateX(5px);
        }
        
        .message-item.unread {
            border-left: 4px solid var(--primary);
            background: #f0f9ff;
        }
        
        .message-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 15px;
        }
        
        .message-sender {
            display: flex;
            align-items: center;
            gap: 10px;
            font-weight: 700;
            color: var(--dark);
            font-size: 1.1rem;
        }
        
        .message-sender i {
            color: var(--primary);
        }
        
        .message-time {
            color: var(--gray);
            font-size: 0.9rem;
            white-space: nowrap;
        }
        
        .message-subject {
            font-weight: 600;
            font-size: 1.2rem;
            color: var(--dark);
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        
        .message-preview {
            color: var(--gray);
            line-height: 1.6;
            margin-bottom: 15px;
        }
        
        .message-actions {
            display: flex;
            gap: 10px;
            opacity: 0;
            transition: opacity 0.3s ease;
        }
        
        .message-item:hover .message-actions {
            opacity: 1;
        }
        
        .msg-action-btn {
            padding: 6px 12px;
            border: none;
            border-radius: 6px;
            font-size: 0.85rem;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 5px;
        }
        
        .msg-action-btn.read {
            background: var(--secondary);
            color: white;
        }
        
        .msg-action-btn.view {
            background: var(--primary);
            color: white;
        }
        
        .msg-action-btn.delete {
            background: var(--danger);
            color: white;
        }
        
        /* History */
        .sessions-list {
            display: grid;
            gap: 20px;
        }
        
        .session-card {
            background: white;
            border-radius: 12px;
            padding: 25px;
            border: 2px solid var(--gray-light);
            transition: all 0.3s ease;
        }
        
        .session-card:hover {
            border-color: var(--primary);
            box-shadow: 0 10px 25px rgba(0, 0, 0, 0.1);
        }
        
        .session-card.active {
            border-color: var(--secondary);
            background: #f0fdf4;
        }
        
        .session-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
        }
        
        .session-email-small {
            font-weight: 600;
            color: var(--dark);
            word-break: break-all;
        }
        
        .session-status {
            padding: 5px 15px;
            border-radius: 20px;
            font-size: 0.85rem;
            font-weight: 600;
        }
        
        .session-status.active {
            background: var(--secondary);
            color: white;
        }
        
        .session-status.inactive {
            background: var(--gray);
            color: white;
        }
        
        .session-stats-small {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 15px;
            margin-top: 15px;
            padding-top: 15px;
            border-top: 1px solid var(--gray-light);
        }
        
        .session-stat {
            text-align: center;
        }
        
        .session-stat-value {
            font-weight: 700;
            font-size: 1.2rem;
            color: var(--primary);
        }
        
        .session-stat-label {
            font-size: 0.8rem;
            color: var(--gray);
        }
        
        /* Settings & Export */
        .settings-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 30px;
        }
        
        .settings-card {
            background: white;
            border-radius: 12px;
            padding: 30px;
            border: 2px solid var(--gray-light);
        }
        
        .settings-card h3 {
            display: flex;
            align-items: center;
            gap: 10px;
            margin-bottom: 20px;
            color: var(--dark);
        }
        
        .settings-card h3 i {
            color: var(--primary);
        }
        
        .form-group {
            margin-bottom: 20px;
        }
        
        .form-group label {
            display: block;
            margin-bottom: 8px;
            font-weight: 500;
            color: var(--dark);
        }
        
        .form-control {
            width: 100%;
            padding: 12px 15px;
            border: 2px solid var(--gray-light);
            border-radius: 8px;
            font-size: 1rem;
            transition: border-color 0.3s ease;
        }
        
        .form-control:focus {
            outline: none;
            border-color: var(--primary);
        }
        
        .export-options {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
            gap: 15px;
            margin-top: 20px;
        }
        
        .export-option {
            display: flex;
            flex-direction: column;
            align-items: center;
            padding: 20px;
            border: 2px solid var(--gray-light);
            border-radius: 10px;
            cursor: pointer;
            transition: all 0.3s ease;
        }
        
        .export-option:hover {
            transform: translateY(-3px);
            border-color: var(--primary);
        }
        
        .export-option i {
            font-size: 2rem;
            margin-bottom: 10px;
            color: var(--primary);
        }
        
        /* Footer */
        .footer {
            text-align: center;
            padding: 20px;
            color: var(--gray);
            font-size: 0.9rem;
            border-top: 1px solid var(--gray-light);
            margin-top: 40px;
        }
        
        /* Responsive */
        @media (max-width: 1024px) {
            .app-container {
                flex-direction: column;
                margin: 10px;
            }
            
            .sidebar {
                width: 100%;
                padding: 20px;
            }
            
            .nav-menu {
                display: flex;
                overflow-x: auto;
                padding-bottom: 10px;
            }
            
            .nav-item {
                margin-bottom: 0;
                margin-right: 10px;
            }
            
            .nav-link {
                white-space: nowrap;
            }
        }
        
        @media (max-width: 768px) {
            .main-content {
                padding: 20px;
            }
            
            .stats-grid {
                grid-template-columns: 1fr;
            }
            
            .quick-actions {
                grid-template-columns: 1fr;
            }
            
            .current-email {
                font-size: 1.5rem;
            }
            
            .controls-grid {
                grid-template-columns: 1fr;
            }
            
            .session-stats-small {
                grid-template-columns: repeat(2, 1fr);
            }
        }
        
        .loading {
            display: none;
            text-align: center;
            padding: 40px;
        }
        
        .loading-spinner {
            width: 50px;
            height: 50px;
            border: 4px solid #f3f3f3;
            border-top: 4px solid var(--primary);
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 20px;
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <div class="app-container">
        <!-- Sidebar Navigation -->
        <div class="sidebar">
            <div class="logo">
                <div class="logo-icon">
                    <i class="fas fa-mail-bulk"></i>
                </div>
                <div class="logo-text">
                    <h1>TempMail Pro</h1>
                    <p>Control Center v2.0</p>
                </div>
            </div>
            
            <ul class="nav-menu">
                <li class="nav-item">
                    <a href="#" class="nav-link active" data-section="dashboard">
                        <i class="fas fa-tachometer-alt"></i>
                        <span>Dashboard</span>
                    </a>
                </li>
                <li class="nav-item">
                    <a href="#" class="nav-link" data-section="generator">
                        <i class="fas fa-envelope"></i>
                        <span>Email Generator</span>
                    </a>
                </li>
                <li class="nav-item">
                    <a href="#" class="nav-link" data-section="inbox">
                        <i class="fas fa-inbox"></i>
                        <span>Inbox</span>
                        <span class="badge" id="unreadBadge" style="margin-left: auto; background: var(--secondary); color: white; padding: 2px 8px; border-radius: 10px; font-size: 0.8rem;">0</span>
                    </a>
                </li>
                <li class="nav-item">
                    <a href="#" class="nav-link" data-section="history">
                        <i class="fas fa-history"></i>
                        <span>Session History</span>
                    </a>
                </li>
                <li class="nav-item">
                    <a href="#" class="nav-link" data-section="export">
                        <i class="fas fa-download"></i>
                        <span>Export & Settings</span>
                    </a>
                </li>
            </ul>
            
            <div class="session-info">
                <h3>Current Session</h3>
                <div class="session-email" id="sidebarEmail">No active session</div>
                <div class="session-stats">
                    <div class="stat-item">
                        <div class="stat-value" id="sidebarTotal">0</div>
                        <div>Total</div>
                    </div>
                    <div class="stat-item">
                        <div class="stat-value" id="sidebarUnread">0</div>
                        <div>Unread</div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Main Content -->
        <div class="main-content">
            <!-- Dashboard Section -->
            <div class="content-section active" id="dashboard">
                <div class="section-header">
                    <div class="section-title">
                        <i class="fas fa-tachometer-alt"></i>
                        <span>Dashboard Overview</span>
                    </div>
                    <div style="display: flex; gap: 10px;">
                        <button class="inbox-btn refresh" onclick="checkAllSessions()">
                            <i class="fas fa-sync-alt"></i> Refresh All
                        </button>
                    </div>
                </div>
                
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-icon">
                            <i class="fas fa-envelope"></i>
                        </div>
                        <div class="stat-value-lg" id="statTotalMessages">0</div>
                        <div class="stat-label">Total Messages</div>
                    </div>
                    <div class="stat-card secondary">
                        <div class="stat-icon">
                            <i class="fas fa-envelope-open"></i>
                        </div>
                        <div class="stat-value-lg" id="statUnreadMessages">0</div>
                        <div class="stat-label">Unread Messages</div>
                    </div>
                    <div class="stat-card warning">
                        <div class="stat-icon">
                            <i class="fas fa-clock"></i>
                        </div>
                        <div class="stat-value-lg" id="statActiveSessions">0</div>
                        <div class="stat-label">Active Sessions</div>
                    </div>
                    <div class="stat-card danger">
                        <div class="stat-icon">
                            <i class="fas fa-trash"></i>
                        </div>
                        <div class="stat-value-lg" id="statDeleted">0</div>
                        <div class="stat-label">Messages Deleted</div>
                    </div>
                </div>
                
                <h3 style="margin: 30px 0 20px 0; color: var(--dark);">Quick Actions</h3>
                <div class="quick-actions">
                    <a href="#" class="action-btn" onclick="switchSection('generator')">
                        <i class="fas fa-plus-circle"></i>
                        <span>Generate New Email</span>
                    </a>
                    <a href="#" class="action-btn" onclick="switchSection('inbox')">
                        <i class="fas fa-inbox"></i>
                        <span>Check Inbox</span>
                    </a>
                    <a href="#" class="action-btn" onclick="exportData('json')">
                        <i class="fas fa-file-export"></i>
                        <span>Export Data</span>
                    </a>
                    <a href="#" class="action-btn" onclick="clearAllSessions()">
                        <i class="fas fa-trash-alt"></i>
                        <span>Cleanup All</span>
                    </a>
                </div>
            </div>
            
            <!-- Email Generator Section -->
            <div class="content-section" id="generator">
                <div class="section-header">
                    <div class="section-title">
                        <i class="fas fa-envelope"></i>
                        <span>Email Generator</span>
                    </div>
                </div>
                
                <div class="generator-card">
                    <div class="email-display-large">
                        <div class="loading" id="generatorLoading">
                            <div class="loading-spinner"></div>
                            <p>Generating secure email...</p>
                        </div>
                        <div id="emailDisplay">
                            <p style="color: var(--gray); margin-bottom: 15px;">Generate a fresh temporary email address</p>
                            <div class="current-email" id="currentEmailDisplay">Not generated yet</div>
                            <div id="emailInfo" style="margin-top: 15px; font-size: 0.9rem; color: var(--gray);">
                                <span id="emailStatus">Ready to generate</span>
                            </div>
                        </div>
                    </div>
                    
                    <div class="controls-grid">
                        <div class="control-btn primary" onclick="generateEmail('standard')">
                            <i class="fas fa-bolt"></i>
                            <div class="control-label">Quick Generate</div>
                            <div class="control-desc">Standard random email</div>
                        </div>
                        
                        <div class="control-btn secondary" onclick="generateEmail('custom')">
                            <i class="fas fa-magic"></i>
                            <div class="control-label">Custom Prefix</div>
                            <div class="control-desc">Add your own prefix</div>
                        </div>
                        
                        <div class="control-btn warning" onclick="copyCurrentEmail()">
                            <i class="fas fa-copy"></i>
                            <div class="control-label">Copy Email</div>
                            <div class="control-desc">Copy to clipboard</div>
                        </div>
                        
                        <div class="control-btn danger" onclick="deleteCurrentSession()">
                            <i class="fas fa-trash"></i>
                            <div class="control-label">Delete Session</div>
                            <div class="control-desc">Clear current email</div>
                        </div>
                    </div>
                </div>
            </div>
            
            <!-- Inbox Section -->
            <div class="content-section" id="inbox">
                <div class="section-header">
                    <div class="section-title">
                        <i class="fas fa-inbox"></i>
                        <span>Inbox Manager</span>
                    </div>
                    <div style="display: flex; gap: 10px; align-items: center;">
                        <span id="lastCheckTime" style="font-size: 0.9rem; color: var(--gray);">Never checked</span>
                    </div>
                </div>
                
                <div class="inbox-controls">
                    <button class="inbox-btn refresh" onclick="refreshInbox()">
                        <i class="fas fa-sync-alt"></i> Refresh
                    </button>
                    <button class="inbox-btn mark-all" onclick="markAllAsRead()">
                        <i class="fas fa-check-double"></i> Mark All Read
                    </button>
                    <button class="inbox-btn export" onclick="showExportOptions()">
                        <i class="fas fa-file-export"></i> Export
                    </button>
                    <button class="inbox-btn delete-all" onclick="deleteAllMessages()">
                        <i class="fas fa-trash-alt"></i> Delete All
                    </button>
                </div>
                
                <div class="messages-list" id="messagesList">
                    <div class="loading" id="inboxLoading">
                        <div class="loading-spinner"></div>
                        <p>Loading messages...</p>
                    </div>
                    <div id="messagesContainer">
                        <!-- Messages will be loaded here -->
                    </div>
                </div>
            </div>
            
            <!-- History Section -->
            <div class="content-section" id="history">
                <div class="section-header">
                    <div class="section-title">
                        <i class="fas fa-history"></i>
                        <span>Session History</span>
                    </div>
                    <button class="inbox-btn refresh" onclick="loadHistory()">
                        <i class="fas fa-sync-alt"></i> Refresh
                    </button>
                </div>
                
                <div class="sessions-list" id="sessionsList">
                    <div class="loading" id="historyLoading">
                        <div class="loading-spinner"></div>
                        <p>Loading session history...</p>
                    </div>
                </div>
            </div>
            
            <!-- Export & Settings Section -->
            <div class="content-section" id="export">
                <div class="section-header">
                    <div class="section-title">
                        <i class="fas fa-download"></i>
                        <span>Export & Settings</span>
                    </div>
                </div>
                
                <div class="settings-grid">
                    <div class="settings-card">
                        <h3><i class="fas fa-file-export"></i> Export Data</h3>
                        <p style="margin-bottom: 20px; color: var(--gray);">Export your messages in various formats.</p>
                        
                        <div class="export-options">
                            <div class="export-option" onclick="exportData('json')">
                                <i class="fas fa-code"></i>
                                <div>JSON</div>
                            </div>
                            <div class="export-option" onclick="exportData('csv')">
                                <i class="fas fa-file-csv"></i>
                                <div>CSV</div>
                            </div>
                            <div class="export-option" onclick="exportData('txt')">
                                <i class="fas fa-file-alt"></i>
                                <div>Text</div>
                            </div>
                        </div>
                    </div>
                    
                    <div class="settings-card">
                        <h3><i class="fas fa-cog"></i> Settings</h3>
                        
                        <div class="form-group">
                            <label for="autoRefresh">Auto-refresh Interval (seconds)</label>
                            <select class="form-control" id="autoRefresh">
                                <option value="0">Disabled</option>
                                <option value="5">5 seconds</option>
                                <option value="10" selected>10 seconds</option>
                                <option value="30">30 seconds</option>
                                <option value="60">1 minute</option>
                            </select>
                        </div>
                        
                        <div class="form-group">
                            <label for="maxMessages">Max Messages per Load</label>
                            <select class="form-control" id="maxMessages">
                                <option value="10">10</option>
                                <option value="25" selected>25</option>
                                <option value="50">50</option>
                                <option value="100">100</option>
                            </select>
                        </div>
                        
                        <button class="inbox-btn" style="width: 100%; margin-top: 10px;" onclick="saveSettings()">
                            <i class="fas fa-save"></i> Save Settings
                        </button>
                    </div>
                </div>
                
                <div class="settings-card" style="margin-top: 30px;">
                    <h3><i class="fas fa-database"></i> Database Management</h3>
                    <p style="margin-bottom: 20px; color: var(--gray);">Manage your local database and sessions.</p>
                    
                    <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                        <button class="inbox-btn warning" onclick="clearDatabase()">
                            <i class="fas fa-eraser"></i> Clear All Data
                        </button>
                        <button class="inbox-btn danger" onclick="deleteInactiveSessions()">
                            <i class="fas fa-trash"></i> Delete Inactive
                        </button>
                        <button class="inbox-btn" onclick="downloadDatabase()">
                            <i class="fas fa-download"></i> Backup DB
                        </button>
                    </div>
                </div>
            </div>
            
            <div class="footer">
                <p>TempMail Control Center v2.0 • Secure Temporary Email Service</p>
                <p style="font-size: 0.8rem; margin-top: 5px; opacity: 0.7;">All data is stored locally in your browser and database</p>
            </div>
        </div>
    </div>

    <script>
        // Global variables
        let currentSession = null;
        let currentEmail = null;
        let autoRefreshInterval = null;
        let settings = {
            autoRefresh: 10,
            maxMessages: 25
        };
        
        // Load settings from localStorage
        function loadSettings() {
            const saved = localStorage.getItem('tempmail_settings');
            if (saved) {
                settings = JSON.parse(saved);
                document.getElementById('autoRefresh').value = settings.autoRefresh;
                document.getElementById('maxMessages').value = settings.maxMessages;
                setupAutoRefresh();
            }
        }
        
        function saveSettings() {
            settings.autoRefresh = parseInt(document.getElementById('autoRefresh').value);
            settings.maxMessages = parseInt(document.getElementById('maxMessages').value);
            localStorage.setItem('tempmail_settings', JSON.stringify(settings));
            setupAutoRefresh();
            showNotification('Settings saved successfully!', 'success');
        }
        
        function setupAutoRefresh() {
            if (autoRefreshInterval) {
                clearInterval(autoRefreshInterval);
            }
            
            if (settings.autoRefresh > 0 && currentSession) {
                autoRefreshInterval = setInterval(refreshInbox, settings.autoRefresh * 1000);
            }
        }
        
        // Navigation
        function switchSection(sectionId) {
            // Update active nav link
            document.querySelectorAll('.nav-link').forEach(link => {
                link.classList.remove('active');
                if (link.dataset.section === sectionId) {
                    link.classList.add('active');
                }
            });
            
            // Show corresponding section
            document.querySelectorAll('.content-section').forEach(section => {
                section.classList.remove('active');
                if (section.id === sectionId) {
                    section.classList.add('active');
                }
            });
            
            // Load section data
            if (sectionId === 'inbox' && currentSession) {
                loadInbox();
            } else if (sectionId === 'history') {
                loadHistory();
            } else if (sectionId === 'dashboard') {
                updateDashboard();
            }
        }
        
        document.querySelectorAll('.nav-link').forEach(link => {
            link.addEventListener('click', function(e) {
                e.preventDefault();
                switchSection(this.dataset.section);
            });
        });
        
        // Email Generation
        function generateEmail(type = 'standard') {
            if (type === 'custom') {
                Swal.fire({
                    title: 'Custom Email Prefix',
                    input: 'text',
                    inputLabel: 'Enter a prefix for your email',
                    inputPlaceholder: 'e.g., myprefix',
                    showCancelButton: true,
                    confirmButtonText: 'Generate',
                    preConfirm: (prefix) => {
                        if (!prefix) {
                            Swal.showValidationMessage('Please enter a prefix');
                            return false;
                        }
                        return prefix;
                    }
                }).then((result) => {
                    if (result.isConfirmed) {
                        performEmailGeneration(result.value);
                    }
                });
            } else {
                performEmailGeneration();
            }
        }
        
        function performEmailGeneration(customPrefix = null) {
            const loading = document.getElementById('generatorLoading');
            const display = document.getElementById('emailDisplay');
            
            loading.style.display = 'block';
            display.style.display = 'none';
            
            let requestData = { type: 'standard' };
            if (customPrefix) {
                requestData = { type: 'custom', prefix: customPrefix };
            }
            
            fetch('/generate-email', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(requestData)
            })
            .then(response => response.json())
            .then(data => {
                loading.style.display = 'none';
                display.style.display = 'block';
                
                if (data.success) {
                    currentSession = data.session_id;
                    currentEmail = data.email;
                    
                    document.getElementById('currentEmailDisplay').textContent = currentEmail;
                    document.getElementById('emailStatus').textContent = `Generated ${new Date().toLocaleTimeString()}`;
                    document.getElementById('sidebarEmail').textContent = currentEmail;
                    
                    // Update session info
                    updateSessionInfo();
                    
                    // Start auto-refresh if enabled
                    setupAutoRefresh();
                    
                    showNotification('New email generated successfully!', 'success');
                    
                    // Switch to inbox after generation
                    setTimeout(() => switchSection('inbox'), 500);
                } else {
                    showNotification('Failed to generate email: ' + (data.error || 'Unknown error'), 'error');
                }
            })
            .catch(error => {
                loading.style.display = 'none';
                display.style.display = 'block';
                showNotification('Network error: ' + error.message, 'error');
            });
        }
        
        function copyCurrentEmail() {
            if (!currentEmail) {
                showNotification('No email to copy', 'warning');
                return;
            }
            
            navigator.clipboard.writeText(currentEmail).then(() => {
                showNotification('Email copied to clipboard!', 'success');
            }).catch(err => {
                showNotification('Failed to copy: ' + err, 'error');
            });
        }
        
        function deleteCurrentSession() {
            if (!currentSession) {
                showNotification('No active session', 'warning');
                return;
            }
            
            Swal.fire({
                title: 'Delete Session?',
                text: 'This will remove all messages for this email',
                icon: 'warning',
                showCancelButton: true,
                confirmButtonColor: '#d33',
                cancelButtonColor: '#3085d6',
                confirmButtonText: 'Yes, delete it!'
            }).then((result) => {
                if (result.isConfirmed) {
                    fetch('/delete-session', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ session_id: currentSession })
                    })
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            currentSession = null;
                            currentEmail = null;
                            document.getElementById('currentEmailDisplay').textContent = 'Not generated yet';
                            document.getElementById('sidebarEmail').textContent = 'No active session';
                            document.getElementById('sidebarTotal').textContent = '0';
                            document.getElementById('sidebarUnread').textContent = '0';
                            document.getElementById('unreadBadge').textContent = '0';
                            showNotification('Session deleted successfully', 'success');
                            switchSection('dashboard');
                        }
                    });
                }
            });
        }
        
        // Inbox Management
        function loadInbox() {
            if (!currentSession) {
                document.getElementById('messagesContainer').innerHTML = `
                    <div style="text-align: center; padding: 40px; color: var(--gray);">
                        <i class="fas fa-envelope" style="font-size: 3rem; margin-bottom: 15px; opacity: 0.5;"></i>
                        <p>Generate an email first to view messages</p>
                    </div>
                `;
                return;
            }
            
            const loading = document.getElementById('inboxLoading');
            const container = document.getElementById('messagesContainer');
            
            loading.style.display = 'block';
            container.style.display = 'none';
            
            fetch('/get-messages', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    session_id: currentSession,
                    limit: settings.maxMessages,
                    offset: 0
                })
            })
            .then(response => response.json())
            .then(data => {
                loading.style.display = 'none';
                container.style.display = 'block';
                
                if (data.success) {
                    displayMessages(data.messages);
                    updateLastCheckTime();
                    updateSessionInfo();
                }
            })
            .catch(error => {
                loading.style.display = 'none';
                container.style.display = 'block';
                showNotification('Error loading messages: ' + error.message, 'error');
            });
        }
        
        function refreshInbox() {
            if (!currentSession) {
                showNotification('No active session', 'warning');
                return;
            }
            
            document.getElementById('lastCheckTime').innerHTML = `<i class="fas fa-sync-alt fa-spin"></i> Checking...`;
            
            fetch('/check-inbox', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    session_id: currentSession,
                    email: currentEmail 
                })
            })
            .then(response => response.json())
            .then(data => {
                updateLastCheckTime();
                if (data.success) {
                    loadInbox();
                    if (data.new_count > 0) {
                        showNotification(`${data.new_count} new message(s) received!`, 'info');
                    }
                }
            });
        }
        
        function displayMessages(messages) {
            const container = document.getElementById('messagesContainer');
            
            if (!messages || messages.length === 0) {
                container.innerHTML = `
                    <div style="text-align: center; padding: 40px; color: var(--gray);">
                        <i class="fas fa-inbox" style="font-size: 3rem; margin-bottom: 15px; opacity: 0.5;"></i>
                        <p>No messages yet. Send emails to your address to see them here.</p>
                    </div>
                `;
                return;
            }
            
            let html = '';
            messages.forEach(msg => {
                const time = new Date(msg.time).toLocaleString();
                const unreadClass = msg.is_read ? '' : 'unread';
                const readIcon = msg.is_read ? 'fa-envelope-open' : 'fa-envelope';
                
                html += `
                    <div class="message-item ${unreadClass}" data-id="${msg.id}">
                        <div class="message-header">
                            <div class="message-sender">
                                <i class="fas fa-user"></i>
                                ${escapeHtml(msg.sender || 'Unknown Sender')}
                            </div>
                            <div class="message-time">${time}</div>
                        </div>
                        <div class="message-subject">
                            <i class="fas ${readIcon}"></i>
                            ${escapeHtml(msg.subject || 'No Subject')}
                        </div>
                        <div class="message-preview">
                            ${escapeHtml(msg.preview || 'No preview available')}
                        </div>
                        <div class="message-actions">
                            ${!msg.is_read ? `
                                <button class="msg-action-btn read" onclick="markAsRead('${msg.id}')">
                                    <i class="fas fa-check"></i> Mark Read
                                </button>
                            ` : ''}
                            <button class="msg-action-btn view" onclick="viewMessage('${msg.id}')">
                                <i class="fas fa-eye"></i> View
                            </button>
                            <button class="msg-action-btn delete" onclick="deleteMessage('${msg.id}')">
                                <i class="fas fa-trash"></i> Delete
                            </button>
                        </div>
                    </div>
                `;
            });
            
            container.innerHTML = html;
        }
        
        function markAsRead(messageId) {
            fetch('/mark-read', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    session_id: currentSession,
                    message_id: messageId 
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    const msgElement = document.querySelector(`[data-id="${messageId}"]`);
                    if (msgElement) {
                        msgElement.classList.remove('unread');
                        msgElement.querySelector('.message-subject i').className = 'fas fa-envelope-open';
                        msgElement.querySelector('.message-actions').innerHTML = `
                            <button class="msg-action-btn view" onclick="viewMessage('${messageId}')">
                                <i class="fas fa-eye"></i> View
                            </button>
                            <button class="msg-action-btn delete" onclick="deleteMessage('${messageId}')">
                                <i class="fas fa-trash"></i> Delete
                            </button>
                        `;
                    }
                    updateSessionInfo();
                }
            });
        }
        
        function viewMessage(messageId) {
            fetch('/get-message', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    session_id: currentSession,
                    message_id: messageId 
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    Swal.fire({
                        title: data.message.subject || 'No Subject',
                        html: `
                            <div style="text-align: left;">
                                <p><strong>From:</strong> ${escapeHtml(data.message.sender)}</p>
                                <p><strong>To:</strong> ${escapeHtml(data.message.recipient)}</p>
                                <p><strong>Time:</strong> ${new Date(data.message.time).toLocaleString()}</p>
                                <hr>
                                <div style="max-height: 300px; overflow-y: auto; background: #f5f5f5; padding: 15px; border-radius: 5px; margin-top: 15px;">
                                    <pre style="white-space: pre-wrap; font-family: inherit;">${escapeHtml(data.message.content)}</pre>
                                </div>
                            </div>
                        `,
                        width: '800px',
                        showCloseButton: true,
                        showConfirmButton: false
                    });
                    
                    // Mark as read when viewed
                    markAsRead(messageId);
                }
            });
        }
        
        function deleteMessage(messageId) {
            Swal.fire({
                title: 'Delete Message?',
                text: 'This action cannot be undone',
                icon: 'warning',
                showCancelButton: true,
                confirmButtonColor: '#d33',
                cancelButtonColor: '#3085d6',
                confirmButtonText: 'Delete'
            }).then((result) => {
                if (result.isConfirmed) {
                    fetch('/delete-message', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ 
                            session_id: currentSession,
                            message_id: messageId 
                        })
                    })
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            const msgElement = document.querySelector(`[data-id="${messageId}"]`);
                            if (msgElement) {
                                msgElement.style.opacity = '0';
                                setTimeout(() => msgElement.remove(), 300);
                            }
                            updateSessionInfo();
                            showNotification('Message deleted', 'success');
                        }
                    });
                }
            });
        }
        
        function markAllAsRead() {
            if (!currentSession) return;
            
            fetch('/mark-all-read', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: currentSession })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    document.querySelectorAll('.message-item.unread').forEach(item => {
                        item.classList.remove('unread');
                        item.querySelector('.message-subject i').className = 'fas fa-envelope-open';
                    });
                    updateSessionInfo();
                    showNotification('All messages marked as read', 'success');
                }
            });
        }
        
        function deleteAllMessages() {
            if (!currentSession) return;
            
            Swal.fire({
                title: 'Delete All Messages?',
                text: 'This will remove all messages for this session',
                icon: 'warning',
                showCancelButton: true,
                confirmButtonColor: '#d33',
                cancelButtonColor: '#3085d6',
                confirmButtonText: 'Delete All'
            }).then((result) => {
                if (result.isConfirmed) {
                    fetch('/delete-all-messages', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ session_id: currentSession })
                    })
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            document.getElementById('messagesContainer').innerHTML = `
                                <div style="text-align: center; padding: 40px; color: var(--gray);">
                                    <i class="fas fa-inbox" style="font-size: 3rem; margin-bottom: 15px; opacity: 0.5;"></i>
                                    <p>All messages have been deleted</p>
                                </div>
                            `;
                            updateSessionInfo();
                            showNotification('All messages deleted', 'success');
                        }
                    });
                }
            });
        }
        
        // Session History
        function loadHistory() {
            const loading = document.getElementById('historyLoading');
            const container = document.getElementById('sessionsList');
            
            loading.style.display = 'block';
            
            fetch('/get-sessions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            })
            .then(response => response.json())
            .then(data => {
                loading.style.display = 'none';
                
                if (data.success && data.sessions.length > 0) {
                    let html = '';
                    data.sessions.forEach(session => {
                        const isActive = session.session_id === currentSession;
                        const created = new Date(session.created_at).toLocaleString();
                        const lastActive = new Date(session.last_activity).toLocaleString();
                        
                        html += `
                            <div class="session-card ${isActive ? 'active' : ''}">
                                <div class="session-header">
                                    <div class="session-email-small">${escapeHtml(session.email)}</div>
                                    <div class="session-status ${session.is_active ? 'active' : 'inactive'}">
                                        ${session.is_active ? 'Active' : 'Inactive'}
                                    </div>
                                </div>
                                <div style="color: var(--gray); font-size: 0.9rem; margin: 10px 0;">
                                    Created: ${created}<br>
                                    Last active: ${lastActive}
                                </div>
                                <div class="session-stats-small">
                                    <div class="session-stat">
                                        <div class="session-stat-value">${session.total_messages}</div>
                                        <div class="session-stat-label">Total</div>
                                    </div>
                                    <div class="session-stat">
                                        <div class="session-stat-value">${session.unread_messages}</div>
                                        <div class="session-stat-label">Unread</div>
                                    </div>
                                    <div class="session-stat">
                                        <div class="session-stat-value">${session.active ? 'Yes' : 'No'}</div>
                                        <div class="session-stat-label">Active</div>
                                    </div>
                                    <div class="session-stat">
                                        ${isActive ? `
                                            <button class="msg-action-btn view" style="padding: 5px 10px; font-size: 0.8rem;" onclick="switchSection('inbox')">
                                                <i class="fas fa-inbox"></i> Open
                                            </button>
                                        ` : `
                                            <button class="msg-action-btn secondary" style="padding: 5px 10px; font-size: 0.8rem;" onclick="activateSession('${session.session_id}')">
                                                <i class="fas fa-play"></i> Activate
                                            </button>
                                        `}
                                    </div>
                                </div>
                            </div>
                        `;
                    });
                    
                    container.innerHTML = html;
                } else {
                    container.innerHTML = `
                        <div style="text-align: center; padding: 40px; color: var(--gray);">
                            <i class="fas fa-history" style="font-size: 3rem; margin-bottom: 15px; opacity: 0.5;"></i>
                            <p>No session history found</p>
                        </div>
                    `;
                }
            });
        }
        
        function activateSession(sessionId) {
            fetch('/activate-session', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: sessionId })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    currentSession = sessionId;
                    currentEmail = data.email;
                    updateSessionInfo();
                    switchSection('inbox');
                    showNotification('Session activated', 'success');
                }
            });
        }
        
        // Export Functions
        function exportData(format) {
            if (!currentSession) {
                showNotification('No active session to export', 'warning');
                return;
            }
            
            fetch('/export-messages', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    session_id: currentSession,
                    format: format 
                })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    const blob = new Blob([data.content], { type: data.mime_type });
                    const url = window.URL.createObjectURL(blob);
                    const a = document.createElement('a');
                    a.href = url;
                    a.download = `tempmail_export_${new Date().toISOString().split('T')[0]}.${format}`;
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    window.URL.revokeObjectURL(url);
                    showNotification(`Exported as ${format.toUpperCase()}`, 'success');
                }
            });
        }
        
        function showExportOptions() {
            Swal.fire({
                title: 'Export Messages',
                text: 'Choose export format:',
                showCancelButton: true,
                showDenyButton: true,
                showConfirmButton: true,
                confirmButtonText: 'JSON',
                denyButtonText: 'CSV',
                cancelButtonText: 'Text'
            }).then((result) => {
                if (result.isConfirmed) {
                    exportData('json');
                } else if (result.isDenied) {
                    exportData('csv');
                } else if (result.dismiss === Swal.DismissReason.cancel) {
                    exportData('txt');
                }
            });
        }
        
        // Database Management
        function clearDatabase() {
            Swal.fire({
                title: 'Clear All Data?',
                text: 'This will delete ALL sessions and messages. This action cannot be undone!',
                icon: 'warning',
                showCancelButton: true,
                confirmButtonColor: '#d33',
                cancelButtonColor: '#3085d6',
                confirmButtonText: 'Yes, clear everything!'
            }).then((result) => {
                if (result.isConfirmed) {
                    fetch('/clear-database', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    })
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            currentSession = null;
                            currentEmail = null;
                            updateSessionInfo();
                            switchSection('dashboard');
                            showNotification('All data cleared', 'success');
                        }
                    });
                }
            });
        }
        
        function deleteInactiveSessions() {
            fetch('/delete-inactive', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    showNotification(`Deleted ${data.deleted_count} inactive sessions`, 'success');
                    loadHistory();
                }
            });
        }
        
        function downloadDatabase() {
            fetch('/download-db')
            .then(response => response.blob())
            .then(blob => {
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `tempmail_backup_${new Date().toISOString().split('T')[0]}.db`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                window.URL.revokeObjectURL(url);
                showNotification('Database backup downloaded', 'success');
            });
        }
        
        // Dashboard Functions
        function updateDashboard() {
            fetch('/get-stats', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' }
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    document.getElementById('statTotalMessages').textContent = data.stats.total_messages || 0;
                    document.getElementById('statUnreadMessages').textContent = data.stats.unread_messages || 0;
                    document.getElementById('statActiveSessions').textContent = data.stats.active_sessions || 0;
                    document.getElementById('statDeleted').textContent = data.stats.deleted_messages || 0;
                }
            });
        }
        
        function checkAllSessions() {
            // Implementation for checking all sessions
            showNotification('Refreshing all sessions...', 'info');
            // You would implement API call here
        }
        
        function clearAllSessions() {
            Swal.fire({
                title: 'Clear All Sessions?',
                text: 'This will deactivate all active sessions',
                icon: 'warning',
                showCancelButton: true,
                confirmButtonColor: '#d33',
                cancelButtonColor: '#3085d6',
                confirmButtonText: 'Clear All'
            }).then((result) => {
                if (result.isConfirmed) {
                    fetch('/clear-all-sessions', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' }
                    })
                    .then(response => response.json())
                    .then(data => {
                        if (data.success) {
                            currentSession = null;
                            currentEmail = null;
                            updateSessionInfo();
                            showNotification('All sessions cleared', 'success');
                        }
                    });
                }
            });
        }
        
        // Utility Functions
        function updateSessionInfo() {
            if (!currentSession) return;
            
            fetch('/get-session-stats', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ session_id: currentSession })
            })
            .then(response => response.json())
            .then(data => {
                if (data.success) {
                    document.getElementById('sidebarTotal').textContent = data.stats.total_messages;
                    document.getElementById('sidebarUnread').textContent = data.stats.unread_messages;
                    document.getElementById('unreadBadge').textContent = data.stats.unread_messages;
                }
            });
        }
        
        function updateLastCheckTime() {
            const now = new Date();
            document.getElementById('lastCheckTime').innerHTML = `
                <i class="fas fa-clock"></i> Last check: ${now.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}
            `;
        }
        
        function showNotification(message, type = 'info') {
            const Toast = Swal.mixin({
                toast: true,
                position: 'top-end',
                showConfirmButton: false,
                timer: 3000,
                timerProgressBar: true,
                didOpen: (toast) => {
                    toast.addEventListener('mouseenter', Swal.stopTimer);
                    toast.addEventListener('mouseleave', Swal.resumeTimer);
                }
            });
            
            Toast.fire({
                icon: type,
                title: message
            });
        }
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        // Initialize
        document.addEventListener('DOMContentLoaded', function() {
            loadSettings();
            updateDashboard();
            
            // Check for existing session in localStorage
            const savedSession = localStorage.getItem('current_session');
            if (savedSession) {
                const sessionData = JSON.parse(savedSession);
                currentSession = sessionData.session_id;
                currentEmail = sessionData.email;
                document.getElementById('sidebarEmail').textContent = currentEmail;
                updateSessionInfo();
            }
            
            // Save session on page unload
            window.addEventListener('beforeunload', function() {
                if (currentSession && currentEmail) {
                    localStorage.setItem('current_session', JSON.stringify({
                        session_id: currentSession,
                        email: currentEmail
                    }));
                }
            });
        });
    </script>
</body>
</html>
'''

# Flask Routes
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/generate-email', methods=['POST'])
def generate_email_endpoint():
    data = request.json
    email_type = data.get('type', 'standard')
    custom_prefix = data.get('prefix')
    
    result = generate_email(custom_prefix=custom_prefix)
    
    if result:
        # Store in localStorage simulation
        response_data = {
            'success': True,
            'email': result['email'],
            'session_id': result['session_id'],
            'custom': result.get('custom', False),
            'fallback': result.get('fallback', False)
        }
        return jsonify(response_data)
    
    return jsonify({
        'success': False,
        'error': 'Failed to generate email'
    })

@app.route('/check-inbox', methods=['POST'])
def check_inbox_endpoint():
    data = request.json
    email = data.get('email')
    session_id = data.get('session_id')
    
    if not email or not session_id:
        return jsonify({'success': False, 'error': 'Missing data'})
    
    # Update session activity
    if session_id in active_sessions:
        active_sessions[session_id].update_activity()
    
    # Get new messages from external API
    raw_data = get_inbox(email, session_id)
    
    if raw_data and 'messages' in raw_data:
        # Get unread count from database
        messages_data = get_session_messages(session_id, unread_only=True)
        
        return jsonify({
            'success': True,
            'new_count': messages_data['unread'],
            'status': 'ok'
        })
    
    return jsonify({
        'success': True,
        'new_count': 0,
        'status': 'no_messages'
    })

@app.route('/get-messages', methods=['POST'])
def get_messages_endpoint():
    data = request.json
    session_id = data.get('session_id')
    limit = data.get('limit', 25)
    offset = data.get('offset', 0)
    unread_only = data.get('unread_only', False)
    
    if not session_id:
        return jsonify({'success': False, 'error': 'Missing session ID'})
    
    messages_data = get_session_messages(session_id, limit, offset, unread_only)
    
    return jsonify({
        'success': True,
        'messages': messages_data['messages'],
        'total': messages_data['total'],
        'unread': messages_data['unread'],
        'has_more': messages_data['has_more']
    })

@app.route('/get-message', methods=['POST'])
def get_message_endpoint():
    data = request.json
    session_id = data.get('session_id')
    message_id = data.get('message_id')
    
    if not session_id or not message_id:
        return jsonify({'success': False, 'error': 'Missing data'})
    
    with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
        cursor = conn.execute('''
            SELECT sender, recipient, subject, body_preview, full_content, received_at
            FROM messages 
            WHERE session_id = ? AND message_id = ?
        ''', (session_id, message_id))
        
        row = cursor.fetchone()
        
        if row:
            return jsonify({
                'success': True,
                'message': {
                    'sender': row[0],
                    'recipient': row[1],
                    'subject': row[2],
                    'preview': row[3],
                    'content': row[4],
                    'time': row[5]
                }
            })
    
    return jsonify({'success': False, 'error': 'Message not found'})

@app.route('/mark-read', methods=['POST'])
def mark_read_endpoint():
    data = request.json
    session_id = data.get('session_id')
    message_id = data.get('message_id')
    
    if not session_id or not message_id:
        return jsonify({'success': False, 'error': 'Missing data'})
    
    success = mark_as_read(message_id, session_id)
    return jsonify({'success': success})

@app.route('/mark-all-read', methods=['POST'])
def mark_all_read_endpoint():
    data = request.json
    session_id = data.get('session_id')
    
    if not session_id:
        return jsonify({'success': False, 'error': 'Missing session ID'})
    
    with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
        conn.execute(
            'UPDATE messages SET is_read = 1 WHERE session_id = ?',
            (session_id,)
        )
        conn.commit()
    
    return jsonify({'success': True})

@app.route('/delete-message', methods=['POST'])
def delete_message_endpoint():
    data = request.json
    session_id = data.get('session_id')
    message_id = data.get('message_id')
    
    if not session_id or not message_id:
        return jsonify({'success': False, 'error': 'Missing data'})
    
    success = delete_message(message_id, session_id)
    return jsonify({'success': success})

@app.route('/delete-all-messages', methods=['POST'])
def delete_all_messages_endpoint():
    data = request.json
    session_id = data.get('session_id')
    
    if not session_id:
        return jsonify({'success': False, 'error': 'Missing session ID'})
    
    with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
        conn.execute(
            'DELETE FROM messages WHERE session_id = ?',
            (session_id,)
        )
        conn.commit()
    
    return jsonify({'success': True})

@app.route('/delete-session', methods=['POST'])
def delete_session_endpoint():
    data = request.json
    session_id = data.get('session_id')
    
    if not session_id:
        return jsonify({'success': False, 'error': 'Missing session ID'})
    
    if session_id in active_sessions:
        active_sessions[session_id].deactivate()
    
    with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
        conn.execute('UPDATE sessions SET is_active = 0 WHERE session_id = ?', (session_id,))
        conn.execute('DELETE FROM messages WHERE session_id = ?', (session_id,))
        conn.commit()
    
    return jsonify({'success': True})

@app.route('/get-sessions', methods=['POST'])
def get_sessions_endpoint():
    with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
        cursor = conn.execute('''
            SELECT s.session_id, s.email, s.created_at, s.last_activity, s.is_active,
                   COUNT(m.message_id) as total_messages,
                   SUM(CASE WHEN m.is_read = 0 THEN 1 ELSE 0 END) as unread_messages
            FROM sessions s
            LEFT JOIN messages m ON s.session_id = m.session_id
            GROUP BY s.session_id
            ORDER BY s.last_activity DESC
        ''')
        
        rows = cursor.fetchall()
        sessions = []
        
        for row in rows:
            sessions.append({
                'session_id': row[0],
                'email': row[1],
                'created_at': row[2],
                'last_activity': row[3],
                'is_active': bool(row[4]),
                'total_messages': row[5] or 0,
                'unread_messages': row[6] or 0,
                'active': row[0] in active_sessions
            })
        
        return jsonify({'success': True, 'sessions': sessions})

@app.route('/activate-session', methods=['POST'])
def activate_session_endpoint():
    data = request.json
    session_id = data.get('session_id')
    
    if not session_id:
        return jsonify({'success': False, 'error': 'Missing session ID'})
    
    with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
        cursor = conn.execute('SELECT email FROM sessions WHERE session_id = ?', (session_id,))
        row = cursor.fetchone()
        
        if row:
            # Reactivate session
            email = row[0]
            session = TempMailSession(email, session_id)
            
            return jsonify({
                'success': True,
                'email': email,
                'session_id': session_id
            })
    
    return jsonify({'success': False, 'error': 'Session not found'})

@app.route('/get-session-stats', methods=['POST'])
def get_session_stats_endpoint():
    data = request.json
    session_id = data.get('session_id')
    
    if not session_id:
        return jsonify({'success': False, 'error': 'Missing session ID'})
    
    stats = get_session_stats(session_id)
    return jsonify({'success': True, 'stats': stats})

@app.route('/get-stats', methods=['POST'])
def get_stats_endpoint():
    with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
        # Total messages
        cursor = conn.execute('SELECT COUNT(*) FROM messages')
        total_messages = cursor.fetchone()[0] or 0
        
        # Unread messages
        cursor = conn.execute('SELECT COUNT(*) FROM messages WHERE is_read = 0')
        unread_messages = cursor.fetchone()[0] or 0
        
        # Active sessions
        cursor = conn.execute('SELECT COUNT(*) FROM sessions WHERE is_active = 1')
        active_sessions_count = cursor.fetchone()[0] or 0
        
        # Deleted messages (estimated)
        deleted_messages = max(0, total_messages - unread_messages - 100)
        
        return jsonify({
            'success': True,
            'stats': {
                'total_messages': total_messages,
                'unread_messages': unread_messages,
                'active_sessions': active_sessions_count,
                'deleted_messages': deleted_messages
            }
        })

@app.route('/export-messages', methods=['POST'])
def export_messages_endpoint():
    data = request.json
    session_id = data.get('session_id')
    format_type = data.get('format', 'json')
    
    if not session_id:
        return jsonify({'success': False, 'error': 'Missing session ID'})
    
    content = export_messages(session_id, format_type)
    
    mime_types = {
        'json': 'application/json',
        'csv': 'text/csv',
        'txt': 'text/plain'
    }
    
    return jsonify({
        'success': True,
        'content': content,
        'format': format_type,
        'mime_type': mime_types.get(format_type, 'text/plain')
    })

@app.route('/clear-database', methods=['POST'])
def clear_database_endpoint():
    try:
        with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
            conn.execute('DELETE FROM messages')
            conn.execute('DELETE FROM sessions')
            conn.execute('DELETE FROM archives')
            conn.commit()
        
        # Clear active sessions
        active_sessions.clear()
        message_cache.clear()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/delete-inactive', methods=['POST'])
def delete_inactive_endpoint():
    try:
        with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
            # Find inactive sessions (older than 24 hours)
            cutoff = datetime.utcnow() - timedelta(hours=24)
            
            cursor = conn.execute('''
                SELECT session_id FROM sessions 
                WHERE is_active = 0 AND last_activity < ?
            ''', (cutoff,))
            
            inactive_sessions = cursor.fetchall()
            
            # Delete messages for inactive sessions
            for session in inactive_sessions:
                session_id = session[0]
                conn.execute('DELETE FROM messages WHERE session_id = ?', (session_id,))
                conn.execute('DELETE FROM sessions WHERE session_id = ?', (session_id,))
                
                # Remove from active sessions if present
                if session_id in active_sessions:
                    del active_sessions[session_id]
            
            conn.commit()
            
            return jsonify({
                'success': True,
                'deleted_count': len(inactive_sessions)
            })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/clear-all-sessions', methods=['POST'])
def clear_all_sessions_endpoint():
    try:
        # Deactivate all sessions
        for session_id in list(active_sessions.keys()):
            active_sessions[session_id].deactivate()
        
        active_sessions.clear()
        
        with closing(sqlite3.connect('temp_mail.db', check_same_thread=False)) as conn:
            conn.execute('UPDATE sessions SET is_active = 0')
            conn.commit()
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/download-db')
def download_db():
    return send_file('temp_mail.db', as_attachment=True)

# Vercel deployment compatibility
if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))