import sys
import os
import json
import requests

def main():
    if len(sys.argv) < 2:
        print("Usage: cloud-run <script.py>")
        sys.exit(1)

    target_file = sys.argv[1]
    
    if not os.path.exists(target_file):
        print(f"❌ Error: File '{target_file}' not found.")
        sys.exit(1)

    # Load connection credentials
    config_path = os.path.expanduser("~/.gpu_share_config.json")
    if not os.path.exists(config_path):
        print("❌ Error: No active connection found. Please join a room via the Dashboard first.")
        sys.exit(1)

    try:
        with open(config_path, "r") as f:
            config = json.load(f)
    except Exception:
        print("❌ Error: Could not read configuration file.")
        sys.exit(1)

    url = config.get("url")
    api_key = config.get("api_key")
    
    # ---------------------------------------------------------
    # NEW: Extract the client name from the config file
    # ---------------------------------------------------------
    client_name = config.get("client_name", "Unknown")

    if not url or not api_key:
        print("❌ Error: Invalid configuration file. Join the room again.")
        sys.exit(1)

    # 1. Zero-Footprint payload: Read ONLY the target file text
    with open(target_file, "r", encoding="utf-8") as f:
        script_content = f.read()

    # ---------------------------------------------------------
    # NEW: Inject client_name into the JSON payload
    # ---------------------------------------------------------
    payload = {
        "client_name": client_name,
        "target_file": target_file,
        "script_content": script_content
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    print("\n🚀 Uploading target script...")
    print("🔗 Executing on remote Node...\n")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("                LIVE EXECUTION                ")
    print("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    try:
        # 2. Make the POST request and stream the chunked response
        with requests.post(f"{url}/execute", json=payload, headers=headers, stream=True) as response:
            if response.status_code == 401:
                print("❌ Error: Unauthorized. Your token might have expired.")
                sys.exit(1)
            # ---------------------------------------------------------
            # NEW: Handle the 403 Forbidden status specifically
            # ---------------------------------------------------------
            elif response.status_code == 403:
                print("❌ Error: Execution denied. You are not approved in the host's lobby.")
                sys.exit(1)
            elif response.status_code != 200:
                print(f"❌ Error: Server returned status {response.status_code}.")
                sys.exit(1)

            # 3. Read raw chunks from the network and print instantly
            for chunk in response.iter_content(chunk_size=None):
                if chunk:
                    # Safely decode the raw binary stream for the Windows terminal
                    sys.stdout.write(chunk.decode('utf-8', errors='replace'))
                    sys.stdout.flush()

    except requests.exceptions.RequestException as e:
        print(f"\n❌ Connection Error: {e}")
        
    print("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

if __name__ == "__main__":
    main()