from fastapi import APIRouter, HTTPException, Body
from app.models import (
    SelectSpotsRequest,
    SimpleTimeGauge,
    SimplifiedSpot,
    VisitPace,
    SimpleUserPreferences,
)
from app.services.firestore_service import get_spot_from_db
from app.services.utils import (
    get_adjusted_visit_duration,
    parse_visit_duration_to_minutes,
)

itinerary_router = APIRouter(prefix="/itinerary", tags=["Itinerary Planning"])


@itinerary_router.post("/select-spots", response_model=SimpleTimeGauge)
async def select_spots(
    request: SelectSpotsRequest = Body(
        default=SelectSpotsRequest(
            selected_spot_ids=[
                "71sKTux0pjVafHBlebaE",
                "AmeCrkZVdM0BYV6t9wNG",
                "At80BN8aB5xOsOd8zPzn",
                "CFgXlW1MYzYyQagvengL",
                "Cpsa7mUr9c5Jq1OiBsoQ",
            ],
            user_preferences=SimpleUserPreferences(
                user_time_available=1290,
                visit_pace=VisitPace.BALANCED,
                n_days=3,
                transport_moyen=30,
            ),
        )
    ),
):
    """
    Simplified spot selection based on time gauge filling algorithm.
    Calculates how many spots can fit in the available time without scheduling them.
    """

    # Extract data from request object
    selected_spot_ids = request.selected_spot_ids
    preferences = request.user_preferences

    # Get spots from database and calculate their scores
    spots_with_scores = []

    for spot_id in selected_spot_ids:
        spot = get_spot_from_db(spot_id)
        if not spot:
            raise HTTPException(status_code=404, detail=f"Spot not found: {spot_id}")

        spots_with_scores.append({"spot": spot, "final_score": spot.score})

    # Sort spots by score (descending - best first)
    spots_with_scores.sort(key=lambda x: x["final_score"], reverse=True)

    # Calculate available time and apply progressive filling algorithm
    temps_restant = preferences.user_time_available
    spots_in_jauge = []

    for index, spot_data in enumerate(spots_with_scores):
        spot = spot_data["spot"]
        final_score = spot_data["final_score"]

        # Calculate visit duration based on pace
        standard_duration_min = parse_visit_duration_to_minutes(spot.visitDuration)
        duree_ajustee = get_adjusted_visit_duration(
            standard_duration_min, preferences.visit_pace
        )

        # Calculate spot cost
        cout_spot = duree_ajustee

        # Add transport time if this is not one of the first n_days spots
        if index >= preferences.n_days:
            cout_spot += preferences.transport_moyen

        # Check if spot fits in remaining time
        if cout_spot <= temps_restant:
            simplified_spot = SimplifiedSpot(
                id=spot.id,
                name=spot.name,
                type=spot.type,
                images=spot.imageGalleryPaths,
                rating=spot.rating,
                ville=spot.cityId.replace("-city", ""),
                final_score=int(final_score / 10000),
            )

            spots_in_jauge.append(simplified_spot)
            temps_restant -= cout_spot
        else:
            continue

    return SimpleTimeGauge(
        spots=spots_in_jauge, time_remaining_after_visits=temps_restant
    )


@itinerary_router.post("/itinerary-validation")
async def itinerary_validation(
    data: SimpleTimeGauge,
):
    remaining_time_min = data.time_remaining_after_visits
    if remaining_time_min < 0:
        return "Votre sélection dépasse le temps disponible. Veuillez retirer un ou plusieurs lieux de visite."
    elif remaining_time_min < 120:
        return "optimisation"
    else:
        return "Il vous reste du temps. Souhaitez-vous ajouter des visites?"
