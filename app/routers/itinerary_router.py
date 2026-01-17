import logging
from app.enums import OptimizationMode
from app.services.optimisation import optimise_travel
from fastapi import APIRouter, HTTPException, Body
from app.models import (
    SelectSpotsRequest,
    SimpleTimeGauge,
    SimplifiedSpot,
    ValidationRequest,
    ValidationResponse,
    VisitPace,
    SimpleUserPreferences,
)
from app.services.firestore_service import get_spot_from_db
from app.services.utils import (
    apply_visit_pace_adjustment,
    calculate_time_gauge,
    get_adjusted_visit_duration,
    parse_visit_duration_to_minutes,
)

itinerary_router = APIRouter(prefix="/itinerary", tags=["Itinerary Planning"])

# Get logger for this module
logger = logging.getLogger(__name__)


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

@itinerary_router.post("/itinerary-validation", response_model=ValidationResponse)
async def itinerary_validation(data: ValidationRequest):
    logger.info(f"🔍 Starting itinerary validation for {len(data.selected_spot_ids)} spots in {data.destination}")
    logger.info(f"📅 Travel dates: {data.travel_dates}, Premium: {data.premium}")
        # 1. Calculate available time and number of days 
    # calculate_time_gauge handles lunch removal (90 min) 
    time_dispo_total, n_days = calculate_time_gauge(
        data.hourly_availability, 
        data.visit_pace
    )

    # 2. Retrieve spots and calculate adjusted durations 
    total_durations = 0
    for spot_id in data.selected_spot_ids: 
        spot = get_spot_from_db(spot_id) 
        # Convert "1:30" format to minutes 
        if not spot or not spot.visitDuration: 
            logger.error(f"❌ Spot not found in DB: {spot_id}")
            continue
        duration = parse_visit_duration_to_minutes(spot.visitDuration) 
        # Adjust based on pace (rapide, équilibré, détendu) 
        adjusted = apply_visit_pace_adjustment(duration, data.visit_pace) 
        total_durations += adjusted 

    # 3. Calculate transport penalty 
    # Each spot beyond the first (n_days) costs 30 mins 
    nb_spots = len(data.selected_spot_ids) 
    transport_penalty = 30 * max(0, nb_spots - n_days) 

    # 4. Final time calculations 
    time_used = total_durations + transport_penalty 
    time_remaining_end = time_dispo_total - time_used 

    # 5. Determine Status 
    itinerary = None
    if time_remaining_end < 0: 
        status = "too_many" 
        title = "Trop de lieux sélectionnés!" 
        message = "Le temps estimé dépasse votre disponibilité. Retirez certains lieux pour un parcours réaliste."
        logger.warning(f"❌ Too many spots selected. Time deficit: {abs(time_remaining_end)} minutes") 
    
    elif time_remaining_end >= 120 and not data.force_continue: 
        status = "too_few" 
        title = "Peu de lieux sélectionnés !" 
        message = "Il reste du temps libre: vous pouvez ajouter d'autres lieux."
        logger.info(f"⏰ Too few spots selected. Remaining time: {time_remaining_end} minutes") 
    
    else: 
        # Case "ok" or forced continue: Run optimization 
        logger.info(f"✅ Validation passed. Running optimization with {len(data.selected_spot_ids)} spots...")
        logger.debug(f"📋 Request data: {data}")
        status = "ok" 
        title = None
        message = None
        # This calls the OR-Tools optimization logic
        if data.premium == True:
            optimizationMode = OptimizationMode.PREMIUM
        else:
            optimizationMode = OptimizationMode.FREEMIUM
        itinerary = optimise_travel(
            city=data.destination,
            spots=[get_spot_from_db(spot_id) for spot_id in data.selected_spot_ids],
            travel_dates=data.travel_dates,
            hourly_availability=data.hourly_availability,
            optimization_mode= optimizationMode,
            max_walk_time_per_segment_min=data.user_max_walk_min,
            visit_pace=data.visit_pace,
            companions=data.companions,
        )
        logger.info(f"🎯 Optimization completed for {data.destination}")

    logger.info(f"📊 Validation result: {status} | Time used: {time_used}min | Remaining: {time_remaining_end}min")

    # Convert FinalItineraryOutput object to dictionary if needed
    itinerary_dict = None
    if itinerary is not None:
        if hasattr(itinerary, 'model_dump'):
            itinerary_dict = itinerary.model_dump()
        elif isinstance(itinerary, dict):
            itinerary_dict = itinerary
        else:
            logger.warning(f"Unexpected itinerary type: {type(itinerary)}")
            itinerary_dict = None

    return ValidationResponse(
        status=status,
        title=title,
        message=message,
        time_dispo_total=time_dispo_total,
        time_used=time_used,
        time_remaining_end=time_remaining_end,
        itinerary=itinerary_dict
    ) 
