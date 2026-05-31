import os
import json
import requests
import firebase_admin
import secrets
import hashlib
import base64
from firebase_admin import credentials, db
from flask import Flask, request, abort, redirect
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage, PushMessageRequest
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import datetime

app = Flask(__name__)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
 

# --- 1. 初始化 Firebase (從環境變數讀取 JSON 字串) ---
if not firebase_admin._apps:
    fb_config_str = os.environ.get('FIREBASE_CONFIG_JSON')
    db_url = os.environ.get('FIREBASE_DB_URL')
    if fb_config_str:
        cred_dict = json.loads(fb_config_str)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {'databaseURL': db_url})

# --- 2. 設定參數 (全數從環境變數讀取) ---
LINE_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
RENDER_URL = os.environ.get('RENDER_URL')
G_CONFIG_STR = os.environ.get('G_CLIENT_SECRET_JSON')

# 先行解析 Google 配置
GOOGLE_CLIENT_CONFIG = json.loads(G_CONFIG_STR) if G_CONFIG_STR else None

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)
SCOPES = 'https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/spreadsheets'

import traceback  # 確保這行有加在 main.py 的最上方（import 區塊）

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data()
    
    try:
        # 這裡會觸發你寫在下方的 @handler.add 邏輯
        handler.handle(body.decode('utf-8'), signature)
        
    except Exception as e:
        # 這次我們強制把底層的 Traceback 全部印出來！
        error_trace = traceback.format_exc()
        print("====== 案發現場開始 ======")
        print(f"錯誤類型: {type(e)}")
        print(f"錯誤訊息: {e}")
        print(f"詳細追蹤:\n{error_trace}")
        print("====== 案發現場結束 ======")
        
        abort(400)
        
    return 'OK'

@app.route("/authorize/<user_id>")
def authorize(user_id):
    client_config = GOOGLE_CLIENT_CONFIG['web']
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).decode().replace('=', '').replace('+', '-').replace('/', '_')
    
    db.reference(f'temp_auth/{user_id}').set({'verifier': code_verifier})
    
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={client_config['client_id']}&"
        f"redirect_uri={RENDER_URL}/oauth2callback&"
        f"response_type=code&"
        f"scope={SCOPES}&"
        f"access_type=offline&"
        f"prompt=consent&"
        f"state={user_id}&"
        f"code_challenge={code_challenge}&"
        f"code_challenge_method=S256"
    )
    return redirect(auth_url)

@app.route("/")
def home():
    # 當 Cron-job.org 訪問這裡時，會看到這行字，並收到 200 成功代碼
    return "Bot is running and staying awake!"

