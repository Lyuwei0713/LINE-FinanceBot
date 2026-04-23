import os
from flask import Flask, request, abort
import requests
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)


LINE_ACCESS_TOKEN = '5jhOdDGeTSmFacb5B+LlxoU0v3gDfBoFQ7dtOkqGH1XjxbFeU0W7rCbNEpIl4SburH287JeYzd9BM7PJLmXMkfaUelsxq0tyeY7kXiVcJbb4C+Y52V9jLNNlRFtlyH7UseXHL7BCdGV97LPnAOsnowdB04t89/1O/w1cDnyilFU='
LINE_SECRET = 'f73eb030318f5abb7ecbcdab8fa20d6f'
GAS_URL = 'https://script.google.com/macros/s/AKfycbyUGvT0RiXWtVyFwbk_NZ8_3r-ZYPPhM3BZR4fwl8iRTWt4FtBhlG6h4ID_uWp5Z8MvnQ/exec' 

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_msg = event.message.text
    # 核心修改：抓取使用者的唯一識別碼 (user_id)
    user_id = event.source.user_id 
    
    parts = user_msg.split()
    
    if len(parts) == 2 and parts[1].isdigit():
        item = parts[0]
        price = parts[1]
        
        # 打包資料：包含使用者 ID、項目和金額
        payload = {
            "user_id": user_id, 
            "item": item, 
            "price": price
        }
        
        try:
            # 將資料推送到 GAS
            requests.post(GAS_URL, json=payload)
            reply_text = f"✅ 已為您記錄：{item} ${price}"
        except:
            reply_text = "⚠️ 系統連線失敗，請稍後再試。"
    else:
        reply_text = "格式錯誤！請輸入：項目 金額（例如：晚餐 200）"

    # 回覆訊息
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)]
            )
        )

if __name__ == "__main__":
    # 這裡必須抓環境變數的 PORT，預設給 8080
    port = int(os.environ.get('PORT', 8080))
    # host 必須是 0.0.0.0，這樣外部才連得進來
    app.run(host='0.0.0.0', port=port)
