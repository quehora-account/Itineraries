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
    calculate_crowd_score_brut,
    time_str_to_minutes,
    get_adjusted_visit_duration,
    LUNCH_DURATION_MIN,
)
from app.services.weather import get_city_weather_data
from app.services.firestore_service import get_spot_from_db, get_all_spots_from_db
from app.services.distance import get_distance_matrix
from typing import Dict, Union

data_prep_router = APIRouter(prefix="/prepare", tags=["Data Preparation"])


@data_prep_router.post(
    "/get-distance", response_model=Tuple[MatrixTime, MatrixScoreDistance]
) 
def get_distance_endpoint(
    spot_ids: List[str], max_walk_time_per_segment_min: int = 30
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
    "/compute-all-distances", response_model=CityWeatherData
)
def compute_all_distances_endpoint(
    max_walk_time_per_segment_min: int = 30
):
    print(f"Computing all distances with max_walk_time_per_segment_min: {max_walk_time_per_segment_min}")
    spots = get_all_spots_from_db()
    print(f"Found {len(spots)} spots")
    matrix_time, matrix_score_distance = get_distance_matrix(spots, max_walk_time_per_segment_min)
    print(f"Matrix time: {matrix_time}")
    print(f"Matrix score distance: {matrix_score_distance}")
    return matrix_time, matrix_score_distance

@data_prep_router.post(
    "/weather-data", response_model=CityWeatherData
)  
async def prepare_weather_data_endpoint_new(
    city: str,
    travel_dates: List[str],
    daily_hours_range: Tuple[str, str] = ("08:00", "18:00"),
):
    if not travel_dates:
        raise HTTPException(status_code=400, detail="Travel dates must be provided.")
    return get_city_weather_data(
        city, travel_dates, daily_hours_range
    )  


@data_prep_router.post("/crowd-score-brut", response_model=float)
async def calculate_crowd_score_brut_endpoint_new(
    data: CrowdScoreInput,
):  # DataPrepCrowdScoreInput
    return calculate_crowd_score_brut(
        data.popular_time, data.density_index
    )  # util_dp_calc_crowd


