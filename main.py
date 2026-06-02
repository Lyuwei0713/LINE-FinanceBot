import os
import json
import requests
import firebase_admin
import secrets
import hashlib
import base64
import yfinance as yf
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

# 這是最重要的 Webhook 接收點，沒有它 LINE 就找不到門進來
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature) # 這裡會呼叫你初始化的 handler
    except Exception as e:
        print(f"Error: {e}")
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

# ==========================================
# 新增：LIFF 股票查詢專屬網頁
# ==========================================
@app.route('/liff')
def liff_page():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=0">
        <title>個股快速查詢</title>
        <script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
        <style>
            body { 
                font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; 
                background-color: #f4f5f7; 
                padding: 20px; 
                text-align: center; 
            }
            .container { 
                background: white; 
                padding: 30px 20px; 
                border-radius: 12px; 
                box-shadow: 0 4px 12px rgba(0,0,0,0.05); 
                margin-top: 10px; 
            }
            h3 { color: #333; margin-bottom: 20px; font-size: 18px; }
            input { 
                padding: 15px; 
                width: 80%; 
                font-size: 18px; 
                border: 1px solid #ddd; 
                border-radius: 8px; 
                margin-bottom: 20px; 
                text-align: center; 
                outline: none;
            }
            input:focus { border-color: #00B900; }
            button { 
                padding: 14px 20px; 
                font-size: 16px; 
                background-color: #00B900; 
                color: white; 
                border: none; 
                border-radius: 8px; 
                font-weight: bold; 
                width: 85%; 
                cursor: pointer; 
            }
            button:active { background-color: #009900; }
        </style>
    </head>
    <body>
        <div class="container">
            <h3>📈 請輸入欲查詢的股票代號</h3>
            <input type="text" id="stockCode" placeholder="例如：2330 或 AAPL" inputmode="numeric">
            <br>
            <button onclick="sendStockCommand()">立即查詢</button>
        </div>

        <script>
            // 系統初始化 (LIFF ID 暫時留空，我們下一步會填入)
            liff.init({ liffId: "2010266740-hdqBlZ15" }).catch(err => console.error(err));

            function sendStockCommand() {
                const code = document.getElementById('stockCode').value.trim();
                if (code) {
                    // 透過 LINE 發送隱藏指令
                    liff.sendMessages([{
                        type: 'text',
                        text: '個股 ' + code
                    }]).then(() => {
                        // 成功後自動關閉視窗
                        liff.closeWindow();
                    }).catch(err => {
                        alert("傳送失敗，請確認網路狀態：" + err);
                    });
                } else {
                    alert("請先輸入股票代號！");
                }
            }
        </script>
    </body>
    </html>
    """

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    reply_token = event.reply_token
    user_text = event.message.text.strip()
    
    # 1. 預設回覆：確保在任何情況下 reply_text 都有值
    reply_text = "⚠️ 無法辨識您的指令，請輸入『功能』查看完整選單。"
    
    try:
        # 先獲取使用者基礎資料
        user_id = event.source.user_id
        user_data = db.reference(f'users/{user_id}').get()

        # 檢查授權狀態
        if not user_data or 'refresh_token' not in user_data:
            reply_text = f"歡迎使用 FinanceBot，請點擊此處進行 Google 授權：{RENDER_URL}/authorize/{user_id}"
        else:
            # 初始化 Google 服務
            creds = Credentials(...) # (此處維持你原本的 Credentials 設定)
            sheets_service = build('sheets', 'v4', credentials=creds)
            spreadsheet_id = user_data.get('spreadsheet_id')

            # 2. 邏輯判斷 (優化：加入寬鬆匹配)
            if user_text in ["功能", "幫助", "help", "功能指南"]:
                reply_text = "📈 【FinanceBot 選單】\n[個股 代號] 查詢行情\n[大盤] 查看加權指數\n[收支] 查詢本月統計"
            
            elif user_text in ["大盤", "大盤走勢"]:
                ticker = yf.Ticker("^TWII", session=req_session)
                hist = ticker.history(period="1d")
                price = hist['Close'].iloc[-1] if not hist.empty else 0
                reply_text = f"📊 目前台股加權指數：{price:,.2f} 點。"

            elif "收支" in user_text or "財報" in user_text:
                # 這裡執行你原本的試算表統計邏輯
                reply_text = "💰 已為您整理本月收支資料..."

            elif user_text.startswith("個股"):
                # 這裡執行你原本的個股查詢邏輯
                reply_text = "📈 正在查詢個股報價..."

    except Exception as e:
        # 發生錯誤時，將錯誤變成文字回覆給你看
        reply_text = f"⚠️ 系統偵測到錯誤，請將此訊息提供給管理員：{str(e)[:50]}"

    # 3. 統一發送回覆 (確保無論成功或失敗，機器人絕對會動)
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=reply_text)]
        ))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
