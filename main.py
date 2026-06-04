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

# 初始化 Gemini AI 基本設定
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    ai_model = True  # 標記金鑰存在
else:
    ai_model = False

# ==========================================
# 2. 讀取環境變數與設定
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
            h3 { color: #333; margin-bottom: 20px; font-size: 18px; }
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
            reply_text = (
                "✨ 歡迎使用 FinanceBot 財務管家 ✨\n"
                "═════════════════\n"
                "您的專屬 AI 理財助理已成功連線！\n\n"
                "💡 核心功能亮點：\n"
                "🔹 隨手記帳 ➔ AI 自動精準分類\n"
                "🔹 行情追蹤 ➔ 即時大盤與詳細 K 線\n"
                "🔹 雲端報表 ➔ 隨時匯總試算表\n\n"
                "🚀 請先點擊下方連結完成 Google 授權：\n"
                f"🔗 {auth_link}\n\n"
                "⚠️ 提示：若出現「Google 尚未驗證」，請點選【進階】➔【允許存取】即可安全啟用。"
            )
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

            # --- 指令分支 ---
            if user_text in ["功能", "幫助", "help", "功能指南"]:
                reply_text = (
                    "📱【 FinanceBot 功能指令指南 】\n"
                    "═════════════════\n"
                    "官方全新升級為【AI 智能語意版】\n\n"
                    "📝 AI 智慧記帳\n"
                    "👉 直接輸入「項目 金額」\n"
                    " ▫️ 範例：`去路易莎喝拿鐵 145`\n"
                    " ▫️ 範例：`發放接案獎金 15000`\n\n"
                    "📈 投資行情查詢\n"
                    "👉 輸入 `大盤` ➔ 查台股指數分析\n"
                    "👉 輸入 `個股 2330` ➔ 查今日詳細行情\n"
                    "👉 輸入 `個股 2330 5` ➔ 查 5 日歷史 K 線\n\n"
                    "💰 雲端財務報表\n"
                    "👉 輸入 `收支` ➔ 查看明細與本月結餘\n"
                    "👉 輸入 `重置帳本` ➔ 清空現有測試數據\n"
                    "═════════════════\n"
                    "💡 提示：點擊下方圖文選單可直接開啟「網頁介面」免空格查詢股票！"
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
                    reply_text = (
                        f"📊【 今日大盤分析 · 台灣加權 】\n"
                        f"═════════════════\n"
                        f" 🔹 當前指數 ｜ {price:,.2f} 點\n"
                        f" 🔸 今日漲跌 ｜ {sign} {abs(change):.2f} ({change_percent:+.2f}%)\n"
                        f"═════════════════\n"
                        f"📅 資料時間：{hist.index[-1].strftime('%Y-%m-%d')}"
                    )
                else:
                    reply_text = "❌ 目前無法取得大盤資料，請稍後再試。"

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
                    
                    reply_text = (
                        f"💰【 本月收支理財儀表板 】\n"
                        f"═════════════════\n"
                        f" 🟢 總 收 入 ｜ ${total_income:,}\n"
                        f" 🔴 總 支 出 ｜ ${total_expense:,}\n"
                        f"═════════════════\n"
                        f"💡【 各項支出明細 】\n"
                    )
                    if expense_detail:
                        for cat, amt in expense_detail.items():
                            reply_text += f" ▫️ {cat} ｜ ${amt:,}\n"
                    else:
                        reply_text += "   暫無任何支出紀錄。\n"
                        
                    reply_text += (
                        f"═════════════════\n"
                        f"✨ 本月結餘 ｜ ${net_balance:,}"
                    )

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
                    is_single_day = len(stock_args) == 1
                    
                    fetch_period = "1y" if is_single_day else f"{int(stock_args[1]) + 5}d"
                    tickers_to_try = [f"{ticker_input}.TW", f"{ticker_input}.TWO"] if ticker_input.isdigit() else [ticker_input]
                    
                    hist = None
                    for t in tickers_to_try:
                        try:
                            stock = yf.Ticker(t, session=req_session)
                            hist = stock.history(period=fetch_period)
                            if not hist.empty: break
                        except Exception:
                            continue
                            
                    if hist is not None and not hist.empty:
                        if is_single_day:
                            latest = hist.iloc[-1]
                            price = latest['Close']
                            open_p = latest['Open']
                            high_p = latest['High']
                            low_p = latest['Low']
                            
                            prev_close = hist.iloc[-2]['Close'] if len(hist) > 1 else price
                            change = price - prev_close
                            change_percent = (change / prev_close) * 100 if prev_close else 0
                            sign = "▲" if change > 0 else "▼" if change < 0 else "─"
                            
                            w52_high = hist['High'].max()
                            w52_low = hist['Low'].min()
                            
                            reply_text = (
                                f"📈【 個股詳細行情 · {ticker_input} 】\n"
                                f"═════════════════\n"
                                f" 💰 當前收盤 ｜ ${price:.2f}\n"
                                f" 🔸 今日漲跌 ｜ {sign} {abs(change):.2f} ({change_percent:+.2f}%)\n"
                                f" 🚪 今日開盤 ｜ ${open_p:.2f}\n"
                                f" 🔝 今日最高 ｜ ${high_p:.2f}\n"
                                f" 📉 今日最低 ｜ ${low_p:.2f}\n"
                                f" 📊 成交股數 ｜ {int(latest['Volume']):,} 股\n"
                                f"═════════════════\n"
                                f" ⭐ 52週最高 ｜ ${w52_high:.2f}\n"
                                f" 🌙 52週最低 ｜ ${w52_low:.2f}\n"
                                f"═════════════════\n"
                                f"📅 資料時間：{hist.index[-1].strftime('%Y-%m-%d')}"
                            )
                        else:
                            days = int(stock_args[1])
                            sub_hist = hist.tail(days)
                            reply_text = f"📊【 {ticker_input} 過去 {days} 天歷史資訊 】\n═════════════════\n"
                            for date, row in reversed(list(sub_hist.iterrows())):
                                reply_text += f"📅 {date.strftime('%m/%d')} ｜ 收盤: ${row['Close']:.2f} ｜ 高: ${row['High']:.2f} ｜ 低: ${row['Low']:.2f}\n"
                    else:
                        reply_text = f"❌ 找不到股票代號：{ticker_input}。"

            # 功能六：AI 智慧語意記帳 (換裝 2026 最新大腦模型池)
            elif len(msg) == 2 and msg[1].isdigit():
                item, price = msg[0], int(msg[1])
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                category = "其他支出"
                ai_status = "❌ AI大腦未啟動 (環境變數缺失)"
                is_ai_success = False
                
                if ai_model:
                    prompt = (
                        f"你是一個極度專業且嚴格的理財記帳助手。請幫我將這個消費/收入項目「{item}」進行精準分類。\n"
                        f"你【只能】從以下選項中挑選一個完全相同的詞彙作為你的回答，絕對不能自己發明新詞：\n"
                        f"[伙食費, 交通費, 娛樂費, 日用品, 帳單費, 收入, 其他支出]\n\n"
                        f"【分類原則指引】\n"
                        f"- 任何早餐、午餐、晚餐、飲料、大餐、外送、買菜 ➔ 伙食費\n"
                        f"- 任何儲值、課金、手遊、買遊戲、看電影、唱歌、Netflix或Spotify訂閱、買玩具 ➔ 娛樂費\n"
                        f"- 搭車、計程車、捷運、加油、高鐵、車輛維修 ➔ 交通費\n"
                        f"- 薪水、獎金、接案、外快、股票股息 ➔ 收入\n"
                        f"- 衛生紙、洗面乳、藥品、生活五金工具 ➔ 日用品\n"
                        f"- 水電費、電話費、瓦斯費、網路費、保險費 ➔ 帳單費\n\n"
                        f"⚠️ 核心要求：絕對不要輸出任何解釋、標點符號、引號、括號或多餘空白，只需要回答那三個字或四個字即可。"
                    )
                    
                    # 🚀 更換為 2026 現行可用的最新模型池
                    model_pool = ['gemini-3.5-flash', 'gemini-2.5-flash', 'gemini-3.1-flash-lite']
                    
                    for model_name in model_pool:
                        try:
                            current_model = genai.GenerativeModel(model_name)
                            ai_response = current_model.generate_content(prompt)
                            predicted_category = ai_response.text.strip().replace("'", "").replace('"', '').replace("「", "").replace("」", "")
                            
                            possible_categories = ["伙食費", "交通費", "娛樂費", "日用品", "帳單費", "收入", "其他支出"]
                            for cat in possible_categories:
                                if cat in predicted_category:
                                    category = cat
                                    ai_status = "OK"
                                    is_ai_success = True
                                    break
                            
                            if is_ai_success:
                                break
                        except Exception as e:
                            ai_status = f"💥 {model_name}錯誤: {str(e)}"
                            continue
                
                if spreadsheet_id:
                    sheets_service.spreadsheets().values().append(
                        spreadsheetId=spreadsheet_id, range="A1", valueInputOption="USER_ENTERED",
                        body={'values': [[now, category, item, price]]}
                    ).execute()
                    
                    if is_ai_success:
                        reply_text = f"✅ 已紀錄：[{category}✨] {item} ${price}"
                    else:
                        reply_text = f"✅ 已紀錄：[{category}☁️] {item} ${price}\n🔧 診斷提示：{ai_status}"
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
# 5. LINE Webhook 通道
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
