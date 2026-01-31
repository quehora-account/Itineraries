from app.models import (
    CityWeatherData,
    WeatherForDate,
    WeatherHourlyData,
    Spot,
)
from app.services.utils import normalize_score
from typing import List, Dict, Tuple, Set, Any, Optional
from datetime import datetime, timedelta
import requests
import logging
from firebase_admin import firestore
from app.services.firestore_service import get_all_spots_from_db


logger = logging.getLogger(__name__)


def get_weather_comfort_scores(
    temp_c: float, precip_percent: float, wind_kmh: float
) -> Tuple[int, int, int]:
    if (temp_c is None) or (precip_percent is None) or (wind_kmh is None):
        return 0, 10, 10  # Neutral scores for missing data
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


def load_city_weather_from_db(city: str) -> Optional[CityWeatherData]:
    """Load weather data for a city from Firestore database."""
    try:
        # Use city name as document ID (same format as when storing)
        doc_id = city.lower().replace(" ", "-")
        doc_ref = firestore.client().collection("city_weather").document(doc_id)

        doc = doc_ref.get()
        if not doc.exists:
            logger.info(f"No weather data found in database for city: {city}")
            return None

        doc_data = doc.to_dict()

        # Convert document data back to CityWeatherData model
        weather_by_date = {}

        for date_str, date_data in doc_data.get("weather_by_date", {}).items():
            hourly_data = {}

            for hour_key, hour_data in date_data.get("hourly_data", {}).items():
                hourly_data[hour_key] = WeatherHourlyData(
                    temp_c=hour_data.get("temp_c", 0.0),
                    precipitation_mm=hour_data.get("precipitation_mm", 0.0),
                    wind_kmh=hour_data.get("wind_kmh", 0.0),
                    summary=hour_data.get("summary", "Unknown"),
                    emoji=hour_data.get("emoji", "❓"),
                    temp_comfort_score=hour_data.get("temp_comfort_score", 50),
                    precip_comfort_score=hour_data.get("precip_comfort_score", 50),
                    wind_comfort_score=hour_data.get("wind_comfort_score", 50),
                    raw_weather_score=hour_data.get("raw_weather_score", 50.0),
                    normalized_weather_score=hour_data.get(
                        "normalized_weather_score", 50.0
                    ),
                )

            weather_by_date[date_str] = WeatherForDate(hourly_data=hourly_data)

        return CityWeatherData(city=city, weather_by_date=weather_by_date)

    except Exception as e:
        logger.error(f"Error loading weather data from database for city {city}: {e}")
        return None


def get_city_weather_data_from_api(
    city: str, dates: List[str], hourly_availability: Dict[str, List[str]]
) -> CityWeatherData:
    """Get weather data from API (original implementation)."""
    weather_by_date_dict = {}
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
        0: ("Ensoleillé", "☀️"),
        1: ("Partiellement nuageux", "🌤️"),
        2: ("Nuageux", "☁️"),
        3: ("Couvert", "☁️"),
        45: ("Brouillard", "🌫️"),
        48: ("Brouillard", "🌫️"),
        51: ("Bruine légère", "🌧️"),
        53: ("Bruine", "🌧️"),
        55: ("Bruine forte", "🌧️"),
        61: ("Pluie légère", "🌧️"),
        63: ("Pluie", "🌧️"),
        65: ("Pluie forte", "🌧️"),
        71: ("Neige légère", "🌨️"),
        73: ("Neige", "🌨️"),
        75: ("Neige forte", "🌨️"),
        77: ("Grains de neige", "🌨️"),
        80: ("Averses légères", "🌧️"),
        81: ("Averses", "🌧️"),
        82: ("Averses fortes", "🌧️"),
        85: ("Averses de neige légères", "🌨️"),
        86: ("Averses de neige fortes", "🌨️"),
        95: ("Orage", "⛈️"),
        96: ("Orage avec grêle légère", "⛈️"),
        99: ("Orage avec grêle forte", "⛈️"),
    }

    for date_str in dates:
        hourly_data_dict = {}
        start_hour = int(hourly_availability[date_str][0].split(":")[0])
        end_hour = int(hourly_availability[date_str][1].split(":")[0])
        for hour_int in range(start_hour, end_hour + 1):
            hour_key = f"{hour_int:02d}"

            # Find the corresponding index in the API response
            target_time = f"{date_str}T{hour_key}:00"
            if target_time not in times:
                continue

            idx = times.index(target_time)

            temp_c = temperatures[idx] is not None and temperatures[idx] or 0.0
            precip_mm_or_percent = precipitations[idx] is not None and precipitations[idx] or 0.0
            wind_kmh = wind_speeds[idx] is not None and wind_speeds[idx] or 0.0
            weather_code = weather_codes[idx] is not None and weather_codes[idx] or 0

            summary, emoji = weather_mapping.get(weather_code, ("Unknown", "❓"))
            logger.info(f"Processing weather data for {idx}: temp={temperatures}, precipitation={precip_mm_or_percent}, wind={wind_kmh}, code={weather_code}")

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


