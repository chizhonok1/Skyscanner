import os
import json
import argparse
from datetime import datetime, timezone
import requests
from google import genai

# Конфигурация из переменных окружения
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TRAVELPAYOUTS_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN")

# Новая официальная инициализация Gemini SDK (google-genai)
client = None
if GEMINI_API_KEY:
    client = genai.Client(api_key=GEMINI_API_KEY)

PRICE_HISTORY_FILE = "price_history.json"

def load_price_history():
    if os.path.exists(PRICE_HISTORY_FILE):
        try:
            with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Ошибка загрузки истории цен: {e}")
            return {}
    return {}

def save_price_history(history):
    try:
        with open(PRICE_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Ошибка сохранения истории цен: {e}")

def fetch_flights_travelpayouts(origin, destination, date):
    if not TRAVELPAYOUTS_TOKEN:
        return None
    url = "https://api.travelpayouts.com/v1/prices/cheap"
    params = {
        "origin": origin,
        "destination": destination,
        "depart_date": date,
        "currency": "usd",
        "token": TRAVELPAYOUTS_TOKEN
    }
    try:
        response = requests.get(url, params=params, timeout=15)
        if response.status_code == 200:
            data = response.json()
            if data.get("success") and destination in data.get("data", {}):
                flights_data = data["data"][destination]
                cheapest_key = min(flights_data, key=lambda k: flights_data[k]["price"]) if flights_data else None
                if cheapest_key:
                    flight_info = flights_data[cheapest_key]
                    return {
                        "airline": flight_info.get("airline", "N/A"),
                        "price_usd": flight_info.get("price"),
                        "flight_number": flight_info.get("flight_number", "N/A"),
                        "departure_at": flight_info.get("departure_at"),
                        "source": "Travelpayouts API"
                    }
    except Exception as e:
        print(f"Ошибка получения данных из Travelpayouts: {e}")
    return None

def fetch_flights_fast_flights(origin, destination, date):
    try:
        from fast_flights import FlightData, Passenger, FlightType, ServiceClass, get_flights
        result = get_flights(
            flight_data=[FlightData(date=date, from_airport=origin, to_airport=destination)],
            trip=FlightType.ONE_WAY,
            passengers=Passenger(adults=1),
            service=ServiceClass.ECONOMY,
            currency="USD"
        )
        if result and result.flights:
            sorted_flights = sorted(result.flights, key=lambda x: x.price if x.price else 9999)
            cheapest = sorted_flights[0]
            return {
                "airline": getattr(cheapest, "airline", "Ryanair / WizzAir"),
                "price_usd": float(str(cheapest.price).replace("$", "").replace(",", "").strip()) if cheapest.price else None,
                "flight_number": getattr(cheapest, "name", "N/A"),
                "departure_at": getattr(cheapest, "departure", "N/A"),
                "duration": getattr(cheapest, "duration", "N/A"),
                "source": "Google Flights (fast-flights)"
            }
    except Exception as e:
        print(f"Fast-flights ошибка: {e}")
    return None

def fetch_cheapest_flight(origin, destination, date):
    flight = fetch_flights_travelpayouts(origin, destination, date)
    if not flight:
        flight = fetch_flights_fast_flights(origin, destination, date)
    return flight

def generate_gemini_analysis(route_info, current_flight, price_history):
    # Защита от NoneType
    flight_data_safe = current_flight if isinstance(current_flight, dict) else {}
    price_val = flight_data_safe.get('price_usd', 'Н/Д')

    if not client:
        return f"✈️ Рейс {route_info['origin']} ➔ {route_info['destination']} ({route_info['date']}): ${price_val}"

    prompt = f"""
    Ты — эксперт по авиабилетам. Сформируй короткое и понятное уведомление для Telegram-бота.
    Маршрут: {route_info['origin']} ➔ {route_info['destination']} на {route_info['date']}.
    Текущие данные о рейсе: {json.dumps(flight_data_safe, ensure_ascii=False)}
    История прошлых цен: {json.dumps(price_history, ensure_ascii=False)}

    Требования:
    1. Использовать эмодзи.
    2. Указать цену в $, авиакомпанию, время вылета (если есть).
    3. Сравнить с прошлыми ценами и дать короткую рекомендацию (стоит ли покупать).
    """
    try:
        # Используем официальную актуальную модель gemini-2.5-flash
        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
        )
        return response.text
    except Exception as e:
        print(f"Ошибка Gemini API: {e}")
        return f"✈️ Билет {route_info['origin']} ➔ {route_info['destination']} ({route_info['date']}): ${price_val}"

def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=10)
        if res.status_code != 200:
            payload["parse_mode"] = ""
            res = requests.post(url, json=payload, timeout=10)
        return res.status_code == 200
    except Exception as e:
        print(f"Ошибка отправки сообщения в Telegram: {e}")
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
        flight_data = fetch_cheapest_flight(args.origin, args.destination, date)
        key = f"{args.origin}_{args.destination}_{date}"
        if key not in history:
            history[key] = []

        if isinstance(flight_data, dict) and flight_data.get("price_usd"):
            history[key].append({
                "timestamp": now_str,
                "price_usd": flight_data["price_usd"],
                "airline": flight_data.get("airline")
            })

        route_info = {"origin": args.origin, "destination": args.destination, "date": date}
        alert_msg = generate_gemini_analysis(route_info, flight_data, history[key])
        send_telegram_message(alert_msg)

    save_price_history(history)

if __name__ == "__main__":
    main()
