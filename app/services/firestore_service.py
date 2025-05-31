import firebase_admin
from firebase_admin import credentials, firestore
from app.models import Spot, SpotCreate
from app.core.config import settings
from typing import List, Optional
from fastapi import HTTPException

if not firebase_admin._apps:
    cred = credentials.Certificate("./hoora-fb944-firebase-adminsdk-hykdj-96b7eea9ff.json")
    firebase_admin.initialize_app(cred)

store = firestore.client()


async def get_spot_from_db(spot_id: str) -> Optional[Spot]:
    if not store:
        return None
    doc_ref = store.collection(settings.SPOTS_COLLECTION).document(spot_id)
    doc = await doc_ref.get()
    if doc.exists:
        spot_data = doc.to_dict()
        spot_data["id"] = doc.id
        return Spot(**spot_data)
    return None


async def get_all_spots_from_db() -> List[Spot]:
    if not store:
        return []
    spots_list = []

    docs_snapshot = store.collection(settings.SPOTS_COLLECTION).get()
    for doc in docs_snapshot:
        spot_data = doc.to_dict()
        spot_data["id"] = doc.id
        spots_list.append(Spot(**spot_data))
    return spots_list


async def add_spot_to_db(spot_data: SpotCreate) -> Spot:
    if not store:
        raise HTTPException(status_code=503, detail="Firestore not available")

    doc_ref = store.collection(settings.SPOTS_COLLECTION).document()
    await doc_ref.set(spot_data.model_dump())
    return Spot(id=doc_ref.id, **spot_data.model_dump())


async def update_spot_embedding_in_db(spot_id: str, embedding: List[float]):
    if not store:
        return
    doc_ref = store.collection(settings.SPOTS_COLLECTION).document(spot_id)
    await doc_ref.update({"embedding": embedding})
