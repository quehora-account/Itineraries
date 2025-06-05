from fastapi import APIRouter, HTTPException, Path
from typing import List
from app.models import UserPreferences, MatchedSpot
from app.services.utils import get_embedding, cosine_similarity, get_embeddings_batch, time_str_to_minutes
from app.services.firestore_service import (
    get_all_playlists_from_db,
    get_all_spots_from_db,
    update_spot_embedding_in_db,
    get_spot_from_db,
)

spots_router = APIRouter(prefix="/spots", tags=["Spot Selection"])


@spots_router.post("/select", response_model=List[MatchedSpot])
async def select_spots_endpoint(preferences: UserPreferences):
    all_playlists_list = get_all_playlists_from_db()
    valid_activity_types = [playlist.name for playlist in all_playlists_list]

    for activity_type in preferences.activity_types:
        if activity_type not in valid_activity_types:
            raise HTTPException(status_code=400, detail=f"Activity type {activity_type} is not valid, valid activity types are: {valid_activity_types}")

    print("Getting all spots")
    all_spots = get_all_spots_from_db()

    # Filter spots by city
    filtered_spots = [spot for spot in all_spots if spot.cityId.lower().find(preferences.destination.lower())]
    if not filtered_spots:
        raise HTTPException(status_code=400, detail=f"No spots found for destination {preferences.destination}")

    # Filter spots by opening hours for the selected dates
    spots_with_valid_hours = []
    for spot in filtered_spots:
        is_valid = True
        for date_str in preferences.travel_dates:
            start_time, end_time = preferences.hourly_availability[date_str]
            start_min = time_str_to_minutes(start_time)
            end_min = time_str_to_minutes(end_time)
            
            # Check if spot has any open hours that overlap with user's availability
            has_overlap = False
            for open_hours in spot.openHours:
                for hours in open_hours.hours:
                    open_start = time_str_to_minutes(hours.start)
                    open_end = time_str_to_minutes(hours.end)
                    if open_start <= end_min and open_end >= start_min:
                        has_overlap = True
                        break
                if has_overlap:
                    break
            
            if not has_overlap:
                is_valid = False
                break
        
        if is_valid:
            spots_with_valid_hours.append(spot)

    if not spots_with_valid_hours:
        raise HTTPException(status_code=400, detail=f"No spots found for destination {preferences.destination} and dates {preferences.travel_dates}")

    print("Computing user embedding")
    pref_text = f"Destination: {preferences.destination}, Activities: {', '.join(preferences.activity_types)}, Pace: {preferences.visit_pace.value}"
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
            price_info = ""
            if spot_obj.fullPrice:
                price_info = f", Price: {spot_obj.fullPrice.price} ({spot_obj.fullPrice.condition})"
            spot_text_to_encode = f"{spot_obj.name}, {spot_obj.description}, {spot_obj.type}, {', '.join(spot_obj.highlights)}, playlists: {', '.join(playlist_labels)}{price_info}"
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
    for spot_obj in spots_with_valid_hours:
        similarity = cosine_similarity(user_emb, spot_obj.embedding)
        normalized_popularity = min(spot_obj.score / 2_500_000, 1.0)
        final_score = 0.8 * similarity + 0.2 * normalized_popularity

        matched_spots_list.append(
            MatchedSpot(
                spot=spot_obj,
                final_score=final_score,
                similarity_score=similarity,
                normalized_popularity=normalized_popularity,
            )
        )

    top_spots = sorted(matched_spots_list, key=lambda x: x.final_score, reverse=True)[
        : 15 * len(preferences.travel_dates)
    ]

    for spot in top_spots:
        del spot.spot.embedding

    return top_spots


@spots_router.post("/{spot_id}/compute-embedding")
async def compute_spot_embedding(spot_id: str = Path(..., example="0D75969QWlcaWyUJtNMn")):
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
    price_info = ""
    if spot.fullPrice:
        price_info = f", Price: {spot.fullPrice.price} ({spot.fullPrice.condition})"
    spot_text_to_encode = f"{spot.name}, {spot.description}, {spot.type}, {', '.join(spot.highlights)}, playlists: {', '.join(playlist_labels)}{price_info}"
    embedding = get_embedding(spot_text_to_encode)
    await update_spot_embedding_in_db(spot.id, embedding)
    spot.embedding = embedding
    return spot
