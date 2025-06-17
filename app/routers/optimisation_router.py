from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from fastapi import APIRouter, HTTPException
from typing import List, Tuple
from app.models import (
    OptimizationMode,
    SimpleTimeGauge,
    FinalItineraryOutput,
)
from app.services.weather import get_city_weather_data
from app.services.distance import (
    get_distance_matrix,
)
from app.services.firestore_service import get_spot_from_db
from app.services.utils import (
    get_affluence_score,
)

optimisation_router = APIRouter(prefix="/optimisation", tags=["Itinerary Optimisation"])

@optimisation_router.post("/optimise-itinerary", response_model=FinalItineraryOutput)
async def optimise_itinerary_endpoint(
    data: SimpleTimeGauge,
    city: str,
    travel_dates: List[str],
    daily_hours_range: Tuple[str, str] = ("08:00", "18:00"),
    optimization_mode: OptimizationMode = OptimizationMode.FREEMIUM,
    max_walk_time_per_segment_min: int = 30,
):
    if not travel_dates:
        raise HTTPException(status_code=400, detail="Travel dates must be provided.")

    if not daily_hours_range or len(daily_hours_range) != 2:
        raise HTTPException(status_code=400, detail="Daily hours range must be provided as a tuple of two strings (start_time, end_time).")

    if optimization_mode not in [OptimizationMode.FREEMIUM, OptimizationMode.PREMIUM]:
        raise HTTPException(status_code=400, detail="Invalid optimization mode.")
    
    if optimization_mode == OptimizationMode.PREMIUM:
        if not city:
            raise HTTPException(status_code=400, detail="City must be provided.")

        city_weather_data = get_city_weather_data(city, travel_dates, daily_hours_range) 
        
    # Get spots
    spots_data = []
    for spot in data.spots_in_jauge:
        spot_data = get_spot_from_db(spot.id)
        if spot_data is None:
            raise HTTPException(status_code=404, detail=f"Spot {spot.id} not found")
        spots_data.append(spot_data)

    # Get distance matrix
    distance_matrix = await get_distance_matrix(spots_data, max_walk_time_per_segment_min)

