import sys
import os
import requests
import base64
import zipfile
import shutil

def create_smart_zip(zip_filename):
    """Zips the directory safely, completely ignoring cache and systems files."""
    exclusions = ["venv", ".venv", "__pycache__", ".git", "temp_workspace", "temp_out", ".vscode"]
    
    with zipfile.ZipFile(zip_filename, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk("."):
            dirs[:] = [d for d in dirs if d not in exclusions]
            for file in files:
                # Do not upload existing zips or models to keep upload instantaneous
                if file.endswith(".zip") or file.endswith(".pt") or file.endswith(".pth"):
                    continue
                file_path = os.path.join(root, file)
                zipf.write(file_path, os.path.relpath(file_path, "."))

def run_remote_job():
    if len(sys.argv) < 2:
        print("Usage: cloud-run <TARGET_SCRIPT.py>")
        sys.exit(1)

    target_file = sys.argv[1]
    if not os.path.exists(target_file):
        print(f"Error: Local file '{target_file}' not found.")
        sys.exit(1)

    print("🔍 Searching local network for available GPU node...")
    host_url = "http://127.0.0.1:9999"
    print("✅ Connected! Initializing live connection stream...")

    # Fast Workspace package
    create_smart_zip("job_payload.zip")
    with open("job_payload.zip", "rb") as f:
        workspace_b64 = base64.b64encode(f.read()).decode('utf-8')
    os.remove("job_payload.zip") 

    payload = {"target_file": target_file, "workspace": workspace_b64}

    print("🚀 Transmitting workspace and launching isolated execution cluster...\n")
    print("--- [REMOTE GPU EXECUTION LOGS] ---")
    
    try:
        # NOTICE: stream=True opens a live open pipeline over the network
        response = requests.post(f"{host_url}/execute", json=payload, stream=True, timeout=300)
        
        if response.status_code == 200:
            in_metadata = False
            metadata_chunks = []
            
            # Read characters/lines as they are typed out by Docker
            for chunk in response.iter_lines(decode_unicode=True):
                if chunk == "---METADATA_START---":
                    in_metadata = True
                    continue
                
                if in_metadata:
                    metadata_chunks.append(chunk)
                else:
                    # This prints your script lines instantly to your screen!
                    print(chunk)
                    
            print("-----------------------------------")
            
            # Unpack returned model data if there is any
            out_b64 = "".join(metadata_chunks).strip()
            if out_b64:
                print("\n📥 Extracting updated weights back to local machine...")
                with open("job_results.zip", "wb") as f:
                    f.write(base64.b64decode(out_b64))
                shutil.unpack_archive("job_results.zip", ".")
                os.remove("job_results.zip")
                print("✨ Workspace sync complete. Your local directory is updated!")
        else:
            print(f"Server error (Status {response.status_code}): {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"\nNetwork error: Ensure your Host UI is running! Details: {e}")

if __name__ == "__main__":
    run_remote_job()