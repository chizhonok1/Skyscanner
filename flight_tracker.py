import os
import json
import argparse
import re
from datetime import datetime, timezone
import requests

# Конфигурация
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
DEBUG_GOOGLE_FLIGHTS = os.environ.get("DEBUG_GOOGLE_FLIGHTS", "").lower() == "true"

PRICE_HISTORY_FILE = "price_history.json"

def get_float_env(name, default):
    raw_value = os.environ.get(name)
    if not raw_value:
        return default
    try:
        return float(raw_value)
    except ValueError:
        print(f"Некорректное значение {name}: {raw_value}. Использую {default}.")
        return default

MAX_PRICE_USD = get_float_env("MAX_PRICE_USD", 80)

def compare_with_history(current_price, price_history):
    previous_prices = [
        item.get("price_usd")
        for item in price_history
        if isinstance(item.get("price_usd"), (int, float))
        and item.get("source") == "Google Flights (fast-flights)"
        and item.get("price_usd") <= MAX_PRICE_USD
    ]
    if not previous_prices:
        return "Пока это первая цена в истории наблюдений."

    previous_price = previous_prices[-1]
    if current_price < previous_price:
        return f"Цена ниже прошлой (${previous_price:.0f} USD), можно присмотреться к покупке."
    if current_price > previous_price:
        return f"Цена выше прошлой (${previous_price:.0f} USD), лучше понаблюдать."
    return "Цена не изменилась с прошлого замера."

def parse_google_flights_price(price):
    if isinstance(price, (int, float)):
        return float(price)
    if not price:
        return None

    cleaned = str(price).replace(",", "")
    match = re.search(r"(\d+(?:\.\d{1,2})?)", cleaned)
    if not match:
        return None

    value = float(match.group(1))
    return value if 10 <= value <= 5000 else None

def format_google_datetime(value):
    date_part = getattr(value, "date", None)
    time_part = getattr(value, "time", None)
    if not date_part or not time_part:
        return "N/A"

    try:
        year, month, day = date_part
        hour, minute = time_part
        return f"{year:04d}-{month:02d}-{day:02d} {hour:02d}:{minute:02d}"
    except Exception:
        return str(value)

def summarize_google_offer(flight):
    legs = getattr(flight, "flights", None) or []
    first_leg = legs[0] if legs else None
    departure = format_google_datetime(getattr(first_leg, "departure", None)) if first_leg else "N/A"

    return {
        "price": getattr(flight, "price", None),
        "airlines": getattr(flight, "airlines", None),
        "type": getattr(flight, "type", None),
        "departure": departure,
        "legs": len(legs),
    }

def fetch_google_flights(origin, destination, date):
    try:
        from fast_flights import FlightQuery, Passengers, create_query, get_flights
    except Exception as e:
        print(f"Ошибка импорта fast-flights: {e}")
        return None

    try:
        query = create_query(
            flights=[
                FlightQuery(
                    date=date,
                    from_airport=origin,
                    to_airport=destination,
                )
            ],
            trip="one-way",
            seat="economy",
            passengers=Passengers(adults=1),
            language="en-US",
            currency="USD",
        )
        result = get_flights(query)
    except Exception as e:
        print(f"Ошибка Google Flights fast-flights: {e}")
        return None

    flights = getattr(result, "flights", result)
    if DEBUG_GOOGLE_FLIGHTS:
        print("Google Flights raw offers:")
        for index, flight in enumerate(list(flights or [])[:8], start=1):
            print(f"{index}. {json.dumps(summarize_google_offer(flight), ensure_ascii=False)}")

    offers = []
    for flight in flights or []:
        price = parse_google_flights_price(getattr(flight, "price", None))
        if not price:
            continue
        offers.append((price, flight))

    if not offers:
        return None

    price, cheapest = min(offers, key=lambda item: item[0])
    legs = getattr(cheapest, "flights", None) or []
    first_leg = legs[0] if legs else None
    airlines = getattr(cheapest, "airlines", None) or []
    airline_text = ", ".join(airlines) if airlines else "N/A"
    departure = format_google_datetime(getattr(first_leg, "departure", None)) if first_leg else "N/A"

    return {
        "airline": airline_text,
        "price_usd": price,
        "flight_number": getattr(cheapest, "flight_number", "N/A"),
        "departure_at": departure,
        "source": "Google Flights (fast-flights)",
    }

def format_flight_message(route_info, flight_data, price_history):
    price = flight_data["price_usd"]
    comparison = compare_with_history(price, price_history)
    airline = flight_data.get("airline", "N/A")
    departure = flight_data.get("departure_at", "N/A")
    source = flight_data.get("source", "данные API")

    return (
        f"✈️ {route_info['origin']} ➔ {route_info['destination']} на {route_info['date']}\n"
        f"💵 Минимальная цена: ${price:.0f} USD\n"
        f"🛫 Авиакомпания: {airline}\n"
        f"⏰ Вылет: {departure}\n"
        f"📊 {comparison}\n"
        f"Источник: {source}"
    )

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

def find_flight_price(route_info, price_history, max_price_usd):
    google_flight = fetch_google_flights(
        route_info["origin"],
        route_info["destination"],
        route_info["date"],
    )
    if google_flight:
        price = google_flight["price_usd"]
        if price > max_price_usd:
            return (
                f"Цена Google Flights для {route_info['origin']} ➔ {route_info['destination']} "
                f"на {route_info['date']}: ${price:.0f} USD. Это выше порога ${max_price_usd:.0f}, "
                "поэтому уведомление в Telegram не отправлено.",
                None,
                google_flight["source"],
                False,
            )
        return (
            format_flight_message(route_info, google_flight, price_history),
            price,
            google_flight["source"],
            True,
        )

    return (
        f"Google Flights не вернул цену для {route_info['origin']} ➔ {route_info['destination']} "
        f"на {route_info['date']}. Уведомление в Telegram не отправлено.",
        None,
        "Google Flights (fast-flights)",
        False,
    )

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
        print(f"\n--- Поиск билетов на: {date} ---")
        key = f"{args.origin}_{args.destination}_{date}"
        if key not in history:
            history[key] = []

        route_info = {"origin": args.origin, "destination": args.destination, "date": date}
        
        # Получаем сообщение, цену и решение, нужно ли отправлять уведомление.
        alert_msg, current_price, source, should_notify = find_flight_price(
            route_info,
            history[key],
            MAX_PRICE_USD,
        )
        
        # Сохраняем в историю, только если один из источников нашёл реальную цену.
        if current_price and current_price > 0:
            history[key].append({
                "timestamp": now_str,
                "price_usd": current_price,
                "source": source or "Unknown"
            })
        
        if should_notify:
            print(f"Отправка:\n{alert_msg}")
            send_telegram_message(alert_msg)
        else:
            print(alert_msg)

    save_price_history(history)

if __name__ == "__main__":
    main()
