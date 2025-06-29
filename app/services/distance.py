import math
from app.models import TravelMode, Spot, MatrixTime, MatrixScoreDistance, TravelSegment
from typing import Tuple, List, Dict
import requests
from app.core.config import settings
from enum import Enum


class GeoapifyMode(str, Enum):
    WALK = "walk"
    DRIVE = "drive"
    CYCLE = "cycle"
    PUBLIC_TRANSPORT = "transit"


def calculate_single_route_distance(
    spot_a: Spot, spot_b: Spot, mode: GeoapifyMode = GeoapifyMode.PUBLIC_TRANSPORT
) -> Tuple[float, float]:
    """Calculate the distance between two spots using Geoapify API"""
    url = f"https://api.geoapify.com/v1/routing?waypoints={spot_a.coordinates.latitude}%2C{spot_a.coordinates.longitude}%7C{spot_b.coordinates.latitude}%2C{spot_b.coordinates.longitude}&mode={mode.value}&apiKey={settings.GEOAPIFY_API_KEY}"
    print(url)
    response = requests.get(url)
    response.raise_for_status()
    data = response.json()

    distance_meters = data["features"][0]["properties"]["distance"]
    time_seconds = data["features"][0]["properties"]["time"]
    return distance_meters, time_seconds


def get_distance_matrix(
    spots: List[Spot], max_walk_time_per_segment_min: int = 15
) -> Tuple[MatrixTime, MatrixScoreDistance]:
    """
    Compute the travel time and distance score matrices for a list of spots.
    If the walking time between two spots is less than 15 minutes, use walking as the mode.
    Otherwise, use public transport as the mode.
    """
    matrix_time_segments = {}

    # Group spots by city
    spots_by_city = {}
    for spot in spots:
        if spot.cityId not in spots_by_city:
            spots_by_city[spot.cityId] = []
        spots_by_city[spot.cityId].append(spot)

    # Create all possible pairs of spots and remove duplicates like (a, b) and (b, a)
    unique_pairs = set()
    spot_pairs = []

    for i in range(len(spots)):
        for j in range(
            i + 1, len(spots)
        ):  # Start from i+1 to avoid duplicates and self-pairs
            spot_a = spots[i]
            spot_b = spots[j]

            # Create a unique identifier for the pair (same for AB and BA)
            pair_id = tuple(sorted([spot_a.id, spot_b.id]))

            if pair_id not in unique_pairs:
                unique_pairs.add(pair_id)
                spot_pairs.append((spot_a, spot_b))

    # Iterate over all unique pairs and calculate the distance
    for spot_a, spot_b in spot_pairs:
        print(f"Calculating distance between {spot_a.name} and {spot_b.name}")
        distance_meters, time_seconds = calculate_single_route_distance(spot_a, spot_b)
        print(f"Distance between {spot_a.id} and {spot_b.id}: {distance_meters}")
        print(f"Time between {spot_a.id} and {spot_b.id}: {time_seconds}")
        matrix_time_segments[f"{spot_a.id}-{spot_b.id}"] = TravelSegment(
            duree=time_seconds, type=TravelMode.TRANSPORT
        )

    return MatrixTime(segments=matrix_time_segments), MatrixScoreDistance(scores={})
