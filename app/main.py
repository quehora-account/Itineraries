from fastapi import FastAPI
from fastapi_utilities import repeat_at

from .routers.spot_router import spots_router
from .routers.itinerary_router import itinerary_router
from .routers.optimisation_router import optimisation_router
from .routers.data_prep_router import data_prep_router
from .services.weather import update_daily_weather_data

app = FastAPI(
    title="Travel Itinerary Planner API (Firestore Refactored)",
    description="API for planning travel itineraries, using Firestore and organized structure.",
    version="1.1.0",
)

app.include_router(data_prep_router, prefix="/api")
app.include_router(spots_router, prefix="/api")
app.include_router(itinerary_router, prefix="/api")
app.include_router(optimisation_router, prefix="/api")


@repeat_at(cron="0 6 * * *")
def update_weather_data():
    update_daily_weather_data()


@app.on_event("startup")
def startup_event():
    update_weather_data()
