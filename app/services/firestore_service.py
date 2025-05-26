import firebase_admin
from firebase_admin import credentials, firestore
from app.models import Spot, SpotCreate
from app.core.config import settings
from typing import List, Optional
from fastapi import HTTPException

if not firebase_admin._apps:
    try:
        firebase_admin.initialize_app()
        print("Firebase Admin SDK initialized using Application Default Credentials.")
    except Exception as e:
        try:
            cred = credentials.ApplicationDefault()
            firebase_admin.initialize_app(
                cred,
                {
                    "apiKey": settings.API_KEY,
                    "projectId": settings.PROJECT_ID,
                    "authDomain": settings.AUTH_DOMAIN,
                    "storageBucket": settings.STORAGE_BUCKET,
                    "messagingSenderId": settings.MESSAGING_SENDER_ID,
                    "appId": settings.APP_ID,
                    "measurementId": settings.MEASUREMENT_ID,
                },
            )
            print(
                f"Firebase Admin SDK initialized with project ID: {settings.PROJECT_ID} (ADC)."
            )
        except Exception as e_adc:
            print(
                f"Critical: Firebase Admin SDK could not be initialized. Error: {e_adc}. Firestore features will not work."
            )


db_firestore = None
try:
    db_firestore = firestore.client()
except Exception as e:
    print(
        f"Error getting Firestore client: {e}. Firestore features might be unavailable."
    )


async def get_spot_from_db(spot_id: str) -> Optional[Spot]:
    if not db_firestore:
        return None
    doc_ref = db_firestore.collection(settings.SPOTS_COLLECTION).document(spot_id)
    doc = await doc_ref.get()
    if doc.exists:
        spot_data = doc.to_dict()
        spot_data["id"] = doc.id
        return Spot(**spot_data)
    return None


async def get_all_spots_from_db() -> List[Spot]:
    if not db_firestore:
        return []
    spots_list = []
    spots_ref = db_firestore.collection(settings.SPOTS_COLLECTION)
    docs = spots_ref.stream()  # Use stream for async if available, or get()
    # Firestore client library for Python is synchronous by default for most operations.
    # For FastAPI, consider running sync Firestore calls in a threadpool executor
    # or using an async-compatible Firestore library if one exists and is stable.
    # For simplicity here, using the standard sync calls. FastAPI handles them in threads.

    # Re-implementing with sync `get()` for clarity as `stream()` is sync too.
    docs_snapshot = db_firestore.collection(settings.SPOTS_COLLECTION).get()
    for doc in docs_snapshot:
        spot_data = doc.to_dict()
        spot_data["id"] = doc.id
        spots_list.append(Spot(**spot_data))
    return spots_list


async def add_spot_to_db(spot_data: SpotCreate) -> Spot:
    if not db_firestore:
        raise HTTPException(status_code=503, detail="Firestore not available")

    # Generate a new ID or use one if provided (though SpotCreate doesn't have id)
    doc_ref = db_firestore.collection(
        settings.SPOTS_COLLECTION
    ).document()  # Auto-generate ID
    await doc_ref.set(spot_data.model_dump())
    return Spot(id=doc_ref.id, **spot_data.model_dump())


async def update_spot_embedding_in_db(spot_id: str, embedding: List[float]):
    if not db_firestore:
        return
    doc_ref = db_firestore.collection(settings.SPOTS_COLLECTION).document(spot_id)
    await doc_ref.update({"embedding": embedding})
