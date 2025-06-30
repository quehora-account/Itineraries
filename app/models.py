from typing import List, Dict, Optional, Tuple, Any, Union, Sequence
from pydantic import BaseModel, Field
from enum import Enum
from datetime import datetime


class GeoPoint(BaseModel):
    latitude: float
    longitude: float


class TravelCompanion(str, Enum):
    SOLO = "solo"
    COUPLE = "couple"
    FRIENDS = "amis"
    FAMILY = "famille"


class VisitPace(str, Enum):
    RELAXED = "détendu"
    BALANCED = "équilibré"
    FAST = "rapide"


class TravelMode(str, Enum):
    WALK = "à pied"
    TRANSPORT = "transport"
    VIRTUAL = "virtuel"


class OptimizationMode(str, Enum):
    PREMIUM = "premium"
    FREEMIUM = "freemium"


class BudgetCategory(str, Enum):
    FREE = "Gratuit"
    SMART = "Budget malin"
    BALANCED = "Budget équilibré"
    UNLIMITED = "Budget libre"


class SpotUserPreferences(BaseModel):
    destination: str = Field(..., example="Paris")
    travel_dates: List[str] = Field(
        ...,
        min_items=1,
        max_items=3,
        description="List of dates in YYYY-MM-DD format",
        example=["2024-07-10", "2024-07-12"],
    )
    budget: Optional[BudgetCategory] = Field(None, example=BudgetCategory.BALANCED)
    companions: TravelCompanion = Field(..., example=TravelCompanion.COUPLE)
    has_children: bool = Field(..., example=False)
    activity_types: List[str] = Field(..., max_items=3, example=["Culture", "Histoire"])
    hourly_availability: Dict[str, Tuple[str, str]] = Field(
        ...,
        description="e.g., {'2025-07-15': ('09:00', '18:00')}",
        example={
            "2024-07-10": ("09:00", "18:00"),
            "2024-07-11": ("09:00", "18:00"),
            "2024-07-12": ("14:00", "18:00"),
        },
    )


class UserPreferences(SpotUserPreferences):
    max_walk_time_per_segment_min: int = Field(default=30, example=30)
    visit_pace: VisitPace = Field(..., example=VisitPace.BALANCED)


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


class LocationType(str, Enum):
    INDOOR = "indoor"
    OUTDOOR = "outdoor"
    MIXED = "mixed"


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
    locationType: LocationType = Field(..., example=LocationType.INDOOR)

    class Config:
        arbitrary_types_allowed = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }


class Spot(SpotBase):
    id: str
    embedding: Optional[Sequence[float]] = None

    class Config:
        from_attributes = True
        arbitrary_types_allowed = True
        json_encoders = {
            datetime: lambda v: v.isoformat(),
        }


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
    match_percent: int


class SimplifiedMatchedSpot(BaseModel):
    id: str
    name: str
    type: str
    score: float
    match_percent: int
    images: List[str]
    city: str
    rating: float


class AdjustedVisitDurationInput(BaseModel):
    standard_duration_min: int
    pace: VisitPace


class SpotTiming(BaseModel):
    spot: Spot
    date: str
    arrival_time: str  # HH:MM format
    departure_time: str  # HH:MM format
    visit_duration_min: int
    transport_time_min: int = 0  # Time to get to this spot from previous


class TimeGaugeStatus(BaseModel):
    total_available_time_min: int
    time_spent_min: int
    remaining_time_min: int
    can_add_more: bool
    spot_timings: List[SpotTiming] = []  # New field for detailed timing


class ValidationRequest(BaseModel):
    selected_spot_ids: List[str]
    user_preferences: UserPreferences


class ValidationResponse(BaseModel):
    message: str
    jauge_status_code: int
    current_total_duration_min: int
    allocated_visit_time_min: int


class TravelSegment(BaseModel):
    duree: float
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


# Simplified models for select-spots endpoint
class SimpleUserPreferences(BaseModel):
    user_time_available: int = Field(..., description="Total time available in minutes")
    visit_pace: VisitPace = Field(
        default=VisitPace.BALANCED, description="Visit pace, defaults to 'équilibré'"
    )
    n_days: int = Field(..., description="Number of days")
    transport_moyen: int = Field(
        default=30, description="Average transport time in minutes, defaults to 30"
    )


class SimplifiedSpot(BaseModel):
    id: str
    name: str
    type: str
    images: List[str]
    rating: float
    ville: str
    final_score: float


class SimpleTimeGauge(BaseModel):
    spots: List[SimplifiedSpot]
    time_remaining_after_visits: int


class SelectSpotsRequest(BaseModel):
    selected_spot_ids: List[str] = Field(
        ..., description="List of spot IDs to evaluate"
    )
    user_preferences: SimpleUserPreferences
