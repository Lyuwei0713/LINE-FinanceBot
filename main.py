import os
import json
import re
import requests
import yfinance as yf
from datetime import datetime
from flask import Flask, request, abort, redirect
import firebase_admin
from firebase_admin import credentials, db
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import google.generativeai as genai

# ==========================================
# 1. 系統與 AI 初始化
# ==========================================
app = Flask(__name__)
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'

# 初始化 Gemini 服務憑證
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY')
if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)
    ai_model = True
else:
    ai_model = False

# ==========================================
# 2. 讀取環境變數與安全設定 
# ==========================================
LINE_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
RENDER_URL = os.environ.get('RENDER_URL')

# 自動偵測並解析單一 JSON 字串環境變數
G_CLIENT_SECRET_JSON = os.environ.get('G_CLIENT_SECRET_JSON')
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CLIENT_SECRET')

if G_CLIENT_SECRET_JSON:
    try:
        g_data = json.loads(G_CLIENT_SECRET_JSON)
        web_data = g_data.get('web', g_data.get('installed', g_data))
        GOOGLE_CLIENT_ID = web_data.get('client_id', GOOGLE_CLIENT_ID)
        GOOGLE_CLIENT_SECRET = web_data.get('client_secret', GOOGLE_CLIENT_SECRET)
    except Exception as e:
        print(f"解析 G_CLIENT_SECRET_JSON 失敗: {e}")

# 建立 LINE 設定實例
configuration = Configuration(access_token=LINE_ACCESS_TOKEN)
handler = WebhookHandler(LINE_SECRET)

# 初始化 Firebase 節點
if not firebase_admin._apps:
    fb_config_str = os.environ.get('FIREBASE_CONFIG_JSON')
    db_url = os.environ.get('FIREBASE_DB_URL')
    if fb_config_str:
        cred_dict = json.loads(fb_config_str)
        cred = credentials.Certificate(cred_dict)
        firebase_admin.initialize_app(cred, {'databaseURL': db_url})

# ==========================================
# 3. Google OAuth2 授權路由
# ==========================================
@app.route('/authorize/<user_id>')
def authorize(user_id):
    """引導用戶前往 Google 進行安全授權"""
    redirect_uri = f"{RENDER_URL}/oauth2callback"
    scopes = 'https://www.googleapis.com/auth/spreadsheets'
    
    auth_url = (
        f"https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={GOOGLE_CLIENT_ID}&"
        f"redirect_uri={redirect_uri}&"
        f"response_type=code&"
        f"scope={scopes}&"
        f"access_type=offline&"
        f"prompt=consent&"
        f"state={user_id}"
    )
    return redirect(auth_url)

@app.route('/oauth2callback')
def oauth2callback():
    """接收 Google 回傳 Token，並自動建立專屬雲端帳本"""
    code = request.args.get('code')
    user_id = request.args.get('state')
    redirect_uri = f"{RENDER_URL}/oauth2callback"
    token_url = "https://oauth2.googleapis.com/token"
    
    data = {
        'code': code,
        'client_id': GOOGLE_CLIENT_ID,
        'client_secret': GOOGLE_CLIENT_SECRET,
        'redirect_uri': redirect_uri,
        'grant_type': 'authorization_code'
    }
    res = requests.post(token_url, data=data).json()
    
    if 'access_token' in res:
        creds = Credentials(
            token=res['access_token'], refresh_token=res.get('refresh_token'),
            token_uri=token_url, client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET, scopes=['https://www.googleapis.com/auth/spreadsheets']
        )
        sheets_service = build('sheets', 'v4', credentials=creds)
        
        spreadsheet_body = {'properties': {'title': 'FinanceBot 雲端財務帳本'}}
        spreadsheet = sheets_service.spreadsheets().create(body=spreadsheet_body, fields='spreadsheetId').execute()
        spreadsheet_id = spreadsheet.get('spreadsheetId')
        
        sheets_service.spreadsheets().values().append(
            spreadsheetId=spreadsheet_id, range="A1", valueInputOption="USER_ENTERED",
            body={'values': [["日期", "分類", "項目", "金額"]]}
        ).execute()
        
        db.reference(f'users/{user_id}').set({
            'token': res['access_token'],
            'refresh_token': res.get('refresh_token'),
            'token_uri': token_url,
            'client_id': GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'scopes': ['https://www.googleapis.com/auth/spreadsheets'],
            'spreadsheet_id': spreadsheet_id
        })
        
        return "<h3 style='font-family:sans-serif; text-align:center; margin-top:50px; color:#06c755;'>🎉 帳戶授權成功！您的個人獨立雲端帳本已初始化完畢。<br>現在可以關閉此網頁，返回 LINE 開始體驗記帳了！</h3>"
    else:
        return f"<h3 style='font-family:sans-serif; text-align:center; margin-top:50px; color:#dd3333;'>❌ 授權程序失敗，請聯繫系統管理員。<br>日誌摘要：{json.dumps(res)}</h3>"

