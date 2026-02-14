from typing import List, Tuple, Dict, Optional, Union, Any
from datetime import datetime, timedelta
import logging
import traceback
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
from app.services.utils import client

logger = logging.getLogger(__name__)


def filter_distance_matrix(
    distance_matrix: MatrixTime, spots_data: List[SimplifiedSpot]
) -> MatrixTime:
    """Filter the distance matrix to only include spots that are in the spots_data list"""
    ids = [spot.id for spot in spots_data]
    segments = distance_matrix.segments

    new_segments = {}
    invalid_segments_count = 0
    for key, segment in segments.items():
        keys = key.split("-")
        start_id = keys[0]
        end_id = keys[1]
        if start_id in ids and end_id in ids:
            logger.info(f"Keeping segment: {key} (distance={segment.distance}, duree={segment.duree})")
            # Also filter out unreachable segments (distance=0, duree=99999)
            if segment.distance <= 0 or segment.duree >= 99999:
                invalid_segments_count += 1
                logger.warning(
                    f"Filtering out invalid segment: {key} (distance={segment.distance}, duree={segment.duree})"
                )
                continue
            new_segments[key] = segment

    if invalid_segments_count > 0:
        logger.info(
            f"Filtered out {invalid_segments_count} invalid segments from distance matrix"
        )

    filtered_matrix = MatrixTime(segments=new_segments)
    return filtered_matrix


def get_distance_matrix_score(distance_matrix: MatrixTime) -> MatrixScoreDistance:
    """
    Normalize all distances in the MatrixTime to a score between 0 and 100.
    score_segment = ((Dmax - Dij) / (Dmax - Dmin)) * 100
    Returns a MatrixScoreDistance with the same keys as the input segments.
    """
    # Extract all distances, filtering out invalid values (distance=0 for unreachable spots)
    valid_distances = [
        segment.distance
        for segment in distance_matrix.segments.values()
        if segment.distance > 0 and segment.duree < 99999  # Filter out error cases
    ]

    if not valid_distances:
        # If no valid distances, return default scores
        scores = {key: 50.0 for key in distance_matrix.segments.keys()}
        return MatrixScoreDistance(scores=scores)

    dmin = min(valid_distances)
    dmax = max(valid_distances)

    # Avoid division by zero if all distances are the same
    if dmax == dmin:
        # All scores are 100 if all distances are the same (best possible)
        scores = {key: 100.0 for key in distance_matrix.segments.keys()}
        return MatrixScoreDistance(scores=scores)

    scores = {}
    for key, segment in distance_matrix.segments.items():
        # Check for invalid segments (unreachable destinations)
        if segment.distance <= 0 or segment.duree >= 99999:
            # Assign very low score for unreachable segments
            scores[key] = 0.0
            logger.warning(
                f"Unreachable segment detected: {key} (distance={segment.distance}, duree={segment.duree})"
            )
        else:
            score = ((dmax - segment.distance) / (dmax - dmin)) * 100
            scores[key] = score

    return MatrixScoreDistance(scores=scores)


def calculate_crowd_hour_for_spot(
    spot,
    weekday: int,
    month: int
    ) -> Dict[int, float]:
    """
    Calcule le score d'affluence pour chaque heure.
    Formule : score_crowd(h) = popular_time(h) * (density[month-1] / 5)
    Args:
    spot: Objet Spot (structure Firebase)
    weekday: Jour de la semaine (0=Lundi, 6=Dimanche)
    month: Mois (1-12)
    Returns:
    Dict {heure: score} où score entre 0-100
    """
    
    # Valeur par défaut si données manquantes
    default_crowd = {hour: 50.0 for hour in range(8, 20)}
    # Vérifier popularTimes
    if not hasattr(spot, 'popularTimes') or not spot.popularTimes:
        return default_crowd
    if weekday >= len(spot.popularTimes):
        return default_crowd
    
    # Calculer coefficient mensuel
    # density est indexé 0-11 (janvier=0, décembre=11)
    # month est 1-12, donc on fait month-1
    if not hasattr(spot, 'density') or not spot.density:
        coefficient = 1.0
    elif month < 1 or month > len(spot.density):
        coefficient = 1.0
    else:
        month_density = spot.density[month - 1]
        coefficient = month_density / 5
    # Récupérer popularTimes pour CE jour de la semaine

    popular_times_day = spot.popularTimes[weekday]
    # Calculer scores
    crowdHour: Dict[int, float] = {}
    for hour in range(8, 20):
        hour_str = str(hour)
        if isinstance(popular_times_day, dict):
            val = popular_times_day.get(hour, popular_times_day.get(hour_str, 0.0))
            popular_time = float(val)
        else:
            popular_time = float(getattr(popular_times_day, hour_str, 0.0))
        
        score = popular_time * coefficient
        crowdHour[hour] = min(max(score, 0.0), 100.0)
    return crowdHour


