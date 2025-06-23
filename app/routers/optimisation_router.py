from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from fastapi import APIRouter, HTTPException
from typing import List, Tuple, Dict, Optional, Any, Union
import math
from datetime import datetime, timedelta

from app.models import (
    OptimizationMode,
    SimpleTimeGauge,
    FinalItineraryOutput,
    DailyItinerary,
    ItineraryStep,
    OptimizationScores,
    PreparedSolverData,
    SolverSpotInfo,
    SolverLunchInfo,
    SolverDepotInfo,
    UserWindow,
    SolverInputWeights,
    MatrixTime,
    MatrixScoreDistance,
    TravelSegment,
    TravelMode,
    LocationType,
)
from app.services.weather import load_city_weather_from_db
from app.services.distance import (
    get_distance_matrix,
)
from app.services.firestore_service import get_spot_from_db
from app.services.utils import (
    get_affluence_score,
    time_str_to_minutes,
    minutes_to_time_str,
    parse_visit_duration_to_minutes,
    client,
    LUNCH_DURATION_MIN,
)

optimisation_router = APIRouter(prefix="/optimisation", tags=["Itinerary Optimisation"])


def prepare_optimization_data(
    spots_data: List[Dict],
    travel_dates: List[str],
    daily_hours_range: Tuple[str, str],
    optimization_mode: OptimizationMode,
    city_weather_data: Optional[Dict] = None,
    distance_matrix: Optional[Dict] = None,
    weights: Optional[Dict] = None,
) -> PreparedSolverData:
    """Prepare all data needed for OR-Tools optimization."""

    if weights is None:
        weights = {"distance": 0.4, "crowd": 0.3, "weather": 0.3}

    num_vehicles = len(travel_dates)
    start_hour, end_hour = daily_hours_range
    start_minutes = time_str_to_minutes(start_hour)
    end_minutes = time_str_to_minutes(end_hour)

    # Create user windows (one per vehicle/day)
    user_windows = []
    for i, date in enumerate(travel_dates):
        day_offset_minutes = i * 24 * 60  # Minutes since day 0
        user_windows.append(
            UserWindow(
                vehicle=i,
                start=start_minutes + day_offset_minutes,
                end=end_minutes + day_offset_minutes,
            )
        )

    # Create locations list: [depot, spots..., lunch nodes...]
    locations = []

    # Add depot (index 0)
    depot = SolverDepotInfo(
        id="depot", dur=0, outdoor=False, time_windows=[(0, 999999)]  # Always available
    )
    locations.append(depot)

    # Add spots
    for spot_data in spots_data:
        spot_duration = parse_visit_duration_to_minutes(
            spot_data.get("visitDuration", "1:00")
        )
        location_type = spot_data.get("locationType", LocationType.INDOOR)

        # Create time windows for this spot based on its opening hours
        spot_time_windows = create_spot_time_windows(
            spot_data, travel_dates, daily_hours_range, spot_duration
        )

        # Prepare crowd data if premium mode
        crowd_hour = None
        if optimization_mode == OptimizationMode.PREMIUM:
            crowd_hour = prepare_crowd_data(spot_data, travel_dates)

        spot_info = SolverSpotInfo(
            id=spot_data["id"],
            dur=spot_duration,
            outdoor=(location_type == LocationType.OUTDOOR),
            time_windows=spot_time_windows,
            crowdHour=crowd_hour,
            ignoreMissingScore=True,
        )
        locations.append(spot_info)

    # Add lunch nodes (one per vehicle)
    lunch_index = {}
    for i in range(num_vehicles):
        lunch_id = f"lunch_{i}"
        day_offset_minutes = i * 24 * 60
        lunch_start = 12 * 60 + day_offset_minutes  # 12:00
        lunch_end = 14 * 60 + day_offset_minutes  # 14:00

        lunch_info = SolverLunchInfo(
            id=lunch_id,
            dur=LUNCH_DURATION_MIN,
            outdoor=False,
            vehicle=i,
            time_windows=[(lunch_start, lunch_end - LUNCH_DURATION_MIN)],
        )
        locations.append(lunch_info)
        lunch_index[str(i)] = lunch_id

    # Prepare weather data if premium mode
    weather_score_hour = None
    if optimization_mode == OptimizationMode.PREMIUM and city_weather_data:
        weather_score_hour = prepare_weather_data(city_weather_data, travel_dates)

    # Convert distance matrix
    matrix_time = MatrixTime(segments={})
    matrix_score_distance = MatrixScoreDistance(scores={})

    if distance_matrix:
        for from_id, to_dict in distance_matrix.items():
            for to_id, data in to_dict.items():
                key = f"{from_id}_{to_id}"
                duration = data.get("duration", 0)
                score = data.get("score", 50.0)

                matrix_time.segments[key] = TravelSegment(
                    duree=duration,
                    type=TravelMode.WALK if duration > 0 else TravelMode.VIRTUAL,
                )
                matrix_score_distance.scores[key] = score

    return PreparedSolverData(
        mode=optimization_mode,
        numVehicles=num_vehicles,
        userWindows=user_windows,
        vehicles=[{"id": i} for i in range(num_vehicles)],
        weights=SolverInputWeights(**weights),
        lunchIndex=lunch_index,
        weatherScoreHour=weather_score_hour,
        matrixTime=matrix_time,
        matrixScoreDistance=matrix_score_distance,
        locations=locations,
    )


