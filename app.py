import flet as ft
import random
import subprocess
import json
import os
import sys
import time

REGISTRY_FILE = "shared_rooms.json"
AGENT_FILE = "agent_server.py"
AGENT_LOG = "agent.log"

# BULLETPROOF FIX: Using Python's native built-in HTTP server instead of FastAPI/Uvicorn.
# This eliminates all third-party dependency crashes and Windows multiprocessing issues.
# LIVE STREAMING AGENT CORE (Replace only this string at the top of your file!)
AGENT_SERVER_CODE = """
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
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            payload = json.loads(post_data)
            
            target_file = payload.get("target_file", "script.py")
            workspace_b64 = payload.get("workspace", "")
            
            if os.path.exists("temp_workspace"):
                shutil.rmtree("temp_workspace", onerror=force_delete_readonly)
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
                
            self.wfile.write(b"\\n---METADATA_START---\\n")
            self.wfile.write(out_b64.encode('utf-8'))
            self.wfile.flush()
            
            if os.path.exists("temp_workspace"): shutil.rmtree("temp_workspace", onerror=force_delete_readonly)
            if os.path.exists("temp_out"): shutil.rmtree("temp_out", onerror=force_delete_readonly)
        else:
            self.send_response(404)
            self.end_headers()

if __name__ == '__main__':
    server = HTTPServer(('127.0.0.1', 9999), StreamingRequestHandler)
    server.serve_forever()
"""

