from fastapi import APIRouter, HTTPException
from typing import List
from app.models import (
    UserPreferences,
    TimeGaugeStatus,
    ValidationRequest,
    ValidationResponse,
    Spot,
)
from app.services.utils import (
    time_str_to_minutes,
    get_adjusted_visit_duration,
    LUNCH_DURATION_MIN,
    DEFAULT_TRANSPORT_MOYEN_MIN,
)
from app.services.distance import get_travel_mode_and_time
from app.services.firestore_service import get_spot_from_db
from typing import List

itinerary_router = APIRouter(prefix="/itinerary", tags=["Itinerary Planning"])


@itinerary_router.post("/calculate-time-gauge-for-day", response_model=TimeGaugeStatus)
async def calculate_time_gauge_for_day_endpoint(
    selected_spot_ids: List[str],
    day_date: str,
    preferences: UserPreferences,
):
    if day_date not in preferences.hourly_availability:
        raise HTTPException(
            status_code=400, detail=f"Availability for date {day_date} not provided."
        )

    start_time_str, end_time_str = preferences.hourly_availability[day_date]
    total_day_minutes = time_str_to_minutes(end_time_str) - time_str_to_minutes(
        start_time_str
    )

    time_for_lunch = (
        LUNCH_DURATION_MIN
        if preferences.lunch_break_required
        and time_str_to_minutes(start_time_str) <= time_str_to_minutes("12:00")
        and time_str_to_minutes(end_time_str) >= time_str_to_minutes("14:00")
        else 0
    )

    available_for_visits_and_transport = total_day_minutes - time_for_lunch
    time_spent_min = 0
    fitted_spots_for_day: List[Spot] = []

    current_spots_objects = []
    for spot_id in selected_spot_ids:
        spot = await get_spot_from_db(spot_id)
        if spot:
            current_spots_objects.append(spot)

    for i, spot_obj in enumerate(current_spots_objects):
        adjusted_duration = get_adjusted_visit_duration(
            spot_obj.standard_duration_min, preferences.visit_pace
        )
        transport_to_this_spot_min = 0
        if fitted_spots_for_day:
            prev_spot = fitted_spots_for_day[-1]
            _, transport_to_this_spot_min = get_travel_mode_and_time(
                prev_spot, spot_obj, preferences.max_walk_time_per_segment_min
            )

        potential_time_increase = adjusted_duration + transport_to_this_spot_min
        if (
            time_spent_min + potential_time_increase
            <= available_for_visits_and_transport
        ):
            time_spent_min += potential_time_increase
            fitted_spots_for_day.append(spot_obj)
        else:
            break

    remaining_time = available_for_visits_and_transport - time_spent_min
    return TimeGaugeStatus(
        total_available_time_min=available_for_visits_and_transport,
        time_spent_min=time_spent_min,
        remaining_time_min=remaining_time,
        selected_spots_for_day=fitted_spots_for_day,
        can_add_more=remaining_time > 30,
    )


@itinerary_router.post("/validate-selection", response_model=ValidationResponse)
async def validate_user_selection_endpoint(request_data: ValidationRequest):
    total_allocated_visit_time_min = 0
    for date_str, (
        start_str,
        end_str,
    ) in request_data.user_preferences.hourly_availability.items():
        daily_minutes = time_str_to_minutes(end_str) - time_str_to_minutes(start_str)
        lunch_time_this_day = 0
        if (
            request_data.user_preferences.lunch_break_required
            and time_str_to_minutes(start_str) <= time_str_to_minutes("12:00")
            and time_str_to_minutes(end_str) >= time_str_to_minutes("14:00")
        ):
            lunch_time_this_day = LUNCH_DURATION_MIN
        total_allocated_visit_time_min += daily_minutes - lunch_time_this_day

    nb_lieux = len(request_data.selected_spot_ids)
    nb_jours = len(request_data.user_preferences.travel_dates)
    current_total_duration_min = 0

    for spot_id in request_data.selected_spot_ids:
        spot = await get_spot_from_db(spot_id)
        if spot:
            current_total_duration_min += get_adjusted_visit_duration(
                spot.standard_duration_min, request_data.user_preferences.visit_pace
            )

    estimated_transport_slots = nb_lieux - nb_jours
    if estimated_transport_slots > 0:
        current_total_duration_min += (
            estimated_transport_slots * DEFAULT_TRANSPORT_MOYEN_MIN
        )

    jauge_diff = total_allocated_visit_time_min - current_total_duration_min
    status_code, message = 0, ""
    if jauge_diff < 0:
        status_code = -1
        message = f"Votre sélection dépasse le temps disponible. Alloué: {total_allocated_visit_time_min} min, Prévu: {current_total_duration_min} min. Veuillez retirer des lieux."
    elif jauge_diff >= 120:
        status_code = 1
        message = f"Il vous reste beaucoup de temps ({jauge_diff} min). Alloué: {total_allocated_visit_time_min} min, Prévu: {current_total_duration_min} min. Souhaitez-vous ajouter des visites?"
    else:
        status_code = 0
        message = "Sélection validée. Prêt pour l'optimisation."

    return ValidationResponse(
        message=message,
        jauge_status_code=status_code,
        current_total_duration_min=current_total_duration_min,
        allocated_visit_time_min=total_allocated_visit_time_min,
    )
