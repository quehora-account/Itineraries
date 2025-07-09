from typing import List, Tuple, Dict, Optional, Union, Any
from datetime import datetime, timedelta
from app.models import (
    MatrixTime,
    Spot,
    SimplifiedSpot,
    OptimizationMode,
    MatrixScoreDistance,
    UserWindow,
    SolverInputWeights,
    SolverSpotInfo,
    SolverLunchInfo,
    SolverDepotInfo,
    PreparedSolverData,
    ItineraryStep,
    DailyItinerary,
    OptimizationScores,
    FinalItineraryOutput,
    CityWeatherData,
    VisitPace,
    LocationType,
    DailySummary,
    StepScores,
)
from app.enums import TravelMode
from app.services.firestore_service import (
    load_distance_matrix_from_db,
    load_optimisation_weights_from_db,
)
from app.services.weather import get_city_weather_data
from ortools.constraint_solver import routing_enums_pb2
from ortools.constraint_solver import pywrapcp
import logging
from app.services.utils import client

logger = logging.getLogger(__name__)


def filter_distance_matrix(
    distance_matrix: MatrixTime, spots_data: List[SimplifiedSpot]
) -> MatrixTime:
    """Filter the distance matrix to only include spots that are in the spots_data list"""
    ids = [spot.id for spot in spots_data]
    segments = distance_matrix.segments

    new_segments = {}
    for key, segment in segments.items():
        keys = key.split("-")
        start_id = keys[0]
        end_id = keys[1]
        if start_id in ids and end_id in ids:
            new_segments[key] = segment

    filtered_matrix = MatrixTime(segments=new_segments)
    return filtered_matrix


def get_distance_matrix_score(distance_matrix: MatrixTime) -> MatrixScoreDistance:
    """
    Normalize all distances in the MatrixTime to a score between 0 and 100.
    score_segment = ((Dmax - Dij) / (Dmax - Dmin)) * 100
    Returns a MatrixScoreDistance with the same keys as the input segments.
    """
    # Extract all distances
    distances = [segment.distance for segment in distance_matrix.segments.values()]
    if not distances:
        return MatrixScoreDistance(scores={})

    dmin = min(distances)
    dmax = max(distances)

    # Avoid division by zero if all distances are the same
    if dmax == dmin:
        # All scores are 100 if all distances are the same (best possible)
        scores = {key: 100.0 for key in distance_matrix.segments.keys()}
        return MatrixScoreDistance(scores=scores)

    scores = {}
    for key, segment in distance_matrix.segments.items():
        score = ((dmax - segment.distance) / (dmax - dmin)) * 100
        scores[key] = score

    return MatrixScoreDistance(scores=scores)


def calculate_crowd_hour_for_spot(spot: Spot, day_index: int) -> Dict[int, float]:
    """
    Calculate the crowdHour dictionary for a given spot and day.
    Args:
        spot: The spot object, expected to have 'popularTimes' (list of dicts) and 'density' (list of floats, 12 values for 8am-8pm).
        day_index: Integer (0=Monday, 6=Sunday) to select the correct day from popularTimes.
    Returns:
        crowdHour: dict mapping hour (int, 8-19) to crowd score (float)
    """
    if not spot.popularTimes or day_index >= len(spot.popularTimes):
        return {hour: 0.0 for hour in range(8, 20)}

    popular_times_day = spot.popularTimes[day_index]  # dict: hour(str) -> score(float)
    density = spot.density  # list of 12 floats (for 8am-8pm)
    crowdHour = {}

    for i, hour in enumerate(range(8, 20)):
        hour_str = str(hour)
        popular_time = float(popular_times_day.get(hour_str, 0.0))
        # density_index is expected to be 1-5, but density array may have floats
        density_index = float(density[i]) if i < len(density) else 1.0
        coefficient = density_index * 0.2
        score_brut = popular_time * coefficient
        crowdHour[hour] = score_brut

    return crowdHour


# === USER AND SPOT DATA MANAGEMENT ===


def process_user_windows(
    travel_dates: List[str], daily_hours_range: Tuple[str, str]
) -> List[UserWindow]:
    """
    Convert daily hours range to user windows for each travel day.
    Returns array of UserWindow objects with day, start, and end in minutes UTC.
    """
    user_windows = []

    for i, date in enumerate(travel_dates):
        # Convert time strings to minutes from midnight
        start_time = daily_hours_range[0]  # e.g., "09:00"
        end_time = daily_hours_range[1]  # e.g., "18:00"

        start_hour, start_min = map(int, start_time.split(":"))
        end_hour, end_min = map(int, end_time.split(":"))

        start_minutes = start_hour * 60 + start_min
        end_minutes = end_hour * 60 + end_min

        user_windows.append(UserWindow(vehicle=i, start=start_minutes, end=end_minutes))

    return user_windows


def determine_lunch_requirements(user_windows: List[UserWindow]) -> Dict[int, bool]:
    """
    Determine if lunch break is needed for each day based on user windows.
    Lunch is needed if the user window covers 12:00-14:00 entirely.
    """
    lunch_requirements = {}

    for window in user_windows:
        # 12:00 = 720 minutes, 14:00 = 840 minutes
        needs_lunch = window.start <= 720 and window.end >= 840
        lunch_requirements[window.vehicle] = needs_lunch

    return lunch_requirements


