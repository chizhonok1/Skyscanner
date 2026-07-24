import os
import json
import argparse
from datetime import datetime, timezone
import requests
from google import genai
from google.genai import types

# Конфигурация
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"Ошибка инициализации Gemini: {e}")

PRICE_HISTORY_FILE = "price_history.json"

def load_price_history():
    if os.path.exists(PRICE_HISTORY_FILE):
        try:
            with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_price_history(history):
    try:
        with open(PRICE_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения истории: {e}")

def analyze_with_gemini_search(route_info, price_history):
    if not client:
        return f"⚠️ Ошибка: Нет API ключа Gemini.", None

    prompt = f"""
    Ты — ИИ-помощник по поиску билетов. Твоя задача — найти актуальную цену на авиабилет в интернете и написать пост для Telegram.
    
    МАРШРУТ: Из аэропорта {route_info['origin']} в {route_info['destination']}
    ДАТА ВЫЛЕТА: {route_info['date']}
    
    ИСТОРИЯ ПРОШЛЫХ ЦЕН (в USD) для аналитики:
    {json.dumps(price_history, ensure_ascii=False)}
    
    ЗАДАЧА:
    1. Сделай поиск в Google, чтобы найти актуальную минимальную цену на рейсы (Ryanair, Wizz Air и др.) для этого маршрута и даты.
    2. Сформируй красивое сообщение для Telegram с использованием эмодзи.
    3. Обязательно укажи найденную цену в USD. Если точной цены нет, укажи примерную стоимость из поиска.
    4. Опираясь на историю, напиши короткий совет (цена упала, выросла или осталась прежней).
    """
    
    try:
        # Вызов Gemini с включенным инструментом поиска Google
        response = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                tools=[{"google_search": {}}], # Включаем доступ в интернет!
                temperature=0.2
            )
        )
        
        # Вспомогательный запрос, чтобы вытащить только цифру для сохранения в историю
        price_prompt = f"Найди в этом тексте минимальную стоимость билета в USD и напиши ТОЛЬКО число (например: 64). Если цены нет, напиши 0.\nТекст: {response.text}"
        price_res = client.models.generate_content(
            model='gemini-1.5-flash',
            contents=price_prompt
        )
        
        raw_price = price_res.text.strip().replace('$', '').replace(' ', '')
        extracted_price = float(raw_price) if raw_price.replace('.', '', 1).isdigit() else 0
        
        return response.text, extracted_price
    except Exception as e:
        print(f"Ошибка Gemini Search: {e}")
        return f"⚠️ Не удалось проанализировать рейс {route_info['origin']} ➔ {route_info['destination']} на {route_info['date']}", None

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code != 200:
            payload["parse_mode"] = "" # Пробуем без Markdown, если сломалось форматирование
            requests.post(url, json=payload, timeout=10)
        return True
    except Exception as e:
        print(f"Ошибка Telegram API: {e}")
        return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--origin", default="ALC")
    parser.add_argument("--destination", default="WAW")
    parser.add_argument("--dates", nargs="+", default=["2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"])
    args = parser.parse_args()

    history = load_price_history()
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    for date in args.dates:
        print(f"\n--- Поиск билетов на: {date} через Google Search ---")
        key = f"{args.origin}_{args.destination}_{date}"
        if key not in history:
            history[key] = []

        route_info = {"origin": args.origin, "destination": args.destination, "date": date}
        
        # Получаем красивый текст и сырую цену
        alert_msg, current_price = analyze_with_gemini_search(route_info, history[key])
        
        # Сохраняем в историю, только если нейросеть нашла реальную цифру больше нуля
        if current_price and current_price > 0:
            history[key].append({
                "timestamp": now_str,
                "price_usd": current_price,
                "source": "Gemini Search"
            })
        
        print(f"Отправка:\n{alert_msg}")
        send_telegram_message(alert_msg)

    save_price_history(history)

if __name__ == "__main__":
    main()
