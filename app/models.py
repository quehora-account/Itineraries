from typing import List, Dict, Optional, Tuple, Any, Union
from pydantic import BaseModel, Field
from enum import Enum


class TravelCompanion(str, Enum):
    SOLO = "solo"
    COUPLE = "couple"
    FRIENDS = "amis"
    FAMILY = "famille"


class VisitPace(str, Enum):
    RELAXED = "détendu"
    BALANCED = "équilibré"
    FAST = "rapide"


class ActivityType(str, Enum):
    NATURE = "nature"
    CULTURE = "culturel"
    HISTORICAL = "historique"
    BEACH = "plage"
    FOOD = "gastronomie"
    SHOPPING = "shopping"
    NIGHTLIFE = "vie nocturne"
    ADVENTURE = "aventure"
    WELLNESS = "bien-être"
    ART = "art"


class TravelMode(str, Enum):
    WALK = "à pied"
    TRANSPORT = "transport"
    VIRTUAL = "virtuel"


class OptimizationMode(str, Enum):
    PREMIUM = "premium"
    FREEMIUM = "freemium"


# --- Pydantic Models (from original code, slightly adapted for Firestore if needed) ---
class UserPreferences(BaseModel):
    destination: str
    travel_dates: List[str] = Field(
        ..., min_items=1, max_items=3, description="List of dates in YYYY-MM-DD format"
    )
    budget: Optional[str] = None
    companions: TravelCompanion
    has_children: bool
    inspiring_activity_types: List[ActivityType] = Field(..., max_items=3)
    max_walk_time_per_segment_min: int = Field(default=30)
    hourly_availability: Dict[str, Tuple[str, str]] = Field(
        ..., description="e.g., {'2025-07-15': ('09:00', '18:00')}"
    )
    visit_pace: VisitPace
    lunch_break_required: bool = Field(default=True)


class SpotHighlight(BaseModel):
    name: str
    description: Optional[str] = None


class SpotBase(BaseModel):  # Base model for creation/update, ID handled by Firestore
    name: str
    description: str
    type: ActivityType
    highlights: List[SpotHighlight] = []
    playlist_labels: List[str] = []
    address: Optional[str] = None
    latitude: float
    longitude: float
    embedding: Optional[List[float]] = None
    popularity_score: float = Field(
        default_factory=lambda: random.uniform(0, 5_000_000)
    )
    standard_duration_min: int
    open_time_local: Optional[str] = None
    close_time_local: Optional[str] = None
    is_outdoor: bool = False
    popular_times_hourly: Optional[Dict[int, int]] = None  # Key: hour (0-23)
    density_index: Optional[int] = Field(None, ge=1, le=5)


class SpotCreate(SpotBase):
    pass


class Spot(SpotBase):  # Model for data retrieved from Firestore, includes ID
    id: str

    class Config:
        orm_mode = True  # For compatibility if ever used with ORMs, good practice
        arbitrary_types_allowed = True


class UserEmbedding(BaseModel):
    embedding: List[float]


class MatchedSpot(BaseModel):
    spot: Spot
    final_score: float
    similarity_score: float
    normalized_popularity: float


class AdjustedVisitDurationInput(BaseModel):
    standard_duration_min: int
    pace: VisitPace


class TimeGaugeStatus(BaseModel):
    total_available_time_min: int
    time_spent_min: int
    remaining_time_min: int
    selected_spots_for_day: List[Spot]
    can_add_more: bool


class ValidationRequest(BaseModel):
    selected_spot_ids: List[str]
    user_preferences: UserPreferences


class ValidationResponse(BaseModel):
    message: str
    jauge_status_code: int
    current_total_duration_min: int
    allocated_visit_time_min: int


class TravelSegment(BaseModel):
    duree: int
    type: TravelMode


class MatrixTime(BaseModel):
    segments: Dict[str, TravelSegment] = Field(default_factory=dict)


class MatrixScoreDistance(BaseModel):
    scores: Dict[str, float] = Field(default_factory=dict)


class WeatherHourlyData(BaseModel):
    temp_c: float
    precipitation_mm: float
    wind_kmh: float
    summary: Optional[str] = None
    emoji: Optional[str] = None
    temp_comfort_score: Optional[int] = None
    precip_comfort_score: Optional[int] = None
    wind_comfort_score: Optional[int] = None
    raw_weather_score: Optional[float] = None
    normalized_weather_score: Optional[float] = None


class WeatherForDate(BaseModel):
    hourly_data: Dict[str, WeatherHourlyData]


class CityWeatherData(BaseModel):
    city: str
    weather_by_date: Dict[str, WeatherForDate]


class CrowdScoreInput(BaseModel):
    popular_time: int
    density_index: int


class UserWindow(BaseModel):
    vehicle: int
    start: int
    end: int


class SolverInputWeights(BaseModel):
    crowd: float = 0.33
    weather: float = 0.33
    distance: float = 0.33


class SolverSpotInfo(BaseModel):
    id: str
    dur: int
    outdoor: bool
    time_windows: List[Tuple[int, int]]
    crowdHour: Optional[Dict[int, float]] = None
    ignoreMissingScore: bool = True


class SolverLunchInfo(BaseModel):
    id: str
    dur: int
    outdoor: bool = False
    vehicle: int
    time_windows: List[Tuple[int, int]]


class SolverDepotInfo(BaseModel):
    id: str = "depot"
    dur: int = 0
    outdoor: bool = False
    # time_windows might not be strictly needed for depot if start/end is on vehicle
    time_windows: List[Tuple[int, int]] = Field(default_factory=list)


class PreparedSolverData(BaseModel):
    mode: OptimizationMode
    numVehicles: int
    userWindows: List[UserWindow]
    vehicles: List[Dict[str, Any]]
    weights: SolverInputWeights
    lunchIndex: Optional[Dict[str, str]] = None
    weatherScoreHour: Optional[Dict[int, float]] = None
    matrixTime: MatrixTime
    matrixScoreDistance: MatrixScoreDistance
    locations: List[Union[SolverSpotInfo, SolverLunchInfo, SolverDepotInfo]]


class ItineraryStep(BaseModel):
    type: str
    id: Optional[str] = None
    start: Optional[str] = None
    duration_min: Optional[int] = None
    from_spot: Optional[str] = Field(None, alias="from")
    to_spot: Optional[str] = Field(None, alias="to")
    mode: Optional[TravelMode] = None


class DailyItinerary(BaseModel):
    day: int
    date: str
    daily_summary: Optional[str] = None
    steps: List[ItineraryStep]


class OptimizationScores(BaseModel):
    crowd: Optional[float] = None
    weather: Optional[float] = None
    distance: Optional[float] = None


class FinalItineraryOutput(BaseModel):
    days: List[DailyItinerary]
    scores: Optional[OptimizationScores] = None
