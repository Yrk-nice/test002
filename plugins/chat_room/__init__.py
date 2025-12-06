from flask import Blueprint, render_template, request, jsonify, current_app, session
from app.utils import get_db_connection, call_ai_messages
import datetime
import json
import threading

bp = Blueprint('chat_room', __name__, template_folder='templates', url_prefix='/chat')

def db_conn():
    return get_db_connection(current_app.config['DB_PATH'])

# Simple in-memory lock for AI processing to avoid double processing if multiple workers (though this is dev server)
ai_lock = threading.Lock()

@bp.route('/')
def index():
    if not session.get('user_id'):
        return current_app.response_class(status=302, headers={'Location': '/login'})
    
    conn = db_conn()
    users = conn.execute('SELECT id, username FROM users WHERE is_active=1').fetchall()
    conn.close()
    
    return render_template('chat_room.html', users=users, current_user_id=session.get('user_id'), current_username=session.get('username'))

@bp.route('/api/messages', methods=['GET'])
def get_messages():
    if not session.get('user_id'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
        
    target_id = request.args.get('target_id') # If None, it's group chat. If set, it's private chat with this user ID.
    last_id = request.args.get('last_id', 0)
    
    conn = db_conn()
    try:
        if target_id:
            # Private chat: messages between current user and target user
            uid = session.get('user_id')
            tid = int(target_id)
            sql = '''
                SELECT m.id, m.sender_id, u.username as sender_name, m.receiver_id, m.content, m.created_at, m.is_ai
                FROM chat_messages m
                LEFT JOIN users u ON m.sender_id = u.id
                WHERE m.id > ? AND (
                    (m.sender_id = ? AND m.receiver_id = ?) OR 
                    (m.sender_id = ? AND m.receiver_id = ?)
                )
                ORDER BY m.id ASC LIMIT 50
            '''
            rows = conn.execute(sql, (last_id, uid, tid, tid, uid)).fetchall()
        else:
            # Group chat: messages with receiver_id IS NULL
            sql = '''
                SELECT m.id, m.sender_id, u.username as sender_name, m.receiver_id, m.content, m.created_at, m.is_ai
                FROM chat_messages m
                LEFT JOIN users u ON m.sender_id = u.id
                WHERE m.id > ? AND m.receiver_id IS NULL
                ORDER BY m.id ASC LIMIT 50
            '''
            rows = conn.execute(sql, (last_id,)).fetchall()
            
        msgs = []
        for r in rows:
            msgs.append({
                'id': r['id'],
                'sender_id': r['sender_id'],
                'sender_name': r['sender_name'] or ('AI助手' if r['is_ai'] else 'Unknown'),
                'content': r['content'],
                'created_at': r['created_at'],
                'is_ai': r['is_ai']
            })
        return jsonify({'ok': True, 'messages': msgs})
    finally:
        conn.close()

@bp.route('/api/send', methods=['POST'])
def send_message():
    if not session.get('user_id'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
        
    payload = request.get_json(silent=True) or {}
    content = (payload.get('content') or '').strip()
    target_id = payload.get('target_id')
    
    if not content:
        return jsonify({'ok': False, 'error': 'Empty content'}), 400
        
    conn = db_conn()
    try:
        sender_id = session.get('user_id')
        receiver_id = int(target_id) if target_id else None
        now = datetime.datetime.utcnow().isoformat()
        
        # Save user message
        cursor = conn.execute('INSERT INTO chat_messages (sender_id, receiver_id, content, created_at, is_ai) VALUES (?, ?, ?, ?, 0)',
                     (sender_id, receiver_id, content, now))
        conn.commit()
        
        # Check for @ai
        if '@ai' in content.lower():
            # Trigger AI response
            # We'll do it synchronously for simplicity, though async task would be better for perf.
            # But since we use polling frontend, synchronous is okay-ish if AI is fast enough, 
            # or we can spawn a thread.
            
            # Let's spawn a thread to handle AI response to avoid blocking the send request too long
            threading.Thread(target=handle_ai_response, args=(current_app._get_current_object(), sender_id, receiver_id, content)).start()
            
        return jsonify({'ok': True})
    finally:
        conn.close()

def handle_ai_response(app, sender_id, receiver_id, user_content):
    with app.app_context():
        conn = get_db_connection(app.config['DB_PATH'])
        try:
            reply = ""
            # Check for weather command
            # Pattern: @ai天气[city] or @ai 天气 [city]
            import re
            weather_match = re.search(r'@ai\s*天气\s*\[(.*?)\]', user_content, re.IGNORECASE)
            if not weather_match:
                 # Try simpler pattern like @ai 天气 北京
                 weather_match = re.search(r'@ai\s*天气\s+(.*)', user_content, re.IGNORECASE)
            
            if weather_match:
                city = weather_match.group(1).strip()
                if not city:
                    reply = "请提供城市名称，例如：@ai天气[北京]"
                else:
                    # Call Weather API
                    try:
                        import requests
                        api_url = "https://v2.xxapi.cn/api/weather"
                        params = {'city': city, 'key': 'c1fb5580b0740252'}
                        headers = {'User-Agent': 'xiaoxiaoapi/1.0.0'}
                        
                        resp = requests.get(api_url, params=params, headers=headers, timeout=10)
                        if resp.status_code == 200:
                            data = resp.json()
                            if data.get('code') == 200 and 'data' in data:
                                wdata = data['data']
                                # Build weather card JSON string, frontend will render it
                                # We can use a special prefix to tell frontend it's a card
                                # Or just return a JSON structure in content and have a type field?
                                # Current schema only has content TEXT.
                                # We will use a JSON string with a specific prefix `JSON:` or just store JSON.
                                # Let's wrap it in a special marker.
                                reply = json.dumps({
                                    'type': 'weather_card',
                                    'city': wdata.get('city', city),
                                    'forecast': wdata.get('data', [])
                                }, ensure_ascii=False)
                            else:
                                reply = f"天气查询失败: {data.get('msg', '未知错误')}"
                        else:
                            reply = f"天气API请求失败: {resp.status_code}"
                    except Exception as e:
                        reply = f"天气服务异常: {str(e)}"
            
            # If not weather command, use AI engine
            if not reply:
                # Get AI engine
                engine = conn.execute('SELECT * FROM ai_engines ORDER BY id DESC LIMIT 1').fetchone()
                if not engine:
                    reply = "抱歉，未配置AI引擎，无法回复。"
                else:
                    # Remove @ai from content
                    prompt = user_content.replace('@ai', '').replace('@AI', '').strip()
                    if not prompt:
                        reply = "我在，请问有什么可以帮您？"
                    else:
                        ok, ai_reply = call_ai_messages(engine, [{'role': 'user', 'content': prompt}])
                        if not ok:
                            reply = f"AI响应失败: {ai_reply}"
                        else:
                            reply = ai_reply
            
            # AI reply target logic (same as before)
            ai_receiver_id = receiver_id 
            
            if ai_receiver_id is None:
                # Group chat
                now = datetime.datetime.utcnow().isoformat()
                conn.execute('INSERT INTO chat_messages (sender_id, receiver_id, content, created_at, is_ai) VALUES (?, ?, ?, ?, 1)',
                             (0, None, reply, now))
                conn.commit()
            else:
                # Private chat logic... skipped for now as per previous turn decision (MVP: Group Chat)
                pass
                
        except Exception as e:
            print(f"AI Error: {e}")
        finally:
            conn.close()

def init_plugin_db(app):
    with app.app_context():
        conn = get_db_connection(app.config['DB_PATH'])
        conn.execute('''
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER,
                receiver_id INTEGER,
                content TEXT,
                is_ai INTEGER DEFAULT 0,
                created_at TEXT
            )
        ''')
        
        # Register menu
        # Check if '聊天室' exists
        row = conn.execute("SELECT id FROM menus WHERE endpoint='chat_room.index'").fetchone()
        if not row:
            # Find '系统管理' id
            sys_row = conn.execute("SELECT id FROM menus WHERE name='系统管理'").fetchone()
            parent_id = sys_row['id'] if sys_row else None
            now = datetime.datetime.utcnow().isoformat()
            conn.execute("INSERT INTO menus (name, endpoint, parent_id, order_num, is_visible, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         ('在线聊天', 'chat_room.index', parent_id, 90, 1, now, now))
            conn.commit()
        conn.close()