@app.route("/oauth2callback")
def oauth2callback():
    code = request.args.get('code')
    user_id = request.args.get('state')
    temp_ref = db.reference(f'temp_auth/{user_id}').get()
    
    if not temp_ref:
        return "驗證逾時，請重新點擊連結。"
    
    code_verifier = temp_ref['verifier']
    client_config = GOOGLE_CLIENT_CONFIG['web']

    token_url = "https://oauth2.googleapis.com/token"
    payload = {
        'code': code,
        'client_id': client_config['client_id'],
        'client_secret': client_config['client_secret'],
        'redirect_uri': f"{RENDER_URL}/oauth2callback",
        'grant_type': 'authorization_code',
        'code_verifier': code_verifier
    }
    
    res = requests.post(token_url, data=payload)
    token_data = res.json()

    if 'error' in token_data:
        return f"授權失敗：{token_data.get('error_description')}"

    db.reference(f'users/{user_id}').update({
        'token': token_data.get('access_token'),
        'refresh_token': token_data.get('refresh_token'),
        'token_uri': "https://oauth2.googleapis.com/token",
        'client_id': client_config['client_id'],
        'client_secret': client_config['client_secret'],
        'scopes': SCOPES.split()
    })
    db.reference(f'temp_auth/{user_id}').delete()

    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, 
                messages=[TextMessage(text="🎊 授權成功！請輸入「項目 金額」開始記帳。")]
            ))
    except: pass

    return '<h1 style="text-align:center;padding-top:50px;font-family:sans-serif;color:#00B900;">✅ 授權成功！請回到 LINE</h1>'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_data = db.reference(f'users/{user_id}').get()

    # 1. 檢查是否已經綁定 Google 帳號
    if not user_data or 'refresh_token' not in user_data:
        auth_link = f"{RENDER_URL}/authorize/{user_id}?openExternalBrowser=1"
        reply_text = f"歡迎！請先授權：\n{auth_link}"
    else:
        user_text = event.message.text.strip()
        msg = user_text.split()
        
        try:
            # 2. 建立 Google API 連線 (財報與記帳共用)
            creds = Credentials(
                token=user_data['token'],
                refresh_token=user_data['refresh_token'],
                token_uri=user_data['token_uri'],
                client_id=user_data['client_id'],
                client_secret=user_data['client_secret'],
                scopes=user_data['scopes']
            )
            drive_service = build('drive', 'v3', credentials=creds)
            sheets_service = build('sheets', 'v4', credentials=creds)
            
            # 3. 取得或自動建立試算表
            spreadsheet_id = user_data.get('spreadsheet_id')
            if not spreadsheet_id:
                results = drive_service.files().list(q="name='LINE_Finance_記帳本' and mimeType='application/vnd.google-apps.spreadsheet'", spaces='drive').execute()
                files = results.get('files', [])
                if files:
                    spreadsheet_id = files[0]['id']
                else:
                    spreadsheet = sheets_service.spreadsheets().create(body={'properties': {'title': 'LINE_Finance_記帳本'}}, fields='spreadsheetId').execute()
                    spreadsheet_id = spreadsheet.get('spreadsheetId')
                    
                    # 初始化 4 欄標題
                    sheets_service.spreadsheets().values().append(
                        spreadsheetId=spreadsheet_id, range="A1",
                        valueInputOption="USER_ENTERED",
                        body={'values': [["日期", "分類", "項目", "金額"]]}
                    ).execute()
                db.reference(f'users/{user_id}').update({'spreadsheet_id': spreadsheet_id})

            # ==========================================
            # 功能 A：產出專屬微型損益表
            # ==========================================
            if user_text == "本月財報":
                # 撈取整張表格前四個欄位
                result = sheets_service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range='A:D').execute()
                values = result.get('values', [])
                
                total_revenue = 0
                total_expense = 0
                
                for row in values[1:]: # 跳過第一行標題
                    if len(row) >= 4:
                        amount = int(row[3])
                        if row[1] == "營業收入":
                            total_revenue += amount
                        else:
                            total_expense += amount
                            
                net_income = total_revenue - total_expense
                
                reply_text = (
                    "📊 【FinanceBot 財務報表】\n"
                    "(*¯︶¯*) 這是您目前的損益結算：\n"
                    "────────────────\n"
                    f"🔹 營業收入： ${total_revenue:,}\n"
                    f"🔻 營業費用： ${total_expense:,}\n"
                    "────────────────\n"
                    f"✨ 本期淨利： ${net_income:,}"
                )

            # ==========================================
            # 功能 B：一鍵重置/升級帳本格式
            # ==========================================
            elif user_text == "重置帳本":
                # 清除範圍 A 到 D 欄
                sheets_service.spreadsheets().values().clear(
                    spreadsheetId=spreadsheet_id,
                    range='A:D'
                ).execute()
                
                # 重新寫入 4 欄標題
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id, range="A1",
                    valueInputOption="USER_ENTERED",
                    body={'values': [["日期", "分類", "項目", "金額"]]}
                ).execute()
                
                reply_text = "✨ 系統重置完畢！\n(*¯︶¯*) 帳本已升級為全新 4 欄格式，舊資料已清除。\n現在可以開始記帳囉！"

            # ==========================================
            # 功能 C：極速記帳與自動分類
            # ==========================================
            elif len(msg) == 2 and msg[1].isdigit():
                item, price = msg[0], int(msg[1])
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                # 自動分類邏輯
                category = "其他費用"
                if item in ["午餐", "晚餐", "早餐", "飲料", "便當", "外送"]:
                    category = "伙食費"
                elif item in ["捷運", "公車", "高鐵", "計程車", "加油"]:
                    category = "交通費"
                elif item in ["薪水", "接案", "獎金"]:
                    category = "營業收入"
                    
                # 寫入 Google Sheets
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id, range="A1",
                    valueInputOption="USER_ENTERED",
                    body={'values': [[now, category, item, price]]}
                ).execute()
                
                reply_text = f"✅ 已紀錄：[{category}] {item} ${price}"

            # ==========================================
            # 例外處理：防呆提示
            # ==========================================
            else:
                reply_text = "格式錯誤！\n📝 記帳請輸入：項目 金額 (例：便當 100)\n📊 查詢請輸入：本月財報\n⚠️ 格式化請輸入：重置帳本"
                
        except Exception as e:
            reply_text = f"⚠️ 系統發生錯誤：{e}"

    # 4. 統一回傳訊息給 LINE 聊天室
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply_text)]
        ))
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
