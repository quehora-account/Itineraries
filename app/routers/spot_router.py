from fastapi import APIRouter
from app.models import SpotUserPreferences, SpotSelectionResponse
from app.services.spot_selection_service import find_and_select_spots


spots_router = APIRouter(prefix="/spots", tags=["Spots"])


@spots_router.post("/find", response_model=SpotSelectionResponse)
async def find_spots(preferences: SpotUserPreferences) -> SpotSelectionResponse:
    """
    Find and select spots based on user preferences and time constraints.

    This endpoint implements the complete spot selection pipeline:
    1. Validates user preferences
    2. Filters spots by location, availability, and pricing
    3. Calculates match percentages using embeddings
    4. Applies time gauge logic to select spots that fit within available time

    Returns selected spots with remaining time after visits and transport.
    """
    return await find_and_select_spots(preferences)
