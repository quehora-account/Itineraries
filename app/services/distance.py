import math
from app.models import TravelMode, Spot, MatrixTime, MatrixScoreDistance, TravelSegment
from typing import Tuple, List, Dict
from app.services.utils import normalize_score
import osmnx as ox
import networkx as nx
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
import threading

# Thread-local storage for graphs to avoid conflicts
_thread_local = threading.local()


def get_thread_local_graphs():
    if not hasattr(_thread_local, "graphs"):
        _thread_local.graphs = {}
    return _thread_local.graphs


@lru_cache(maxsize=32)
def get_cached_graph(lat: float, lon: float, dist: int = 5000):
    """Cache graphs based on location to avoid redundant downloads"""
    # Round coordinates to reduce cache misses for nearby points
    rounded_lat = round(lat, 3)
    rounded_lon = round(lon, 3)

    graphs = get_thread_local_graphs()
    key = (rounded_lat, rounded_lon, dist)

    if key not in graphs:
        try:
            graphs[key] = ox.graph_from_point(
                (lat, lon),
                dist=dist,
                network_type="drive",
            )
        except Exception as e:
            print(f"Error downloading graph for ({lat}, {lon}): {e}")
            return None

    return graphs[key]


def calculate_single_route_distance(spot_a: Spot, spot_b: Spot) -> Tuple[str, float]:
    """Calculate the real distance between two spots using OSM routing"""
    key = f"{spot_a.id}-{spot_b.id}"

    # Calculate haversine distance as fallback
    raw_distance = haversine_distance_km(
        spot_a.coordinates.latitude,
        spot_a.coordinates.longitude,
        spot_b.coordinates.latitude,
        spot_b.coordinates.longitude,
    )

    try:
        # Use the midpoint to get the graph
        mid_lat = (spot_a.coordinates.latitude + spot_b.coordinates.latitude) / 2
        mid_lon = (spot_a.coordinates.longitude + spot_b.coordinates.longitude) / 2

        G = get_cached_graph(mid_lat, mid_lon)
        if G is None:
            return key, raw_distance

        orig_node = ox.nearest_nodes(
            G, spot_a.coordinates.longitude, spot_a.coordinates.latitude
        )
        dest_node = ox.nearest_nodes(
            G, spot_b.coordinates.longitude, spot_b.coordinates.latitude
        )

        route = nx.shortest_path(G, orig_node, dest_node, weight="length")
        route_length = nx.path_weight(G, route, weight="length") / 1000

        print(
            f"Raw distance between {spot_a.id} and {spot_b.id}: {raw_distance:.2f} km"
        )
        print(
            f"Real distance between {spot_a.id} and {spot_b.id}: {route_length:.2f} km"
        )

        return key, route_length

    except (nx.NetworkXNoPath, Exception) as e:
        print(f"Error calculating route between {spot_a.id} and {spot_b.id}: {e}")
        return key, raw_distance


def haversine_distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371
    lat1_rad, lon1_rad = math.radians(lat1), math.radians(lon1)
    lat2_rad, lon2_rad = math.radians(lat2), math.radians(lon2)
    dlon = lon2_rad - lon1_rad
    dlat = lat2_rad - lat1_rad
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.asin(math.sqrt(a))
    return R * c


def calculate_travel_time_min(distance_km: float, mode: TravelMode) -> int:
    if mode == TravelMode.WALK:
        return math.ceil(distance_km * 12)
    elif mode == TravelMode.TRANSPORT:
        return math.ceil(distance_km * 4)
    return 0


def get_travel_mode_and_time(
    spot_from: Spot, spot_to: Spot, max_walk_time_min: int
) -> Tuple[TravelMode, int]:
    """
    Get the travel mode and time between two spots.
    If the walk time is less than the max walk time, return the walk mode.
    Otherwise, return the transport mode.
    """
    distance_km = haversine_distance_km(
        spot_from.coordinates.latitude,
        spot_from.coordinates.longitude,
        spot_to.coordinates.latitude,
        spot_to.coordinates.longitude,
    )
    walk_time = calculate_travel_time_min(distance_km, TravelMode.WALK)
    if walk_time <= max_walk_time_min:
        return TravelMode.WALK, walk_time
    else:
        transport_time = calculate_travel_time_min(distance_km, TravelMode.TRANSPORT)
        return TravelMode.TRANSPORT, transport_time


def get_distance_matrix(
    spots: List[Spot], max_walk_time_per_segment_min: int
) -> Tuple[MatrixTime, MatrixScoreDistance]:
    matrix_time_segments = {}
    raw_distances_km = {}
    real_distances_km = {}

    # Group spots by city
    spots_by_city = {}
    for spot in spots:
        if spot.cityId not in spots_by_city:
            spots_by_city[spot.cityId] = []
        spots_by_city[spot.cityId].append(spot)

    # Prepare all spot pairs for parallel processing
    spot_pairs = []
    for city, city_spots in spots_by_city.items():
        for i in range(len(city_spots)):
            for j in range(len(city_spots)):
                if i == j:
                    continue
                spot_pairs.append((city_spots[i], city_spots[j]))

    # Calculate basic distances and travel modes first (fast operations)
    for spot_a, spot_b in spot_pairs:
        key = f"{spot_a.id}-{spot_b.id}"
        mode, time_min = get_travel_mode_and_time(
            spot_a, spot_b, max_walk_time_per_segment_min
        )
        matrix_time_segments[key] = TravelSegment(duree=time_min, type=mode)
        raw_distances_km[key] = haversine_distance_km(
            spot_a.coordinates.latitude,
            spot_a.coordinates.longitude,
            spot_b.coordinates.latitude,
            spot_b.coordinates.longitude,
        )

    # Calculate real distances in parallel (expensive operations)
    print(f"Calculating {len(spot_pairs)} route distances in parallel...")
    with ThreadPoolExecutor(max_workers=min(8, len(spot_pairs))) as executor:
        # Submit all route calculations
        future_to_pair = {
            executor.submit(calculate_single_route_distance, spot_a, spot_b): (
                spot_a,
                spot_b,
            )
            for spot_a, spot_b in spot_pairs
        }

        # Collect results as they complete
        for future in as_completed(future_to_pair):
            try:
                key, real_distance = future.result()
                real_distances_km[key] = real_distance
            except Exception as e:
                spot_a, spot_b = future_to_pair[future]
                print(f"Error processing route for {spot_a.id}-{spot_b.id}: {e}")
                # Use raw distance as fallback
                key = f"{spot_a.id}-{spot_b.id}"
                real_distances_km[key] = raw_distances_km[key]

    matrix_score_distance_scores = {}
    if raw_distances_km:
        d_min, d_max = min(raw_distances_km.values()), max(raw_distances_km.values())
        for key, dist_km in raw_distances_km.items():
            matrix_score_distance_scores[key] = normalize_score(dist_km, d_min, d_max)

    return MatrixTime(segments=matrix_time_segments), MatrixScoreDistance(
        scores=matrix_score_distance_scores
    )