def create_spot_time_windows(
    spot_data: Dict,
    travel_dates: List[str],
    daily_hours_range: Tuple[str, str],
    spot_duration: int,
) -> List[Tuple[int, int]]:
    """Create time windows for a spot based on its opening hours and travel dates."""
    time_windows = []

    start_hour, end_hour = daily_hours_range
    daily_start = time_str_to_minutes(start_hour)
    daily_end = time_str_to_minutes(end_hour)

    for day_idx, date in enumerate(travel_dates):
        day_offset_minutes = day_idx * 24 * 60

        # Get spot opening hours for this day
        open_hours = spot_data.get("openHours", [])
        if not open_hours:
            # Default to daily hours if no opening hours specified
            window_start = daily_start + day_offset_minutes
            window_end = daily_end + day_offset_minutes - spot_duration
            if window_end > window_start:
                time_windows.append((window_start, window_end))
        else:
            # Use actual opening hours
            for hours in open_hours:
                spot_open = time_str_to_minutes(hours.get("start", start_hour))
                spot_close = time_str_to_minutes(hours.get("end", end_hour))

                # Intersect with daily availability
                window_start = max(spot_open, daily_start) + day_offset_minutes
                window_end = (
                    min(spot_close, daily_end) + day_offset_minutes - spot_duration
                )

                if window_end > window_start:
                    time_windows.append((window_start, window_end))

    return time_windows if time_windows else [(0, 1)]  # Fallback


def prepare_crowd_data(spot_data: Dict, travel_dates: List[str]) -> Dict[int, float]:
    """Prepare crowd score data for each hour."""
    crowd_hour = {}

    popular_times = spot_data.get("popularTimes", [])
    density = spot_data.get("density", [1.0])
    density_index = density[0] if density else 1.0

    for day_idx in range(len(travel_dates)):
        day_offset_hours = day_idx * 24

        for hour in range(24):
            # Get popular time for this hour (0-23)
            popular_time = 50  # Default
            if hour < len(popular_times):
                day_popular = popular_times[hour]
                if isinstance(day_popular, dict):
                    popular_time = day_popular.get("value", 50)
                else:
                    popular_time = day_popular

            # Calculate affluence score
            affluence_score = get_affluence_score(popular_time, density_index)
            crowd_hour[day_offset_hours + hour] = affluence_score

    return crowd_hour


def prepare_weather_data(
    city_weather_data: Dict, travel_dates: List[str]
) -> Dict[int, float]:
    """Prepare weather score data for each hour."""
    weather_score_hour = {}

    for day_idx, date in enumerate(travel_dates):
        day_offset_hours = day_idx * 24

        weather_for_date = city_weather_data.get("weather_by_date", {}).get(date, {})
        hourly_data = weather_for_date.get("hourly_data", {})

        for hour in range(24):
            hour_key = f"{hour:02d}:00"
            weather_data = hourly_data.get(hour_key, {})

            # Use normalized weather score if available, otherwise calculate
            weather_score = weather_data.get("normalized_weather_score", 50.0)
            weather_score_hour[day_offset_hours + hour] = weather_score

    return weather_score_hour


def round_time_for_score(start_time_minutes: int) -> int:
    """Apply rounding rule for score calculation."""
    minutes = start_time_minutes % 60
    hours = start_time_minutes // 60

    if minutes <= 29:
        return hours
    else:
        return hours + 1


