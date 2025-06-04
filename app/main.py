from fastapi import FastAPI

from .routers.spot_router import spots_router
from .routers.itinerary_router import itinerary_router
from .routers.data_prep_router import data_prep_router
from .routers.generation_router import generation_router

app = FastAPI(
    title="Travel Itinerary Planner API (Firestore Refactored)",
    description="API for planning travel itineraries, using Firestore and organized structure.",
    version="1.1.0",
)

app.include_router(spots_router, prefix="/api")
app.include_router(itinerary_router, prefix="/api")
app.include_router(data_prep_router, prefix="/api")
app.include_router(generation_router, prefix="/api")