def check_weather_data_coverage(
    weather_data: CityWeatherData, dates: List[str], hourly_availability: Dict[str, List[str]]
) -> bool:
    """Check if weather data covers all required dates and hours."""
    for date_str in dates:
        if date_str not in hourly_availability:
            return False
        start_hour = int(hourly_availability[date_str][0].split(":")[0])
        end_hour = int(hourly_availability[date_str][1].split(":")[0])

    for date_str in dates:
        if date_str not in weather_data.weather_by_date:
            return False

        hourly_data = weather_data.weather_by_date[date_str].hourly_data
        for hour_int in range(start_hour, end_hour + 1):
            hour_key = f"{hour_int:02d}"
            if hour_key not in hourly_data:
                return False

    return True


def renormalize_weather_data_for_trip(
    weather_data: CityWeatherData, dates: List[str], hours_range: Dict[str, List[str]],
) -> CityWeatherData:
    """
    Re-normalize weather data using min/max across the entire trip dates and hours.
    This ensures consistent normalization across all cities for the user's trip.
    """

    # Collect all raw weather scores for the requested dates and hours
    all_raw_scores = []

    for date_str in dates:
        start_hour = int(hours_range[date_str][0].split(":")[0])
        end_hour = int(hours_range[date_str][1].split(":")[0])
        
        if date_str in weather_data.weather_by_date:
            hourly_data = weather_data.weather_by_date[date_str].hourly_data
            for hour_int in range(start_hour, end_hour + 1):
                hour_key = f"{hour_int:02d}"
                if (
                    hour_key in hourly_data
                    and hourly_data[hour_key].raw_weather_score is not None
                ):
                    all_raw_scores.append(hourly_data[hour_key].raw_weather_score)

    if not all_raw_scores:
        return weather_data

    # Calculate min/max for the trip
    meteo_min = min(all_raw_scores)
    meteo_max = max(all_raw_scores)
    normalized_score_const = 50.0 if meteo_min == meteo_max else None

    # Re-normalize all weather scores for the trip dates/hours
    for date_str in dates:
        if date_str in weather_data.weather_by_date:
            hourly_data = weather_data.weather_by_date[date_str].hourly_data
            for hour_int in range(start_hour, end_hour + 1):
                hour_key = f"{hour_int:02d}"
                if (
                    hour_key in hourly_data
                    and hourly_data[hour_key].raw_weather_score is not None
                ):
                    weather_data.weather_by_date[date_str].hourly_data[
                        hour_key
                    ].normalized_weather_score = (
                        normalized_score_const
                        if normalized_score_const is not None
                        else normalize_score(
                            hourly_data[hour_key].raw_weather_score,
                            meteo_min,
                            meteo_max,
                        )
                    )

    return weather_data


