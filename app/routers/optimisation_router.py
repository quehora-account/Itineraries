from fastapi import APIRouter, HTTPException
from typing import List, Tuple

from app.models import (
    OptimizationMode,
    SimpleTimeGauge,
    FinalItineraryOutput,
)
from app.services.weather import load_city_weather_from_db
from app.services.distance import (
    get_distance_matrix,
)
from app.services.firestore_service import get_spot_from_db
from app.services.optimisation import (
    prepare_optimization_data,
    solve_vrp,
    calculate_final_scores,
    convert_solution_to_itinerary,
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

    if optimization_mode not in [OptimizationMode.FREEMIUM, OptimizationMode.PREMIUM]:
        raise HTTPException(status_code=400, detail="Invalid optimization mode.")

    city_weather_data = None
    if optimization_mode == OptimizationMode.PREMIUM:
        if not city:
            raise HTTPException(status_code=400, detail="City must be provided.")

        city_weather_data = load_city_weather_from_db(city)

    # Get spots data
    spots_data = []
    for spot in data.spots:
        spot_data = get_spot_from_db(spot.id)
        if spot_data is None:
            raise HTTPException(status_code=404, detail=f"Spot {spot.id} not found")
        spots_data.append(spot_data)

    # Get distance matrix
    distance_matrix = get_distance_matrix(spots_data, max_walk_time_per_segment_min)

    # Prepare optimization data
    solver_data = prepare_optimization_data(
        spots_data=spots_data,
        travel_dates=travel_dates,
        daily_hours_range=daily_hours_range,
        optimization_mode=optimization_mode,
        city_weather_data=city_weather_data,
        distance_matrix=distance_matrix,
    )

    # Solve the optimization problem
    solution_data = solve_vrp(solver_data)

    if not solution_data or not solution_data.get("success"):
        raise HTTPException(
            status_code=500,
            detail="Unable to find optimal itinerary. Please try with different constraints.",
        )

    # Calculate final scores
    scores = calculate_final_scores(solution_data, solver_data, travel_dates)

    # Convert to final itinerary format
    daily_itineraries = convert_solution_to_itinerary(
        solution_data=solution_data,
        solver_data=solver_data,
        travel_dates=travel_dates,
        spots_data=spots_data,
        city=city,
        companions="voyageurs",
    )

    return FinalItineraryOutput(days=daily_itineraries, scores=scores)
