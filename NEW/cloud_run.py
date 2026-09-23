import sys
import os
import json
import subprocess
import requests

def run_play_demo():
    """
    'cloud-run game.py' is a different kind of job than the text-streaming
    path below: the live GPU-rendered scene needs real-time video + input,
    not stdout. This launches remote_play/client.py, which auto-discovers
    the GPU host on the LAN and prompts for its pairing code.
    """
    if getattr(sys, 'frozen', False):
        base_dir = os.path.dirname(sys.executable)
    else:
        base_dir = os.path.dirname(os.path.abspath(__file__))

    client_script = os.path.join(base_dir, "remote_play", "client.py")
    if not os.path.exists(client_script):
        print("❌ Error: remote_play/client.py not found next to cloud_run.py.")
        sys.exit(1)

    result = subprocess.run([sys.executable, client_script], cwd=os.path.dirname(client_script))
    sys.exit(result.returncode)

def main():
    if len(sys.argv) < 2:
        print("Usage: cloud-run <script.py>")
        sys.exit(1)

    target_file = sys.argv[1]

    if not os.path.exists(target_file):
        print(f"❌ Error: File '{target_file}' not found.")
        sys.exit(1)

    if os.path.basename(target_file).lower() == "game.py":
        run_play_demo()
        return

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

    if not url or not api_key:
        print("❌ Error: Invalid configuration file. Join the room again.")
        sys.exit(1)

    # 1. Zero-Footprint payload: Read ONLY the target file text
    with open(target_file, "r", encoding="utf-8") as f:
        script_content = f.read()

    payload = {
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