
import json
import subprocess
import base64
import os
import shutil
import stat
from http.server import BaseHTTPRequestHandler, HTTPServer

def force_delete_readonly(action, name, exc):
    try:
        os.chmod(name, stat.S_IWRITE)
        action(name)
    except Exception:
        pass

class StreamingRequestHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == '/execute':
            # 1. Parse Input Payload
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            payload = json.loads(post_data)
            
            target_file = payload.get("target_file", "script.py")
            workspace_b64 = payload.get("workspace", "")
            
            # 2. Extract Workspace Cleanly
            if os.path.exists("temp_workspace"):
                shutil.rmtree("temp_workspace", onerror=force_delete_readonly)
            os.makedirs("temp_workspace", exist_ok=True)
            
            if workspace_b64:
                with open("temp_in.zip", "wb") as f:
                    f.write(base64.b64decode(workspace_b64))
                shutil.unpack_archive("temp_in.zip", "temp_workspace")
                os.remove("temp_in.zip")
                
            # 3. Push to Docker
            subprocess.run(["docker", "exec", "gpu_share_node", "rm", "-rf", "/app"])
            subprocess.run(["docker", "cp", "temp_workspace", "gpu_share_node:/app"])
            
            # 4. Start HTTP Streaming Response Headers
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.end_headers()
            
            # 5. Execute Code and Stream Logs LINE-BY-LINE Instantly
            cmd = ["docker", "exec", "-w", "/app", "gpu_share_node", "python", "-u", target_file]
            process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            
            for line in process.stdout:
                self.wfile.write(line.encode('utf-8'))
                self.wfile.flush() # Forces network to send the line immediately
                
            process.wait()
            
            # 6. Extract modified files after execution finishes
            if os.path.exists("temp_out"):
                shutil.rmtree("temp_out", onerror=force_delete_readonly)
            os.makedirs("temp_out", exist_ok=True)
            
            subprocess.run(["docker", "cp", "gpu_share_node:/app", "temp_out/"])
            shutil.make_archive("temp_out_zip", "zip", "temp_out/app")
            
            out_b64 = ""
            if os.path.exists("temp_out_zip.zip"):
                with open("temp_out_zip.zip", "rb") as f:
                    out_b64 = base64.b64encode(f.read()).decode('utf-8')
                os.remove("temp_out_zip.zip")
                
            # 7. Append File Data silently at the absolute end of the stream
            self.wfile.write(b"\n---METADATA_START---\n")
            self.wfile.write(out_b64.encode('utf-8'))
            self.wfile.flush()
            
            # 8. Clean up Host system
            if os.path.exists("temp_workspace"): shutil.rmtree("temp_workspace", onerror=force_delete_readonly)
            if os.path.exists("temp_out"): shutil.rmtree("temp_out", onerror=force_delete_readonly)
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == '__main__':
    server = HTTPServer(('127.0.0.1', 9999), StreamingRequestHandler)
    server.serve_forever()