def adjust_visit_duration(standard_duration_min: int, pace: VisitPace) -> int:
    """Adjust visit duration based on user's pace preference."""
    if pace == VisitPace.RELAXED:
        return int(standard_duration_min * 1.3)
    elif pace == VisitPace.FAST:
        return int(standard_duration_min * 0.7)
    else:  # BALANCED
        return standard_duration_min


def process_spot_info(
    spots: List[Spot], travel_dates: List[str], pace: VisitPace = VisitPace.BALANCED
) -> List[SolverSpotInfo]:
    """
    Process detailed information for each POI to create SolverSpotInfo objects.
    """
    solver_spots = []

    for spot in spots:
        try:
            # Parse visit duration (assuming format like "2h30", "90min", "2h", "120")
            duration_str = spot.visitDuration.lower().strip()

            if "h" in duration_str:
                parts = duration_str.split("h")
                hours = int(parts[0])
                minutes = int(parts[1]) if len(parts) > 1 and parts[1] else 0
                duration_min = hours * 60 + minutes
            elif "min" in duration_str:
                duration_min = int(duration_str.replace("min", ""))
            elif duration_str.isdigit():
                duration_min = int(duration_str)  # Assume minutes
            else:
                duration_min = 60  # Default 1 hour

            # Ensure minimum duration
            duration_min = max(15, duration_min)

            # Adjust duration based on pace
            adjusted_duration = adjust_visit_duration(duration_min, pace)

            # Create time windows based on opening hours
            time_windows = []
            if spot.openHours:
                for open_hour in spot.openHours:
                    for hours in open_hour.hours:
                        try:
                            start_hour, start_min = map(int, hours.start.split(":"))
                            end_hour, end_min = map(int, hours.end.split(":"))

                            start_minutes = start_hour * 60 + start_min
                            end_minutes = end_hour * 60 + end_min

                            # Ensure visit can complete before closing
                            latest_start = end_minutes - adjusted_duration
                            if latest_start > start_minutes:
                                time_windows.append((start_minutes, latest_start))
                        except (ValueError, IndexError):
                            logger.warning(
                                f"Invalid opening hours format for spot {spot.id}"
                            )
                            continue

            # If no time windows, assume always open during day
            if not time_windows:
                time_windows = [(480, 1200)]  # 8:00 to 20:00

            # Calculate crowd hours for the first day (can be extended for multiple days)
            crowd_hour = calculate_crowd_hour_for_spot(spot, 0)

            # Determine if spot is outdoor
            is_outdoor = hasattr(spot, "locationType") and (
                spot.locationType == LocationType.OUTDOOR
                or spot.locationType == LocationType.MIXED
            )

            solver_spots.append(
                SolverSpotInfo(
                    id=spot.id,
                    dur=adjusted_duration,
                    outdoor=is_outdoor,
                    time_windows=time_windows,
                    crowdHour=crowd_hour,
                    ignoreMissingScore=True,
                )
            )
        except Exception as e:
            logger.warning(f"Error processing spot {spot.id}: {e}")
            # Use default values for problematic spots
            solver_spots.append(
                SolverSpotInfo(
                    id=spot.id,
                    dur=60,  # Default 1 hour
                    outdoor=False,
                    time_windows=[(480, 1200)],  # 8:00 to 20:00
                    crowdHour={},
                    ignoreMissingScore=True,
                )
            )

    return solver_spots


def create_lunch_info(lunch_requirements: Dict[int, bool]) -> List[SolverLunchInfo]:
    """Create lunch break information for days that need it."""
    lunch_infos = []

    for vehicle, needs_lunch in lunch_requirements.items():
        if needs_lunch:
            lunch_infos.append(
                SolverLunchInfo(
                    id=f"lunch_day_{vehicle}",
                    dur=90,  # 1.5 hours
                    outdoor=False,
                    vehicle=vehicle,
                    time_windows=[(720, 840)],  # 12:00 to 14:00 start time
                )
            )

    return lunch_infos


def create_depot_info() -> List[SolverDepotInfo]:
    """Create depot information - single depot for all vehicles."""
    return [
        SolverDepotInfo(
            id="depot",
            dur=0,
            outdoor=False,
            time_windows=[(0, 1440)],  # Full day available
        )
    ]


def extract_weather_score_hours(
    weather_data: CityWeatherData,
    travel_dates: List[str],
    daily_hours_range: Tuple[str, str],
) -> Dict[int, float]:
    """
    Extract hourly weather scores for the optimization period.
    Returns dict mapping hour (in minutes from midnight) to normalized weather score.
    """
    weather_scores = {}

    start_hour = int(daily_hours_range[0].split(":")[0])
    end_hour = int(daily_hours_range[1].split(":")[0])

    for date in travel_dates:
        if date in weather_data.weather_by_date:
            date_weather = weather_data.weather_by_date[date]

            for hour in range(start_hour, end_hour + 1):
                hour_str = str(hour)
                if hour_str in date_weather.hourly_data:
                    hour_data = date_weather.hourly_data[hour_str]
                    # Use normalized weather score (0-100, higher is better)
                    score = hour_data.normalized_weather_score or 50.0
                    weather_scores[hour * 60] = score  # Convert to minutes

    return weather_scores


# === OR-TOOLS SOLVER INTEGRATION ===


