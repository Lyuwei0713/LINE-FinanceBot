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
from linebot.v3.messaging import (
    Configuration, 
    ApiClient, 
    MessagingApi, 
    ReplyMessageRequest, 
    TextMessage, 
    PushMessageRequest
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import datetime

app = Flask(__name__)
# 允許非 HTTPS 跳轉
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# --- 1. 初始化 Firebase (安全版) ---
if not firebase_admin._apps:
    # 從環境變數讀取 JSON 字串，避免直接上傳金鑰檔
    firebase_config = os.environ.get('FIREBASE_CONFIG_JSON')
    if firebase_config:
        cred_dict = json.loads(firebase_config)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {
            'databaseURL': 'https://financebot-db-default-rtdb.firebaseio.com/'
        })
    else:
        # 這是為了防錯，如果環境變數沒設定好會提醒你
        print("Error: FIREBASE_CONFIG_JSON not found in environment variables")

# --- 2. 設定參數 ---
LINE_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
RENDER_URL = os.environ.get('RENDER_URL')

# 讀取 Google Client Config (同樣從環境變數讀取)
G_CONFIG = os.environ.get('G_CLIENT_SECRET_JSON')
if G_CONFIG:
    GOOGLE_CLIENT_CONFIG = json.loads(G_CONFIG)
else:
    print("Error: G_CLIENT_SECRET_JSON not found in environment variables")

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)
# 權限字串
SCOPES = 'https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/spreadsheets'

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        print(f"Webhook Error: {e}")
        abort(400)
    return 'OK'

@app.route("/authorize/<user_id>")
def authorize(user_id):
    with open('client_secret.json', 'r') as f:
        client_config = json.load(f)['web']
    
    # --- 手動生成 PKCE 驗證碼 ---
    code_verifier = secrets.token_urlsafe(64)
    code_challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).decode().replace('=', '').replace('+', '-').replace('/', '_')
    
    # 暫存 Verifier 到 Firebase，以便 callback 時取出
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

@app.route("/oauth2callback")
def oauth2callback():
    code = request.args.get('code')
    user_id = request.args.get('state')
    
    # 拿回 Verifier
    temp_ref = db.reference(f'temp_auth/{user_id}').get()
    if not temp_ref:
        return "驗證逾時或狀態錯誤，請重新從 LINE 點擊連結。"
    code_verifier = temp_ref['verifier']

    with open('client_secret.json', 'r') as f:
        client_config = json.load(f)['web']

    # 手動 Request 換取 Token
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

    # 正式存入用戶資料
    db.reference(f'users/{user_id}').update({
        'token': token_data.get('access_token'),
        'refresh_token': token_data.get('refresh_token'),
        'token_uri': "https://oauth2.googleapis.com/token",
        'client_id': client_config['client_id'],
        'client_secret': client_config['client_secret'],
        'scopes': SCOPES.split()
    })
    
    db.reference(f'temp_auth/{user_id}').delete()

    # 主動發送教學訊息
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            welcome_text = "🎊 授權成功！現在您可以開始記帳了。\n\n📍 格式：項目 金額\n範例：早餐 80"
            line_bot_api.push_message(PushMessageRequest(to=user_id, messages=[TextMessage(text=welcome_text)]))
    except: pass

    return """
    <div style="font-family: sans-serif; text-align: center; padding: 50px;">
        <h1 style="color: #00B900;">✅ 授權成功！</h1>
        <p>請回到 LINE 聊天室，我已經把教學傳給您了。</p>
    </div>
    """

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_data = db.reference(f'users/{user_id}').get()

    if not user_data or 'refresh_token' not in user_data:
        auth_link = f"{RENDER_URL}/authorize/{user_id}?openExternalBrowser=1"
        reply_text = f"歡迎！請先授權 Google 帳號以建立記帳本：\n{auth_link}"
    else:
        msg = event.message.text.split()
        if len(msg) == 2 and msg[1].isdigit():
            item, price = msg[0], msg[1]
            try:
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

                spreadsheet_id = user_data.get('spreadsheet_id')
                if not spreadsheet_id:
                    results = drive_service.files().list(q="name='LINE_Finance_記帳本' and mimeType='application/vnd.google-apps.spreadsheet'", spaces='drive').execute()
                    files = results.get('files', [])
                    if files:
                        spreadsheet_id = files[0]['id']
                    else:
                        spreadsheet = sheets_service.spreadsheets().create(body={'properties': {'title': 'LINE_Finance_記帳本'}}, fields='spreadsheetId').execute()
                        spreadsheet_id = spreadsheet.get('spreadsheetId')
                        sheets_service.spreadsheets().values().append(
                            spreadsheetId=spreadsheet_id, range="A1",
                            valueInputOption="USER_ENTERED",
                            body={'values': [["日期", "項目", "金額"]]}
                        ).execute()
                    db.reference(f'users/{user_id}').update({'spreadsheet_id': spreadsheet_id})

                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id, range="A1",
                    valueInputOption="USER_ENTERED",
                    body={'values': [[now, item, price]]}
                ).execute()
                reply_text = f"✅ 已紀錄：{item} ${price}"
            except Exception as e:
                print(f"Record Error: {e}")
                reply_text = "⚠️ 紀錄失敗，請重新授權。"
        else:
            reply_text = "格式：項目 金額（例如：便當 100）"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply_text)]
        ))

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