def calculate_visit_score(
    start_time_minutes: int,
    duration_minutes: int,
    score_data: Dict[int, float],
    day_offset_hours: int = 0,
) -> float:
    """Calculate average score over visit duration."""
    start_hour = round_time_for_score(start_time_minutes)
    duration_hours = math.ceil(duration_minutes / 60)

    scores = []
    for hour_offset in range(duration_hours):
        hour_key = start_hour + hour_offset + day_offset_hours
        score = score_data.get(hour_key, 50.0)
        scores.append(score)

    return sum(scores) / len(scores) if scores else 50.0


def create_cost_callback(solver_data: PreparedSolverData):
    """Create cost callback function for OR-Tools solver."""

    def cost_callback(from_index, to_index):
        from_node = solver_data.locations[from_index]
        to_node = solver_data.locations[to_index]

        # Get distance cost
        key = f"{from_node.id}_{to_node.id}"
        distance_score = solver_data.matrixScoreDistance.scores.get(key, 50.0)
        distance_cost = distance_score * solver_data.weights.distance

        total_cost = distance_cost

        # Add crowd cost if premium mode
        if (
            solver_data.mode == OptimizationMode.PREMIUM
            and hasattr(to_node, "crowdHour")
            and to_node.crowdHour
        ):
            # This is simplified - in reality we'd need the actual start time
            # For now, use a representative score
            crowd_score = sum(to_node.crowdHour.values()) / len(to_node.crowdHour)
            crowd_cost = crowd_score * solver_data.weights.crowd
            total_cost += crowd_cost

        # Add weather cost if premium mode and outdoor
        if (
            solver_data.mode == OptimizationMode.PREMIUM
            and to_node.outdoor
            and solver_data.weatherScoreHour
        ):
            # This is simplified - in reality we'd need the actual start time
            weather_score = sum(solver_data.weatherScoreHour.values()) / len(
                solver_data.weatherScoreHour
            )
            weather_cost = weather_score * solver_data.weights.weather
            total_cost += weather_cost

        return int(total_cost * 100)  # Scale for integer arithmetic

    return cost_callback


def create_time_callback(solver_data: PreparedSolverData):
    """Create time callback function for OR-Tools solver."""

    def time_callback(from_index, to_index):
        from_node = solver_data.locations[from_index]
        to_node = solver_data.locations[to_index]

        # Get travel time
        key = f"{from_node.id}_{to_node.id}"
        travel_time = solver_data.matrixTime.segments.get(
            key, TravelSegment(duree=0, type=TravelMode.VIRTUAL)
        ).duree

        # Add service time of destination node
        service_time = to_node.dur

        return travel_time + service_time

    return time_callback


def solve_vrp(solver_data: PreparedSolverData) -> Optional[Dict]:
    """Solve the Vehicle Routing Problem using OR-Tools."""

    # Create the routing index manager
    manager = pywrapcp.RoutingIndexManager(
        len(solver_data.locations), solver_data.numVehicles, 0  # depot index
    )

    # Create routing model
    routing = pywrapcp.RoutingModel(manager)

    # Create cost callback
    cost_callback = create_cost_callback(solver_data)
    cost_callback_index = routing.RegisterUnaryTransitCallback(cost_callback)
    routing.SetArcCostEvaluatorOfAllVehicles(cost_callback_index)

    # Create time callback
    time_callback = create_time_callback(solver_data)
    time_callback_index = routing.RegisterUnaryTransitCallback(time_callback)

    # Add time dimension
    routing.AddDimension(
        time_callback_index,
        0,  # no slack
        3000,  # max time per vehicle (50 hours)
        False,  # Don't force start cumul to zero
        "Time",
    )
    time_dimension = routing.GetDimensionOrDie("Time")

    # Add time windows constraints
    for location_idx, location in enumerate(solver_data.locations):
        if location.time_windows:
            for start_time, end_time in location.time_windows:
                index = manager.NodeToIndex(location_idx)
                time_dimension.CumulVar(index).SetRange(start_time, end_time)

    # Add vehicle time windows
    for vehicle_id in range(solver_data.numVehicles):
        user_window = solver_data.userWindows[vehicle_id]
        start_index = routing.Start(vehicle_id)
        end_index = routing.End(vehicle_id)

        time_dimension.CumulVar(start_index).SetRange(
            user_window.start, user_window.end
        )
        time_dimension.CumulVar(end_index).SetRange(user_window.start, user_window.end)

    # Add lunch constraints (each lunch node must be visited by its assigned vehicle)
    if solver_data.lunchIndex:
        for vehicle_id, lunch_id in solver_data.lunchIndex.items():
            # Find lunch node index
            lunch_index = None
            for idx, location in enumerate(solver_data.locations):
                if location.id == lunch_id:
                    lunch_index = idx
                    break

            if lunch_index is not None:
                routing.AddVariableMinimizedByFinalizer(
                    time_dimension.CumulVar(manager.NodeToIndex(lunch_index))
                )
                # Force this lunch to be on the correct vehicle
                routing.VehicleVar(manager.NodeToIndex(lunch_index)).SetValue(
                    int(vehicle_id)
                )

    # Set search parameters
    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = (
        routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    )
    search_parameters.time_limit.seconds = 2  # 2 second timeout

    # Solve
    solution = routing.SolveWithParameters(search_parameters)

    if solution:
        return extract_solution(manager, routing, solution, solver_data)
    else:
        return None