def create_distance_callback(
    matrix_score_distance: MatrixScoreDistance,
    locations: List[Union[SolverSpotInfo, SolverLunchInfo, SolverDepotInfo]],
) -> callable:
    """Create distance callback function for OR-Tools solver."""

    def distance_callback(from_index, to_index):
        from_id = locations[from_index].id
        to_id = locations[to_index].id

        # Virtual transitions (depot connections) have zero cost
        if from_id.startswith("depot") or to_id.startswith("depot"):
            return 0

        # Look up distance score
        key = f"{from_id}-{to_id}"
        if key in matrix_score_distance.scores:
            # Convert to cost (100 - score) and scale for solver
            cost = int((100 - matrix_score_distance.scores[key]) * 100)
            return cost

        return 10000  # High penalty for missing distances

    return distance_callback


def create_cost_callback(
    matrix_score_distance: MatrixScoreDistance,
    locations: List[Union[SolverSpotInfo, SolverLunchInfo, SolverDepotInfo]],
    weights: SolverInputWeights,
    mode: OptimizationMode,
    weather_scores: Dict[int, float],
) -> callable:
    """Create comprehensive cost callback incorporating all factors."""

    def cost_callback(from_index, to_index):
        from_id = locations[from_index].id
        to_id = locations[to_index].id

        # Virtual transitions (depot connections) have zero cost
        if from_id.startswith("depot") or to_id.startswith("depot"):
            return 0

        # For simplicity, use only distance component for now
        # Distance component (always applied)
        key = f"{from_id}-{to_id}"
        if key in matrix_score_distance.scores:
            distance_cost = (100 - matrix_score_distance.scores[key]) * weights.distance
            return int(distance_cost * 10)  # Scale for solver

        # High penalty for missing distances
        return 1000

    return cost_callback


def round_visit_time(visit_start_minutes: int) -> int:
    """Round visit start time according to the rule: 00-29 down, 30-59 up."""
    minutes = visit_start_minutes % 60
    if minutes < 30:
        return visit_start_minutes - minutes
    else:
        return visit_start_minutes + (60 - minutes)


def solve_optimization_problem(
    prepared_data: PreparedSolverData,
) -> Optional[Dict[str, Any]]:
    """
    Solve the optimization problem using OR-Tools VRPTW solver.
    """
    try:
        # Create the routing index manager
        num_vehicles = prepared_data.numVehicles
        num_locations = len(prepared_data.locations)

        # Use single depot configuration for simplicity
        depot = 0  # Use first location as depot

        manager = pywrapcp.RoutingIndexManager(num_locations, num_vehicles, depot)

        # Create routing model
        routing = pywrapcp.RoutingModel(manager)

        # Create simplified distance callback
        def distance_callback(from_index, to_index):
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)

            from_location = prepared_data.locations[from_node]
            to_location = prepared_data.locations[to_node]

            # Zero cost for depot transitions
            if from_location.id.startswith("depot") or to_location.id.startswith(
                "depot"
            ):
                return 0

            # Look up distance score between spots
            key = f"{from_location.id}-{to_location.id}"
            if key in prepared_data.matrixScoreDistance.scores:
                # Convert score to cost (invert and scale)
                score = prepared_data.matrixScoreDistance.scores[key]
                cost = int((100 - score) * 10)  # Scale for better solver precision
                return max(1, cost)  # Ensure positive cost

            # High penalty for missing distances
            return 1000

        distance_callback_index = routing.RegisterTransitCallback(distance_callback)
        routing.SetArcCostEvaluatorOfAllVehicles(distance_callback_index)

        # Add time dimension with service times + travel times
        def time_callback(from_index, to_index):
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)

            from_location = prepared_data.locations[from_node]
            to_location = prepared_data.locations[to_node]

            # Travel time from origin to destination
            travel_time = 0
            if not from_location.id.startswith(
                "depot"
            ) and not to_location.id.startswith("depot"):
                # Look up travel time in distance matrix
                key = f"{from_location.id}-{to_location.id}"
                if key in prepared_data.matrixTime.segments:
                    travel_time = int(
                        prepared_data.matrixTime.segments[key].duree
                    )  # duree is in minutes
                else:
                    # Default travel time if not found
                    travel_time = 15  # 15 minutes default

            # Service time at destination
            service_time = to_location.dur

            # Total time = travel time + service time
            return travel_time + service_time

        time_callback_index = routing.RegisterTransitCallback(time_callback)

        # Add time windows
        time_dimension_name = "Time"
        routing.AddDimension(
            time_callback_index,
            30,  # slack_max (30 minutes buffer)
            1440,  # vehicle maximum (24 hours)
            False,  # Don't force start cumul to zero
            time_dimension_name,
        )

        time_dimension = routing.GetDimensionOrDie(time_dimension_name)

        # Set vehicle time windows
        for vehicle_id in range(num_vehicles):
            if vehicle_id < len(prepared_data.userWindows):
                window = prepared_data.userWindows[vehicle_id]
                start_index = routing.Start(vehicle_id)
                end_index = routing.End(vehicle_id)

                # Set time windows for vehicle start and end
                time_dimension.CumulVar(start_index).SetRange(
                    window.start, window.start + 60
                )
                time_dimension.CumulVar(end_index).SetRange(window.start, window.end)

        # Set location time windows (simplified - use first time window only)
        for location_idx, location in enumerate(prepared_data.locations):
            if location.time_windows and not location.id.startswith("depot"):
                index = manager.NodeToIndex(location_idx)
                if index >= 0:  # Valid index
                    # Use the first time window for simplicity
                    start_time, end_time = location.time_windows[0]
                    time_dimension.CumulVar(index).SetRange(start_time, end_time)

        # Add disjunction for optional visits (spots can be skipped if infeasible)
        penalty = 1000
        for location_idx, location in enumerate(prepared_data.locations):
            if not location.id.startswith("depot") and not location.id.startswith(
                "lunch"
            ):
                index = manager.NodeToIndex(location_idx)
                if index >= 0:
                    routing.AddDisjunction([index], penalty)

        # Add vehicle assignment constraints for lunch breaks
        for location_idx, location in enumerate(prepared_data.locations):
            if location.id.startswith("lunch_day_"):
                try:
                    vehicle_id = int(location.id.split("_")[-1])
                    if vehicle_id < num_vehicles:
                        index = manager.NodeToIndex(location_idx)
                        if index >= 0:
                            # Force this lunch to only be served by the correct vehicle
                            for v in range(num_vehicles):
                                if v != vehicle_id:
                                    routing.VehicleVar(index).RemoveValue(v)
                except (ValueError, IndexError):
                    logger.warning(f"Invalid lunch ID format: {location.id}")

        # Configure search parameters
        search_parameters = pywrapcp.DefaultRoutingSearchParameters()
        search_parameters.first_solution_strategy = (
            routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
        )
        search_parameters.local_search_metaheuristic = (
            routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
        )
        search_parameters.time_limit.seconds = 5
        search_parameters.log_search = True

        # Solve the problem
        logger.info("Starting OR-Tools solver...")
        solution = routing.SolveWithParameters(search_parameters)

        if solution:
            logger.info(f"Solution found with status: {routing.status()}")
            return extract_solution_data(manager, routing, solution, prepared_data)
        else:
            logger.warning(f"No solution found. Status: {routing.status()}")
            return None

    except Exception as e:
        logger.error(f"Error solving optimization problem: {e}")
        import traceback

        logger.error(f"Traceback: {traceback.format_exc()}")
        return None


