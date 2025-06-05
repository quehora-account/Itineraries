from fastapi import APIRouter, HTTPException
from typing import List, Tuple, Optional
from app.models import (
    MatrixTime,
    MatrixScoreDistance,
    CityWeatherData,
    CrowdScoreInput,
    PreparedSolverData,
    UserPreferences,
    OptimizationMode,
    SolverInputWeights,
    TravelSegment,
    TravelMode,
    UserWindow,
    SolverSpotInfo,
    SolverLunchInfo,
    SolverDepotInfo,
)
from app.services.utils import (
    normalize_score,
    time_str_to_minutes,
    get_adjusted_visit_duration,
    get_affluence_score,
    LUNCH_DURATION_MIN,
)
from app.services.weather import get_city_weather_data
from app.services.firestore_service import get_spot_from_db, get_all_spots_from_db
from app.services.distance import (
    get_distance_matrix,
    get_travel_mode_and_time,
    haversine_distance_km,
)
from typing import Dict, Union

data_prep_router = APIRouter(prefix="/prepare", tags=["Data Preparation"])


@data_prep_router.post(
    "/get-distance", response_model=Tuple[MatrixTime, MatrixScoreDistance]
)
def get_distance_endpoint(spot_ids: List[str], max_walk_time_per_segment_min: int = 30):
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


@data_prep_router.post("/compute-all-distances", response_model=CityWeatherData)
def compute_all_distances_endpoint(max_walk_time_per_segment_min: int = 30):
    print(
        f"Computing all distances with max_walk_time_per_segment_min: {max_walk_time_per_segment_min}"
    )
    spots = get_all_spots_from_db()
    print(f"Found {len(spots)} spots")
    matrix_time, matrix_score_distance = get_distance_matrix(
        spots, max_walk_time_per_segment_min
    )
    print(f"Matrix time: {matrix_time}")
    print(f"Matrix score distance: {matrix_score_distance}")
    return matrix_time, matrix_score_distance


@data_prep_router.post("/weather-data", response_model=CityWeatherData)
async def prepare_weather_data_endpoint_new(
    city: str,
    travel_dates: List[str],
    daily_hours_range: Tuple[str, str] = ("08:00", "18:00"),
):
    if not travel_dates:
        raise HTTPException(status_code=400, detail="Travel dates must be provided.")
    return get_city_weather_data(city, travel_dates, daily_hours_range)


@data_prep_router.post("/affluence", response_model=float)
async def get_affluence_score_endpoint(
    data: CrowdScoreInput,
):
    return get_affluence_score(data.popular_time, data.density_index)

