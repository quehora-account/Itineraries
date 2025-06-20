import math
from app.models import TravelMode, Spot, MatrixTime, MatrixScoreDistance, TravelSegment
from typing import Tuple, List
from app.services.utils import normalize_score
import osmnx as ox
import networkx as nx


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
    for i in range(len(spots)):
        for j in range(len(spots)):
            if i == j:
                continue
            spot_a, spot_b = spots[i], spots[j]
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

            G = ox.graph_from_point(
                (spot_a.coordinates.latitude, spot_a.coordinates.longitude),
                dist=5000,  # 5km radius should be enough for most city distances
                network_type="drive",
            )

            orig_node = ox.nearest_nodes(
                G, spot_a.coordinates.longitude, spot_a.coordinates.latitude
            )
            dest_node = ox.nearest_nodes(
                G, spot_b.coordinates.longitude, spot_b.coordinates.latitude
            )

            try:
                route = nx.shortest_path(G, orig_node, dest_node, weight="length")
                route_length = nx.path_weight(G, route, weight="length") / 1000
            except nx.NetworkXNoPath:
                # If no path found, fallback to haversine distance
                route_length = raw_distances_km[key]

            real_distances_km[key] = route_length
            print(
                f"Raw distance between {spot_a.id} and {spot_b.id}: {raw_distances_km[key]} km"
            )
            print(
                f"Real distance between {spot_a.id} and {spot_b.id}: {route_length} km"
            )

    matrix_score_distance_scores = {}
    if raw_distances_km:
        d_min, d_max = min(raw_distances_km.values()), max(raw_distances_km.values())
        for key, dist_km in raw_distances_km.items():
            matrix_score_distance_scores[key] = normalize_score(dist_km, d_min, d_max)

    return MatrixTime(segments=matrix_time_segments), MatrixScoreDistance(
        scores=matrix_score_distance_scores
    )
