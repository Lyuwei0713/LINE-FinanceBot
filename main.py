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

    # 1. 檢查是否已經綁定 Google 帳號
    if not user_data or 'refresh_token' not in user_data:
        auth_link = f"{RENDER_URL}/authorize/{user_id}?openExternalBrowser=1"
        reply_text = (
            "歡迎使用 FinanceBot！✨\n"
            "請先點擊下方連結完成 Google 帳號授權，才能開啟智慧記帳功能喔：\n"
            f"{auth_link}\n\n"
            "⚠️ 【 授權小提示 】\n"
            "若跳出「Google 尚未驗證」或「不安全」的警告，請安心點擊左下角【進階】，並選擇【前往... (不安全)】勾選允許即可！"
        )
    else:
        user_text = event.message.text.strip()
        msg = user_text.split()
        
        try:
            # 2. 建立 Google API 連線
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
            
            # 3. 取得試算表 ID
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
            # 功能一：功能導覽提示 
            # ==========================================
            if user_text in ["功能", "幫助", "help", "指令"]:
                reply_text = (
                    "🌟 【FinanceBot 功能指令大全】\n"
                    "(*¯︶¯*) 主人～我是您的專屬財務管家！\n\n"
                    "📝 1. 極速記帳\n"
                    "👉 格式：`[項目] [金額]` (例：`便當 100`)\n\n"
                    "📊 2. 本月收支明細儀表板\n"
                    "👉 關鍵字：`本月收支` 或 `本月財報`\n\n"
                    "📈 3. 股票行情快查\n"
                    "👉 當天行情：`個股 [代號]` (例：`個股 2330`)\n"
                    "👉 歷史區間：`個股 [代號] [天數]` (例：`個股 2330 5`)\n\n"
                    "⚠️ 4. 一鍵重置帳本\n"
                    "👉 關鍵字：`重置帳本`"
                )

            # ==========================================
            # 功能二：升級版收支報告 (動態計算分類明細，不用再開試算表！)
            # ==========================================
            elif user_text in ["本月財報", "本月收支"]:
                result = sheets_service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range='A:D').execute()
                values = result.get('values', [])
                
                total_income = 0
                total_expense = 0
                expense_detail = {} # 用來動態統計各項支出的字典
                
                for row in values[1:]: # 跳過標題列
                    if len(row) >= 4:
                        category = row[1]
                        amount = int(row[3])
                        
                        if category == "收入":
                            total_income += amount
                        else:
                            total_expense += amount
                            # 自動把相同的支出分類加總在一起
                            expense_detail[category] = expense_detail.get(category, 0) + amount
                            
                net_balance = total_income - total_expense
                
                # 組合出漂亮的收支儀表板
                reply_text = (
                    "📊 【本月收支理財儀表板】\n"
                    "(*¯︶¯*) 主人，這是您目前的資產狀態：\n"
                    "────────────────\n"
                    f"💰 總收入： ${total_income:,}\n"
                    f"💸 總支出： ${total_expense:,}\n"
                    "────────────────\n"
                    "💡 【各項支出明細】\n"
                )
                
                # 如果有支出明細，就逐行印出來；沒有的話就顯示無支出
                if expense_detail:
                    for cat, amt in expense_detail.items():
                        reply_text += f" ▫️ {cat}： ${amt:,}\n"
                else:
                    reply_text += "  暫無任何支出紀錄報告。\n"
                    
                reply_text += (
                    "────────────────\n"
                    f"✨ 本月結餘： ${net_balance:,}\n\n"
                    "運算完畢！主人今天也辛苦了～"
                )

            # ==========================================
            # 功能三：一鍵重置帳本
            # ==========================================
            elif user_text == "重置帳本":
                sheets_service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range='A:D').execute()
                sheets_service.spreadsheets().values().append(
                    spreadsheetId=spreadsheet_id, range="A1",
                    valueInputOption="USER_ENTERED",
                    body={'values': [["日期", "分類", "項目", "金額"]]}
                ).execute()
                reply_text = "✨ 系統重置完畢！帳本已更新為全新乾淨狀態。"

            # ==========================================
            # 功能四：股票查詢系統
            # ==========================================
            elif user_text.startswith("個股"):
                stock_args = user_text.replace("個股", "").strip().split()
                if len(stock_args) >= 1:
                    ticker_input = stock_args[0].upper()
                    days = 2 if len(stock_args) == 1 else int(stock_args[1])
                    tickers_to_try = [f"{ticker_input}.TW", f"{ticker_input}.TWO"] if ticker_input.isdigit() else [ticker_input]
                    
                    hist = None
                    for t in tickers_to_try:
                        stock = yf.Ticker(t)
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
                        elif len(stock_args) == 2:
                            reply_text = f"📊 【{ticker_input} 過去 {days} 天歷史資訊】\n────────────────\n"
                            for date, row in reversed(list(hist.iterrows())):
                                date_str = date.strftime('%m/%d')
                                reply_text += f"📅 {date_str} | 收盤: ${row['Close']:.2f} | 總量: {int(row['Volume'])/1000:,.0f}K 股\n"
                            reply_text += "────────────────\n💡 註：成交量 K 代表千股。"
                    else:
                        reply_text = f"❌ 找不到股票代號【{ticker_input}】。"
                else:
                    reply_text = "❌ 股票查詢格式錯誤！"

            # ==========================================
            # 功能五：極速記帳與自動分類
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
                reply_text = "主人，我看不懂這個指令 w\n💡 請輸入『功能』查看完整指令清單！"
                
        except Exception as e:
            reply_text = f"⚠️ 系統發生錯誤：{e}"

    # 4. 統一回傳訊息給 LINE 聊天室
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(ReplyMessageRequest(
            reply_token=event.reply_token,
            messages=[TextMessage(text=reply_text)]
        ))
if __name__ == "__main__":
    app.run(host='0.0.0.0', port=10000)
