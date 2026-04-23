import os
import json
import firebase_admin
from firebase_admin import credentials, db
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from datetime import datetime

app = Flask(__name__)

# --- 1. 初始化 Firebase ---
if not firebase_admin._apps:
    cred = credentials.Certificate('firebase_admin.json')
    firebase_admin.initialize_app(cred, {
        'databaseURL': 'https://financebot-db-default-rtdb.firebaseio.com/'
    })

# --- 2. 設定 LINE 與 Google 參數 ---
LINE_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
RENDER_URL = os.environ.get('RENDER_URL')

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)
# 權限包含：建立檔案、編輯試算表
SCOPES = ['https://www.googleapis.com/auth/drive.file', 'https://www.googleapis.com/auth/spreadsheets']

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        print(f"Error: {e}")
        abort(400)
    return 'OK'

@app.route("/authorize/<user_id>")
def authorize(user_id):
    flow = Flow.from_client_secrets_file('client_secret.json', scopes=SCOPES, redirect_uri=f"{RENDER_URL}/oauth2callback")
    # access_type='offline' 才能拿到 refresh_token，讓機器人永久記帳
    authorization_url, state = flow.authorization_url(access_type='offline', prompt='consent', state=user_id)
    return redirect(authorization_url)

@app.route("/oauth2callback")
def oauth2callback():
    user_id = request.args.get('state')
    flow = Flow.from_client_secrets_file('client_secret.json', scopes=SCOPES, redirect_uri=f"{RENDER_URL}/oauth2callback")
    flow.fetch_token(authorization_response=request.url)
    creds = flow.credentials
    
    # 存入 Firebase
    db.reference(f'users/{user_id}').update({
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': creds.scopes
    })
    return "✅ 授權成功！請回到 LINE 開始記帳（格式：項目 金額）。"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_data = db.reference(f'users/{user_id}').get()

    # 第一階段：檢查是否有授權
    if not user_data or 'refresh_token' not in user_data:
        auth_link = f"{RENDER_URL}/authorize/{user_id}?openExternalBrowser=1"
        reply_text = f"歡迎使用！請先點擊連結授權您的 Google 帳號，我才能幫您在您的雲端硬碟建立記帳表：\n{auth_link}"
    else:
        # 第二階段：解析訊息
        msg = event.message.text.split()
        if len(msg) == 2 and msg[1].isdigit():
            item, price = msg[0], msg[1]
            try:
                # 建立 Google API 憑證物件
                creds = Credentials(
                    token=user_data['token'],
                    refresh_token=user_data['refresh_token'],
                    token_uri=user_data['token_uri'],
                    client_id=user_data['client_id'],
                    client_secret=user_data['client_secret'],
                    scopes=user_data['scopes']
                )
                
                # 連結 Google Drive & Sheets
                drive_service = build('drive', 'v3', credentials=creds)
                sheets_service = build('sheets', 'v4', credentials=creds)

                # 尋找是否已有記帳表
                spreadsheet_id = user_data.get('spreadsheet_id')
                if not spreadsheet_id:
                    # 搜尋雲端硬碟中是否有同名的表
                    results = drive_service.files().list(q="name='LINE_Finance_記帳本' and mimeType='application/vnd.google-apps.spreadsheet'", spaces='drive').execute()
                    files = results.get('files', [])
                    if files:
                        spreadsheet_id = files[0]['id']
                    else:
                        # 新建一個試算表
                        spreadsheet = {'properties': {'title': 'LINE_Finance_記帳本'}}
                        spreadsheet = sheets_service.spreadsheets().create(body=spreadsheet, fields='spreadsheetId').execute()
                        spreadsheet_id = spreadsheet.get('spreadsheetId')
                        # 初始化標題欄位
                        sheets_service.spreadsheets().values().append(
                            spreadsheetId=spreadsheet_id, range="A1",
                            valueInputOption="USER_ENTERED",
                            body={'values': [["日期", "項目", "金額"]]}
                        ).execute()
                    # 存回 Firebase
                    db.reference(f'users/{user_id}').update({'spreadsheet_id': spreadsheet_id})

                # 寫入資料
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id, range="A1",
                    valueInputOption="USER_ENTERED",
                    body={'values': [[now, item, price]]}
                ).execute()
                
                reply_text = f"✅ 已紀錄：{item} ${price}\n資料已存入您的 Google Drive 試算表。"
            except Exception as e:
                print(f"Google API Error: {e}")
                reply_text = "⚠️ 寫入失敗，可能是憑證過期，請嘗試重新點擊授權連結。"
        else:
            reply_text = "格式請輸入：項目 金額\n例如：便當 100"

    # 回覆 LINE 訊息
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply_text)]
        ))

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
