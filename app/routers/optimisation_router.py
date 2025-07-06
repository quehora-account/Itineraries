from fastapi import APIRouter, HTTPException, Depends
from typing import List, Tuple

from app.models import (
    OptimizationMode,
    SimpleTimeGauge,
    FinalItineraryOutput,
    SimplifiedSpot,
)
from app.services.weather import get_city_weather_data
from app.services.firestore_service import (
    get_spot_from_db,
    load_distance_matrix_from_db,
    load_optimisation_weights_from_db,
)
from app.services.optimisation import (
    filter_distance_matrix,
    optimise_travel,
)

optimisation_router = APIRouter(prefix="/optimisation", tags=["Itinerary Optimisation"])


def default_simple_time_gauge():
    return SimpleTimeGauge(
        spots=[
            SimplifiedSpot(
                id="71sKTux0pjVafHBlebaE",
                name="Basilique du Sacré-Cœur de Montmartre",
                type="Église",
                images=[
                    "1-Basilique du Sacré-Cœur de Montmartre.jpg",
                    "2-Basilique du Sacré-Cœur de Montmartre.jpg",
                    "3-Basilique du Sacré-Cœur de Montmartre.jpg",
                ],
                rating=4.7,
                ville="Paris",
                final_score=100,
            ),
            SimplifiedSpot(
                id="At80BN8aB5xOsOd8zPzn",
                name="Jardin des Tuileries",
                type="Jardin ",
                images=[
                    "1-Jardin des Tuileries.jpg",
                    "2-Jardin des Tuileries.jpg",
                    "3-Jardin des Tuileries.jpg",
                    "4-Jardin des Tuileries.jpg",
                    "5-Jardin des Tuileries.jpg",
                ],
                rating=4.6,
                ville="Paris",
                final_score=100,
            ),
            SimplifiedSpot(
                id="Cpsa7mUr9c5Jq1OiBsoQ",
                name="Panthéon",
                type="Monument",
                images=[
                    "1-Panthéon.jpg",
                    "2-Panthéon.jpg",
                    "3-Panthéon.jpg",
                    "4-Panthéon.jpg",
                    "5-Panthéon.jpg",
                ],
                rating=4.6,
                ville="Paris",
                final_score=100,
            ),
            SimplifiedSpot(
                id="CFgXlW1MYzYyQagvengL",
                name="La Villette",
                type=" Parc",
                images=[
                    "1-La Villette.jpg",
                    "2-La Villette.jpg",
                    "3-La Villette.jpg",
                    "4-La Villette.jpg",
                ],
                rating=4.4,
                ville="Paris",
                final_score=100,
            ),
            SimplifiedSpot(
                id="AmeCrkZVdM0BYV6t9wNG",
                name="Musée de l'Armée",
                type="Musée",
                images=[
                    "1-Musée de l'Armée.jpg",
                    "2-Musée de l'Armée.jpg",
                    "3-Musée de l'Armée.jpg",
                    "4-Musée de l'Armée.jpg",
                    "5-Musée de l'Armée.jpg",
                ],
                rating=4.6,
                ville="Paris",
                final_score=100,
            ),
        ],
        time_remaining_after_visits=600,
    )


@optimisation_router.post("/optimise-itinerary", response_model=FinalItineraryOutput)
async def optimise_itinerary_endpoint(
    data: SimpleTimeGauge = default_simple_time_gauge(),
    city: str = "Paris-city",
    travel_dates: List[str] = ["2025-07-14", "2025-07-12"],
    daily_hours_range: Tuple[str, str] = ("08:00", "18:00"),
    optimization_mode: OptimizationMode = OptimizationMode.FREEMIUM,
    max_walk_time_per_segment_min: int = 30,
):
    if not travel_dates:
        raise HTTPException(status_code=400, detail="Travel dates must be provided.")

    if optimization_mode not in [OptimizationMode.FREEMIUM, OptimizationMode.PREMIUM]:
        raise HTTPException(status_code=400, detail="Invalid optimization mode.")

    # Get spots data
    spots_data = []
    for spot in data.spots:
        spot_data = get_spot_from_db(spot.id)
        if spot_data is None:
            raise HTTPException(status_code=404, detail=f"Spot {spot.id} not found")

        del spot_data.embedding
        spots_data.append(spot_data)

    optimised_travel = optimise_travel(
        city,
        spots_data,
        travel_dates,
        daily_hours_range,
        optimization_mode,
        max_walk_time_per_segment_min,
    )

    return optimised_travel
