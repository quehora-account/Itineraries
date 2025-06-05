from fastapi import APIRouter, HTTPException, Body
from typing import List
from app.models import (
    UserPreferences,
    TimeGaugeStatus,
    ValidationRequest,
    ValidationResponse,
    Spot,
    SpotTiming,
)
from app.services.utils import (
    time_str_to_minutes,
    minutes_to_time_str,
    get_adjusted_visit_duration,
    parse_visit_duration_to_minutes,
    LUNCH_DURATION_MIN,
    DEFAULT_TRANSPORT_MOYEN_MIN,
)
from app.services.distance import get_travel_mode_and_time
from app.services.firestore_service import get_spot_from_db
from typing import List

itinerary_router = APIRouter(prefix="/itinerary", tags=["Itinerary Planning"])


@itinerary_router.post("/select-spots", response_model=TimeGaugeStatus)
async def select_spots(
    preferences: UserPreferences,
    selected_spot_ids: List[str] = Body(default=["71sKTux0pjVafHBlebaE", "AmeCrkZVdM0BYV6t9wNG", "At80BN8aB5xOsOd8zPzn"], description="List of spot IDs to select"),
):
    """
    Select spots based on available time gauge and progressive filling algorithm.
    Automatically determines lunch breaks and calculates optimal spot selection.
    """
    
    # Calculate total available time with automatic lunch break detection
    total_allocated_visit_time_min = 0
    daily_schedules = []  # Track each day's schedule
    
    for date_str, (start_str, end_str) in preferences.hourly_availability.items():
        daily_minutes = time_str_to_minutes(end_str) - time_str_to_minutes(start_str)
        start_time_min = time_str_to_minutes(start_str)
        
        # Automatically determine lunch break for this day
        lunch_time_this_day = 0
        if (start_time_min <= time_str_to_minutes("12:00") and 
            time_str_to_minutes(end_str) >= time_str_to_minutes("14:00")):
            lunch_time_this_day = LUNCH_DURATION_MIN
            
        available_time_this_day = daily_minutes - lunch_time_this_day
        total_allocated_visit_time_min += available_time_this_day
        
        daily_schedules.append({
            'date': date_str,
            'start_time_min': start_time_min,
            'end_time_min': time_str_to_minutes(end_str),
            'available_time_min': available_time_this_day,
            'current_time_min': start_time_min,
            'remaining_time_min': available_time_this_day
        })
    
    nb_jours = len(preferences.travel_dates)
    nb_lieux_initial = len(selected_spot_ids)
    
    # Calculate base transport time that will be deducted from available time
    base_transport_slots = max(0, nb_lieux_initial - nb_jours)
    base_transport_time = base_transport_slots * DEFAULT_TRANSPORT_MOYEN_MIN
    
    # The maximum time available for visits (gauge maximum)
    max_gauge_time = total_allocated_visit_time_min - base_transport_time
    
    if max_gauge_time <= 0:
        return TimeGaugeStatus(
            total_available_time_min=total_allocated_visit_time_min,
            time_spent_min=0,
            remaining_time_min=0,
            selected_spots_for_day=[],
            can_add_more=False,
            spot_timings=[]
        )
    
    # Progressive gauge filling algorithm with timing calculation across multiple days
    selected_spots = []
    spot_timings = []
    current_nb_lieux = 0
    current_day_index = 0
    
    # Define lunch break constants
    LUNCH_START_MIN = time_str_to_minutes("12:00")  # 720 minutes (12:00)
    LUNCH_END_MIN = time_str_to_minutes("14:00")    # 840 minutes (14:00)
    
    for spot_id in selected_spot_ids:
        spot = get_spot_from_db(spot_id)
        if not spot:
            return HTTPException(status_code=404, detail=f"Spot with ID {spot_id} not found")

        if spot.embedding:
            del spot.embedding
            
        # Parse visit duration from spot data
        standard_duration_min = parse_visit_duration_to_minutes(spot.visitDuration)
        print(f"standard_duration_min: {standard_duration_min}")
        
        # Calculate adjusted visit duration for this spot
        visit_duration = get_adjusted_visit_duration(
            standard_duration_min, preferences.visit_pace
        )
        
        transport_time = 0
        if current_nb_lieux >= nb_jours:
            transport_time = DEFAULT_TRANSPORT_MOYEN_MIN
            
        total_spot_time = visit_duration + transport_time
        
        # Find a day that can accommodate this spot
        spot_scheduled = False
        days_tried = 0
        
        while days_tried < len(daily_schedules) and not spot_scheduled:
            current_day = daily_schedules[current_day_index]
            
            # Check if this spot fits in the current day's remaining time
            if total_spot_time <= current_day['remaining_time_min']:
                # Add transport time to current time (arrival at spot)
                current_day['current_time_min'] += transport_time
                
                # Check if we're entering lunch break period and skip it if necessary
                visit_end_time = current_day['current_time_min'] + visit_duration
                
                # If the visit would start during or extend into lunch break, skip to after lunch
                if (current_day['current_time_min'] < LUNCH_END_MIN and visit_end_time > LUNCH_START_MIN):
                    # If we're about to start during lunch or extend into lunch, skip to after lunch
                    if current_day['current_time_min'] < LUNCH_END_MIN:
                        current_day['current_time_min'] = LUNCH_END_MIN
                        
                arrival_time = minutes_to_time_str(current_day['current_time_min'])
                
                # Calculate departure time
                departure_time_min = current_day['current_time_min'] + visit_duration
                departure_time = minutes_to_time_str(departure_time_min)
                
                # Create timing info with proper date
                spot_timing = SpotTiming(
                    spot=spot,
                    date=current_day['date'],
                    arrival_time=arrival_time,
                    departure_time=departure_time,
                    visit_duration_min=visit_duration,
                    transport_time_min=transport_time
                )
                
                selected_spots.append(spot)
                spot_timings.append(spot_timing)
                current_day['remaining_time_min'] -= total_spot_time
                current_nb_lieux += 1
                current_day['current_time_min'] = departure_time_min
                spot_scheduled = True
            else:
                # Try next day
                current_day_index = (current_day_index + 1) % len(daily_schedules)
                days_tried += 1
        
        # If we couldn't schedule this spot on any day, skip it
        if not spot_scheduled:
            continue
    
    # Calculate final metrics
    total_remaining_time = sum(day['remaining_time_min'] for day in daily_schedules)
    time_spent = max_gauge_time - total_remaining_time
    can_add_more = total_remaining_time >= 60  # Can add more if at least 1 hour remaining
    
    return TimeGaugeStatus(
        total_available_time_min=total_allocated_visit_time_min,
        time_spent_min=time_spent,
        remaining_time_min=total_remaining_time,
        can_add_more=can_add_more,
        spot_timings=spot_timings
    )

@itinerary_router.post("/itinerary-validation")
async def itinerary_validation(
    data: TimeGaugeStatus,
):
    remaining_time_min = data.remaining_time_min
    if remaining_time_min < 0:
        return "Votre sélection dépasse le temps disponible. Veuillez retirer un ou plusieurs lieux de visite."
    elif remaining_time_min < 120:
        return "optimisation"
    else:
        return "Il vous reste du temps. Souhaitez-vous ajouter des visites?"