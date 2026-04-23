import os
import json
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
# 關鍵：允許 OAuth 在本地/非標准環境下運行，解決部分跳轉問題
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
    # 不傳送 code_challenge_method，徹底避開 PKCE 問題
    authorization_url, state = flow.authorization_url(
        access_type='offline',
        prompt='consent',
        state=user_id
    )
    return redirect(authorization_url)

@app.route("/oauth2callback")
def oauth2callback():
    # 從 URL 參數中拿回當初存進去的 user_id (即 state)
    user_id = request.args.get('state')
    
    flow = Flow.from_client_secrets_file(
        'client_secret.json', 
        scopes=SCOPES, 
        redirect_uri=f"{RENDER_URL}/oauth2callback"
    )
    
    try:
        # 強制使用授權回應的 URL 換取 Token
        flow.fetch_token(authorization_response=request.url)
    except Exception as e:
        print(f"Fetch Token Error: {e}")
        return f"授權失敗，請重新從 LINE 點擊連結。錯誤資訊：{e}"

    creds = flow.credentials
    
    # 1. 存入 Firebase
    db.reference(f'users/{user_id}').update({
        'token': creds.token,
        'refresh_token': creds.refresh_token,
        'token_uri': creds.token_uri,
        'client_id': creds.client_id,
        'client_secret': creds.client_secret,
        'scopes': creds.scopes
    })

    # 2. 推播成功訊息
    try:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            welcome_text = "🎊 授權成功！現在您可以開始記帳了。\n\n📍 格式：項目 金額"
            line_bot_api.push_message(PushMessageRequest(
                to=user_id, 
                messages=[TextMessage(text=welcome_text)]
            ))
    except:
        pass

    return '<h1 style="text-align:center;padding-top:50px;font-family:sans-serif;">✅ 授權成功！請回到 LINE</h1>'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_data = db.reference(f'users/{user_id}').get()

    if not user_data or 'refresh_token' not in user_data:
        auth_link = f"{RENDER_URL}/authorize/{user_id}?openExternalBrowser=1"
        reply_text = f"歡迎！請先授權 Google 帳號：\n{auth_link}"
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
                        spreadsheet = {'properties': {'title': 'LINE_Finance_記帳本'}}
                        spreadsheet = sheets_service.spreadsheets().create(body=spreadsheet, fields='spreadsheetId').execute()
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
            reply_text = "格式：項目 金額（例：晚餐 100）"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply_text)]
        ))

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
