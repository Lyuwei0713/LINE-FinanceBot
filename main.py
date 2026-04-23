import os
import json
import requests
import firebase_admin
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
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import datetime

app = Flask(__name__)
# 允許 OAuth 在非 HTTPS 環境下進行某些跳轉（對偵錯有幫助）
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# --- 1. 初始化 Firebase ---
if not firebase_admin._apps:
    cred = credentials.Certificate('firebase_admin.json')
    firebase_admin.initialize_app(cred, {
        'databaseURL': 'https://financebot-db-default-rtdb.firebaseio.com/'
    })

# --- 2. 設定參數 ---
LINE_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
RENDER_URL = os.environ.get('RENDER_URL')

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)
SCOPES = ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/spreadsheets']

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
    flow = Flow.from_client_secrets_file(
        'client_secret.json', 
        scopes=SCOPES, 
        redirect_uri=f"{RENDER_URL}/oauth2callback"
    )
    # 這裡產生的 URL 會引導使用者去 Google 登入
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        state=user_id
    )
    return redirect(authorization_url)

@app.route("/oauth2callback")
def oauth2callback():
    # 從 Google 回傳的網址中取得 code 與 state (user_id)
    code = request.args.get('code')
    user_id = request.args.get('state')
    
    if not code:
        return "授權失敗，未取得驗證碼。"

    # 讀取 client_secret.json
    with open('client_secret.json', 'r') as f:
        client_config = json.load(f)['web']

    # --- 關鍵修正：手動使用 requests 向 Google 換取 Token，避開 PKCE 檢查 ---
    token_url = "https://oauth2.googleapis.com/token"
    payload = {
        'code': code,
        'client_id': client_config['client_id'],
        'client_secret': client_config['client_secret'],
        'redirect_uri': f"{RENDER_URL}/oauth2callback",
        'grant_type': 'authorization_code'
    }
    
    res = requests.post(token_url, data=payload)
    token_data = res.json()

    if 'error' in token_data:
        print(f"Google Token Error: {token_data}")
        return f"授權發生錯誤：{token_data.get('error_description')}"

    # 存入 Firebase
    db.reference(f'users/{user_id}').update({
        'token': token_data.get('access_token'),
        'refresh_token': token_data.get('refresh_token'),
        'token_uri': "https://oauth2.googleapis.com/token",
        'client_id': client_config['client_id'],
        'client_secret': client_config['client_secret'],
        'scopes': SCOPES
    })

    # 主動發送 LINE 訊息通知使用者
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            welcome_text = "🎊 授權成功！現在您可以開始記帳了。\n\n📍 格式：項目 金額\n範例：午餐 120"
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, 
                messages=[TextMessage(text=welcome_text)]
            ))
    except Exception as e:
        print(f"Push Message Error: {e}")

    return '<h1 style="text-align:center;padding-top:50px;font-family:sans-serif;color:#00B900;">✅ 授權成功！請回到 LINE</h1>'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_data = db.reference(f'users/{user_id}').get()

    if not user_data or 'refresh_token' not in user_data:
        auth_link = f"{RENDER_URL}/authorize/{user_id}?openExternalBrowser=1"
        reply_text = f"歡迎使用！請先點擊連結授權您的 Google 帳號：\n{auth_link}"
    else:
        msg = event.message.text.split()
        if len(msg) == 2 and msg[1].isdigit():
            item, price = msg[0], msg[1]
            try:
                # 重新建立憑證用於讀取 Sheet
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
                reply_text = "⚠️ 紀錄失敗，請重新點擊授權連結。"
        else:
            reply_text = "格式：項目 金額（例：早餐 80）"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply_text)]
        ))

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
