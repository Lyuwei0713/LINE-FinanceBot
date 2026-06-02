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
           <body>
    <div class="container">
        <h3>📈 個股快速查詢</h3>
        
        <input type="text" id="stockCode" placeholder="輸入代碼 (例：2330 或 AAPL)" style="text-transform: uppercase;">
        <br>
        
        <input type="number" id="days" placeholder="查詢天數 (選填，預設為當日)">
        <br>
        
        <button onclick="sendStockCommand()">立即查詢</button>
    </div>

    <script>
        // 記得把這裡的 LIFF ID 換成你自己的！
        liff.init({ liffId: "YOUR_LIFF_ID_HERE" }).catch(err => console.error(err));

        function sendStockCommand() {
            // 抓取兩個輸入框的值
            const code = document.getElementById('stockCode').value.trim().toUpperCase();
            const days = document.getElementById('days').value.trim();
            
            if (code) {
                // 自動幫使用者把「代號」跟「天數」用空格組合起來
                let commandText = '個股 ' + code;
                if (days) {
                    commandText += ' ' + days;
                }

                // 透過 LINE 發送隱藏指令
                liff.sendMessages([{
                    type: 'text',
                    text: commandText
                }]).then(() => {
                    // 成功後自動關閉視窗
                    liff.closeWindow();
                }).catch(err => {
                    alert("傳送失敗，請確認網路狀態：" + err);
                });
            } else {
                alert("請先輸入股票代碼！");
            }
        }
    </script>
