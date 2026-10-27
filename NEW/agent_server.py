
import json, subprocess, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class StreamingRequestHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_POST(self):
        if self.path == '/pair':
            content_length = int(self.headers.get('Content-Length', 0))
            payload = json.loads(self.rfile.read(content_length))
            code = payload.get("code", "")
            client_name = payload.get("client_name", "Unknown")

            if code != os.environ.get('LAN_CODE', ''):
                body = json.dumps({"status": "rejected"}).encode('utf-8')
                self.send_response(403)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

            clients = {}
            try:
                if os.path.exists("allowed_clients.json"):
                    with open("allowed_clients.json", "r") as f:
                        clients = json.load(f)
            except Exception:
                clients = {}
            clients[client_name] = "approved"
            with open("allowed_clients.json", "w") as f:
                json.dump(clients, f)

            body = json.dumps({"status": "approved", "api_key": os.environ.get('GPU_API_KEY', '')}).encode('utf-8')
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        auth_header = self.headers.get('Authorization')
        if auth_header != f"Bearer {os.environ.get('GPU_API_KEY', '')}":
            self.send_response(401); self.end_headers(); return

        if self.path == '/execute':
            content_length = int(self.headers.get('Content-Length', 0))
            payload = json.loads(self.rfile.read(content_length))

            client_name = payload.get("client_name", "Unknown")

            is_approved = False
            try:
                if os.path.exists("allowed_clients.json"):
                    with open("allowed_clients.json", "r") as f:
                        clients = json.load(f)
                        if clients.get(client_name) == "approved":
                            is_approved = True
            except Exception:
                pass

            if not is_approved:
                self.send_response(403); self.end_headers(); return

            target_file = payload.get("target_file", "script.py")
            script_content = payload.get("script_content", "")

            with open("temp_script.py", "w", encoding="utf-8") as f:
                f.write(script_content)

            subprocess.run(["docker", "exec", "gpu_share_node", "mkdir", "-p", "/app"], capture_output=True)
            subprocess.run(["docker", "cp", "temp_script.py", f"gpu_share_node:/app/{target_file}"], capture_output=True)
            if os.path.exists("temp_script.py"): os.remove("temp_script.py")

            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Transfer-Encoding', 'chunked')
            self.end_headers()

            cmd = ["docker", "exec", "-w", "/app", "gpu_share_node", "python", "-u", target_file]
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            while True:
                chunk = process.stdout.read1(4096)
                if not chunk:
                    if process.poll() is not None: break
                    continue
                self.wfile.write(f"{len(chunk):X}\r\n".encode('utf-8'))
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()

            process.wait()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        else:
            self.send_response(404); self.end_headers()

if __name__ == '__main__':
    port = int(os.environ.get('AGENT_PORT', 9999))
    ThreadingHTTPServer(('0.0.0.0', port), StreamingRequestHandler).serve_forever()
