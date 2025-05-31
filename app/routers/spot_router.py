from fastapi import APIRouter
from typing import List
from app.models import UserPreferences, MatchedSpot
from app.services.utils import get_embedding, cosine_similarity
from app.services.firestore_service import (
    get_all_playlists_from_db,
    get_all_spots_from_db,
    update_spot_embedding_in_db,
)

spots_router = APIRouter(prefix="/spots", tags=["Spot Selection"])


@spots_router.post("/select", response_model=List[MatchedSpot])
async def select_spots_endpoint(preferences: UserPreferences):
    print('Computing user embedding')
    pref_text = f"Destination: {preferences.destination}, Activities: {', '.join(preferences.inspiring_activity_types)}, Pace: {preferences.visit_pace.value}"
    user_emb = get_embedding(pref_text)

    all_playlists_list = await get_all_playlists_from_db()
    all_playlists = {playlist.id: playlist for playlist in all_playlists_list}

    all_spots = await get_all_spots_from_db()

    matched_spots_list = []
    for spot_obj in all_spots:
        if True or spot_obj.embedding is None:
            print('Computing embedding for spot: ', spot_obj.name)
            playlist_labels = [all_playlists[playlist_id].name for playlist_id in spot_obj.playlistIds]

            spot_text_to_encode = (
                f"{spot_obj.name}, {spot_obj.description}, {spot_obj.type}, {', '.join(spot_obj.highlights)}, playlists: {', '.join(playlist_labels)}"
            )

            spot_obj.embedding = get_embedding(spot_text_to_encode)
            await update_spot_embedding_in_db(spot_obj.id, spot_obj.embedding)

        print('Computing similarity for spot: ', spot_obj.name)
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
    return top_spots
