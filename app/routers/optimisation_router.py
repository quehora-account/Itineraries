from ortools.constraint_solver import pywrapcp, routing_enums_pb2
from fastapi import APIRouter
from app.models import (
    OptimizationMode,
    TimeGaugeStatus,
)

optimisation_router = APIRouter(prefix="/optimisation", tags=["Itinerary Optimisation"])


@optimisation_router.post("/optimise-itinerary")
async def optimise_itinerary_endpoint(
    data: TimeGaugeStatus,
    optimization_mode: OptimizationMode = OptimizationMode.FREEMIUM,
):
    return "Under construction"