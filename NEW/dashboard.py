import customtkinter as ctk
import subprocess
import secrets
import time
import os
import json
import threading
import socket
import requests
import shutil       # <-- NEW: Used to check if Docker/Cloudflared are installed
import webbrowser   # <-- NEW: Used to open download links in the browser
import urllib.request # <-- NEW: For auto-downloading Cloudflared
import winreg
import sys
import base64
import io
from PIL import Image

# =========================================================================
# CONFIGURATION: YOUR VERIFIED FIREBASE SETTINGS
# =========================================================================
FIREBASE_PROJECT_ID = "computenexus" 
FIREBASE_WEB_API_KEY = "AIzaSyDXqtmyGSjfLk978sH7W6WJX0L2ePbm2FI"  # <--- REPLACE THIS WITH YOUR WEB API KEY!

# --- EMBEDDED AGENT SERVER (For the Provider) ---
AGENT_SERVER_CODE = """
import json, subprocess, os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class StreamingRequestHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def do_POST(self):
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
                self.wfile.write(f"{len(chunk):X}\\r\\n".encode('utf-8'))
                self.wfile.write(chunk)
                self.wfile.write(b"\\r\\n")
                self.wfile.flush()
                
            process.wait()
            self.wfile.write(b"0\\r\\n\\r\\n")
            self.wfile.flush()
        else:
            self.send_response(404); self.end_headers()

if __name__ == '__main__':
    port = int(os.environ.get('AGENT_PORT', 9999))
    ThreadingHTTPServer(('127.0.0.1', port), StreamingRequestHandler).serve_forever()
"""

# --- LAN-ONLY VARIANT: same /execute engine, plus a /pair handshake so a
# client can trade a 6-digit code for the api_key directly, no Firebase
# involved. Binds 0.0.0.0 (not just localhost) so other LAN devices can
# actually reach it. Kept as a separate constant so the Cloudflare/Firebase
# path above is untouched. ---
LAN_AGENT_SERVER_CODE = """
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
                self.wfile.write(f"{len(chunk):X}\\r\\n".encode('utf-8'))
                self.wfile.write(chunk)
                self.wfile.write(b"\\r\\n")
                self.wfile.flush()

            process.wait()
            self.wfile.write(b"0\\r\\n\\r\\n")
            self.wfile.flush()
        else:
            self.send_response(404); self.end_headers()

if __name__ == '__main__':
    port = int(os.environ.get('AGENT_PORT', 9999))
    ThreadingHTTPServer(('0.0.0.0', port), StreamingRequestHandler).serve_forever()
"""

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("green")

class ComputeNexusApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Compute Nexus")
        self.geometry("950x650")
        self.resizable(True, True)
        
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.tunnel_proc = None
        self.agent_proc = None
        self.is_hosting = False
        self.monitor_thread_active = False
        self.current_pin = None
        self.known_clients_dict = {}  
        self.client_listening = False
        
        self.my_username = "User"
        self.my_email = ""
        self.id_token = None
        
        self.profile_modal_frame = None

        self._demo_section_revealed = False
        self._demo_running = False
        self._last_render_path = None

        self.is_lan_hosting = False
        self.lan_announce_stop = None
        self.lan_code = None
        self.lan_port = None

        self.build_auth_screen()

    # =========================================================================
    # AUTHENTICATION ENGINE
    # =========================================================================
    def build_auth_screen(self):
        self.auth_frame = ctk.CTkFrame(self, fg_color="#121212")
        self.auth_frame.pack(expand=True, fill="both")

        self.logo = ctk.CTkLabel(self.auth_frame, text="COMPUTE NEXUS", font=ctk.CTkFont(size=34, weight="bold"), text_color="#1DB954")
        self.logo.pack(pady=(60, 5))
        
        self.sub = ctk.CTkLabel(self.auth_frame, text="Decentralized Resource Sharing Gateway", text_color="gray50", font=ctk.CTkFont(size=14))
        self.sub.pack(pady=(0, 35))

        self.username_box = ctk.CTkEntry(self.auth_frame, width=320, height=45, placeholder_text="Choose Username", justify="center")
        self.email_box = ctk.CTkEntry(self.auth_frame, width=320, height=45, placeholder_text="Email Address", justify="center")
        self.pass_box = ctk.CTkEntry(self.auth_frame, width=320, height=45, placeholder_text="Account Password", show="*", justify="center")
        
        self.status_lbl = ctk.CTkLabel(self.auth_frame, text="", font=ctk.CTkFont(size=13))
        self.status_lbl.pack(pady=5)

        self.action_btn = ctk.CTkButton(self.auth_frame, text="Sign In", width=320, height=45, font=ctk.CTkFont(weight="bold"), command=self.submit_auth)
        self.action_btn.pack(pady=5)

        self.forgot_btn = ctk.CTkButton(self.auth_frame, text="Forgot Password?", fg_color="transparent", text_color="gray50", width=120, hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.switch_to_forgot_mode)
        self.forgot_btn.pack(pady=2)

        self.toggle_mode_btn = ctk.CTkButton(self.auth_frame, text="Don't have an account? Create One", fg_color="transparent", text_color="#1DB954", font=ctk.CTkFont(weight="bold"), command=self.switch_to_signup_mode)
        self.toggle_mode_btn.pack(pady=(20, 10))

        self.lan_link_btn = ctk.CTkButton(self.auth_frame, text="⚡ Connect Locally (Same Wi-Fi, No Internet)", fg_color="transparent", text_color="#f39c12", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.show_lan_screen)
        self.lan_link_btn.pack(pady=(0, 20))

        self.switch_to_login_mode()

    def switch_to_login_mode(self):
        self.auth_mode = "login"
        self.logo.configure(text="COMPUTE NEXUS")
        self.username_box.pack_forget()
        self.email_box.pack(after=self.sub, pady=6)
        self.pass_box.pack(after=self.email_box, pady=6)
        self.forgot_btn.pack(after=self.status_lbl, pady=2)
        self.action_btn.configure(text="Sign In")
        self.toggle_mode_btn.configure(text="Don't have an account? Create One", command=self.switch_to_signup_mode)
        self.status_lbl.configure(text="")

    def switch_to_signup_mode(self):
        self.auth_mode = "signup"
        self.logo.configure(text="CREATE ACCOUNT")
        self.username_box.pack(after=self.sub, pady=6)
        self.email_box.pack(after=self.username_box, pady=6)
        self.pass_box.pack(after=self.email_box, pady=6)
        self.forgot_btn.pack_forget()
        self.action_btn.configure(text="Register Account")
        self.toggle_mode_btn.configure(text="Already registered? Sign In Instead", command=self.switch_to_login_mode)
        self.status_lbl.configure(text="")

    def switch_to_forgot_mode(self):
        self.auth_mode = "forgot"
        self.logo.configure(text="RESET PASSWORD")
        self.username_box.pack_forget()
        self.pass_box.pack_forget()
        self.forgot_btn.pack_forget()
        self.action_btn.configure(text="Send Reset Link Email")
        self.toggle_mode_btn.configure(text="Back to Sign In screen", command=self.switch_to_login_mode)
        self.status_lbl.configure(text="Enter your email to receive an official recovery link.", text_color="gray50")

    def submit_auth(self):
        email = self.email_box.get().strip()
        password = self.pass_box.get().strip()
        username = self.username_box.get().strip()

        if self.auth_mode == "forgot":
            if not email:
                self.status_lbl.configure(text="❌ Please specify account email address.", text_color="#E22134")
                return
            self.execute_firebase_password_reset(email)
            return

        if not email or not password:
            self.status_lbl.configure(text="❌ Complete email and password fields.", text_color="#E22134")
            return

        if self.auth_mode == "signup" and not username:
            self.status_lbl.configure(text="❌ Please choose a screen username setup.", text_color="#E22134")
            return

        self.status_lbl.configure(text="⏳ Communicating with network cloud secure tier...", text_color="#f39c12")
        self.update()

        if self.auth_mode == "login":
            self.execute_firebase_login(email, password)
        else:
            if len(password) < 6:
                self.status_lbl.configure(text="❌ Password must scale above 5 characters.", text_color="#E22134")
                return
            self.execute_firebase_signup(email, password, username)

    def execute_firebase_login(self, email, password):
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_WEB_API_KEY}"
        payload = {"email": email, "password": password, "returnSecureToken": True}
        try:
            res = requests.post(url, json=payload, timeout=7)
            data = res.json()
            if res.status_code == 200:
                token = data.get("idToken")
                profile_url = f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={FIREBASE_WEB_API_KEY}"
                p_res = requests.post(profile_url, json={"idToken": token}, timeout=5).json()
                cloud_name = p_res.get("users", [{}])[0].get("displayName", email.split("@")[0])
                
                self.id_token = token
                self.my_email = data.get("email")
                self.my_username = cloud_name.replace(".", "_")
                
                self.auth_frame.pack_forget()
                self.build_main_workspace()
            else:
                self.status_lbl.configure(text=f"❌ Error: {data.get('error',{}).get('message','AUTH_FAILED')}", text_color="#E22134")
        except Exception as e:
            self.status_lbl.configure(text=f"❌ Cloud Connection Error: {e}", text_color="#E22134")

    def execute_firebase_signup(self, email, password, username):
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={FIREBASE_WEB_API_KEY}"
        payload = {"email": email, "password": password, "returnSecureToken": True}
        try:
            res = requests.post(url, json=payload, timeout=7)
            data = res.json()
            if res.status_code == 200:
                token = data.get("idToken")
                up_url = f"https://identitytoolkit.googleapis.com/v1/accounts:updateProfile?key={FIREBASE_WEB_API_KEY}"
                requests.post(up_url, json={"idToken": token, "displayName": username}, timeout=5)
                
                self.status_lbl.configure(text="✅ Account provisioned! Click 'Sign In' below.", text_color="#1DB954")
                self.switch_to_login_mode()
            else:
                self.status_lbl.configure(text=f"❌ Error: {data.get('error',{}).get('message','SIGNUP_FAILED')}", text_color="#E22134")
        except Exception as e:
            self.status_lbl.configure(text=f"❌ Cloud Connection Error: {e}", text_color="#E22134")

    def execute_firebase_password_reset(self, email):
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={FIREBASE_WEB_API_KEY}"
        payload = {"requestType": "PASSWORD_RESET", "email": email}
        try:
            res = requests.post(url, json=payload, timeout=7)
            if res.status_code == 200:
                self.status_lbl.configure(text="📩 Reset email dispatched! Check your spam folder.", text_color="#1DB954")
            else:
                self.status_lbl.configure(text="❌ Account Email destination not registered.", text_color="#E22134")
        except Exception as e:
            self.status_lbl.configure(text=f"❌ Request Timeout: {e}", text_color="#E22134")

    # =========================================================================
    # MAIN WORKSPACE DASHBOARD
    # =========================================================================
    def build_main_workspace(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)

        self.sidebar_frame = ctk.CTkFrame(self, width=220, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(3, weight=1) 

        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="COMPUTE\nNEXUS", font=ctk.CTkFont(size=20, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(30, 20))

        self.join_tab_btn = ctk.CTkButton(self.sidebar_frame, text="Join Room Lobby", command=self.show_join_frame, fg_color="transparent", border_width=2, text_color=("#121212", "#DCE4EE"))
        self.join_tab_btn.grid(row=1, column=0, padx=20, pady=10, sticky="ew")

        self.host_tab_btn = ctk.CTkButton(self.sidebar_frame, text="Host GPU Node", command=self.show_host_frame, fg_color="transparent", border_width=2, text_color=("#121212", "#DCE4EE"))
        self.host_tab_btn.grid(row=2, column=0, padx=20, pady=10, sticky="ew")

        self.profile_trigger_btn = ctk.CTkButton(
            self.sidebar_frame, 
            text=self.my_username[0].upper(), 
            width=42, height=42, 
            corner_radius=21, 
            fg_color="#1DB954", 
            hover_color="#179140",
            font=ctk.CTkFont(weight="bold", size=18),
            command=self.toggle_profile_modal
        )
        self.profile_trigger_btn.grid(row=4, column=0, padx=20, pady=20, sticky="sw")

        self.main_frame = ctk.CTkFrame(self, corner_radius=0, fg_color="#121212")
        self.main_frame.grid(row=0, column=1, sticky="nsew")
        self.main_frame.grid_rowconfigure(0, weight=1)
        self.main_frame.grid_columnconfigure(0, weight=1)

        self.build_join_frame()
        self.build_host_frame()
        self.show_join_frame()

    def toggle_profile_modal(self):
        if self.profile_modal_frame and self.profile_modal_frame.winfo_exists():
            self.profile_modal_frame.destroy()
            self.profile_modal_frame = None
            return

        self.profile_modal_frame = ctk.CTkFrame(self.main_frame, width=280, fg_color="#1c1c1c", border_width=1, border_color="#333333", corner_radius=12)
        self.profile_modal_frame.place(relx=0.02, rely=0.96, anchor="sw")
        
        user_lbl = ctk.CTkLabel(self.profile_modal_frame, text=self.my_username, font=ctk.CTkFont(size=18, weight="bold"), text_color="white")
        user_lbl.pack(pady=(15, 0), padx=20, anchor="w")
        
        email_lbl = ctk.CTkLabel(self.profile_modal_frame, text=self.my_email, font=ctk.CTkFont(size=12), text_color="gray60")
        email_lbl.pack(pady=(0, 15), padx=20, anchor="w")
        
        divider = ctk.CTkFrame(self.profile_modal_frame, height=1, fg_color="#333333")
        divider.pack(fill="x", padx=10, pady=5)
        
        edit_btn = ctk.CTkButton(self.profile_modal_frame, text="✏️ Edit Profile Name", fg_color="transparent", hover_color="#2b2b2b", anchor="w", text_color="white", command=self.handle_edit_profile)
        edit_btn.pack(fill="x", padx=10, pady=5)
        
        logout_btn = ctk.CTkButton(self.profile_modal_frame, text="🚪 Sign Out", fg_color="transparent", hover_color="#3b1f1f", anchor="w", text_color="#E22134", command=self.handle_logout)
        logout_btn.pack(fill="x", padx=10, pady=(5, 10))

    def handle_edit_profile(self):
        dialog = ctk.CTkInputDialog(text="Enter new Display Name:", title="Edit Profile")
        new_name = dialog.get_input()
        
        if new_name and new_name.strip():
            self.my_username = new_name.strip()
            self.profile_trigger_btn.configure(text=self.my_username[0].upper())
            if self.profile_modal_frame and self.profile_modal_frame.winfo_exists():
                self.profile_modal_frame.destroy()
                self.profile_modal_frame = None

    def handle_logout(self):
        self.client_listening = False
        if self.is_hosting:
            self.stop_hosting()
        if self.profile_modal_frame and self.profile_modal_frame.winfo_exists():
            self.profile_modal_frame.destroy()
            self.profile_modal_frame = None
            
        self.id_token = None
        self.my_email = ""
        self.my_username = "User"

        for widget in self.winfo_children():
            widget.pack_forget()
            widget.grid_forget()
            
        self.build_auth_screen()


    def setup_cloud_run_command(self):
        # 1. Get the folder where this .exe is currently running
        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(sys.executable)
        else:
            app_dir = os.path.dirname(os.path.abspath(__file__))

        try:
            # 2. Open the Windows Registry for the current user's environment variables
            reg_key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_ALL_ACCESS)
            
            # 3. Get the current PATH string
            try:
                current_path, _ = winreg.QueryValueEx(reg_key, "Path")
            except FileNotFoundError:
                current_path = ""

            # 4. Check if our folder is already in the PATH
            if app_dir in current_path:
                winreg.CloseKey(reg_key)
                return True # It's already there, we can skip the popup!

            # 5. If not, we append our folder to the PATH
            new_path = current_path + ";" + app_dir if current_path else app_dir
            winreg.SetValueEx(reg_key, "Path", 0, winreg.REG_EXPAND_SZ, new_path)
            winreg.CloseKey(reg_key)

            # 6. Show the user a success popup!
            self.show_path_success_popup()
            return True

        except Exception as e:
            print(f"Failed to set path: {e}")
            return False

    def show_path_success_popup(self):
        pop = ctk.CTkToplevel(self)
        pop.title("Terminal Setup Complete")
        pop.geometry("400x200")
        pop.attributes("-topmost", True)
        
        lbl = ctk.CTkLabel(pop, text="✅ Command Line Tool Ready!", font=ctk.CTkFont(size=18, weight="bold"), text_color="#1DB954")
        lbl.pack(pady=(20, 10))
        
        desc = ctk.CTkLabel(pop, text="We've configured your system so you can now use the\n'cloud-run' command from any terminal on this PC.\n\nNote: If you have a terminal open, restart it for changes to apply.", text_color="gray70")
        desc.pack(pady=5)
        
        btn = ctk.CTkButton(pop, text="Awesome", command=pop.destroy)
        btn.pack(pady=(15, 10))

    # =========================================================================
    # RESOURCE MANAGEMENT INTERFACE 
    # =========================================================================
    
    def build_join_frame(self):
        self.join_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        title = ctk.CTkLabel(self.join_frame, text="Connect to a Node", font=ctk.CTkFont(size=28, weight="bold"))
        title.pack(pady=(60, 5))
        subtitle = ctk.CTkLabel(self.join_frame, text="Securely bind computing tasks using cloud coordinates.", text_color="gray60")
        subtitle.pack(pady=(0, 25))
        
        self.pin_input = ctk.CTkEntry(self.join_frame, width=280, height=45, placeholder_text="6-Digit Room PIN", font=ctk.CTkFont(size=16, weight="bold"), justify="center")
        self.pin_input.pack(pady=8)
        
        self.join_status = ctk.CTkLabel(self.join_frame, text="", font=ctk.CTkFont(size=14))
        self.join_status.pack(pady=10)
        
        self.join_btn = ctk.CTkButton(self.join_frame, text="Join Room Lobby", height=45, width=280, command=self.handle_join)
        self.join_btn.pack(pady=10)

        # Revealed once the host approves this client (see monitor_kick_status)
        self.demo_section = ctk.CTkFrame(self.join_frame, fg_color="#1a1a1a", corner_radius=10)
        self.demo_section.columnconfigure(0, weight=1)
        self.demo_section.columnconfigure(1, weight=1)

        demo_label = ctk.CTkLabel(self.demo_section, text="Run a workload on the host's GPU", font=ctk.CTkFont(size=15, weight="bold"))
        demo_label.grid(row=0, column=0, columnspan=2, padx=15, pady=(15, 8), sticky="w")

        self.preview_local_btn = ctk.CTkButton(self.demo_section, text="🐌 Step 1: Preview Locally (this laptop, no GPU sharing)", height=34,
                                                 fg_color="#3a2a12", hover_color="#4a3618", text_color="#f0a030",
                                                 command=self.run_local_preview)
        self.preview_local_btn.grid(row=1, column=0, columnspan=2, padx=15, pady=(0, 8), sticky="ew")

        self.matmul_btn = ctk.CTkButton(self.demo_section, text="🎲 Matrix Multiply (5000x5000)", height=38, fg_color="#333333", hover_color="#444444",
                                         command=lambda: self.run_demo_script("train_model.py", "Matrix Multiply"))
        self.matmul_btn.grid(row=2, column=0, padx=(15, 5), pady=5, sticky="ew")

        self.raytrace_btn = ctk.CTkButton(self.demo_section, text="🎨 Step 2: Run on Shared GPU (high quality)", height=38,
                                           command=lambda: self.run_demo_script("gpu_raytracer.py", "GPU Ray Tracer"))
        self.raytrace_btn.grid(row=2, column=1, padx=(5, 15), pady=5, sticky="ew")

        self.demo_output_box = ctk.CTkTextbox(self.demo_section, height=130, font=ctk.CTkFont(family="Courier", size=11))
        self.demo_output_box.grid(row=3, column=0, columnspan=2, padx=15, pady=10, sticky="nsew")
        self.demo_output_box.configure(state="disabled")

        self.demo_image_label = ctk.CTkLabel(self.demo_section, text="", fg_color="#0d0d0d", corner_radius=8)
        self.demo_image_label.grid(row=4, column=0, columnspan=2, padx=15, pady=(0, 5), sticky="nsew")

        self.open_render_btn = ctk.CTkButton(self.demo_section, text="📂 Open Full-Resolution Image", height=32, command=self.open_full_render)

    def build_host_frame(self):
        self.host_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        self.host_frame.columnconfigure(0, weight=1)
        self.host_frame.columnconfigure(1, weight=1)
        self.host_frame.rowconfigure(0, weight=1)
        
        self.controls_pane = ctk.CTkFrame(self.host_frame, fg_color="transparent")
        self.controls_pane.grid(row=0, column=0, sticky="nsew", padx=20, pady=20)
        
        self.host_title = ctk.CTkLabel(self.controls_pane, text="Host Your GPU", font=ctk.CTkFont(size=26, weight="bold"))
        self.host_title.pack(pady=(20, 5), anchor="w")
        self.host_status = ctk.CTkLabel(self.controls_pane, text="Ready to share resources.", text_color="gray60", font=ctk.CTkFont(size=13))
        self.host_status.pack(pady=(0, 15), anchor="w")
        
        self.gpu_switch = ctk.CTkSwitch(self.controls_pane, text="Enable NVIDIA Core Acceleration", progress_color="#1DB954")
        self.gpu_switch.pack(pady=10, anchor="w")
        
        self.threshold_frame = ctk.CTkFrame(self.controls_pane, fg_color="transparent")
        self.threshold_frame.pack(pady=10, fill="x")
        self.threshold_label = ctk.CTkLabel(self.threshold_frame, text="Auto-Stop CPU Threshold: 90%", font=ctk.CTkFont(size=12))
        self.threshold_label.pack(anchor="w")
        self.threshold_slider = ctk.CTkSlider(self.threshold_frame, from_=20, to=100, command=self.update_slider_label, progress_color="#1DB954")
        self.threshold_slider.set(90)
        self.threshold_slider.pack(pady=5, fill="x")

        self.host_btn = ctk.CTkButton(self.controls_pane, text="Start Hosting", height=45, command=self.start_hosting_thread)
        self.host_btn.pack(pady=15, fill="x")
        
        self.pin_display_label = ctk.CTkLabel(self.controls_pane, text="", font=ctk.CTkFont(size=32, weight="bold"), text_color="#1DB954")
        self.copy_btn = ctk.CTkButton(self.controls_pane, text="Copy PIN", height=35, command=self.copy_pin)
        self.stop_btn = ctk.CTkButton(self.controls_pane, text="Stop Hosting Node", height=45, fg_color="#E22134", hover_color="#B01625", command=self.stop_hosting)
        self.telemetry_label = ctk.CTkLabel(self.controls_pane, text="", font=ctk.CTkFont(family="Courier", size=11), text_color="#1DB954")
        
        self.lobby_pane = ctk.CTkFrame(self.host_frame, fg_color="#1a1a1a", corner_radius=10)
        self.lobby_pane.grid(row=0, column=1, sticky="nsew", padx=20, pady=30)
        
        self.lobby_title = ctk.CTkLabel(self.lobby_pane, text="Connected Clients Lobby", font=ctk.CTkFont(size=16, weight="bold"))
        self.lobby_title.pack(pady=(15, 5))
        
        self.allow_all_btn = ctk.CTkButton(self.lobby_pane, text="Allow All Pending", height=30, fg_color="#f39c12", hover_color="#d68910", command=self.provider_allow_all)
        self.allow_all_btn.pack(pady=(0, 10))

        self.client_scroll_frame = ctk.CTkScrollableFrame(self.lobby_pane, fg_color="transparent")
        self.client_scroll_frame.pack(expand=True, fill="both", padx=10, pady=10)

    def update_slider_label(self, val):
        self.threshold_label.configure(text=f"Auto-Stop CPU Threshold: {int(val)}%")

    def show_join_frame(self):
        self.host_frame.pack_forget()
        self.join_frame.pack(expand=True, fill="both")
        self.join_tab_btn.configure(fg_color=["#3b8ed0", "#1f538d"])
        self.host_tab_btn.configure(fg_color="transparent")

    def show_host_frame(self):
        self.join_frame.pack_forget()
        self.host_frame.pack(expand=True, fill="both")
        self.host_tab_btn.configure(fg_color=["#3b8ed0", "#1f538d"])
        self.join_tab_btn.configure(fg_color="transparent")

    def handle_join(self):
        username = self.my_username
        pin = self.pin_input.get().strip()
        
        if len(pin) != 6 or not pin.isdigit():
            self.join_status.configure(text="❌ Please enter a valid 6-digit number.", text_color="#E22134")
            return
            
        # ---> NEW TRIGGER: Run the silent PATH check right here <---
        self.setup_cloud_run_command()
            
        self.join_status.configure(text="🔍 Validating Room with Backend...", text_color="#f39c12")
        self.update()

        try:
            fb_url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/default/documents/rooms/{pin}"
            response = requests.get(fb_url, timeout=5)
            if response.status_code != 200:
                self.join_status.configure(text="❌ Room not found or expired.", text_color="#E22134")
                return
                
            data = response.json()
            node_url = data.get("fields", {}).get("url", {}).get("stringValue")
            api_key = data.get("fields", {}).get("api_key", {}).get("stringValue")
            
            client_url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/default/documents/rooms/{pin}/clients?documentId={username}"
            reg_payload = {"fields": {"status": {"stringValue": "pending"}, "email": {"stringValue": self.my_email}}}
            reg_res = requests.post(client_url, json=reg_payload, timeout=5)
            
            if reg_res.status_code != 200:
                requests.patch(f"{client_url.split('?')[0]}/{username}?updateMask.fieldPaths=status", json=reg_payload, timeout=5)
                
            config_path = os.path.expanduser("~/.gpu_share_config.json")
            with open(config_path, "w") as f:
                json.dump({"url": node_url, "api_key": api_key, "client_name": username}, f)
                
            self.current_pin = pin
            self.join_status.configure(text=f"⏳ Linked securely! Awaiting host authorization...", text_color="#f39c12")
            
            self.client_listening = True
            threading.Thread(target=self.monitor_kick_status, daemon=True).start()
        except Exception:
            self.join_status.configure(text=f"❌ Network Error connecting to Firestore.", text_color="#E22134")

    def monitor_kick_status(self):
        while getattr(self, 'client_listening', False):
            if not self.current_pin: break
            url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/default/documents/rooms/{self.current_pin}/clients/{self.my_username}"
            try:
                res = requests.get(url, timeout=5)
                if res.status_code == 404:
                    self.after(0, lambda: self.join_status.configure(text="❌ Access denied or kicked by provider.", text_color="#E22134"))
                    self.client_listening = False
                    break
                elif res.status_code == 200:
                    status = res.json().get("fields", {}).get("status", {}).get("stringValue", "pending")
                    if status == "approved":
                        self.after(0, lambda: self.join_status.configure(text="✅ Access Verified! Secure connection to Node active.", text_color="#1DB954"))
                        if not self._demo_section_revealed:
                            self._demo_section_revealed = True
                            self.after(0, self.reveal_demo_section)
            except Exception: pass
            time.sleep(3)

    # --- NEW: GRACEFUL DEPENDENCY CHECKER ---
    def show_dependency_error(self, missing_docker, docker_not_running):
        err_win = ctk.CTkToplevel(self)
        err_win.title("Hosting Requirements Missing")
        err_win.geometry("450x220")
        err_win.attributes("-topmost", True)
        
        lbl = ctk.CTkLabel(err_win, text="⚠️ Cannot Start Host Node", font=ctk.CTkFont(size=20, weight="bold"), text_color="#E22134")
        lbl.pack(pady=(20, 10))

        if missing_docker:
            desc = ctk.CTkLabel(err_win, text="To share your compute power, you must install Docker.", text_color="gray70")
            desc.pack(pady=(0, 15))
            d_btn = ctk.CTkButton(err_win, text="Download Docker Desktop", fg_color="#1f538d", 
                                  command=lambda: webbrowser.open("https://www.docker.com/products/docker-desktop/"))
            d_btn.pack(pady=5)
            
        elif docker_not_running:
            desc = ctk.CTkLabel(err_win, text="Docker is installed, but the background engine is off.", text_color="gray70")
            desc.pack(pady=(0, 15))
            warning = ctk.CTkLabel(err_win, text="Please open Docker Desktop on your PC and try again.", font=ctk.CTkFont(weight="bold"), text_color="#f39c12")
            warning.pack(pady=5)
            
        ok_btn = ctk.CTkButton(err_win, text="Got it", fg_color="#333333", hover_color="#444444", command=err_win.destroy)
        ok_btn.pack(pady=(15, 10))

    def start_hosting_thread(self):
        # 1. Check Docker status
        has_docker = shutil.which("docker") is not None
        docker_not_running = False
        
        if has_docker:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            try:
                res = subprocess.run(["docker", "info"], capture_output=True, startupinfo=startupinfo)
                if res.returncode != 0:
                    docker_not_running = True
            except Exception:
                docker_not_running = True

        # 2. Trigger appropriate popup if Docker has issues
        if not has_docker or docker_not_running:
            self.show_dependency_error(not has_docker, docker_not_running)
            return

        # If Docker is good, lock UI and proceed to download Cloudflared / boot
        self.host_btn.configure(state="disabled")
        self.gpu_switch.configure(state="disabled")
        self.threshold_slider.configure(state="disabled")
        self.is_hosting = True
        
        # Start the prep thread so UI doesn't freeze during download
        threading.Thread(target=self.prepare_and_boot, daemon=True).start()
    
    def prepare_and_boot(self):
        # 3. Cloudflared Auto-Download Logic
        has_cloudflared_path = shutil.which("cloudflared") is not None
        has_cloudflared_local = os.path.exists("cloudflared.exe")

        if not has_cloudflared_path and not has_cloudflared_local:
            self.after(0, lambda: self.host_status.configure(text="Downloading Cloudflare Tunnel (One-time)...", text_color="#f39c12"))
            try:
                # Downloads the official Windows binary directly from Cloudflare
                url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
                urllib.request.urlretrieve(url, "cloudflared.exe")
            except Exception as e:
                self.after(0, lambda: self.host_status.configure(text="❌ Failed to download network tunnel.", text_color="#E22134"))
                self.after(0, self.reset_host_ui)
                return

        # Move onto the actual Docker booting
        self.boot_node()

    def get_free_port(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("", 0))
        s.listen(1)
        port = s.getsockname()[1]
        s.close()
        return port

    def boot_node(self):
        self.after(0, lambda: self.host_status.configure(text="Provisioning secure container...", text_color="#f39c12"))

        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        use_gpu = self.gpu_switch.get() == 1
        subprocess.run(["docker", "rm", "-f", "gpu_share_node"], capture_output=True, startupinfo=startupinfo)
        
        if use_gpu:
            res = subprocess.run(["docker", "run", "-d", "--gpus", "all", "--name", "gpu_share_node", "pytorch/pytorch:latest", "tail", "-f", "/dev/null"], capture_output=True, text=True, startupinfo=startupinfo)
        else:
            res = subprocess.run(["docker", "run", "-d", "--name", "gpu_share_node", "python:3.10-slim", "tail", "-f", "/dev/null"], capture_output=True, text=True, startupinfo=startupinfo)

        if res.returncode != 0:
            self.after(0, lambda: self.host_status.configure(text="❌ Docker provisioning failed.", text_color="#E22134"))
            self.after(0, self.reset_host_ui)
            return

        api_key = secrets.token_urlsafe(16)
        with open("agent_server.py", "w") as f: f.write(AGENT_SERVER_CODE)
        
        dynamic_port = self.get_free_port()
        env = os.environ.copy()
        env["GPU_API_KEY"] = api_key
        env["AGENT_PORT"] = str(dynamic_port)
        
        self.agent_proc = subprocess.Popen(["python", "agent_server.py"], env=env, startupinfo=startupinfo)
        
        # Determine whether to use the local cloudflared.exe or the one in the system PATH
        cf_command = "cloudflared.exe" if os.path.exists("cloudflared.exe") else "cloudflared"
        
        self.tunnel_proc = subprocess.Popen([cf_command, "tunnel", "--url", f"http://127.0.0.1:{dynamic_port}"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, startupinfo=startupinfo)

        time.sleep(3)
        for line in self.tunnel_proc.stdout:
            if "trycloudflare.com" in line and "https://" in line:
                url = line.split("https://")[1].split()[0].replace("|", "").replace("\\", "").strip()
                full_url = f"https://{url}"
                pin = "".join(secrets.choice("0123456789") for _ in range(6))
                self.current_pin = pin
                
                fb_url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/default/documents/rooms?documentId={pin}"
                payload = {
                    "fields": {
                        "url": {"stringValue": full_url},
                        "api_key": {"stringValue": api_key},
                        "hardware": {"stringValue": "GPU" if use_gpu else "CPU"},
                        "status": {"stringValue": "live"},
                        "host_email": {"stringValue": self.my_email}
                    }
                }
                res = requests.post(fb_url, json=payload, timeout=5)
                if res.status_code != 200:
                    self.after(0, lambda: self.host_status.configure(text=f"❌ Backend database connection rejected.", text_color="#E22134"))
                    self.after(0, self.stop_hosting)
                    return
                self.after(0, self.show_host_success, pin)
                break

    def show_host_success(self, pin):
        self.host_status.configure(text="🟢 Live & Broadcasting!", text_color="#1DB954")
        self.host_btn.pack_forget()
        self.pin_display_label.configure(text=f"ROOM PIN: {pin}")
        self.pin_display_label.pack(pady=5)
        self.copy_btn.pack(pady=5, fill="x")
        self.stop_btn.pack(pady=10, fill="x")
        self.telemetry_label.pack(pady=5)
        
        self.monitor_thread_active = True
        threading.Thread(target=self.resource_monitor_loop, daemon=True).start()
        threading.Thread(target=self.cloud_lobby_listener, daemon=True).start()

    def cloud_lobby_listener(self):
        self.known_clients_dict = {}
        while self.monitor_thread_active and self.is_hosting:
            if not self.current_pin:
                time.sleep(2); continue
            url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/default/documents/rooms/{self.current_pin}:runQuery"
            payload = {"structuredQuery": {"from": [{"collectionId": "clients"}]}}
            try:
                response = requests.post(url, json=payload, timeout=5)
                if response.status_code == 200:
                    data = response.json()
                    current_clients = {}
                    for item in data:
                        if "document" in item:
                            doc = item["document"]
                            doc_path = doc.get("name", "")
                            fields = doc.get("fields", {})
                            if doc_path:
                                username = doc_path.split("/")[-1]
                                status = fields.get("status", {}).get("stringValue", "pending")
                                email = fields.get("email", {}).get("stringValue", "No Email Provided")
                                current_clients[username] = {"status": status, "email": email}
                    
                    if current_clients != self.known_clients_dict:
                        self.known_clients_dict = current_clients
                        simple_status_map = {u: d["status"] for u, d in self.known_clients_dict.items()}
                        try:
                            with open("allowed_clients.json", "w") as f: json.dump(simple_status_map, f)
                        except Exception: pass
                        self.after(0, self.refresh_lobby_ui)
            except Exception: pass
            time.sleep(2)

    def refresh_lobby_ui(self):
        for widget in self.client_scroll_frame.winfo_children(): widget.destroy()
        if not self.known_clients_dict:
            waiting_label = ctk.CTkLabel(self.client_scroll_frame, text="Waiting for clients...", text_color="gray", font=ctk.CTkFont(slant="italic"))
            waiting_label.pack(pady=20)
            return

        for client_name, data in self.known_clients_dict.items():
            status = data["status"]
            email = data["email"]
            row = ctk.CTkFrame(self.client_scroll_frame, fg_color="#262626", height=50)
            row.pack(fill="x", pady=4, padx=5)
            row.pack_propagate(False)
            
            text_frame = ctk.CTkFrame(row, fg_color="transparent")
            text_frame.pack(side="left", padx=10, fill="y", pady=5)

            if status == "pending":
                c_lbl = ctk.CTkLabel(text_frame, text=f"⏳ {client_name}", font=ctk.CTkFont(size=12, weight="bold"), text_color="#f39c12")
                c_lbl.pack(anchor="w")
                e_lbl = ctk.CTkLabel(text_frame, text=email, font=ctk.CTkFont(size=10), text_color="gray")
                e_lbl.pack(anchor="w")
                kick_btn = ctk.CTkButton(row, text="Decline", width=65, height=28, fg_color="#E22134", hover_color="#B01625", command=lambda c=client_name: threading.Thread(target=self.provider_kick_client, args=(c,)).start())
                kick_btn.pack(side="right", padx=5)
                allow_btn = ctk.CTkButton(row, text="Allow", width=65, height=28, fg_color="#1DB954", hover_color="#179140", command=lambda c=client_name: threading.Thread(target=self.provider_allow_client, args=(c,)).start())
                allow_btn.pack(side="right", padx=5)
            elif status == "approved":
                c_lbl = ctk.CTkLabel(text_frame, text=f"✅ {client_name}", font=ctk.CTkFont(size=12, weight="bold"), text_color="#1DB954")
                c_lbl.pack(anchor="w")
                e_lbl = ctk.CTkLabel(text_frame, text=email, font=ctk.CTkFont(size=10), text_color="gray")
                e_lbl.pack(anchor="w")
                kick_btn = ctk.CTkButton(row, text="Kick", width=65, height=28, fg_color="#E22134", hover_color="#B01625", command=lambda c=client_name: threading.Thread(target=self.provider_kick_client, args=(c,)).start())
                kick_btn.pack(side="right", padx=5)

    def provider_allow_client(self, username):
        url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/default/documents/rooms/{self.current_pin}/clients/{username}?updateMask.fieldPaths=status"
        payload = {"fields": {"status": {"stringValue": "approved"}}}
        try: requests.patch(url, json=payload, timeout=5)
        except Exception: pass

    def provider_allow_all(self):
        if dict_val := getattr(self, 'known_clients_dict', {}):
            for username, status_data in dict_val.items():
                if status_data.get("status") == "pending":
                    threading.Thread(target=self.provider_allow_client, args=(username,), daemon=True).start()

    def provider_kick_client(self, username):
        url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/default/documents/rooms/{self.current_pin}/clients/{username}"
        try: requests.delete(url, timeout=5)
        except Exception: pass

    def resource_monitor_loop(self):
        limit = int(self.threshold_slider.get())
        consecutive_violations = 0
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        while self.monitor_thread_active and self.is_hosting:
            stat_res = subprocess.run(["docker", "stats", "gpu_share_node", "--no-stream", "--format", "{{.CPUPerc}} | {{.MemUsage}}"], capture_output=True, text=True, startupinfo=startupinfo)
            if stat_res.returncode == 0 and stat_res.stdout.strip():
                try:
                    raw_out = stat_res.stdout.strip()
                    cpu_part = raw_out.split("|")[0].replace("%", "").strip()
                    mem_part = raw_out.split("|")[1].strip()
                    self.after(0, lambda c=cpu_part, m=mem_part: self.telemetry_label.configure(text=f"STATS -> CPU: {c}% | RAM: {m}"))
                    if float(cpu_part) > limit:
                        consecutive_violations += 1
                        if consecutive_violations >= 5: self.after(0, self.stop_hosting); break
                    else: consecutive_violations = 0
                except ValueError: pass
            time.sleep(1)

    def stop_hosting(self):
        self.host_status.configure(text="Stopping host node and cleaning resources...", text_color="#f39c12")
        self.update()

        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        # 1. ONLY try to clean up Docker if Docker actually exists on this system!
        if shutil.which("docker") is not None:
            try:
                subprocess.run(["docker", "rm", "-f", "gpu_share_node"], capture_output=True, startupinfo=startupinfo)
            except Exception as e:
                print(f"Safe Bypass: Docker cleanup skipped or failed ({e})")
        else:
            print("Safe Bypass: Docker not installed, skipping container removal.")

        # 2. Safely terminate the local agent process if it exists
        if hasattr(self, 'agent_proc') and self.agent_proc:
            try:
                self.agent_proc.terminate()
            except Exception:
                pass
            self.agent_proc = None

        # 3. Safely terminate the tunnel process if it exists
        if hasattr(self, 'tunnel_proc') and self.tunnel_proc:
            try:
                self.tunnel_proc.terminate()
            except Exception:
                pass
            self.tunnel_proc = None

        # 4. Safely wipe the room database entry if we generated a PIN
        if hasattr(self, 'current_pin') and self.current_pin:
            try:
                fb_url = f"https://firestore.googleapis.com/v1/projects/{FIREBASE_PROJECT_ID}/databases/default/documents/rooms/{self.current_pin}"
                requests.delete(fb_url, timeout=3)
            except Exception:
                pass
            self.current_pin = None

        self.is_hosting = False
        self.reset_host_ui()

    def reset_host_ui(self):
        self.telemetry_label.pack_forget()
        self.pin_display_label.pack_forget()
        self.copy_btn.pack_forget()
        self.stop_btn.pack_forget()
        for w in self.client_scroll_frame.winfo_children(): w.destroy()
        self.host_btn.pack(pady=15, fill="x")
        self.host_btn.configure(state="normal")
        self.gpu_switch.configure(state="normal")
        self.threshold_slider.configure(state="normal")
        self.host_status.configure(text="Ready to share resources.", text_color="gray60")

    def copy_pin(self):
        if self.current_pin:
            self.clipboard_clear()
            self.clipboard_append(self.current_pin)

    # =========================================================================
    # RUN DEMO WORKLOAD (client side) -- same /execute channel used by the
    # cloud-run CLI, just driven from the GUI with a textbox + image preview
    # instead of a terminal.
    # =========================================================================
    def reveal_demo_section(self):
        self.demo_section.pack(pady=(20, 10), fill="both", expand=True, padx=10)

    def run_demo_script(self, filename, display_name):
        if self._demo_running:
            return

        config_path = os.path.expanduser("~/.gpu_share_config.json")
        if not os.path.exists(config_path):
            self.append_demo_output("❌ Not connected to a host yet.\n")
            return

        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(sys.executable)
        else:
            app_dir = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(app_dir, filename)
        if not os.path.exists(script_path):
            self.append_demo_output(f"❌ {filename} not found next to dashboard.py.\n")
            return

        self._demo_running = True
        self.matmul_btn.configure(state="disabled")
        self.raytrace_btn.configure(state="disabled")
        self.preview_local_btn.configure(state="disabled")
        self.open_render_btn.grid_forget()
        self.demo_image_label.configure(image=None, text="")

        self.demo_output_box.configure(state="normal")
        self.demo_output_box.delete("1.0", "end")
        self.demo_output_box.insert("end", f"▶ Running {display_name} ({filename}) on the remote GPU...\n\n")
        self.demo_output_box.configure(state="disabled")

        threading.Thread(target=self._run_demo_worker, args=(script_path, filename), daemon=True).start()

    def _run_demo_worker(self, script_path, filename):
        try:
            with open(os.path.expanduser("~/.gpu_share_config.json"), "r") as f:
                config = json.load(f)
            url = config.get("url")
            api_key = config.get("api_key")
            client_name = config.get("client_name", "Unknown")
            if not url or not api_key:
                self.after(0, self.append_demo_output, "❌ Invalid connection config. Join the room again.\n")
                return

            with open(script_path, "r", encoding="utf-8") as f:
                script_content = f.read()

            payload = {"client_name": client_name, "target_file": filename, "script_content": script_content}
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

            buffer = ""
            capturing = False
            capture_name = None
            capture_chunks = []

            with requests.post(f"{url}/execute", json=payload, headers=headers, stream=True, timeout=(10, 300)) as response:
                if response.status_code == 403:
                    self.after(0, self.append_demo_output, "❌ Denied - you are not approved in the host's lobby yet.\n")
                    return
                if response.status_code != 200:
                    self.after(0, self.append_demo_output, f"❌ Server returned status {response.status_code}.\n")
                    return

                for chunk in response.iter_content(chunk_size=None):
                    if not chunk:
                        continue
                    buffer += chunk.decode('utf-8', errors='replace')
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        stripped = line.rstrip("\r")
                        if not capturing:
                            if stripped.startswith("##CLOUDRUN_FILE_START:") and stripped.endswith("##"):
                                capture_name = stripped[len("##CLOUDRUN_FILE_START:"):-2]
                                capturing = True
                                capture_chunks = []
                                self.after(0, self.append_demo_output, f"\n📦 Receiving {capture_name} ...\n")
                            else:
                                self.after(0, self.append_demo_output, line + "\n")
                        else:
                            if stripped == "##CLOUDRUN_FILE_END##":
                                try:
                                    data = base64.b64decode("".join(capture_chunks))
                                    self.after(0, self.show_demo_image, data, capture_name)
                                except Exception as e:
                                    self.after(0, self.append_demo_output, f"❌ Failed to decode received file: {e}\n")
                                capturing = False
                            else:
                                capture_chunks.append(stripped)

            self.after(0, self.append_demo_output, "\n✔ Done.\n")
        except requests.exceptions.RequestException as e:
            self.after(0, self.append_demo_output, f"\n❌ Connection Error: {e}\n")
        except Exception as e:
            self.after(0, self.append_demo_output, f"\n❌ Unexpected error: {e}\n")
        finally:
            self.after(0, self._demo_finished)

    def append_demo_output(self, text):
        self.demo_output_box.configure(state="normal")
        self.demo_output_box.insert("end", text)
        self.demo_output_box.see("end")
        self.demo_output_box.configure(state="disabled")

    def show_demo_image(self, image_bytes, filename):
        try:
            pil_img = Image.open(io.BytesIO(image_bytes))
            pil_img.load()
        except Exception as e:
            self.append_demo_output(f"❌ Could not open received image: {e}\n")
            return

        out_dir = os.path.expanduser("~/ComputeNexus_Renders")
        try:
            os.makedirs(out_dir, exist_ok=True)
            out_path = os.path.join(out_dir, filename)
            pil_img.save(out_path)
            self._last_render_path = out_path
        except Exception:
            self._last_render_path = None

        preview = pil_img.copy()
        preview.thumbnail((520, 300))
        ctk_img = ctk.CTkImage(light_image=preview, dark_image=preview, size=preview.size)
        self.demo_image_label.configure(image=ctk_img, text="")
        self._demo_image_ref = ctk_img  # keep a reference so Tk doesn't garbage-collect it

        if self._last_render_path:
            self.open_render_btn.grid(row=5, column=0, columnspan=2, padx=15, pady=(0, 15), sticky="ew")

    def _demo_finished(self):
        self._demo_running = False
        self.matmul_btn.configure(state="normal")
        self.raytrace_btn.configure(state="normal")
        self.preview_local_btn.configure(state="normal")

    def run_local_preview(self):
        """Renders the same ray tracer script right here on this laptop, at
        low quality, with no host/network involved at all -- this is the
        'before' half of the low-quality-local vs high-quality-shared-GPU
        comparison. Doesn't require being joined/approved to any room."""
        if self._demo_running:
            return

        if getattr(sys, 'frozen', False):
            app_dir = os.path.dirname(sys.executable)
        else:
            app_dir = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(app_dir, "gpu_raytracer.py")
        if not os.path.exists(script_path):
            self.append_demo_output("❌ gpu_raytracer.py not found next to dashboard.py.\n")
            return

        self._demo_running = True
        self.matmul_btn.configure(state="disabled")
        self.raytrace_btn.configure(state="disabled")
        self.preview_local_btn.configure(state="disabled")
        self.open_render_btn.grid_forget()
        self.demo_image_label.configure(image=None, text="")

        self.demo_output_box.configure(state="normal")
        self.demo_output_box.delete("1.0", "end")
        self.demo_output_box.insert("end", "▶ Rendering locally on THIS laptop (low quality, no GPU sharing)...\n\n")
        self.demo_output_box.configure(state="disabled")

        threading.Thread(target=self._run_local_preview_worker, args=(script_path,), daemon=True).start()

    def _run_local_preview_worker(self, script_path):
        out_path = os.path.join(os.path.expanduser("~"), "_cn_local_preview.png")
        try:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

            proc = subprocess.Popen(
                [sys.executable, script_path, "--quality", "low", "--out", out_path, "--no-send"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, startupinfo=startupinfo,
            )
            for line in proc.stdout:
                self.after(0, self.append_demo_output, line if line.endswith("\n") else line + "\n")
            proc.wait()

            if proc.returncode == 0 and os.path.exists(out_path):
                with open(out_path, "rb") as f:
                    data = f.read()
                self.after(0, self.show_demo_image, data, "local_preview.png")
            else:
                self.after(0, self.append_demo_output, "❌ Local render failed.\n")
        except Exception as e:
            self.after(0, self.append_demo_output, f"❌ Unexpected error: {e}\n")
        finally:
            self.after(0, self._demo_finished)

    def open_full_render(self):
        if self._last_render_path and os.path.exists(self._last_render_path):
            try:
                os.startfile(self._last_render_path)
            except Exception:
                pass

    # =========================================================================
    # CONNECT LOCALLY (LAN) -- an alternate path from the sign-in screen that
    # skips Firebase/Cloudflare entirely: pair via a 6-digit code + LAN
    # broadcast discovery instead. Reuses the same /execute engine and the
    # same ~/.gpu_share_config.json the demo-runner panel already reads, so
    # once paired, everything downstream (Join tab's demo buttons) works
    # completely unchanged.
    # =========================================================================
    def get_lan_ip(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.168.255.255", 1))
            return s.getsockname()[0]
        except Exception:
            return "127.0.0.1"
        finally:
            s.close()

    def show_lan_screen(self):
        self.auth_frame.pack_forget()
        if not hasattr(self, 'lan_frame'):
            self.build_lan_screen()
        self.lan_frame.pack(expand=True, fill="both")

    def back_to_signin_from_lan(self):
        self.lan_frame.pack_forget()
        self.auth_frame.pack(expand=True, fill="both")

    def build_lan_screen(self):
        self.lan_frame = ctk.CTkFrame(self, fg_color="#121212")

        back_btn = ctk.CTkButton(self.lan_frame, text="← Back to Sign In", fg_color="transparent", text_color="gray50", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.back_to_signin_from_lan)
        back_btn.pack(pady=(20, 0), anchor="w", padx=20)

        logo = ctk.CTkLabel(self.lan_frame, text="CONNECT LOCALLY", font=ctk.CTkFont(size=30, weight="bold"), text_color="#1DB954")
        logo.pack(pady=(10, 5))
        sub = ctk.CTkLabel(self.lan_frame, text="Same Wi-Fi network, no internet or sign-in needed.", text_color="gray50", font=ctk.CTkFont(size=13))
        sub.pack(pady=(0, 20))

        tabs = ctk.CTkFrame(self.lan_frame, fg_color="transparent")
        tabs.pack(pady=(0, 15))
        self.lan_host_tab_btn = ctk.CTkButton(tabs, text="Host My GPU", width=180, command=self.show_lan_host_pane)
        self.lan_host_tab_btn.grid(row=0, column=0, padx=5)
        self.lan_join_tab_btn = ctk.CTkButton(tabs, text="Join a GPU", width=180, command=self.show_lan_join_pane, fg_color="transparent", border_width=2, text_color=("#121212", "#DCE4EE"))
        self.lan_join_tab_btn.grid(row=0, column=1, padx=5)

        self.lan_pane_container = ctk.CTkFrame(self.lan_frame, fg_color="transparent")
        self.lan_pane_container.pack(expand=True, fill="both", padx=40)

        # --- Host pane ---
        self.lan_host_pane = ctk.CTkFrame(self.lan_pane_container, fg_color="transparent")

        self.lan_name_entry_host = ctk.CTkEntry(self.lan_host_pane, width=300, height=40, placeholder_text="Display Name")
        self.lan_name_entry_host.insert(0, socket.gethostname())
        self.lan_name_entry_host.pack(pady=8)

        self.lan_gpu_switch = ctk.CTkSwitch(self.lan_host_pane, text="Enable NVIDIA Core Acceleration", progress_color="#1DB954")
        self.lan_gpu_switch.pack(pady=8)

        self.lan_host_status = ctk.CTkLabel(self.lan_host_pane, text="", text_color="gray60", font=ctk.CTkFont(size=13))
        self.lan_host_status.pack(pady=5)

        self.lan_start_host_btn = ctk.CTkButton(self.lan_host_pane, text="⚡ Start Hosting (LAN)", height=45, width=300, command=self.start_lan_hosting)
        self.lan_start_host_btn.pack(pady=8)

        self.lan_host_info_label = ctk.CTkLabel(self.lan_host_pane, text="", font=ctk.CTkFont(size=15, weight="bold"), text_color="#1DB954", justify="center")
        self.lan_code_label = ctk.CTkLabel(self.lan_host_pane, text="", font=ctk.CTkFont(size=34, weight="bold"), text_color="#1DB954")
        self.lan_stop_host_btn = ctk.CTkButton(self.lan_host_pane, text="Stop Hosting", height=40, width=300, fg_color="#E22134", hover_color="#B01625", command=self.stop_lan_hosting)

        # --- Join pane ---
        self.lan_join_pane = ctk.CTkFrame(self.lan_pane_container, fg_color="transparent")

        self.lan_name_entry_join = ctk.CTkEntry(self.lan_join_pane, width=300, height=40, placeholder_text="Display Name")
        self.lan_name_entry_join.insert(0, socket.gethostname())
        self.lan_name_entry_join.pack(pady=8)

        self.lan_scan_btn = ctk.CTkButton(self.lan_join_pane, text="🔍 Scan for Nearby Hosts", height=38, width=300, command=self.scan_lan_hosts)
        self.lan_scan_btn.pack(pady=8)

        self.lan_scan_results = ctk.CTkFrame(self.lan_join_pane, fg_color="transparent", width=300)
        self.lan_scan_results.pack(pady=2, fill="x")

        self.lan_ip_entry = ctk.CTkEntry(self.lan_join_pane, width=300, height=40, placeholder_text="Host IP:Port (auto-filled, or type it)")
        self.lan_ip_entry.pack(pady=8)

        self.lan_code_entry = ctk.CTkEntry(self.lan_join_pane, width=300, height=40, placeholder_text="6-Digit Code from Host", font=ctk.CTkFont(size=16, weight="bold"), justify="center")
        self.lan_code_entry.pack(pady=8)

        self.lan_join_status = ctk.CTkLabel(self.lan_join_pane, text="", text_color="gray60", font=ctk.CTkFont(size=13))
        self.lan_join_status.pack(pady=5)

        self.lan_connect_btn = ctk.CTkButton(self.lan_join_pane, text="Connect", height=45, width=300, command=self.connect_lan_client)
        self.lan_connect_btn.pack(pady=8)

        self.show_lan_host_pane()

    def show_lan_host_pane(self):
        self.lan_join_pane.pack_forget()
        self.lan_host_pane.pack(expand=True, fill="both")
        self.lan_host_tab_btn.configure(fg_color=["#3b8ed0", "#1f538d"])
        self.lan_join_tab_btn.configure(fg_color="transparent")

    def show_lan_join_pane(self):
        self.lan_host_pane.pack_forget()
        self.lan_join_pane.pack(expand=True, fill="both")
        self.lan_join_tab_btn.configure(fg_color=["#3b8ed0", "#1f538d"])
        self.lan_host_tab_btn.configure(fg_color="transparent")

    # --- LAN hosting (provider side) ---
    def start_lan_hosting(self):
        has_docker = shutil.which("docker") is not None
        docker_not_running = False
        if has_docker:
            startupinfo = None
            if os.name == 'nt':
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            try:
                res = subprocess.run(["docker", "info"], capture_output=True, startupinfo=startupinfo)
                if res.returncode != 0:
                    docker_not_running = True
            except Exception:
                docker_not_running = True

        if not has_docker or docker_not_running:
            self.show_dependency_error(not has_docker, docker_not_running)
            return

        self.lan_start_host_btn.configure(state="disabled")
        self.lan_gpu_switch.configure(state="disabled")
        self.lan_name_entry_host.configure(state="disabled")
        self.lan_host_status.configure(text="Provisioning local container...", text_color="#f39c12")

        self.is_lan_hosting = True
        threading.Thread(target=self.boot_lan_node, daemon=True).start()

    def boot_lan_node(self):
        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        use_gpu = self.lan_gpu_switch.get() == 1
        subprocess.run(["docker", "rm", "-f", "gpu_share_node"], capture_output=True, startupinfo=startupinfo)

        if use_gpu:
            res = subprocess.run(["docker", "run", "-d", "--gpus", "all", "--name", "gpu_share_node", "pytorch/pytorch:latest", "tail", "-f", "/dev/null"], capture_output=True, text=True, startupinfo=startupinfo)
        else:
            res = subprocess.run(["docker", "run", "-d", "--name", "gpu_share_node", "python:3.10-slim", "tail", "-f", "/dev/null"], capture_output=True, text=True, startupinfo=startupinfo)

        if res.returncode != 0:
            self.after(0, lambda: self.lan_host_status.configure(text="❌ Docker provisioning failed.", text_color="#E22134"))
            self.after(0, self.reset_lan_host_ui)
            return

        api_key = secrets.token_urlsafe(16)
        lan_code = "".join(secrets.choice("0123456789") for _ in range(6))
        with open("agent_server.py", "w") as f:
            f.write(LAN_AGENT_SERVER_CODE)

        dynamic_port = self.get_free_port()
        env = os.environ.copy()
        env["GPU_API_KEY"] = api_key
        env["AGENT_PORT"] = str(dynamic_port)
        env["LAN_CODE"] = lan_code

        self.agent_proc = subprocess.Popen(["python", "agent_server.py"], env=env, startupinfo=startupinfo)

        self.lan_code = lan_code
        self.lan_port = dynamic_port
        self.lan_announce_stop = threading.Event()
        host_name = self.lan_name_entry_host.get().strip() or socket.gethostname()
        threading.Thread(target=self.lan_announce_loop, args=(self.lan_announce_stop, host_name, dynamic_port), daemon=True).start()

        self.after(0, self.show_lan_host_success, lan_code, dynamic_port)

    def lan_announce_loop(self, stop_event, name, port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        ip = self.get_lan_ip()
        payload = json.dumps({"type": "COMPUTE_NEXUS_LAN_HOST", "name": name, "ip": ip, "port": port}).encode('utf-8')
        try:
            while not stop_event.is_set():
                try:
                    sock.sendto(payload, ('255.255.255.255', 47600))
                except Exception:
                    pass
                stop_event.wait(1.0)
        finally:
            sock.close()

    def show_lan_host_success(self, code, port):
        ip = self.get_lan_ip()
        self.lan_host_status.configure(text="🟢 LAN Hosting Active", text_color="#1DB954")
        self.lan_start_host_btn.pack_forget()
        self.lan_host_info_label.configure(text=f"Give this to the other laptop:\nIP: {ip}   Port: {port}")
        self.lan_host_info_label.pack(pady=(10, 0))
        self.lan_code_label.configure(text=code)
        self.lan_code_label.pack(pady=5)
        self.lan_stop_host_btn.pack(pady=10)

    def stop_lan_hosting(self):
        self.lan_host_status.configure(text="Stopping...", text_color="#f39c12")
        self.update()

        if self.lan_announce_stop:
            self.lan_announce_stop.set()
            self.lan_announce_stop = None

        startupinfo = None
        if os.name == 'nt':
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW

        if shutil.which("docker") is not None:
            try:
                subprocess.run(["docker", "rm", "-f", "gpu_share_node"], capture_output=True, startupinfo=startupinfo)
            except Exception:
                pass

        if hasattr(self, 'agent_proc') and self.agent_proc:
            try:
                self.agent_proc.terminate()
            except Exception:
                pass
            self.agent_proc = None

        self.is_lan_hosting = False
        self.reset_lan_host_ui()

    def reset_lan_host_ui(self):
        self.lan_host_info_label.pack_forget()
        self.lan_code_label.pack_forget()
        self.lan_stop_host_btn.pack_forget()
        self.lan_start_host_btn.pack(pady=8)
        self.lan_start_host_btn.configure(state="normal")
        self.lan_gpu_switch.configure(state="normal")
        self.lan_name_entry_host.configure(state="normal")
        self.lan_host_status.configure(text="Ready to share resources.", text_color="gray60")

    # --- LAN joining (client side) ---
    def scan_lan_hosts(self):
        self.lan_scan_btn.configure(state="disabled", text="Scanning...")
        for w in self.lan_scan_results.winfo_children():
            w.destroy()
        threading.Thread(target=self._scan_lan_hosts_worker, daemon=True).start()

    def _scan_lan_hosts_worker(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.5)
        found = {}
        try:
            sock.bind(('', 47600))
            deadline = time.time() + 2.5
            while time.time() < deadline:
                try:
                    data, addr = sock.recvfrom(2048)
                    info = json.loads(data.decode('utf-8'))
                    if info.get('type') == 'COMPUTE_NEXUS_LAN_HOST':
                        found[info.get('ip', addr[0])] = info
                except socket.timeout:
                    continue
                except Exception:
                    continue
        except Exception:
            pass
        finally:
            sock.close()

        self.after(0, self._populate_lan_scan_results, list(found.values()))

    def _populate_lan_scan_results(self, hosts):
        self.lan_scan_btn.configure(state="normal", text="🔍 Scan for Nearby Hosts")
        for w in self.lan_scan_results.winfo_children():
            w.destroy()

        if not hosts:
            lbl = ctk.CTkLabel(self.lan_scan_results, text="No hosts found. Enter IP manually below.", text_color="gray50", font=ctk.CTkFont(size=11, slant="italic"))
            lbl.pack(pady=2)
            return

        for h in hosts:
            btn = ctk.CTkButton(self.lan_scan_results, text=f"{h.get('name', '?')}  @  {h['ip']}", height=32, fg_color="#262626", hover_color="#333333",
                                 command=lambda ip=h['ip'], port=h.get('port', 0): self.select_lan_host(ip, port))
            btn.pack(pady=2, fill="x")

    def select_lan_host(self, ip, port):
        self.lan_ip_entry.delete(0, 'end')
        self.lan_ip_entry.insert(0, f"{ip}:{port}")

    def connect_lan_client(self):
        target = self.lan_ip_entry.get().strip()
        code = self.lan_code_entry.get().strip()
        name = self.lan_name_entry_join.get().strip() or socket.gethostname()

        if not target or ':' not in target:
            self.lan_join_status.configure(text="❌ Enter a valid Host IP:Port (scan or type it).", text_color="#E22134")
            return
        if len(code) != 6 or not code.isdigit():
            self.lan_join_status.configure(text="❌ Enter the 6-digit code from the host.", text_color="#E22134")
            return

        self.lan_connect_btn.configure(state="disabled")
        self.lan_join_status.configure(text="🔍 Connecting...", text_color="#f39c12")

        threading.Thread(target=self._connect_lan_client_worker, args=(target, code, name), daemon=True).start()

    def _connect_lan_client_worker(self, target, code, name):
        try:
            ip, port = target.split(':', 1)
            url = f"http://{ip}:{port}"
            res = requests.post(f"{url}/pair", json={"code": code, "client_name": name}, timeout=5)
            if res.status_code == 403:
                self.after(0, lambda: self.lan_join_status.configure(text="❌ Incorrect code.", text_color="#E22134"))
                self.after(0, lambda: self.lan_connect_btn.configure(state="normal"))
                return
            if res.status_code != 200:
                self.after(0, lambda: self.lan_join_status.configure(text=f"❌ Host returned status {res.status_code}.", text_color="#E22134"))
                self.after(0, lambda: self.lan_connect_btn.configure(state="normal"))
                return

            data = res.json()
            api_key = data.get("api_key")
            if not api_key:
                self.after(0, lambda: self.lan_join_status.configure(text="❌ Invalid response from host.", text_color="#E22134"))
                self.after(0, lambda: self.lan_connect_btn.configure(state="normal"))
                return

            config_path = os.path.expanduser("~/.gpu_share_config.json")
            with open(config_path, "w") as f:
                json.dump({"url": url, "api_key": api_key, "client_name": name}, f)

            self.my_username = name
            self.my_email = ""
            self.after(0, self.enter_workspace_via_lan)
        except requests.exceptions.RequestException as e:
            self.after(0, lambda err=e: self.lan_join_status.configure(text=f"❌ Connection error: {err}", text_color="#E22134"))
            self.after(0, lambda: self.lan_connect_btn.configure(state="normal"))
        except Exception as e:
            self.after(0, lambda err=e: self.lan_join_status.configure(text=f"❌ Unexpected error: {err}", text_color="#E22134"))
            self.after(0, lambda: self.lan_connect_btn.configure(state="normal"))

    def enter_workspace_via_lan(self):
        self.lan_frame.pack_forget()
        self.build_main_workspace()
        self.show_join_frame()
        self.join_status.configure(text="✅ Connected via LAN! GPU workload panel ready below.", text_color="#1DB954")
        self._demo_section_revealed = True
        self.reveal_demo_section()

    def on_closing(self):
        # Wrap everything in a try...finally block. 
        # No matter WHAT crashes inside stop_hosting, self.destroy() WILL run.
        try:
            self.stop_hosting()
        except Exception as e:
            print(f"Error during shutdown sequence: {e}")
        finally:
            print("Closing application window safely.")
            self.destroy()

if __name__ == "__main__":
    app = ComputeNexusApp()
    app.mainloop()