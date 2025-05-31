from typing import List, Dict, Optional, Tuple, Any, Union
from pydantic import BaseModel, Field, field_validator
from enum import Enum
from datetime import datetime
from google.cloud.firestore import GeoPoint
from google.cloud.firestore_v1.vector import Vector


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


class Hours(BaseModel):
    start: str
    end: str


class Tarification(BaseModel):
    price: str
    condition: str


class OpenHours(BaseModel):
    hours: List[Hours]


class ExceptionalOpenHours(BaseModel):
    date: datetime
    hours: List[Hours]


class LastCrowdReport(BaseModel):
    createdAt: datetime
    userId: str
    duration: str
    intensity: int


class BalancePremium(BaseModel):
    from_date: datetime
    to: datetime
    gem: int


class PulsePremium(BaseModel):
    from_date: datetime = Field(alias="from")
    to: datetime
    gem: int


class SpotBase(BaseModel):
    name: str
    fullPrice: Optional[Tarification]
    reducedPrice: Optional[Tarification]
    freePrice: Optional[Tarification]
    imageCardPath: str
    imageIsoPath: str
    imageGalleryPaths: List[str]
    visitDuration: str
    score: float
    type: str
    website: str
    description: str
    tips: List[str]
    highlights: List[str]
    coordinates: GeoPoint
    cityId: str
    circleRadius: float
    regionId: str
    playlistIds: List[str]
    density: List[float]
    exceptionalOpenHours: List[ExceptionalOpenHours]
    allCrowdReports: List[LastCrowdReport]
    openHours: List[OpenHours]
    popularTimes: List[Dict[str, float]]
    lastCrowdReport: Optional[LastCrowdReport] = None
    balancePremium: Optional[BalancePremium]
    pulsePremium: Optional[PulsePremium]
    rating: float

    @field_validator('coordinates', mode='before')
    @classmethod
    def validate_coordinates(cls, v):
        if isinstance(v, dict) and 'latitude' in v and 'longitude' in v:
            return GeoPoint(v['latitude'], v['longitude'])
        return v

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }

class Spot(SpotBase):
    id: str
    embedding: Optional[Vector] = None

    class Config:
        from_attributes = True
        arbitrary_types_allowed = True

class Playlist(BaseModel):
    id: str
    imageGreen: str
    imagePath: str
    name: str
    priority: int


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