def extract_solution_data(
    manager: pywrapcp.RoutingIndexManager,
    routing: pywrapcp.RoutingModel,
    solution: pywrapcp.Assignment,
    prepared_data: PreparedSolverData,
) -> Dict[str, Any]:
    """Extract solution data from OR-Tools solver result."""
    routes = []
    total_cost = 0

    time_dimension = routing.GetDimensionOrDie("Time")

    for vehicle_id in range(prepared_data.numVehicles):
        route = []
        index = routing.Start(vehicle_id)
        route_cost = 0

        while not routing.IsEnd(index):
            node_index = manager.IndexToNode(index)
            location = prepared_data.locations[node_index]

            time_var = time_dimension.CumulVar(index)
            start_time = solution.Value(time_var)

            route.append(
                {
                    "location_id": location.id,
                    "start_time": start_time,
                    "duration": location.dur,
                    "node_index": node_index,
                }
            )

            previous_index = index
            index = solution.Value(routing.NextVar(index))
            route_cost += routing.GetArcCostForVehicle(
                previous_index, index, vehicle_id
            )

        routes.append({"vehicle_id": vehicle_id, "route": route, "cost": route_cost})
        total_cost += route_cost

    return {"routes": routes, "total_cost": total_cost, "status": "optimal"}


# === OUTPUT AND RELEVANCE SCORING ===


def calculate_individual_scores(
    solution_data: Dict[str, Any], prepared_data: PreparedSolverData
) -> OptimizationScores:
    """Calculate individual criterion scores (distance, crowd, weather)."""

    # For now, return placeholder scores
    # In a full implementation, these would be calculated from the actual solution
    total_distance_cost = 0
    total_crowd_cost = 0
    total_weather_cost = 0

    # Calculate based on actual routes
    for route in solution_data["routes"]:
        for step in route["route"]:
            location_id = step["location_id"]
            # Add cost calculations based on actual solution

    # Convert costs to scores (100 - cost)
    distance_score = max(0, 100 - (total_distance_cost / 100))
    crowd_score = (
        max(0, 100 - (total_crowd_cost / 100))
        if prepared_data.mode == OptimizationMode.PREMIUM
        else None
    )
    weather_score = (
        max(0, 100 - (total_weather_cost / 100))
        if prepared_data.mode == OptimizationMode.PREMIUM
        else None
    )

    return OptimizationScores(
        distance=distance_score, crowd=crowd_score, weather=weather_score
    )


def calculate_relevance_score(
    scores: OptimizationScores, weights: SolverInputWeights
) -> float:
    """Calculate overall relevance score as weighted average."""
    total_score = 0.0
    total_weight = 0.0

    if scores.distance is not None:
        total_score += scores.distance * weights.distance
        total_weight += weights.distance

    if scores.crowd is not None:
        total_score += scores.crowd * weights.crowd
        total_weight += weights.crowd

    if scores.weather is not None:
        total_score += scores.weather * weights.weather
        total_weight += weights.weather

    return total_score / total_weight if total_weight > 0 else 0.0


