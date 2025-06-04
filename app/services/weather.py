from app.models import CityWeatherData, WeatherForDate, WeatherHourlyData
from app.services.utils import normalize_score
from typing import List, Dict, Tuple
from datetime import datetime
import requests


def get_weather_comfort_scores(
    temp_c: float, precip_percent: float, wind_kmh: float
) -> Tuple[int, int, int]:
    if temp_c <= 0:
        temp_score = 0
    elif temp_c <= 5:
        temp_score = 20
    elif temp_c <= 10:
        temp_score = 40
    elif temp_c <= 15:
        temp_score = 60
    elif temp_c <= 18:
        temp_score = 80
    elif temp_c <= 21:
        temp_score = 90
    elif temp_c <= 24:
        temp_score = 100
    elif temp_c <= 27:
        temp_score = 80
    elif temp_c <= 29:
        temp_score = 60
    elif temp_c <= 34:
        temp_score = 40
    elif temp_c <= 39:
        temp_score = 20
    else:
        temp_score = 0

    if precip_percent == 0:
        precip_score = 100
    elif precip_percent <= 10:
        precip_score = 90
    elif precip_percent <= 20:
        precip_score = 80
    elif precip_percent <= 30:
        precip_score = 70
    elif precip_percent <= 40:
        precip_score = 60
    elif precip_percent <= 50:
        precip_score = 50
    elif precip_percent <= 60:
        precip_score = 40
    elif precip_percent <= 70:
        precip_score = 30
    elif precip_percent <= 80:
        precip_score = 20
    else:
        precip_score = 10  # Covers 81-100

    if wind_kmh <= 5:
        wind_score = 100
    elif wind_kmh <= 10:
        wind_score = 90
    elif wind_kmh <= 15:
        wind_score = 80
    elif wind_kmh <= 20:
        wind_score = 60
    elif wind_kmh <= 25:
        wind_score = 40
    elif wind_kmh <= 30:
        wind_score = 20
    else:
        wind_score = 10
    return temp_score, precip_score, wind_score


def calculate_raw_weather_score(
    temp_comfort: int, precip_comfort: int, wind_comfort: int
) -> float:
    return 100 - ((temp_comfort + precip_comfort + wind_comfort) / 3.0)


def get_city_coordinates(city: str) -> Tuple[float, float]:
    """Get latitude and longitude for a city using Open-Meteo Geocoding API."""
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={city}&count=1"
    response = requests.get(url)
    data = response.json()

    if not data.get("results"):
        raise ValueError(f"Could not find coordinates for city: {city}")

    result = data["results"][0]
    return result["latitude"], result["longitude"]


def get_city_weather_data(
    city: str, dates: List[str], hours_range: Tuple[str, str]
) -> CityWeatherData:
    weather_by_date_dict = {}
    start_hour = int(hours_range[0].split(":")[0])
    end_hour = int(hours_range[1].split(":")[0])
    all_raw_scores = []
    hourly_data_for_norm_stage: Dict[str, Dict[str, WeatherHourlyData]] = {}

    # Get city coordinates
    lat, lon = get_city_coordinates(city)

    # Convert dates to datetime objects for API
    start_date = datetime.strptime(dates[0], "%Y-%m-%d")
    end_date = datetime.strptime(dates[-1], "%Y-%m-%d")

    # Fetch weather data from Open-Meteo
    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        f"&hourly=temperature_2m,precipitation_probability,wind_speed_10m,weathercode"
        f"&start_date={start_date.strftime('%Y-%m-%d')}"
        f"&end_date={end_date.strftime('%Y-%m-%d')}"
        f"&timezone=auto"
    )

    response = requests.get(url)
    data = response.json()

    # Process the hourly data
    hourly_data = data["hourly"]
    times = hourly_data["time"]
    temperatures = hourly_data["temperature_2m"]
    precipitations = hourly_data["precipitation_probability"]
    wind_speeds = hourly_data["wind_speed_10m"]
    weather_codes = hourly_data["weathercode"]

    # Weather code to summary and emoji mapping
    weather_mapping = {
        0: ("Clear", "☀️"),
        1: ("Partly Cloudy", "🌤️"),
        2: ("Cloudy", "☁️"),
        3: ("Overcast", "☁️"),
        45: ("Foggy", "🌫️"),
        48: ("Foggy", "🌫️"),
        51: ("Light Drizzle", "🌧️"),
        53: ("Drizzle", "🌧️"),
        55: ("Heavy Drizzle", "🌧️"),
        61: ("Light Rain", "🌧️"),
        63: ("Rain", "🌧️"),
        65: ("Heavy Rain", "🌧️"),
        71: ("Light Snow", "🌨️"),
        73: ("Snow", "🌨️"),
        75: ("Heavy Snow", "🌨️"),
        77: ("Snow Grains", "🌨️"),
        80: ("Light Showers", "🌧️"),
        81: ("Showers", "🌧️"),
        82: ("Heavy Showers", "🌧️"),
        85: ("Light Snow Showers", "🌨️"),
        86: ("Heavy Snow Showers", "🌨️"),
        95: ("Thunderstorm", "⛈️"),
        96: ("Thunderstorm with Light Hail", "⛈️"),
        99: ("Thunderstorm with Heavy Hail", "⛈️"),
    }

    for date_str in dates:
        hourly_data_dict = {}
        for hour_int in range(start_hour, end_hour + 1):
            hour_key = f"{hour_int:02d}"

            # Find the corresponding index in the API response
            target_time = f"{date_str}T{hour_key}:00"
            if target_time not in times:
                continue

            idx = times.index(target_time)

            temp_c = temperatures[idx]
            precip_mm_or_percent = precipitations[idx]
            wind_kmh = wind_speeds[idx]
            weather_code = weather_codes[idx]

            summary, emoji = weather_mapping.get(weather_code, ("Unknown", "❓"))

            temp_comfort, precip_comfort, wind_comfort = get_weather_comfort_scores(
                temp_c, precip_mm_or_percent, wind_kmh
            )
            raw_score = calculate_raw_weather_score(
                temp_comfort, precip_comfort, wind_comfort
            )
            all_raw_scores.append(raw_score)

            hourly_data_dict[hour_key] = WeatherHourlyData(
                temp_c=temp_c,
                precipitation_mm=precip_mm_or_percent,
                wind_kmh=wind_kmh,
                summary=summary,
                emoji=emoji,
                temp_comfort_score=temp_comfort,
                precip_comfort_score=precip_comfort,
                wind_comfort_score=wind_comfort,
                raw_weather_score=raw_score,
            )

        weather_by_date_dict[date_str] = WeatherForDate(hourly_data=hourly_data_dict)
        hourly_data_for_norm_stage[date_str] = hourly_data_dict

    meteo_min = min(all_raw_scores) if all_raw_scores else 0
    meteo_max = max(all_raw_scores) if all_raw_scores else 100
    normalized_score_const = 50.0 if meteo_min == meteo_max else None

    for date_str, hourly_entries in hourly_data_for_norm_stage.items():
        for hour_key, data_entry in hourly_entries.items():
            if data_entry.raw_weather_score is not None:
                data_entry.normalized_weather_score = (
                    normalized_score_const
                    if normalized_score_const is not None
                    else normalize_score(
                        data_entry.raw_weather_score, meteo_min, meteo_max
                    )
                )
                weather_by_date_dict[date_str].hourly_data[hour_key] = data_entry

    return CityWeatherData(city=city, weather_by_date=weather_by_date_dict)
