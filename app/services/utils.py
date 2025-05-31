import math
import random
from app.models import (
    VisitPace,
    Spot,
    TravelMode,
    CityWeatherData,
    WeatherHourlyData,
    WeatherForDate,
)
from typing import List, Dict, Tuple
from openai import OpenAI
from app.core.config import settings
from google.cloud.firestore_v1.vector import Vector

DEFAULT_TRANSPORT_MOYEN_MIN = 30
LUNCH_DURATION_MIN = 90

client = OpenAI(api_key=settings.OPENAI_API_KEY)

def get_embedding(
    text: str
) -> Vector:
    response = client.embeddings.create(
        input=text,
        model="text-embedding-3-small",
    )
    return response.data[0].embedding


def cosine_similarity(vec1: Vector, vec2: Vector) -> float:
    if len(vec1) != len(vec2) or not vec1 or not vec2:
        return 0.0
    dot_product = sum(p * q for p, q in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(p * p for p in vec1))
    magnitude2 = math.sqrt(sum(q * q for q in vec2))
    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0
    return dot_product / (magnitude1 * magnitude2)


def get_duration_category(duration_min: int) -> str:
    if duration_min <= 30:
        return "≤ 30 min"
    if duration_min <= 120:
        return "30 min - 2h"
    if duration_min <= 180:
        return "2h-3h"
    return "> 3h"


def get_adjusted_visit_duration(standard_duration_min: int, pace: VisitPace) -> int:
    if standard_duration_min <= 30:
        return 30
    adjustment = 0
    category = get_duration_category(standard_duration_min)
    if category == "30 min - 2h":
        if pace == VisitPace.FAST:
            adjustment = -15
        elif pace == VisitPace.RELAXED:
            adjustment = 15
    elif category == "2h-3h":
        if pace == VisitPace.FAST:
            adjustment = -30
        elif pace == VisitPace.RELAXED:
            adjustment = 30
    elif category == "> 3h":
        if pace == VisitPace.FAST:
            adjustment = -60
        elif pace == VisitPace.RELAXED:
            adjustment = 60
    return max(30, standard_duration_min + adjustment)


def time_str_to_minutes(time_str: str) -> int:
    h, m = map(int, time_str.split(":"))
    return h * 60 + m


def minutes_to_time_str(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371
    lat1_rad, lon1_rad = math.radians(lat1), math.radians(lon1)
    lat2_rad, lon2_rad = math.radians(lat2), math.radians(lon2)
    dlon = lon2_rad - lon1_rad
    dlat = lat2_rad - lat1_rad
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.asin(math.sqrt(a))
    return R * c


def calculate_travel_time_min(distance_km: float, mode: TravelMode) -> int:
    if mode == TravelMode.WALK:
        return math.ceil(distance_km * 12)
    elif mode == TravelMode.TRANSPORT:
        return math.ceil(distance_km * 4)
    return 0


def get_travel_mode_and_time(
    spot_from: Spot, spot_to: Spot, max_walk_time_min: int
) -> Tuple[TravelMode, int]:
    distance_km = haversine_distance_km(
        spot_from.latitude, spot_from.longitude, spot_to.latitude, spot_to.longitude
    )
    walk_time = calculate_travel_time_min(distance_km, TravelMode.WALK)
    if walk_time <= max_walk_time_min:
        return TravelMode.WALK, walk_time
    else:
        transport_time = calculate_travel_time_min(distance_km, TravelMode.TRANSPORT)
        return TravelMode.TRANSPORT, transport_time


def normalize_score(
    value: float, min_val: float, max_val: float, scale_to: float = 100.0
) -> float:
    if max_val == min_val:
        return scale_to / 2
    return ((value - min_val) / (max_val - min_val)) * scale_to


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


def simulate_get_city_weather_data(
    city: str, dates: List[str], hours_range: Tuple[str, str]
) -> CityWeatherData:
    weather_by_date_dict = {}
    start_hour = int(hours_range[0].split(":")[0])
    end_hour = int(hours_range[1].split(":")[0])
    all_raw_scores = []
    hourly_data_for_norm_stage: Dict[str, Dict[str, WeatherHourlyData]] = {}

    for date_str in dates:
        hourly_data_dict = {}
        for hour_int in range(
            start_hour, end_hour + 1
        ):  # Assuming end_hour is inclusive
            hour_key = f"{hour_int:02d}"
            temp_c, precip_mm_or_percent, wind_kmh = (
                random.uniform(5, 30),
                random.uniform(0, 100),
                random.uniform(0, 35),
            )
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
                summary=random.choice(["Clear", "Cloudy", "Rain"]),
                emoji=random.choice(["☀️", "☁️", "🌧️"]),
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


def calculate_crowd_score_brut(popular_time: int, density_index: int) -> float:
    if not (0 <= popular_time <= 100):
        popular_time = 50
    coefficient = density_index * 0.2
    return popular_time * coefficient