def get_multiple_cities_weather_data(
    cities: List[str], dates: List[str], hours_range: Tuple[str, str]
) -> Dict[str, CityWeatherData]:
    """
    Get weather data for multiple cities and normalize scores consistently
    across ALL cities for the entire trip.

    This ensures that weather scores are comparable between different cities
    during the user's trip by using global min/max across all cities and dates.

    Formula used: score_meteo = (raw_weather_score - meteo_min) / (meteo_max - meteo_min) * 100
    where meteo_min and meteo_max are extremes observed across ALL hours of ALL cities
    during the entire trip.
    """
    logger.info(f"Getting weather data for cities: {cities}")

    # Step 1: Get raw weather data for all cities
    cities_weather_data: Dict[str, CityWeatherData] = {}
    all_raw_scores_global = []

    start_hour = int(hours_range[0].split(":")[0])
    end_hour = int(hours_range[1].split(":")[0])

    for city in cities:
        try:
            weather_data = get_city_weather_data(city, dates, hours_range)
            cities_weather_data[city] = weather_data

            # Collect all raw scores from this city for global normalization
            for date_str in dates:
                if date_str in weather_data.weather_by_date:
                    hourly_data = weather_data.weather_by_date[date_str].hourly_data
                    for hour_int in range(start_hour, end_hour + 1):
                        hour_key = f"{hour_int:02d}"
                        if (
                            hour_key in hourly_data
                            and hourly_data[hour_key].raw_weather_score is not None
                        ):
                            all_raw_scores_global.append(
                                hourly_data[hour_key].raw_weather_score
                            )

        except Exception as e:
            logger.error(f"Failed to get weather data for city {city}: {e}")
            continue

    if not all_raw_scores_global:
        logger.warning("No weather data collected for global normalization")
        return cities_weather_data

    # Step 2: Calculate global min/max across ALL cities and dates
    meteo_min_global = min(all_raw_scores_global)
    meteo_max_global = max(all_raw_scores_global)
    normalized_score_const = 50.0 if meteo_min_global == meteo_max_global else None

    logger.info(
        f"Global weather score range: min={meteo_min_global:.2f}, max={meteo_max_global:.2f}"
    )

    # Step 3: Re-normalize all cities with global min/max
    for city, weather_data in cities_weather_data.items():
        for date_str in dates:
            if date_str in weather_data.weather_by_date:
                hourly_data = weather_data.weather_by_date[date_str].hourly_data
                for hour_int in range(start_hour, end_hour + 1):
                    hour_key = f"{hour_int:02d}"
                    if (
                        hour_key in hourly_data
                        and hourly_data[hour_key].raw_weather_score is not None
                    ):
                        # Apply global normalization: (raw_score - meteo_min) / (meteo_max - meteo_min) * 100
                        weather_data.weather_by_date[date_str].hourly_data[
                            hour_key
                        ].normalized_weather_score = (
                            normalized_score_const
                            if normalized_score_const is not None
                            else normalize_score(
                                hourly_data[hour_key].raw_weather_score,
                                meteo_min_global,
                                meteo_max_global,
                            )
                        )

    logger.info(
        f"Successfully normalized weather data for {len(cities_weather_data)} cities"
    )
    return cities_weather_data


def get_city_weather_data(
    city: str, dates: List[str], hourly_availability: Dict[str, List[str]],
) -> CityWeatherData:
    """
    Get weather data for a city, preferring database over API.

    First tries to load from database, then falls back to API if:
    - No data exists in database for the city
    - Database data doesn't cover all required dates/hours
    - Database data is older than 6 hours

    Always re-normalizes the weather scores based on the min/max across
    the entire trip (all requested dates and hours).
    """
    logger.info(f"Getting weather data for city: {city}")

    # Try to load from database first
    cached_weather_data = load_city_weather_from_db(city)

    if cached_weather_data:
        logger.info(f"Found weather data in database for city: {city}")

        # Check if cached data covers all required dates and hours
        if check_weather_data_coverage(cached_weather_data, dates, hourly_availability):
            logger.info(
                f"Database weather data covers all required dates/hours for city: {city}"
            )
            # Re-normalize weather data for the specific trip dates/hours
            return renormalize_weather_data_for_trip(
                cached_weather_data, dates, hourly_availability
            )
        else:
            logger.info(
                f"Database weather data incomplete for city: {city}, falling back to API"
            )

    # Fall back to API if no cached data or incomplete coverage
    logger.info(f"Fetching weather data from API for city: {city}")
    return get_city_weather_data_from_api(city, dates, hourly_availability)


