from typing import List, Dict, Any, Tuple
from datetime import datetime
from fastapi import HTTPException

from app.models import SpotUserPreferences, SimplifiedMatchedSpot, SpotSelectionResponse
from app.services.utils import (
    get_embedding,
    cosine_similarity,
    get_embeddings_batch,
    time_str_to_minutes,
    parse_visit_duration_to_minutes,
    apply_visit_pace_adjustment,
    calculate_time_gauge,
)
from app.services.firestore_service import (
    get_all_playlists_from_db,
    get_all_spots_from_db,
    update_spot_embedding_in_db,
)


def validate_activity_types(activity_types: List[str]) -> None:
    """Validate user's activity types against available playlists."""
    all_playlists_list = get_all_playlists_from_db()
    valid_activity_types = [playlist.name for playlist in all_playlists_list]

    for activity_type in activity_types:
        if activity_type not in valid_activity_types:
            raise HTTPException(
                status_code=400,
                detail=f"Activity type {activity_type} is not valid, valid activity types are: {valid_activity_types}",
            )


def filter_spots_by_city(destination: str) -> List[Any]:
    """Filter spots by destination city."""
    print("Getting all spots")
    all_spots = get_all_spots_from_db()

    filtered_spots = [
        spot
        for spot in all_spots
        if spot.cityId.lower().find(destination.lower()) != -1
    ]

    if not filtered_spots:
        raise HTTPException(
            status_code=400,
            detail=f"No spots found for destination {destination}",
        )

    return filtered_spots


def check_spot_availability_for_dates(
    spot: Any,
    travel_dates: List[str],
    hourly_availability: Dict[str, Tuple[str, str]],
    visit_pace,
) -> bool:
    """Check if a spot is available on any travel date with sufficient time overlap."""
    standard_duration_min = parse_visit_duration_to_minutes(spot.visitDuration)
    visit_duration_min = apply_visit_pace_adjustment(standard_duration_min, visit_pace)

    for date_str in travel_dates:
        date_obj = datetime.strptime(date_str, "%Y-%m-%d")
        weekday = date_obj.weekday()

        # Check if spot is open on this weekday
        if weekday >= len(spot.openHours) or not spot.openHours[weekday].hours:
            continue

        # Get user's availability for this date
        start_time, end_time = hourly_availability[date_str]
        start_user_min = time_str_to_minutes(start_time)
        end_user_min = time_str_to_minutes(end_time)

        # Check if there's sufficient overlap for the visit
        if has_sufficient_time_overlap(
            spot.openHours[weekday].hours,
            start_user_min,
            end_user_min,
            visit_duration_min,
        ):
            return True

    return False


def has_sufficient_time_overlap(
    open_hours: List[Any],
    start_user_min: int,
    end_user_min: int,
    visit_duration_min: int,
) -> bool:
    """Check if there's sufficient time overlap between user availability and spot hours."""
    for hours in open_hours:
        open_start_min = time_str_to_minutes(hours.start)
        open_end_min = time_str_to_minutes(hours.end)

        # Calculate intersection between user availability and opening hours
        intersection_start = max(start_user_min, open_start_min)
        intersection_end = min(end_user_min, open_end_min)

        # Check if intersection exists and is sufficient for visit duration
        if intersection_start < intersection_end:
            intersection_duration = intersection_end - intersection_start
            if intersection_duration >= visit_duration_min:
                return True

    return False


def filter_spots_by_availability(
    spots: List[Any], preferences: SpotUserPreferences
) -> List[Any]:
    """Filter spots based on opening hours and user availability."""
    spots_with_valid_hours = []

    for spot in spots:
        if check_spot_availability_for_dates(
            spot,
            preferences.travel_dates,
            preferences.hourly_availability,
            preferences.visit_pace,
        ):
            spots_with_valid_hours.append(spot)

    if not spots_with_valid_hours:
        raise HTTPException(
            status_code=400,
            detail=f"No spots found for destination {preferences.destination} and dates {preferences.travel_dates}",
        )

    return spots_with_valid_hours


def filter_free_spots(
    spots: List[Any], destination: str, travel_dates: List[str]
) -> List[Any]:
    """Apply free-only filter if requested."""
    free_spots = [
        spot for spot in spots if spot.fullPrice and spot.fullPrice.price == "Gratuit"
    ]

    if not free_spots:
        raise HTTPException(
            status_code=400,
            detail=f"No free spots found for destination {destination} and dates {travel_dates}",
        )

    return free_spots


def generate_user_embedding(preferences: SpotUserPreferences) -> List[float]:
    """Create embedding from user preferences."""
    print("Computing user embedding")
    accompagnants = preferences.companions.value
    enfants_fragment = ", avec des enfants" if preferences.has_children else ""
    centres_interet = ", ".join(preferences.activity_types)

    pref_text = f"Je visite {accompagnants}{enfants_fragment}, et je m'intéresse à {centres_interet}."
    return get_embedding(pref_text)


