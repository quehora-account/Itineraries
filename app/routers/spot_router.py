from fastapi import APIRouter, HTTPException, Path
from typing import List
import math
from datetime import datetime
from app.models import SpotUserPreferences, MatchedSpot, SimplifiedMatchedSpot
from app.services.utils import (
    get_embedding,
    cosine_similarity,
    get_embeddings_batch,
    time_str_to_minutes,
    generate_tarif_description,
    parse_visit_duration_to_minutes,
    get_adjusted_visit_duration,
)
from app.services.firestore_service import (
    get_all_playlists_from_db,
    get_all_spots_from_db,
    update_spot_embedding_in_db,
    get_spot_from_db,
)

spots_router = APIRouter(prefix="/spots", tags=["Spots"])


@spots_router.post("/find", response_model=List[SimplifiedMatchedSpot])
async def find_spots(preferences: SpotUserPreferences):
    all_playlists_list = get_all_playlists_from_db()
    valid_activity_types = [playlist.name for playlist in all_playlists_list]

    for activity_type in preferences.activity_types:
        if activity_type not in valid_activity_types:
            raise HTTPException(
                status_code=400,
                detail=f"Activity type {activity_type} is not valid, valid activity types are: {valid_activity_types}",
            )

    print("Getting all spots")
    all_spots = get_all_spots_from_db()

    # Filter spots by city
    filtered_spots = [
        spot
        for spot in all_spots
        if spot.cityId.lower().find(preferences.destination.lower()) != -1
    ]
    if not filtered_spots:
        raise HTTPException(
            status_code=400,
            detail=f"No spots found for destination {preferences.destination}",
        )

    # Filter spots by opening hours for the selected dates
    spots_with_valid_hours = []
    for spot in filtered_spots:
        is_valid = True

        # Calculate visit duration for this spot (assuming balanced pace as default)
        standard_duration_min = parse_visit_duration_to_minutes(spot.visitDuration)
        # Use balanced pace as default for filtering (can be adjusted later)
        from app.models import VisitPace

        visit_duration_min = get_adjusted_visit_duration(
            standard_duration_min, VisitPace.BALANCED
        )

        for date_str in preferences.travel_dates:
            # Parse date to get weekday (0 = Monday, 6 = Sunday)
            date_obj = datetime.strptime(date_str, "%Y-%m-%d")
            weekday = date_obj.weekday()  # 0 = Monday, 6 = Sunday

            # Get user's availability for this date
            start_time, end_time = preferences.hourly_availability[date_str]
            start_user_min = time_str_to_minutes(start_time)
            end_user_min = time_str_to_minutes(end_time)

            # Condition 1: Check if spot is open on this weekday
            if weekday >= len(spot.openHours) or not spot.openHours[weekday].hours:
                # Spot is closed on this day
                is_valid = False
                break

            # Condition 2: Check if there's sufficient overlap for the visit
            has_sufficient_overlap = False
            day_open_hours = spot.openHours[weekday].hours

            for hours in day_open_hours:
                open_start_min = time_str_to_minutes(hours.start)
                open_end_min = time_str_to_minutes(hours.end)

                # Calculate intersection between user availability and opening hours
                intersection_start = max(start_user_min, open_start_min)
                intersection_end = min(end_user_min, open_end_min)

                # Check if intersection exists and is sufficient for visit duration
                if intersection_start < intersection_end:
                    intersection_duration = intersection_end - intersection_start
                    if intersection_duration >= visit_duration_min:
                        has_sufficient_overlap = True
                        break

            if not has_sufficient_overlap:
                is_valid = False
                break

        if is_valid:
            spots_with_valid_hours.append(spot)

    if not spots_with_valid_hours:
        raise HTTPException(
            status_code=400,
            detail=f"No spots found for destination {preferences.destination} and dates {preferences.travel_dates}",
        )

    print("Computing user embedding")
    pref_text = f"Destination: {preferences.destination}, Activities: {', '.join(preferences.activity_types)}"
    if preferences.budget:
        pref_text += f", Budget: {preferences.budget.value}"
    user_emb = get_embedding(pref_text)

    all_playlists = {playlist.id: playlist for playlist in all_playlists_list}

    # Collect spots that need embeddings
    spots_to_update = []
    spot_texts = []
    for spot_obj in spots_with_valid_hours:
        if spot_obj.embedding is None:
            playlist_labels = [
                all_playlists[playlist_id].name for playlist_id in spot_obj.playlistIds
            ]
            tarif_desc = generate_tarif_description(spot_obj)
            spot_text_to_encode = f"{spot_obj.name}, {spot_obj.description}, {spot_obj.type}, {', '.join(spot_obj.highlights)}, playlists: {', '.join(playlist_labels)}. {tarif_desc}"
            spots_to_update.append(spot_obj)
            spot_texts.append(spot_text_to_encode)

    if spots_to_update:
        print(f"Computing embeddings for {len(spots_to_update)} spots")
        embeddings = get_embeddings_batch(spot_texts)

        # Update spots with their embeddings
        for spot_obj, embedding in zip(spots_to_update, embeddings):
            spot_obj.embedding = embedding
            await update_spot_embedding_in_db(spot_obj.id, embedding)

    matched_spots_list = []
    # Find max score for normalization
    max_score = max((spot_obj.score for spot_obj in spots_with_valid_hours), default=1)

    for spot_obj in spots_with_valid_hours:
        similarity = cosine_similarity(user_emb, spot_obj.embedding)
        # Use logarithmic normalization
        normalized_popularity = math.log(1 + spot_obj.score) / math.log(1 + max_score)
        final_score = 0.8 * similarity + 0.2 * normalized_popularity
        # Convert to percentage
        match_percent = round(final_score * 100)

        matched_spots_list.append(
            MatchedSpot(
                spot=spot_obj,
                final_score=final_score,
                similarity_score=similarity,
                normalized_popularity=normalized_popularity,
                match_percent=match_percent,
            )
        )

    # Calculate the actual number of days in the travel period
    start_date = datetime.strptime(preferences.travel_dates[0], "%Y-%m-%d")
    end_date = datetime.strptime(preferences.travel_dates[-1], "%Y-%m-%d")
    num_days = (
        end_date - start_date
    ).days + 1  # +1 to include both start and end dates

    top_spots = sorted(matched_spots_list, key=lambda x: x.final_score, reverse=True)[
        : 10 * num_days
    ]

    simplified_spots = []
    for spot in top_spots:
        simplified_spots.append(
            SimplifiedMatchedSpot(
                name=spot.spot.name,
                city=spot.spot.cityId.replace("-city", ""),
                type=spot.spot.type,
                score=spot.final_score,
                match_percent=spot.match_percent,
                images=spot.spot.imageGalleryPaths,
            )
        )

    return simplified_spots


@spots_router.post("/{spot_id}/compute-embedding")
async def compute_spot_embedding(
    spot_id: str = Path(..., example="0D75969QWlcaWyUJtNMn")
):
    spot = get_spot_from_db(spot_id)
    if not spot:
        raise HTTPException(status_code=404, detail="Spot not found")

    # Fetch playlists for label enrichment
    all_playlists_list = get_all_playlists_from_db()
    all_playlists = {playlist.id: playlist for playlist in all_playlists_list}
    playlist_labels = [
        all_playlists[playlist_id].name
        for playlist_id in spot.playlistIds
        if playlist_id in all_playlists
    ]
    tarif_desc = generate_tarif_description(spot)
    spot_text_to_encode = f"{spot.name}, {spot.description}, {spot.type}, {', '.join(spot.highlights)}, playlists: {', '.join(playlist_labels)}. {tarif_desc}"
    embedding = get_embedding(spot_text_to_encode)
    await update_spot_embedding_in_db(spot.id, embedding)
    spot.embedding = embedding
    return spot
