from fastapi import APIRouter
from typing import Dict, List
from app.models import (
    FinalItineraryOutput,
    DailyItinerary,
    ItineraryStep,
    UserPreferences,
    OptimizationMode,
    OptimizationScores,
)
from app.services.utils import (
    time_str_to_minutes,
    minutes_to_time_str,
    get_adjusted_visit_duration,
    LUNCH_DURATION_MIN,
)
from app.services.distance import get_travel_mode_and_time
from app.services.firestore_service import get_spot_from_db
import random

generation_router = APIRouter(prefix="/generate", tags=["Itinerary Generation"])


@generation_router.post("/final-itinerary", response_model=FinalItineraryOutput)
async def generate_final_itinerary_endpoint(
    ordered_spot_ids_per_day: Dict[str, List[str]],
    preferences: UserPreferences,
    optimization_mode: OptimizationMode = OptimizationMode.FREEMIUM,
):
    final_days_itinerary: List[DailyItinerary] = []
    day_counter = 1

    for date_str, spot_ids_for_day in ordered_spot_ids_per_day.items():
        if not spot_ids_for_day:
            continue
        daily_steps: List[ItineraryStep] = []
        current_time_min = time_str_to_minutes(
            preferences.hourly_availability[date_str][0]
        )

        for i, spot_id in enumerate(spot_ids_for_day):
            spot = await get_spot_from_db(spot_id)
            if not spot:
                continue
            if i > 0:
                prev_spot = await get_spot_from_db(spot_ids_for_day[i - 1])
                if prev_spot:
                    mode, travel_dur = get_travel_mode_and_time(
                        prev_spot, spot, preferences.max_walk_time_per_segment_min
                    )
                    daily_steps.append(
                        ItineraryStep(
                            type="travel",
                            from_spot=prev_spot.id,
                            to_spot=spot.id,
                            mode=mode,
                            duration_min=travel_dur,
                        )
                    )
                    current_time_min += travel_dur

            visit_start = minutes_to_time_str(current_time_min)
            visit_dur = get_adjusted_visit_duration(
                spot.standard_duration_min, preferences.visit_pace
            )
            daily_steps.append(
                ItineraryStep(
                    type="visit", id=spot.id, start=visit_start, duration_min=visit_dur
                )
            )
            current_time_min += visit_dur

            if (
                preferences.lunch_break_required
                and time_str_to_minutes("12:00")
                <= current_time_min
                < time_str_to_minutes("14:00")
                and (i < len(spot_ids_for_day) - 1)
                and current_time_min + LUNCH_DURATION_MIN
                < time_str_to_minutes(preferences.hourly_availability[date_str][1])
            ):
                daily_steps.append(
                    ItineraryStep(
                        type="visit",
                        id=f"lunch_{day_counter-1}",
                        start=minutes_to_time_str(current_time_min),
                        duration_min=LUNCH_DURATION_MIN,
                    )
                )
                current_time_min += LUNCH_DURATION_MIN

        spot_names = []
        for sid in spot_ids_for_day:
            s = await get_spot_from_db(sid)
            if s:
                spot_names.append(s.name)
        llm_summary = f"Enjoy your {preferences.companions.value} trip to {preferences.destination} on {date_str}, focusing on {', '.join(spot_names)}."
        final_days_itinerary.append(
            DailyItinerary(
                day=day_counter,
                date=date_str,
                daily_summary=llm_summary[:150],
                steps=daily_steps,
            )
        )
        day_counter += 1

    opt_scores_obj = None
    if optimization_mode == OptimizationMode.PREMIUM:
        opt_scores_obj = OptimizationScores(
            crowd=random.uniform(70, 95),
            weather=random.uniform(65, 90),
            distance=random.uniform(80, 98),
        )
    return FinalItineraryOutput(days=final_days_itinerary, scores=opt_scores_obj)