# === USER AND SPOT DATA MANAGEMENT ===


# ex: hourly_availability: {"2024-07-10": ["09:00", "18:00"]}
def process_user_windows(
    travel_dates: List[str], hourly_availability: Dict[str, List[str]], 
) -> List[UserWindow]:
    """
    Convert daily hours range to user windows for each travel day.
    Returns array of UserWindow objects with day, start, and end in minutes UTC.
    """
    user_windows = []
    
    for i, date in enumerate(travel_dates):
        # Convert time strings to minutes from midnight
        start_time = hourly_availability[date][0]  # e.g., "09:00"
        end_time = hourly_availability[date][1]  # e.g., "18:00"

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
    if standard_duration_min <= 30:
        return standard_duration_min
    
    if 30 < standard_duration_min <= 120:
        if pace == VisitPace.FAST:
            return standard_duration_min - 15
        elif pace == VisitPace.RELAXED:
            return standard_duration_min + 15
        else:
            return standard_duration_min
    elif 120 < standard_duration_min <= 180:
        if pace == VisitPace.FAST:
            return standard_duration_min - 30
        elif pace == VisitPace.RELAXED:
            return standard_duration_min + 30
        else:
            return standard_duration_min
    else:
        if pace == VisitPace.FAST:
            return standard_duration_min - 60
        elif pace == VisitPace.RELAXED:
            return standard_duration_min + 60
        else:
            return standard_duration_min


def process_spot_info(
    spots: List[Spot],
    travel_dates: List[str],
    hourly_availability: Dict[str, List[str]],
    pace: VisitPace = VisitPace.BALANCED
    ) -> List[SolverSpotInfo]:
    """
    Transforme les spots en nœuds SolverSpotInfo pour OR-Tools.
    
    Crée un nœud par (spot, jour) pour permettre à OR-Tools de
    choisir le meilleur jour pour chaque visite.
    Args:
        spots: Liste des spots bruts depuis Firebase
        travel_dates: Liste des dates de voyage ["2024-07-10", ...]
        hourly_availability: Disponibilités user par jour
        pace: Rythme de visite (FAST, BALANCED, RELAXED)
    Returns:
        Liste de SolverSpotInfo prêts pour OR-Tools
    """
    
    solver_spots = []
    
    for spot in spots:
        try:
            # Parser la durée de visite
            duration_min = parse_visit_duration(spot.visitDuration)
            adjusted_duration = adjust_visit_duration(duration_min, pace)
            
            # Déterminer si outdoor
            is_outdoor = hasattr(spot, "locationType") and (
                spot.locationType == LocationType.OUTDOOR
                or spot.locationType == LocationType.MIXED
            )
            
            # Créer un nœud pour CHAQUE jour
            for day_index, date_str in enumerate(travel_dates):
                # Calculer weekday et month pour cette date
                date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                weekday = date_obj.weekday() # Pour horaires d'ouverture + affluence
                month = date_obj.month # Pour coefficient affluence uniquement
                
                # Récupérer disponibilité user CE jour
                if date_str not in hourly_availability:
                    continue
                user_start_time, user_end_time = hourly_availability[date_str]
                user_start_min = parse_time_to_minutes(user_start_time)
                user_end_min = parse_time_to_minutes(user_end_time)

                # Récupérer horaires d'ouverture du spot CE jour
                spot_windows = get_spot_windows_for_day(spot, weekday)
                
                # Calculer intersection (fenêtres de DÉBUT possible)
                time_windows = intersect_windows_with_user_availability(
                    spot_windows, user_start_min, user_end_min, adjusted_duration
                )
                
                # Si aucune fenêtre valide → spot non visitable ce jour
                if not time_windows:
                    continue
                
                # Calculer affluence pour CE jour (weekday + month)
                logger.info(f'Calculating crowd hour for spot {spot.id} on date {date_str} (weekday={weekday}, month={month})')
                crowd_hour = calculate_crowd_hour_for_spot(spot, weekday, month)
                
                # Créer le nœud
                for tw_idx, tw in enumerate(time_windows):
                    solver_spots.append(
                    SolverSpotInfo(
                        id=f"{spot.id}_day_{day_index}_tw_{tw_idx}",
                        original_spot_id=spot.id,   # regroupe toutes les variantes
                        vehicle=day_index,
                        dur=adjusted_duration,
                        outdoor=is_outdoor,
                        time_windows=[tw],          # 1 seule fenêtre par node
                        crowdHour=crowd_hour,
                        ignoreMissingScore=True,
                    )
                )
        except Exception as e:
            logger.warning(f"Error processing spot {spot.id}: {e}")
            continue
    
    
    return solver_spots