def format_solution_output(
    solution_data: Dict[str, Any],
    prepared_data: PreparedSolverData,
    travel_dates: List[str],
    scores: OptimizationScores,
    relevance_score: float,
) -> FinalItineraryOutput:
    """Format the solution into final JSON output."""

    daily_itineraries = []

    for route in solution_data["routes"]:
        vehicle_id = route["vehicle_id"]
        if vehicle_id < len(travel_dates):
            date = travel_dates[vehicle_id]

            steps = []
            route_steps = route["route"]

            for i, step_data in enumerate(route_steps):
                location_id = step_data["location_id"]
                start_time = step_data["start_time"]
                duration = step_data["duration"]

                # Convert minutes to HH:MM format
                hours = start_time // 60
                minutes = start_time % 60
                start_time_str = f"{hours:02d}:{minutes:02d}"

                if not location_id.startswith("depot"):
                    step_type = "lunch" if location_id.startswith("lunch") else "visit"

                    # Determine from/to for visit and lunch steps
                    from_location = None
                    to_location = None

                    if i > 0:
                        # Get previous non-depot location
                        prev_step = route_steps[i - 1]
                        if not prev_step["location_id"].startswith("depot"):
                            from_location = prev_step["location_id"]

                    if i < len(route_steps) - 1:
                        # Get next non-depot location
                        next_step = route_steps[i + 1]
                        if not next_step["location_id"].startswith("depot"):
                            to_location = next_step["location_id"]

                    steps.append(
                        ItineraryStep(
                            type=step_type,
                            id=location_id,
                            start=start_time_str,
                            duration_min=duration,
                            **{"from": from_location, "to": to_location},
                            mode=None,  # No transport mode for visit/lunch steps
                        )
                    )

                    # Add travel step to next location (only for visits, not lunch)
                    if (
                        i < len(route_steps) - 1 and step_type == "visit"
                    ):  # Only add transport for visits

                        next_step = route_steps[i + 1]
                        next_location_id = next_step["location_id"]

                        # Skip if next is depot or lunch
                        if not next_location_id.startswith(
                            "depot"
                        ) and not next_location_id.startswith("lunch"):

                            # Calculate travel time and mode
                            travel_time = 15  # Default
                            travel_mode = "walk"  # Default mode

                            key = f"{location_id}-{next_location_id}"
                            if key in prepared_data.matrixTime.segments:
                                segment = prepared_data.matrixTime.segments[key]
                                travel_time = int(segment.duree)
                                travel_mode = segment.type

                            # Calculate travel start time (after visit ends)
                            travel_start = start_time + duration
                            travel_hours = travel_start // 60
                            travel_minutes = travel_start % 60
                            travel_start_str = (
                                f"{travel_hours:02d}:{travel_minutes:02d}"
                            )

                            steps.append(
                                ItineraryStep(
                                    type="transport",
                                    **{"from": location_id, "to": next_location_id},
                                    start=travel_start_str,
                                    duration_min=travel_time,
                                    mode=travel_mode,
                                )
                            )

            daily_itineraries.append(
                DailyItinerary(day=vehicle_id, date=date, steps=steps)
            )

    return FinalItineraryOutput(days=daily_itineraries, scores=scores)


# === DAILY SUMMARY GENERATION ===


def calculate_step_scores(
    step: ItineraryStep,
    spots: List[Spot],
    weather_data: CityWeatherData,
    date: str,
    distance_matrix: MatrixTime,
) -> StepScores:
    """Calculate crowd and weather scores for a single itinerary step."""

    if step.type == "visit":
        # Find the spot data
        spot = next((s for s in spots if s.id == step.id), None)
        if not spot:
            return StepScores(crowd_percentage=50.0, weather_percentage=50.0)

        # Calculate crowd score based on start time
        start_hour = int(step.start.split(":")[0])
        day_index = datetime.strptime(date, "%Y-%m-%d").weekday()
        crowd_hour = calculate_crowd_hour_for_spot(spot, day_index)
        crowd_score = crowd_hour.get(start_hour, 0.0)
        crowd_percentage = min(100.0, max(0.0, crowd_score))

        # Calculate weather score based on start time
        weather_percentage = 50.0  # Default
        if date in weather_data.weather_by_date:
            date_weather = weather_data.weather_by_date[date]
            hour_str = str(start_hour)
            if hour_str in date_weather.hourly_data:
                weather_percentage = (
                    date_weather.hourly_data[hour_str].normalized_weather_score or 50.0
                )

        return StepScores(
            crowd_percentage=crowd_percentage, weather_percentage=weather_percentage
        )

    elif step.type == "transport":
        # For transport, use average conditions during travel time
        start_hour = int(step.start.split(":")[0])

        # Calculate distance cost
        distance_cost = 0.0
        if step.from_spot and step.to_spot:
            key = f"{step.from_spot}-{step.to_spot}"
            if key in distance_matrix.segments:
                # Convert distance to cost (higher distance = higher cost)
                distance = distance_matrix.segments[key].distance
                distance_cost = distance / 1000.0  # Convert to km

        # Weather score during transport
        weather_percentage = 50.0  # Default
        if date in weather_data.weather_by_date:
            date_weather = weather_data.weather_by_date[date]
            hour_str = str(start_hour)
            if hour_str in date_weather.hourly_data:
                weather_percentage = (
                    date_weather.hourly_data[hour_str].normalized_weather_score or 50.0
                )

        return StepScores(
            crowd_percentage=30.0,  # Assume moderate crowd during transport
            weather_percentage=weather_percentage,
            distance_cost=distance_cost,
        )

    else:  # lunch or other
        return StepScores(crowd_percentage=50.0, weather_percentage=50.0)


