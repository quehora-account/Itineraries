from fastapi import APIRouter
from typing import List
from app.models import UserPreferences, MatchedSpot
from app.services.utils import simulate_text_embedding, cosine_similarity
from app.services.firestore_service import (
    get_all_spots_from_db,
    update_spot_embedding_in_db,
    Spot as SpotFromDB,
)  #

spots_router = APIRouter(prefix="/spots", tags=["Spot Selection"])


@spots_router.post("/select", response_model=List[MatchedSpot])
async def select_spots_endpoint(preferences: UserPreferences):
    pref_text = f"Destination: {preferences.destination}, Activities: {', '.join(preferences.inspiring_activity_types)}, Pace: {preferences.visit_pace.value}"
    user_emb = simulate_text_embedding(pref_text)  # Using util version

    all_spots = await get_all_spots_from_db()

    matched_spots_list = []
    for spot_obj in all_spots:
        if spot_obj.embedding is None:
            spot_text_to_encode = (
                f"{spot_obj.name}, {spot_obj.description}, {spot_obj.type.value}"
            )
            spot_obj.embedding = simulate_text_embedding(
                spot_text_to_encode
            )  # Using util version
            await update_spot_embedding_in_db(spot_obj.id, spot_obj.embedding)

        similarity = cosine_similarity(
            user_emb, spot_obj.embedding
        )  # Using util version
        normalized_popularity = min(spot_obj.popularity_score / 2_500_000, 1.0)
        final_score = 0.8 * similarity + 0.2 * normalized_popularity

        matched_spots_list.append(
            MatchedSpot(
                spot=spot_obj,  # spot_obj is already a Spot model instance
                final_score=final_score,
                similarity_score=similarity,
                normalized_popularity=normalized_popularity,
            )
        )

    top_spots = sorted(matched_spots_list, key=lambda x: x.final_score, reverse=True)[
        : 15 * len(preferences.travel_dates)
    ]
    return top_spots
