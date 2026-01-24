import math
from typing import List
from app.models import (
    VisitPace,
)
from openai import OpenAI
from app.core.config import settings
from google.cloud.firestore_v1.vector import Vector

DEFAULT_TRANSPORT_MOYEN_MIN = 30
LUNCH_DURATION_MIN = 90

client = OpenAI(api_key=settings.OPENAI_API_KEY)


# Get embeddings in batch
def get_embeddings_batch(texts: List[str]) -> List[Vector]:
    response = client.embeddings.create(
        input=texts,
        model="text-embedding-3-small",
    )
    return [response.data[i].embedding for i in range(len(texts))]


def get_embedding(text: str) -> Vector:
    response = client.embeddings.create(
        input=text,
        model="text-embedding-3-small",
    )
    return response.data[0].embedding


def cosine_similarity(vec1: Vector, vec2: Vector) -> float:
    if len(vec1) != len(vec2) or not vec1 or not vec2:
        return 0.0
    dot_product = sum(p * q for p, q in zip(vec1, vec2))
    magnitude1 = math.sqrt(sum(p * p for p in vec1))
    magnitude2 = math.sqrt(sum(q * q for q in vec2))
    if magnitude1 == 0 or magnitude2 == 0:
        return 0.0
    return dot_product / (magnitude1 * magnitude2)


def get_duration_category(duration_min: int) -> str:
    if duration_min <= 30:
        return "≤ 30 min"
    if duration_min <= 120:
        return "30 min - 2h"
    if duration_min <= 180:
        return "2h-3h"
    return "> 3h"


def get_adjusted_visit_duration(standard_duration_min: int, pace: VisitPace) -> int:
    if standard_duration_min <= 30:
        return standard_duration_min
    adjustment = 0
    category = get_duration_category(standard_duration_min)
    if category == "30 min - 2h":
        if pace == VisitPace.FAST:
            adjustment = -15
        elif pace == VisitPace.RELAXED:
            adjustment = 15
    elif category == "2h-3h":
        if pace == VisitPace.FAST:
            adjustment = -30
        elif pace == VisitPace.RELAXED:
            adjustment = 30
    elif category == "> 3h":
        if pace == VisitPace.FAST:
            adjustment = -60
        elif pace == VisitPace.RELAXED:
            adjustment = 60
    return max(30, standard_duration_min + adjustment)


def parse_visit_duration_to_minutes(visit_duration: str) -> int:
    """Convert visitDuration string (e.g., '1:30') to minutes"""
    h, m = map(int, visit_duration.split(":"))
    return h * 60 + m


def time_str_to_minutes(time_str: str) -> int:
    h, m = map(int, time_str.split(":"))
    return h * 60 + m


def minutes_to_time_str(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def normalize_score(
    value: float, min_val: float, max_val: float, scale_to: float = 100.0
) -> float:
    if max_val == min_val:
        return scale_to / 2
    return ((value - min_val) / (max_val - min_val)) * scale_to


def calculate_time_gauge(hourly_availability: dict, visit_pace: VisitPace) -> tuple:
    total_time_available = 0
    n_days = len(hourly_availability)

    for date_str, (start_time, end_time) in hourly_availability.items():
        start_minutes = time_str_to_minutes(start_time)
        end_minutes = time_str_to_minutes(end_time)
        span = end_minutes - start_minutes

        lunch_deduction = 0
        if start_minutes <= 12 * 60 and end_minutes >= 14 * 60:
            lunch_deduction = 90

        total_time_available += span - lunch_deduction

    return total_time_available, n_days


def apply_visit_pace_adjustment(standard_duration_min: int, pace: VisitPace) -> int:
    if standard_duration_min <= 30:
        return standard_duration_min

    if 30 < standard_duration_min <= 120:
        if pace == VisitPace.FAST:
            return standard_duration_min - 15
        elif pace == VisitPace.RELAXED:
            return standard_duration_min + 15
        else:
            return standard_duration_min
    elif 120 < standard_duration_min <= 180:
        if pace == VisitPace.FAST:
            return standard_duration_min - 30
        elif pace == VisitPace.RELAXED:
            return standard_duration_min + 30
        else:
            return standard_duration_min
    else:
        if pace == VisitPace.FAST:
            return standard_duration_min - 60
        elif pace == VisitPace.RELAXED:
            return standard_duration_min + 60
        else:
            return standard_duration_min


def get_affluence_score(popular_time: int, density_index: int) -> float:
    if not (0 <= popular_time <= 100):
        popular_time = 50
    coefficient = density_index * 0.2
    return popular_time * coefficient


def generate_tarif_description(spot) -> str:
    """
    Génère une description enrichie des tarifs et du budget pour un spot.
    Note: Ne pas utiliser pour les embeddings, car le prix fausse la pertinence.
    """
    parts = []
    # Gratuit
    if spot.freePrice and spot.freePrice.price and spot.freePrice.price.strip() != "0":
        parts.append(
            f"Tarif gratuit : {spot.freePrice.price}€ {spot.freePrice.condition}"
        )
    elif spot.freePrice and spot.freePrice.condition:
        parts.append(f"Tarif gratuit pour {spot.freePrice.condition}")
    elif spot.freePrice:
        parts.append(f"Tarif gratuit")
    # Réduit
    if spot.reducedPrice and spot.reducedPrice.price:
        try:
            price_val = float(spot.reducedPrice.price.replace(",", "."))
            if price_val > 0:
                parts.append(
                    f"Tarif réduit : {spot.reducedPrice.price}€ {spot.reducedPrice.condition}"
                )
        except Exception:
            parts.append(
                f"Tarif réduit : {spot.reducedPrice.price} {spot.reducedPrice.condition}"
            )
    # Plein tarif
    full_price_val = None
    if spot.fullPrice and spot.fullPrice.price:
        try:
            full_price_val = float(spot.fullPrice.price.replace(",", "."))
            parts.append(
                f"Plein tarif : {spot.fullPrice.price}€ {spot.fullPrice.condition}"
            )
        except Exception:
            parts.append(
                f"Plein tarif : {spot.fullPrice.price} {spot.fullPrice.condition}"
            )
    # Catégorie budget
    budget_cat = None
    if full_price_val is not None:
        if full_price_val == 0:
            budget_cat = "Gratuit"
        elif 1 <= full_price_val <= 7:
            budget_cat = "Malin"
        elif 8 <= full_price_val <= 14:
            budget_cat = "Équilibré"
        elif full_price_val > 14:
            budget_cat = "Libre"
    if budget_cat:
        parts.append(f"Budget : {budget_cat}")
    return ". ".join(parts)
