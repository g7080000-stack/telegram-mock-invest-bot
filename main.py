import nest_asyncio
nest_asyncio.apply()

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import FinanceDataReader as fdr
import pandas as pd
import yfinance as yf
import json
import os
import re
import random
import time
import urllib.parse
from datetime import datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

DB_FILE = "stock_db.json"
EXCHANGE_RATE = 1500
INITIAL_CASH = 10_000_000

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "8844020527:AAEfFnjRKCNr4javTv8tQllnQrt0Y1TiSBo")
ADMIN_ID = 8727188480 # 방장 전용 명령어 텔레그램 ID

KST = ZoneInfo("Asia/Seoul")

KOREAN_MARKET_HOLIDAYS = {
    "2026-01-01", "2026-02-16", "2026-02-17", "2026-02-18", "2026-03-02", 
    "2026-05-01", "2026-05-05", "2026-05-25", "2026-08-17", "2026-09-24", 
    "2026-09-25", "2026-09-28", "2026-10-09", "2026-12-25", "2026-12-31",
}

US_MARKET_HOLIDAYS = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-04-03", "2026-05-25", 
    "2026-06-19", "2026-07-03", "2026-09-07", "2026-11-26", "2026-12-25",
}

db = {}

# =========================
# DB 관리
# =========================
def save_db():
    with open(DB_FILE, "w", encoding="utf-8") as f:
        temp_db = {str(k): v for k, v in db.items()}
        json.dump(temp_db, f, ensure_ascii=False, indent=4)

def load_db():
    global db
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r", encoding="utf-8") as f:
            temp_db = json.load(f)
            db = {}
            for k, v in temp_db.items():
                if k in ["GAME_STATE", "REG_STATE", "TEAM_SUGGESTIONS"]:
                    db[k] = v
                else:
                    db[int(k)] = v
    else:
        db = {}

    if "GAME_STATE" not in db: db["GAME_STATE"] = False
    if "REG_STATE" not in db: db["REG_STATE"] = False
    if "TEAM_SUGGESTIONS" not in db: db["TEAM_SUGGESTIONS"] = {}

load_db()

# =========================
# 장 시간 체크
# =========================
def is_korean_market_open():
    now = datetime.now(KST)
    if now.weekday() >= 5: return False, "주말에는 국내 주식장이 열리지 않습니다."
    if now.strftime("%Y-%m-%d") in KOREAN_MARKET_HOLIDAYS: return False, "오늘은 국내 증시 휴장일입니다."
        
    market_open = dtime(9, 5)
    market_close = dtime(15, 30)
    if not (market_open <= now.time() <= market_close):
        return False, "국내 정규장 시간은 09:05~15:30입니다."
    return True, ""

def is_us_market_open():
    now = datetime.now(KST)
    if now.weekday() in [5, 6]: return False, "미국 주식은 주말 동안 거래가 중단됩니다."
    if now.weekday() == 0 and now.time() < dtime(17, 0): return False, "월요일 주간에는 시세가 제공되지 않습니다."
    if dtime(9, 0) <= now.time() < dtime(17, 0): return False, "🚫 [거래 불가]\n주간 시간대는 API 시세 지연으로 거래를 제한합니다.\n(가능 시간: 17:00 ~ 익일 09:00)"
    
    adjusted_now = now - timedelta(hours=9)
    if adjusted_now.strftime("%Y-%m-%d") in US_MARKET_HOLIDAYS:
        return False, "오늘은 미국 증시 휴장일입니다."
        
    return True, ""

# =========================
# 종목 데이터
# =========================
def normalize_name(name):
    return (str(name).replace(" ", "").replace("-", "").replace("_", "").replace(".", "").upper())

try:
    df_krx = fdr.StockListing("KRX")[["Code", "Name"]]
    df_krx["Code"] = df_krx["Code"].astype(str).str.zfill(6)
    df_krx["CleanName"] = df_krx["Name"].apply(normalize_name)
except Exception:
    df_krx = pd.DataFrame(columns=["Code", "Name", "CleanName"])

