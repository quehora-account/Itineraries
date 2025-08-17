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
    WALK = "walk"
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


class LocationType(str, Enum):
    INDOOR = "indoor"
    OUTDOOR = "outdoor"
    MIXED = "mixed"
