
import json, subprocess, base64, os, shutil, stat
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

def force_delete_readonly(action, name, exc):
    try:
        os.chmod(name, stat.S_IWRITE)
        action(name)
    except Exception: pass

class StreamingRequestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        # 1. API Key Authentication
        auth_header = self.headers.get('Authorization')
        expected_key = os.environ.get('GPU_API_KEY', '')
        if auth_header != f"Bearer {expected_key}":
            self.send_response(401)
            self.end_headers()
            return

        if self.path == '/execute':
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            payload = json.loads(post_data)
            target_file = payload.get("target_file", "script.py")
            workspace_b64 = payload.get("workspace", "")
            
            os.makedirs("temp_workspace", exist_ok=True)
            if workspace_b64:
                with open("temp_in.zip", "wb") as f:
                    f.write(base64.b64decode(workspace_b64))
                shutil.unpack_archive("temp_in.zip", "temp_workspace")
                os.remove("temp_in.zip")
                
            subprocess.run(["docker", "exec", "gpu_share_node", "rm", "-rf", "/app"])
            subprocess.run(["docker", "cp", "temp_workspace", "gpu_share_node:/app"])
            
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            
            cmd = ["docker", "exec", "-w", "/app", "gpu_share_node", "python", "-u", target_file]
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            
            for line in process.stdout:
                self.wfile.write(line.encode('utf-8'))
                self.wfile.flush()
            process.wait()
            
            if os.path.exists("temp_workspace"): shutil.rmtree("temp_workspace", onerror=force_delete_readonly)
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 9999), StreamingRequestHandler)
    print("Agent running on port 9999...")
    server.serve_forever()