def calculate_daily_scores(
    daily_itinerary: DailyItinerary,
    spots: List[Spot],
    weather_data: CityWeatherData,
    distance_matrix: MatrixTime,
) -> Tuple[float, float, float]:
    """Calculate daily optimization scores (distance, crowd, weather)."""

    total_distance_cost = 0.0
    total_crowd_cost = 0.0
    total_weather_cost = 0.0
    visit_count = 0
    transport_count = 0

    for step in daily_itinerary.steps:
        step_scores = calculate_step_scores(
            step, spots, weather_data, daily_itinerary.date, distance_matrix
        )

        if step.type == "visit":
            visit_count += 1
            # Higher crowd percentage = higher cost (worse)
            total_crowd_cost += 100 - step_scores.crowd_percentage
            # Lower weather percentage = higher cost (worse)
            total_weather_cost += 100 - step_scores.weather_percentage

        elif step.type == "transport":
            transport_count += 1
            total_distance_cost += step_scores.distance_cost
            # Weather affects transport too
            total_weather_cost += (
                100 - step_scores.weather_percentage
            ) * 0.5  # Reduced weight for transport

    # Calculate average costs
    avg_distance_cost = total_distance_cost / max(1, transport_count)
    avg_crowd_cost = total_crowd_cost / max(1, visit_count)
    avg_weather_cost = total_weather_cost / max(1, visit_count + transport_count)

    # Convert costs to scores (100 - cost)
    distance_score = max(
        0, 100 - min(100, avg_distance_cost * 10)
    )  # Scale distance cost
    crowd_score = max(0, 100 - min(100, avg_crowd_cost))
    weather_score = max(0, 100 - min(100, avg_weather_cost))

    return distance_score, crowd_score, weather_score


def generate_llm_prompt(
    daily_itinerary: DailyItinerary,
    city: str,
    companions: str,
    spots: List[Spot],
    weather_data: CityWeatherData,
    distance_matrix: MatrixTime,
) -> str:
    """Generate LLM prompt for daily summary description."""

    prompt_parts = [
        "Rôle : guide de voyage.",
        "Données :",
        f"- Date : {daily_itinerary.date}",
        f"- Ville : {city}",
        f"- Voyageurs : {companions}",
        f"- Itinéraire jour {daily_itinerary.day + 1} :",
    ]

    step_counter = 1
    for step in daily_itinerary.steps:
        step_scores = calculate_step_scores(
            step, spots, weather_data, daily_itinerary.date, distance_matrix
        )

        if step.type == "visit":
            # Find spot name
            spot = next((s for s in spots if s.id == step.id), None)
            spot_name = spot.name if spot else f"Spot {step.id}"

            end_time = calculate_end_time(step.start, step.duration_min)
            prompt_parts.append(
                f"{step_counter}) Visite {spot_name} – {step.start}–{end_time} "
                f"(affluence : {step_scores.crowd_percentage:.0f} %, "
                f"météo : {step_scores.weather_percentage:.0f} %)"
            )
            step_counter += 1

        elif step.type == "transport":
            mode_french = {
                "walk": "marche",
                "public_transit": "transport en commun",
                "car": "voiture",
                "bike": "vélo",
            }.get(step.mode, step.mode)

            prompt_parts.append(
                f"{step_counter}) Transport – {mode_french} "
                f"({step.duration_min} min, affluence : {step_scores.crowd_percentage:.0f} %, "
                f"météo : {step_scores.weather_percentage:.0f} %)"
            )
            step_counter += 1

    prompt_parts.append(
        "Tâche : rédige un paragraphe de moins de 150 caractères décrivant l'esprit de cette journée."
    )

    return "\n".join(prompt_parts)


def calculate_end_time(start_time: str, duration_min: int) -> str:
    """Calculate end time given start time and duration."""
    start_hour, start_min = map(int, start_time.split(":"))
    total_minutes = start_hour * 60 + start_min + duration_min

    end_hour = (total_minutes // 60) % 24
    end_min = total_minutes % 60

    return f"{end_hour:02d}:{end_min:02d}"


def generate_daily_summary(
    daily_itinerary: DailyItinerary,
    city: str,
    companions: str,
    spots: List[Spot],
    weather_data: CityWeatherData,
    distance_matrix: MatrixTime,
    optimization_mode: OptimizationMode,
) -> DailySummary:
    """Generate complete daily summary with scores and AI description."""

    # Calculate daily scores
    distance_score, crowd_score, weather_score = calculate_daily_scores(
        daily_itinerary, spots, weather_data, distance_matrix
    )

    # Generate LLM prompt
    prompt = generate_llm_prompt(
        daily_itinerary, city, companions, spots, weather_data, distance_matrix
    )

    # Get AI description
    ai_description = (
        client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
        )
        .choices[0]
        .message.content
    )

    print(ai_description)

    # Adjust scores based on optimization mode
    if optimization_mode == OptimizationMode.FREEMIUM:
        # Only distance score is meaningful in basic mode
        crowd_score = None
        weather_score = None

    return DailySummary(
        date=daily_itinerary.date,
        distance_score=distance_score,
        crowd_score=crowd_score,
        weather_score=weather_score,
        ai_description=ai_description,
    )


