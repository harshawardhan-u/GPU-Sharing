import sys
import os
import json
import base64
import requests

# Some Windows terminals default to a legacy codepage (e.g. cp1252) that
# can't encode emoji, which would otherwise crash mid-stream on any of the
# print statements below (existing ones included).
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

FILE_START_PREFIX = "##CLOUDRUN_FILE_START:"
FILE_END_MARKER = "##CLOUDRUN_FILE_END##"

class StreamFileCapture:
    """
    Processes the streamed stdout line-by-line, watching for
    ##CLOUDRUN_FILE_START:<name>## ... base64 ... ##CLOUDRUN_FILE_END##
    blocks that a script (e.g. gpu_raytracer.py) can emit to send a
    generated file back over the same text channel. Normal lines print
    live exactly as before; a captured block is decoded and saved to disk
    instead of being dumped as raw base64 text.
    """
    def __init__(self):
        self.buffer = ""
        self.capturing = False
        self.capture_name = None
        self.capture_chunks = []
        self.saved_files = []

    def feed(self, text_chunk):
        self.buffer += text_chunk
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            self._handle_line(line)

    def finish(self):
        if self.buffer:
            self._handle_line(self.buffer)
            self.buffer = ""

    def _handle_line(self, line):
        stripped = line.rstrip("\r")
        if not self.capturing:
            if stripped.startswith(FILE_START_PREFIX) and stripped.endswith("##"):
                self.capture_name = stripped[len(FILE_START_PREFIX):-2]
                self.capturing = True
                self.capture_chunks = []
                print(f"\n📦 Receiving generated file: {self.capture_name} ...")
            else:
                print(line)
        else:
            if stripped == FILE_END_MARKER:
                self._save_capture()
                self.capturing = False
            else:
                self.capture_chunks.append(stripped)

    def _save_capture(self):
        try:
            data = base64.b64decode("".join(self.capture_chunks))
            out_path = os.path.join(os.getcwd(), self.capture_name)
            with open(out_path, "wb") as f:
                f.write(data)
            print(f"✅ Saved: {out_path}")
            self.saved_files.append(out_path)
            if os.name == 'nt':
                try:
                    os.startfile(out_path)
                except Exception:
                    pass
        except Exception as e:
            print(f"❌ Failed to decode/save received file: {e}")

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
    client_name = config.get("client_name", "Unknown")

    if not url or not api_key:
        print("❌ Error: Invalid configuration file. Join the room again.")
        sys.exit(1)

    # 1. Zero-Footprint payload: Read ONLY the target file text
    with open(target_file, "r", encoding="utf-8") as f:
        script_content = f.read()

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
            elif response.status_code == 403:
                print("❌ Error: Execution denied. You are not approved in the host's lobby.")
                sys.exit(1)
            elif response.status_code != 200:
                print(f"❌ Error: Server returned status {response.status_code}.")
                sys.exit(1)

            # 3. Read raw chunks from the network, print instantly, and
            #    transparently capture any file the script sends back.
            capture = StreamFileCapture()
            for chunk in response.iter_content(chunk_size=None):
                if chunk:
                    capture.feed(chunk.decode('utf-8', errors='replace'))
                    sys.stdout.flush()
            capture.finish()

    except requests.exceptions.RequestException as e:
        print(f"\n❌ Connection Error: {e}")
        
    print("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

if __name__ == "__main__":
    main()