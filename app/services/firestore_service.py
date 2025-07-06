import firebase_admin
import json
from firebase_admin import credentials, firestore
from app.models import (
    Spot,
    SpotBase,
    Playlist,
    GeoPoint,
    MatrixTime,
    MatrixScoreDistance,
)
from app.core.config import settings
from typing import List, Optional, Dict
from google.cloud.firestore_v1.vector import Vector
from google.cloud.firestore_v1._helpers import GeoPoint as FirestoreGeoPoint

if not firebase_admin._apps:
    cred = credentials.Certificate(
        "./hoora-fb944-firebase-adminsdk-hykdj-96b7eea9ff.json"
    )
    firebase_admin.initialize_app(cred)

store = firestore.client()


def get_spot_from_db(spot_id: str) -> Optional[Spot]:
    doc_ref = store.collection(settings.SPOTS_COLLECTION).document(spot_id)
    doc = doc_ref.get()
    if doc.exists:
        spot_data = doc.to_dict()
        spot_data["id"] = doc.id
        if isinstance(spot_data.get("coordinates"), FirestoreGeoPoint):
            spot_data["coordinates"] = GeoPoint(
                latitude=spot_data["coordinates"].latitude,
                longitude=spot_data["coordinates"].longitude,
            )
        return Spot(**spot_data)
    return None


def get_all_spots_from_db() -> List[Spot]:
    spots_list = []

    docs_snapshot = store.collection(settings.SPOTS_COLLECTION).get()
    for doc in docs_snapshot:
        spot_data = doc.to_dict()
        spot_data["id"] = doc.id
        if isinstance(spot_data.get("coordinates"), FirestoreGeoPoint):
            spot_data["coordinates"] = GeoPoint(
                latitude=spot_data["coordinates"].latitude,
                longitude=spot_data["coordinates"].longitude,
            )
        try:
            spots_list.append(Spot(**spot_data))
        except Exception as e:
            print(f"Error parsing spot {spot_data['id']}: {e}")
            continue
    return spots_list


def get_all_playlists_from_db() -> List[Playlist]:
    playlists_list = []
    docs_snapshot = store.collection(settings.PLAYLISTS_COLLECTION).get()
    for doc in docs_snapshot:
        playlist_data = doc.to_dict()
        playlist_data["id"] = doc.id
        playlists_list.append(Playlist(**playlist_data))
    return playlists_list


async def add_spot_to_db(spot_data: SpotBase) -> Spot:
    doc_ref = store.collection(settings.SPOTS_COLLECTION).document()
    await doc_ref.set(spot_data.model_dump())
    return Spot(id=doc_ref.id, **spot_data.model_dump())


async def update_spot_embedding_in_db(spot_id: str, embedding: Vector):
    doc_ref = store.collection(settings.SPOTS_COLLECTION).document(spot_id)
    doc_ref.update({"embedding": embedding})


def save_distance_matrices_to_db(
    cities_matrix: Dict[str, MatrixTime],
):
    """Save the computed distance matrices to the database."""
    for city, matrix_time in cities_matrix.items():
        doc_id = city

        # Convert to dictionary structure explicitly to ensure proper Firestore map format
        matrix_dict = {"segments": {}}

        for key, segment in matrix_time.segments.items():
            matrix_dict["segments"][key] = {
                "duree": segment.duree,
                "distance": segment.distance,
                "type": segment.type.value,  # Convert enum to string value
            }

        matrix_data = {
            "matrix_time": matrix_dict,
            "computed_at": firestore.SERVER_TIMESTAMP,
        }

        doc_ref = store.collection("distance_matrices").document(doc_id)
        doc_ref.set(matrix_data)
        print(f"Distance matrices saved to database with ID: {doc_id}")


def load_distance_matrix_from_db(spots: List[Spot]) -> MatrixTime:
    """Load the distance matrix from the database"""
    # Fixed document ID to match what's being saved
    doc_ref = store.collection("distance_matrices").document("distance_matrix")
    doc = doc_ref.get()

    if not doc.exists:
        raise ValueError("Distance matrix not found in database")

    return MatrixTime(**doc.to_dict()["matrix_time"])