# === MAIN OPTIMIZATION FUNCTION ===


def create_fallback_solution(
    spots: List[Spot],
    travel_dates: List[str],
    daily_hours_range: Tuple[str, str],
    weights: SolverInputWeights,
    distance_matrix: Optional[MatrixTime] = None,
) -> FinalItineraryOutput:
    """Create a simple fallback solution when OR-Tools fails."""
    logger.info("Creating fallback solution")

    daily_itineraries = []

    # Simple greedy allocation of spots to days
    spots_per_day = len(spots) // len(travel_dates)
    extra_spots = len(spots) % len(travel_dates)

    spot_index = 0
    for day_idx, date in enumerate(travel_dates):
        # Determine number of spots for this day
        num_spots = spots_per_day + (1 if day_idx < extra_spots else 0)

        # Get spots for this day
        day_spots = spots[spot_index : spot_index + num_spots]
        spot_index += num_spots

        # Create simple schedule
        start_hour, start_min = map(int, daily_hours_range[0].split(":"))
        current_time = start_hour * 60 + start_min

        steps = []
        for i, spot in enumerate(day_spots):
            hours = current_time // 60
            minutes = current_time % 60
            start_time_str = f"{hours:02d}:{minutes:02d}"

            # Parse visit duration
            visit_duration = 60  # Default 1 hour
            try:
                duration_str = spot.visitDuration.lower().strip()
                if "h" in duration_str:
                    parts = duration_str.split("h")
                    hours_part = int(parts[0])
                    minutes_part = int(parts[1]) if len(parts) > 1 and parts[1] else 0
                    visit_duration = hours_part * 60 + minutes_part
                elif "min" in duration_str:
                    visit_duration = int(duration_str.replace("min", ""))
                elif duration_str.isdigit():
                    visit_duration = int(duration_str)
            except:
                visit_duration = 60

            steps.append(
                ItineraryStep(
                    type="visit",
                    id=spot.id,
                    start=start_time_str,
                    duration_min=visit_duration,
                )
            )

            # Add visit duration to current time
            current_time += visit_duration

            # Add travel time to next spot
            if i < len(day_spots) - 1:
                next_spot = day_spots[i + 1]
                travel_time = 30  # Default 30 minutes
                travel_mode = "walk"  # Default mode

                # Try to get actual travel time from distance matrix
                if distance_matrix:
                    key = f"{spot.id}-{next_spot.id}"
                    if key in distance_matrix.segments:
                        segment = distance_matrix.segments[key]
                        travel_time = int(segment.duree)
                        travel_mode = segment.type

                # Add transport step
                travel_start = current_time
                travel_hours = travel_start // 60
                travel_minutes = travel_start % 60
                travel_start_str = f"{travel_hours:02d}:{travel_minutes:02d}"

                steps.append(
                    ItineraryStep(
                        type="transport",
                        from_spot=spot.id,
                        to_spot=next_spot.id,
                        start=travel_start_str,
                        duration_min=travel_time,
                        mode=travel_mode,
                    )
                )

                current_time += travel_time

        daily_itineraries.append(DailyItinerary(day=day_idx, date=date, steps=steps))

    return FinalItineraryOutput(
        days=daily_itineraries,
        scores=OptimizationScores(distance=50, crowd=50, weather=50),
    )


