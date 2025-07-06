from app.models import Spot, MatrixTime, TravelSegment
from app.enums import TravelMode
from typing import List, Dict, Any
import requests
from enum import Enum
import time

app_id = "2fb43759"
api_key = "3dc56850237343f7ca9ddaa97e2be710"

headers = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "X-Application-Id": app_id,
    "X-Api-Key": api_key,
}

url = "https://api.traveltimeapp.com/v4/time-filter/fast"


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


def get_travel_data(city, source, destinations, transportation="public_transport"):
    print(f"Processing city {city}: {source['id']} with {len(destinations)} spots")

    locations = [source, *destinations]

    data = {
        "arrival_searches": {
            "one_to_many": [
                {
                    "id": f"to_{source['id']}",
                    "departure_location_id": source["id"],
                    "arrival_location_ids": [
                        destination["id"] for destination in destinations
                    ],
                    "transportation": {"type": transportation},
                    "travel_time": 7200,
                    "arrival_time_period": "weekday_morning",
                    "properties": ["travel_time", "distance"],
                }
            ]
        },
        "locations": locations,
    }

    time.sleep(1)
    response = requests.post(url, headers=headers, json=data)
    response.raise_for_status()
    data = response.json()

    return data


def calculate_travel_time_matrix_batch(
    travel: Dict[str, Any],
    city,
    transportation: str = "public_transport",
    retry_count: int = 0,
) -> Dict[str, TravelSegment]:
    """
    Calculate travel time matrix using TravelTime API's many-to-one batch requests.
    For each destination spot, get travel times from all other spots to minimize API calls.
    """

    matrix_time_segments = {}

    source_spot = travel["source"]
    data = get_travel_data(
        city, travel["source"], travel["destinations"], transportation
    )

    for search_result in data["results"]:
        for location in search_result["locations"]:
            travel_time = location["properties"]["travel_time"]
            distance = location["properties"]["distance"]

            mode = TravelMode.TRANSPORT

            matrix_time_segments[f"{source_spot['id']}-{location['id']}"] = (
                TravelSegment(duree=travel_time, type=mode, distance=distance)
            )

            matrix_time_segments[f"{location['id']}-{source_spot['id']}"] = (
                TravelSegment(duree=travel_time, type=mode, distance=distance)
            )

        unreachables = search_result["unreachable"]
        unreachable_destinations = [
            destination
            for destination in travel["destinations"]
            if destination["id"] in unreachables
        ]
        if len(unreachable_destinations) > 0:
            print(
                f"Unreachable destinations for {source_spot['id']}: {unreachable_destinations}, retry count: {retry_count}"
            )

            if retry_count < 2:
                # Retry with walking mode
                new_travel = {
                    "source": source_spot,
                    "destinations": unreachable_destinations,
                }
                retry_segments = calculate_travel_time_matrix_batch(
                    new_travel,
                    city,
                    transportation="walking",
                    retry_count=retry_count + 1,
                )
                # Merge the retry results into the main matrix
                matrix_time_segments.update(retry_segments)
            else:
                # Max retries reached, set duree to 99999 for unreachable destinations
                print(
                    f"Max retries reached for unreachable destinations. Setting duree to 99999."
                )
                for destination in unreachable_destinations:
                    matrix_time_segments[f"{source_spot['id']}-{destination['id']}"] = (
                        TravelSegment(
                            duree=99999, type=TravelMode.TRANSPORT, distance=0
                        )
                    )
                    matrix_time_segments[f"{destination['id']}-{source_spot['id']}"] = (
                        TravelSegment(
                            duree=99999, type=TravelMode.TRANSPORT, distance=0
                        )
                    )

    return matrix_time_segments


def get_distance_matrix(spots: List[Spot]) -> MatrixTime:
    """
    Compute the travel time and distance score matrices for a list of spots.
    Uses TravelTime API's many-to-one batch requests to minimize API calls.
    """
    cities_matrix = get_cities_matrix(spots)

    # Initialize combined matrix_time_segments
    combined_matrix_time_segments = {}

    # Process each city
    for city in cities_matrix:
        city_travel = cities_matrix[city]

        for travel in city_travel:
            matrix_time_segments = calculate_travel_time_matrix_batch(travel, city)
            combined_matrix_time_segments.update(matrix_time_segments)

    return MatrixTime(segments=combined_matrix_time_segments)


def load_distance_matrix(spots: List[Spot]):
    """Load the distance matrix from the database"""
    matrix_time, matrix_score_distance = get_distance_matrix(spots)
    return matrix_time, matrix_score_distance
