import os
import re
import sqlite3
import json
import PIL.Image as Image
from flask import Flask, jsonify, render_template, request, redirect, url_for
from flask_cors import CORS
from agent_tools import load_raw_expense_data
from agents import AccountAgents

app = Flask(__name__)
CORS(app)

# --- 資料庫與工具邏輯 ---
def get_db_conn():
    path = '/tmp/bookkeeper.db' if os.environ.get('VERCEL') else 'bookkeeper.db'
    return sqlite3.connect(path)

def init_db():
    with get_db_conn() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS user_sheets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                sheet_url TEXT NOT NULL,
                UNIQUE(username, sheet_url)
            )''')

def get_spreadsheet_id(url):
    pattern = r"/d/([a-zA-Z0-9-_]+)"
    match = re.search(pattern, url)
    return match.group(1) if match else None

def save_user_sheet(username, sheet_url):
    with get_db_conn() as conn:
        try:
            conn.execute('INSERT INTO user_sheets (username, sheet_url) VALUES (?, ?)', (username, sheet_url))
        except sqlite3.IntegrityError:
            pass

def get_user_sheets(username):
    with get_db_conn() as conn:
        cursor = conn.execute('SELECT DISTINCT sheet_url FROM user_sheets WHERE username = ?', (username,))
        return [row[0] for row in cursor.fetchall()]

# 初始化資料庫
init_db()

# --- 路由邏輯 ---

@app.route('/')
def index():
    return render_template('welcome.html')

@app.route('/app', methods=['GET', 'POST'])
def app_page():
    saved_sheets = get_user_sheets('default_user')
    result = None
    sheet_url = ''
    
    if request.method == 'POST':
        sheet_url = request.form.get('sheet_url')
        file = request.files.get('receipt')
        
        if sheet_url and file:
            try:
                spreadsheet_id = get_spreadsheet_id(sheet_url)
                save_user_sheet('default_user', sheet_url)
                
                img = Image.open(file.stream)
                result = AccountAgents.run_bookkeeper(img, spreadsheet_id)
            except Exception as e:
                result = f"Error: {str(e)}"
                
    return render_template('add_record.html', saved_sheets=saved_sheets, sheet_url=sheet_url, result=result)

@app.route('/view_history', methods=['GET', 'POST'])
def view_history_page():
    saved_sheets = get_user_sheets('default_user')
    sheet_url = request.args.get('sheet_url') or request.form.get('sheet_url') or ''
    history_data = None
    total_trend = None
    
    if sheet_url:
        spreadsheet_id = get_spreadsheet_id(sheet_url)
        if spreadsheet_id:
            try:
                raw_data = load_raw_expense_data(spreadsheet_id)
                history_data = raw_data.get("data", {})
                total_trend = {month: sum(categories.values()) for month, categories in history_data.items()}
            except Exception as e:
                pass
                
    return render_template('view_history.html', saved_sheets=saved_sheets, history=history_data, total_trend=total_trend, sheet_url=sheet_url)

@app.route('/api/upload_receipt', methods=['POST'])
def upload_receipt_api():
    """Flutter 專用：上傳收據並寫入試算表"""
    try:
        sheet_url = request.form.get('sheet_url')
        file = request.files.get('receipt')
        if not sheet_url or not file:
            return jsonify({"success": False, "error": "缺少網址或檔案"}), 400

        spreadsheet_id = get_spreadsheet_id(sheet_url)
        save_user_sheet('default_user', sheet_url)
        
        img = Image.open(file.stream)
        result = AccountAgents.run_bookkeeper(img, spreadsheet_id) 
        return jsonify({"success": True, "message": result})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/history', methods=['GET'])
def get_history_api():
    """Flutter 專用：獲取歷史數據以供圖表顯示"""
    sheet_url = request.args.get('sheet_url')
    if not sheet_url:
        return jsonify({"error": "缺少 sheet_url"}), 400
        
    spreadsheet_id = get_spreadsheet_id(sheet_url)
    if not spreadsheet_id:
        return jsonify({"error": "無效的網址"}), 400

    raw_data = load_raw_expense_data(spreadsheet_id)
    # 計算總趨勢
    data = raw_data.get("data", {})
    total_trend = {month: sum(categories.values()) for month, categories in data.items()}
    
    raw_data["total_trend"] = total_trend
    return jsonify(raw_data)

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)