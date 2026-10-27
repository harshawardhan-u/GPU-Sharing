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

        self.is_lan_hosting = False
        self.lan_announce_stop = None
        self.lan_code = None
        self.lan_port = None

        self.rtcompute_announce_stop = None
        self.rtcompute_server_obj = None
        self.rtcompute_code = None

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
        self.lan_link_btn.pack(pady=(0, 5))

        self.rtcompute_link_btn = ctk.CTkButton(self.auth_frame, text="🎮 Real-Time Compute Setup (for game.py)", fg_color="transparent", text_color="#66c2ff", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.show_rtcompute_screen)
        self.rtcompute_link_btn.pack(pady=(0, 5))

        self.pool_link_btn = ctk.CTkButton(self.auth_frame, text="🧩 GPU Pool (multi-GPU demo)", fg_color="transparent", text_color="#eb6834", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.show_pool_screen)
        self.pool_link_btn.pack(pady=(0, 5))

        self.arena_link_btn = ctk.CTkButton(self.auth_frame, text="📱 Live Arena (phones join by QR)", fg_color="transparent", text_color="#e87ba4", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.show_arena_screen)
        self.arena_link_btn.pack(pady=(0, 5))

        self.captions_link_btn = ctk.CTkButton(self.auth_frame, text="💬 Live Captions (speech → text on GPU)", fg_color="transparent", text_color="#1baf7a", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.show_captions_screen)
        self.captions_link_btn.pack(pady=(0, 5))

        self.em_link_btn = ctk.CTkButton(self.auth_frame, text="🌊 EM Wave Lab (Maxwell's equations on GPU)", fg_color="transparent", text_color="#00b8d9", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.show_em_screen)
        self.em_link_btn.pack(pady=(0, 5))

        self.swarm_link_btn = ctk.CTkButton(self.auth_frame, text="🌐 Room Supercomputer (everyone's phones render together)", fg_color="transparent", text_color="#b48cff", hover=False, font=ctk.CTkFont(size=12, weight="bold", underline=True), command=self.show_swarm_screen)
        self.swarm_link_btn.pack(pady=(0, 5))

        self.fed_link_btn = ctk.CTkButton(self.auth_frame, text="🧠 Train an AI Together (federated learning on phones)", fg_color="transparent", text_color="#6aa2ff", hover=False, font=ctk.CTkFont(size=12, weight="bold", underline=True), command=self.show_fed_screen)
        self.fed_link_btn.pack(pady=(0, 5))

        self.crack_link_btn = ctk.CTkButton(self.auth_frame, text="🔐 Password Strength Lab (phones pool GPUs to crack a demo password)", fg_color="transparent", text_color="#F2C744", hover=False, font=ctk.CTkFont(size=12, weight="bold", underline=True), command=self.show_crack_screen)
        self.crack_link_btn.pack(pady=(0, 20))

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
        self.join_status.configure(text="✅ Connected via LAN! Run 'cloud-run <script.py>' from a terminal now.", text_color="#1DB954")

    # =========================================================================
    # REAL-TIME COMPUTE SETUP -- a separate screen for pairing with the
    # real_time_compute.py server (game.py). This is a different protocol
    # from the Docker/cloud-run system above (its own LAN discovery port/
    # magic string, see real_time_compute.py), so it gets its own screen
    # rather than being folded into "Connect Locally". The GUI only handles
    # discovery + pairing here; running game.py itself still happens in a
    # terminal, same as cloud-run.
    # =========================================================================
    def show_rtcompute_screen(self):
        self.auth_frame.pack_forget()
        if not hasattr(self, 'rtcompute_frame'):
            self.build_rtcompute_screen()
        self.rtcompute_frame.pack(expand=True, fill="both")

    def back_to_signin_from_rtcompute(self):
        self.rtcompute_frame.pack_forget()
        self.auth_frame.pack(expand=True, fill="both")

    def build_rtcompute_screen(self):
        self.rtcompute_frame = ctk.CTkFrame(self, fg_color="#121212")

        back_btn = ctk.CTkButton(self.rtcompute_frame, text="← Back to Sign In", fg_color="transparent", text_color="gray50", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.back_to_signin_from_rtcompute)
        back_btn.pack(pady=(20, 0), anchor="w", padx=20)

        logo = ctk.CTkLabel(self.rtcompute_frame, text="REAL-TIME COMPUTE SETUP", font=ctk.CTkFont(size=26, weight="bold"), text_color="#66c2ff")
        logo.pack(pady=(10, 5))
        sub = ctk.CTkLabel(self.rtcompute_frame, text="Pairs with real_time_compute.py for game.py. You still run 'python game.py'\nyourself afterward -- this just sets up the connection it will use.", text_color="gray50", font=ctk.CTkFont(size=12), justify="center")
        sub.pack(pady=(0, 20))

        tabs = ctk.CTkFrame(self.rtcompute_frame, fg_color="transparent")
        tabs.pack(pady=(0, 15))
        self.rtcompute_host_tab_btn = ctk.CTkButton(tabs, text="Host (GPU laptop)", width=180, command=self.show_rtcompute_host_pane)
        self.rtcompute_host_tab_btn.grid(row=0, column=0, padx=5)
        self.rtcompute_join_tab_btn = ctk.CTkButton(tabs, text="Join (non-GPU laptop)", width=180, command=self.show_rtcompute_join_pane, fg_color="transparent", border_width=2, text_color=("#121212", "#DCE4EE"))
        self.rtcompute_join_tab_btn.grid(row=0, column=1, padx=5)

        self.rtcompute_pane_container = ctk.CTkFrame(self.rtcompute_frame, fg_color="transparent")
        self.rtcompute_pane_container.pack(expand=True, fill="both", padx=40)

        # --- Host pane ---
        self.rtcompute_host_pane = ctk.CTkFrame(self.rtcompute_pane_container, fg_color="transparent")

        self.rtcompute_host_status = ctk.CTkLabel(self.rtcompute_host_pane, text="Ready to serve real-time GPU compute.", text_color="gray60", font=ctk.CTkFont(size=13))
        self.rtcompute_host_status.pack(pady=(10, 5))

        self.rtcompute_start_btn = ctk.CTkButton(self.rtcompute_host_pane, text="⚡ Start Real-Time Compute Server", height=45, width=320, command=self.start_rtcompute_server)
        self.rtcompute_start_btn.pack(pady=8)

        self.rtcompute_code_label = ctk.CTkLabel(self.rtcompute_host_pane, text="", font=ctk.CTkFont(size=34, weight="bold"), text_color="#1DB954")
        self.rtcompute_info_label = ctk.CTkLabel(self.rtcompute_host_pane, text="", font=ctk.CTkFont(size=13, weight="bold"), text_color="#66c2ff", justify="center")
        self.rtcompute_stop_btn = ctk.CTkButton(self.rtcompute_host_pane, text="Stop Server", height=40, width=320, fg_color="#E22134", hover_color="#B01625", command=self.stop_rtcompute_server)

        # --- Join pane ---
        self.rtcompute_join_pane = ctk.CTkFrame(self.rtcompute_pane_container, fg_color="transparent")

        self.rtcompute_scan_btn = ctk.CTkButton(self.rtcompute_join_pane, text="🔍 Scan for Nearby Hosts", height=38, width=320, command=self.scan_rtcompute_hosts)
        self.rtcompute_scan_btn.pack(pady=(15, 8))

        self.rtcompute_scan_results = ctk.CTkFrame(self.rtcompute_join_pane, fg_color="transparent", width=320)
        self.rtcompute_scan_results.pack(pady=2, fill="x")

        self.rtcompute_ip_entry = ctk.CTkEntry(self.rtcompute_join_pane, width=320, height=40, placeholder_text="Host IP:Port (auto-filled, or type it)")
        self.rtcompute_ip_entry.pack(pady=8)

        self.rtcompute_code_entry = ctk.CTkEntry(self.rtcompute_join_pane, width=320, height=40, placeholder_text="6-Digit Code from Host", font=ctk.CTkFont(size=16, weight="bold"), justify="center")
        self.rtcompute_code_entry.pack(pady=8)

        self.rtcompute_join_status = ctk.CTkLabel(self.rtcompute_join_pane, text="", text_color="gray60", font=ctk.CTkFont(size=13), wraplength=320, justify="center")
        self.rtcompute_join_status.pack(pady=5)

        self.rtcompute_save_btn = ctk.CTkButton(self.rtcompute_join_pane, text="✅ Verify & Save Connection", height=45, width=320, command=self.verify_and_save_rtcompute)
        self.rtcompute_save_btn.pack(pady=8)

        self.show_rtcompute_host_pane()

    def show_rtcompute_host_pane(self):
        self.rtcompute_join_pane.pack_forget()
        self.rtcompute_host_pane.pack(expand=True, fill="both")
        self.rtcompute_host_tab_btn.configure(fg_color=["#3b8ed0", "#1f538d"])
        self.rtcompute_join_tab_btn.configure(fg_color="transparent")

    def show_rtcompute_join_pane(self):
        self.rtcompute_host_pane.pack_forget()
        self.rtcompute_join_pane.pack(expand=True, fill="both")
        self.rtcompute_join_tab_btn.configure(fg_color=["#3b8ed0", "#1f538d"])
        self.rtcompute_host_tab_btn.configure(fg_color="transparent")

    # --- Host side (GPU laptop) ---
    def start_rtcompute_server(self):
        self.rtcompute_start_btn.configure(state="disabled")
        self.rtcompute_host_status.configure(text="Checking for an NVIDIA GPU...", text_color="#f39c12")
        threading.Thread(target=self._rtcompute_server_worker, daemon=True).start()

    def _rtcompute_server_worker(self):
        import real_time_compute as rtc

        has_gpu, gpu_name = rtc.check_nvidia_gpu()
        if not has_gpu:
            self.after(0, lambda: self.rtcompute_host_status.configure(text="❌ No NVIDIA GPU detected on this machine.", text_color="#E22134"))
            self.after(0, lambda: self.rtcompute_start_btn.configure(state="normal"))
            return

        try:
            import torch
            if not torch.cuda.is_available():
                self.after(0, lambda: self.rtcompute_host_status.configure(text="❌ PyTorch can't see CUDA on this machine.", text_color="#E22134"))
                self.after(0, lambda: self.rtcompute_start_btn.configure(state="normal"))
                return
        except ImportError:
            self.after(0, lambda: self.rtcompute_host_status.configure(text="❌ PyTorch isn't installed (needed to serve compute).", text_color="#E22134"))
            self.after(0, lambda: self.rtcompute_start_btn.configure(state="normal"))
            return

        session_code = ''.join(secrets.choice('0123456789') for _ in range(6))
        self.rtcompute_code = session_code
        self.rtcompute_announce_stop = threading.Event()
        host_name = socket.gethostname()
        threading.Thread(target=rtc.announce_loop, args=(self.rtcompute_announce_stop, host_name, rtc.DEFAULT_PORT), daemon=True).start()

        self.after(0, self.show_rtcompute_host_success, session_code, gpu_name, rtc.DEFAULT_PORT)

        handler = rtc.make_ws_handler(session_code)
        from websockets.sync.server import serve
        try:
            with serve(handler, '0.0.0.0', rtc.DEFAULT_PORT, max_size=None) as server:
                self.rtcompute_server_obj = server
                server.serve_forever()
        except Exception as e:
            self.after(0, lambda err=e: self.rtcompute_host_status.configure(text=f"❌ Server error: {err}", text_color="#E22134"))
        finally:
            self.rtcompute_server_obj = None

    def show_rtcompute_host_success(self, code, gpu_name, port):
        ip = self.get_lan_ip()
        self.rtcompute_host_status.configure(text=f"🟢 Live on GPU: {gpu_name}", text_color="#1DB954")
        self.rtcompute_start_btn.pack_forget()
        self.rtcompute_code_label.configure(text=code)
        self.rtcompute_code_label.pack(pady=5)
        self.rtcompute_info_label.configure(text=f"IP: {ip}   Port: {port}\nOn the non-GPU laptop: Real-Time Compute Setup -> Join")
        self.rtcompute_info_label.pack(pady=(0, 10))
        self.rtcompute_stop_btn.pack(pady=10)

    def stop_rtcompute_server(self):
        if self.rtcompute_announce_stop:
            self.rtcompute_announce_stop.set()
            self.rtcompute_announce_stop = None
        if self.rtcompute_server_obj:
            try:
                self.rtcompute_server_obj.shutdown()
            except Exception:
                pass
            self.rtcompute_server_obj = None
        self.reset_rtcompute_host_ui()

    def reset_rtcompute_host_ui(self):
        self.rtcompute_code_label.pack_forget()
        self.rtcompute_info_label.pack_forget()
        self.rtcompute_stop_btn.pack_forget()
        self.rtcompute_start_btn.pack(pady=8)
        self.rtcompute_start_btn.configure(state="normal")
        self.rtcompute_host_status.configure(text="Ready to serve real-time GPU compute.", text_color="gray60")

    # --- Join side (non-GPU laptop) ---
    def scan_rtcompute_hosts(self):
        self.rtcompute_scan_btn.configure(state="disabled", text="Scanning...")
        for w in self.rtcompute_scan_results.winfo_children():
            w.destroy()
        threading.Thread(target=self._scan_rtcompute_worker, daemon=True).start()

    def _scan_rtcompute_worker(self):
        import real_time_compute as rtc
        hosts = rtc.discover_hosts(timeout=3.0)
        self.after(0, self._populate_rtcompute_results, hosts)

    def _populate_rtcompute_results(self, hosts):
        self.rtcompute_scan_btn.configure(state="normal", text="🔍 Scan for Nearby Hosts")
        for w in self.rtcompute_scan_results.winfo_children():
            w.destroy()

        if not hosts:
            lbl = ctk.CTkLabel(self.rtcompute_scan_results, text="No hosts found. Enter IP manually below.", text_color="gray50", font=ctk.CTkFont(size=11, slant="italic"))
            lbl.pack(pady=2)
            return

        for h in hosts:
            btn = ctk.CTkButton(self.rtcompute_scan_results, text=f"{h.get('name', '?')}  @  {h['ip']}", height=32, fg_color="#262626", hover_color="#333333",
                                 command=lambda ip=h['ip'], port=h.get('port', 47612): self.select_rtcompute_host(ip, port))
            btn.pack(pady=2, fill="x")

    def select_rtcompute_host(self, ip, port):
        self.rtcompute_ip_entry.delete(0, 'end')
        self.rtcompute_ip_entry.insert(0, f"{ip}:{port}")

    def verify_and_save_rtcompute(self):
        target = self.rtcompute_ip_entry.get().strip()
        code = self.rtcompute_code_entry.get().strip()

        if not target or ':' not in target:
            self.rtcompute_join_status.configure(text="❌ Enter a valid Host IP:Port (scan or type it).", text_color="#E22134")
            return
        if len(code) != 6 or not code.isdigit():
            self.rtcompute_join_status.configure(text="❌ Enter the 6-digit code from the host.", text_color="#E22134")
            return

        self.rtcompute_save_btn.configure(state="disabled")
        self.rtcompute_join_status.configure(text="🔍 Verifying...", text_color="#f39c12")
        threading.Thread(target=self._verify_and_save_rtcompute_worker, args=(target, code), daemon=True).start()

    def _verify_and_save_rtcompute_worker(self, target, code):
        try:
            ip, port = target.split(':', 1)
            from websockets.sync.client import connect
            url = f"ws://{ip}:{port}"
            with connect(url, open_timeout=5) as ws:
                ws.send(json.dumps({'code': code}))
                ack = json.loads(ws.recv(timeout=5))
                if ack.get('status') != 'approved':
                    self.after(0, lambda: self.rtcompute_join_status.configure(text="❌ Incorrect code.", text_color="#E22134"))
                    self.after(0, lambda: self.rtcompute_save_btn.configure(state="normal"))
                    return

            config_path = os.path.expanduser("~/.rtcompute_config.json")
            with open(config_path, "w") as f:
                json.dump({"host": ip, "port": int(port), "code": code}, f)

            self.after(0, lambda: self.rtcompute_join_status.configure(text="✅ Saved! Now run 'python game.py' in a terminal.", text_color="#1DB954"))
            self.after(0, lambda: self.rtcompute_save_btn.configure(state="normal"))
        except Exception as e:
            self.after(0, lambda err=e: self.rtcompute_join_status.configure(text=f"❌ {err}", text_color="#E22134"))
            self.after(0, lambda: self.rtcompute_save_btn.configure(state="normal"))

    # =========================================================================
    # GPU POOL -- runs pool_compute.py (share this laptop's GPUs) and
    # pool_swarm.py (the pooled game) as child processes, so the whole
    # multi-GPU demo can be driven from here without a terminal.
    # =========================================================================
    POOL_DIR = os.path.dirname(os.path.abspath(__file__))

    def show_pool_screen(self):
        self.auth_frame.pack_forget()
        if not hasattr(self, 'pool_frame'):
            self.build_pool_screen()
        self.pool_frame.pack(expand=True, fill="both")

    def back_to_signin_from_pool(self):
        self.pool_frame.pack_forget()
        self.auth_frame.pack(expand=True, fill="both")

    def build_pool_screen(self):
        self.pool_proc = None
        self.game_proc = None
        self.pool_frame = ctk.CTkFrame(self, fg_color="#121212")

        back_btn = ctk.CTkButton(self.pool_frame, text="← Back to Sign In", fg_color="transparent", text_color="gray50", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.back_to_signin_from_pool)
        back_btn.pack(pady=(20, 0), anchor="w", padx=20)

        ctk.CTkLabel(self.pool_frame, text="GPU POOL", font=ctk.CTkFont(size=26, weight="bold"), text_color="#eb6834").pack(pady=(10, 5))
        ctk.CTkLabel(self.pool_frame, text="Every GPU on every laptop (NVIDIA, Intel, AMD) works on one game together.\nWork is shared by speed, moves away from lost or cheating GPUs, and comes back when they return.", text_color="gray50", font=ctk.CTkFont(size=12), justify="center").pack(pady=(0, 15))

        tabs = ctk.CTkFrame(self.pool_frame, fg_color="transparent")
        tabs.pack(pady=(0, 10))
        self.pool_host_tab_btn = ctk.CTkButton(tabs, text="Share My GPUs", width=180, command=self.show_pool_host_pane)
        self.pool_host_tab_btn.grid(row=0, column=0, padx=5)
        self.pool_play_tab_btn = ctk.CTkButton(tabs, text="Play Pooled Game", width=180, command=self.show_pool_play_pane, fg_color="transparent", border_width=2, text_color=("#121212", "#DCE4EE"))
        self.pool_play_tab_btn.grid(row=0, column=1, padx=5)

        container = ctk.CTkFrame(self.pool_frame, fg_color="transparent")
        container.pack(expand=True, fill="both", padx=40)

        # --- Share pane ---
        self.pool_host_pane = ctk.CTkFrame(container, fg_color="transparent")
        self.pool_host_status = ctk.CTkLabel(self.pool_host_pane, text="Share this laptop's GPUs with the pool.", text_color="gray60", font=ctk.CTkFont(size=13))
        self.pool_host_status.pack(pady=(5, 5))
        self.pool_code_input = ctk.CTkEntry(self.pool_host_pane, width=320, height=38, placeholder_text="Pairing code (leave empty for a random one)", justify="center")
        self.pool_code_input.pack(pady=5)
        self.pool_start_btn = ctk.CTkButton(self.pool_host_pane, text="⚡ Start Sharing", height=42, width=320, command=self.start_pool_sharing)
        self.pool_start_btn.pack(pady=5)
        self.pool_code_label = ctk.CTkLabel(self.pool_host_pane, text="", font=ctk.CTkFont(size=32, weight="bold"), text_color="#1DB954")
        self.pool_gpu_rows = ctk.CTkFrame(self.pool_host_pane, fg_color="#1a1a1a", corner_radius=8)
        self.pool_stop_btn = ctk.CTkButton(self.pool_host_pane, text="Stop Sharing", height=38, width=320, fg_color="#E22134", hover_color="#B01625", command=self.stop_pool_sharing)
        self.pool_log = ctk.CTkTextbox(self.pool_host_pane, height=110, width=520, font=ctk.CTkFont(family="Consolas", size=11))

        # --- Play pane ---
        self.pool_play_pane = ctk.CTkFrame(container, fg_color="transparent")
        ctk.CTkLabel(self.pool_play_pane, text="Pooled Gravity Swarm", font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(10, 5))
        self.pool_play_code = ctk.CTkEntry(self.pool_play_pane, width=320, height=40, placeholder_text="Code shown on the sharing laptop(s)", font=ctk.CTkFont(size=16, weight="bold"), justify="center")
        self.pool_play_code.pack(pady=8)
        ctk.CTkLabel(self.pool_play_pane, text="Particles", text_color="gray60").pack(pady=(6, 0))
        self.pool_n_choice = ctk.CTkSegmentedButton(self.pool_play_pane, values=["10000", "20000", "30000", "50000"])
        self.pool_n_choice.set("30000")
        self.pool_n_choice.pack(pady=5)
        self.pool_local_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(self.pool_play_pane, text="Also use this laptop's own GPU directly\n(leave off if this laptop is already sharing)", variable=self.pool_local_var).pack(pady=8)
        self.pool_play_status = ctk.CTkLabel(self.pool_play_pane, text="", text_color="gray60", font=ctk.CTkFont(size=13), wraplength=420, justify="center")
        self.pool_play_status.pack(pady=5)
        self.pool_launch_btn = ctk.CTkButton(self.pool_play_pane, text="🎮 Launch Game", height=45, width=320, command=self.launch_pool_game)
        self.pool_launch_btn.pack(pady=8)
        ctk.CTkLabel(self.pool_play_pane, text="In the game: hold mouse = attract,  E / Q = more / fewer particles,  Esc = quit", text_color="gray50", font=ctk.CTkFont(size=11)).pack(pady=(4, 0))

        self.show_pool_host_pane()

    def show_pool_host_pane(self):
        self.pool_play_pane.pack_forget()
        self.pool_host_pane.pack(expand=True, fill="both")
        self.pool_host_tab_btn.configure(fg_color=["#3b8ed0", "#1f538d"])
        self.pool_play_tab_btn.configure(fg_color="transparent")

    def show_pool_play_pane(self):
        self.pool_host_pane.pack_forget()
        self.pool_play_pane.pack(expand=True, fill="both")
        self.pool_play_tab_btn.configure(fg_color=["#3b8ed0", "#1f538d"])
        self.pool_host_tab_btn.configure(fg_color="transparent")

    def _pool_python(self):
        return sys.executable if not getattr(sys, 'frozen', False) else "python"

    def _pool_creationflags(self):
        return subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

    # --- Share side ---
    def start_pool_sharing(self):
        try:
            from pool_kernels import list_gpus, short_name
            gpus = list_gpus()
        except Exception:
            gpus = []
        if not gpus:
            self.pool_host_status.configure(text="❌ No GPU found through OpenCL. Run: pip install pyopencl", text_color="#E22134")
            return
        code = self.pool_code_input.get().strip()
        if code and (len(code) != 6 or not code.isdigit()):
            self.pool_host_status.configure(text="❌ The code must be 6 digits (or leave it empty).", text_color="#E22134")
            return
        code = code or "".join(secrets.choice("0123456789") for _ in range(6))
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONWARNINGS"] = "ignore"
        self.pool_proc = subprocess.Popen(
            [self._pool_python(), "-u", "pool_compute.py", "--serve", "--code", code],
            cwd=self.POOL_DIR, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env, creationflags=self._pool_creationflags())
        threading.Thread(target=self._pool_read_log, args=(self.pool_proc,), daemon=True).start()

        self.pool_start_btn.pack_forget()
        self.pool_code_input.pack_forget()
        self.pool_host_status.configure(text=f"🟢 Sharing {len(gpus)} GPU(s) on {self.get_lan_ip()}  —  give this code to players:", text_color="#1DB954")
        self.pool_code_label.configure(text=code)
        self.pool_code_label.pack(pady=2)
        for w in self.pool_gpu_rows.winfo_children():
            w.destroy()
        for i, dev in enumerate(gpus):
            row = ctk.CTkFrame(self.pool_gpu_rows, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=4)
            ctk.CTkLabel(row, text=f"[{i}]  {short_name(dev)}", font=ctk.CTkFont(size=13, weight="bold"), width=300, anchor="w").pack(side="left")
            ctk.CTkSwitch(row, text="Cheat mode", progress_color="#E22134", command=lambda idx=i: self._pool_toggle_cheat(idx)).pack(side="right")
        self.pool_gpu_rows.pack(pady=8, fill="x")
        self.pool_stop_btn.pack(pady=5)
        self.pool_log.delete("1.0", "end")
        self.pool_log.pack(pady=5)

    def _pool_read_log(self, proc):
        for line in proc.stdout:
            line = line.rstrip()
            if not line or "lambda" in line or "Warning" in line or line.startswith("=") or "Commands:" in line:
                continue
            self.after(0, lambda l=line: self._pool_append_log(l))

    def _pool_append_log(self, line):
        if hasattr(self, 'pool_log'):
            self.pool_log.insert("end", time.strftime("%H:%M:%S  ") + line.strip() + "\n")
            self.pool_log.see("end")

    def _pool_toggle_cheat(self, idx):
        if self.pool_proc and self.pool_proc.poll() is None:
            try:
                self.pool_proc.stdin.write(f"cheat {idx}\n")
                self.pool_proc.stdin.flush()
            except Exception:
                pass

    def _pool_kill(self, proc):
        if proc and proc.poll() is None:
            if os.name == 'nt':
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, creationflags=self._pool_creationflags())
            else:
                proc.terminate()

    def stop_pool_sharing(self):
        self._pool_kill(self.pool_proc)
        self.pool_proc = None
        for w in (self.pool_code_label, self.pool_gpu_rows, self.pool_stop_btn, self.pool_log):
            w.pack_forget()
        self.pool_code_input.pack(pady=5)
        self.pool_start_btn.pack(pady=5)
        self.pool_host_status.configure(text="Share this laptop's GPUs with the pool.", text_color="gray60")

    # --- Play side ---
    def launch_pool_game(self):
        code = self.pool_play_code.get().strip()
        if len(code) != 6 or not code.isdigit():
            self.pool_play_status.configure(text="❌ Enter the 6-digit code shown on the sharing laptop.", text_color="#E22134")
            return
        if self.game_proc and self.game_proc.poll() is None:
            self.pool_play_status.configure(text="The game is already running.", text_color="#f39c12")
            return
        args = [self._pool_python(), "-u", "pool_swarm.py", "--code", code, "--n", self.pool_n_choice.get(),
                "--local", "auto" if self.pool_local_var.get() else "off"]
        env = os.environ.copy()
        env["PYTHONWARNINGS"] = "ignore"
        self.game_proc = subprocess.Popen(args, cwd=self.POOL_DIR, env=env, creationflags=self._pool_creationflags())
        self.pool_play_status.configure(text="✅ Game launched — the window opens in a few seconds.", text_color="#1DB954")

    # =========================================================================
    # LIVE ARENA -- runs nexus_hub.py: phones/laptops join by QR code and
    # play on this laptop's GPU; Mission Control slices the GPU per player.
    # =========================================================================
    ARENA_PORT = 8765

    def show_arena_screen(self):
        self.auth_frame.pack_forget()
        if not hasattr(self, 'arena_frame'):
            self.build_arena_screen()
        self.arena_frame.pack(expand=True, fill="both")

    def back_to_signin_from_arena(self):
        self.arena_frame.pack_forget()
        self.auth_frame.pack(expand=True, fill="both")

    def build_arena_screen(self):
        self.arena_proc = None
        self.arena_frame = ctk.CTkFrame(self, fg_color="#121212")
        ctk.CTkButton(self.arena_frame, text="← Back to Sign In", fg_color="transparent", text_color="gray50", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.back_to_signin_from_arena).pack(pady=(20, 0), anchor="w", padx=20)
        ctk.CTkLabel(self.arena_frame, text="LIVE ARENA", font=ctk.CTkFont(size=26, weight="bold"), text_color="#e87ba4").pack(pady=(10, 5))
        ctk.CTkLabel(self.arena_frame, text="Anyone on this Wi-Fi scans the QR code and plays in their browser.\nTheir phone only draws; all the physics runs on this laptop's GPU.", text_color="gray50", font=ctk.CTkFont(size=12), justify="center").pack(pady=(0, 12))
        self.arena_status = ctk.CTkLabel(self.arena_frame, text="Ready to start.", text_color="gray60", font=ctk.CTkFont(size=13))
        self.arena_status.pack(pady=4)
        self.arena_start_btn = ctk.CTkButton(self.arena_frame, text="⚡ Start Arena", height=44, width=320, command=self.start_arena)
        self.arena_start_btn.pack(pady=6)
        self.arena_qr_label = ctk.CTkLabel(self.arena_frame, text="")
        self.arena_url_label = ctk.CTkLabel(self.arena_frame, text="", font=ctk.CTkFont(size=18, weight="bold"), text_color="#1DB954")
        self.arena_btn_row = ctk.CTkFrame(self.arena_frame, fg_color="transparent")
        ctk.CTkButton(self.arena_btn_row, text="🛰 Open Mission Control", width=210, height=38, command=lambda: webbrowser.open(self.arena_url + "/control")).grid(row=0, column=0, padx=5)
        ctk.CTkButton(self.arena_btn_row, text="🎮 Play on this laptop", width=210, height=38, fg_color="transparent", border_width=2, command=lambda: webbrowser.open(self.arena_url)).grid(row=0, column=1, padx=5)
        self.arena_stop_btn = ctk.CTkButton(self.arena_frame, text="Stop Arena", height=36, width=320, fg_color="#E22134", hover_color="#B01625", command=self.stop_arena)
        self.arena_log = ctk.CTkTextbox(self.arena_frame, height=100, width=560, font=ctk.CTkFont(family="Consolas", size=11))

    def start_arena(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONWARNINGS"] = "ignore"
        self.arena_proc = subprocess.Popen(
            [self._pool_python(), "-u", "nexus_hub.py", "--port", str(self.ARENA_PORT)],
            cwd=self.POOL_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", env=env, creationflags=self._pool_creationflags())
        threading.Thread(target=self._arena_read_log, args=(self.arena_proc,), daemon=True).start()
        self.arena_url = f"http://{self.get_lan_ip()}:{self.ARENA_PORT}"
        self.arena_start_btn.pack_forget()
        self.arena_status.configure(text="🟢 Arena is live — scan to join (same Wi-Fi):", text_color="#1DB954")
        try:
            import qrcode
            qr = qrcode.make(self.arena_url, box_size=8, border=2)
            img = (qr.get_image() if hasattr(qr, "get_image") else qr._img).convert("RGB")
            self.arena_qr_img = ctk.CTkImage(light_image=img, dark_image=img, size=(220, 220))
            self.arena_qr_label.configure(image=self.arena_qr_img, text="")
        except Exception:
            self.arena_qr_label.configure(text="(install 'qrcode' to show a QR code here: pip install qrcode)", text_color="gray50")
        self.arena_qr_label.pack(pady=4)
        self.arena_url_label.configure(text=self.arena_url)
        self.arena_url_label.pack(pady=2)
        self.arena_btn_row.pack(pady=8)
        self.arena_stop_btn.pack(pady=4)
        self.arena_log.delete("1.0", "end")
        self.arena_log.pack(pady=6)

    def _arena_read_log(self, proc):
        for line in proc.stdout:
            line = line.rstrip()
            if not line or "lambda" in line or "Warning" in line or line.startswith("="):
                continue
            self.after(0, lambda l=line: (self.arena_log.insert("end", time.strftime("%H:%M:%S  ") + l.strip() + "\n"), self.arena_log.see("end")))

    def stop_arena(self):
        self._pool_kill(self.arena_proc)
        self.arena_proc = None
        for w in (self.arena_qr_label, self.arena_url_label, self.arena_btn_row, self.arena_stop_btn, self.arena_log):
            w.pack_forget()
        self.arena_start_btn.pack(pady=6)
        self.arena_status.configure(text="Ready to start.", text_color="gray60")

    # =========================================================================
    # LIVE CAPTIONS -- runs nexus_captions.py: a speaker's laptop streams its
    # microphone here, the GPU turns it into text, listeners read it by QR.
    # =========================================================================
    CAPTIONS_PORT = 8770

    def show_captions_screen(self):
        self.auth_frame.pack_forget()
        if not hasattr(self, 'captions_frame'):
            self.build_captions_screen()
        self.captions_frame.pack(expand=True, fill="both")

    def back_to_signin_from_captions(self):
        self.captions_frame.pack_forget()
        self.auth_frame.pack(expand=True, fill="both")

    def build_captions_screen(self):
        self.captions_proc = None
        self.speaker_proc = None
        f = self.captions_frame = ctk.CTkFrame(self, fg_color="#121212")
        ctk.CTkButton(f, text="← Back to Sign In", fg_color="transparent", text_color="gray50", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.back_to_signin_from_captions).pack(pady=(20, 0), anchor="w", padx=20)
        ctk.CTkLabel(f, text="LIVE CAPTIONS", font=ctk.CTkFont(size=26, weight="bold"), text_color="#1baf7a").pack(pady=(10, 5))
        ctk.CTkLabel(f, text="The speaker's laptop sends only audio. This GPU turns it into text with Whisper,\nand everyone reads the captions live on their phone — no app, no cloud.", text_color="gray50", font=ctk.CTkFont(size=12), justify="center").pack(pady=(0, 12))
        tabs = ctk.CTkFrame(f, fg_color="transparent")
        tabs.pack(pady=(0, 10))
        self.cap_host_tab = ctk.CTkButton(tabs, text="Host (GPU laptop)", width=180, command=self.show_cap_host)
        self.cap_host_tab.grid(row=0, column=0, padx=5)
        self.cap_spk_tab = ctk.CTkButton(tabs, text="Speaker (any laptop)", width=180, command=self.show_cap_speaker, fg_color="transparent", border_width=2, text_color=("#121212", "#DCE4EE"))
        self.cap_spk_tab.grid(row=0, column=1, padx=5)
        box = ctk.CTkFrame(f, fg_color="transparent")
        box.pack(expand=True, fill="both", padx=40)

        # --- host pane ---
        h = self.cap_host_pane = ctk.CTkFrame(box, fg_color="transparent")
        self.cap_settings = ctk.CTkFrame(h, fg_color="transparent")
        self.cap_settings.pack(pady=4)
        ctk.CTkLabel(self.cap_settings, text="Model").grid(row=0, column=0, padx=6, sticky="e")
        self.cap_model = ctk.CTkSegmentedButton(self.cap_settings, values=["small", "medium"])
        self.cap_model.set("medium")
        self.cap_model.grid(row=0, column=1, padx=6, pady=4, sticky="w")
        ctk.CTkLabel(self.cap_settings, text="Spoken language").grid(row=1, column=0, padx=6, sticky="e")
        self.cap_lang = ctk.CTkOptionMenu(self.cap_settings, values=["Auto-detect", "English", "Tamil", "Hindi", "Telugu", "Malayalam", "Kannada"])
        self.cap_lang.set("English")
        self.cap_lang.grid(row=1, column=1, padx=6, pady=4, sticky="w")
        self.cap_mic_var = ctk.BooleanVar(value=True)
        ctk.CTkCheckBox(self.cap_settings, text="Also caption this laptop's microphone", variable=self.cap_mic_var).grid(row=2, column=0, columnspan=2, pady=6)
        self.cap_status = ctk.CTkLabel(h, text="Ready.", text_color="gray60", font=ctk.CTkFont(size=13))
        self.cap_status.pack(pady=4)
        self.cap_start_btn = ctk.CTkButton(h, text="⚡ Start Live Captions", height=44, width=320, command=self.start_captions)
        self.cap_start_btn.pack(pady=6)
        self.cap_live_row = ctk.CTkFrame(h, fg_color="transparent")
        self.cap_qr = ctk.CTkLabel(self.cap_live_row, text="")
        self.cap_qr.grid(row=0, column=0, rowspan=4, padx=12)
        self.cap_url = ctk.CTkLabel(self.cap_live_row, text="", font=ctk.CTkFont(size=16, weight="bold"), text_color="#1DB954")
        self.cap_url.grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(self.cap_live_row, text="Speaker code (for the speaker's laptop):", text_color="gray60").grid(row=1, column=1, sticky="w")
        self.cap_code = ctk.CTkLabel(self.cap_live_row, text="", font=ctk.CTkFont(size=30, weight="bold"), text_color="#1baf7a")
        self.cap_code.grid(row=2, column=1, sticky="w")
        ctk.CTkButton(self.cap_live_row, text="📺 Open captions page", width=220, command=lambda: webbrowser.open(self.cap_url_text)).grid(row=3, column=1, sticky="w", pady=6)
        self.cap_stop_btn = ctk.CTkButton(h, text="Stop Captions", height=36, width=320, fg_color="#E22134", hover_color="#B01625", command=self.stop_captions)
        self.cap_log = ctk.CTkTextbox(h, height=110, width=640, font=ctk.CTkFont(family="Consolas", size=11), wrap="word")

        # --- speaker pane ---
        s = self.cap_spk_pane = ctk.CTkFrame(box, fg_color="transparent")
        ctk.CTkButton(s, text="🔍 Find the GPU laptop", height=38, width=320, command=self.scan_captions_host).pack(pady=(12, 6))
        self.cap_spk_host = ctk.CTkEntry(s, width=320, height=40, placeholder_text="GPU laptop IP (auto-filled, or type it)")
        self.cap_spk_host.pack(pady=6)
        self.cap_spk_code = ctk.CTkEntry(s, width=320, height=40, placeholder_text="Speaker code", font=ctk.CTkFont(size=16, weight="bold"), justify="center")
        self.cap_spk_code.pack(pady=6)
        self.cap_spk_status = ctk.CTkLabel(s, text="", text_color="gray60", wraplength=420)
        self.cap_spk_status.pack(pady=6)
        self.cap_spk_btn = ctk.CTkButton(s, text="🎙 Start Speaking", height=44, width=320, command=self.toggle_speaker)
        self.cap_spk_btn.pack(pady=6)
        self.show_cap_host()

    def show_cap_host(self):
        self.cap_spk_pane.pack_forget()
        self.cap_host_pane.pack(expand=True, fill="both")
        self.cap_host_tab.configure(fg_color=["#3b8ed0", "#1f538d"])
        self.cap_spk_tab.configure(fg_color="transparent")

    def show_cap_speaker(self):
        self.cap_host_pane.pack_forget()
        self.cap_spk_pane.pack(expand=True, fill="both")
        self.cap_spk_tab.configure(fg_color=["#3b8ed0", "#1f538d"])
        self.cap_host_tab.configure(fg_color="transparent")

    def start_captions(self):
        langs = {"Auto-detect": "auto", "English": "en", "Tamil": "ta", "Hindi": "hi", "Telugu": "te", "Malayalam": "ml", "Kannada": "kn"}
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        args = [self._pool_python(), "-u", "nexus_captions.py", "--serve", "--code", code, "--model", self.cap_model.get(),
                "--language", langs[self.cap_lang.get()], "--port", str(self.CAPTIONS_PORT)]
        if self.cap_mic_var.get():
            args.append("--mic")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONWARNINGS"] = "ignore"
        self.captions_proc = subprocess.Popen(args, cwd=self.POOL_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                              encoding="utf-8", errors="replace", env=env, creationflags=self._pool_creationflags())
        threading.Thread(target=self._captions_read_log, args=(self.captions_proc,), daemon=True).start()
        self.cap_url_text = f"http://{self.get_lan_ip()}:{self.CAPTIONS_PORT}"
        self.cap_settings.pack_forget()
        self.cap_start_btn.pack_forget()
        self.cap_status.configure(text="⏳ Loading the speech model on the GPU (10–20 s the first time)...", text_color="#f39c12")
        try:
            import qrcode
            qr = qrcode.make(self.cap_url_text, box_size=8, border=2)
            img = (qr.get_image() if hasattr(qr, "get_image") else qr._img).convert("RGB")
            self.cap_qr_img = ctk.CTkImage(light_image=img, dark_image=img, size=(170, 170))
            self.cap_qr.configure(image=self.cap_qr_img, text="")
        except Exception:
            self.cap_qr.configure(text="(pip install qrcode)")
        self.cap_url.configure(text=self.cap_url_text)
        self.cap_code.configure(text=code)
        self.cap_live_row.pack(pady=6)
        self.cap_stop_btn.pack(pady=4)
        self.cap_log.delete("1.0", "end")
        self.cap_log.pack(pady=6)

    def _captions_read_log(self, proc):
        for line in proc.stdout:
            line = line.rstrip()
            if not line or "Warning" in line or line.startswith("=") or "%|" in line:
                continue
            if "SPEAKER CODE" in line:
                self.after(0, lambda: self.cap_status.configure(text="🟢 Live — scan to read captions, speak to see them appear", text_color="#1DB954"))
            line = line.replace("[captions] ", "")
            self.after(0, lambda l=line: (self.cap_log.insert("end", l.strip() + "\n"), self.cap_log.see("end")))

    def stop_captions(self):
        self._pool_kill(self.captions_proc)
        self.captions_proc = None
        for w in (self.cap_live_row, self.cap_stop_btn, self.cap_log):
            w.pack_forget()
        self.cap_settings.pack(pady=4)
        self.cap_start_btn.pack(pady=6)
        self.cap_status.configure(text="Ready.", text_color="gray60")

    def scan_captions_host(self):
        self.cap_spk_status.configure(text="Searching the network (3 s)...", text_color="#f39c12")

        def work():
            try:
                import nexus_captions
                hosts = nexus_captions.discover(3.0)
            except Exception:
                hosts = []
            def show():
                if hosts:
                    self.cap_spk_host.delete(0, "end")
                    self.cap_spk_host.insert(0, hosts[0]["ip"])
                    self.cap_spk_status.configure(text=f"Found {hosts[0]['host']} ({hosts[0]['gpu']})", text_color="#1DB954")
                else:
                    self.cap_spk_status.configure(text="Not found. Type the GPU laptop's IP shown on its captions screen.", text_color="#E22134")
            self.after(0, show)
        threading.Thread(target=work, daemon=True).start()

    def toggle_speaker(self):
        if self.speaker_proc and self.speaker_proc.poll() is None:
            self._pool_kill(self.speaker_proc)
            self.speaker_proc = None
            self.cap_spk_btn.configure(text="🎙 Start Speaking", fg_color=["#3b8ed0", "#1f538d"])
            self.cap_spk_status.configure(text="Stopped.", text_color="gray60")
            return
        host, code = self.cap_spk_host.get().strip(), self.cap_spk_code.get().strip()
        if not host or len(code) != 6 or not code.isdigit():
            self.cap_spk_status.configure(text="❌ Enter the GPU laptop's IP and the 6-digit speaker code.", text_color="#E22134")
            return
        env = os.environ.copy()
        env["PYTHONWARNINGS"] = "ignore"
        self.speaker_proc = subprocess.Popen([self._pool_python(), "-u", "nexus_captions.py", "--speak", "--host", host,
                                              "--code", code, "--port", str(self.CAPTIONS_PORT)],
                                             cwd=self.POOL_DIR, env=env, creationflags=self._pool_creationflags())
        self.cap_spk_btn.configure(text="■ Stop Speaking", fg_color="#E22134")
        self.cap_spk_status.configure(text=f"🎙 Streaming your microphone to {host} — speak now.", text_color="#1DB954")

    # =========================================================================
    # EM WAVE LAB -- runs nexus_em.py: this GPU solves Maxwell's equations
    # live; anyone on the Wi-Fi opens the lab by QR and edits the scene.
    # =========================================================================
    EM_PORT = 8780

    def show_em_screen(self):
        self.auth_frame.pack_forget()
        if not hasattr(self, 'em_frame'):
            self.build_em_screen()
        self.em_frame.pack(expand=True, fill="both")

    def back_to_signin_from_em(self):
        self.em_frame.pack_forget()
        self.auth_frame.pack(expand=True, fill="both")

    def build_em_screen(self):
        self.em_proc = None
        f = self.em_frame = ctk.CTkFrame(self, fg_color="#121212")
        ctk.CTkButton(f, text="← Back to Sign In", fg_color="transparent", text_color="gray50", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.back_to_signin_from_em).pack(pady=(20, 0), anchor="w", padx=20)
        ctk.CTkLabel(f, text="EM WAVE LAB", font=ctk.CTkFont(size=26, weight="bold"), text_color="#00b8d9").pack(pady=(10, 5))
        ctk.CTkLabel(f, text="This GPU solves Maxwell's equations on a grid of up to a million cells, thousands of times a second.\nAnyone on the Wi-Fi scans the QR code, places antennas and draws walls, and watches the waves live.", text_color="gray50", font=ctk.CTkFont(size=12), justify="center").pack(pady=(0, 12))
        self.em_settings = ctk.CTkFrame(f, fg_color="transparent")
        self.em_settings.pack(pady=4)
        ctk.CTkLabel(self.em_settings, text="Solver").grid(row=0, column=0, padx=6, sticky="e")
        self.em_solver = ctk.CTkSegmentedButton(self.em_settings, values=["GPU", "CPU (to compare)"])
        self.em_solver.set("GPU")
        self.em_solver.grid(row=0, column=1, padx=6, pady=4, sticky="w")
        ctk.CTkLabel(self.em_settings, text="Grid size").grid(row=1, column=0, padx=6, sticky="e")
        self.em_grid = ctk.CTkSegmentedButton(self.em_settings, values=["512", "768", "1024"])
        self.em_grid.set("768")
        self.em_grid.grid(row=1, column=1, padx=6, pady=4, sticky="w")
        self.em_status = ctk.CTkLabel(f, text="Ready.", text_color="gray60", font=ctk.CTkFont(size=13))
        self.em_status.pack(pady=4)
        self.em_start_btn = ctk.CTkButton(f, text="⚡ Start EM Wave Lab", height=44, width=320, command=self.start_em)
        self.em_start_btn.pack(pady=6)
        self.em_live_row = ctk.CTkFrame(f, fg_color="transparent")
        self.em_qr = ctk.CTkLabel(self.em_live_row, text="")
        self.em_qr.grid(row=0, column=0, rowspan=4, padx=12)
        self.em_url = ctk.CTkLabel(self.em_live_row, text="", font=ctk.CTkFont(size=16, weight="bold"), text_color="#1DB954")
        self.em_url.grid(row=0, column=1, sticky="w")
        self.em_speed_title = ctk.CTkLabel(self.em_live_row, text="Max solver speed:", text_color="gray60")
        self.em_speed_title.grid(row=1, column=1, sticky="w")
        self.em_speed = ctk.CTkLabel(self.em_live_row, text="—", font=ctk.CTkFont(size=24, weight="bold"), text_color="#00b8d9")
        self.em_speed.grid(row=2, column=1, sticky="w")
        self.em_live = ctk.CTkLabel(self.em_live_row, text="", text_color="gray60")
        self.em_live.grid(row=3, column=1, sticky="w")
        ctk.CTkButton(self.em_live_row, text="🌊 Open the lab", width=220, command=lambda: webbrowser.open(self.em_url_text)).grid(row=4, column=1, sticky="w", pady=6)
        self.em_stop_btn = ctk.CTkButton(f, text="Stop Lab", height=36, width=320, fg_color="#E22134", hover_color="#B01625", command=self.stop_em)
        self.em_log = ctk.CTkTextbox(f, height=110, width=640, font=ctk.CTkFont(family="Consolas", size=11), wrap="word")

    def start_em(self):
        args = [self._pool_python(), "-u", "nexus_em.py", "--grid", self.em_grid.get(), "--port", str(self.EM_PORT)]
        if self.em_solver.get() != "GPU":
            args.append("--cpu")
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONWARNINGS"] = "ignore"
        self.em_proc = subprocess.Popen(args, cwd=self.POOL_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        encoding="utf-8", errors="replace", env=env, creationflags=self._pool_creationflags())
        threading.Thread(target=self._em_read_log, args=(self.em_proc,), daemon=True).start()
        self.em_url_text = f"http://{self.get_lan_ip()}:{self.EM_PORT}"
        self.em_settings.pack_forget()
        self.em_start_btn.pack_forget()
        self.em_status.configure(text="⏳ Starting the solver...", text_color="#f39c12")
        try:
            import qrcode
            qr = qrcode.make(self.em_url_text, box_size=8, border=2)
            img = (qr.get_image() if hasattr(qr, "get_image") else qr._img).convert("RGB")
            self.em_qr_img = ctk.CTkImage(light_image=img, dark_image=img, size=(170, 170))
            self.em_qr.configure(image=self.em_qr_img, text="")
        except Exception:
            self.em_qr.configure(text="(pip install qrcode)")
        self.em_url.configure(text=self.em_url_text)
        self.em_speed.configure(text="measuring...")
        self.em_live.configure(text="")
        self.em_speed_title.configure(text=f"Max solver speed on the {'GPU' if self.em_solver.get() == 'GPU' else 'CPU'}:")
        self.em_live_row.pack(pady=6)
        self.em_stop_btn.pack(pady=4)
        self.em_log.delete("1.0", "end")
        self.em_log.pack(pady=6)

    def _em_read_log(self, proc):
        for line in proc.stdout:
            line = line.rstrip()
            if not line or "Warning" in line or line.startswith("="):
                continue
            if "EM WAVE LAB on" in line:
                self.after(0, lambda: self.em_status.configure(text="🟢 Live — scan to open the lab, tap to place antennas", text_color="#1DB954"))
            if line.startswith("[em] max speed:"):
                p = line.split()
                self.after(0, lambda p=p: self.em_speed.configure(text=f"{int(p[3]):,} steps/s  ·  {float(p[5]):,.0f} M cell-updates/s"))
            elif line.startswith("[em]") and "steps/s" in line:
                p = line.split()
                self.after(0, lambda p=p: self.em_live.configure(text=f"Live: {p[1]} steps/s (limited to 600 so the waves stay watchable) · {p[6]} viewer(s)"))
            line = line.replace("[em] ", "")
            self.after(0, lambda l=line: (self.em_log.insert("end", time.strftime("%H:%M:%S  ") + l.strip() + "\n"), self.em_log.see("end")))

    def stop_em(self):
        self._pool_kill(self.em_proc)
        self.em_proc = None
        for w in (self.em_live_row, self.em_stop_btn, self.em_log):
            w.pack_forget()
        self.em_settings.pack(pady=4)
        self.em_start_btn.pack(pady=6)
        self.em_status.configure(text="Ready.", text_color="gray60")

    # =========================================================================
    # ROOM SUPERCOMPUTER -- runs nexus_swarm.py: everyone in the room scans a QR
    # code and their phone's GPU renders squares of one giant picture; this
    # laptop hands out the squares, checks them and shows the result.
    # =========================================================================
    SWARM_PORT = 8790

    def show_swarm_screen(self):
        self.auth_frame.pack_forget()
        if not hasattr(self, 'swarm_frame'):
            self.build_swarm_screen()
        self.swarm_frame.pack(expand=True, fill="both")

    def back_to_signin_from_swarm(self):
        self.swarm_frame.pack_forget()
        self.auth_frame.pack(expand=True, fill="both")

    def build_swarm_screen(self):
        self.swarm_proc = None
        self.swarm_gpu_proc = None
        self.swarm_cpu_proc = None
        f = self.swarm_frame = ctk.CTkFrame(self, fg_color="#121212")
        ctk.CTkButton(f, text="← Back to Sign In", fg_color="transparent", text_color="gray50", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.back_to_signin_from_swarm).pack(pady=(20, 0), anchor="w", padx=20)
        ctk.CTkLabel(f, text="ROOM SUPERCOMPUTER", font=ctk.CTkFont(size=26, weight="bold"), text_color="#b48cff").pack(pady=(10, 5))
        ctk.CTkLabel(f, text="Everyone in the room scans the QR code. Each phone's GPU draws squares of one giant 3D picture,\nthis laptop checks every phone's work, and the picture appears on the projector.", text_color="gray50", font=ctk.CTkFont(size=12), justify="center").pack(pady=(0, 12))
        self.swarm_status = ctk.CTkLabel(f, text="Ready.", text_color="gray60", font=ctk.CTkFont(size=13))
        self.swarm_status.pack(pady=4)
        self.swarm_start_btn = ctk.CTkButton(f, text="⚡ Start Room Supercomputer", height=44, width=320, command=self.start_swarm)
        self.swarm_start_btn.pack(pady=6)
        self.swarm_live_row = ctk.CTkFrame(f, fg_color="transparent")
        self.swarm_qr = ctk.CTkLabel(self.swarm_live_row, text="")
        self.swarm_qr.grid(row=0, column=0, rowspan=5, padx=12)
        ctk.CTkLabel(self.swarm_live_row, text="Phones scan this, or open:", text_color="gray60").grid(row=0, column=1, sticky="w")
        self.swarm_url = ctk.CTkLabel(self.swarm_live_row, text="", font=ctk.CTkFont(size=16, weight="bold"), text_color="#1DB954")
        self.swarm_url.grid(row=1, column=1, sticky="w")
        ctk.CTkButton(self.swarm_live_row, text="🖥 Open the projector wall", width=260, command=lambda: webbrowser.open(self.swarm_url_text + "/wall")).grid(row=2, column=1, sticky="w", pady=(8, 4))
        self.swarm_gpu_btn = ctk.CTkButton(self.swarm_live_row, text="➕ Add this laptop's GPU as a worker", width=260, fg_color="transparent", border_width=2, command=self.toggle_swarm_gpu)
        self.swarm_gpu_btn.grid(row=3, column=1, sticky="w", pady=4)
        self.swarm_cpu_btn = ctk.CTkButton(self.swarm_live_row, text="➕ Add a CPU-only worker (to compare)", width=260, fg_color="transparent", border_width=2, command=self.toggle_swarm_cpu)
        self.swarm_cpu_btn.grid(row=4, column=1, sticky="w", pady=4)
        self.swarm_stop_btn = ctk.CTkButton(f, text="Stop", height=36, width=320, fg_color="#E22134", hover_color="#B01625", command=self.stop_swarm)
        self.swarm_log = ctk.CTkTextbox(f, height=120, width=660, font=ctk.CTkFont(family="Consolas", size=11), wrap="word")

    def start_swarm(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONWARNINGS"] = "ignore"
        self.swarm_proc = subprocess.Popen([self._pool_python(), "-u", "nexus_swarm.py", "--port", str(self.SWARM_PORT)],
                                           cwd=self.POOL_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                           encoding="utf-8", errors="replace", env=env, creationflags=self._pool_creationflags())
        threading.Thread(target=self._swarm_read_log, args=(self.swarm_proc,), daemon=True).start()
        self.swarm_url_text = f"http://{self.get_lan_ip()}:{self.SWARM_PORT}"
        self.swarm_start_btn.pack_forget()
        self.swarm_status.configure(text="⏳ Starting…", text_color="#f39c12")
        try:
            import qrcode
            qr = qrcode.make(self.swarm_url_text, box_size=8, border=2)
            img = (qr.get_image() if hasattr(qr, "get_image") else qr._img).convert("RGB")
            self.swarm_qr_img = ctk.CTkImage(light_image=img, dark_image=img, size=(190, 190))
            self.swarm_qr.configure(image=self.swarm_qr_img, text="")
        except Exception:
            self.swarm_qr.configure(text="(pip install qrcode)")
        self.swarm_url.configure(text=self.swarm_url_text)
        self.swarm_live_row.pack(pady=6)
        self.swarm_stop_btn.pack(pady=4)
        self.swarm_log.delete("1.0", "end")
        self.swarm_log.pack(pady=6)

    def _swarm_read_log(self, proc):
        for line in proc.stdout:
            line = line.rstrip()
            if not line or "Warning" in line or line.startswith("="):
                continue
            if "ROOM SUPERCOMPUTER" in line:
                self.after(0, lambda: (self.swarm_status.configure(text="🟢 Live — open the projector wall, then ask everyone to scan the QR code", text_color="#1DB954"),
                                       webbrowser.open(self.swarm_url_text + "/wall")))
            line = line.replace("[swarm] ", "")
            self.after(0, lambda l=line: (self.swarm_log.insert("end", time.strftime("%H:%M:%S  ") + l.strip() + "\n"), self.swarm_log.see("end")))

    def _chrome_path(self):
        for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                  r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                  os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
                  r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                  r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"):
            if os.path.exists(p):
                return p
        return None

    def _browser_worker(self, port, name, kind, flags, status_label=None):
        """A hidden browser that joins a demo (swarm or federated) like a phone would,
        using this laptop's GPU (or CPU)."""
        import tempfile
        import urllib.parse
        chrome = self._chrome_path()
        if not chrome:
            if status_label:
                status_label.configure(text="❌ Chrome or Edge is needed for a laptop worker.", text_color="#E22134")
            return None
        profile = tempfile.mkdtemp(prefix="nx_worker_")
        url = f"http://127.0.0.1:{port}/?auto=1&kind={kind}&name={urllib.parse.quote(name)}"
        return subprocess.Popen([chrome, "--headless=new", f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
                                 "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                                 "--disable-backgrounding-occluded-windows", *flags, url],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=self._pool_creationflags())

    def _swarm_browser_worker(self, name, kind, flags):
        return self._browser_worker(self.SWARM_PORT, name, kind, flags, self.swarm_status)

    GPU_WORKER_FLAGS = ["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist", "--force_high_performance_gpu"]
    CPU_WORKER_FLAGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]

    def toggle_swarm_gpu(self):
        if self.swarm_gpu_proc and self.swarm_gpu_proc.poll() is None:
            self._pool_kill(self.swarm_gpu_proc)
            self.swarm_gpu_proc = None
            self.swarm_gpu_btn.configure(text="➕ Add this laptop's GPU as a worker", fg_color="transparent")
            return
        self.swarm_gpu_proc = self._swarm_browser_worker("Host laptop GPU", "laptop",
                                                         ["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist", "--force_high_performance_gpu"])
        if self.swarm_gpu_proc:
            self.swarm_gpu_btn.configure(text="■ Remove this laptop's GPU", fg_color="#3b2a5a")

    def toggle_swarm_cpu(self):
        if self.swarm_cpu_proc and self.swarm_cpu_proc.poll() is None:
            self._pool_kill(self.swarm_cpu_proc)
            self.swarm_cpu_proc = None
            self.swarm_cpu_btn.configure(text="➕ Add a CPU-only worker (to compare)", fg_color="transparent")
            return
        self.swarm_cpu_proc = self._swarm_browser_worker("Laptop CPU (no GPU)", "cpu",
                                                         ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"])
        if self.swarm_cpu_proc:
            self.swarm_cpu_btn.configure(text="■ Remove the CPU-only worker", fg_color="#3b2a5a")

    def stop_swarm(self):
        for name in ('swarm_gpu_proc', 'swarm_cpu_proc', 'swarm_proc'):
            self._pool_kill(getattr(self, name, None))
            setattr(self, name, None)
        self.swarm_gpu_btn.configure(text="➕ Add this laptop's GPU as a worker", fg_color="transparent")
        self.swarm_cpu_btn.configure(text="➕ Add a CPU-only worker (to compare)", fg_color="transparent")
        for w in (self.swarm_live_row, self.swarm_stop_btn, self.swarm_log):
            w.pack_forget()
        self.swarm_start_btn.pack(pady=6)
        self.swarm_status.configure(text="Ready.", text_color="gray60")

    # =========================================================================
    # TRAIN AN AI TOGETHER -- runs nexus_fed.py: phones each train a shared
    # digit-recognition AI on their own private data (GPU, TensorFlow.js); this
    # laptop averages the updates, rejects poisoned ones, and shows accuracy.
    # =========================================================================
    FED_PORT = 8795

    def show_fed_screen(self):
        self.auth_frame.pack_forget()
        if not hasattr(self, 'fed_frame'):
            self.build_fed_screen()
        self.fed_frame.pack(expand=True, fill="both")

    def back_to_signin_from_fed(self):
        self.fed_frame.pack_forget()
        self.auth_frame.pack(expand=True, fill="both")

    def build_fed_screen(self):
        self.fed_proc = None
        self.fed_gpu_proc = None
        self.fed_cpu_proc = None
        f = self.fed_frame = ctk.CTkFrame(self, fg_color="#121212")
        ctk.CTkButton(f, text="← Back to Sign In", fg_color="transparent", text_color="gray50", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.back_to_signin_from_fed).pack(pady=(20, 0), anchor="w", padx=20)
        ctk.CTkLabel(f, text="TRAIN AN AI TOGETHER", font=ctk.CTkFont(size=26, weight="bold"), text_color="#6aa2ff").pack(pady=(10, 5))
        ctk.CTkLabel(f, text="Each phone trains a shared digit-recognition AI on its own private data using the phone's GPU,\nand sends back only what it learned. This laptop averages it, rejects poisoned updates, and the accuracy climbs live.", text_color="gray50", font=ctk.CTkFont(size=12), justify="center").pack(pady=(0, 12))
        self.fed_status = ctk.CTkLabel(f, text="Ready.", text_color="gray60", font=ctk.CTkFont(size=13))
        self.fed_status.pack(pady=4)
        self.fed_start_btn = ctk.CTkButton(f, text="⚡ Start the Federated AI server", height=44, width=320, command=self.start_fed)
        self.fed_start_btn.pack(pady=6)
        self.fed_live_row = ctk.CTkFrame(f, fg_color="transparent")
        self.fed_qr = ctk.CTkLabel(self.fed_live_row, text="")
        self.fed_qr.grid(row=0, column=0, rowspan=5, padx=12)
        ctk.CTkLabel(self.fed_live_row, text="Phones scan this, or open:", text_color="gray60").grid(row=0, column=1, sticky="w")
        self.fed_url = ctk.CTkLabel(self.fed_live_row, text="", font=ctk.CTkFont(size=16, weight="bold"), text_color="#1DB954")
        self.fed_url.grid(row=1, column=1, sticky="w")
        ctk.CTkButton(self.fed_live_row, text="🖥 Open the projector wall", width=260, command=lambda: webbrowser.open(self.fed_url_text + "/wall")).grid(row=2, column=1, sticky="w", pady=(8, 4))
        self.fed_gpu_btn = ctk.CTkButton(self.fed_live_row, text="➕ Add this laptop's GPU as a trainer", width=260, fg_color="transparent", border_width=2, command=self.toggle_fed_gpu)
        self.fed_gpu_btn.grid(row=3, column=1, sticky="w", pady=4)
        self.fed_cpu_btn = ctk.CTkButton(self.fed_live_row, text="➕ Add a CPU-only trainer (to compare)", width=260, fg_color="transparent", border_width=2, command=self.toggle_fed_cpu)
        self.fed_cpu_btn.grid(row=4, column=1, sticky="w", pady=4)
        self.fed_stop_btn = ctk.CTkButton(f, text="Stop", height=36, width=320, fg_color="#E22134", hover_color="#B01625", command=self.stop_fed)
        self.fed_log = ctk.CTkTextbox(f, height=120, width=660, font=ctk.CTkFont(family="Consolas", size=11), wrap="word")

    def start_fed(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONWARNINGS"] = "ignore"
        self.fed_proc = subprocess.Popen([self._pool_python(), "-u", "nexus_fed.py", "--port", str(self.FED_PORT)],
                                         cwd=self.POOL_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace", env=env, creationflags=self._pool_creationflags())
        threading.Thread(target=self._fed_read_log, args=(self.fed_proc,), daemon=True).start()
        self.fed_url_text = f"http://{self.get_lan_ip()}:{self.FED_PORT}"
        self.fed_start_btn.pack_forget()
        self.fed_status.configure(text="⏳ Starting…", text_color="#f39c12")
        try:
            import qrcode
            qr = qrcode.make(self.fed_url_text, box_size=8, border=2)
            img = (qr.get_image() if hasattr(qr, "get_image") else qr._img).convert("RGB")
            self.fed_qr_img = ctk.CTkImage(light_image=img, dark_image=img, size=(190, 190))
            self.fed_qr.configure(image=self.fed_qr_img, text="")
        except Exception:
            self.fed_qr.configure(text="(pip install qrcode)")
        self.fed_url.configure(text=self.fed_url_text)
        self.fed_live_row.pack(pady=6)
        self.fed_stop_btn.pack(pady=4)
        self.fed_log.delete("1.0", "end")
        self.fed_log.pack(pady=6)

    def _fed_read_log(self, proc):
        for line in proc.stdout:
            line = line.rstrip()
            if not line or "Warning" in line or line.startswith("="):
                continue
            if "FEDERATED LEARNING" in line:
                self.after(0, lambda: (self.fed_status.configure(text="🟢 Live — open the projector wall, then ask everyone to scan the QR code", text_color="#1DB954"),
                                       webbrowser.open(self.fed_url_text + "/wall")))
            line = line.replace("[fed] ", "")
            self.after(0, lambda l=line: (self.fed_log.insert("end", time.strftime("%H:%M:%S  ") + l.strip() + "\n"), self.fed_log.see("end")))

    def toggle_fed_gpu(self):
        if self.fed_gpu_proc and self.fed_gpu_proc.poll() is None:
            self._pool_kill(self.fed_gpu_proc)
            self.fed_gpu_proc = None
            self.fed_gpu_btn.configure(text="➕ Add this laptop's GPU as a trainer", fg_color="transparent")
            return
        self.fed_gpu_proc = self._browser_worker(self.FED_PORT, "Host laptop GPU", "laptop", self.GPU_WORKER_FLAGS, self.fed_status)
        if self.fed_gpu_proc:
            self.fed_gpu_btn.configure(text="■ Remove this laptop's GPU", fg_color="#2a3a5a")

    def toggle_fed_cpu(self):
        if self.fed_cpu_proc and self.fed_cpu_proc.poll() is None:
            self._pool_kill(self.fed_cpu_proc)
            self.fed_cpu_proc = None
            self.fed_cpu_btn.configure(text="➕ Add a CPU-only trainer (to compare)", fg_color="transparent")
            return
        self.fed_cpu_proc = self._browser_worker(self.FED_PORT, "Laptop CPU (no GPU)", "cpu", self.CPU_WORKER_FLAGS, self.fed_status)
        if self.fed_cpu_proc:
            self.fed_cpu_btn.configure(text="■ Remove the CPU-only trainer", fg_color="#2a3a5a")

    def stop_fed(self):
        for name in ('fed_gpu_proc', 'fed_cpu_proc', 'fed_proc'):
            self._pool_kill(getattr(self, name, None))
            setattr(self, name, None)
        self.fed_gpu_btn.configure(text="➕ Add this laptop's GPU as a trainer", fg_color="transparent")
        self.fed_cpu_btn.configure(text="➕ Add a CPU-only trainer (to compare)", fg_color="transparent")
        for w in (self.fed_live_row, self.fed_stop_btn, self.fed_log):
            w.pack_forget()
        self.fed_start_btn.pack(pady=6)
        self.fed_status.configure(text="Ready.", text_color="gray60")

    # =========================================================================
    # PASSWORD STRENGTH LAB -- runs nexus_crack.py: phones pool their GPUs to
    # brute-force a random demo password, showing why length & variety matter.
    # The target is always a random password this tool makes up; it is a
    # teaching demo about password strength, not an attack tool.
    # =========================================================================
    CRACK_PORT = 8798

    def show_crack_screen(self):
        self.auth_frame.pack_forget()
        if not hasattr(self, 'crack_frame'):
            self.build_crack_screen()
        self.crack_frame.pack(expand=True, fill="both")

    def back_to_signin_from_crack(self):
        self.crack_frame.pack_forget()
        self.auth_frame.pack(expand=True, fill="both")

    def build_crack_screen(self):
        self.crack_proc = None
        self.crack_gpu_proc = None
        self.crack_cpu_proc = None
        f = self.crack_frame = ctk.CTkFrame(self, fg_color="#121212")
        ctk.CTkButton(f, text="← Back to Sign In", fg_color="transparent", text_color="gray50", hover=False, font=ctk.CTkFont(size=12, underline=True), command=self.back_to_signin_from_crack).pack(pady=(20, 0), anchor="w", padx=20)
        ctk.CTkLabel(f, text="PASSWORD STRENGTH LAB", font=ctk.CTkFont(size=26, weight="bold"), text_color="#F2C744").pack(pady=(10, 5))
        ctk.CTkLabel(f, text="Phones pool their GPUs to brute-force a RANDOM demo password this laptop makes up — showing how fast a short\npassword falls and why a longer one is safe. No real passwords are involved; it's a teaching demo, not an attack tool.", text_color="gray50", font=ctk.CTkFont(size=12), justify="center").pack(pady=(0, 12))
        self.crack_status = ctk.CTkLabel(f, text="Ready.", text_color="gray60", font=ctk.CTkFont(size=13))
        self.crack_status.pack(pady=4)
        self.crack_start_btn = ctk.CTkButton(f, text="⚡ Start the Password Lab server", height=44, width=320, command=self.start_crack)
        self.crack_start_btn.pack(pady=6)
        self.crack_live_row = ctk.CTkFrame(f, fg_color="transparent")
        self.crack_qr = ctk.CTkLabel(self.crack_live_row, text="")
        self.crack_qr.grid(row=0, column=0, rowspan=5, padx=12)
        ctk.CTkLabel(self.crack_live_row, text="Phones scan this, or open:", text_color="gray60").grid(row=0, column=1, sticky="w")
        self.crack_url = ctk.CTkLabel(self.crack_live_row, text="", font=ctk.CTkFont(size=16, weight="bold"), text_color="#1DB954")
        self.crack_url.grid(row=1, column=1, sticky="w")
        ctk.CTkButton(self.crack_live_row, text="🖥 Open the projector wall", width=260, command=lambda: webbrowser.open(self.crack_url_text + "/wall")).grid(row=2, column=1, sticky="w", pady=(8, 4))
        self.crack_gpu_btn = ctk.CTkButton(self.crack_live_row, text="➕ Add this laptop's GPU as a cracker", width=260, fg_color="transparent", border_width=2, command=self.toggle_crack_gpu)
        self.crack_gpu_btn.grid(row=3, column=1, sticky="w", pady=4)
        self.crack_cpu_btn = ctk.CTkButton(self.crack_live_row, text="➕ Add a CPU-only cracker (to compare)", width=260, fg_color="transparent", border_width=2, command=self.toggle_crack_cpu)
        self.crack_cpu_btn.grid(row=4, column=1, sticky="w", pady=4)
        self.crack_stop_btn = ctk.CTkButton(f, text="Stop", height=36, width=320, fg_color="#E22134", hover_color="#B01625", command=self.stop_crack)
        self.crack_log = ctk.CTkTextbox(f, height=120, width=660, font=ctk.CTkFont(family="Consolas", size=11), wrap="word")

    def start_crack(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONWARNINGS"] = "ignore"
        self.crack_proc = subprocess.Popen([self._pool_python(), "-u", "nexus_crack.py", "--port", str(self.CRACK_PORT)],
                                           cwd=self.POOL_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                           encoding="utf-8", errors="replace", env=env, creationflags=self._pool_creationflags())
        threading.Thread(target=self._crack_read_log, args=(self.crack_proc,), daemon=True).start()
        self.crack_url_text = f"http://{self.get_lan_ip()}:{self.CRACK_PORT}"
        self.crack_start_btn.pack_forget()
        self.crack_status.configure(text="⏳ Starting…", text_color="#f39c12")
        try:
            import qrcode
            qr = qrcode.make(self.crack_url_text, box_size=8, border=2)
            img = (qr.get_image() if hasattr(qr, "get_image") else qr._img).convert("RGB")
            self.crack_qr_img = ctk.CTkImage(light_image=img, dark_image=img, size=(190, 190))
            self.crack_qr.configure(image=self.crack_qr_img, text="")
        except Exception:
            self.crack_qr.configure(text="(pip install qrcode)")
        self.crack_url.configure(text=self.crack_url_text)
        self.crack_live_row.pack(pady=6)
        self.crack_stop_btn.pack(pady=4)
        self.crack_log.delete("1.0", "end")
        self.crack_log.pack(pady=6)

    def _crack_read_log(self, proc):
        for line in proc.stdout:
            line = line.rstrip()
            if not line or "Warning" in line or line.startswith("="):
                continue
            if "PASSWORD STRENGTH LAB" in line:
                self.after(0, lambda: (self.crack_status.configure(text="🟢 Live — open the projector wall, then ask everyone to scan the QR code", text_color="#1DB954"),
                                       webbrowser.open(self.crack_url_text + "/wall")))
            line = line.replace("[crack] ", "")
            self.after(0, lambda l=line: (self.crack_log.insert("end", time.strftime("%H:%M:%S  ") + l.strip() + "\n"), self.crack_log.see("end")))

    def toggle_crack_gpu(self):
        if self.crack_gpu_proc and self.crack_gpu_proc.poll() is None:
            self._pool_kill(self.crack_gpu_proc)
            self.crack_gpu_proc = None
            self.crack_gpu_btn.configure(text="➕ Add this laptop's GPU as a cracker", fg_color="transparent")
            return
        self.crack_gpu_proc = self._browser_worker(self.CRACK_PORT, "Host laptop GPU", "laptop", self.GPU_WORKER_FLAGS, self.crack_status)
        if self.crack_gpu_proc:
            self.crack_gpu_btn.configure(text="■ Remove this laptop's GPU", fg_color="#3b3312")

    def toggle_crack_cpu(self):
        if self.crack_cpu_proc and self.crack_cpu_proc.poll() is None:
            self._pool_kill(self.crack_cpu_proc)
            self.crack_cpu_proc = None
            self.crack_cpu_btn.configure(text="➕ Add a CPU-only cracker (to compare)", fg_color="transparent")
            return
        self.crack_cpu_proc = self._browser_worker(self.CRACK_PORT, "Laptop CPU (no GPU)", "cpu", self.CPU_WORKER_FLAGS, self.crack_status)
        if self.crack_cpu_proc:
            self.crack_cpu_btn.configure(text="■ Remove the CPU-only cracker", fg_color="#3b3312")

    def stop_crack(self):
        for name in ('crack_gpu_proc', 'crack_cpu_proc', 'crack_proc'):
            self._pool_kill(getattr(self, name, None))
            setattr(self, name, None)
        self.crack_gpu_btn.configure(text="➕ Add this laptop's GPU as a cracker", fg_color="transparent")
        self.crack_cpu_btn.configure(text="➕ Add a CPU-only cracker (to compare)", fg_color="transparent")
        for w in (self.crack_live_row, self.crack_stop_btn, self.crack_log):
            w.pack_forget()
        self.crack_start_btn.pack(pady=6)
        self.crack_status.configure(text="Ready.", text_color="gray60")

    def on_closing(self):
        for name in ('game_proc', 'pool_proc', 'arena_proc', 'captions_proc', 'speaker_proc', 'em_proc',
                     'swarm_gpu_proc', 'swarm_cpu_proc', 'swarm_proc',
                     'fed_gpu_proc', 'fed_cpu_proc', 'fed_proc',
                     'crack_gpu_proc', 'crack_cpu_proc', 'crack_proc'):
            try:
                self._pool_kill(getattr(self, name, None))
            except Exception:
                pass
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