def main(page: ft.Page):
    page.title = "GPU Share Hub"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = ft.Colors.BLACK
    page.padding = 0
    page.window.width = 1000
    page.window.height = 700

    app_state = {
        "username": "",
        "is_hosting": False,
        "is_connected": False,
        "room_code": "",
        "ngrok_url": "",
        "connected_party": "None",
        "container_name": "gpu_share_node",
        "host_specs": "",
        "agent_proc": None,
        "log_file_obj": None
    }

    def register_room(code, url, ram, cpu, img):
        rooms = {}
        if os.path.exists(REGISTRY_FILE):
            try:
                with open(REGISTRY_FILE, "r") as f: rooms = json.load(f)
            except: pass
        rooms[code] = {"url": url, "host": app_state["username"], "ram": ram, "cpu": cpu, "image": img}
        with open(REGISTRY_FILE, "w") as f: json.dump(rooms, f)

    def unregister_room(code):
        if os.path.exists(REGISTRY_FILE):
            try:
                with open(REGISTRY_FILE, "r") as f: rooms = json.load(f)
                if code in rooms: del rooms[code]
                with open(REGISTRY_FILE, "w") as f: json.dump(rooms, f)
            except: pass

    # ==========================================
    # VIEW: JOIN / CLIENT DASHBOARD VIEW
    # ==========================================
    def build_join_view():
        room_input = ft.TextField(label="Enter Room Code", width=300, border_color=ft.Colors.GREEN_ACCENT, prefix_icon=ft.Icons.KEY)

        def client_connect(e):
            code = room_input.value.strip()
            if not code or not os.path.exists(REGISTRY_FILE):
                room_input.error_text = "Room Code offline or missing!"
                page.update()
                return

            with open(REGISTRY_FILE, "r") as f: rooms = json.load(f)
            
            if code in rooms:
                app_state["is_connected"] = True
                app_state["room_code"] = code
                app_state["ngrok_url"] = rooms[code]["url"]
                app_state["connected_party"] = rooms[code]["host"]
                app_state["host_specs"] = f"RAM: {rooms[code]['ram']} | CPU: {rooms[code]['cpu']} | Env: {rooms[code]['image']}"
                
                with open("client_config.json", "w") as f:
                    json.dump({"active_room": code}, f)

                navigate(None, "join")
            else:
                room_input.error_text = "Invalid Code!"
                page.update()

        def client_disconnect(e):
            app_state["is_connected"] = False
            app_state["room_code"] = ""
            app_state["ngrok_url"] = ""
            if os.path.exists("client_config.json"): os.remove("client_config.json")
            navigate(None, "join")

        if not app_state["is_connected"]:
            return ft.Column([
                ft.Text("Join a Compute Node", size=32, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                ft.Text("Connect to a remote GPU container using a Room Code.", color=ft.Colors.GREY_400),
                ft.Divider(height=20, color=ft.Colors.TRANSPARENT),
                room_input,
                ft.Divider(height=10, color=ft.Colors.TRANSPARENT),
                ft.FilledButton("Connect to Node", icon=ft.Icons.LOGIN_ROUNDED, style=ft.ButtonStyle(color=ft.Colors.BLACK, bgcolor=ft.Colors.GREEN_ACCENT), height=45, on_click=client_connect)
            ])
        else:
            return ft.Column([
                ft.Text("Connected to Remote GPU Cluster", size=32, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                ft.Text(f"Target Cluster Admin: {app_state['connected_party']}", color=ft.Colors.BLUE_300, size=16),
                ft.Divider(height=10, color=ft.Colors.TRANSPARENT),
                ft.Container(
                    content=ft.Column([
                        ft.Text("Active Hardware Environment Specs", weight=ft.FontWeight.BOLD),
                        ft.Text(app_state["host_specs"], color=ft.Colors.GREY_400),
                        ft.Text(f"Tunnel Route: {app_state['ngrok_url']}", size=12, color=ft.Colors.GREY_500),
                        ft.Divider(color=ft.Colors.GREY_800),
                        ft.Row([
                            ft.Icon(ft.Icons.TERMINAL, color=ft.Colors.GREEN_ACCENT),
                            ft.Text("Ready for External IDE Execution via VS Code CLI tool.", color=ft.Colors.GREEN_ACCENT)
                        ]),
                        ft.Divider(height=10, color=ft.Colors.TRANSPARENT),
                        ft.FilledButton("Disconnect from Node", icon=ft.Icons.LOGOUT_ROUNDED, style=ft.ButtonStyle(color=ft.Colors.WHITE, bgcolor=ft.Colors.RED_900), height=40, on_click=client_disconnect)
                    ]),
                    padding=25, bgcolor=ft.Colors.GREY_900, border_radius=12, border=ft.Border.all(1, ft.Colors.GREY_800)
                )
            ])

    # ==========================================
    # VIEW: HOST / CLUSTER ADMIN VIEW
    # ==========================================
    def build_host_view():
        def start_hosting(e):
            if app_state["is_hosting"]: return
            from pyngrok import ngrok
            
            app_state["room_code"] = f"{random.randint(100,999)}-{random.randint(100,999)}"
            ram_val = ram_dd.value.replace("GB", "g")
            cpu_val = cpu_dd.value.split(" ")[0]
            selected_image = env_dd.value

            try:
                subprocess.run(["docker", "rm", "-f", app_state["container_name"]], capture_output=True)
                
                subprocess.run([
                    "docker", "run", "-d", "--gpus", "all", "--name", app_state["container_name"],
                    "--memory", ram_val, "--cpus", cpu_val,
                    selected_image, "tail", "-f", "/dev/null"
                ], check=True)

                with open(AGENT_FILE, "w") as f:
                    f.write(AGENT_SERVER_CODE)
                
                app_state["log_file_obj"] = open(AGENT_LOG, "w")
                app_state["agent_proc"] = subprocess.Popen(
                    [sys.executable, AGENT_FILE], 
                    stdout=app_state["log_file_obj"], 
                    stderr=app_state["log_file_obj"]
                )

                # Give the native python server 1.5 seconds to fully bind to the port
                time.sleep(1.5)

                public_url = ngrok.connect("127.0.0.1:9999").public_url
                app_state["ngrok_url"] = public_url

                register_room(app_state["room_code"], public_url, ram_val, cpu_val, selected_image)

                app_state["is_hosting"] = True
                navigate(None, "host") 
            except Exception as err:
                print(f"Error starting cluster: {err}")

        def stop_hosting(e):
            if not app_state["is_hosting"]: return
            from pyngrok import ngrok
            
            subprocess.run(["docker", "rm", "-f", app_state["container_name"]], capture_output=True)
            
            if app_state["agent_proc"]:
                app_state["agent_proc"].terminate()
                app_state["agent_proc"].wait()
            
            if app_state["log_file_obj"]:
                app_state["log_file_obj"].close()

            try: ngrok.kill()
            except: pass
            
            unregister_room(app_state["room_code"])
            if os.path.exists(AGENT_FILE): os.remove(AGENT_FILE)

            app_state["is_hosting"] = False
            app_state["room_code"] = ""
            app_state["ngrok_url"] = ""
            navigate(None, "host")

        ram_dd = ft.Dropdown(options=[ft.dropdown.Option("4GB"), ft.dropdown.Option("8GB"), ft.dropdown.Option("16GB")], value="8GB", width=120, label="RAM")
        cpu_dd = ft.Dropdown(options=[ft.dropdown.Option("2 Core"), ft.dropdown.Option("4 Core"), ft.dropdown.Option("8 Core")], value="4 Core", width=120, label="CPU")
        env_dd = ft.Dropdown(
            options=[ft.dropdown.Option("pytorch/pytorch:latest"), ft.dropdown.Option("tensorflow/tensorflow:latest-gpu"), ft.dropdown.Option("ubuntu:latest")], 
            value="pytorch/pytorch:latest", width=300, label="Base Framework Environment"
        )

        if not app_state["is_hosting"]:
            return ft.Column([
                ft.Text("Host Infrastructure", size=32, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                ft.Text("Configure and allocate resources to share your local GPU environment.", color=ft.Colors.GREY_400),
                ft.Divider(height=20, color=ft.Colors.TRANSPARENT),
                ft.Container(
                    content=ft.Column([
                        ft.Text("Hardware Allocation", weight=ft.FontWeight.BOLD, size=16),
                        ft.Divider(color=ft.Colors.GREY_800),
                        env_dd,
                        ft.Row([ram_dd, cpu_dd], spacing=15),
                        ft.Divider(height=10, color=ft.Colors.TRANSPARENT),
                        ft.FilledButton("Start Hosting GPU", icon=ft.Icons.PLAY_ARROW_ROUNDED, style=ft.ButtonStyle(color=ft.Colors.BLACK, bgcolor=ft.Colors.GREEN_ACCENT), height=50, on_click=start_hosting)
                    ]),
                    padding=25, bgcolor=ft.Colors.GREY_900, border_radius=12, border=ft.Border.all(1, ft.Colors.GREY_800)
                )
            ])
        else:
            return ft.Column([
                ft.Text("Live Hosting Dashboard", size=32, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                ft.Container(
                    content=ft.Column([
                        ft.Row([ft.Icon(ft.Icons.LIVE_TV, color=ft.Colors.GREEN_ACCENT), ft.Text("Node Status: ONLINE", color=ft.Colors.GREEN_ACCENT, weight=ft.FontWeight.BOLD)]),
                        ft.Divider(color=ft.Colors.GREY_800),
                        ft.Row([ft.Text("Your Room Code: ", size=16, color=ft.Colors.GREY_400), ft.Text(app_state["room_code"], size=22, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE)]),
                        ft.Text(f"Public Tunnel API Route: {app_state['ngrok_url']}", size=14, color=ft.Colors.BLUE_300),
                        ft.Divider(height=15, color=ft.Colors.TRANSPARENT),
                        ft.FilledButton("Terminate Hosting", icon=ft.Icons.STOP_ROUNDED, style=ft.ButtonStyle(color=ft.Colors.WHITE, bgcolor=ft.Colors.RED_900), width=float('inf'), height=45, on_click=stop_hosting)
                    ]),
                    padding=25, bgcolor=ft.Colors.GREY_900, border_radius=12, border=ft.Border.all(1, ft.Colors.GREY_800)
                )
            ])

    # ==========================================
    # APP LAYOUT ROUTER
    # ==========================================
    main_content_area = ft.Container(expand=True, bgcolor=ft.Colors.with_opacity(0.05, ft.Colors.WHITE), border_radius=ft.BorderRadius.only(top_left=20, bottom_left=20), padding=40)

    def navigate(e, view_name):
        if view_name == "host": main_content_area.content = build_host_view()
        elif view_name == "join": main_content_area.content = build_join_view()
        page.update()

    def build_main_app():
        page.clean()
        sidebar = ft.Container(
            width=220, padding=20,
            content=ft.Column([
                ft.Row([ft.Icon(ft.Icons.HUB, color=ft.Colors.GREEN_ACCENT, size=30), ft.Text("GPU Share", size=20, weight=ft.FontWeight.BOLD)]),
                ft.Divider(height=30, color=ft.Colors.GREY_900),
                ft.TextButton("Host GPU", icon=ft.Icons.DNS_ROUNDED, style=ft.ButtonStyle(color=ft.Colors.GREY_300), on_click=lambda e: navigate(e, "host"), width=200),
                ft.TextButton("Join GPU", icon=ft.Icons.LOGIN_ROUNDED, style=ft.ButtonStyle(color=ft.Colors.GREY_300), on_click=lambda e: navigate(e, "join"), width=200),
                ft.Container(expand=True),
                ft.Container(
                    content=ft.Row([ft.CircleAvatar(content=ft.Text(app_state["username"][0].upper()), bgcolor=ft.Colors.BLUE_900), ft.Text(app_state["username"], weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE)]),
                    padding=10, bgcolor=ft.Colors.GREY_900, border_radius=10
                )
            ])
        )
        page.add(ft.Row([sidebar, main_content_area], expand=True, spacing=0))
        navigate(None, "host")

    def login_click(e):
        if username_field.value:
            app_state["username"] = username_field.value
            build_main_app()

    username_field = ft.TextField(label="Enter your Developer Alias", width=300, on_submit=login_click)
    page.add(ft.Container(
        content=ft.Column([
            ft.Icon(ft.Icons.ACCOUNT_CIRCLE, size=80, color=ft.Colors.GREEN_ACCENT),
            ft.Text("Identify Yourself", size=30, weight=ft.FontWeight.BOLD),
            username_field,
            ft.FilledButton("Enter Hub", on_click=login_click, style=ft.ButtonStyle(color=ft.Colors.BLACK, bgcolor=ft.Colors.GREEN_ACCENT), height=45)
        ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, alignment=ft.MainAxisAlignment.CENTER),
        expand=True, alignment=ft.Alignment.CENTER
    ))

if __name__ == "__main__":
    ft.run(main)