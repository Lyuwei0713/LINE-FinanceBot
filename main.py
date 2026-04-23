import os
import json
from flask import Flask, request, abort, redirect, url_for
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

app = Flask(__name__)

# --- 設定環境變數 ---
LINE_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
RENDER_URL = os.environ.get('RENDER_URL') # 例如 https://xxx.onrender.com

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)
SCOPES = ['https://www.googleapis.com/auth/drive.file']

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    handler.handle(body, signature)
    return 'OK'

@app.route("/authorize/<user_id>")
def authorize(user_id):
    # 建立授權流
    flow = Flow.from_client_secrets_file(
        'client_secret.json',
        scopes=SCOPES,
        redirect_uri=f"{RENDER_URL}/oauth2callback"
    )
    # 把 user_id 放在 state 裡傳給 Google，回傳時才知道是誰
    authorization_url, state = flow.authorization_url(access_type='offline', state=user_id)
    return redirect(authorization_url)

@app.route("/oauth2callback")
def oauth2callback():
    user_id = request.args.get('state')
    # 這裡應該要將取得的 credentials 存入資料庫 (例如 Firebase)
    # 為了教學簡化，我們先確認能換到 token
    return f"授權成功！{user_id} 您的記帳表已準備好，請回到 LINE。"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    # 這裡之後要寫邏輯：檢查資料庫是否有此 user_id 的 token
    # 如果沒有，回傳授權連結：
    auth_link = f"{RENDER_URL}/authorize/{user_id}"
    reply_text = f"歡迎使用！請先點擊以下連結授權 Google 權限：\n{auth_link}"
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply_text)]
        ))
