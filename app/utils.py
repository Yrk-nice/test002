# app/utils.py
import sqlite3
import requests
import os

# We need a way to access current_app config, but utils might be imported before app is created.
# So we pass db_path or engine_row explicitly.

def get_db_connection(db_path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def call_ai(engine_row, prompt: str, system_prompt: str | None = None) -> tuple[bool, str]:
    api_url = engine_row['api_url']
    api_key = engine_row['api_key']
    model = engine_row['model_name']
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
        'api-key': api_key,
    }
    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': system_prompt or '你是一个有用的助手'},
            {'role': 'user', 'content': prompt},
        ],
        'temperature': 0.2,
    }
    try:
        r = requests.post(api_url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        # OpenAI style
        if isinstance(data, dict) and 'choices' in data and data['choices']:
            choice = data['choices'][0]
            msg = choice.get('message') or {}
            content = msg.get('content') or ''
            if content:
                return True, content
        # fallback common fields
        for k in ('output_text', 'text', 'result'):
            if k in data and data[k]:
                return True, data[k]
        return False, '响应未包含文本'
    except Exception as e:
        return False, str(e)

def call_ai_messages(engine_row, messages: list, temperature: float = 0.2) -> tuple[bool, str]:
    api_url = engine_row['api_url']
    api_key = engine_row['api_key']
    model = engine_row['model_name']
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}',
        'api-key': api_key,
    }
    payload = {
        'model': model,
        'messages': messages,
        'temperature': temperature,
    }
    try:
        r = requests.post(api_url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict) and 'choices' in data and data['choices']:
            msg = (data['choices'][0] or {}).get('message') or {}
            content = msg.get('content') or ''
            if content:
                return True, content
        for k in ('output_text', 'text', 'result'):
            if k in data and data[k]:
                return True, data[k]
        return False, '响应未包含文本'
    except Exception as e:
        return False, str(e)