SPECIAL_NAMES = {"하이닉스": "000660", "SK하이닉스": "000660", "네이버": "035420", "포스코": "005490"}
US_STOCK_NAMES = {
    "애플": "AAPL", "테슬라": "TSLA", "엔비디아": "NVDA", "마이크로소프트": "MSFT", "마소": "MSFT",
    "구글": "GOOGL", "아마존": "AMZN", "메타": "META", "넷플릭스": "NFLX", "에이엠디": "AMD",
    "AMD": "AMD", "티큐": "TQQQ", "속슬": "SOXL", "에스파이": "SPY", "나스닥": "QQQ",
    "팔란티어": "PLTR", "인텔": "INTC", "코인베이스": "COIN", "마이크론": "MU", "브로드컴": "AVGO"
}

def find_stock_candidates(target, max_count=8):
    target_clean = normalize_name(target)
    if df_krx.empty: return []
    exact = df_krx[df_krx["CleanName"] == target_clean]
    if not exact.empty: return exact[["Code", "Name"]].to_dict("records")
    prefix = df_krx[df_krx["CleanName"].str.startswith(target_clean)]
    if not prefix.empty: return prefix[["Code", "Name"]].head(max_count).to_dict("records")
    partial = df_krx[df_krx["CleanName"].str.contains(target_clean, regex=False)]
    if not partial.empty: return partial[["Code", "Name"]].head(max_count).to_dict("records")
    return []

def get_stock_info(target):
    target = str(target).strip()
    target_clean = normalize_name(target)

    us_map = {normalize_name(k): v for k, v in US_STOCK_NAMES.items()}
    if target_clean in us_map: return us_map[target_clean], target, True, []

    if target_clean.isdigit() and len(target_clean) == 6:
        match = df_krx[df_krx["Code"] == target_clean]
        if not match.empty: return target_clean, match.iloc[0]["Name"], False, []
        return target_clean, target_clean, False, []
    
    special_map = {normalize_name(k): v for k, v in SPECIAL_NAMES.items()}
    if target_clean in special_map:
        code = special_map[target_clean]
        match = df_krx[df_krx["Code"] == code]
        if not match.empty: return code, match.iloc[0]["Name"], False, []
        return code, target, False, []
    
    candidates = find_stock_candidates(target)
    if len(candidates) == 1: return candidates[0]["Code"], candidates[0]["Name"], False, []
    if len(candidates) > 1: return None, None, False, candidates
    
    if re.fullmatch(r"[A-Za-z]{1,6}", target):
        ticker_upper = target.upper()
        try:
            tk_info = yf.Ticker(ticker_upper).info
            company_name = tk_info.get("shortName", ticker_upper)
        except Exception:
            company_name = ticker_upper
        return ticker_upper, company_name, True, []
    
    return None, None, False, []

def get_current_price(code, is_us):
    try:
        if is_us:
            try:
                ex_df = yf.Ticker("USDKRW=X").history(period="1d", interval="1m")
                current_exchange_rate = float(ex_df["Close"].dropna().iloc[-1])
            except Exception:
                current_exchange_rate = 1500

            tk = yf.Ticker(code)
            df_min = tk.history(period="1d", interval="1m", prepost=True)
            if df_min.empty: raise Exception("미국 주식 데이터 불러오기 실패")
            current_price = float(df_min["Close"].dropna().iloc[-1])
            
            df_daily = tk.history(period="5d")
            if len(df_daily) >= 2:
                last_regular = float(df_daily["Close"].dropna().iloc[-1])
                prev_regular = float(df_daily["Close"].dropna().iloc[-2])
            else:
                last_regular = current_price
                prev_regular = current_price

            if abs(current_price - last_regular) > 0.001:
                change_rate = ((current_price - last_regular) / last_regular) * 100
                market_state = "🌙 프리/애프터장"
            else:
                change_rate = ((last_regular - prev_regular) / prev_regular) * 100 if prev_regular > 0 else 0.0
                market_state = "☀️ 정규장"
                
            usd_price = round(current_price, 2)
            krw_price = int(current_price * current_exchange_rate)
            return krw_price, change_rate, usd_price, market_state
            
        else:
            start_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            try:
                df = fdr.DataReader(code, start_date)
                if df.empty: raise Exception("empty dataframe")
            except Exception:
                df = fdr.DataReader(f"KRX:{code}", start_date)
            raw_price = float(df["Close"].iloc[-1])
            if len(df) >= 2:
                prev_price = float(df["Close"].iloc[-2])
                change_rate = ((raw_price - prev_price) / prev_price) * 100
            else:
                change_rate = 0.0
            return int(raw_price), change_rate, None, "☀️ 정규장"
    except Exception as e:
        print(f"가격 조회 실패 ({code}): {e}")
        raise e