def optimise_travel(
    city: str,
    spots: List[Spot],
    travel_dates: List[str],
    daily_hours_range: Tuple[str, str],
    optimization_mode: OptimizationMode,
    max_walk_time_per_segment_min: int,
    visit_pace: VisitPace = VisitPace.BALANCED,
    companions: str = "solo",  # Add companions parameter
) -> FinalItineraryOutput:
    """
    Main optimization function that coordinates all sub-functions to create an optimal itinerary.
    """
    logger.info(
        f"Starting optimization for {city} with {len(spots)} spots over {len(travel_dates)} days"
    )

    try:
        # Load base data
        distance_matrix = load_distance_matrix_from_db(city)
        weights_dict = load_optimisation_weights_from_db()
        weights = SolverInputWeights(**weights_dict)

        # Filter and score distance matrix
        filtered_distance_matrix = filter_distance_matrix(distance_matrix, spots)
        distance_matrix_score = get_distance_matrix_score(filtered_distance_matrix)

        # Get weather data
        weather_data = get_city_weather_data(
            city.split("-")[0], travel_dates, daily_hours_range
        )

        # === USER AND SPOT DATA MANAGEMENT ===

        # Process user windows
        user_windows = process_user_windows(travel_dates, daily_hours_range)

        # Determine lunch requirements
        lunch_requirements = determine_lunch_requirements(user_windows)

        # Process spot information
        solver_spots = process_spot_info(spots, travel_dates, visit_pace)

        # Create lunch and depot info
        lunch_infos = create_lunch_info(lunch_requirements)
        depot_infos = create_depot_info()

        # Extract weather scores
        weather_scores = extract_weather_score_hours(
            weather_data, travel_dates, daily_hours_range
        )

        # Combine all locations
        all_locations = depot_infos + solver_spots + lunch_infos

        # Validate that we have enough data
        if not all_locations or len(solver_spots) == 0:
            logger.warning("No valid spots found, using fallback solution")
            fallback_output = create_fallback_solution(
                spots,
                travel_dates,
                daily_hours_range,
                weights,
                filtered_distance_matrix,
            )
            # Generate daily summaries for fallback
            daily_summaries = []
            for day in fallback_output.days:
                summary = generate_daily_summary(
                    day,
                    city,
                    companions,
                    spots,
                    weather_data,
                    filtered_distance_matrix,
                    optimization_mode,
                )
                daily_summaries.append(summary)
            fallback_output.daily_summaries = daily_summaries
            return fallback_output

        # Prepare solver data
        prepared_data = PreparedSolverData(
            mode=optimization_mode,
            numVehicles=len(travel_dates),
            userWindows=user_windows,
            vehicles=[{"id": i} for i in range(len(travel_dates))],
            weights=weights,
            lunchIndex={
                str(v): f"lunch_day_{v}"
                for v, needs in lunch_requirements.items()
                if needs
            },
            weatherScoreHour=weather_scores,
            matrixTime=filtered_distance_matrix,
            matrixScoreDistance=distance_matrix_score,
            locations=all_locations,
        )

        # === OR-TOOLS SOLVER INTEGRATION ===

        # Solve optimization problem
        solution_data = solve_optimization_problem(prepared_data)

        if not solution_data:
            logger.warning("OR-Tools failed to find solution, using fallback")
            fallback_output = create_fallback_solution(
                spots,
                travel_dates,
                daily_hours_range,
                weights,
                filtered_distance_matrix,
            )
            # Generate daily summaries for fallback
            daily_summaries = []
            for day in fallback_output.days:
                try:
                    summary = generate_daily_summary(
                        day,
                        city,
                        companions,
                        spots,
                        CityWeatherData(city=city, weather_by_date={}),
                        filtered_distance_matrix or MatrixTime(segments={}),
                        optimization_mode,
                    )
                    daily_summaries.append(summary)
                except Exception as summary_error:
                    logger.warning(f"Failed to generate daily summary: {summary_error}")
                    # Add basic summary on failure
                    daily_summaries.append(
                        DailySummary(
                            date=day.date,
                            distance_score=50.0,
                            crowd_score=(
                                50.0
                                if optimization_mode == OptimizationMode.PREMIUM
                                else None
                            ),
                            weather_score=(
                                50.0
                                if optimization_mode == OptimizationMode.PREMIUM
                                else None
                            ),
                            ai_description="Une journée soigneusement planifiée pour découvrir la ville.",
                        )
                    )
            fallback_output.daily_summaries = daily_summaries
            return fallback_output

        # === OUTPUT AND RELEVANCE SCORING ===

        # Calculate individual scores
        scores = calculate_individual_scores(solution_data, prepared_data)

        # Calculate relevance score
        relevance_score = calculate_relevance_score(scores, weights)

        # Format final output
        final_output = format_solution_output(
            solution_data, prepared_data, travel_dates, scores, relevance_score
        )

        # Generate daily summaries
        daily_summaries = []
        for day in final_output.days:
            summary = generate_daily_summary(
                day,
                city,
                companions,
                spots,
                weather_data,
                filtered_distance_matrix,
                optimization_mode,
            )
            daily_summaries.append(summary)

        # Add daily summaries to output
        final_output.daily_summaries = daily_summaries

        logger.info(
            f"Optimization completed successfully with relevance score: {relevance_score:.2f}"
        )
        return final_output

    except Exception as e:
        logger.error(f"Error in optimization: {e}")
        import traceback

        logger.error(f"Traceback: {traceback.format_exc()}")

        # Return fallback solution
        try:
            weights = SolverInputWeights(distance=0.5, crowd=0.25, weather=0.25)
            # Try to load distance matrix for fallback
            try:
                distance_matrix = load_distance_matrix_from_db(city)
                filtered_distance_matrix = filter_distance_matrix(
                    distance_matrix, spots
                )
            except:
                filtered_distance_matrix = None
            fallback_output = create_fallback_solution(
                spots,
                travel_dates,
                daily_hours_range,
                weights,
                filtered_distance_matrix,
            )
            # Generate daily summaries for fallback
            daily_summaries = []
            for day in fallback_output.days:
                try:
                    summary = generate_daily_summary(
                        day,
                        city,
                        companions,
                        spots,
                        CityWeatherData(city=city, weather_by_date={}),
                        filtered_distance_matrix or MatrixTime(segments={}),
                        optimization_mode,
                    )
                    daily_summaries.append(summary)
                except Exception as summary_error:
                    logger.warning(f"Failed to generate daily summary: {summary_error}")
                    # Add basic summary on failure
                    daily_summaries.append(
                        DailySummary(
                            date=day.date,
                            distance_score=50.0,
                            crowd_score=(
                                50.0
                                if optimization_mode == OptimizationMode.PREMIUM
                                else None
                            ),
                            weather_score=(
                                50.0
                                if optimization_mode == OptimizationMode.PREMIUM
                                else None
                            ),
                            ai_description="Une journée soigneusement planifiée pour découvrir la ville.",
                        )
                    )
            fallback_output.daily_summaries = daily_summaries
            return fallback_output
        except Exception as fallback_error:
            logger.error(f"Fallback solution also failed: {fallback_error}")
            return FinalItineraryOutput(
                days=[],
                scores=OptimizationScores(distance=0, crowd=0, weather=0),
                daily_summaries=[],
            )