def create_lunch_info(lunch_requirements: Dict[int, bool], user_windows: List[UserWindow]) -> List[SolverLunchInfo]:
    """Create lunch break information for days that need it."""
    lunch_infos = []
    
    LUNCH_DURATION = 90
    LUNCH_EARLIEST_START = 720 # 12:00
    LUNCH_LATEST_START_MAX = 840 # 2:00 p.m.
    
    # Create a dictionary for quick access to user windows:
    user_windows_by_vehicle = {w.vehicle: w for w in user_windows}

    for vehicle, needs_lunch in lunch_requirements.items():
        if not needs_lunch:
            continue
        
        window = user_windows_by_vehicle.get(vehicle)
        if not window:
            continue
        
        # Calculer la dernière heure de début possible
        # (pour que le lunch finisse avant la fin de journée)
        latest_start = min(LUNCH_LATEST_START_MAX, window.end - LUNCH_DURATION)
        
        if latest_start < LUNCH_EARLIEST_START:
            # Pas assez de temps pour un lunch ce jour-là
            continue
        
        lunch_infos.append(
            SolverLunchInfo(
                id=f"lunch_day_{vehicle}",
                dur=LUNCH_DURATION,  # 1.5 hours
                outdoor=False,
                vehicle=vehicle,
                time_windows=[
                    (LUNCH_EARLIEST_START, latest_start)
                ], 
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
    hourly_availability: Dict[str, List[str]],
) -> Dict[int, float]:
    """
    Extract hourly weather scores for the optimization period.
    Returns dict mapping hour (in minutes from midnight) to normalized weather score.
    """
    weather_scores = {}

    

    for date in travel_dates:
        
        start_hour = int(hourly_availability[date][0].split(":")[0])
        end_hour = int(hourly_availability[date][1].split(":")[0])
        
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

def base_spot_id(loc) -> str:
    """
    Retourne l'ID original du spot pour les lookups dans les matrices.
    Les nodes dupliqués (day_X_tw_Y) sont mappés vers leur original_spot_id.
    """
    if loc.id.startswith("depot") or loc.id.startswith("lunch"):
        return loc.id
    if hasattr(loc, "original_spot_id") and loc.original_spot_id:
        return loc.original_spot_id
    return loc.id



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
        
        # 1. Grouper les node_ids par original_spot_id
        nodes_by_original = {}
        for node_id, loc in enumerate(prepared_data.locations):
            # Ignorer depot et lunch
            if loc.id.startswith("depot") or loc.id.startswith("lunch"):
                continue
            # Spot dupliqué : original_spot_id doit exister
            if hasattr(loc, "original_spot_id") and loc.original_spot_id:
                original_id = loc.original_spot_id
                nodes_by_original.setdefault(original_id, []).append(node_id)
                
        # forcer le spot sur son véhicule
        for location_idx, location in enumerate(prepared_data.locations):
            if hasattr(location, "original_spot_id") and location.original_spot_id:
                if hasattr(location, "vehicle"):
                    vehicle_id = location.vehicle
                    index = manager.NodeToIndex(location_idx)
                    if index >= 0 and vehicle_id < num_vehicles:
                        for v in range(num_vehicles):
                            if v != vehicle_id:
                                routing.VehicleVar(index).RemoveValue(v)
        # 2. Créer les disjonctions par groupe
        # For each original spot with multiple variants: force exactly one active
        solver = routing.solver()
        
        # make lunch mandatory
        for location_idx, location in enumerate(prepared_data.locations):
            if location.id.startswith("lunch_day_"):
                index = manager.NodeToIndex(location_idx)
                if index >= 0:
                    solver.Add(routing.ActiveVar(index) == 1)
        
        for oid, node_ids in nodes_by_original.items():
            if len(node_ids) <= 1:
                continue
            indices = [manager.NodeToIndex(n) for n in node_ids]
            indices = [i for i in indices if i >= 0]
            # max_cardinality=1 impose AU PLUS 1 variante visitée
            # La pénalité élevée incite fortement à en choisir une plutôt que de toutes les skipper
            routing.AddDisjunction(indices, 100000, 1)
            

        # Create simplified distance callback
        def distance_callback(from_index, to_index):
            from_node = manager.IndexToNode(from_index)
            to_node = manager.IndexToNode(to_index)

            from_location = prepared_data.locations[from_node]
            to_location = prepared_data.locations[to_node]
            
            from_id = base_spot_id(from_location)
            to_id = base_spot_id(to_location)
            key = f"{from_id}-{to_id}"
            # from_id = locations[from_index].id

            # Zero cost for depot transitions
            if from_location.id.startswith("depot") or to_location.id.startswith(
                "depot"
            ):
                return 0

            # Look up distance score between spots
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
            
            from_node_id = base_spot_id(from_location)
            to_node_id = base_spot_id(to_location)

            # Travel time from origin to destination
            travel_time = 0
            if not from_location.id.startswith(
                "depot"
            ) and not to_location.id.startswith("depot"):
                # Look up travel time in distance matrix
                key = f"{from_node_id}-{to_node_id}"
                if key in prepared_data.matrixTime.segments:
                    segment_duree = prepared_data.matrixTime.segments[key].duree
                    # Filter out unreachable destinations (duree=99999)
                    if segment_duree >= 99999:
                        # Use very high penalty for unreachable destinations to discourage this route
                        travel_time = 1000  # High penalty but not infinite to allow solver to work
                        logger.warning(
                            f"Unreachable route detected: {key} (duree={segment_duree})"
                        )
                    else:
                        travel_time = int(segment_duree)  # duree is in minutes
                else:
                    # Default travel time if not found
                    travel_time = 15  # 15 minutes default

            # Service time at destination
            service_time = 0 if from_location.id.startswith("depot") else from_location.dur

            # Total time = travel time + service time
            return travel_time + service_time

        time_callback_index = routing.RegisterTransitCallback(time_callback)

        # Add time windows
        time_dimension_name = "Time"
        routing.AddDimension(
            time_callback_index,
            0,  # slack_max (no buffer to eliminate gaps)
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
        for node_id, loc in enumerate(prepared_data.locations):
            if loc.id.startswith("depot") or loc.id.startswith("lunch"):
                continue
            if hasattr(loc, "original_spot_id") and loc.original_spot_id:
                nodes_by_original.setdefault(loc.original_spot_id, []).append(node_id)

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
        logger.info(f"ENding OR-Tools solver...: ${solution}")
        if solution:
            logger.info(f"Solution found with status: {routing.status()}")
            return extract_solution_data(manager, routing, solution, prepared_data)
        else:
            logger.warning(f"No solution found. Status: {routing.status()}")
            return None

    except Exception as e:
        logger.error(f"Error solving optimization problem: {e}")
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

    def adjust_schedule_times(
        route_steps: List[Dict], prepared_data: PreparedSolverData
    ) -> List[Dict]:
        """Post-process route to eliminate gaps and fix timing conflicts."""
        if not route_steps:
            return route_steps

        # Find lunch position and optimize its timing
        lunch_index = None
        for i, step in enumerate(route_steps):
            if step["location_id"].startswith("lunch"):
                lunch_index = i
                break

        adjusted_steps = []
        current_time = route_steps[0][
            "start_time"
        ]  # Start with the first activity time

        for i, step in enumerate(route_steps):
            location_id = step["location_id"]
            duration = step["duration"]

            # Skip depot locations
            if location_id.startswith("depot"):
                continue

            # Special handling for lunch to optimize timing
            if location_id.startswith("lunch"):
                # Ensure lunch starts in optimal window (12:00-14:00 = 720-840 minutes)
                optimal_lunch_start = max(720, current_time)  # Don't start before 12:00
                optimal_lunch_start = min(
                    840, optimal_lunch_start
                )  # Don't start after 14:00

                # Check if we need to adjust timing to fit lunch properly
                if current_time < 720:  # If we're too early for lunch
                    current_time = 720  # Start lunch at 12:00
                elif current_time > 840:  # If we're too late for lunch window
                    current_time = 840  # Start lunch at 14:00 (latest possible)
                else:
                    current_time = optimal_lunch_start

            # Set the start time to current_time
            step_copy = step.copy()
            step_copy["start_time"] = current_time
            adjusted_steps.append(step_copy)

            # Update current_time for next activity
            current_time += duration

            # Add travel time to next location if there is one
            if i < len(route_steps) - 1:
                next_step = route_steps[i + 1]
                next_location_id = next_step["location_id"]

                if not next_location_id.startswith("depot"):
                    # Calculate travel time
                    travel_time = 15  # Default
                    if "_tw_" in location_id and "day_" in location_id:
                        location_id_base = location_id.split("_day_")[0]
                    if "_tw_" in next_location_id and "day_" in next_location_id:
                        next_location_id_base = next_location_id.split("_day_")[0]
                        
                        
                    logger.info(f"Calculating travel time from {location_id_base} to {next_location_id_base}")
                    key = f"{location_id_base}-{next_location_id_base}"
                    if key in prepared_data.matrixTime.segments:
                        travel_time = int(prepared_data.matrixTime.segments[key].duree)

                    current_time += travel_time

        return adjusted_steps

    daily_itineraries = []

    for route in solution_data["routes"]:
        vehicle_id = route["vehicle_id"]
        if vehicle_id < len(travel_dates):
            date = travel_dates[vehicle_id]

            steps = []
            # Adjust route to eliminate gaps and fix timing conflicts
            route_steps = adjust_schedule_times(route["route"], prepared_data)

            for i, step_data in enumerate(route_steps):
                location_id = step_data["location_id"]
                start_time = step_data["start_time"]
                duration = step_data["duration"]
                
                visit_id = location_id
                loc = next((l for l in prepared_data.locations if l.id == location_id), None)
                if loc and hasattr(loc, "original_spot_id") and loc.original_spot_id:
                    visit_id = loc.original_spot_id

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
                    
                    from_location_id = from_location
                    if (from_location is not None) and "_tw_" in from_location:
                        from_location_id = from_location.split("_day_")[0]
                    to_location_id = to_location
                    if (to_location is not None) and "_tw_" in to_location:
                        to_location_id = to_location.split("_day_")[0]
                    steps.append(
                        ItineraryStep(
                            type=step_type,
                            id=visit_id,
                            start=start_time_str,
                            duration_min=duration,
                            **{"from": from_location_id, "to": to_location_id},
                            mode=None,  # No transport mode for visit/lunch steps
                        )
                    )

                    # Add travel step to next location (for both visits and lunch)
                    if i < len(route_steps) - 1:  # Add transport after any activity

                        next_step = route_steps[i + 1]
                        next_location_id = next_step["location_id"]

                        # Skip if next is depot or lunch
                        if not next_location_id.startswith(
                            "depot"
                        ) and not next_location_id.startswith("lunch"):

                            # Calculate travel time and mode
                            travel_time = 15  # Default
                            travel_mode = TravelMode.WALK  # Default mode
                            
                            if "_tw_" in location_id and "day_" in location_id:
                                location_id_base = location_id.split("_day_")[0]
                            if "_tw_" in next_location_id and "day_" in next_location_id:
                                next_location_id_base = next_location_id.split("_day_")[0]
                            key = f"{location_id_base}-{next_location_id_base}"
                            if key in prepared_data.matrixTime.segments:
                                segment = prepared_data.matrixTime.segments[key]
                                travel_time = int(segment.duree)
                                # Ensure travel_mode is properly set from segment type
                                if hasattr(segment, "type") and segment.type:
                                    if isinstance(segment.type, str):
                                        # Map string values to TravelMode enum
                                        mode_mapping = {
                                            "walk": TravelMode.WALK,
                                            "transport": TravelMode.TRANSPORT,
                                            "virtuel": TravelMode.VIRTUAL,
                                            "public_transit": TravelMode.TRANSPORT,
                                            "walking": TravelMode.WALK,
                                            "transit": TravelMode.TRANSPORT,
                                        }
                                        travel_mode = mode_mapping.get(
                                            segment.type.lower(), TravelMode.WALK
                                        )
                                    else:
                                        travel_mode = segment.type
                                else:
                                    travel_mode = TravelMode.WALK

                            # Calculate travel start time (after visit ends)
                            travel_start = start_time + duration
                            travel_hours = travel_start // 60
                            travel_minutes = travel_start % 60
                            travel_start_str = (
                                f"{travel_hours:02d}:{travel_minutes:02d}"
                            )
                            logger.info(f"Adding transport step from {visit_id} to {next_location_id_base}, start={travel_start_str}, duration={travel_time}, mode={travel_mode}")
                            steps.append(
                                ItineraryStep(
                                    type="transport",
                                    **{"from": visit_id, "to": next_location_id_base},
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
        
        base_id = step.id
        if "_tw_" in step.id and "day_" in step.id:
            base_id = step.id.split("_day_")[0]
        spot = next((s for s in spots if s.id == base_id), None)
        if not spot:
            return StepScores(crowd_percentage=50.0, weather_percentage=50.0)

        # Calculate crowd score based on start time
        start_hour = int(step.start.split(":")[0])
        day_index = datetime.strptime(date, "%Y-%m-%d").weekday()
        date_obj = datetime.strptime(date, "%Y-%m-%d")
        weekday = date_obj.weekday()
        month = date_obj.month
        crowd_hour = calculate_crowd_hour_for_spot(spot, weekday, month)
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
    """Generate optimized LLM prompt for daily summary description."""

    def get_weather_summary(weather_percentage: float) -> str:
        """Convert weather percentage to descriptive summary."""
        if weather_percentage >= 80:
            return "ensoleillé"
        elif weather_percentage >= 60:
            return "nuageux"
        elif weather_percentage >= 40:
            return "mitigé"
        else:
            return "pluvieux"

    # Start building the optimized prompt
    prompt = f"""Rédige un paragraphe fluide et engageant (max 400 caractères) résumant une journée touristique.
Infos fournies :
• Date : {daily_itinerary.date}
• Ville : {city}
• Profil : {companions}
• Étapes :"""

    visit_spots = []
    transport_segments = []

    # Process steps to separate visits and transports
    for i, step in enumerate(daily_itinerary.steps):
        step_scores = calculate_step_scores(
            step, spots, weather_data, daily_itinerary.date, distance_matrix
        )

        if step.type == "visit":
            # Find spot name
            base_id = step.id
            if "_tw_" in step.id and "day_" in step.id:
                base_id = step.id.split("_day_")[0]
            spot = next((s for s in spots if s.id == base_id), None)
            spot_name = spot.name if spot else f"Spot {step.id}"

            end_time = calculate_end_time(step.start, step.duration_min)
            weather_summary = get_weather_summary(step_scores.weather_percentage)

            visit_spots.append(
                {
                    "name": spot_name,
                    "start": step.start,
                    "end": end_time,
                    "weather": weather_summary,
                    "crowd": int(step_scores.crowd_percentage),
                }
            )

        elif step.type == "transport":
            mode_french = {
                "walk": "marche",
                "public_transit": "transport en commun",
                "car": "voiture",
                "bike": "vélo",
            }.get(step.mode, step.mode)

            transport_segments.append(
                {"mode": mode_french, "duration": step.duration_min}
            )

    # Add visit spots and transport segments to prompt
    for i, visit in enumerate(visit_spots):
        prompt += f"\n○ {visit['name']}, {visit['start']}–{visit['end']}, météo : {visit['weather']}, affluence : {visit['crowd']} %"

        # Add transport info if there's a next segment
        if i < len(transport_segments):
            transport = transport_segments[i]
            prompt += (
                f"\n○ Transport : {transport['mode']}, {transport['duration']} min"
            )

    prompt += """

Ton :
• Décris ce qu'on fait et l'ambiance (vue, culture, détente…).
• Mets en avant météo agréable, faible affluence, rythme fluide.
• Adapte au profil : romantique (couple), ludique (famille), contemplatif (solo), vivant (amis).
• Pas de chiffres, transforme-les en sensations naturelles.
• Pas de "visite 1", enchaîne naturellement."""

    return prompt


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
    hourly_availability: Dict[str, List[str]],
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
        start_hour, start_min = map(int, hourly_availability[date][0].split(":"))
        current_time = start_hour * 60 + start_min
        
        end_hour, end_min = map(int, hourly_availability[date][1].split(":"))
        
        start_minutes = start_hour * 60 + start_min
        end_minutes = end_hour * 60 + end_min
        need_lunch = start_minutes <= 720 and end_minutes >= 840  # 12:00-14:00

        steps = []
        for i, spot in enumerate(day_spots):
            hours = current_time // 60
            minutes = current_time % 60
            start_time_str = f"{hours:02d}:{minutes:02d}"
            
            if need_lunch and current_time >= 720 and current_time <= 840:
                steps.append(
                    ItineraryStep(
                        type="lunch",
                        id=f"lunch_day_{day_idx}",
                        start=start_time_str,
                        duration_min=90,
                    )
                )
                current_time += 90
                need_lunch = False

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
                        # Filter out unreachable segments
                        if segment.duree < 99999 and segment.distance > 0:
                            travel_time = int(segment.duree)
                            travel_mode = segment.type
                        else:
                            logger.warning(
                                f"Skipping unreachable segment in fallback: {key}"
                            )
                            # Keep default values for unreachable segments

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
    
def parse_time_to_minutes(time_str: str) -> int:
    """
    Converts a time in 'HH:MM' format into minutes from  midnight.
    """
    try:
        parts = time_str.split(":")
        hours = int(parts[0])
        minutes = int(parts[1]) if len(parts) > 1 else 0
        return hours * 60 + minutes
    except (ValueError, IndexError):
        return 0 # Fallback
    
def parse_visit_duration(duration_str: str) -> int:
    """
    Parses a visit duration in text format to minutes.
    Supported formats: "2h30", "90min", "2h", "120", "HH:MM"
    Returns:
    Duration in minutes (default: 60 if format not recognized)
    """
    
    duration_str = duration_str.lower().strip()
    try:
        if ":" in duration_str:
            parts = duration_str.split(":")
            hours = int(parts[0])
            minutes = int(parts[1]) if len(parts) > 1 else 0
            return hours * 60 + minutes
        elif "h" in duration_str:
            parts = duration_str.split("h")
            hours = int(parts[0])
            minutes = int(parts[1]) if len(parts) > 1 and parts[1] else 0
            return hours * 60 + minutes
        elif "min" in duration_str:
            return int(duration_str.replace("min", ""))
        elif duration_str.isdigit():
            return int(duration_str)
        else:
            return 60 # Default 1 hour
    except (ValueError, IndexError):
        return 60
    
    
def get_spot_windows_for_day(spot, weekday: int) -> List[Tuple[int, int]]:
    """
    Retrieves the time windows for a location on a given day of the week.
    Arguments: spot: Spot object (Firebase structure) weekday: Day of the week (0=Monday, 6=Sunday)
    Returns:
    List of tuples (start_min, end_min). If no times are specified ÿ returns [(480, 1200)] (8am-8pm by default)
    """
    
    spot_windows = []
    if not spot.openHours or weekday >= len(spot.openHours):
        # No set hours ÿ consider open 8am-8pm by default
        return [(480, 1200)]
    
    day_data = spot.openHours[weekday]
    # Manage Firebase structure (dict or object)
    if isinstance(day_data, dict):
        hours_list = day_data.get("hours", [])
    else:
        hours_list = getattr(day_data, "hours", [])
        
    for period in hours_list:
        if isinstance(period, dict):
            open_time = period.get("start", "00:00")
            close_time = period.get("end", "23:59")
        else:
            open_time = getattr(period, "start", "00:00")
            close_time = getattr(period, "end", "23:59")
            
        open_min = parse_time_to_minutes(open_time)
        close_min = parse_time_to_minutes(close_time)
        spot_windows.append((open_min, close_min))
        
    # If no window found ÿ default 8am-8pm
    if not spot_windows:
        return [(480, 1200)]
    return spot_windows


def intersect_windows_with_user_availability(
    spot_windows: List[Tuple[int, int]],
    user_start_min: int,
    user_end_min: int,
    adjusted_duration: int
    ) -> List[Tuple[int, int]]:
    """
    Calculates the intersection of spot windows with user availability.
    Args:
    spot_windows: Spot opening windows
    user_start_min: Start available user (minutes)
    user_end_min: End available user (minutes)
    Machine Translated by Google
    adjusted_duration: Adjusted visit duration (minutes)
    Returns:
    Possible START windows for the visit
    """
    
    final_windows = []
    for spot_start, spot_end in spot_windows:
        # Début possible = max(ouverture spot, début dispo user)
        window_start = max(spot_start, user_start_min)
        # Fin possible = min(fermeture spot - durée, fin dispo user - durée)
        # Car la visite doit se terminer avant les deux limites
        window_end = min(spot_end - adjusted_duration, user_end_min - adjusted_duration)
        
        if window_start < window_end:
            final_windows.append((window_start, window_end))
            
    return final_windows

def optimise_travel(
    city: str,
    spots: List[Spot],
    travel_dates: List[str],
    hourly_availability: Dict[str, List[str]],
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
    logger.info(f"city: {city}, travel_dates: {travel_dates}, hourly_availability: {hourly_availability}, optimization_mode: {optimization_mode}, max_walk_time_per_segment_min: {max_walk_time_per_segment_min}, visit_pace: {visit_pace}, companions: {companions}, spots: {[spot.id for spot in spots]}")

    try:
        # Load base data
        distance_matrix = load_distance_matrix_from_db(city)
        logger.info(f"Loaded distance matrix with segments: {list(distance_matrix.segments.keys())}")
        weights_dict = load_optimisation_weights_from_db()
        weights = SolverInputWeights(**weights_dict)

        # Filter and score distance matrix
        filtered_distance_matrix = filter_distance_matrix(distance_matrix, spots)
        logger.info(f"Filtered distance matrix segments: {list(filtered_distance_matrix.segments.keys())}")
        distance_matrix_score = get_distance_matrix_score(filtered_distance_matrix)

        # Get weather data
        if (city.count("-") == 1):
            city = city.split("-")[0].lower()
        weather_data = get_city_weather_data(
            city, travel_dates, hourly_availability
        )

        # === USER AND SPOT DATA MANAGEMENT ===

        # Process user windows
        user_windows = process_user_windows(travel_dates, hourly_availability)

        # Determine lunch requirements
        lunch_requirements = determine_lunch_requirements(user_windows)

        # Process spot information
        solver_spots = process_spot_info(spots, travel_dates, hourly_availability, visit_pace)

        # Create lunch and depot info
        lunch_infos = create_lunch_info(lunch_requirements, user_windows)
        depot_infos = create_depot_info()

        # Extract weather scores
        weather_scores = extract_weather_score_hours(
            weather_data, travel_dates, hourly_availability
        )

        # Combine all locations
        all_locations = depot_infos + solver_spots + lunch_infos

        # Validate that we have enough data
        if not all_locations or len(solver_spots) == 0:
            logger.warning("No valid spots found, using fallback solution")
            fallback_output = create_fallback_solution(
                spots,
                travel_dates,
                hourly_availability,
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
                hourly_availability,
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
                hourly_availability,
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