async def ensure_spot_embeddings(spots: List[Any]) -> None:
    """Generate missing embeddings for spots."""
    all_playlists_list = get_all_playlists_from_db()
    all_playlists = {playlist.id: playlist for playlist in all_playlists_list}

    # Collect spots that need embeddings
    spots_to_update = []
    spot_texts = []

    for spot_obj in spots:
        if spot_obj.embedding is None:
            playlist_labels = [
                all_playlists[playlist_id].name for playlist_id in spot_obj.playlistIds
            ]
            spot_text_to_encode = f"{', '.join(playlist_labels)}, {spot_obj.name}, {spot_obj.type}, {spot_obj.description}, {', '.join(spot_obj.highlights)}"
            spots_to_update.append(spot_obj)
            spot_texts.append(spot_text_to_encode)

    if spots_to_update:
        print(f"Computing embeddings for {len(spots_to_update)} spots")
        embeddings = get_embeddings_batch(spot_texts)

        # Update spots with their embeddings
        for spot_obj, embedding in zip(spots_to_update, embeddings):
            spot_obj.embedding = embedding
            await update_spot_embedding_in_db(spot_obj.id, embedding)


def normalize_similarity_to_match_percent(
    similarity: float, min_similarity: float, max_similarity: float
) -> float:
    """Convert similarity score to [45, 95] range."""
    if max_similarity == min_similarity:
        return 70.0  # Middle of [45, 95]
    else:
        return (
            45
            + ((similarity - min_similarity) / (max_similarity - min_similarity)) * 50
        )


def calculate_spot_scores(
    spots: List[Any], user_embedding: List[float], visit_pace
) -> List[Dict[str, Any]]:
    """Calculate match percentages and prepare spot data."""
    # Calculate embedding similarities and normalize to match_percent [45, 95]
    similarities = [
        cosine_similarity(user_embedding, spot_obj.embedding) for spot_obj in spots
    ]
    min_similarity = min(similarities)
    max_similarity = max(similarities)

    # Create spots with normalized match_percent and popularity scores
    spots_with_scores = []
    for spot_obj, similarity in zip(spots, similarities):
        match_percent = normalize_similarity_to_match_percent(
            similarity, min_similarity, max_similarity
        )
        popularity_score = spot_obj.score

        spots_with_scores.append(
            {
                "spot": spot_obj,
                "match_percent": match_percent,
                "popularity_score": popularity_score,
                "visit_duration": apply_visit_pace_adjustment(
                    parse_visit_duration_to_minutes(spot_obj.visitDuration),
                    visit_pace,
                ),
            }
        )

    # Sort by popularity score (descending)
    spots_with_scores.sort(key=lambda x: x["popularity_score"], reverse=True)

    return spots_with_scores


def create_simplified_spot(spot_data: Dict[str, Any]) -> SimplifiedMatchedSpot:
    """Convert spot data to SimplifiedMatchedSpot model."""
    spot = spot_data["spot"]
    return SimplifiedMatchedSpot(
        id=spot.id,
        name=spot.name,
        type=spot.type,
        match_percent=spot_data["match_percent"],
        images=spot.imageGalleryPaths,
        rating=spot.rating,
        city=spot.cityId.replace("-city", ""),
        latitude=spot.coordinates.latitude,
        longitude=spot.coordinates.longitude,
    )


def apply_time_gauge_selection(
    spots_with_scores: List[Dict[str, Any]],
    hourly_availability: Dict[str, Tuple[str, str]],
    visit_pace,
) -> SpotSelectionResponse:
    """Select spots that fit within time constraints."""
    total_time_available, n_days = calculate_time_gauge(hourly_availability, visit_pace)

    included_spots = []
    time_remaining = total_time_available
    spots_included = 0

    for spot_data in spots_with_scores:
        visit_duration = spot_data["visit_duration"]

        # Add transport penalty after n_days spots
        transport_penalty = 30 if spots_included >= n_days else 0
        total_time_needed = visit_duration + transport_penalty

        if time_remaining >= total_time_needed:
            included_spots.append(create_simplified_spot(spot_data))
            time_remaining -= total_time_needed
            spots_included += 1

    return SpotSelectionResponse(
        included_spots=included_spots, time_remaining_end=time_remaining
    )


async def find_and_select_spots(
    preferences: SpotUserPreferences,
) -> SpotSelectionResponse:
    """Main function to find and select spots based on user preferences and time constraints."""
    # Step 1: Validate activity types
    validate_activity_types(preferences.activity_types)

    # Step 2: Filter spots by city
    filtered_spots = filter_spots_by_city(preferences.destination)

    # Step 3: Filter spots by availability
    spots_with_valid_hours = filter_spots_by_availability(filtered_spots, preferences)

    # Step 4: Apply free-only filter if requested
    if preferences.free_only:
        spots_with_valid_hours = filter_free_spots(
            spots_with_valid_hours, preferences.destination, preferences.travel_dates
        )

    # Step 5: Generate user embedding
    user_embedding = generate_user_embedding(preferences)

    # Step 6: Ensure all spots have embeddings
    await ensure_spot_embeddings(spots_with_valid_hours)

    # Step 7: Calculate spot scores and similarities
    spots_with_scores = calculate_spot_scores(
        spots_with_valid_hours, user_embedding, preferences.visit_pace
    )

    # Step 8: Apply time gauge selection
    return apply_time_gauge_selection(
        spots_with_scores, preferences.hourly_availability, preferences.visit_pace
    )
