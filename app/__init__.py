import os
import sqlite3
import functools
import datetime
import re
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
import json
from urllib.parse import urlparse
from .crawler import crawl_baidu_news, deep_fetch_text, deep_fetch_with_rule, fetch_page_text, suggest_content_xpath
from .utils import get_db_connection, call_ai as call_ai_util, call_ai_messages as call_ai_messages_util


def create_app():
    app = Flask(__name__, static_folder='../static', template_folder='../templates')
    app.config['SECRET_KEY'] = os.environ.get('APP_SECRET_KEY', 'dev-secret')
    app.config['DB_PATH'] = os.path.join(os.path.dirname(__file__), 'app.db')
    app.permanent_session_lifetime = datetime.timedelta(days=7)

    def db_conn():
        return get_db_connection(app.config['DB_PATH'])


    def ensure_menus_schema():
        conn = db_conn()
        try:
            cols = conn.execute('PRAGMA table_info(menus)').fetchall()
            names = [r['name'] for r in cols] if cols else []
            # If missing required columns, just drop and recreate (simplest for dev, or alter add)
            # Let's try adding columns first to preserve data if possible, but for 'menus' structure change, drop might be cleaner if we want to reset to default structure.
            # Given the user instruction "OperationalError", it's safer to ensure the table has correct columns.
            if 'parent_id' not in names or 'is_visible' not in names or 'updated_at' not in names:
                # Check if it's the old schema (id, name, endpoint, order_num, role_required, created_at)
                # We will just drop and re-init to ensure consistency with the new requirement
                conn.execute('DROP TABLE IF EXISTS menus')
                conn.execute(
                    'CREATE TABLE menus (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, endpoint TEXT, parent_id INTEGER, order_num INTEGER DEFAULT 0, is_visible INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT)'
                )
                # Re-init data
                now = datetime.datetime.utcnow().isoformat()
                conn.execute("INSERT INTO menus (name, endpoint, parent_id, order_num, created_at, updated_at) VALUES ('首页', 'dashboard', NULL, 1, ?, ?)", (now, now))
                conn.execute("INSERT INTO menus (name, endpoint, parent_id, order_num, created_at, updated_at) VALUES ('系统管理', '#', NULL, 2, ?, ?)", (now, now))
                sys_id = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
                
                items = [
                    ('用户管理', 'admin_users'),
                    ('菜单管理', 'admin_menus_list'),
                    ('角色管理', 'admin_roles'),
                    ('系统设置', 'admin_settings'),
                    ('采集规则库', 'admin_rules_list'),
                    ('AI数据清洗分析', 'admin_clean'),
                    ('AI引擎管理', 'admin_engines_list'),
                    ('爬虫管理', 'admin_crawlers_list'),
                    ('数据大屏', 'data_screen'),
                    ('数据采集管理', 'crawl_page'),
                    ('数据仓库管理', 'warehouse_list')
                ]
                for idx, (name, ep) in enumerate(items):
                    conn.execute("INSERT INTO menus (name, endpoint, parent_id, order_num, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)", (name, ep, sys_id, idx+1, now, now))
                conn.commit()
        finally:
            conn.close()

    def init_db():
        conn = db_conn()
        c = conn.cursor()
        # ... existing tables ...

        c.execute(
            'CREATE TABLE IF NOT EXISTS roles (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL, description TEXT)'
        )
        c.execute(
            'CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, role_id INTEGER, is_active INTEGER DEFAULT 1, created_at TEXT, FOREIGN KEY(role_id) REFERENCES roles(id))'
        )
        c.execute(
            'CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)'
        )
        c.execute(
            'CREATE TABLE IF NOT EXISTS articles (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, summary TEXT, cover_url TEXT, original_url TEXT, source TEXT, keyword TEXT, created_at TEXT)'
        )
        c.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_url ON articles(original_url)')
        try:
            c.execute('ALTER TABLE articles ADD COLUMN deep_done INTEGER DEFAULT 0')
        except Exception:
            pass
        try:
            c.execute('ALTER TABLE articles ADD COLUMN deep_content TEXT')
        except Exception:
            pass
        try:
            c.execute('ALTER TABLE articles ADD COLUMN deep_fetched_at TEXT')
        except Exception:
            pass
        try:
            c.execute('ALTER TABLE articles ADD COLUMN ai_analysis TEXT')
        except Exception:
            pass
        try:
            c.execute('ALTER TABLE articles ADD COLUMN ai_analyzed_at TEXT')
        except Exception:
            pass
        c.execute(
            'CREATE TABLE IF NOT EXISTS crawl_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT UNIQUE NOT NULL, title_xpath TEXT, content_xpath TEXT, headers_json TEXT, created_at TEXT, updated_at TEXT)'
        )
        c.execute(
            'CREATE TABLE IF NOT EXISTS ai_engines (id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, api_url TEXT NOT NULL, api_key TEXT NOT NULL, model_name TEXT NOT NULL, created_at TEXT, updated_at TEXT)'
        )
        c.execute(
            'CREATE TABLE IF NOT EXISTS crawler_configs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, base_url TEXT, start_url TEXT, enabled INTEGER DEFAULT 1, headers_json TEXT, notes TEXT, created_at TEXT, updated_at TEXT)'
        )
        c.execute(
            'CREATE TABLE IF NOT EXISTS article_details (id INTEGER PRIMARY KEY AUTOINCREMENT, article_id INTEGER, title TEXT, content TEXT, source TEXT, region TEXT, keywords TEXT, created_at TEXT)'
        )
        c.execute(
            'CREATE TABLE IF NOT EXISTS menus (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, endpoint TEXT, parent_id INTEGER, order_num INTEGER DEFAULT 0, is_visible INTEGER DEFAULT 1, created_at TEXT, updated_at TEXT)'
        )
        # Initialize Menus if empty
        c.execute('SELECT count(*) FROM menus')
        if c.fetchone()[0] == 0:
            now = datetime.datetime.utcnow().isoformat()
            c.execute("INSERT INTO menus (name, endpoint, parent_id, order_num, created_at, updated_at) VALUES ('首页', 'dashboard', NULL, 1, ?, ?)", (now, now))
            c.execute("INSERT INTO menus (name, endpoint, parent_id, order_num, created_at, updated_at) VALUES ('系统管理', '#', NULL, 2, ?, ?)", (now, now))
            sys_id = c.lastrowid
            
            items = [
                ('用户管理', 'admin_users'),
                ('菜单管理', 'admin_menus_list'),
                ('角色管理', 'admin_roles'),
                ('系统设置', 'admin_settings'),
                ('采集规则库', 'admin_rules_list'),
                ('AI数据清洗分析', 'admin_clean'),
                ('AI引擎管理', 'admin_engines_list'),
                ('爬虫管理', 'admin_crawlers_list'),
                ('数据大屏', 'data_screen'),
                ('数据采集管理', 'crawl_page'),
                ('数据仓库管理', 'warehouse_list')
            ]
            for idx, (name, ep) in enumerate(items):
                c.execute("INSERT INTO menus (name, endpoint, parent_id, order_num, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)", (name, ep, sys_id, idx+1, now, now))
        
        conn.commit()
        c.execute('SELECT id FROM roles WHERE name=?', ('admin',))
        if not c.fetchone():
            c.execute('INSERT INTO roles (name, description) VALUES (?, ?)', ('admin', '管理员'))
        c.execute('SELECT id FROM roles WHERE name=?', ('user',))
        if not c.fetchone():
            c.execute('INSERT INTO roles (name, description) VALUES (?, ?)', ('user', '普通用户'))
        conn.commit()
        c.execute('SELECT id FROM users WHERE username=?', ('admin',))
        if not c.fetchone():
            c.execute('SELECT id FROM roles WHERE name=?', ('admin',))
            role_id = c.fetchone()['id']
            ph = generate_password_hash('admin123')
            c.execute(
                'INSERT INTO users (username, password_hash, role_id, created_at) VALUES (?, ?, ?, ?)',
                ('admin', ph, role_id, datetime.datetime.utcnow().isoformat()),
            )
        c.execute('SELECT value FROM settings WHERE key=?', ('app_name',))
        if not c.fetchone():
            c.execute('INSERT INTO settings (key, value) VALUES (?, ?)', ('app_name', '政企智能舆情分析报告生成智能体应用系统'))
        conn.close()

    def get_setting(key, default=''):
        conn = db_conn()
        row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
        conn.close()
        return row['value'] if row else default

    def set_setting(key, value):
        conn = db_conn()
        conn.execute('INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, value))
        conn.commit()
        conn.close()

    def ensure_rules_schema():
        conn = db_conn()
        try:
            cols = conn.execute('PRAGMA table_info(crawl_rules)').fetchall()
            names = [r['name'] for r in cols] if cols else []
            if ('site' not in names) or ('headers_json' not in names) or ('title_xpath' not in names) or ('content_xpath' not in names):
                conn.execute('DROP TABLE IF EXISTS crawl_rules')
                conn.execute('CREATE TABLE crawl_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT UNIQUE NOT NULL, title_xpath TEXT, content_xpath TEXT, headers_json TEXT, created_at TEXT, updated_at TEXT)')
                conn.commit()
        finally:
            conn.close()

    def login_required(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get('user_id'):
                return redirect(url_for('login'))
            return fn(*args, **kwargs)
        return wrapper

    def admin_required(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not session.get('user_id'):
                return redirect(url_for('login'))
            if session.get('role_name') != 'admin':
                return redirect(url_for('dashboard'))
            return fn(*args, **kwargs)
        return wrapper

    @app.context_processor
    def inject_settings():
        try:
            logo = get_setting('logo_path', '')
            name = get_setting('app_name', '应用')
            url = url_for('static', filename=logo) if logo else ''
            
            menu_tree = []
            if session.get('user_id'):
                conn = db_conn()
                try:
                    all_menus = conn.execute('SELECT * FROM menus WHERE is_visible=1 ORDER BY order_num ASC').fetchall()
                finally:
                    conn.close()
                
                menu_map = {}
                for m in all_menus:
                    d = dict(m)
                    d['children'] = []
                    menu_map[m['id']] = d
                
                for m in all_menus:
                    d = menu_map[m['id']]
                    pid = m['parent_id']
                    if pid and pid in menu_map:
                        menu_map[pid]['children'].append(d)
                    elif not pid:
                        menu_tree.append(d)
                
                # Filter based on role
                final_tree = []
                role = session.get('role_name', 'user')
                valid_eps = set(app.view_functions.keys())
                valid_eps.add('#')
                
                for item in menu_tree:
                    ep = item.get('endpoint')
                    if ep and ep not in valid_eps:
                        continue

                    # Check item permission (simple convention: admin_ prefix requires admin role)
                    if 'admin_' in (ep or '') and role != 'admin':
                        continue
                    
                    # Check children
                    if item.get('children'):
                        valid_children = []
                        for child in item['children']:
                            cep = child.get('endpoint')
                            if cep and cep not in valid_eps:
                                continue
                            if 'admin_' in (cep or '') and role != 'admin':
                                continue
                            valid_children.append(child)
                        item['children'] = valid_children
                        
                        # If parent is just a container (no endpoint) and has no visible children, skip it
                        if (not ep or ep == '#') and not valid_children:
                            continue
                                
                    final_tree.append(item)
                menu_tree = final_tree
                         
            return dict(app_name=name, app_logo_url=url, current_user=session.get('username', ''), menu_tree=menu_tree)
        except Exception as e:
            with open('debug_error.log', 'a') as f:
                import traceback
                f.write(traceback.format_exc())
            return dict(app_name='Error', app_logo_url='', current_user='', menu_tree=[])

    @app.route('/')
    def root():
        if session.get('user_id'):
            return redirect(url_for('dashboard'))
        return redirect(url_for('login'))

    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            remember = bool(request.form.get('remember'))
            conn = db_conn()
            user = conn.execute('SELECT users.id, users.password_hash, roles.name AS role_name FROM users LEFT JOIN roles ON users.role_id=roles.id WHERE users.username=? AND users.is_active=1', (username,)).fetchone()
            conn.close()
            if not user or not check_password_hash(user['password_hash'], password):
                return render_template('login.html', error='用户名或密码错误')
            session['user_id'] = user['id']
            session['username'] = username
            session['role_name'] = user['role_name'] or 'user'
            session.permanent = remember
            return redirect(url_for('dashboard'))
        return render_template('login.html')

    @app.route('/logout')
    def logout():
        session.clear()
        return redirect(url_for('login'))

    @app.route('/dashboard')
    @login_required
    def dashboard():
        return render_template('dashboard.html')

    @app.route('/crawl')
    @login_required
    def crawl_page():
        return render_template('crawl.html')

    @app.route('/api/crawl')
    def api_crawl():
        q = request.args.get('q', '').strip()
        if not q:
            return jsonify([])
        delay_ms = request.args.get('delay_ms')
        dms = int(delay_ms) if delay_ms and delay_ms.isdigit() else None
        data = crawl_baidu_news(q, limit=int(request.args.get('limit', '10')), delay_ms=dms)
        return jsonify(data)

    @app.route('/api/articles/save', methods=['POST'])
    @login_required
    def api_articles_save():
        payload = request.get_json(silent=True) or {}
        items = payload.get('items') or []
        keyword = payload.get('keyword') or ''
        conn = db_conn()
        for it in items:
            try:
                title = (it.get('title') or '').strip()
                url = (it.get('url') or '').strip()
                if not title or not url:
                    continue
                conn.execute(
                    'INSERT OR IGNORE INTO articles (title, summary, cover_url, original_url, source, keyword, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
                    (
                        title,
                        it.get('summary') or '',
                        it.get('cover') or '',
                        url,
                        it.get('source') or '',
                        keyword,
                        datetime.datetime.utcnow().isoformat(),
                    ),
                )
            except Exception:
                pass
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'count': len(items)})

    @app.route('/articles')
    @login_required
    def articles_list():
        conn = db_conn()
        rows = conn.execute('SELECT id, title, source, original_url, created_at, deep_done FROM articles ORDER BY id DESC LIMIT 200').fetchall()
        conn.close()
        
        results = []
        for r in rows:
            item = dict(r)
            if not item['source'] and item.get('original_url'):
                try:
                    item['source'] = urlparse(item['original_url']).netloc
                except Exception:
                    pass
            results.append(item)
            
        return render_template('articles_list.html', rows=results)

    @app.route('/articles/delete/<int:article_id>', methods=['POST'])
    @admin_required
    def articles_delete(article_id):
        conn = db_conn()
        try:
            conn.execute('DELETE FROM articles WHERE id=?', (article_id,))
            conn.commit()
        except Exception:
            pass
        conn.close()
        return redirect(url_for('articles_list'))

    @app.route('/api/articles/deep/<int:article_id>', methods=['POST'])
    @login_required
    def api_articles_deep(article_id):
        conn = db_conn()
        row = conn.execute('SELECT id, original_url FROM articles WHERE id=?', (article_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'ok': False, 'error': 'not_found'}), 404
        url = row['original_url']
        try:
            host = urlparse(url).netloc
            rule = conn.execute('SELECT site, title_xpath, content_xpath, headers_json FROM crawl_rules WHERE site=?', (host,)).fetchone()
            if not rule:
                rule = conn.execute('SELECT site, title_xpath, content_xpath, headers_json FROM crawl_rules WHERE ? LIKE "%" || site || "%"', (host,)).fetchone()
            if rule:
                headers_override = None
                if rule['headers_json']:
                    try:
                        headers_override = json.loads(rule['headers_json'])
                    except Exception:
                        headers_override = None
                title, content = deep_fetch_with_rule(url, headers_override, rule['title_xpath'], rule['content_xpath'])
            else:
                title, content = None, deep_fetch_text(url)
            if content and content.strip():
                if title and title.strip():
                    conn.execute('UPDATE articles SET title=? WHERE id=?', (title.strip(), article_id))
                conn.execute('UPDATE articles SET deep_done=1, deep_content=?, deep_fetched_at=? WHERE id=?', (content.strip(), datetime.datetime.utcnow().isoformat(), article_id))
                conn.commit()
                conn.close()
                return jsonify({'ok': True})
            conn.close()
            return jsonify({'ok': False, 'error': 'empty'}), 500
        except Exception:
            conn.close()
            return jsonify({'ok': False}), 500

    @app.route('/warehouse')
    @login_required
    def warehouse_list():
        conn = db_conn()
        q = request.args.get('q', '').strip()
        if q:
            rows = conn.execute('SELECT id, title, source, original_url, created_at, deep_done FROM articles WHERE title LIKE ? OR source LIKE ? OR keyword LIKE ? ORDER BY id DESC LIMIT 500', (f'%{q}%', f'%{q}%', f'%{q}%')).fetchall()
        else:
            rows = conn.execute('SELECT id, title, source, original_url, created_at, deep_done FROM articles ORDER BY id DESC LIMIT 500').fetchall()
        conn.close()
        
        results = []
        for r in rows:
            item = dict(r)
            if not item['source'] and item.get('original_url'):
                try:
                    item['source'] = urlparse(item['original_url']).netloc
                except Exception:
                    pass
            results.append(item)

        return render_template('warehouse_list.html', rows=results, q=q)

    @app.route('/warehouse/edit/<int:article_id>', methods=['GET', 'POST'])
    @admin_required
    def warehouse_edit(article_id):
        conn = db_conn()
        if request.method == 'POST':
            title = request.form.get('title', '').strip()
            summary = request.form.get('summary', '').strip()
            source = request.form.get('source', '').strip()
            cover_url = request.form.get('cover_url', '').strip()
            keyword = request.form.get('keyword', '').strip()
            deep_content = request.form.get('deep_content', '').strip()
            conn.execute('UPDATE articles SET title=?, summary=?, source=?, cover_url=?, keyword=?, deep_content=? WHERE id=?', (title, summary, source, cover_url, keyword, deep_content, article_id))
            conn.commit()
            conn.close()
            return redirect(url_for('warehouse_list'))
        row = conn.execute('SELECT id, title, summary, source, cover_url, original_url, keyword, deep_content FROM articles WHERE id=?', (article_id,)).fetchone()
        conn.close()
        return render_template('warehouse_edit.html', row=row)

    @app.route('/warehouse/delete/<int:article_id>', methods=['POST'])
    @admin_required
    def warehouse_delete(article_id):
        conn = db_conn()
        try:
            conn.execute('DELETE FROM articles WHERE id=?', (article_id,))
            conn.commit()
        except Exception:
            pass
        conn.close()
        return redirect(url_for('warehouse_list'))

    @app.route('/api/warehouse/analyze/<int:article_id>', methods=['POST'])
    @admin_required
    def warehouse_analyze(article_id):
        conn = db_conn()
        row = conn.execute('SELECT id FROM articles WHERE id=?', (article_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'ok': False, 'error': 'not_found'}), 404
        conn.execute('UPDATE articles SET ai_analysis=?, ai_analyzed_at=? WHERE id=?', ('', datetime.datetime.utcnow().isoformat(), article_id))
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'msg': 'placeholder'})

    def find_rule_for(source: str, url: str):
        conn = db_conn()
        host = urlparse(url).netloc
        rules = conn.execute('SELECT site, title_xpath, content_xpath, headers_json FROM crawl_rules ORDER BY id DESC').fetchall()
        conn.close()
        for r in rules:
            site = r['site'] or ''
            if source and (source in site or site in source):
                return r
            if host and (host in site or site in host):
                return r
        return None

    @app.route('/api/warehouse/preview/<int:article_id>')
    @login_required
    def api_warehouse_preview(article_id):
        conn = db_conn()
        row = conn.execute('SELECT id, title, source, original_url FROM articles WHERE id=?', (article_id,)).fetchone()
        conn.close()
        if not row:
            return jsonify({'ok': False, 'error': 'not_found'}), 404
        rule = find_rule_for(row['source'], row['original_url'])
        headers_override = None
        if rule and rule['headers_json']:
            try:
                headers_override = json.loads(rule['headers_json'])
            except Exception:
                headers_override = None
        if rule:
            t, c = deep_fetch_with_rule(row['original_url'], headers_override, rule['title_xpath'], rule['content_xpath'])
        else:
            t, c = None, deep_fetch_text(row['original_url'])
        return jsonify({'ok': True, 'title': t or row['title'], 'content': c or ''})

    @app.route('/api/warehouse/collect/<int:article_id>', methods=['POST'])
    @login_required
    def api_warehouse_collect(article_id):
        conn = db_conn()
        row = conn.execute('SELECT id, title, source, original_url FROM articles WHERE id=?', (article_id,)).fetchone()
        if not row:
            conn.close()
            return jsonify({'ok': False, 'error': 'not_found'}), 404
        rule = find_rule_for(row['source'], row['original_url'])
        headers_override = None
        title_xpath = None
        content_xpath = None
        if rule:
            title_xpath = rule['title_xpath']
            content_xpath = rule['content_xpath']
            if rule['headers_json']:
                try:
                    headers_override = json.loads(rule['headers_json'])
                except Exception:
                    headers_override = None
        # 尝试规则采集
        t, c = deep_fetch_with_rule(row['original_url'], headers_override, title_xpath, content_xpath) if rule else (None, deep_fetch_text(row['original_url']))
        if rule and (not c or not c.strip()):
            pass
        # 若无正文，则尝试建议 XPath 并自动更新规则
        if (not c or not c.strip()) and rule:
            page_text = fetch_page_text(row['original_url'], headers_override)
            sx = suggest_content_xpath(page_text)
            if sx and sx != content_xpath:
                conn2 = db_conn()
                conn2.execute('UPDATE crawl_rules SET content_xpath=?, updated_at=? WHERE site=?', (sx, datetime.datetime.utcnow().isoformat(), rule['site']))
                conn2.commit()
                conn2.close()
                # 用新规则再试一次
                t, c = deep_fetch_with_rule(row['original_url'], headers_override, title_xpath, sx)
        # 保存结果
        if c and c.strip():
            if t and t.strip():
                conn.execute('UPDATE articles SET title=? WHERE id=?', (t.strip(), article_id))
            conn.execute('UPDATE articles SET deep_done=1, deep_content=?, deep_fetched_at=? WHERE id=?', (c.strip(), datetime.datetime.utcnow().isoformat(), article_id))
            conn.commit()
            conn.close()
            return jsonify({'ok': True})
        conn.close()
        return jsonify({'ok': False, 'error': 'empty'})

    @app.route('/admin/rules', methods=['GET'])
    @admin_required
    def admin_rules_list():
        ensure_rules_schema()
        conn = db_conn()
        rows = conn.execute('SELECT id, site, title_xpath, content_xpath, headers_json, updated_at FROM crawl_rules ORDER BY id DESC').fetchall()
        conn.close()
        return render_template('admin_rules_list.html', rows=rows)

    @app.route('/admin/rules/new', methods=['GET', 'POST'])
    @admin_required
    def admin_rules_new():
        if request.method == 'POST':
            site = request.form.get('site', '').strip()
            title_xpath = request.form.get('title_xpath', '').strip()
            content_xpath = request.form.get('content_xpath', '').strip()
            headers_json = request.form.get('headers_json', '').strip()
            if not site:
                return render_template('admin_rules_edit.html', row=None, error='站点不能为空')
            # 校验 headers_json
            if headers_json:
                try:
                    json.loads(headers_json)
                except Exception:
                    return render_template('admin_rules_edit.html', row=None, error='Headers 必须是合法的 JSON')
            # 规范化站点，允许输入完整URL
            try:
                p = urlparse(site)
                if p.scheme and p.netloc:
                    site = p.netloc
            except Exception:
                pass
            ensure_rules_schema()
            conn = db_conn()
            try:
                conn.execute(
                    'CREATE TABLE IF NOT EXISTS crawl_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, site TEXT UNIQUE NOT NULL, title_xpath TEXT, content_xpath TEXT, headers_json TEXT, created_at TEXT, updated_at TEXT)'
                )
                conn.execute('INSERT OR IGNORE INTO crawl_rules (site, title_xpath, content_xpath, headers_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)', (site, title_xpath, content_xpath, headers_json, datetime.datetime.utcnow().isoformat(), datetime.datetime.utcnow().isoformat()))
                conn.commit()
                conn.close()
                return redirect(url_for('admin_rules_list'))
            except Exception as e:
                conn.close()
                return render_template('admin_rules_edit.html', row=None, error='保存失败：' + str(e))
        return render_template('admin_rules_edit.html', row=None)

    @app.route('/admin/rules/edit/<int:rule_id>', methods=['GET', 'POST'])
    @admin_required
    def admin_rules_edit(rule_id):
        ensure_rules_schema()
        conn = db_conn()
        if request.method == 'POST':
            site = request.form.get('site', '').strip()
            title_xpath = request.form.get('title_xpath', '').strip()
            content_xpath = request.form.get('content_xpath', '').strip()
            headers_json = request.form.get('headers_json', '').strip()
            if not site:
                row = conn.execute('SELECT id, site, title_xpath, content_xpath, headers_json FROM crawl_rules WHERE id=?', (rule_id,)).fetchone()
                conn.close()
                return render_template('admin_rules_edit.html', row=row, error='站点不能为空')
            if headers_json:
                try:
                    json.loads(headers_json)
                except Exception:
                    row = conn.execute('SELECT id, site, title_xpath, content_xpath, headers_json FROM crawl_rules WHERE id=?', (rule_id,)).fetchone()
                    conn.close()
                    return render_template('admin_rules_edit.html', row=row, error='Headers 必须是合法的 JSON')
            # 规范化站点
            try:
                p = urlparse(site)
                if p.scheme and p.netloc:
                    site = p.netloc
            except Exception:
                pass
            try:
                conn.execute('UPDATE crawl_rules SET site=?, title_xpath=?, content_xpath=?, headers_json=?, updated_at=? WHERE id=?', (site, title_xpath, content_xpath, headers_json, datetime.datetime.utcnow().isoformat(), rule_id))
                conn.commit()
                conn.close()
                return redirect(url_for('admin_rules_list'))
            except Exception as e:
                row = conn.execute('SELECT id, site, title_xpath, content_xpath, headers_json FROM crawl_rules WHERE id=?', (rule_id,)).fetchone()
                conn.close()
                return render_template('admin_rules_edit.html', row=row, error='保存失败：' + str(e))
        row = conn.execute('SELECT id, site, title_xpath, content_xpath, headers_json FROM crawl_rules WHERE id=?', (rule_id,)).fetchone()
        conn.close()
        return render_template('admin_rules_edit.html', row=row)

    @app.route('/admin/rules/delete/<int:rule_id>', methods=['POST'])
    @admin_required
    def admin_rules_delete(rule_id):
        conn = db_conn()
        try:
            conn.execute('DELETE FROM crawl_rules WHERE id=?', (rule_id,))
            conn.commit()
        except Exception:
            pass
        conn.close()
        return redirect(url_for('admin_rules_list'))

    @app.route('/admin/engines', methods=['GET'])
    @admin_required
    def admin_engines_list():
        conn = db_conn()
        rows = conn.execute('SELECT id, provider, api_url, api_key, model_name, updated_at FROM ai_engines ORDER BY id DESC').fetchall()
        conn.close()
        return render_template('admin_engines_list.html', rows=rows)

    @app.route('/admin/engines/new', methods=['GET', 'POST'])
    @admin_required
    def admin_engines_new():
        if request.method == 'POST':
            provider = request.form.get('provider', '').strip()
            api_url = request.form.get('api_url', '').strip()
            api_key = request.form.get('api_key', '').strip()
            model_name = request.form.get('model_name', '').strip()
            if not provider or not api_url or not api_key or not model_name:
                return render_template('admin_engines_edit.html', row=None, error='所有字段均为必填')
            conn = db_conn()
            try:
                conn.execute('INSERT INTO ai_engines (provider, api_url, api_key, model_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)', (provider, api_url, api_key, model_name, datetime.datetime.utcnow().isoformat(), datetime.datetime.utcnow().isoformat()))
                conn.commit()
                conn.close()
                return redirect(url_for('admin_engines_list'))
            except Exception as e:
                conn.close()
                return render_template('admin_engines_edit.html', row=None, error='保存失败：' + str(e))
        return render_template('admin_engines_edit.html', row=None)

    @app.route('/admin/engines/edit/<int:engine_id>', methods=['GET', 'POST'])
    @admin_required
    def admin_engines_edit(engine_id):
        conn = db_conn()
        if request.method == 'POST':
            provider = request.form.get('provider', '').strip()
            api_url = request.form.get('api_url', '').strip()
            api_key = request.form.get('api_key', '').strip()
            model_name = request.form.get('model_name', '').strip()
            if not provider or not api_url or not api_key or not model_name:
                row = conn.execute('SELECT id, provider, api_url, api_key, model_name FROM ai_engines WHERE id=?', (engine_id,)).fetchone()
                conn.close()
                return render_template('admin_engines_edit.html', row=row, error='所有字段均为必填')
            try:
                conn.execute('UPDATE ai_engines SET provider=?, api_url=?, api_key=?, model_name=?, updated_at=? WHERE id=?', (provider, api_url, api_key, model_name, datetime.datetime.utcnow().isoformat(), engine_id))
                conn.commit()
                conn.close()
                return redirect(url_for('admin_engines_list'))
            except Exception as e:
                row = conn.execute('SELECT id, provider, api_url, api_key, model_name FROM ai_engines WHERE id=?', (engine_id,)).fetchone()
                conn.close()
                return render_template('admin_engines_edit.html', row=row, error='保存失败：' + str(e))
        row = conn.execute('SELECT id, provider, api_url, api_key, model_name FROM ai_engines WHERE id=?', (engine_id,)).fetchone()
        conn.close()
        return render_template('admin_engines_edit.html', row=row)

    @app.route('/admin/engines/delete/<int:engine_id>', methods=['POST'])
    @admin_required
    def admin_engines_delete(engine_id):
        conn = db_conn()
        try:
            conn.execute('DELETE FROM ai_engines WHERE id=?', (engine_id,))
            conn.commit()
        except Exception:
            pass
        conn.close()
        return redirect(url_for('admin_engines_list'))

    @app.route('/admin/crawlers', methods=['GET'])
    @admin_required
    def admin_crawlers_list():
        conn = db_conn()
        rows = conn.execute('SELECT id, name, base_url, start_url, enabled, updated_at FROM crawler_configs ORDER BY id DESC').fetchall()
        conn.close()
        return render_template('admin_crawlers_list.html', rows=rows)

    @app.route('/admin/crawlers/new', methods=['GET', 'POST'])
    @admin_required
    def admin_crawlers_new():
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            base_url = request.form.get('base_url', '').strip()
            start_url = request.form.get('start_url', '').strip()
            enabled = 1 if request.form.get('enabled') in ('1', 'on', 'true') else 0
            headers_json = request.form.get('headers_json', '').strip()
            notes = request.form.get('notes', '').strip()
            if not name:
                return render_template('admin_crawlers_edit.html', row=None, error='名称不能为空')
            if headers_json:
                try:
                    json.loads(headers_json)
                except Exception:
                    return render_template('admin_crawlers_edit.html', row=None, error='Headers 必须是合法的 JSON')
            conn = db_conn()
            try:
                conn.execute('INSERT INTO crawler_configs (name, base_url, start_url, enabled, headers_json, notes, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (name, base_url, start_url, enabled, headers_json, notes, datetime.datetime.utcnow().isoformat(), datetime.datetime.utcnow().isoformat()))
                conn.commit()
                conn.close()
                return redirect(url_for('admin_crawlers_list'))
            except Exception as e:
                conn.close()
                return render_template('admin_crawlers_edit.html', row=None, error='保存失败：' + str(e))
        return render_template('admin_crawlers_edit.html', row=None)

    @app.route('/admin/crawlers/edit/<int:crawler_id>', methods=['GET', 'POST'])
    @admin_required
    def admin_crawlers_edit(crawler_id):
        conn = db_conn()
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            base_url = request.form.get('base_url', '').strip()
            start_url = request.form.get('start_url', '').strip()
            enabled = 1 if request.form.get('enabled') in ('1', 'on', 'true') else 0
            headers_json = request.form.get('headers_json', '').strip()
            notes = request.form.get('notes', '').strip()
            if not name:
                row = conn.execute('SELECT id, name, base_url, start_url, enabled, headers_json, notes FROM crawler_configs WHERE id=?', (crawler_id,)).fetchone()
                conn.close()
                return render_template('admin_crawlers_edit.html', row=row, error='名称不能为空')
            if headers_json:
                try:
                    json.loads(headers_json)
                except Exception:
                    row = conn.execute('SELECT id, name, base_url, start_url, enabled, headers_json, notes FROM crawler_configs WHERE id=?', (crawler_id,)).fetchone()
                    conn.close()
                    return render_template('admin_crawlers_edit.html', row=row, error='Headers 必须是合法的 JSON')
            try:
                conn.execute('UPDATE crawler_configs SET name=?, base_url=?, start_url=?, enabled=?, headers_json=?, notes=?, updated_at=? WHERE id=?', (name, base_url, start_url, enabled, headers_json, notes, datetime.datetime.utcnow().isoformat(), crawler_id))
                conn.commit()
                conn.close()
                return redirect(url_for('admin_crawlers_list'))
            except Exception as e:
                row = conn.execute('SELECT id, name, base_url, start_url, enabled, headers_json, notes FROM crawler_configs WHERE id=?', (crawler_id,)).fetchone()
                conn.close()
                return render_template('admin_crawlers_edit.html', row=row, error='保存失败：' + str(e))
        row = conn.execute('SELECT id, name, base_url, start_url, enabled, headers_json, notes FROM crawler_configs WHERE id=?', (crawler_id,)).fetchone()
        conn.close()
        return render_template('admin_crawlers_edit.html', row=row)

    @app.route('/admin/crawlers/delete/<int:crawler_id>', methods=['POST'])
    @admin_required
    def admin_crawlers_delete(crawler_id):
        conn = db_conn()
        try:
            conn.execute('DELETE FROM crawler_configs WHERE id=?', (crawler_id,))
            conn.commit()
        except Exception:
            pass
        conn.close()
        return redirect(url_for('admin_crawlers_list'))

    def call_ai(engine_row, prompt: str, system_prompt: str | None = None) -> tuple[bool, str]:
        return call_ai_util(engine_row, prompt, system_prompt)

    def call_ai_messages(engine_row, messages: list, temperature: float = 0.2) -> tuple[bool, str]:
        return call_ai_messages_util(engine_row, messages, temperature)

    @app.route('/admin/engines/test', methods=['GET', 'POST'])
    @admin_required
    def admin_engines_test():
        conn = db_conn()
        engines = conn.execute('SELECT id, provider, api_url, api_key, model_name FROM ai_engines ORDER BY id DESC').fetchall()
        selected_id = request.args.get('engine_id') or request.form.get('engine_id')
        selected = None
        if selected_id and selected_id.isdigit():
            selected = conn.execute('SELECT id, provider, api_url, api_key, model_name FROM ai_engines WHERE id=?', (int(selected_id),)).fetchone()
        conn.close()
        answer = ''
        error = ''
        prompt = ''
        system_prompt = ''
        if request.method == 'POST':
            prompt = request.form.get('prompt', '').strip()
            system_prompt = request.form.get('system_prompt', '').strip()
            if not selected:
                error = '请选择一个引擎'
            elif not prompt:
                error = '请输入测试问题'
            else:
                ok, text = call_ai(selected, prompt, system_prompt or None)
                if ok:
                    answer = text
                else:
                    error = text
        return render_template('admin_engines_test.html', engines=engines, selected_id=(selected['id'] if selected else ''), prompt=prompt, system_prompt=system_prompt, answer=answer, error=error)

    @app.route('/admin/engines/chat', methods=['POST'])
    @admin_required
    def admin_engines_chat():
        payload = request.get_json(silent=True) or {}
        engine_id = payload.get('engine_id')
        system_prompt = payload.get('system_prompt') or ''
        history = payload.get('messages') or []
        if not engine_id:
            return jsonify({'ok': False, 'error': '缺少引擎'}), 400
        conn = db_conn()
        eng = conn.execute('SELECT id, provider, api_url, api_key, model_name FROM ai_engines WHERE id=?', (int(engine_id),)).fetchone()
        conn.close()
        if not eng:
            return jsonify({'ok': False, 'error': '引擎不存在'}), 404
        msgs = []
        if system_prompt.strip():
            msgs.append({'role': 'system', 'content': system_prompt.strip()})
        for m in history:
            role = (m.get('role') or 'user').strip()
            content = (m.get('content') or '').strip()
            if not content:
                continue
            msgs.append({'role': role, 'content': content})
        ok, text = call_ai_messages(eng, msgs)
        if ok:
            return jsonify({'ok': True, 'reply': text})
        return jsonify({'ok': False, 'error': text}), 500

    def _strip_code_fences(s: str) -> str:
        s = s.strip()
        s = re.sub(r"^```json\s*|^```\s*", '', s, flags=re.IGNORECASE)
        s = re.sub(r"```$", '', s)
        s = s.strip()
        return s

    def _remove_html_tags(s: str) -> str:
        return re.sub(r"<[^>]+>", "", s)

    def _extract_clean_result(text: str) -> tuple[str, str]:
        t = ''
        c = ''
        raw = _strip_code_fences(text)
        try:
            obj = json.loads(raw)
            if isinstance(obj, list) and obj:
                obj = obj[0]
            if isinstance(obj, dict):
                t = (obj.get('title') or '').strip()
                c = (obj.get('content') or '').strip()
        except Exception:
            m = re.search(r"\{[\s\S]*\}", raw)
            if m:
                try:
                    obj = json.loads(m.group(0))
                    t = (obj.get('title') or '').strip()
                    c = (obj.get('content') or '').strip()
                except Exception:
                    pass
            if not c:
                # 兼容“标题：\n 正文：”格式
                lines = raw.splitlines()
                for i, ln in enumerate(lines):
                    if ln.strip().startswith('标题'):
                        t = ln.split('：', 1)[-1].strip() if '：' in ln else ln.strip().replace('标题', '').strip()
                        c = '\n'.join(lines[i+1:]).strip()
                        break
                if not c:
                    c = raw.strip()
        if c:
            c = _remove_html_tags(c)
        return t, c

    @app.route('/admin/clean', methods=['GET'])
    @admin_required
    def admin_clean():
        conn = db_conn()
        engines = conn.execute('SELECT id, provider, model_name FROM ai_engines ORDER BY id DESC').fetchall()
        q = request.args.get('q', '').strip()
        if q:
            rows = conn.execute('SELECT id, title, source, original_url, created_at, deep_done FROM articles WHERE deep_content IS NOT NULL AND (title LIKE ? OR source LIKE ? OR keyword LIKE ?) ORDER BY id DESC LIMIT 500', (f'%{q}%', f'%{q}%', f'%{q}%')).fetchall()
        else:
            rows = conn.execute('SELECT id, title, source, original_url, created_at, deep_done FROM articles WHERE deep_content IS NOT NULL ORDER BY id DESC LIMIT 500').fetchall()
        conn.close()
        return render_template('admin_clean.html', rows=rows, engines=engines, q=q)

    @app.route('/admin/clean/preview', methods=['POST'])
    @admin_required
    def admin_clean_preview():
        payload = request.get_json(silent=True) or {}
        engine_id = payload.get('engine_id')
        article_id = payload.get('article_id')
        system_prompt = (payload.get('system_prompt') or '').strip()
        if not engine_id or not article_id:
            return jsonify({'ok': False, 'error': '缺少参数'}), 400
        conn = db_conn()
        eng = conn.execute('SELECT id, provider, api_url, api_key, model_name FROM ai_engines WHERE id=?', (int(engine_id),)).fetchone()
        art = conn.execute('SELECT id, title, source, original_url, deep_content FROM articles WHERE id=?', (int(article_id),)).fetchone()
        conn.close()
        if not eng or not art:
            return jsonify({'ok': False, 'error': '对象不存在'}), 404
        title = art['title'] or ''
        content = (art['deep_content'] or '').strip()
        if not content:
            rule = find_rule_for(art['source'], art['original_url'])
            headers_override = None
            if rule and rule['headers_json']:
                try:
                    headers_override = json.loads(rule['headers_json'])
                except Exception:
                    headers_override = None
            if rule:
                t, c = deep_fetch_with_rule(art['original_url'], headers_override, rule['title_xpath'], rule['content_xpath'])
            else:
                t, c = None, deep_fetch_text(art['original_url'])
            title = t or title
            content = c or ''
        if not content.strip():
            return jsonify({'ok': False, 'error': '无可清洗内容'}), 400
        sys = system_prompt or '你是一个AI数据清洗助手，负责去广告、优化排版、识别标题与正文。请以JSON返回{"title":"...","content":"..."}，content为纯文本，无HTML标签、无广告、无水印。保留有效信息与段落结构。'
        user_msg = f"原始标题：{title}\n正文：\n{content}\n\n请清洗并返回JSON，字段：title, content。"
        ok, text = call_ai_messages(eng, [
            {'role': 'system', 'content': sys},
            {'role': 'user', 'content': user_msg},
        ])
        if not ok:
            return jsonify({'ok': False, 'error': text}), 500
        nt, nc = _extract_clean_result(text)
        new_title = nt or title
        new_content = nc or content
        return jsonify({'ok': True, 'title': new_title, 'content': new_content})

    @app.route('/admin/clean/apply', methods=['POST'])
    @admin_required
    def admin_clean_apply():
        payload = request.get_json(silent=True) or {}
        article_id = payload.get('article_id')
        title = (payload.get('title') or '').strip()
        content = (payload.get('content') or '').strip()
        if not article_id or not content:
            return jsonify({'ok': False, 'error': '缺少必要参数'}), 400
        conn = db_conn()
        try:
            if title:
                conn.execute('UPDATE articles SET title=? WHERE id=?', (title, int(article_id)))
            conn.execute('UPDATE articles SET deep_content=?, ai_analyzed_at=? WHERE id=?', (content, datetime.datetime.utcnow().isoformat(), int(article_id)))
            conn.commit()
            conn.close()
            return jsonify({'ok': True})
        except Exception:
            conn.close()
            return jsonify({'ok': False, 'error': '更新失败'}), 500

    @app.route('/admin/rules/preview/<int:rule_id>', methods=['GET', 'POST'])
    @admin_required
    def admin_rules_preview(rule_id):
        ensure_rules_schema()
        conn = db_conn()
        row = conn.execute('SELECT id, site, title_xpath, content_xpath, headers_json FROM crawl_rules WHERE id=?', (rule_id,)).fetchone()
        conn.close()
        if not row:
            return redirect(url_for('admin_rules_list'))
        result_title = ''
        result_content = ''
        url_input = ''
        error = ''
        if request.method == 'POST':
            url_input = request.form.get('url', '').strip()
            headers_override = None
            if row['headers_json']:
                try:
                    headers_override = json.loads(row['headers_json'])
                except Exception:
                    headers_override = None
            try:
                t, c = deep_fetch_with_rule(url_input, headers_override, row['title_xpath'], row['content_xpath'])
                result_title = t or ''
                result_content = c or ''
                if not result_content:
                    error = '未提取到正文'
            except Exception as e:
                error = str(e)
        return render_template('admin_rules_preview.html', rule=row, url_input=url_input, result_title=result_title, result_content=result_content, error=error)

    

    @app.route('/admin/users', methods=['GET', 'POST'])
    @admin_required
    def admin_users():
        conn = db_conn()
        if request.method == 'POST':
            username = request.form.get('username', '').strip()
            password = request.form.get('password', '')
            role = request.form.get('role', 'user')
            r = conn.execute('SELECT id FROM roles WHERE name=?', (role,)).fetchone()
            role_id = r['id'] if r else None
            ph = generate_password_hash(password)
            try:
                conn.execute('INSERT INTO users (username, password_hash, role_id, created_at) VALUES (?, ?, ?, ?)', (username, ph, role_id, datetime.datetime.utcnow().isoformat()))
                conn.commit()
            except Exception:
                pass
        users = conn.execute('SELECT users.id, users.username, users.is_active, roles.name AS role_name FROM users LEFT JOIN roles ON users.role_id=roles.id ORDER BY users.id DESC').fetchall()
        roles = conn.execute('SELECT name FROM roles').fetchall()
        conn.close()
        return render_template('admin_users.html', users=users, roles=roles)

    @app.route('/admin/users/delete/<int:user_id>', methods=['POST'])
    @admin_required
    def admin_users_delete(user_id):
        conn = db_conn()
        try:
            conn.execute('DELETE FROM users WHERE id=?', (user_id,))
            conn.commit()
        except Exception:
            pass
        conn.close()
        return redirect(url_for('admin_users'))

    @app.route('/admin/roles', methods=['GET', 'POST'])
    @admin_required
    def admin_roles():
        conn = db_conn()
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            desc = request.form.get('description', '').strip()
            try:
                conn.execute('INSERT INTO roles (name, description) VALUES (?, ?)', (name, desc))
                conn.commit()
            except Exception:
                pass
        roles = conn.execute('SELECT id, name, description FROM roles ORDER BY id DESC').fetchall()
        conn.close()
        return render_template('admin_roles.html', roles=roles)

    @app.route('/admin/settings', methods=['GET', 'POST'])
    @admin_required
    def admin_settings():
        if request.method == 'POST':
            name = request.form.get('app_name', '').strip()
            if name:
                set_setting('app_name', name)
            file = request.files.get('logo')
            if file and file.filename:
                upload_dir = os.path.join(app.static_folder, 'uploads')
                os.makedirs(upload_dir, exist_ok=True)
                fn = 'logo_' + datetime.datetime.utcnow().strftime('%Y%m%d%H%M%S') + os.path.splitext(file.filename)[1]
                path = os.path.join(upload_dir, fn)
                file.save(path)
                rel = os.path.join('uploads', fn).replace('\\', '/')
                set_setting('logo_path', rel)
        name = get_setting('app_name', '')
        logo = get_setting('logo_path', '')
        return render_template('admin_settings.html', app_name=name, logo_path=logo)

    @app.route('/forgot')
    def forgot():
        return render_template('forgot.html')

    @app.route('/data/screen')
    @login_required
    def data_screen():
        return render_template('data_screen.html')

    @app.route('/api/data/latest')
    @login_required
    def api_data_latest():
        conn = db_conn()
        rows = []
        try:
            rows = conn.execute('SELECT id, title, source, created_at FROM article_details ORDER BY created_at DESC, id DESC LIMIT 20').fetchall()
        except Exception:
            rows = []
        if not rows:
            rows = conn.execute('SELECT id, title, source, created_at FROM articles ORDER BY created_at DESC, id DESC LIMIT 20').fetchall()
        conn.close()
        data = []
        for r in rows:
            data.append({'id': r['id'], 'title': r['title'] or '', 'source': r['source'] or '', 'created_at': r['created_at'] or ''})
        return jsonify({'ok': True, 'data': data})

    def _province_list():
        return ['北京','天津','上海','重庆','河北','山西','辽宁','吉林','黑龙江','江苏','浙江','安徽','福建','江西','山东','河南','湖北','湖南','广东','海南','四川','贵州','云南','陕西','甘肃','青海','台湾','广西','内蒙古','宁夏','新疆','西藏','香港','澳门']

    @app.route('/api/data/heat')
    @login_required
    def api_data_heat():
        conn = db_conn()
        rows = []
        try:
            rows = conn.execute('SELECT region, keywords, content FROM article_details ORDER BY id DESC LIMIT 1000').fetchall()
        except Exception:
            rows = []
        if not rows:
            rows = conn.execute('SELECT source AS region, keyword AS keywords, deep_content AS content FROM articles ORDER BY id DESC LIMIT 500').fetchall()
        conn.close()
        provs = _province_list()
        counts = {p: 0 for p in provs}
        for r in rows:
            s = ((r['region'] or '') + ' ' + (r['keywords'] or '') + ' ' + ((r['content'] or '')[:2000])).strip()
            if not s:
                continue
            for p in provs:
                if p in s:
                    counts[p] = counts.get(p, 0) + 1
        data = [{'name': p, 'value': counts[p]} for p in provs if counts.get(p, 0) > 0]
        return jsonify({'ok': True, 'data': data})

    @app.route('/admin/menus', methods=['GET'])
    @admin_required
    def admin_menus_list():
        conn = db_conn()
        menus = conn.execute('SELECT m.*, p.name as parent_name FROM menus m LEFT JOIN menus p ON m.parent_id = p.id ORDER BY m.order_num ASC').fetchall()
        conn.close()
        return render_template('admin_menus_list.html', menus=menus)

    @app.route('/admin/menus/new', methods=['GET', 'POST'])
    @admin_required
    def admin_menus_new():
        conn = db_conn()
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            endpoint = request.form.get('endpoint', '').strip()
            parent_id = request.form.get('parent_id')
            order_num = request.form.get('order_num', '0')
            is_visible = 1 if request.form.get('is_visible') else 0
            
            if not name:
                parents = conn.execute('SELECT id, name FROM menus WHERE parent_id IS NULL ORDER BY order_num ASC').fetchall()
                conn.close()
                return render_template('admin_menus_edit.html', row=None, parents=parents, error='名称不能为空')
            
            pid = int(parent_id) if parent_id and parent_id.isdigit() else None
            onum = int(order_num) if order_num and order_num.isdigit() else 0
            now = datetime.datetime.utcnow().isoformat()
            
            try:
                conn.execute('INSERT INTO menus (name, endpoint, parent_id, order_num, is_visible, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)', 
                             (name, endpoint, pid, onum, is_visible, now, now))
                conn.commit()
                conn.close()
                return redirect(url_for('admin_menus_list'))
            except Exception as e:
                parents = conn.execute('SELECT id, name FROM menus WHERE parent_id IS NULL ORDER BY order_num ASC').fetchall()
                conn.close()
                return render_template('admin_menus_edit.html', row=None, parents=parents, error='保存失败: ' + str(e))
        
        parents = conn.execute('SELECT id, name FROM menus WHERE parent_id IS NULL ORDER BY order_num ASC').fetchall()
        conn.close()
        return render_template('admin_menus_edit.html', row=None, parents=parents)

    @app.route('/admin/menus/edit/<int:menu_id>', methods=['GET', 'POST'])
    @admin_required
    def admin_menus_edit(menu_id):
        conn = db_conn()
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            endpoint = request.form.get('endpoint', '').strip()
            parent_id = request.form.get('parent_id')
            order_num = request.form.get('order_num', '0')
            is_visible = 1 if request.form.get('is_visible') else 0
            
            if not name:
                row = conn.execute('SELECT * FROM menus WHERE id=?', (menu_id,)).fetchone()
                parents = conn.execute('SELECT id, name FROM menus WHERE parent_id IS NULL AND id != ? ORDER BY order_num ASC', (menu_id,)).fetchall()
                conn.close()
                return render_template('admin_menus_edit.html', row=row, parents=parents, error='名称不能为空')

            pid = int(parent_id) if parent_id and parent_id.isdigit() else None
            onum = int(order_num) if order_num and order_num.isdigit() else 0
            now = datetime.datetime.utcnow().isoformat()
            
            try:
                conn.execute('UPDATE menus SET name=?, endpoint=?, parent_id=?, order_num=?, is_visible=?, updated_at=? WHERE id=?', 
                             (name, endpoint, pid, onum, is_visible, now, menu_id))
                conn.commit()
                conn.close()
                return redirect(url_for('admin_menus_list'))
            except Exception as e:
                row = conn.execute('SELECT * FROM menus WHERE id=?', (menu_id,)).fetchone()
                parents = conn.execute('SELECT id, name FROM menus WHERE parent_id IS NULL AND id != ? ORDER BY order_num ASC', (menu_id,)).fetchall()
                conn.close()
                return render_template('admin_menus_edit.html', row=row, parents=parents, error='保存失败: ' + str(e))

        row = conn.execute('SELECT * FROM menus WHERE id=?', (menu_id,)).fetchone()
        parents = conn.execute('SELECT id, name FROM menus WHERE parent_id IS NULL AND id != ? ORDER BY order_num ASC', (menu_id,)).fetchall()
        conn.close()
        return render_template('admin_menus_edit.html', row=row, parents=parents)

    @app.route('/admin/menus/delete/<int:menu_id>', methods=['POST'])
    @admin_required
    def admin_menus_delete(menu_id):
        conn = db_conn()
        try:
            conn.execute('DELETE FROM menus WHERE id=?', (menu_id,))
            conn.execute('UPDATE menus SET parent_id=NULL WHERE parent_id=?', (menu_id,))
            conn.commit()
        except Exception:
            pass
        conn.close()
        return redirect(url_for('admin_menus_list'))

    # Register Plugins
    try:
        from plugins.report_management import bp as report_bp, init_plugin_db
        app.register_blueprint(report_bp)
        init_plugin_db(app)
    except Exception as e:
        print(f"Failed to load report_management plugin: {e}")

    try:
        from plugins.data_analysis import bp as analysis_bp, init_plugin as init_analysis
        app.register_blueprint(analysis_bp)
        init_analysis(app)
    except Exception as e:
        print(f"Failed to load data_analysis plugin: {e}")

    try:
        from plugins.chat_room import bp as chat_bp, init_plugin_db as init_chat
        app.register_blueprint(chat_bp)
        init_chat(app)
    except Exception as e:
        print(f"Failed to load chat_room plugin: {e}")

    init_db()
    ensure_menus_schema()
    return app


app = create_app()
