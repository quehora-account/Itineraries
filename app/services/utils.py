import math
from app.models import (
    VisitPace,
)
from openai import OpenAI
from app.core.config import settings
from google.cloud.firestore_v1.vector import Vector

DEFAULT_TRANSPORT_MOYEN_MIN = 30
LUNCH_DURATION_MIN = 90

client = OpenAI(api_key=settings.OPENAI_API_KEY)

def get_embedding(
    text: str
) -> Vector:
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
        return 30
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

def calculate_crowd_score_brut(popular_time: int, density_index: int) -> float:
    if not (0 <= popular_time <= 100):
        popular_time = 50
    coefficient = density_index * 0.2
    return popular_time * coefficient