</body>
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
    msg = user_text.split()
    
    # 1. 預設回覆，防止任何未定義狀況
    reply_text = "⚠️ 無法辨識您的指令。\n💡 請輸入『功能』查看完整選單。"
    
    try:
        user_id = event.source.user_id
        user_data = db.reference(f'users/{user_id}').get()

        # 檢查授權
        if not user_data or 'refresh_token' not in user_data:
            auth_link = f"{RENDER_URL}/authorize/{user_id}?openExternalBrowser=1"
            reply_text = (
                "歡迎使用 FinanceBot 財務管家。\n\n"
                "請先點擊下方連結完成 Google 授權：\n"
                f"{auth_link}\n\n"
                "⚠️ 若出現「Google 尚未驗證」，請點擊左下角【進階】並允許存取。"
            )
        else:
            # --- 初始化 Google API ---
            creds = Credentials(
                token=user_data['token'],
                refresh_token=user_data['refresh_token'],
                token_uri=user_data['token_uri'],
                client_id=user_data['client_id'],
                client_secret=user_data['client_secret'],
                scopes=user_data['scopes']
            )
            sheets_service = build('sheets', 'v4', credentials=creds)
            spreadsheet_id = user_data.get('spreadsheet_id')

            # 建立網路請求 Session (給股票查詢用)
            import requests
            req_session = requests.Session()
            req_session.headers.update({"User-Agent": "Mozilla/5.0"})

            # ==========================================
            # 功能一：功能指南
            # ==========================================
            if user_text in ["功能", "幫助", "help", "功能指南"]:
                reply_text = (
                    "📊 【FinanceBot 系統指令指南】\n"
                    "📈 1. 個股查詢：`個股 2330` 或 `個股 2330 5` (查5天)\n"
                    "💰 2. 本月收支：輸入 `收支` 查看結算\n"
                    "📊 3. 大盤走勢：輸入 `大盤`\n"
                    "📝 4. 快速記帳：`便當 100`\n"
                    "⚠️ 5. 重置系統：輸入 `重置帳本`"
                )

            # ==========================================
            # 功能二：大盤走勢
            # ==========================================
            elif user_text in ["大盤", "大盤走勢"]:
                ticker = yf.Ticker("^TWII", session=req_session)
                hist = ticker.history(period="2d")
                if not hist.empty:
                    price = hist.iloc[-1]['Close']
                    prev_close = hist.iloc[-2]['Close'] if len(hist) > 1 else price
                    change = price - prev_close
                    change_percent = (change / prev_close) * 100 if prev_close else 0
                    sign = "▲" if change > 0 else "▼" if change < 0 else "─"
                    
                    reply_text = (
                        f"📊 【今日大盤分析 - 台灣加權指數】\n"
                        f"────────────────\n"
                        f"🔹 當前指數：{price:,.2f}\n"
                        f"🔸 今日漲跌：{sign} {abs(change):.2f} ({change_percent:+.2f}%)\n"
                        f"────────────────\n"
                        f"📅 資料時間：{hist.index[-1].strftime('%Y-%m-%d')}"
                    )
                else:
                    reply_text = "❌ 目前無法取得大盤資料，請稍後再試。"

            # ==========================================
            # 功能三：本月收支結算
            # ==========================================
            elif "收支" in user_text or "財報" in user_text:
                if not spreadsheet_id:
                    reply_text = "⚠️ 找不到您的試算表，請輸入『重置帳本』來建立。"
                else:
                    result = sheets_service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range='A:D').execute()
                    values = result.get('values', [])
                    
                    total_income, total_expense = 0, 0
                    expense_detail = {}
                    
                    for row in values[1:]:
                        if len(row) >= 4:
                            category, amount = row[1], int(row[3])
                            if category == "收入":
                                total_income += amount
                            else:
                                total_expense += amount
                                expense_detail[category] = expense_detail.get(category, 0) + amount
                                
                    net_balance = total_income - total_expense
                    
                    reply_text = f"📊 【本月收支理財儀表板】\n────────────────\n💰 總收入： ${total_income:,}\n💸 總支出： ${total_expense:,}\n────────────────\n💡 【各項支出明細】\n"
                    if expense_detail:
                        for cat, amt in expense_detail.items():
                            reply_text += f" ▫️ {cat}： ${amt:,}\n"
                    else:
                        reply_text += "  暫無任何支出紀錄。\n"
                    reply_text += f"────────────────\n✨ 本月結餘： ${net_balance:,}"

            # ==========================================
            # 功能四：重置帳本
            # ==========================================
            elif user_text == "重置帳本":
                if spreadsheet_id:
                    sheets_service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range='A:D').execute()
                    sheets_service.spreadsheets().values().append(
                        spreadsheetId=spreadsheet_id, range="A1",
                        valueInputOption="USER_ENTERED",
                        body={'values': [["日期", "分類", "項目", "金額"]]}
                    ).execute()
                    reply_text = "✅ 系統重置完畢，帳本已更新為初始狀態。"

            # ==========================================
            # 功能五：股票查詢
            # ==========================================
            elif user_text.startswith("個股"):
                stock_args = user_text.replace("個股", "").strip().split()
                if len(stock_args) == 0:
                    reply_text = "⚠️ 請在「個股」後面加上半形空格與代號。\n▫️ 範例：`個股 2330`"
                else:
                    ticker_input = stock_args[0].upper()
                    days = 2 if len(stock_args) == 1 else int(stock_args[1])
                    tickers_to_try = [f"{ticker_input}.TW", f"{ticker_input}.TWO"] if ticker_input.isdigit() else [ticker_input]
                    
                    hist = None
                    for t in tickers_to_try:
                        stock = yf.Ticker(t, session=req_session)
                        hist = stock.history(period=f"{days}d")
                        if not hist.empty:
                            break
                            
                    if hist is not None and not hist.empty:
                        if len(stock_args) == 1:
                            latest = hist.iloc[-1]
                            price = latest['Close']
                            prev_close = hist.iloc[-2]['Close'] if len(hist) > 1 else price
                            change = price - prev_close
                            change_percent = (change / prev_close) * 100 if prev_close else 0
                            sign = "▲" if change > 0 else "▼" if change < 0 else "─"
                            
                            reply_text = (
                                f"📈 【個股當日行情 - {ticker_input}】\n"
                                f"────────────────\n"
                                f"🔹 當前收盤：${price:.2f}\n"
                                f"🔸 今日漲跌：{sign} {abs(change):.2f} ({change_percent:+.2f}%)\n"
                                f"🔹 成交股數：{int(latest['Volume']):,} 股\n"
                                f"────────────────\n"
                                f"📅 資料時間：{hist.index[-1].strftime('%Y-%m-%d')}"
                            )
                        else:
                            reply_text = f"📊 【{ticker_input} 過去 {days} 天歷史資訊】\n────────────────\n"
                            for date, row in reversed(list(hist.iterrows())):
                                reply_text += f"📅 {date.strftime('%m/%d')} | 收盤: ${row['Close']:.2f}\n"
                    else:
                        reply_text = f"❌ 找不到股票代號：{ticker_input}，請確認代碼是否正確。"

            # ==========================================
            # 功能六：極速記帳
            # ==========================================
            elif len(msg) == 2 and msg[1].isdigit():
                item, price = msg[0], int(msg[1])
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                category = "其他支出"
                if item in ["午餐", "晚餐", "早餐", "飲料", "便當", "外送"]: category = "伙食費"
                elif item in ["捷運", "公車", "高鐵", "計程車", "加油"]: category = "交通費"
                elif item in ["薪水", "接案", "獎金", "零用錢", "股息"]: category = "收入"
                
                if spreadsheet_id:
                    sheets_service.spreadsheets().values().append(
                        spreadsheetId=spreadsheet_id, range="A1",
                        valueInputOption="USER_ENTERED",
                        body={'values': [[now, category, item, price]]}
                    ).execute()
                    reply_text = f"✅ 已紀錄：[{category}] {item} ${price}"
                else:
                    reply_text = "⚠️ 找不到帳本，請先輸入『重置帳本』。"

    except ValueError as ve:
        reply_text = "⚠️ 【資料格式異常】\n請檢查 Google 試算表「金額」欄位是否誤填了文字或符號。"
    except Exception as e:
        error_msg = str(e).lower()
        if "invalid_grant" in error_msg:
            reply_text = "⚠️ 【授權狀態失效】\n請隨意輸入文字，點擊系統提供的連結重新綁定 Google 帳號。"
        else:
            reply_text = f"⚠️ 系統處理時發生異常，請稍後再試。\n(錯誤代碼：{str(e)[:40]})"

    # 統一發送回覆
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=reply_text)]
        ))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
