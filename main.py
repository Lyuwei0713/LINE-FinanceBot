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

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data()
    
    try:
        # 這裡會觸發你寫在下方的 @handler.add 邏輯
        handler.handle(body.decode('utf-8'), signature)
        
    except Exception as e:
        # 這次我們強制把底層的 Traceback 全部印出來！
        error_trace = traceback.format_exc()
        print("====== 案發現場開始 ======")
        print(f"錯誤類型: {type(e)}")
        print(f"錯誤訊息: {e}")
        print(f"詳細追蹤:\n{error_trace}")
        print("====== 案發現場結束 ======")
        
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

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_id = event.source.user_id
    user_data = db.reference(f'users/{user_id}').get()

    if not user_data or 'refresh_token' not in user_data:
        auth_link = f"{RENDER_URL}/authorize/{user_id}?openExternalBrowser=1"
        reply_text = (
            "歡迎使用 FinanceBot 財務管家系統。\n\n"
            "請先點擊下方連結完成 Google 帳號授權，以啟用雲端記帳與試算表功能：\n"
            f"{auth_link}\n\n"
            "⚠️ 【 授權提示 】\n"
            "若出現「Google 尚未驗證」警告，請點擊左下角【進階】，並選擇【前往... (不安全)】允許存取即可。"
        )
    else:
        user_text = event.message.text.strip()
        msg = user_text.split()
        
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
                        body={'values': [["日期", "分類", "項目", "金額"]]}
                    ).execute()
                db.reference(f'users/{user_id}').update({'spreadsheet_id': spreadsheet_id})

            # ==========================================
            # 🌟 優化核心：建立偽裝瀏覽器的 Session
            # ==========================================
            custom_session = requests.Session()
            custom_session.headers.update({
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5"
            })

            # ==========================================
            # 功能一：功能指南 
            # ==========================================
            if user_text in ["功能", "幫助", "help", "指令", "功能指南"]:
                reply_text = (
                    "📊 【FinanceBot 系統指令指南】\n"
                    "為您提供四大核心財務功能：\n\n"
                    "📈 1. 【個股行情】\n"
                    "👉 查當天：`個股 [代號]` (例：`個股 2330`)\n"
                    "👉 查歷史：`個股 [代號] [天數]`\n\n"
                    "💰 2. 【本月收支】\n"
                    "👉 輸入 `本月收支` 查看分類明細與結算\n"
                    "👉 (記帳格式：`[項目] [金額]`，例：`便當 100`)\n\n"
                    "📊 3. 【大盤走勢】\n"
                    "👉 輸入 `大盤走勢` 查看今日台股加權指數\n\n"
                    "📖 4. 【功能指南】\n"
                    "👉 顯示此功能說明書\n\n"
                    "⚠️ 系統指令：輸入 `重置帳本` 可清空所有紀錄"
                )

          # ==========================================
            # 功能二：大盤走勢分析 (直連台灣證交所官方 API)
            # ==========================================
            elif user_text in ["大盤走勢", "大盤"]:
                try:
                    # 捨棄 Yahoo Finance，直接呼叫 TWSE 官方即時 JSON 介面
                    url = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw&json=1&delay=0"
                    
                    # 發送請求 (附帶偽裝標頭確保連線順暢)
                    res = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=5)
                    data = res.json()
                    
                    if "msgArray" in data and len(data["msgArray"]) > 0:
                        info = data["msgArray"][0]
                        
                        # 解析官方資料欄位 (z:最新指數, y:昨收, h:最高, l:最低)
                        z_val = info.get("z", "-")
                        # 若遇盤前未開盤狀態，將最新價暫時代入昨收價防呆
                        current_price = float(z_val) if z_val != "-" else float(info.get("y", 0))
                        prev_close = float(info.get("y", 0))
                        high = float(info.get("h", current_price))
                        low = float(info.get("l", current_price))
                        
                        change = current_price - prev_close
                        change_percent = (change / prev_close) * 100 if prev_close != 0 else 0.0
                        
                        sign = "▲" if change > 0 else "▼" if change < 0 else "─"
                        
                        if change > 0:
                            trend_text = "市場呈現上漲趨勢，請持續留意後續動能。"
                        elif change < 0:
                            trend_text = "市場呈現下跌趨勢，建議審慎評估部位風險。"
                        else:
                            trend_text = "大盤平盤整理，建議持續觀察。"
                            
                        # 格式化日期時間 (將 20260602 轉為 2026-06-02)
                        date_str = info.get('d', '')
                        formatted_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}" if len(date_str) == 8 else date_str
                        
                        reply_text = (
                            f"📊 【今日大盤分析 - 台灣加權指數】\n"
                            f"────────────────\n"
                            f"🔹 當前指數：{current_price:,.2f}\n"
                            f"🔸 今日漲跌：{sign} {abs(change):.2f} ({change_percent:+.2f}%)\n"
                            f"🔹 今日最高：{high:,.2f}\n"
                            f"🔸 今日最低：{low:,.2f}\n"
                            f"────────────────\n"
                            f"💡 系統觀察：{trend_text}\n"
                            f"📅 資料時間：{formatted_date} {info.get('t', '')}"
                        )
                    else:
                        reply_text = "❌ 無法解析證交所資料，請稍後再試。"
                except Exception as e:
                    reply_text = f"❌ 讀取大盤時發生連線錯誤：{e}"
            # ==========================================
            # 功能三：升級版收支報告 
            # ==========================================
            elif user_text in ["本月財報", "本月收支"]:
                result = sheets_service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range='A:D').execute()
                values = result.get('values', [])
                
                total_income = 0
                total_expense = 0
                expense_detail = {}
                
                for row in values[1:]:
                    if len(row) >= 4:
                        category = row[1]
                        amount = int(row[3])
                        
                        if category == "收入":
                            total_income += amount
                        else:
                            total_expense += amount
                            expense_detail[category] = expense_detail.get(category, 0) + amount
                            
                net_balance = total_income - total_expense
                
                reply_text = (
                    "📊 【本月收支理財儀表板】\n"
                    "────────────────\n"
                    f"💰 總收入： ${total_income:,}\n"
                    f"💸 總支出： ${total_expense:,}\n"
                    "────────────────\n"
                    "💡 【各項支出明細】\n"
                )
                
                if expense_detail:
                    for cat, amt in expense_detail.items():
                        reply_text += f" ▫️ {cat}： ${amt:,}\n"
                else:
                    reply_text += "  暫無任何支出紀錄。\n"
                    
                reply_text += (
                    "────────────────\n"
                    f"✨ 本月結餘： ${net_balance:,}"
                )

            # ==========================================
            # 功能四：一鍵重置帳本
            # ==========================================
            elif user_text == "重置帳本":
                sheets_service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range='A:D').execute()
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id, range="A1",
                    valueInputOption="USER_ENTERED",
                    body={'values': [["日期", "分類", "項目", "金額"]]}
                ).execute()
                reply_text = "✅ 系統重置完畢，帳本已更新為初始狀態。"

            # ==========================================
            # 功能五：股票查詢系統 (導入 Session)
            # ==========================================
            elif user_text.startswith("個股"):
                stock_args = user_text.replace("個股", "").strip().split()
                
                if len(stock_args) == 0:
                    reply_text = (
                        "⚠️ 【股票查詢格式不完整】\n"
                        "系統未偵測到您想查詢的股票代號。\n\n"
                        "💡 修正建議：\n"
                        "請在「個股」後面加上半形空格與代號。\n"
                        "▫️ 查當日範例：`個股 2330`\n"
                        "▫️ 查歷史範例：`個股 2330 5`"
                    )
                else:
                    ticker_input = stock_args[0].upper()
                    
                    if len(stock_args) >= 2 and not stock_args[1].isdigit():
                        reply_text = (
                            "⚠️ 【查詢天數格式異常】\n"
                            "歷史天數必須是「純阿拉伯數字」。\n\n"
                            "💡 修正建議：\n"
                            "正確範例請輸入：`個股 2330 5` (代表查詢過去 5 天)"
                        )
                    else:
                        days = 2 if len(stock_args) == 1 else int(stock_args[1])
                        tickers_to_try = [f"{ticker_input}.TW", f"{ticker_input}.TWO"] if ticker_input.isdigit() else [ticker_input]
                        
                        hist = None
                        for t in tickers_to_try:
                            # 加入偽裝 Session 進行查詢
                            stock = yf.Ticker(t, session=custom_session)
                            hist = stock.history(period=f"{days}d")
                            if not hist.empty:
                                break
                                
                        if hist is not None and not hist.empty:
                            if len(stock_args) == 1:
                                latest = hist.iloc[-1]
                                price = latest['Close']
                                if len(hist) > 1:
                                    prev_close = hist.iloc[-2]['Close']
                                    change = price - prev_close
                                    change_percent = (change / prev_close) * 100
                                else:
                                    change, change_percent = 0.0, 0.0
                                sign = "▲" if change > 0 else "▼" if change < 0 else "─"
                                reply_text = (
                                    f"📈 【個股當日行情 - {ticker_input}】\n"
                                    f"────────────────\n"
                                    f"🔹 當前收盤：${price:.2f}\n"
                                    f"🔸 今日漲跌：{sign} {abs(change):.2f} ({change_percent:+.2f}%)\n"
                                    f"🔹 今日最高：${latest['High']:.2f}\n"
                                    f"🔸 今日最低：${latest['Low']:.2f}\n"
                                    f"🔹 成交股數：{int(latest['Volume']):,} 股\n"
                                    f"────────────────\n"
                                    f"📅 資料時間：{hist.index[-1].strftime('%Y-%m-%d')}"
                                )
                            elif len(stock_args) >= 2:
                                reply_text = f"📊 【{ticker_input} 過去 {days} 天歷史資訊】\n────────────────\n"
                                for date, row in reversed(list(hist.iterrows())):
                                    date_str = date.strftime('%m/%d')
                                    reply_text += f"📅 {date_str} | 收盤: ${row['Close']:.2f} | 總量: {int(row['Volume'])/1000:,.0f}K 股\n"
                                reply_text += "────────────────\n💡 註：成交量 K 代表千股。"
                        else:
                            reply_text = (
                                f"❌ 【找不到股票代號：{ticker_input}】\n"
                                "資料庫中無法取得該股票的報價資訊。\n\n"
                                "💡 修正建議：\n"
                                "1. 請確認代碼是否輸入正確。\n"
                                "2. 若為剛上市櫃之新股，API 可能尚未建檔。"
                            )

            # ==========================================
            # 功能六：極速記帳與自動分類
            # ==========================================
            elif len(msg) == 2 and msg[1].isdigit():
                item, price = msg[0], int(msg[1])
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                
                category = "其他支出"
                if item in ["午餐", "晚餐", "早餐", "飲料", "便當", "外送"]:
                    category = "伙食費"
                elif item in ["捷運", "公車", "高鐵", "計程車", "加油"]:
                    category = "交通費"
                elif item in ["薪水", "接案", "獎金", "零用錢", "股息"]:
                    category = "收入"
                    
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id, range="A1",
                    valueInputOption="USER_ENTERED",
                    body={'values': [[now, category, item, price]]}
                ).execute()
                reply_text = f"✅ 已紀錄：[{category}] {item} ${price}"

            else:
                reply_text = "⚠️ 無法辨識指令。\n💡 請輸入『功能』查看完整系統指令清單。"
                
        # ==========================================
        # 錯誤捕捉與友善引導系統
        # ==========================================
        except ValueError as ve:
            reply_text = (
                "⚠️ 【資料格式異常】\n"
                "系統在結算財報時，發現了無法計算的內容。\n\n"
                "💡 修正建議：\n"
                "請打開您的 Google 試算表，檢查「金額」欄位是否不小心輸入了文字或符號（例如：1,000、$100、一百）。請將它們改回純數字格式即可！\n\n"
                f"🔧 (系統除錯代碼：{ve})"
            )
            
        except Exception as e:
            error_msg = str(e).lower()
            if "invalid_grant" in error_msg or "refresh_token" in error_msg:
                reply_text = (
                    "⚠️ 【授權狀態失效】\n"
                    "系統與您的 Google 雲端硬碟失去了連線。\n\n"
                    "💡 修正建議：\n"
                    "可能是授權已過期，或是您曾更改過密碼。請在聊天室輸入任意文字，系統會重新給您授權連結，點擊重新綁定即可恢復！"
                )
            elif "not found" in error_msg or "404" in error_msg:
                reply_text = (
                    "⚠️ 【找不到雲端帳本】\n"
                    "系統無法在您的 Google 雲端硬碟中找到記帳本。\n\n"
                    "💡 修正建議：\n"
                    "請確認您沒有不小心刪除名為「LINE_Finance_記帳本」的檔案。您可以直接對我輸入『重置帳本』，我會為您重新建立一份全新的！"
                )
            elif "quota" in error_msg or "rate limit" in error_msg or "429" in error_msg or "too many requests" in error_msg:
                reply_text = (
                    "⚠️ 【系統線路壅塞】\n"
                    "目前查詢資料庫的頻率太高，觸發了安全保護機制。\n\n"
                    "💡 修正建議：\n"
                    "請稍作休息，大約 1~2 分鐘後再重新輸入指令查詢即可！"
                )
            else:
                reply_text = (
                    "⚠️ 【發生預期外的狀況】\n"
                    "抱歉，系統遇到了一個未知的錯誤。\n\n"
                    "💡 修正建議：\n"
                    "請將下方的除錯代碼截圖提供給開發人員進行維修：\n"
                    f"🔧 ({e})"
                )

    # 4. 統一回傳訊息給 LINE 聊天室
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply_text)]
        ))
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
