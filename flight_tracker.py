import os
import json
import argparse
import re
from datetime import datetime, timezone
import requests
from google import genai
from google.genai import types

# Конфигурация
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
TRAVELPAYOUTS_TOKEN = os.environ.get("TRAVELPAYOUTS_TOKEN")
ENABLE_GEMINI_SEARCH = os.environ.get("ENABLE_GEMINI_SEARCH", "").lower() == "true"
DEBUG_GOOGLE_FLIGHTS = os.environ.get("DEBUG_GOOGLE_FLIGHTS", "").lower() == "true"
FALLBACK_GEMINI_MODELS = ["gemini-3.5-flash-lite"]

client = None
if GEMINI_API_KEY:
    try:
        client = genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        print(f"Ошибка инициализации Gemini: {e}")

PRICE_HISTORY_FILE = "price_history.json"

def get_gemini_models():
    models = [model.strip() for model in GEMINI_MODEL.split(",") if model.strip()]
    for model in FALLBACK_GEMINI_MODELS:
        if model not in models:
            models.append(model)
    return models

def short_error(error):
    message = str(error).replace("\n", " ").strip()
    if len(message) > 500:
        message = message[:500] + "..."
    return message or error.__class__.__name__

def extract_price_usd(text):
    if not text:
        return 0

    normalized = text.replace(",", ".")
    patterns = [
        r"(?:USD|\$)\s*(\d+(?:\.\d{1,2})?)",
        r"(\d+(?:\.\d{1,2})?)\s*(?:USD|\$)",
    ]

    prices = []
    for pattern in patterns:
        for match in re.findall(pattern, normalized, flags=re.IGNORECASE):
            try:
                price = float(match)
            except ValueError:
                continue
            if 10 <= price <= 5000:
                prices.append(price)

    return min(prices) if prices else 0

def compare_with_history(current_price, price_history):
    previous_prices = [
        item.get("price_usd")
        for item in price_history
        if isinstance(item.get("price_usd"), (int, float))
    ]
    if not previous_prices:
        return "Пока это первая цена в истории наблюдений."

    previous_price = previous_prices[-1]
    if current_price < previous_price:
        return f"Цена ниже прошлой (${previous_price:.0f} USD), можно присмотреться к покупке."
    if current_price > previous_price:
        return f"Цена выше прошлой (${previous_price:.0f} USD), лучше понаблюдать."
    return "Цена не изменилась с прошлого замера."

