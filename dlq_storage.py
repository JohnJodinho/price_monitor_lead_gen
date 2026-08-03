import asyncio
import logging
from typing import Tuple

from appwrite.client import Client
from appwrite.services.storage import Storage
from appwrite.input_file import InputFile

from config import get_settings

logger = logging.getLogger(__name__)

def _upload_file_sync(bucket_id: str, file_name: str, file_bytes: bytes) -> str:
    """
    Synchronously upload a file to Appwrite storage and return its view URL.
    """
    settings = get_settings()
    
    # Initialize client
    client = Client()
    client.set_endpoint(settings.APP_WRITE_API_ENDPOINT)
    client.set_project(settings.APP_WRITE_PROJECT_ID)
    client.set_key(settings.APP_WRITE_API_KEY.get_secret_value())
    
    storage = Storage(client)
    
    # Create InputFile from bytes
    input_file = InputFile.from_bytes(
        file_bytes,
        filename=file_name
    )
    
    # Create file in Appwrite
    result = storage.create_file(
        bucket_id=bucket_id,
        file_id="unique()",
        file=input_file
    )
    
    file_id = result["$id"]
    
    # Construct view URL
    view_url = f"{settings.APP_WRITE_API_ENDPOINT}/storage/buckets/{bucket_id}/files/{file_id}/view?project={settings.APP_WRITE_PROJECT_ID}"
    
    return view_url

async def upload_dlq_artifacts(html_bytes: bytes, screenshot_bytes: bytes, property_id: str) -> Tuple[str, str]:
    """
    Concurrently upload HTML and screenshot bytes to Appwrite.
    
    Returns:
        Tuple[str, str]: (html_url, screenshot_url)
    """
    settings = get_settings()
    bucket_id = settings.APP_WRITE_BUCKET_ID
    
    html_filename = f"block_{property_id}.html"
    screenshot_filename = f"block_{property_id}.jpg"
    
    try:
        html_url, screenshot_url = await asyncio.gather(
            asyncio.to_thread(_upload_file_sync, bucket_id, html_filename, html_bytes),
            asyncio.to_thread(_upload_file_sync, bucket_id, screenshot_filename, screenshot_bytes)
        )
        logger.info(f"Successfully uploaded DLQ artifacts for {property_id}")
        return html_url, screenshot_url
    except Exception as e:
        logger.error(f"Failed to upload DLQ artifacts to Appwrite: {e}", exc_info=True)
        return None, None

def _download_file_sync(file_id: str) -> bytes:
    """
    Synchronously download a file from Appwrite storage.
    """
    settings = get_settings()
    client = Client()
    client.set_endpoint(settings.APP_WRITE_API_ENDPOINT)
    client.set_project(settings.APP_WRITE_PROJECT_ID)
    client.set_key(settings.APP_WRITE_API_KEY.get_secret_value())
    
    storage = Storage(client)
    result = storage.get_file_download(
        bucket_id=settings.APP_WRITE_BUCKET_ID,
        file_id=file_id
    )
    return result

async def get_dlq_file_bytes(file_id: str) -> bytes:
    """
    Download file bytes via to_thread to avoid blocking event loop.
    """
    try:
        file_bytes = await asyncio.to_thread(_download_file_sync, file_id)
        return file_bytes
    except Exception as e:
        logger.error(f"Failed to download DLQ file {file_id}: {e}", exc_info=True)
        return b""