def extract_cities_from_spots(spots: List[Spot]) -> Set[str]:
    """Extract unique city names from the list of spots."""
    cities = set()
    for spot in spots:
        if spot.cityId:
            # Clean city name by removing '-city' suffix if present
            city_name = spot.cityId.replace("-city", "").strip()
            city_name = city_name.replace("-City", "").strip()
            if city_name:
                cities.add(city_name)
    return cities


def create_weather_collection_document(
    city: str, weather_data: CityWeatherData
) -> Dict[str, Any]:
    """Convert CityWeatherData to a Firestore-compatible document."""
    doc_data = {
        "city": weather_data.city,
        "last_updated": datetime.now(),
        "weather_by_date": {},
    }

    # Convert the weather data to a serializable format
    for date_str, weather_for_date in weather_data.weather_by_date.items():
        doc_data["weather_by_date"][date_str] = {"hourly_data": {}}

        for hour_key, hourly_data in weather_for_date.hourly_data.items():
            doc_data["weather_by_date"][date_str]["hourly_data"][hour_key] = {
                "temp_c": hourly_data.temp_c,
                "precipitation_mm": hourly_data.precipitation_mm,
                "wind_kmh": hourly_data.wind_kmh,
                "summary": hourly_data.summary,
                "emoji": hourly_data.emoji,
                "temp_comfort_score": hourly_data.temp_comfort_score,
                "precip_comfort_score": hourly_data.precip_comfort_score,
                "wind_comfort_score": hourly_data.wind_comfort_score,
                "raw_weather_score": hourly_data.raw_weather_score,
                "normalized_weather_score": hourly_data.normalized_weather_score,
            }

    return doc_data


def update_city_weather_in_db(city: str, weather_data: CityWeatherData) -> None:
    """Update weather data for a city in Firestore."""
    try:
        # Use city name as document ID for easy retrieval
        doc_id = city.lower().replace(" ", "-")
        doc_ref = firestore.client().collection("city_weather").document(doc_id)

        # Convert weather data to Firestore document
        doc_data = create_weather_collection_document(city, weather_data)

        # Set the document (this will create or update)
        doc_ref.set(doc_data)
        logger.info(f"Successfully updated weather data for city: {city}")

    except Exception as e:
        logger.error(f"Error updating weather data for city {city}: {e}")
        raise


def update_daily_weather_data() -> None:
    """
    Function that updates weather data for all cities.
    Designed to be called from a cron job that runs daily at 6 AM Paris time.

    This function:
    1. Fetches all spots from the database
    2. Extracts unique cities from the spots
    3. Gets weather data for each city for the next 7 days
    4. Updates the Firestore collection with the weather data
    """
    try:
        logger.info("Starting daily weather data update...")

        # Step 1: Fetch all spots from database
        logger.info("Fetching all spots from database...")
        spots = get_all_spots_from_db()
        logger.info(f"Found {len(spots)} spots in database")

        # Step 2: Extract unique cities
        logger.info("Extracting cities from spots...")
        cities = extract_cities_from_spots(spots)
        logger.info(f"Found {len(cities)} unique cities: {', '.join(sorted(cities))}")

        # Step 3: Generate date range for the next 14 days
        today = datetime.now()
        dates = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14)]
        hours_range = ("07", "23")  # 7 AM to 11 PM

        # Step 4: Get weather data for each city and update database
        successful_updates = 0
        failed_updates = 0

        for city in cities:
            try:
                logger.info(f"Getting weather data for city: {city}")
                weather_data = get_city_weather_data_from_api(city, dates, hours_range)

                # Update the database
                update_city_weather_in_db(city, weather_data)
                successful_updates += 1

            except Exception as e:
                logger.error(f"Failed to update weather for city {city}: {e}")
                failed_updates += 1
                continue

        # Log summary
        total_cities = len(cities)
        logger.info(
            f"Weather update completed: {successful_updates}/{total_cities} cities updated successfully"
        )
        if failed_updates > 0:
            logger.warning(f"{failed_updates} cities failed to update")

    except Exception as e:
        logger.error(f"Critical error in daily weather update: {e}")
        raise


if __name__ == "__main__":
    update_daily_weather_data()