@data_prep_router.post(
    "/all-solver-data", response_model=PreparedSolverData
)  # DataPrepPreparedSolverData
async def prepare_all_solver_data_endpoint_new(
    selected_spot_ids: List[str],  # DataPrepList
    preferences: UserPreferences,  # DataPrepUserPreferences
    mode: OptimizationMode = OptimizationMode.FREEMIUM,  # DataPrepOptimizationMode
    weights: Optional[
        SolverInputWeights
    ] = None,  # DataPrepOptional, DataPrepSolverInputWeights
):
    if not selected_spot_ids:
        raise HTTPException(status_code=400, detail="No spots selected.")
    num_vehicles = len(preferences.travel_dates)
    if not (1 <= num_vehicles <= 3):
        raise HTTPException(status_code=400, detail="Days must be 1-3.")

    user_windows_list: List[UserWindow] = []  # DataPrepUserWindow
    cumulative_minute_offset = 0
    for i, date_str in enumerate(preferences.travel_dates):
        if date_str not in preferences.hourly_availability:
            raise HTTPException(
                status_code=400, detail=f"No availability for {date_str}."
            )
        start_str, end_str = preferences.hourly_availability[date_str]
        start_min, end_min = time_str_to_minutes(start_str), time_str_to_minutes(
            end_str
        )  # util_dp_time_to_min
        user_windows_list.append(
            UserWindow(
                vehicle=i,
                start=cumulative_minute_offset + start_min,
                end=cumulative_minute_offset + end_min,
            )
        )
        cumulative_minute_offset += 24 * 60

    solver_vehicles = [
        {"id": f"veh_{i}", "start_index": "depot", "end_index": "depot"}
        for i in range(num_vehicles)
    ]
    solver_weights = weights if weights else SolverInputWeights()
    lunch_indices: Dict[str, str] = {}
    solver_locations: List[Union[SolverSpotInfo, SolverLunchInfo, SolverDepotInfo]] = [
        SolverDepotInfo()
    ]  # DataPrepSolverSpotInfo etc.

    cumulative_offset_day_specific = 0
    for i, date_str in enumerate(preferences.travel_dates):
        start_s, end_s = preferences.hourly_availability[date_str]
        day_start_local, day_end_local = time_str_to_minutes(
            start_s
        ), time_str_to_minutes(end_s)
        lunch_start_w_min, lunch_end_w_max = 720, 840
        if (
            preferences.lunch_break_required
            and day_start_local <= lunch_start_w_min
            and day_end_local >= (lunch_start_w_min + LUNCH_DURATION_MIN)
        ):  # UTIL_DP_LUNCH_DUR
            actual_lunch_start = max(day_start_local, lunch_start_w_min)
            actual_lunch_end = min(day_end_local - LUNCH_DURATION_MIN, lunch_end_w_max)
            if actual_lunch_start <= actual_lunch_end:
                lunch_id = f"lunch_{i}"
                lunch_indices[str(i)] = lunch_id
                lunch_node_time_window_cumulative = [
                    (
                        cumulative_offset_day_specific + actual_lunch_start,
                        cumulative_offset_day_specific + actual_lunch_end,
                    )
                ]
                solver_locations.append(
                    SolverLunchInfo(
                        id=lunch_id,
                        vehicle=i,
                        time_windows=lunch_node_time_window_cumulative,
                        dur=LUNCH_DURATION_MIN,
                    )
                )  # UTIL_DP_LUNCH_DUR
        cumulative_offset_day_specific += 24 * 60

    city_weather = simulate_get_city_weather_data(
        preferences.destination, preferences.travel_dates, ("00:00", "23:00")
    )
    weather_score_hour_cum: Dict[int, float] = {}
    current_cum_min_offset = 0
    for date_str in preferences.travel_dates:
        if date_str in city_weather.weather_by_date:
            for hour_s, hourly_d in city_weather.weather_by_date[
                date_str
            ].hourly_data.items():
                if hourly_d.normalized_weather_score is not None:
                    weather_score_hour_cum[
                        current_cum_min_offset + (int(hour_s) * 60)
                    ] = hourly_d.normalized_weather_score
        current_cum_min_offset += 24 * 60

    # Simplified matrix generation for all nodes (depot, spots, lunches)
    all_node_ids_for_matrices = (
        ["depot"] + selected_spot_ids + list(lunch_indices.values())
    )
    # This is a placeholder. A full implementation would fetch all spots involved, including depot/lunch representations if they have coords.
    # For now, use spot-to-spot and augment.

    # Fetch spot objects for matrix calculation
    db_spot_objects = {sid: await get_spot_from_db(sid) for sid in selected_spot_ids}
    valid_db_spot_objects = [s for s in db_spot_objects.values() if s]

    final_matrix_time_segments = {}
    final_matrix_score_distance_scores = {}

    if valid_db_spot_objects:
        # Calculate spot-to-spot first
        temp_raw_distances = {}
        for i_idx in range(len(valid_db_spot_objects)):
            for j_idx in range(len(valid_db_spot_objects)):
                if i_idx == j_idx:
                    continue
                s_a, s_b = valid_db_spot_objects[i_idx], valid_db_spot_objects[j_idx]
                key = f"{s_a.id}-{s_b.id}"
                mode, time_val = get_travel_mode_and_time(
                    s_a, s_b, preferences.max_walk_time_per_segment_min
                )
                final_matrix_time_segments[key] = TravelSegment(
                    duree=time_val, type=mode
                )
                temp_raw_distances[key] = haversine_distance_km(
                    s_a.latitude, s_a.longitude, s_b.latitude, s_b.longitude
                )

        if temp_raw_distances:
            d_min_val, d_max_val = min(temp_raw_distances.values()), max(
                temp_raw_distances.values()
            )
            for k, dist_v in temp_raw_distances.items():
                final_matrix_score_distance_scores[k] = normalize_score(
                    dist_v, d_min_val, d_max_val
                )

    # Augment for depot and lunch (simplified: 0 cost/time)
    for node_id_outer in all_node_ids_for_matrices:
        for node_id_inner in all_node_ids_for_matrices:
            if node_id_outer == node_id_inner:
                continue
            key = f"{node_id_outer}-{node_id_inner}"
            if (
                key not in final_matrix_time_segments
            ):  # Avoid overwriting existing spot-spot
                # Depot or lunch connections
                if (
                    node_id_outer == "depot"
                    or node_id_inner == "depot"
                    or node_id_outer.startswith("lunch_")
                    or node_id_inner.startswith("lunch_")
                ):
                    final_matrix_time_segments[key] = TravelSegment(
                        duree=0, type=TravelMode.VIRTUAL
                    )
                    # No distance score for virtual by default in example

    # Locations (Spots)
    for spot_id in selected_spot_ids:
        spot = db_spot_objects.get(spot_id)  # Fetched earlier
        if not spot:
            continue
        adj_dur = get_adjusted_visit_duration(
            spot.standard_duration_min, preferences.visit_pace
        )  # util_dp_get_adj_dur
        spot_time_windows_cum: List[Tuple[int, int]] = []
        day_offset_spot = 0
        for _ in range(num_vehicles):
            if spot.open_time_local and spot.close_time_local:
                open_m, close_m = time_str_to_minutes(
                    spot.open_time_local
                ), time_str_to_minutes(spot.close_time_local)
                if close_m > open_m:
                    spot_time_windows_cum.append(
                        (day_offset_spot + open_m, day_offset_spot + close_m)
                    )
            day_offset_spot += 24 * 60

        crowd_hour_data_cum: Dict[int, float] = {}
        if spot.popular_times_hourly and spot.density_index:
            day_offset_crowd = 0
            for day_i in range(num_vehicles):
                day_d_str = preferences.travel_dates[day_i]
                user_s_str, user_e_str = preferences.hourly_availability[day_d_str]
                user_s_min, user_e_min = time_str_to_minutes(
                    user_s_str
                ), time_str_to_minutes(user_e_str)
                for hour_loc, pop_score in spot.popular_times_hourly.items():
                    if user_s_min <= hour_loc * 60 < user_e_min:
                        crowd_s = calculate_crowd_score_brut(
                            pop_score, spot.density_index
                        )
                        crowd_hour_data_cum[day_offset_crowd + (hour_loc * 60)] = (
                            crowd_s
                        )
                day_offset_crowd += 24 * 60
        solver_locations.append(
            SolverSpotInfo(
                id=spot.id,
                dur=adj_dur,
                outdoor=spot.is_outdoor,
                time_windows=spot_time_windows_cum,
                crowdHour=crowd_hour_data_cum if crowd_hour_data_cum else None,
            )
        )

    return PreparedSolverData(
        mode=mode,
        numVehicles=num_vehicles,
        userWindows=user_windows_list,
        vehicles=solver_vehicles,
        weights=solver_weights,
        lunchIndex=lunch_indices if lunch_indices else None,
        weatherScoreHour=weather_score_hour_cum if weather_score_hour_cum else None,
        matrixTime=MatrixTime(segments=final_matrix_time_segments),
        matrixScoreDistance=MatrixScoreDistance(
            scores=final_matrix_score_distance_scores
        ),
        locations=solver_locations,
    )