def fetch_travelpayouts_flight(origin, destination, date):
    if not TRAVELPAYOUTS_TOKEN:
        return None

    params = {
        "origin": origin,
        "destination": destination,
        "depart_date": date,
        "currency": "usd",
        "token": TRAVELPAYOUTS_TOKEN,
    }

    try:
        response = requests.get(
            "https://api.travelpayouts.com/v1/prices/cheap",
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as e:
        print(f"Ошибка Travelpayouts API: {e}")
        return None

    if not payload.get("success"):
        print(f"Travelpayouts API вернул неуспешный ответ: {payload}")
        return None

    route_data = payload.get("data", {}).get(destination, {})
    offers = []
    for offer in route_data.values():
        price = offer.get("price")
        if isinstance(price, (int, float)) and price > 0:
            offers.append(offer)

    if not offers:
        return None

    cheapest = min(offers, key=lambda offer: offer["price"])
    return {
        "airline": cheapest.get("airline", "N/A"),
        "price_usd": float(cheapest["price"]),
        "flight_number": cheapest.get("flight_number", "N/A"),
        "departure_at": cheapest.get("departure_at", "N/A"),
        "source": "Travelpayouts API",
    }

def fetch_travelpayouts_calendar_flight(origin, destination, date):
    if not TRAVELPAYOUTS_TOKEN:
        return None

    month = date[:7]
    params = {
        "origin": origin,
        "destination": destination,
        "depart_date": month,
        "calendar_type": "departure_date",
        "currency": "usd",
        "token": TRAVELPAYOUTS_TOKEN,
    }

    try:
        response = requests.get(
            "https://api.travelpayouts.com/v1/prices/calendar",
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as e:
        print(f"Ошибка Travelpayouts Calendar API: {e}")
        return None

    if not payload.get("success"):
        print(f"Travelpayouts Calendar API вернул неуспешный ответ: {payload}")
        return None

    offer = payload.get("data", {}).get(date)
    if not offer or not isinstance(offer.get("price"), (int, float)):
        return None

    return {
        "airline": offer.get("airline", "N/A"),
        "price_usd": float(offer["price"]),
        "flight_number": offer.get("flight_number", "N/A"),
        "departure_at": offer.get("departure_at", f"{date}T00:00:00"),
        "source": "Travelpayouts Calendar API",
    }

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

def find_flight_price(route_info, price_history):
    travelpayouts_flight = fetch_travelpayouts_flight(
        route_info["origin"],
        route_info["destination"],
        route_info["date"],
    )
    if not travelpayouts_flight:
        travelpayouts_flight = fetch_travelpayouts_calendar_flight(
            route_info["origin"],
            route_info["destination"],
            route_info["date"],
        )
    if travelpayouts_flight:
        return (
            format_flight_message(route_info, travelpayouts_flight, price_history),
            travelpayouts_flight["price_usd"],
            travelpayouts_flight["source"],
        )

    google_flight = fetch_google_flights(
        route_info["origin"],
        route_info["destination"],
        route_info["date"],
    )
    if google_flight:
        return (
            format_flight_message(route_info, google_flight, price_history),
            google_flight["price_usd"],
            google_flight["source"],
        )

    if not ENABLE_GEMINI_SEARCH:
        travelpayouts_status = (
            "Travelpayouts API не нашёл предложений на эту дату."
            if TRAVELPAYOUTS_TOKEN
            else "TRAVELPAYOUTS_TOKEN не добавлен в GitHub Secrets."
        )
        return (
            f"⚠️ Цена не найдена для {route_info['origin']} ➔ {route_info['destination']} "
            f"на {route_info['date']}\n\n"
            f"{travelpayouts_status}\n"
            "Google Flights через fast-flights тоже не вернул цену.\n"
            "Gemini Search отключён, чтобы не расходовать квоту. Чтобы включить его, добавь "
            "ENABLE_GEMINI_SEARCH=true в GitHub Actions variables/secrets.",
            None,
            None,
        )

    if not client:
        return (
            "⚠️ Не удалось получить данные о рейсе.\n\n"
            "Причина: нет доступного источника данных. Добавь GEMINI_API_KEY или TRAVELPAYOUTS_TOKEN в GitHub Secrets.",
            None,
            None,
        )

    prompt = f"""
    Ты — ИИ-помощник по поиску билетов. Твоя задача — найти актуальную цену на авиабилет в интернете и написать пост для Telegram.
    
    МАРШРУТ: Из аэропорта {route_info['origin']} в {route_info['destination']}
    ДАТА ВЫЛЕТА: {route_info['date']}
    
    ИСТОРИЯ ПРОШЛЫХ ЦЕН (в USD) для аналитики:
    {json.dumps(price_history, ensure_ascii=False)}
    
    ЗАДАЧА:
    1. Сделай поиск в Google, чтобы найти актуальную минимальную цену на рейсы для этого маршрута и даты.
    2. Сформируй короткое сообщение для Telegram с использованием эмодзи.
    3. Обязательно укажи цену в формате "$123 USD". Если точной цены нет, напиши, что точная цена не найдена.
    4. Опираясь на историю, напиши короткий совет (цена упала, выросла или осталась прежней).
    """
    
    gemini_errors = []
    for model in get_gemini_models():
        try:
            # Вызов Gemini с актуальным инструментом Google Search.
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    tools=[types.Tool(google_search=types.GoogleSearch())]
                )
            )

            response_text = (response.text or "").strip()
            if not response_text:
                raise ValueError("Gemini вернул пустой ответ")

            extracted_price = extract_price_usd(response_text)
            if not extracted_price:
                price_prompt = f"Найди в этом тексте минимальную стоимость билета в USD и напиши ТОЛЬКО число. Если цены нет, напиши 0.\nТекст: {response_text}"
                try:
                    price_res = client.models.generate_content(
                        model=model,
                        contents=price_prompt
                    )
                    raw_price = (price_res.text or "").strip().replace("$", "").replace(" ", "").replace(",", ".")
                    extracted_price = float(raw_price) if raw_price.replace(".", "", 1).isdigit() else 0
                except Exception as price_error:
                    print(f"Ошибка извлечения цены Gemini ({model}): {price_error}")

            return response_text, extracted_price, "Gemini Search"
        except Exception as e:
            gemini_errors.append((model, e))
            print(f"Ошибка Gemini Search ({model}): {e}")

    if gemini_errors:
        reason = "; ".join(
            f"{model}: {short_error(error)}"
            for model, error in gemini_errors
        )
    else:
        reason = "неизвестная ошибка"

    travelpayouts_status = (
        "Travelpayouts API не нашёл предложений на эту дату."
        if TRAVELPAYOUTS_TOKEN
        else "TRAVELPAYOUTS_TOKEN не добавлен в GitHub Secrets."
    )

    return (
        f"⚠️ Цена не найдена для {route_info['origin']} ➔ {route_info['destination']} "
        f"на {route_info['date']}\n\n"
        f"{travelpayouts_status}\n"
        "Google Flights через fast-flights тоже не вернул цену.\n"
        f"Gemini Search недоступен: {reason}",
        None,
        None,
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
        
        # Получаем красивый текст и сырую цену
        alert_msg, current_price, source = find_flight_price(route_info, history[key])
        
        # Сохраняем в историю, только если один из источников нашёл реальную цену.
        if current_price and current_price > 0:
            history[key].append({
                "timestamp": now_str,
                "price_usd": current_price,
                "source": source or "Unknown"
            })
        
        print(f"Отправка:\n{alert_msg}")
        send_telegram_message(alert_msg)

    save_price_history(history)

if __name__ == "__main__":
    main()