def extract_solution(
    manager, routing, solution, solver_data: PreparedSolverData
) -> Dict:
    """Extract solution from OR-Tools solver."""

    solution_data = {
        "total_cost": solution.ObjectiveValue(),
        "routes": [],
        "success": True,
    }

    time_dimension = routing.GetDimensionOrDie("Time")

    for vehicle_id in range(solver_data.numVehicles):
        route = {"vehicle_id": vehicle_id, "steps": []}

        index = routing.Start(vehicle_id)
        while not routing.IsEnd(index):
            node_index = manager.IndexToNode(index)
            location = solver_data.locations[node_index]

            time_var = time_dimension.CumulVar(index)
            start_time = solution.Value(time_var)

            route["steps"].append(
                {
                    "location_id": location.id,
                    "location_type": type(location).__name__,
                    "start_time": start_time,
                    "duration": location.dur,
                    "node_index": node_index,
                }
            )

            index = solution.Value(routing.NextVar(index))

        solution_data["routes"].append(route)

    return solution_data


def calculate_final_scores(
    solution_data: Dict, solver_data: PreparedSolverData, travel_dates: List[str]
) -> OptimizationScores:
    """Calculate final optimization scores."""

    total_distance_cost = 0
    total_crowd_cost = 0
    total_weather_cost = 0
    total_segments = 0

    for route in solution_data["routes"]:
        steps = route["steps"]
        day_idx = route["vehicle_id"]
        day_offset_hours = day_idx * 24

        for i in range(len(steps) - 1):
            current_step = steps[i]
            next_step = steps[i + 1]

            # Distance cost
            from_id = current_step["location_id"]
            to_id = next_step["location_id"]
            key = f"{from_id}_{to_id}"
            distance_score = solver_data.matrixScoreDistance.scores.get(key, 50.0)
            total_distance_cost += distance_score

            # Crowd cost (premium only)
            if solver_data.mode == OptimizationMode.PREMIUM:
                next_location = None
                for loc in solver_data.locations:
                    if loc.id == to_id:
                        next_location = loc
                        break

                if (
                    next_location
                    and hasattr(next_location, "crowdHour")
                    and next_location.crowdHour
                ):
                    crowd_score = calculate_visit_score(
                        next_step["start_time"],
                        next_step["duration"],
                        next_location.crowdHour,
                        day_offset_hours,
                    )
                    total_crowd_cost += crowd_score

            # Weather cost (premium & outdoor only)
            if (
                solver_data.mode == OptimizationMode.PREMIUM
                and solver_data.weatherScoreHour
            ):
                next_location = None
                for loc in solver_data.locations:
                    if loc.id == to_id:
                        next_location = loc
                        break

                if next_location and next_location.outdoor:
                    weather_score = calculate_visit_score(
                        next_step["start_time"],
                        next_step["duration"],
                        solver_data.weatherScoreHour,
                        day_offset_hours,
                    )
                    total_weather_cost += weather_score

            total_segments += 1

    # Calculate average scores and convert to 0-100 scale
    distance_score = (
        100 - (total_distance_cost / total_segments) if total_segments > 0 else 100
    )
    crowd_score = (
        100 - (total_crowd_cost / total_segments)
        if total_segments > 0 and solver_data.mode == OptimizationMode.PREMIUM
        else None
    )
    weather_score = (
        100 - (total_weather_cost / total_segments)
        if total_segments > 0 and solver_data.mode == OptimizationMode.PREMIUM
        else None
    )

    return OptimizationScores(
        distance=max(0, min(100, distance_score)),
        crowd=max(0, min(100, crowd_score)) if crowd_score is not None else None,
        weather=max(0, min(100, weather_score)) if weather_score is not None else None,
    )


