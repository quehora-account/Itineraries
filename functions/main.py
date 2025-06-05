import firebase_functions as functions
from fastapi import FastAPI, HTTPException
from app.services.firestore_service import get_spot_from_db, get_all_playlists_from_db
from app.services.utils import get_embedding
from app.services.firestore_service import update_spot_embedding_in_db

app = FastAPI()

@app.get("/")
def home():
    return {"message": "Hello from Firebase Functions"}

@app.get("/compute-spot-embedding/{spot_id}")
def compute_spot_embedding(spot_id: str):
    spot = get_spot_from_db(spot_id)
    if not spot:
        raise HTTPException(status_code=404, detail="Spot not found")

    # Fetch playlists for label enrichment
    all_playlists_list = get_all_playlists_from_db()
    all_playlists = {playlist.id: playlist for playlist in all_playlists_list}
    playlist_labels = [
        all_playlists[playlist_id].name
        for playlist_id in spot.playlistIds
        if playlist_id in all_playlists
    ]
    price_info = ""
    if spot.fullPrice:
        price_info = f", Price: {spot.fullPrice.price} ({spot.fullPrice.condition})"
    spot_text_to_encode = f"{spot.name}, {spot.description}, {spot.type}, {', '.join(spot.highlights)}, playlists: {', '.join(playlist_labels)}{price_info}"
    embedding = get_embedding(spot_text_to_encode)
    update_spot_embedding_in_db(spot.id, embedding)
    spot.embedding = embedding
    return spot

firebase_function = functions.https.on_request(app)