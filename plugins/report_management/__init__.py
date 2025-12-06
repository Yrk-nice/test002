from flask import Blueprint, render_template, request, jsonify, current_app, send_file, session
import sqlite3
import datetime
import io
import json
from xhtml2pdf import pisa
from app.utils import get_db_connection, call_ai_messages

bp = Blueprint('report_management', __name__, template_folder='templates', url_prefix='/report')

def db_conn():
    return get_db_connection(current_app.config['DB_PATH'])

@bp.route('/')
def index():
    if not session.get('user_id'):
        return current_app.response_class(status=302, headers={'Location': '/login'})
    
    conn = db_conn()
    try:
        # Check if table exists
        conn.execute('SELECT 1 FROM plugin_reports LIMIT 1')
    except sqlite3.OperationalError:
        # Create table if not exists (though init_plugin_db should handle this, 
        # sometimes it might fail or not run if app context issues)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS plugin_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER,
                title TEXT,
                content_html TEXT,
                created_at TEXT
            )
        ''')
        conn.commit()

    # Find articles that have deep_content (ready for report generation)
    articles = conn.execute('SELECT id, title, source, deep_fetched_at, ai_analyzed_at FROM articles WHERE deep_done=1 ORDER BY id DESC LIMIT 100').fetchall()
    
    # Also fetch existing reports
    # Assuming we store reports in a new table 'plugin_reports' or similar
    # For now, let's just query articles and see if we have generated a report for them?
    # Or better, create a table for reports.
    reports = conn.execute('SELECT * FROM plugin_reports ORDER BY id DESC').fetchall()
    
    conn.close()
    return render_template('report_list.html', articles=articles, reports=reports)

@bp.route('/generate/<int:article_id>', methods=['POST'])
def generate(article_id):
    if not session.get('user_id'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    
    conn = db_conn()
    article = conn.execute('SELECT * FROM articles WHERE id=?', (article_id,)).fetchone()
    
    # Get AI engine (use the first one or a specific one)
    engine = conn.execute('SELECT * FROM ai_engines ORDER BY id DESC LIMIT 1').fetchone()
    
    if not article or not engine:
        conn.close()
        return jsonify({'ok': False, 'error': 'Article or AI Engine not found'}), 404
        
    content = article['deep_content']
    if not content:
        conn.close()
        return jsonify({'ok': False, 'error': 'No content to analyze'}), 400
    
    prompt = f"""
    请根据以下文章内容，生成一份专业的分析报告。
    报告应包含：摘要、主要观点、关键词提取、以及结论。
    请使用HTML格式输出，使用h2, h3, p, ul, li等标签进行排版，不要包含```html```代码块标记。
    
    文章标题：{article['title']}
    文章内容：
    {content[:3000]} (截取前3000字)
    """
    
    ok, report_html = call_ai_messages(engine, [{'role': 'user', 'content': prompt}])
    
    if ok:
        # Save report
        now = datetime.datetime.utcnow().isoformat()
        conn.execute('INSERT INTO plugin_reports (article_id, title, content_html, created_at) VALUES (?, ?, ?, ?)',
                     (article_id, f"分析报告: {article['title']}", report_html, now))
        conn.commit()
        conn.close()
        return jsonify({'ok': True})
    else:
        conn.close()
        return jsonify({'ok': False, 'error': report_html}), 500

@bp.route('/view/<int:report_id>')
def view(report_id):
    conn = db_conn()
    report = conn.execute('SELECT * FROM plugin_reports WHERE id=?', (report_id,)).fetchone()
    conn.close()
    if not report:
        return "Report not found", 404
    return render_template('report_view.html', report=report)

@bp.route('/download/<int:report_id>')
def download(report_id):
    conn = db_conn()
    report = conn.execute('SELECT * FROM plugin_reports WHERE id=?', (report_id,)).fetchone()
    conn.close()
    if not report:
        return "Report not found", 404
        
    # Convert HTML to PDF
    # We need to wrap the content in a full HTML structure with font support for Chinese if possible
    # For simplicity, we assume the server has fonts or we use basic configuration.
    # xhtml2pdf has limited support for CJK without custom font registration.
    # We will try to use a template that includes a font that supports Chinese (e.g. SimHei or similar if available on Windows)
    # Or just rely on system fallback.
    
    # Windows typically has 'SimSun' or 'Microsoft YaHei'.
    
    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <style>
            @page {{
                size: A4;
                margin: 1cm;
            }}
            body {{
                font-family: sans-serif; 
            }}
        </style>
    </head>
    <body>
        <h1>{report['title']}</h1>
        <div>{report['content_html']}</div>
    </body>
    </html>
    """
    
    pdf_buffer = io.BytesIO()
    pisa_status = pisa.CreatePDF(io.BytesIO(html_content.encode('utf-8')), dest=pdf_buffer, encoding='utf-8')
    
    if pisa_status.err:
        return f"PDF generation error: {pisa_status.err}", 500
        
    pdf_buffer.seek(0)
    return send_file(pdf_buffer, as_attachment=True, download_name=f"report_{report_id}.pdf", mimetype='application/pdf')

def init_plugin_db(app):
    with app.app_context():
        conn = get_db_connection(app.config['DB_PATH'])
        conn.execute('''
            CREATE TABLE IF NOT EXISTS plugin_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id INTEGER,
                title TEXT,
                content_html TEXT,
                created_at TEXT
            )
        ''')
        
        # Register menu
        # Check if '报告管理' exists
        row = conn.execute("SELECT id FROM menus WHERE endpoint='report_management.index'").fetchone()
        if not row:
            # Find '系统管理' id
            sys_row = conn.execute("SELECT id FROM menus WHERE name='系统管理'").fetchone()
            parent_id = sys_row['id'] if sys_row else None
            now = datetime.datetime.utcnow().isoformat()
            conn.execute("INSERT INTO menus (name, endpoint, parent_id, order_num, is_visible, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                         ('报告管理', 'report_management.index', parent_id, 99, 1, now, now))
            conn.commit()
        conn.close()