def generate_daily_description(
    route: Dict,
    spots_data: List[Dict],
    date: str,
    city: str,
    companions: str = "voyageurs",
) -> str:
    """Generate a brief description for a daily itinerary using LLM."""

    # Prepare data for the prompt
    visits = []
    for step in route["steps"]:
        if step["location_type"] == "SolverSpotInfo":
            # Find spot data
            spot_data = None
            for spot in spots_data:
                if spot["id"] == step["location_id"]:
                    spot_data = spot
                    break

            if spot_data:
                start_time = minutes_to_time_str(step["start_time"] % (24 * 60))
                end_time = minutes_to_time_str(
                    (step["start_time"] + step["duration"]) % (24 * 60)
                )
                visits.append(
                    {
                        "name": spot_data["name"],
                        "start": start_time,
                        "end": end_time,
                        "type": spot_data.get("type", "Visite"),
                    }
                )

    if not visits:
        return f"Journée libre à {city}"

    # Create prompt for LLM
    visits_text = "\n".join(
        [
            f"{i+1}) {visit['name']} – {visit['start']}–{visit['end']} ({visit['type']})"
            for i, visit in enumerate(visits)
        ]
    )

    prompt = f"""Rôle : guide de voyage.
Données :
- Date : {date}
- Ville : {city}
- Voyageurs : {companions}
- Itinéraire jour :
{visits_text}

Tâche : rédige un paragraphe de moins de 150 caractères décrivant l'esprit de cette journée."""

    try:
        response = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=100,
            temperature=0.7,
        )
        description = response.choices[0].message.content.strip()
        return description[:150]  # Ensure limit
    except Exception:
        return f"Découverte de {city} avec {len(visits)} visites prévues"


def convert_solution_to_itinerary(
    solution_data: Dict,
    solver_data: PreparedSolverData,
    travel_dates: List[str],
    spots_data: List[Dict],
    city: str,
    companions: str = "voyageurs",
) -> List[DailyItinerary]:
    """Convert solver solution to final itinerary format."""

    daily_itineraries = []

    for route in solution_data["routes"]:
        day_idx = route["vehicle_id"]
        date = travel_dates[day_idx]
        steps = []

        previous_step = None
        for step_data in route["steps"]:
            location_id = step_data["location_id"]

            # Skip depot
            if location_id == "depot":
                previous_step = step_data
                continue

            # Add transport step if needed
            if previous_step and previous_step["location_id"] != "depot":
                transport_key = f"{previous_step['location_id']}_{location_id}"
                transport_duration = solver_data.matrixTime.segments.get(
                    transport_key, TravelSegment(duree=0, type=TravelMode.VIRTUAL)
                ).duree

                if transport_duration > 0:
                    transport_step = ItineraryStep(
                        type="transport",
                        from_spot=previous_step["location_id"],
                        to_spot=location_id,
                        duration_min=transport_duration,
                        mode=TravelMode.WALK,
                    )
                    steps.append(transport_step)

            # Add visit step
            start_time = minutes_to_time_str(step_data["start_time"] % (24 * 60))

            if location_id.startswith("lunch_"):
                visit_step = ItineraryStep(
                    type="pause",
                    id=location_id,
                    start=start_time,
                    duration_min=step_data["duration"],
                )
            else:
                visit_step = ItineraryStep(
                    type="visite",
                    id=location_id,
                    start=start_time,
                    duration_min=step_data["duration"],
                )

            steps.append(visit_step)
            previous_step = step_data

        # Generate daily description
        daily_summary = generate_daily_description(
            route, spots_data, date, city, companions
        )

        daily_itinerary = DailyItinerary(
            day=day_idx + 1, date=date, daily_summary=daily_summary, steps=steps
        )
        daily_itineraries.append(daily_itinerary)

    return daily_itineraries


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
        raise HTTPException(
            status_code=400,
            detail="Daily hours range must be provided as a tuple of two strings (start_time, end_time).",
        )

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
    distance_matrix = await get_distance_matrix(
        spots_data, max_walk_time_per_segment_min
    )

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
        companions="voyageurs",  # Could be extracted from request if available
    )

    return FinalItineraryOutput(days=daily_itineraries, scores=scores)