# ==========================================
# 4. LIFF 數據查詢網頁路由
# ==========================================
@app.route('/liff')
def liff_page():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=0">
        <title>個股數據查詢系統</title>
        <script charset="utf-8" src="https://static.line-scdn.net/liff/edge/2/sdk.js"></script>
        <style>
            body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #f4f5f7; padding: 20px; text-align: center; }
            .container { background: white; padding: 30px 20px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); margin-top: 10px; }
            h3 { color: #222; margin-bottom: 20px; font-size: 16px; font-weight: 600; }
            input { padding: 12px; width: 80%; font-size: 15px; border: 1px solid #ccc; border-radius: 6px; margin-bottom: 15px; text-align: center; outline: none; }
            input:focus { border-color: #00B900; }
            button { padding: 12px 20px; font-size: 15px; background-color: #00B900; color: white; border: none; border-radius: 6px; font-weight: bold; width: 85%; cursor: pointer; }
            button:active { background-color: #009900; }
        </style>
    </head>
    <body>
        <div class="container">
            <h3>📈 個股市場數據查詢</h3>
            <input type="text" id="stockCode" placeholder="請輸入股票代碼 (例：2330 或 AAPL)" style="text-transform: uppercase;">
            <input type="number" id="days" placeholder="請輸入查詢天數 (選填，預設為當日)">
            <button onclick="sendStockCommand()">提交查詢</button>
        </div>
        <script>
            liff.init({ liffId: "2010266740-hdqBlZ15" }).catch(err => console.error(err));
            function sendStockCommand() {
                const code = document.getElementById('stockCode').value.trim().toUpperCase();
                const days = document.getElementById('days').value.trim();
                if (code) {
                    let commandText = '個股 ' + code;
                    if (days) { commandText += ' ' + days; }
                    liff.sendMessages([{ type: 'text', text: commandText }]).then(() => { liff.closeWindow(); }).catch(err => { alert("數據傳輸失敗：" + err); });
                } else { alert("請輸入有效的股票代碼。"); }
            }
        </script>
    </body>
    </html>
    """

# ==========================================
# 5. 加入好友即時推送授權連結機制
# ==========================================
@handler.add(FollowEvent)
def handle_follow(event):
    reply_token = event.reply_token
    user_id = event.source.user_id
    
    auth_link = f"{RENDER_URL}/authorize/{user_id}?openExternalBrowser=1"
    
    welcome_text = (
        "【 FinanceBot 資產管理助理 】\n"
        "═════════════════\n"
        "歡迎使用 FinanceBot 財務數據管理系統。\n\n"
        "本系統提供自動化收支會計紀錄、市場大盤動態及個股歷史數據分析。為啟用您的專屬雲端帳本，請先完成 Google 帳戶安全授權。\n\n"
        "👉 專屬授權連結：\n"
        f"🔗 {auth_link}\n\n"
        "※ 安全提示：若程序中出現「Google 尚未驗證」之警示，請點選【進階】並選擇【允許存取】即可完成安全性綁定。"
    )
    
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=reply_token, messages=[TextMessage(text=welcome_text)]
        ))

# ==========================================
# 6. LINE 訊息處理核心邏輯（模糊比對優化版）
# ==========================================
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    reply_token = event.reply_token
    user_text = event.message.text.strip()
    msg = user_text.split()
    
    reply_text = "⚠️ 未能辨識該指令。請輸入「功能」以獲取系統操作指南。"
    
    try:
        user_id = event.source.user_id
        user_data = db.reference(f'users/{user_id}').get()

        # 權限驗證防禦機制
        if not user_data or 'refresh_token' not in user_data:
            auth_link = f"{RENDER_URL}/authorize/{user_id}?openExternalBrowser=1"
            reply_text = (
                "【 FinanceBot 資產管理助理 】\n"
                "═════════════════\n"
                "歡迎使用 FinanceBot 財務數據管理系統。\n\n"
                "本系統提供自動化收支會計紀錄、市場大盤動態及個股歷史數據分析。為啟用您的專屬雲端帳本，請先完成 Google 帳戶安全授權。\n\n"
                "👉 授權連結：\n"
                f"🔗 {auth_link}\n\n"
                "※ 安全提示：若程序中出現「Google 尚未驗證」之警示，請點選【進階】並選擇【允許存取】即可完成安全性綁定。"
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

            # ---- 智慧語意模糊判定分支 ----
            
            # 1. 操作指南模糊判定
            if any(k in user_text for k in ["功能", "幫助", "help", "指南", "怎麼用", "選單", "指令", "說明"]):
                reply_text = (
                    "📋【 FinanceBot 系統操作指南 】\n"
                    "═════════════════\n"
                    "請依據下列標準格式輸入指令，或搭配圖文選單進行操作：\n\n"
                    "■ 智慧會計記帳\n"
                    "格式 ➔ [項目名稱] [半形空格] [金額]\n"
                    " ▫️ 範例：便當 75\n"
                    " ▫️ 範例：零用錢 150\n"
                    "（系統將自動解析語意並歸類至雲端帳本）\n\n"
                    "■ 市場行情查詢\n"
                    " ▫️ 大盤 ➔ 台灣加權指數當日走勢分析\n"
                    " ▫️ 個股 [代碼] ➔ 指定個股當日詳細 K 線數據\n"
                    " ▫️ 個股 [代碼] [天數] ➔ 指定個股歷史數據變動表\n\n"
                    "■ 雲端帳務管理\n"
                    " ▫️ 帳本 ➔ 獲取雲端試算表帳本直達連結\n"
                    " ▫️ 收支 ➔ 匯總本月財務摘要與分類明細表\n"
                    " ▫️ 重置帳本 ➔ 清空現有數據並還原初始欄位\n"
                    "═════════════════\n"
                    "※ 提示：點擊下方選單可直接開啟網頁介面，進行免空格個股查詢。"
                )

            # 2. 帳本直達連結模糊判定
            elif any(k in user_text for k in ["帳本", "連結", "表格", "試算表", "excel", "查帳", "網址", "我的帳", "雲端帳"]):
                if spreadsheet_id:
                    reply_text = (
                        "📂【 雲端電子帳本直達通道 】\n"
                        "═════════════════\n"
                        "點擊下列連結即可跨平台檢視完整會計流水帳與明細數據：\n\n"
                        f"🔗 https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit?usp=drivesdk"
                    )
                else:
                    reply_text = "⚠️ 系統提示：未偵測到雲端試算表帳本，請輸入「重置帳本」以進行初始化。"

            # 3. 清空與還原帳本模糊判定
            elif any(k in user_text for k in ["重置", "重製", "清空", "洗掉", "刪除帳", "還原帳", "重新初始化"]):
                if spreadsheet_id:
                    sheets_service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range='A:D').execute()
                    sheets_service.spreadsheets().values().append(
                        spreadsheetId=spreadsheet_id, range="A1", valueInputOption="USER_ENTERED",
                        body={'values': [["日期", "分類", "項目", "金額"]]}
                    ).execute()
                    reply_text = "✅ 系統提示：雲端試算表數據已清空，成功初始化會計科目欄位。"

            # 4. 台灣大盤加權指數模糊判定
            elif any(k in user_text for k in ["大盤", "加權指數", "台股走勢", "市場行情"]):
                ticker = yf.Ticker("^TWII", session=req_session)
                hist = ticker.history(period="2d")
                if not hist.empty:
                    price = hist.iloc[-1]['Close']
                    prev_close = hist.iloc[-2]['Close'] if len(hist) > 1 else price
                    change = price - prev_close
                    change_percent = (change / prev_close) * 100 if prev_close else 0
                    sign = "▲" if change > 0 else "▼" if change < 0 else "─"
                    reply_text = (
                        f"📊【 市場行情動態 · 台灣加權指數 】\n"
                        f"═════════════════\n"
                        f" ▪️ 當前指數 ｜ {price:,.2f} 點\n"
                        f" ▪️ 當日漲跌 ｜ {sign} {abs(change):.2f} ({change_percent:+.2f}%)\n"
                        f"═════════════════\n"
                        f"📅 數據時間：{hist.index[-1].strftime('%Y-%m-%d')}"
                    )
                else:
                    reply_text = "❌ 系統提示：無法取得大盤即時數據，請稍後再試。"

            # 5. 本月財務摘要模糊判定
            elif any(k in user_text for k in ["收支", "財報", "財務報告", "花多少", "結餘", "帳目", "統計"]):
                if not spreadsheet_id:
                    reply_text = "⚠️ 系統提示：未偵測到雲端試算表帳本，請輸入「重置帳本」以進行初始化。"
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
                        f"💰【 本月財務收支摘要報告 】\n"
                        f"═════════════════\n"
                        f" 🟢 總計收入 ｜ ${total_income:,}\n"
                        f" 🔴 總計支出 ｜ ${total_expense:,}\n"
                        f"═════════════════\n"
                        f"📋【 各項目會計明細 】\n"
                    )
                    if expense_detail:
                        for cat, amt in expense_detail.items():
                            reply_text += f" ▫️ {cat} ｜ ${amt:,}\n"
                    else:
                        reply_text += "   當前無任何會計紀錄。\n"
                        
                    reply_text += (
                        f"═════════════════\n"
                        f"🔹 本期淨結餘 ｜ ${net_balance:,}"
                    )

            # 6. 個股數據查詢 (Token 提取免空格技術)
            elif any(k in user_text for k in ["個股", "股票"]):
                tokens = re.findall(r'[A-Za-z0-9]+', user_text)
                if not tokens:
                    reply_text = "⚠️ 系統提示：未能從指令中提取有效的證券代碼。\n▫️ 範例：個股 2330"
                else:
                    ticker_input = tokens[0].upper()
                    is_single_day = len(tokens) == 1
                    
                    fetch_period = "1y" if is_single_day else f"{int(tokens[1]) + 5}d"
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
                                f"📈【 數據分析報告 · {ticker_input} 】\n"
                                f"═════════════════\n"
                                f" ▪️ 當前收盤 ｜ ${price:.2f}\n"
                                f" ▪️ 當日漲跌 ｜ {sign} {abs(change):.2f} ({change_percent:+.2f}%)\n"
                                f" ▪️ 當日開盤 ｜ ${open_p:.2f}\n"
                                f" ▪️ 當日最高 ｜ ${high_p:.2f}\n"
                                f" ▪️ 當日最低 ｜ ${low_p:.2f}\n"
                                f" ▪️ 成交股數 ｜ {int(latest['Volume']):,} 股\n"
                                f"═════════════════\n"
                                f" ▪️ 52週最高 ｜ ${w52_high:.2f}\n"
                                f" ▪️ 52週最低 ｜ ${w52_low:.2f}\n"
                                f"═════════════════\n"
                                f"📅 數據時間：{hist.index[-1].strftime('%Y-%m-%d')}"
                            )
                        else:
                            days = int(tokens[1])
                            sub_hist = hist.tail(days)
                            reply_text = f"📊【 歷史交易數據變動表 · {ticker_input} 】\n═════════════════\n"
                            for date, row in reversed(list(sub_hist.iterrows())):
                                reply_text += f"📅 {date.strftime('%m/%d')} ｜ 收盤: ${row['Close']:.2f} ｜ 高: ${row['High']:.2f} ｜ 低: ${row['Low']:.2f}\n"
                    else:
                        reply_text = f"❌ 系統提示：於市場數據庫中未搜尋到代號「{ticker_input}」，請確認後重試。"

            # 7. 智慧語意解析記帳 (標準輸入阻斷)
            elif len(msg) == 2 and msg[1].isdigit():
                item, price = msg[0], int(msg[1])
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                category = "其他支出"
                ai_status = "❌ 語意解析模組未啟動 (環境設定異常)"
                is_ai_success = False
                
                if ai_model:
                    prompt = (
                        f"你是一個客觀、嚴謹的財務會計分類系統。請幫我將這個收支項目「{item}」進行精準分類。\n"
                        f"你【只能】從以下選項中挑選一個完全相同的詞彙作為你的回答，絕對不能包含任何其他字詞、解釋或符號：\n"
                        f"[伙食費, 交通費, 娛樂費, 日用品, 帳單費, 收入, 其他支出]\n\n"
                        f"【會計準則指引】\n"
                        f"- 任何餐飲、外送、食材採購 ➔ 伙食費\n"
                        f"- 任何遊戲儲值、電影、數位娛樂訂閱、休閒娛樂 ➔ 娛樂費\n"
                        f"- 運輸交通、計程車、燃油支出、大眾運輸 ➔ 交通費\n"
                        f"- 薪資收入、業務報酬、投資收益 ➔ 收入\n"
                        f"- 生活常用品、清潔用品、醫療保健 ➔ 日用品\n"
                        f"- 公共事業規費、電信費、保險費、固定帳單 ➔ 帳單費\n\n"
                        f"核心要求：請保持回答的純淨度，只需直接輸出對應的科目名稱即可。"
                    )
                    
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
                            ai_status = f"💥 模組錯誤 ({model_name}): {str(e)}"
                            continue
                
                if spreadsheet_id:
                    sheets_service.spreadsheets().values().append(
                        spreadsheetId=spreadsheet_id, range="A1", valueInputOption="USER_ENTERED",
                        body={'values': [[now, category, item, price]]}
                    ).execute()
                    
                    if is_ai_success:
                        reply_text = f"✅ 帳務紀錄成功：[{category}] {item} ${price}"
                    else:
                        reply_text = f"✅ 帳務紀錄成功：[{category}] {item} ${price}\n🔧 系統日誌：{ai_status}"
                else:
                    reply_text = "⚠️ 系統提示：未偵測到雲端試算表帳本，請輸入「重置帳本」以進行初始化。"

    except ValueError:
        reply_text = "⚠️ 【資料型態衝突】\n請檢查雲端試算表內「金額」欄位是否包含非數字符號。"
    except Exception as e:
        if "invalid_grant" in str(e).lower():
            reply_text = "⚠️ 【憑證失效】\n安全性金鑰已過期，請隨意輸入文字重新觸發 Google 帳戶安全授權程序。"
        else:
            reply_text = f"⚠️ 系統核心處理異常，程序已安全中斷。\n(錯誤代碼：{str(e)[:40]})"

    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=reply_token, messages=[TextMessage(text=reply_text)]
        ))

# ==========================================
# 7. LINE Webhook 接收通道
# ==========================================
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except Exception as e:
        print(f"Callback Verification Failed: {e}")
        abort(400)
    return 'OK'

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
