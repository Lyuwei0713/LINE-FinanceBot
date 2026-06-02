import os
import json
import requests
import yfinance as yf
from datetime import datetime
from flask import Flask, request, abort
import firebase_admin
from firebase_admin import credentials, db
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import google.generativeai as genai

# ==========================================
# 1. 系統與 AI 初始化
# ==========================================
app = Flask(__name__)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# 初始化 Gemini AI
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    ai_model = genai.GenerativeModel('gemini-1.5-flash')
else:
    ai_model = None

# ==========================================
# 2. 讀取環境變數
# ==========================================
LINE_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
RENDER_URL = os.environ.get('RENDER_URL')

configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

if not firebase_admin._apps:
    fb_config_str = os.environ.get('FIREBASE_CONFIG_JSON')
    db_url = os.environ.get('FIREBASE_DB_URL')
    if fb_config_str:
        cred_dict = json.loads(fb_config_str)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {'databaseURL': db_url})

# ==========================================
# 3. LIFF 股票查詢專屬網頁路由
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
            body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background-color: #f4f5f7; padding: 20px; text-align: center; }
            .container { background: white; padding: 30px 20px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); margin-top: 10px; }
            h3 { color: #333; margin-bottom: 20px; }
            input { padding: 15px; width: 80%; font-size: 16px; border: 1px solid #ddd; border-radius: 8px; margin-bottom: 20px; text-align: center; outline: none; }
            input:focus { border-color: #00B900; }
            button { padding: 14px 20px; font-size: 16px; background-color: #00B900; color: white; border: none; border-radius: 8px; font-weight: bold; width: 85%; cursor: pointer; }
            button:active { background-color: #009900; }
        </style>
    </head>
    <body>
        <div class="container">
            <h3>📈 個股快速查詢</h3>
            <input type="text" id="stockCode" placeholder="輸入代碼 (例：2330 或 AAPL)" style="text-transform: uppercase;">
            <input type="number" id="days" placeholder="查詢天數 (選填，預設為當日)">
            <button onclick="sendStockCommand()">立即查詢</button>
        </div>
        <script>
            liff.init({ liffId: "2010266740-hdqBlZ15" }).catch(err => console.error(err));
            function sendStockCommand() {
                const code = document.getElementById('stockCode').value.trim().toUpperCase();
                const days = document.getElementById('days').value.trim();
                if (code) {
                    let commandText = '個股 ' + code;
                    if (days) { commandText += ' ' + days; }
                    liff.sendMessages([{ type: 'text', text: commandText }]).then(() => { liff.closeWindow(); }).catch(err => { alert("傳送失敗：" + err); });
                } else { alert("請先輸入股票代碼！"); }
            }
        </script>
    </body>
    </html>
    """

# ==========================================
# 4. LINE 機器人訊息處理邏輯
# ==========================================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    reply_token = event.reply_token
    user_text = event.message.text.strip()
    msg = user_text.split()
    
    reply_text = "⚠️ 無法辨識您的指令。\n💡 請輸入『功能』查看完整選單。"
    
    try:
        user_id = event.source.user_id
        user_data = db.reference(f'users/{user_id}').get()

        if not user_data or 'refresh_token' not in user_data:
            auth_link = f"{RENDER_URL}/authorize/{user_id}?openExternalBrowser=1"
            reply_text = f"歡迎使用 FinanceBot，請先點擊連結完成 Google 授權：\n{auth_link}"
        else:
            creds = Credentials(
                token=user_data['token'], refresh_token=user_data['refresh_token'],
                token_uri=user_data['token_uri'], client_id=user_data['client_id'],
                client_secret=user_data['client_secret'], scopes=user_data['scopes']
            )
            sheets_service = build('sheets', 'v4', credentials=creds)
            spreadsheet_id = user_data.get('spreadsheet_id')

            req_session = requests.Session()
            req_session.headers.update({"User-Agent": "Mozilla/5.0"})

            # --- 功能表 ---
            if user_text in ["功能", "幫助", "help", "功能指南"]:
                reply_text = (
                    "📊 【FinanceBot 系統指令指南】\n"
                    "📈 1. 個股查詢：`個股 2330` 或由選單開啟網頁\n"
                    "💰 2. 本月收支：輸入 `收支` 查看結算\n"
                    "📊 3. 大盤走勢：輸入 `大盤`\n"
                    "📝 4. AI 智慧記帳：直接輸入 `項目 金額` (例：搭計程車 250)\n"
                    "⚠️ 5. 重置系統：輸入 `重置帳本`"
                )

            elif user_text in ["大盤", "大盤走勢"]:
                ticker = yf.Ticker("^TWII", session=req_session)
                hist = ticker.history(period="2d")
                if not hist.empty:
                    price = hist.iloc[-1]['Close']
                    prev_close = hist.iloc[-2]['Close'] if len(hist) > 1 else price
                    change = price - prev_close
                    change_percent = (change / prev_close) * 100 if prev_close else 0
                    sign = "▲" if change > 0 else "▼" if change < 0 else "─"
                    reply_text = f"📊 【今日大盤分析 - 台灣加權指數】\n────────────────\n🔹 當前指數：{price:,.2f}\n🔸 今日漲跌：{sign} {abs(change):.2f} ({change_percent:+.2f}%)\n────────────────\n📅 資料時間：{hist.index[-1].strftime('%Y-%m-%d')}"
                else:
                    reply_text = "❌ 目前無法取得大盤資料。"

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
                            if category == "收入": total_income += amount
                            else:
                                total_expense += amount
                                expense_detail[category] = expense_detail.get(category, 0) + amount
                    net_balance = total_income - total_expense
                    reply_text = f"📊 【本月收支理財儀表板】\n────────────────\n💰 總收入： ${total_income:,}\n💸 總支出： ${total_expense:,}\n────────────────\n💡 【各項支出明細】\n"
                    if expense_detail:
                        for cat, amt in expense_detail.items(): reply_text += f" ▫️ {cat}： ${amt:,}\n"
                    else: reply_text += "  暫無任何支出紀錄。\n"
                    reply_text += f"────────────────\n✨ 本月結餘： ${net_balance:,}"

            elif user_text == "重置帳本":
                if spreadsheet_id:
                    sheets_service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range='A:D').execute()
                    sheets_service.spreadsheets().values().append(
                        spreadsheetId=spreadsheet_id, range="A1", valueInputOption="USER_ENTERED",
                        body={'values': [["日期", "分類", "項目", "金額"]]}
                    ).execute()
                    reply_text = "✅ 系統重置完畢，帳本已更新為初始狀態。"

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
                        try:
                            stock = yf.Ticker(t, session=req_session)
                            hist = stock.history(period=f"{days}d")
                            if not hist.empty: break
                        except Exception:
                            continue
                            
                    if hist is not None and not hist.empty:
                        if len(stock_args) == 1:
                            latest = hist.iloc[-1]
                            price = latest['Close']
                            prev_close = hist.iloc[-2]['Close'] if len(hist) > 1 else price
                            change = price - prev_close
                            change_percent = (change / prev_close) * 100 if prev_close else 0
                            sign = "▲" if change > 0 else "▼" if change < 0 else "─"
                            reply_text = f"📈 【個股當日行情 - {ticker_input}】\n────────────────\n🔹 當前收盤：${price:.2f}\n🔸 今日漲跌：{sign} {abs(change):.2f} ({change_percent:+.2f}%)\n🔹 成交股數：{int(latest['Volume']):,} 股\n────────────────\n📅 資料時間：{hist.index[-1].strftime('%Y-%m-%d')}"
                        else:
                            reply_text = f"📊 【{ticker_input} 過去 {days} 天歷史資訊】\n────────────────\n"
                            for date, row in reversed(list(hist.iterrows())):
                                reply_text += f"📅 {date.strftime('%m/%d')} | 收盤: ${row['Close']:.2f}\n"
                    else:
                        reply_text = f"❌ 找不到股票代號：{ticker_input}。"

            # AI 智慧記帳判斷
            elif len(msg) == 2 and msg[1].isdigit():
                item, price = msg[0], int(msg[1])
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                category = "其他支出"
                
                if ai_model:
                    prompt = f"你是一個專業的理財記帳助手。請幫我將這個消費項目「{item}」進行分類。你只能從以下選項中【挑選一個】作為回答：[伙食費, 交通費, 娛樂費, 日用品, 帳單費, 收入, 其他支出]。注意：絕對不要輸出任何解釋或標點符號，只要回答那三個字或四個字即可。"
                    try:
                        ai_response = ai_model.generate_content(prompt)
                        predicted_category = ai_response.text.strip()
                        if predicted_category in ["伙食費", "交通費", "娛樂費", "日用品", "帳單費", "收入", "其他支出"]:
                            category = predicted_category
                    except Exception:
                        pass
                
                if spreadsheet_id:
                    sheets_service.spreadsheets().values().append(
                        spreadsheetId=spreadsheet_id, range="A1", valueInputOption="USER_ENTERED",
                        body={'values': [[now, category, item, price]]}
                    ).execute()
                    reply_text = f"✅ 已紀錄：[{category}✨] {item} ${price}"
                else:
                    reply_text = "⚠️ 找不到帳本，請先輸入『重置帳本』。"

    except ValueError:
        reply_text = "⚠️ 【資料格式異常】\n請檢查 Google 試算表欄位是否誤填。"
    except Exception as e:
        if "invalid_grant" in str(e).lower():
            reply_text = "⚠️ 【授權狀態失效】\n請隨意輸入文字重新點擊連結綁定 Google 帳號。"
        else:
            reply_text = f"⚠️ 系統處理時發生異常，請稍後再試。\n(錯誤代碼：{str(e)[:40]})"

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=reply_token, messages=[TextMessage(text=reply_text)]
        ))

# ==========================================
# 5. Webhook 與啟動
# ==========================================
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        print(f"Error: {e}")
        abort(400)
    return 'OK'

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
