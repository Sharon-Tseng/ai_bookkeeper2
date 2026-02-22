import os
import re
import sqlite3
import json
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
import PIL.Image as Image
from flask import Flask, jsonify, render_template, request, redirect, url_for
from flask_cors import CORS
from backend.agent_tools import load_raw_expense_data
from backend.agents import AccountAgents

app = Flask(__name__)
CORS(app)

TEMP_UPLOAD_DIR = '/tmp/bookkeeper_uploads' if os.environ.get('VERCEL') else os.path.join(os.path.dirname(__file__), 'tmp_uploads')
os.makedirs(TEMP_UPLOAD_DIR, exist_ok=True)
RETRY_FILE_METADATA = {}
MAX_CONCURRENT_FILES = 3

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

def persist_uploaded_file(file_storage):
    _, ext = os.path.splitext(file_storage.filename or '')
    filename = f"{uuid.uuid4().hex}{ext.lower() or '.png'}"
    file_path = os.path.join(TEMP_UPLOAD_DIR, filename)
    file_storage.save(file_path)
    RETRY_FILE_METADATA[filename] = file_storage.filename
    return filename, file_path

def run_bookkeeper_for_file(file_path, spreadsheet_id):
    with Image.open(file_path) as img:
        return AccountAgents.run_bookkeeper(img.copy(), spreadsheet_id)

def remove_temp_file(file_path):
    try:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)
    except OSError:
        pass

def run_parallel_jobs(task_items, worker):
    if not task_items:
        return []

    results = []
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FILES) as executor:
        future_map = {executor.submit(worker, item): item for item in task_items}
        for future in as_completed(future_map):
            item = future_map[future]
            try:
                results.append(future.result())
            except Exception as error:
                results.append({
                    "index": item.get("index", 0),
                    "filename": item.get("filename", "-"),
                    "success": False,
                    "message": f"Error: {str(error)}",
                    "retry_token": item.get("retry_token")
                })

    return sorted(results, key=lambda row: row.get("index", 0))

# 初始化資料庫
init_db()

# --- 路由邏輯 ---

@app.route('/')
def index():
    return render_template('welcome.html')

@app.route('/app', methods=['GET', 'POST'])
def app_page():
    saved_sheets = get_user_sheets('default_user')
    results = []
    result_summary = None
    sheet_url = ''
    
    if request.method == 'POST':
        sheet_url = request.form.get('sheet_url')
        retry_tokens_raw = request.form.get('retry_tokens', '').strip()
        files = request.files.getlist('receipts') or request.files.getlist('receipt')
        valid_files = [file for file in files if file and file.filename]
        
        if sheet_url and (valid_files or retry_tokens_raw):
            try:
                spreadsheet_id = get_spreadsheet_id(sheet_url)
                if not spreadsheet_id:
                    raise ValueError("Invalid Google Sheet URL")
                save_user_sheet('default_user', sheet_url)

                if retry_tokens_raw:
                    retry_tokens = [token.strip() for token in retry_tokens_raw.split(',') if token.strip()]
                    retry_tasks = []
                    for index, token in enumerate(retry_tokens, start=1):
                        retry_tasks.append({
                            "index": index,
                            "token": token,
                            "file_path": os.path.join(TEMP_UPLOAD_DIR, token),
                            "filename": RETRY_FILE_METADATA.get(token, f"retry_{token[:8]}"),
                            "retry_token": token
                        })

                    def retry_worker(task):
                        if not os.path.exists(task["file_path"]):
                            raise FileNotFoundError("Temporary file not found for retry")

                        message = run_bookkeeper_for_file(task["file_path"], spreadsheet_id)
                        remove_temp_file(task["file_path"])
                        RETRY_FILE_METADATA.pop(task["token"], None)

                        return {
                            "index": task["index"],
                            "filename": task["filename"],
                            "success": True,
                            "message": message,
                            "retry_token": None
                        }

                    results.extend(run_parallel_jobs(retry_tasks, retry_worker))
                else:
                    upload_tasks = []
                    for index, file in enumerate(valid_files, start=1):
                        try:
                            retry_token, file_path = persist_uploaded_file(file)
                            upload_tasks.append({
                                "index": index,
                                "filename": file.filename,
                                "retry_token": retry_token,
                                "file_path": file_path
                            })
                        except Exception as file_error:
                            results.append({
                                "index": index,
                                "filename": file.filename,
                                "success": False,
                                "message": f"Error: {str(file_error)}",
                                "retry_token": None
                            })

                    def upload_worker(task):
                        try:
                            message = run_bookkeeper_for_file(task["file_path"], spreadsheet_id)
                            remove_temp_file(task["file_path"])
                            RETRY_FILE_METADATA.pop(task["retry_token"], None)
                            return {
                                "index": task["index"],
                                "filename": task["filename"],
                                "success": True,
                                "message": message,
                                "retry_token": None
                            }
                        except Exception as file_error:
                            return {
                                "index": task["index"],
                                "filename": task["filename"],
                                "success": False,
                                "message": f"Error: {str(file_error)}",
                                "retry_token": task["retry_token"]
                            }

                    results.extend(run_parallel_jobs(upload_tasks, upload_worker))
            except Exception as e:
                results = [{
                    "index": 1,
                    "filename": "-",
                    "success": False,
                    "message": f"Error: {str(e)}",
                    "retry_token": None
                }]

            if results:
                success_count = sum(1 for row in results if row.get("success"))
                result_summary = {
                    "total": len(results),
                    "success": success_count,
                    "failed": len(results) - success_count,
                }
                
    return render_template(
        'add_record.html',
        saved_sheets=saved_sheets,
        sheet_url=sheet_url,
        results=results,
        result_summary=result_summary
    )

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
        files = request.files.getlist('receipts') or request.files.getlist('receipt')
        valid_files = [file for file in files if file and file.filename]

        if not sheet_url or not valid_files:
            return jsonify({"success": False, "error": "缺少網址或檔案"}), 400

        spreadsheet_id = get_spreadsheet_id(sheet_url)
        if not spreadsheet_id:
            return jsonify({"success": False, "error": "無效的 Google Sheet URL"}), 400
        save_user_sheet('default_user', sheet_url)

        file_results = []
        upload_tasks = []
        for index, file in enumerate(valid_files, start=1):
            try:
                retry_token, file_path = persist_uploaded_file(file)
                upload_tasks.append({
                    "index": index,
                    "filename": file.filename,
                    "retry_token": retry_token,
                    "file_path": file_path
                })
            except Exception as file_error:
                file_results.append({
                    "index": index,
                    "filename": file.filename,
                    "success": False,
                    "message": str(file_error)
                })

        def api_upload_worker(task):
            try:
                message = run_bookkeeper_for_file(task["file_path"], spreadsheet_id)
                return {
                    "index": task["index"],
                    "filename": task["filename"],
                    "success": True,
                    "message": message
                }
            except Exception as file_error:
                return {
                    "index": task["index"],
                    "filename": task["filename"],
                    "success": False,
                    "message": str(file_error)
                }
            finally:
                remove_temp_file(task["file_path"])
                RETRY_FILE_METADATA.pop(task["retry_token"], None)

        file_results.extend(run_parallel_jobs(upload_tasks, api_upload_worker))
        file_results = sorted(file_results, key=lambda row: row.get("index", 0))
        success_count = sum(1 for row in file_results if row.get("success"))

        return jsonify({
            "success": success_count > 0,
            "processed_count": len(valid_files),
            "success_count": success_count,
            "results": file_results
        })
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