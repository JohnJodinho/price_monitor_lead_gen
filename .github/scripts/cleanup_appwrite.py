import os
from appwrite.client import Client
from appwrite.services.storage import Storage
from appwrite.query import Query

def cleanup_appwrite_reports():
    endpoint = os.environ.get("APP_WRITE_API_ENDPOINT")
    project_id = os.environ.get("APP_WRITE_PROJECT_ID")
    api_key = os.environ.get("APP_WRITE_API_KEY")
    bucket_id = os.environ.get("APP_WRITE_BUCKET_ID")
    
    if not all([endpoint, project_id, api_key, bucket_id]):
        print("Missing required Appwrite environment variables.")
        exit(1)
        
    client = Client()
    client.set_endpoint(endpoint)
    client.set_project(project_id)
    client.set_key(api_key)
    
    storage = Storage(client)
    
    deleted_count = 0
    has_next = True
    
    print(f"Starting cleanup for bucket: {bucket_id}")
    
    while has_next:
        try:
            # We fetch up to 100 files at a time. Using search query on 'report_'
            response = storage.list_files(
                bucket_id=bucket_id,
                queries=[Query.limit(100), Query.search("name", "report_")]
            )
            
            files = response.get('files', [])
            
            if not files:
                has_next = False
                break
                
            for file in files:
                if file.get('name', '').startswith("report_"):
                    file_id = file.get('$id')
                    try:
                        storage.delete_file(bucket_id, file_id)
                        print(f"Deleted file: {file.get('name')} (ID: {file_id})")
                        deleted_count += 1
                    except Exception as e:
                        print(f"Error deleting file {file_id}: {e}")
                        
            # If we processed fewer than 100, we're likely done with this batch
            if len(files) < 100:
                has_next = False
                
        except Exception as e:
            print(f"Error fetching files: {e}")
            break
            
    print(f"Cleanup finished. Total files deleted: {deleted_count}")

if __name__ == "__main__":
    cleanup_appwrite_reports()
