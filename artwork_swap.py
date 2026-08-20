import os
import shutil
import requests
from artwork_sync import (
    download_image,
    plex_find_library_key,
    plex_upload_collection_poster,
    _plex_headers,
    _plex_url,
    artist_to_folder,
)

def plex_swap_collection_artwork(config, artist_name, new_image_url):
    """
    Swap the artwork for a given artist's Plex collection.

    Steps:
    1. Determine the artist folder on the NAS and download the new image.
    2. Overwrite the existing folder.jpg (and poster.jpg) with the new image.
    3. Locate the Plex smart collection for the artist.
    4. Upload the new image as the collection poster via Plex API.
    """
    # Resolve artist folder
    artwork_cfg = config.get('artwork_sync', {})
    root_path = artwork_cfg.get('root_path', '/app/music_videos_final')
    folder_name = artist_to_folder(artist_name)
    artist_path = os.path.join(root_path, folder_name)

    if not os.path.isdir(artist_path):
        return {'success': False, 'error': f'Artist folder not found: {artist_path}'}

    # Download new image to a temporary location
    temp_path = os.path.join(artist_path, 'temp_swap.jpg')
    if not download_image(new_image_url, temp_path):
        return {'success': False, 'error': 'Failed to download new image'}

    # Overwrite folder.jpg and poster.jpg.
    # NOTE: os.replace() is a move — it consumes temp_path. Calling it twice
    # in a loop with the same source moved the file to folder.jpg on the
    # first pass, then failed on the second pass because temp_path no longer
    # existed. That exception was exactly why the swap button always failed.
    # Fix: move once to folder.jpg, then do a manual buffered copy (never
    # shutil.copy2/copyfile — see CLAUDE.md gotcha #2, these files live on
    # the CIFS-mounted NAS share) from folder.jpg to poster.jpg.
    folder_dest = os.path.join(artist_path, 'folder.jpg')
    poster_dest = os.path.join(artist_path, 'poster.jpg')
    try:
        os.replace(temp_path, folder_dest)
    except Exception as exc:
        return {'success': False, 'error': f'Failed to replace folder.jpg: {exc}'}
    try:
        with open(folder_dest, 'rb') as fsrc, open(poster_dest, 'wb') as fdst:
            shutil.copyfileobj(fsrc, fdst)
    except Exception as exc:
        return {'success': False, 'error': f'Failed to replace poster.jpg: {exc}'}
    # Clean up any leftover temp file
    if os.path.isfile(temp_path):
        try:
            os.remove(temp_path)
        except Exception:
            pass

    # Find the Plex collection key for this artist. Prefer the saved,
    # hand-verified music_video_library_key (see REFERENCE.md Bug C — this
    # account's library title has a typo that breaks title-substring
    # auto-discovery) and only fall back to plex_find_library_key() if it's
    # not set, matching the pattern used everywhere else in app.py.
    library_key = config.get('plex', {}).get('music_video_library_key', '')
    if not library_key:
        library_key = plex_find_library_key(config)
    if not library_key:
        return {'success': False, 'error': 'Unable to determine Plex library key'}

    base_url = _plex_url(config)
    if not base_url:
        return {'success': False, 'error': 'Plex server_url is not configured'}
    headers = _plex_headers(config)

    try:
        resp = requests.get(f"{base_url}/library/sections/{library_key}/collections",
                            headers=headers, timeout=10)
        resp.raise_for_status()
        collections = resp.json().get('MediaContainer', {}).get('Metadata', [])
        collection_key = None
        for col in collections:
            if col.get('title', '').lower() == artist_name.lower():
                collection_key = col.get('ratingKey')
                break
        if not collection_key:
            return {'success': False, 'error': f'Plex collection not found for artist {artist_name}'}
    except Exception as exc:
        return {'success': False, 'error': f'Failed to list Plex collections: {exc}'}

    # Upload the new poster image to the collection
    poster_path = os.path.join(artist_path, 'folder.jpg')
    try:
        uploaded = plex_upload_collection_poster(config, collection_key, poster_path)
        if uploaded:
            return {'success': True, 'message': 'Artwork swapped successfully'}
        else:
            return {'success': False, 'error': 'Plex API rejected the poster upload'}
    except Exception as exc:
        return {'success': False, 'error': f'Error uploading poster to Plex: {exc}'}