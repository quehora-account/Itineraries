from app.models import TravelMode, Spot, MatrixTime, TravelSegment, MatrixScoreDistance
from typing import Tuple, List, Dict, Any
import requests
from enum import Enum


class GeoapifyMode(str, Enum):
    WALK = "walk"
    DRIVE = "drive"
    CYCLE = "cycle"
    PUBLIC_TRANSPORT = "transit"


def get_cities_matrix(spots: List[Spot]) -> Dict[str, MatrixTime]:
    """Get the matrix for all cities"""

    # Group spots by city for better organization
    spots_by_city = {}
    for spot in spots:
        # Filter wrong city ids
        if spot.cityId not in spots_by_city:
            spots_by_city[spot.cityId] = []
        spots_by_city[spot.cityId].append(spot)

    cities_matrix = {}
    for city_id, city_spots in spots_by_city.items():
        print(f"Getting city matrix for {city_id} with {len(city_spots)} spots")
        if len(city_spots) > 1:
            cities_matrix[city_id] = get_city_matrix(city_spots)

    return cities_matrix


def get_city_matrix(spots: List[Spot]) -> List[Dict[str, Any]]:
    """Get the matrix for a city"""
    travels = []
    for i in range(len(spots) - 1):
        travel = {
            "source": {
                "id": spots[i].id,
                "coords": {
                    "lat": spots[i].coordinates.latitude,
                    "lng": spots[i].coordinates.longitude,
                },
            },
            "destinations": [],
        }
        for j in range(i + 1, len(spots)):
            travel["destinations"].append(
                {
                    "id": spots[j].id,
                    "coords": {
                        "lat": spots[j].coordinates.latitude,
                        "lng": spots[j].coordinates.longitude,
                    },
                }
            )
        travels.append(travel)
    return travels


def calculate_travel_time_matrix_batch(
    city_matrix: List[Dict[str, Any]], max_walk_time_per_segment_min: int = 30
) -> Dict[str, TravelSegment]:
    """
    Calculate travel time matrix using TravelTime API's many-to-one batch requests.
    For each destination spot, get travel times from all other spots to minimize API calls.
    """
    app_id = "2fb43759"
    api_key = "3dc56850237343f7ca9ddaa97e2be710"

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Application-Id": app_id,
        "X-Api-Key": api_key,
    }

    url = "https://api.traveltimeapp.com/v4/time-filter/fast"

    matrix_time_segments = {}

    # Process each city
    for city in city_matrix:
        city_travel = city_matrix[city]

        for travel in city_travel:
            print(
                f"Processing city: {travel['source']['id']} with {len(travel['destinations'])} spots"
            )

            # Build locations array from city_travel data
            locations = []

            # Add source location
            locations.append(
                {
                    "id": travel["source"]["id"],
                    "coords": travel["source"]["coords"],
                }
            )

            # Add all destination locations
            for dest in travel["destinations"]:
                locations.append({"id": dest["id"], "coords": dest["coords"]})

            # For each spot in the city, create a many-to-one request
            for destination_spot in travel["destinations"]:
                # Get all other spots as departure locations
                departure_spot_ids = [
                    loc["id"]
                    for loc in locations
                    if loc["id"] != destination_spot["id"]
                ]

                if not departure_spot_ids:
                    continue

                # Create the many-to-one request
                data = {
                    "arrival_searches": {
                        "many_to_one": [
                            {
                                "id": f"to_{destination_spot['id']}",
                                "departure_location_ids": departure_spot_ids,
                                "arrival_location_id": destination_spot["id"],
                                "transportation": {"type": "walking+ferry"},
                                "travel_time": max_walk_time_per_segment_min
                                * 60,  # Convert to seconds
                                "arrival_time_period": "weekday_morning",
                                "properties": ["travel_time", "distance"],
                            }
                        ]
                    },
                    "locations": locations,
                }

                try:
                    response = requests.post(url, headers=headers, json=data)
                    response.raise_for_status()
                    result = response.json()

                    # Process the results
                    if "results" in result:
                        for search_result in result["results"]:
                            if (
                                search_result["search_id"]
                                == f"to_{destination_spot['id']}"
                            ):
                                # Process reachable locations
                                for location in search_result.get("locations", []):
                                    departure_spot_id = location["id"]
                                    travel_time = location["properties"]["travel_time"]

                                    # Determine travel mode based on time
                                    if travel_time > max_walk_time_per_segment_min * 60:
                                        mode = TravelMode.TRANSPORT
                                    else:
                                        mode = TravelMode.WALK

                                    # Store both directions (A->B and B->A)
                                    matrix_time_segments[
                                        f"{departure_spot_id}-{destination_spot['id']}"
                                    ] = TravelSegment(duree=travel_time, type=mode)
                                    matrix_time_segments[
                                        f"{destination_spot['id']}-{departure_spot_id}"
                                    ] = TravelSegment(duree=travel_time, type=mode)

                                # Process unreachable locations
                                for unreachable_id in search_result.get(
                                    "unreachable", []
                                ):
                                    # Set high travel time for unreachable locations
                                    matrix_time_segments[
                                        f"{unreachable_id}-{destination_spot['id']}"
                                    ] = TravelSegment(
                                        duree=999999, type=TravelMode.TRANSPORT
                                    )
                                    matrix_time_segments[
                                        f"{destination_spot['id']}-{unreachable_id}"
                                    ] = TravelSegment(
                                        duree=999999, type=TravelMode.TRANSPORT
                                    )

                    print(f"Processed destination: {destination_spot['id']}")

                except Exception as e:
                    print(f"Error processing destination {destination_spot['id']}: {e}")
                    # Fallback: set default values for this destination
                    for loc in locations:
                        if loc["id"] != destination_spot["id"]:
                            matrix_time_segments[
                                f"{loc['id']}-{destination_spot['id']}"
                            ] = TravelSegment(
                                duree=1800, type=TravelMode.WALK  # Default 30 minutes
                            )
                            matrix_time_segments[
                                f"{destination_spot['id']}-{loc['id']}"
                            ] = TravelSegment(duree=1800, type=TravelMode.WALK)

    return matrix_time_segments


def get_distance_matrix(
    spots: List[Spot], max_walk_time_per_segment_min: int = 30
) -> Tuple[MatrixTime, MatrixScoreDistance]:
    """
    Compute the travel time and distance score matrices for a list of spots.
    Uses TravelTime API's many-to-one batch requests to minimize API calls.
    """
    cities_matrix = get_cities_matrix(spots)
    print(cities_matrix)

    matrix_time_segments = calculate_travel_time_matrix_batch(
        cities_matrix, max_walk_time_per_segment_min
    )

    return matrix_time_segments


def load_distance_matrix(spots: List[Spot]):
    """Load the distance matrix from the database"""
    matrix_time, matrix_score_distance = get_distance_matrix(spots)
    return matrix_time, matrix_score_distance
