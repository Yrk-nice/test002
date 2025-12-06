from flask import Blueprint, jsonify, request, current_app, session
from app.utils import get_db_connection, call_ai_messages
import datetime
import json

bp = Blueprint('data_analysis', __name__, url_prefix='/analysis')

def db_conn():
    return get_db_connection(current_app.config['DB_PATH'])

@bp.route('/screen/analyze', methods=['POST'])
def analyze_screen():
    if not session.get('user_id'):
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
        
    conn = db_conn()
    
    # Fetch data to analyze
    # Latest news
    latest_rows = conn.execute('SELECT title, source, created_at FROM articles ORDER BY created_at DESC, id DESC LIMIT 20').fetchall()
    latest_news = [{"title": r['title'], "source": r['source'], "time": r['created_at']} for r in latest_rows]
    
    # Regional distribution (Top 10)
    # Re-using logic from api_data_heat but simplified for prompt context
    rows = conn.execute('SELECT source AS region, keyword AS keywords, deep_content AS content FROM articles ORDER BY id DESC LIMIT 500').fetchall()
    
    provs = ['北京','天津','上海','重庆','河北','山西','辽宁','吉林','黑龙江','江苏','浙江','安徽','福建','江西','山东','河南','湖北','湖南','广东','海南','四川','贵州','云南','陕西','甘肃','青海','台湾','广西','内蒙古','宁夏','新疆','西藏','香港','澳门']
    counts = {p: 0 for p in provs}
    for r in rows:
        s = ((r['region'] or '') + ' ' + (r['keywords'] or '') + ' ' + ((r['content'] or '')[:2000])).strip()
        if not s: continue
        for p in provs:
            if p in s:
                counts[p] = counts.get(p, 0) + 1
    
    region_data = [{'name': p, 'value': counts[p]} for p in provs if counts.get(p, 0) > 0]
    region_data.sort(key=lambda x: x['value'], reverse=True)
    region_data = region_data[:10]
    
    # Get AI engine
    engine = conn.execute('SELECT * FROM ai_engines ORDER BY id DESC LIMIT 1').fetchone()
    
    if not engine:
        conn.close()
        return jsonify({'ok': False, 'error': 'AI Engine not configured'}), 400
        
    # Construct Prompt
    prompt = f"""
    请作为一位资深舆情分析师，根据以下数据对当前舆情态势进行简要分析总结。
    
    【最新舆情资讯（前20条）】：
    {json.dumps(latest_news, ensure_ascii=False, indent=2)}
    
    【地域热度分布（Top 10）】：
    {json.dumps(region_data, ensure_ascii=False, indent=2)}
    
    请输出一段不超过300字的分析总结，包含：
    1. 当前主要关注的热点话题或领域。
    2. 舆情高发的地域特征。
    3. 简要的趋势研判或建议。
    
    请直接输出分析内容，不要包含Markdown标记。
    """
    
    ok, result = call_ai_messages(engine, [{'role': 'user', 'content': prompt}])
    conn.close()
    
    if ok:
        return jsonify({'ok': True, 'analysis': result})
    
    return jsonify({'ok': False, 'error': result}), 500

def init_plugin(app):
    # No specific db init needed for now unless we want to store analysis history
    pass
