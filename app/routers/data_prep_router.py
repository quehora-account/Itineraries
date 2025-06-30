from fastapi import APIRouter, HTTPException, Body
from typing import List, Tuple, Optional
from app.models import (
    MatrixTime,
    MatrixScoreDistance,
    CityWeatherData,
    CrowdScoreInput,
)
from app.services.utils import (
    get_affluence_score,
)
from app.services.weather import get_city_weather_data, get_multiple_cities_weather_data
from app.services.firestore_service import (
    get_spot_from_db,
    get_all_spots_from_db,
    save_distance_matrices_to_db,
)
from app.services.distance import (
    get_distance_matrix,
)
from typing import Dict

data_prep_router = APIRouter(prefix="/prepare", tags=["Data Preparation"])


@data_prep_router.post(
    "/get-distance", response_model=Tuple[MatrixTime, MatrixScoreDistance]
)
def get_distance_endpoint(
    spot_ids: List[str] = Body(
        ..., example=["71sKTux0pjVafHBlebaE", "AmeCrkZVdM0BYV6t9wNG"]
    ),
    max_walk_time_per_segment_min: int = 30,
):
    spots_to_process = []
    for sid in spot_ids:
        spot = get_spot_from_db(sid)
        if spot:
            spots_to_process.append(spot)

    if not spots_to_process:
        raise HTTPException(
            status_code=400, detail="No valid spot IDs provided or spots not found."
        )

    return get_distance_matrix(spots_to_process, max_walk_time_per_segment_min)


@data_prep_router.post(
    "/compute-all-distances", response_model=Tuple[MatrixTime, MatrixScoreDistance]
)
def compute_all_distances_endpoint(max_walk_time_per_segment_min: int = 30):
    print(
        f"Computing all distances with max_walk_time_per_segment_min: {max_walk_time_per_segment_min}"
    )
    spots = get_all_spots_from_db()
    print(f"Found {len(spots)} spots")
    matrix_time = get_distance_matrix(spots, max_walk_time_per_segment_min)
    print(f"Matrix time: {matrix_time}")

    # Save the computed matrices to the database
    save_distance_matrices_to_db(matrix_time, max_walk_time_per_segment_min)

    return matrix_time


@data_prep_router.post("/weather-data", response_model=CityWeatherData)
async def prepare_weather_data_endpoint_new(
    city: str,
    travel_dates: List[str] = ["2025-06-13", "2025-06-14", "2025-06-15"],
    daily_hours_range: Tuple[str, str] = ("08:00", "18:00"),
):
    if not travel_dates:
        raise HTTPException(status_code=400, detail="Travel dates must be provided.")
    return get_city_weather_data(city, travel_dates, daily_hours_range)


@data_prep_router.post(
    "/weather-data-multiple-cities", response_model=Dict[str, CityWeatherData]
)
async def prepare_weather_data_multiple_cities_endpoint(
    cities: List[str],
    travel_dates: List[str] = ["2025-06-13", "2025-06-14", "2025-06-15"],
    daily_hours_range: Tuple[str, str] = ("08:00", "18:00"),
):
    """
    Get weather data for multiple cities with consistent normalization across all cities.

    This endpoint ensures that weather scores are comparable between different cities
    during the user's trip by using global min/max across all cities and dates.

    The normalization formula used is:
    score_meteo = (raw_weather_score - meteo_min) / (meteo_max - meteo_min) * 100

    Where meteo_min and meteo_max are the extremes observed across ALL hours
    of ALL cities during the entire trip.
    """
    if not cities:
        raise HTTPException(
            status_code=400, detail="At least one city must be provided."
        )
    if not travel_dates:
        raise HTTPException(status_code=400, detail="Travel dates must be provided.")

    return get_multiple_cities_weather_data(cities, travel_dates, daily_hours_range)


@data_prep_router.post("/affluence", response_model=float)
async def get_affluence_score_endpoint(
    data: CrowdScoreInput,
):
    return get_affluence_score(data.popular_time, data.density_index)