def make_candidate_message(candidates):
    msg = "❓ 종목명이 여러 개 검색되었습니다.\n정확한 종목명 또는 종목코드로 다시 입력해주세요.\n\n"
    for c in candidates: msg += f"- {c['Name']} ({c['Code']})\n"
    return msg

# =========================
# 매수 / 매도 로직
# =========================
async def buy_logic(update, context, ticker_input, amount):
    user_id = update.message.from_user.id
    try:
        code, name, is_us, candidates = get_stock_info(ticker_input)
        if candidates:
            await update.message.reply_text(make_candidate_message(candidates))
            return
        if code is None:
            await update.message.reply_text("⚠️ 종목명을 찾지 못했습니다. 해외는 티커로 주문해주세요.")
            return
        
        market_open, reason = is_us_market_open() if is_us else is_korean_market_open()
        if not market_open:
            await update.message.reply_text(f"⛔ 거래 불가\n{reason}")
            return
        
        current_price, change_rate, usd_price, _ = get_current_price(code, is_us)
        
        total_eval, _ = evaluate_user(db[user_id])
        current_held_qty = db[user_id]["portfolio"].get(code, {}).get("quantity", 0) if code in db[user_id]["portfolio"] else 0
        current_held_value = current_held_qty * current_price

        # 40% 제한
        if change_rate >= 40.0:
            max_allowed_value = total_eval * 0.4
            remaining_allowed_value = max_allowed_value - current_held_value
            max_limit_shares = int(min(remaining_allowed_value // current_price, db[user_id]["cash"] // current_price)) if remaining_allowed_value > 0 else 0
        else:
            max_limit_shares = int(db[user_id]["cash"] // current_price)

        if amount == "ALL":
            amount = max_limit_shares
            if amount <= 0:
                await update.message.reply_text("❌ 잔액 부족 또는 보유 한도 초과로 매수할 수 없습니다.")
                return
        elif isinstance(amount, str) and amount.endswith("%"):
            pct = int(amount.replace("%", ""))
            if pct <= 0 or pct > 100:
                await update.message.reply_text("⚠️ 비율은 1~100% 사이로 입력해주세요.")
                return
            target_cash = db[user_id]["cash"] * (pct / 100.0)
            amount = int(target_cash // current_price)
            if amount <= 0:
                await update.message.reply_text("❌ 해당 비율의 현금으로는 1주도 살 수 없습니다.")
                return
            if change_rate >= 40.0 and amount > max_limit_shares:
                await update.message.reply_text(f"🚫 급등주 비중 제한 초과. 최대 {max_limit_shares}주까지만 매수 가능합니다.")
                return
        else:
            amount = int(amount)
            if change_rate >= 40.0 and amount > max_limit_shares:
                await update.message.reply_text(f"🚫 급등주 비중 제한 초과. 최대 {max_limit_shares}주까지만 매수 가능합니다.")
                return

        total = current_price * amount
        if db[user_id]["cash"] < total:
            await update.message.reply_text(f"❌ 잔액 부족.\n필요 금액: {total:,}원\n보유 현금: {db[user_id]['cash']:,}원")
            return
            
        db[user_id]["cash"] -= total
        
        if code not in db[user_id]["portfolio"]:
            db[user_id]["portfolio"][code] = {"name": name, "quantity": 0, "is_us": is_us, "avg_price": 0, "avg_price_usd": 0.0, "last_buy_time": 0}
            
        holding = db[user_id]["portfolio"][code]
        old_qty = holding["quantity"]
        new_qty = old_qty + amount
        
        old_total_value = holding.get("avg_price", 0) * old_qty
        holding["avg_price"] = int((old_total_value + total) / new_qty)
        
        if is_us and usd_price is not None:
            old_total_usd = holding.get("avg_price_usd", 0.0) * old_qty
            total_usd = usd_price * amount
            holding["avg_price_usd"] = (old_total_usd + total_usd) / new_qty
            
        holding["quantity"] = new_qty
        holding["name"] = name
        holding["is_us"] = is_us
        holding["last_buy_time"] = time.time()
        save_db()
        
        price_str = f"🇺🇸 ${usd_price:,.2f} (🇰🇷 {current_price:,}원)" if (is_us and usd_price is not None) else f"{current_price:,}원"

        await update.message.reply_text(
            f"✅ {name} {amount}주 매수 완료!\n"
            f"현재가: {price_str}\n"
            f"매수금액: {total:,}원\n"
            f"남은 현금: {db[user_id]['cash']:,}원\n"
            f"나의 평단가: {holding['avg_price']:,}원\n"
            f"⏱️ 시세 지연 악용 방지를 위해 10분간 매도 금지"
        )
    except Exception:
        await update.message.reply_text("⚠️ 매수 실패. 종목명과 수량을 확인해주세요.")

async def sell_logic(update, context, ticker_input, amount):
    user_id = update.message.from_user.id
    try:
        code, name, is_us, candidates = get_stock_info(ticker_input)
        if candidates:
            await update.message.reply_text(make_candidate_message(candidates))
            return
        if code is None:
            await update.message.reply_text("⚠️ 종목명을 찾지 못했습니다.")
            return
            
        market_open, reason = is_us_market_open() if is_us else is_korean_market_open()
        if not market_open:
            await update.message.reply_text(f"⛔ 거래 불가\n{reason}")
            return
            
        holding = db[user_id]["portfolio"].get(code)
        if not holding:
            await update.message.reply_text(f"❌ {name} 주식을 보유하고 있지 않습니다.")
            return
        
        elapsed_time = time.time() - holding.get("last_buy_time", 0)
        if elapsed_time < 600:
            remain_sec = int(600 - elapsed_time)
            await update.message.reply_text(f"🚫 [단타 금지]\n매수 후 10분 동안 팔 수 없습니다.\n남은 시간: {remain_sec // 60}분 {remain_sec % 60}초")
            return

        if amount == "ALL": 
            amount = holding["quantity"]
        elif isinstance(amount, str) and amount.endswith("%"):
            pct = int(amount.replace("%", ""))
            if pct <= 0 or pct > 100:
                await update.message.reply_text("⚠️ 비율은 1~100% 사이로 입력해주세요.")
                return
            amount = int(holding["quantity"] * (pct / 100.0))
            if amount <= 0:
                await update.message.reply_text("❌ 해당 비율의 수량은 1주가 안 됩니다.")
                return
        else:
            amount = int(amount)

        if holding["quantity"] < amount:
            await update.message.reply_text(f"❌ 보유 수량이 부족합니다. (현재 {holding['quantity']}주 보유)")
            return
        
        current_price, _, usd_price, _ = get_current_price(code, is_us)
        total = current_price * amount
        avg_price = holding.get("avg_price", current_price)
        buy_total = avg_price * amount
        profit_amount = total - buy_total
        profit_rate = (profit_amount / buy_total) * 100 if buy_total > 0 else 0
        
        db[user_id]["cash"] += total
        db[user_id]["portfolio"][code]["quantity"] -= amount
        if db[user_id]["portfolio"][code]["quantity"] == 0:
            del db[user_id]["portfolio"][code]
        save_db()
        
        sign = "+" if profit_rate > 0 else ""
        price_str = f"🇺🇸 ${usd_price:,.2f} (🇰🇷 {current_price:,}원)" if (is_us and usd_price is not None) else f"{current_price:,}원"

        await update.message.reply_text(
            f"💰 {name} {amount}주 매도 완료!\n"
            f"현재가: {price_str} / 평단가: {avg_price:,}원\n"
            f"수익률: {sign}{profit_rate:.2f}% ({sign}{int(profit_amount):,}원)\n"
            f"보유 현금: {db[user_id]['cash']:,}원"
        )
    except Exception:
        await update.message.reply_text("⚠️ 매도 실패. 종목명을 확인해주세요.")

# =========================
# 평가 및 시각화
# =========================
def evaluate_user(data):
    total_eval = data.get("cash", 0)
    seed = data.get("seed", INITIAL_CASH)
    if "portfolio" in data:
        for code, info in data["portfolio"].items():
            try:
                cur_price, _, _, _ = get_current_price(code, info.get("is_us", False))
                total_eval += cur_price * info.get("quantity", 0)
            except Exception:
                continue
    profit_rate = ((total_eval - seed) / seed) * 100 if seed > 0 else 0
    return total_eval, profit_rate

def get_roast_message(name, rate):
    roasts = [
        f" {name}님, 시장에 기부하는중?",
        f" {name}님, 투자가 아니라 투기를 하는 중입니다.",
        f" {name}님, 교육비를 제대로 납부 중입니다.",
        f" {name}님, 계좌가 겨울입니다. 매우 춥습니다.",
        f" {name}님, 손실 체험학습 중입니다.",
    ]
    if rate >= 0: return f"😅 꼴찌인데도 수익이 났네요. 분발하세요."
    return random.choice(roasts)

def build_portfolio_data(data):
    total_eval, rate = evaluate_user(data)
    seed = data.get('seed', INITIAL_CASH)
    profit = total_eval - seed
    sign = "+" if profit > 0 else ""
    cash = data['cash']
    
    msg = (f"📊 [{data['name']}]님의 계좌\n━━━━━━━━━━━━━━\n"
           f"💵 현금: {cash:,}원\n"
           f"💰 평가자산: {int(total_eval):,}원\n"
           f"📈 수익률: {sign}{rate:.2f}% ({sign}{int(profit):,}원)\n\n")

    items, labels, values = [], [], []
    if data.get("portfolio"):
        for code, info in data["portfolio"].items():
            is_us = info.get("is_us", False)
            try:
                cur_price, _, _, _ = get_current_price(code, is_us)
                val = cur_price * info['quantity']
                items.append({
                    "name": info['name'], "qty": info['quantity'], 
                    "avg_price": info.get('avg_price', 0), "cur_price": cur_price,
                    "val": val, "error": False
                })
            except Exception:
                items.append({"name": info['name'], "qty": info['quantity'], "val": 0, "error": True})
                
    items = sorted(items, key=lambda x: x['val'], reverse=True)
    
    for item in items:
        if not item["error"] and item['val'] > 0:
            labels.append(item['name'])
            values.append(item['val'])
    if cash > 0:
        labels.append("현금")
        values.append(cash)

    chart_url = None
    if values and total_eval > 0:
        chart_config = {
            "type": "doughnut",
            "data": {"labels": labels, "datasets": [{"data": values}]},
            "options": {"plugins": {"legend": {"position": "right", "labels": {"font": {"size": 16}}}}}
        }
        encoded_config = urllib.parse.quote(json.dumps(chart_config))
        chart_url = f"https://quickchart.io/chart?w=600&h=300&c={encoded_config}"

    msg += "[보유 종목 상세]\n"
    if not items:
        msg += "보유 주식 없음\n"
    else:
        for item in items:
            if item["error"]:
                msg += f"- {item['name']}: {item['qty']}주 (로딩 실패)\n"
                continue
            if item['avg_price'] > 0:
                item_rate = ((item['cur_price'] - item['avg_price']) / item['avg_price']) * 100
                i_sign = "+" if item_rate > 0 else ""
                msg += f"- {item['name']}: {item['qty']}주 ({i_sign}{item_rate:.2f}%)\n"
            else:
                msg += f"- {item['name']}: {item['qty']}주\n"
    return msg, chart_url

# =========================
# 기본 명령어
# =========================
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "📖 [모의투자 봇 도움말]\n"
        "━━━━━━━━━━━━━━\n"
        "🛒 주식 매매\n"
        "- /[종목] [수량]주 매수 (예: /삼성전자 10주 매수)\n"
        "- /[종목] [비율]% 매수 (예: /테슬라 50% 매수)\n"
        "- /[종목] 풀매수 / 풀매도\n\n"
        "🔍 조회\n"
        "- /[종목] : 현재가 조회\n"
        "- /내계좌 : 포트폴리오 차트 조회\n"
        "- /[상대닉네임] 계좌 : 상대방 계좌 확인\n"
        "- /순위 : 팀전/개인 랭킹\n"
        "- /팀명 [제안할이름] : 팀명 후보 등록"
    )
    await update.message.reply_text(msg)

async def rank(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db or (len(db) <= 2 and "GAME_STATE" in db):
        await update.message.reply_text("참가자가 부족합니다.")
        return
    await update.message.reply_text("⏳ 수익률 집계 중...")
    
    team_stats, indiv_list = {}, []
    
    for uid, data in db.items():
        if type(uid) != int: continue
        total_eval, rate = evaluate_user(data)
        t_name = data.get("team", "무소속")
        
        if t_name not in team_stats:
            team_stats[t_name] = {"total_eval": 0, "seed": 0}
            
        team_stats[t_name]["total_eval"] += total_eval
        team_stats[t_name]["seed"] += data.get("seed", INITIAL_CASH)
        indiv_list.append({"team": t_name, "name": data["name"], "rate": rate})

    msg = "🏆 [팀 랭킹]\n━━━━━━━━━━━━━━\n"
    team_rank = []
    for t_name, stats in team_stats.items():
        if stats["seed"] > 0:
            team_rate = ((stats["total_eval"] - stats["seed"]) / stats["seed"]) * 100
            team_rank.append({"team": t_name, "rate": team_rate})
    
    team_rank = sorted(team_rank, key=lambda x: x["rate"], reverse=True)
    for i, t in enumerate(team_rank, 1):
        msg += f"{i}위 [{t['team']}] : {t['rate']:+.2f}%\n"
        
    msg += "\n📊 [개인 순위]\n━━━━━━━━━━━━━━\n"
    sorted_indiv = sorted(indiv_list, key=lambda x: x["rate"], reverse=True)
    for i, r in enumerate(sorted_indiv, 1):
        msg += f"{i}위 {r['name']} ({r['team']}): {r['rate']:+.2f}%\n"
        
    await update.message.reply_text(msg)

# =========================
# 메인 핸들러
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    raw_text = update.message.text.strip()
    user_id = update.message.from_user.id
    if not raw_text.startswith("/"): return
    text = raw_text[1:].strip()

    # 방장 명령어
    if text in ["방장 도움말", "방장도움말", "admin"]:
        if user_id != ADMIN_ID: return
        admin_msg = (
            "👑 [방장 전용]\n"
            "- /대회시작 : 새로운 대회 접수 오픈 (데이터 초기화)\n"
            "- /팀배정 [닉네임] [팀명] : 팀 수동 분배\n"
            "- /시작 : 매매 활성화 및 팀명 랜덤 확정\n"
            "- /종료1028 : 대회 종료\n"
            "- /잔고수정 [닉네임] [금액]\n"
            "- /평단수정 [닉네임] [종목] [정상가격] : 평단 변경 및 잔고 동기화\n"
        )
        await update.message.reply_text(admin_msg)
        return

    if text.startswith("팀배정"):
        if user_id != ADMIN_ID: return
        parts = text.split()
        if len(parts) != 3: return
        target_uid = next((uid for uid, data in db.items() if type(uid) == int and data["name"] == parts[1]), None)
        if not target_uid: return
        db[target_uid]["team"] = parts[2]
        save_db()
        await update.message.reply_text(f"✅ [{parts[1]}]님을 [{parts[2]}]에 배정했습니다.")
        return

    if text.startswith("잔고수정") and user_id == ADMIN_ID:
        parts = text.split()
        if len(parts) != 3: return
        target_uid = next((uid for uid, data in db.items() if type(uid) == int and data["name"] == parts[1]), None)
        if target_uid:
            db[target_uid]["cash"] = int(parts[2])
            save_db()
            await update.message.reply_text(f"✅ [{parts[1]}]님의 잔고가 수정되었습니다.")
        return

    if text.startswith("평단수정") and user_id == ADMIN_ID:
        parts = text.split()
        if len(parts) != 4: return
        target_uid = next((uid for uid, data in db.items() if type(uid) == int and data["name"] == parts[1]), None)
        if not target_uid: return
        code, name, is_us, _ = get_stock_info(parts[2])
        if code in db[target_uid]["portfolio"]:
            holding = db[target_uid]["portfolio"][code]
            old_avg = holding.get("avg_price", 0)
            new_avg = int(parts[3])
            db[target_uid]["cash"] -= (new_avg - old_avg) * holding.get("quantity", 0)
            holding["avg_price"] = new_avg
            if is_us: holding["avg_price_usd"] = new_avg / 1500
            save_db()
            await update.message.reply_text(f"✅ [{parts[1]}]님의 [{name}] 정보가 수정되었습니다.")
        return

    if text == "대회시작":
        if user_id != ADMIN_ID: return
        db.clear()
        db.update({"GAME_STATE": False, "REG_STATE": True, "TEAM_SUGGESTIONS": {}})
        save_db()
        await update.message.reply_text("📢 대회 참가 접수를 시작합니다.\n'/입장 [닉네임]' 을 입력해주세요.")
        return

    if text.startswith("입장"):
        if not db.get("REG_STATE", False): return
        parts = text.split()
        if len(parts) != 2: return
        if user_id in db: return
        db[user_id] = {"name": parts[1], "team": "무소속", "cash": INITIAL_CASH, "portfolio": {}, "seed": INITIAL_CASH, "last_trade_time": time.time()}
        save_db()
        await update.message.reply_text(f"🎉 [{parts[1]}]님 접수 완료. 방장의 팀 배정을 대기해주세요.")
        return

    # ✨ 팀명 후보 등록
    if text.startswith("팀명 "):
        if user_id not in db: return
        new_name = text[3:].strip()
        t = db[user_id].get("team", "무소속")
        if t == "무소속":
            await update.message.reply_text("⚠️ 소속된 팀이 없습니다.")
            return
        if "TEAM_SUGGESTIONS" not in db: db["TEAM_SUGGESTIONS"] = {}
        if t not in db["TEAM_SUGGESTIONS"]: db["TEAM_SUGGESTIONS"][t] = []
        db["TEAM_SUGGESTIONS"][t].append(new_name)
        save_db()
        await update.message.reply_text(f"💡 [{new_name}] 이름이 소속팀 후보로 등록되었습니다.")
        return

    if text == "시작" and user_id == ADMIN_ID:
        db["GAME_STATE"] = True
        
        # ✨ 팀명 랜덤 추첨 적용
        team_map = {}
        if "TEAM_SUGGESTIONS" in db:
            for t, names in db["TEAM_SUGGESTIONS"].items():
                if names: team_map[t] = random.choice(names)
                
        for uid, data in db.items():
            if type(uid) == int:
                old_t = data.get("team")
                if old_t in team_map: data["team"] = team_map[old_t]

        save_db()
        
        msg = "🎉 팀 배틀 모의투자 대회를 개막합니다!\n\n[확정된 팀 이름]\n"
        for old_t, new_t in team_map.items():
            msg += f"👉 기존 {old_t} ➡️ {new_t}\n"
            
        msg += "\n지금부터 주식 거래가 가능합니다."
        await update.message.reply_text(msg)
        return

    if text == "종료1028" and user_id == ADMIN_ID:
        db.update({"GAME_STATE": False, "REG_STATE": False})
        save_db()
        await update.message.reply_text("🏁 대회가 종료되었습니다. /순위 명령어로 결과를 확인하세요.")
        return

    if text in ["도움말", "help"]:
        await help_command(update, context)
        return
        
    if text in ["내계좌", "my"]:
        if user_id not in db: return
        msg, chart_url = build_portfolio_data(db[user_id])
        if chart_url: await update.message.reply_photo(photo=chart_url, caption=msg)
        else: await update.message.reply_text(msg)
        return

    match_peek = re.search(r"^([가-힣A-Za-z0-9\s\.\-_]+?)\s*계좌$", text)
    if match_peek:
        target_data = next((data for uid, data in db.items() if type(uid) == int and data["name"] == match_peek.group(1).strip()), None)
        if not target_data: return
        msg, chart_url = build_portfolio_data(target_data)
        if chart_url: await update.message.reply_photo(photo=chart_url, caption=f"👀 상대 계좌 확인\n\n{msg}")
        else: await update.message.reply_text(f"👀 상대 계좌 확인\n\n{msg}")
        return

    if text in ["순위", "rank"]:
        await rank(update, context)
        return

    if user_id not in db: return

    # 주문 처리
    match_qty = re.search(r"([가-힣A-Za-z0-9\s\.\-_]+?)\s+(\d+)\s*주?\s*(매수|사줘|매도|팔아)", text)
    match_pct = re.search(r"([가-힣A-Za-z0-9\s\.\-_]+?)\s+(\d+)\s*%\s*(매수|사줘|매도|팔아)", text)
    match_all = re.search(r"([가-힣A-Za-z0-9\s\.\-_]+?)\s*(풀매수|풀매도)", text)

    if match_qty or match_pct or match_all:
        if not db.get("GAME_STATE", False):
            await update.message.reply_text("⏳ 아직 대회가 시작되지 않았습니다.")
            return

    if match_qty:
        await buy_logic(update, context, match_qty.group(1).strip(), int(match_qty.group(2))) if match_qty.group(3) in ["매수", "사줘"] else await sell_logic(update, context, match_qty.group(1).strip(), int(match_qty.group(2)))
        return
    elif match_pct:
        action = match_pct.group(3)
        amt_str = f"{match_pct.group(2)}%"
        await buy_logic(update, context, match_pct.group(1).strip(), amt_str) if action in ["매수", "사줘"] else await sell_logic(update, context, match_pct.group(1).strip(), amt_str)
        return
    elif match_all:
        action = match_all.group(2)
        await buy_logic(update, context, match_all.group(1).strip(), "ALL") if action == "풀매수" else await sell_logic(update, context, match_all.group(1).strip(), "ALL")
        return

    try:
        code, name, is_us, candidates = get_stock_info(text.strip())
        if candidates:
            await update.message.reply_text(make_candidate_message(candidates))
            return
        if code:
            current_price, change_rate, usd_price, market_state = get_current_price(code, is_us)
            emoji = "🔺" if change_rate > 0 else "🔻" if change_rate < 0 else "➖"
            sign = "+" if change_rate > 0 else ""
            
            if is_us and usd_price is not None:
                await update.message.reply_text(f"📈 {name} ({market_state})\n━━━━━━━━━━━━━━\n달러: ${usd_price:,.2f}\n원화: {current_price:,}원\n등락률: {emoji} {sign}{change_rate:.2f}%")
            else:
                await update.message.reply_text(f"📈 {name} ({market_state})\n━━━━━━━━━━━━━━\n현재가: {current_price:,}원\n등락률: {emoji} {sign}{change_rate:.2f}%")
            return
    except Exception:
        pass

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.COMMAND, handle_message))
    print("🚀 서버 가동 완료!")
    app.run_polling()

if __name__ == "__main__":
    main()
