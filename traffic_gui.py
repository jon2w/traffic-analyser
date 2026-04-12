#!/usr/bin/env python3
"""
Traffic Analyser Desktop App — GUI for local analysis + remote submission.

Simple desktop application for non-technical users to:
1. Record or select a video file
2. Run local analysis with one click
3. Auto-submit results to remote server (with API key auth)

No command line needed — just double-click and go!

Requirements:
    pip install requests
    (tkinter is built into Python)

Usage:
    python traffic_gui.py
"""

import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import requests


# ─── Zone editor constants ─────────────────────────────────────────────────────

ZONES_PATH   = Path(__file__).parent / "zones.json"
ZONE_TYPES   = ["side_on", "end_on"]
SNAP_RADIUS  = 12   # canvas pixels — click within this distance of a point to delete it
PALETTE_RGB  = [
    (220,  50,  50),  # red
    (220, 200,   0),  # yellow
    ( 50, 180,  50),  # green
    ( 50, 100, 220),  # blue
    (180,  50, 220),  # magenta
    (220, 130,   0),  # orange
]


# ─── Configuration Management ──────────────────────────────────────────────────

CONFIG_FILE = Path.home() / ".traffic_analyzer_config.json"

def load_config():
    """Load settings from local config file."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, 'r') as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_config(config):
    """Save settings to local config file."""
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        print(f"Failed to save config: {e}")

def get_config_value(key, default=""):
    """Get a config value."""
    return load_config().get(key, default)

def set_config_value(key, value):
    """Set a config value."""
    config = load_config()
    config[key] = value
    save_config(config)


# ─── Main Application ──────────────────────────────────────────────────────────

class TrafficAnalyzerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Traffic Analyzer")
        self.root.geometry("700x800")
        self.root.resizable(True, True)
        
        self.selected_file = None
        self.is_processing = False
        self.annotated_path = None  # set after processing if save-annotated is on
        
        # Define colors
        self.bg_color = "#f0f0f0"
        self.header_color = "#2c3e50"
        self.button_color = "#27ae60"
        self.button_hover = "#229954"
        
        self.root.configure(bg=self.bg_color)
        
        self.build_ui()
        self.check_first_run()
    
    def build_ui(self):
        """Build the user interface."""
        
        # ── Header ─────────────────────────────────────────────────────────────
        header_frame = tk.Frame(self.root, bg=self.header_color, height=60)
        header_frame.pack(fill=tk.X, side=tk.TOP)
        header_frame.pack_propagate(False)
        
        title_label = tk.Label(
            header_frame,
            text="Traffic Analyzer",
            font=("Arial", 18, "bold"),
            fg="white",
            bg=self.header_color
        )
        title_label.pack(side=tk.LEFT, padx=20, pady=10)
        
        settings_btn = tk.Button(
            header_frame,
            text="⚙ Settings",
            font=("Arial", 10),
            bg="#34495e",
            fg="white",
            command=self.show_settings,
            relief=tk.FLAT,
            padx=15,
            pady=5
        )
        settings_btn.pack(side=tk.RIGHT, padx=20, pady=10)

        zones_btn = tk.Button(
            header_frame,
            text="⬡ Configure Zones",
            font=("Arial", 10),
            bg="#1a6b8a",
            fg="white",
            command=self.show_zone_editor,
            relief=tk.FLAT,
            padx=15,
            pady=5
        )
        zones_btn.pack(side=tk.RIGHT, padx=(0, 5), pady=10)

        batch_btn = tk.Button(
            header_frame,
            text="⊞ Batch Process",
            font=("Arial", 10),
            bg="#6d4c8a",
            fg="white",
            command=self.show_batch_processor,
            relief=tk.FLAT,
            padx=15,
            pady=5
        )
        batch_btn.pack(side=tk.RIGHT, padx=(0, 5), pady=10)
        
        # ── Main Content ───────────────────────────────────────────────────────
        content_frame = tk.Frame(self.root, bg=self.bg_color)
        content_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=20)
        
        # File selection
        file_frame = tk.LabelFrame(
            content_frame,
            text="1. Select Video File",
            font=("Arial", 11, "bold"),
            bg=self.bg_color,
            padx=10,
            pady=10
        )
        file_frame.pack(fill=tk.X, pady=(0, 15))
        
        self.file_label = tk.Label(
            file_frame,
            text="No file selected",
            font=("Arial", 10),
            bg="white",
            fg="#7f8c8d",
            anchor=tk.W,
            padx=10,
            pady=8
        )
        self.file_label.pack(fill=tk.X, pady=(0, 10))
        
        file_btn = tk.Button(
            file_frame,
            text="📁 Choose File...",
            font=("Arial", 11),
            bg=self.button_color,
            fg="white",
            command=self.select_file,
            relief=tk.FLAT,
            padx=15,
            pady=8
        )
        file_btn.pack(fill=tk.X)
        
        # Location name
        location_frame = tk.LabelFrame(
            content_frame,
            text="2. Location Name (Optional)",
            font=("Arial", 11, "bold"),
            bg=self.bg_color,
            padx=10,
            pady=10
        )
        location_frame.pack(fill=tk.X, pady=(0, 15))
        
        self.location_var = tk.StringVar(value=get_config_value("last_location", ""))
        location_entry = tk.Entry(
            location_frame,
            textvariable=self.location_var,
            font=("Arial", 10),
            bg="white"
        )
        location_entry.pack(fill=tk.X, pady=(0, 5))
        
        help_label = tk.Label(
            location_frame,
            text="E.g., 'Main Street', 'Parking Lot #3', 'Downtown Intersection'",
            font=("Arial", 9),
            fg="#7f8c8d",
            bg=self.bg_color
        )
        help_label.pack(anchor=tk.W)
        
        # Process button
        process_frame = tk.Frame(content_frame, bg=self.bg_color)
        process_frame.pack(fill=tk.X, pady=(0, 15))
        
        self.process_btn = tk.Button(
            process_frame,
            text="▶ Process & Submit",
            font=("Arial", 12, "bold"),
            bg=self.button_color,
            fg="white",
            command=self.process_and_submit,
            relief=tk.FLAT,
            padx=20,
            pady=12
        )
        self.process_btn.pack(fill=tk.X)

        # Options row
        options_frame = tk.Frame(content_frame, bg=self.bg_color)
        options_frame.pack(fill=tk.X, pady=(5, 5))

        self.submit_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            options_frame,
            text="Submit results to server",
            variable=self.submit_var,
            font=("Arial", 10),
            bg=self.bg_color,
        ).pack(side=tk.LEFT, padx=(0, 20))

        self.save_annotated_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            options_frame,
            text="Save annotated video",
            variable=self.save_annotated_var,
            font=("Arial", 10),
            bg=self.bg_color,
        ).pack(side=tk.LEFT)

        self.open_video_btn = tk.Button(
            options_frame,
            text="▶ Open annotated video",
            font=("Arial", 10),
            bg="#1a6b8a",
            fg="white",
            command=self.open_annotated_video,
            relief=tk.FLAT,
            padx=10,
            pady=4,
            state=tk.DISABLED,
        )
        self.open_video_btn.pack(side=tk.RIGHT)

        # Output panel
        output_frame = tk.LabelFrame(
            content_frame,
            text="3. Processing Output",
            font=("Arial", 11, "bold"),
            bg=self.bg_color,
            padx=10,
            pady=10
        )
        output_frame.pack(fill=tk.BOTH, expand=True, pady=(0, 15))
        
        self.output_text = scrolledtext.ScrolledText(
            output_frame,
            height=15,
            font=("Courier", 9),
            bg="white",
            fg="#2c3e50",
            state=tk.DISABLED
        )
        self.output_text.pack(fill=tk.BOTH, expand=True)
        
        # Status bar
        self.status_var = tk.StringVar(value="Ready")
        status_bar = tk.Label(
            self.root,
            textvariable=self.status_var,
            font=("Arial", 9),
            bg="#34495e",
            fg="white",
            anchor=tk.W,
            padx=10,
            pady=8
        )
        status_bar.pack(fill=tk.X, side=tk.BOTTOM)
    
    def check_first_run(self):
        """Show settings if this is first run."""
        if not get_config_value("server_url"):
            self.show_settings()
    
    def show_settings(self):
        """Show settings dialog."""
        settings_win = tk.Toplevel(self.root)
        settings_win.title("Settings")
        settings_win.geometry("500x380")
        settings_win.transient(self.root)
        settings_win.grab_set()
        
        # Server URL
        url_frame = tk.Frame(settings_win, bg=self.bg_color)
        url_frame.pack(fill=tk.X, padx=20, pady=(20, 10))
        
        tk.Label(
            url_frame,
            text="Server URL:",
            font=("Arial", 10, "bold"),
            bg=self.bg_color
        ).pack(anchor=tk.W)
        
        server_var = tk.StringVar(value=get_config_value("server_url", "https://your-domain.com"))
        server_entry = tk.Entry(url_frame, textvariable=server_var, font=("Arial", 10), width=50)
        server_entry.pack(fill=tk.X, pady=(5, 0))
        
        help1 = tk.Label(
            url_frame,
            text="E.g., https://your-domain.com (via CloudFlare Tunnel)",
            font=("Arial", 9),
            fg="#7f8c8d",
            bg=self.bg_color
        )
        help1.pack(anchor=tk.W)
        
        # API Key
        key_frame = tk.Frame(settings_win, bg=self.bg_color)
        key_frame.pack(fill=tk.X, padx=20, pady=(10, 10))
        
        tk.Label(
            key_frame,
            text="API Key:",
            font=("Arial", 10, "bold"),
            bg=self.bg_color
        ).pack(anchor=tk.W)
        
        key_var = tk.StringVar(value=get_config_value("api_key", ""))
        key_entry = tk.Entry(key_frame, textvariable=key_var, font=("Arial", 10), width=50, show="•")
        key_entry.pack(fill=tk.X, pady=(5, 0))
        
        help2 = tk.Label(
            key_frame,
            text="Your API key for authentication (keep secret!)",
            font=("Arial", 9),
            fg="#7f8c8d",
            bg=self.bg_color
        )
        help2.pack(anchor=tk.W)
        
        # Analysis location
        analyse_frame = tk.Frame(settings_win, bg=self.bg_color)
        analyse_frame.pack(fill=tk.X, padx=20, pady=(10, 10))
        
        tk.Label(
            analyse_frame,
            text="analyse.py Location:",
            font=("Arial", 10, "bold"),
            bg=self.bg_color
        ).pack(anchor=tk.W)
        
        analyse_var = tk.StringVar(value=get_config_value("analyse_path", "analyse.py"))
        analyse_entry = tk.Entry(analyse_frame, textvariable=analyse_var, font=("Arial", 10), width=50)
        analyse_entry.pack(fill=tk.X, pady=(5, 0))
        
        help3 = tk.Label(
            analyse_frame,
            text="Path to analyse.py (relative or absolute)",
            font=("Arial", 9),
            fg="#7f8c8d",
            bg=self.bg_color
        )
        help3.pack(anchor=tk.W)
        
        # Buttons
        btn_frame = tk.Frame(settings_win, bg=self.bg_color)
        btn_frame.pack(fill=tk.X, padx=20, pady=20)
        
        def save_settings():
            set_config_value("server_url", server_var.get().strip().rstrip("/"))
            set_config_value("api_key", key_var.get())
            set_config_value("analyse_path", analyse_var.get().strip())
            
            if server_var.get().strip() and key_var.get().strip():
                messagebox.showinfo("Settings", "Settings saved successfully!")
                settings_win.destroy()
            else:
                messagebox.showerror("Error", "Server URL and API Key are required!")
        
        save_btn = tk.Button(
            btn_frame,
            text="Save",
            font=("Arial", 11),
            bg=self.button_color,
            fg="white",
            command=save_settings,
            relief=tk.FLAT,
            padx=20,
            pady=8
        )
        save_btn.pack(side=tk.LEFT, padx=(0, 10))
        
        cancel_btn = tk.Button(
            btn_frame,
            text="Cancel",
            font=("Arial", 11),
            bg="#95a5a6",
            fg="white",
            command=settings_win.destroy,
            relief=tk.FLAT,
            padx=20,
            pady=8
        )
        cancel_btn.pack(side=tk.LEFT)
    
    def show_batch_processor(self):
        BatchProcessWindow(self.root)

    def open_annotated_video(self):
        """Open the annotated video in the system default player."""
        if self.annotated_path and os.path.exists(self.annotated_path):
            os.startfile(self.annotated_path)
        else:
            messagebox.showerror("Error", "Annotated video file not found.")

    def show_zone_editor(self):
        ZoneEditorWindow(self.root)

    def select_file(self):
        """Open file picker."""
        filepath = filedialog.askopenfilename(
            title="Select MP4 Video File",
            filetypes=[("MP4 Videos", "*.mp4"), ("All Files", "*.*")],
            initialdir=Path.home() / "Videos"
        )
        
        if filepath:
            self.selected_file = filepath
            filename = Path(filepath).name
            self.file_label.config(text=f"✓ {filename}", fg="#27ae60")
            self.status_var.set(f"Ready to process: {filename}")
    
    def log_output(self, message):
        """Add text to output panel."""
        self.output_text.config(state=tk.NORMAL)
        self.output_text.insert(tk.END, message + "\n")
        self.output_text.see(tk.END)
        self.output_text.config(state=tk.DISABLED)
        self.root.update()
    
    def process_and_submit(self):
        """Process video and submit results."""
        if not self.selected_file:
            messagebox.showerror("Error", "Please select a video file first!")
            return
        
        if not os.path.exists(self.selected_file):
            messagebox.showerror("Error", f"File not found: {self.selected_file}")
            return
        
        # Check settings only if submitting
        submit_to_server = self.submit_var.get()
        server_url = get_config_value("server_url")
        api_key = get_config_value("api_key")

        if submit_to_server and (not server_url or not api_key):
            messagebox.showerror("Error", "Please configure server URL and API key in Settings!")
            return

        # Save location name for next time
        location_name = self.location_var.get().strip()
        if location_name:
            set_config_value("last_location", location_name)

        save_annotated = self.save_annotated_var.get()

        # Run in background thread to avoid freezing UI
        thread = threading.Thread(
            target=self._process_and_submit_bg,
            args=(self.selected_file, location_name, server_url, api_key,
                  submit_to_server, save_annotated)
        )
        thread.daemon = True
        thread.start()
    
    def _process_and_submit_bg(self, filepath, location_name, server_url, api_key,
                               submit_to_server=True, save_annotated=False):
        """Background thread for processing and submission."""
        try:
            self.is_processing = True
            self.annotated_path = None
            self.process_btn.config(state=tk.DISABLED)
            self.open_video_btn.config(state=tk.DISABLED)
            self.output_text.config(state=tk.NORMAL)
            self.output_text.delete(1.0, tk.END)
            self.output_text.config(state=tk.DISABLED)

            filename = Path(filepath).name
            self.log_output(f"Starting analysis of: {filename}\n")
            self.status_var.set("Processing...")

            # Step 1: Run analyse.py
            self.log_output("=" * 60)
            self.log_output("Step 1: Running local analysis...")
            self.log_output("=" * 60 + "\n")

            analyse_path = get_config_value("analyse_path", "analyse.py")
            # Resolve to absolute path relative to the GUI script's own directory
            # so it works correctly when launched via a desktop shortcut
            if not os.path.isabs(analyse_path):
                analyse_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), analyse_path
                )
            base_dir = os.path.dirname(analyse_path)

            results_path = os.path.join(base_dir, "results.json")
            cmd = [
                sys.executable,
                analyse_path,
                "--input", filepath,
                "--no-show",
                "--output-json", results_path,
            ]

            if save_annotated:
                stem = Path(filepath).stem
                annotated_path = os.path.join(base_dir, f"{stem}_annotated.mp4")
                cmd += ["--output", annotated_path]
            else:
                annotated_path = None

            self.log_output(f"Running: {' '.join(cmd)}\n")

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=base_dir,   # always run from project dir so results.json lands there
            )
            
            for line in proc.stdout:
                self.log_output(line.rstrip())
            
            proc.wait()
            
            if proc.returncode != 0:
                self.log_output(f"\n❌ Analysis failed with exit code {proc.returncode}")
                self.status_var.set("Analysis failed!")
                messagebox.showerror("Error", "Analysis failed. Check output for details.")
                return
            
            self.log_output("\n✓ Analysis complete!\n")

            # Show annotated video button if file was saved
            if annotated_path and os.path.exists(annotated_path):
                self.annotated_path = annotated_path
                self.open_video_btn.config(state=tk.NORMAL)
                self.log_output(f"Annotated video saved: {annotated_path}\n")

            if not submit_to_server:
                # Just show local results summary
                if os.path.exists(results_path):
                    with open(results_path, "r", encoding="utf-8") as f:
                        results = json.load(f)
                    vehicle_count = len(results.get("vehicles", []))
                    self.log_output(f"Vehicles detected: {vehicle_count}")
                self.status_var.set("✓ Analysis complete (not submitted)")
                return

            # Step 2: Find and read results.json
            self.log_output("=" * 60)
            self.log_output("Step 2: Submitting results to server...")
            self.log_output("=" * 60 + "\n")

            if not os.path.exists(results_path):
                self.log_output(f"❌ Results file not found: {results_path}")
                self.status_var.set("Results file not found!")
                messagebox.showerror("Error", "Could not find results.json after analysis")
                return

            self.log_output(f"Found results: {results_path}\n")

            with open(results_path, "r", encoding="utf-8") as f:
                results = json.load(f)

            # Add metadata
            results["filename"] = filename
            if location_name:
                results["location_name"] = location_name
            results["camera_name"] = "Desktop App"

            # Step 3: Submit to server
            self.log_output(f"Submitting {len(results.get('vehicles', []))} vehicle(s) to server...\n")

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            }

            submit_url = f"{server_url}/api/submit_results"
            self.log_output(f"POST {submit_url}\n")

            resp = requests.post(
                submit_url,
                headers=headers,
                json=results,
                timeout=60
            )

            if resp.status_code == 201:
                data = resp.json()
                recording_id = data.get("recording_id")
                vehicle_count = data.get("vehicle_count", 0)

                self.log_output(f"\n✓ Results submitted successfully!")
                self.log_output(f"  Recording ID: {recording_id}")
                self.log_output(f"  Vehicles detected: {vehicle_count}\n")

                self.status_var.set(f"✓ Complete! Recording #{recording_id}, {vehicle_count} vehicles")
                messagebox.showinfo(
                    "Success!",
                    f"Results submitted successfully!\n\n"
                    f"Recording ID: {recording_id}\n"
                    f"Vehicles detected: {vehicle_count}"
                )
            else:
                error_msg = resp.text
                try:
                    error_data = resp.json()
                    error_msg = error_data.get("error", error_msg)
                except Exception:
                    pass

                self.log_output(f"\n❌ Submission failed (HTTP {resp.status_code})")
                self.log_output(f"Error: {error_msg}\n")
                self.status_var.set("Submission failed!")
                messagebox.showerror("Error", f"Submission failed:\n{error_msg}")

        except Exception as e:
            self.log_output(f"\n❌ Error: {str(e)}\n")
            self.status_var.set("Error!")
            messagebox.showerror("Error", f"An error occurred:\n{str(e)}")

        finally:
            self.is_processing = False
            self.process_btn.config(state=tk.NORMAL)


# ─── Batch Processor ──────────────────────────────────────────────────────────

class BatchProcessWindow:
    """
    Batch video processor.  Scans one or more folders recursively for .mp4 files,
    skips anything already submitted (tracked locally + optionally synced from
    server), and processes the remainder one by one.
    """

    SUBMITTED_FILE = Path.home() / ".traffic_analyzer_submitted.json"

    # Treeview column layout
    _COLS = ("file", "size", "status")

    def __init__(self, parent):
        self.parent = parent
        self.win = tk.Toplevel(parent)
        self.win.title("Batch Process")
        self.win.geometry("900x650")
        self.win.resizable(True, True)

        # queue: list of dicts {path, size, mtime, iid (treeview row id)}
        self.queue = []
        self.submitted = {}   # path -> {size, mtime, submitted_at, recording_id}
        self.stop_flag = False
        self.is_running = False

        self._load_submitted()
        self._build_ui()

    # ── persistence ───────────────────────────────────────────────────────────

    def _load_submitted(self):
        try:
            if self.SUBMITTED_FILE.exists():
                with open(self.SUBMITTED_FILE, "r", encoding="utf-8") as f:
                    self.submitted = json.load(f).get("files", {})
        except Exception:
            self.submitted = {}

    def _save_submitted(self):
        try:
            with open(self.SUBMITTED_FILE, "w", encoding="utf-8") as f:
                json.dump({"files": self.submitted}, f, indent=2)
        except Exception as e:
            print(f"Warning: could not save submitted list: {e}")

    def _is_submitted(self, path, size, mtime):
        entry = self.submitted.get(path)
        if not entry:
            return False
        return entry.get("size") == size and abs(entry.get("mtime", 0) - mtime) < 2

    def _mark_submitted(self, path, size, mtime, recording_id=None):
        self.submitted[path] = {
            "size": size,
            "mtime": mtime,
            "submitted_at": datetime.now().isoformat(),
            "recording_id": recording_id,
        }
        self._save_submitted()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        bg = "#f0f0f0"
        self.win.configure(bg=bg)

        # ── top controls ──────────────────────────────────────────────────────
        top = tk.Frame(self.win, bg=bg)
        top.pack(fill=tk.X, padx=10, pady=8)

        tk.Button(top, text="+ Add Folder", font=("Arial", 10),
                  bg="#27ae60", fg="white", relief=tk.FLAT, padx=10, pady=4,
                  command=self.add_folder).pack(side=tk.LEFT, padx=(0, 6))

        tk.Button(top, text="Clear Done/Skipped", font=("Arial", 10),
                  bg="#95a5a6", fg="white", relief=tk.FLAT, padx=10, pady=4,
                  command=self.clear_done).pack(side=tk.LEFT, padx=(0, 6))

        tk.Button(top, text="Sync from Server", font=("Arial", 10),
                  bg="#1a6b8a", fg="white", relief=tk.FLAT, padx=10, pady=4,
                  command=self.sync_from_server).pack(side=tk.LEFT, padx=(0, 6))

        self.start_btn = tk.Button(top, text="▶ Start", font=("Arial", 10, "bold"),
                                   bg="#27ae60", fg="white", relief=tk.FLAT,
                                   padx=14, pady=4, command=self.start_processing)
        self.start_btn.pack(side=tk.RIGHT, padx=(6, 0))

        self.stop_btn = tk.Button(top, text="■ Stop", font=("Arial", 10),
                                  bg="#c0392b", fg="white", relief=tk.FLAT,
                                  padx=14, pady=4, command=self.stop_processing,
                                  state=tk.DISABLED)
        self.stop_btn.pack(side=tk.RIGHT, padx=(6, 0))

        # ── options row ───────────────────────────────────────────────────────
        opts = tk.Frame(self.win, bg=bg)
        opts.pack(fill=tk.X, padx=10, pady=(0, 4))

        self.submit_var = tk.BooleanVar(value=True)
        tk.Checkbutton(opts, text="Submit results to server", variable=self.submit_var,
                       font=("Arial", 10), bg=bg).pack(side=tk.LEFT, padx=(0, 16))

        self.annotated_var = tk.BooleanVar(value=False)
        tk.Checkbutton(opts, text="Save annotated videos", variable=self.annotated_var,
                       font=("Arial", 10), bg=bg).pack(side=tk.LEFT)

        # ── file treeview ─────────────────────────────────────────────────────
        tree_frame = tk.Frame(self.win, bg=bg)
        tree_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=(0, 6))

        cols = ("file", "size", "status")
        self.tree = ttk.Treeview(tree_frame, columns=cols, show="headings", height=15)
        self.tree.heading("file",   text="File")
        self.tree.heading("size",   text="Size")
        self.tree.heading("status", text="Status")
        self.tree.column("file",   width=540, stretch=True)
        self.tree.column("size",   width=80,  anchor=tk.E, stretch=False)
        self.tree.column("status", width=120, anchor=tk.CENTER, stretch=False)

        vsb = ttk.Scrollbar(tree_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        # colour tags
        self.tree.tag_configure("pending",    foreground="#2c3e50")
        self.tree.tag_configure("skipped",    foreground="#7f8c8d")
        self.tree.tag_configure("processing", foreground="#e67e22", font=("Arial", 9, "bold"))
        self.tree.tag_configure("done",       foreground="#27ae60")
        self.tree.tag_configure("failed",     foreground="#c0392b")

        # ── progress bar ──────────────────────────────────────────────────────
        prog_frame = tk.Frame(self.win, bg=bg)
        prog_frame.pack(fill=tk.X, padx=10, pady=(0, 4))

        self.progress_var = tk.DoubleVar(value=0)
        self.progress_bar = ttk.Progressbar(prog_frame, variable=self.progress_var,
                                             maximum=100)
        self.progress_bar.pack(fill=tk.X)

        self.progress_label = tk.Label(prog_frame, text="", font=("Arial", 9),
                                       bg=bg, anchor=tk.W)
        self.progress_label.pack(fill=tk.X)

        # ── log area ──────────────────────────────────────────────────────────
        log_frame = tk.LabelFrame(self.win, text="Log", font=("Arial", 10, "bold"),
                                  bg=bg, padx=6, pady=6)
        log_frame.pack(fill=tk.X, padx=10, pady=(0, 10))

        self.log = scrolledtext.ScrolledText(log_frame, height=6, font=("Courier", 8),
                                             bg="white", fg="#2c3e50", state=tk.DISABLED)
        self.log.pack(fill=tk.X)

    # ── folder scanning ───────────────────────────────────────────────────────

    def add_folder(self):
        folder = filedialog.askdirectory(title="Select folder to scan", parent=self.win)
        if not folder:
            return
        added = 0
        existing_paths = {item["path"] for item in self.queue}
        for mp4 in sorted(Path(folder).rglob("*.mp4")):
            path = str(mp4.resolve())
            if path in existing_paths:
                continue
            try:
                stat = mp4.stat()
                size, mtime = stat.st_size, stat.st_mtime
            except OSError:
                continue
            submitted = self._is_submitted(path, size, mtime)
            status = "skipped" if submitted else "pending"
            size_str = self._fmt_size(size)
            iid = self.tree.insert("", tk.END,
                                   values=(path, size_str, status),
                                   tags=(status,))
            self.queue.append({"path": path, "size": size, "mtime": mtime, "iid": iid})
            existing_paths.add(path)
            added += 1
        self._log(f"Added {added} file(s) from {folder}")

    def clear_done(self):
        remaining = []
        for item in self.queue:
            status = self.tree.set(item["iid"], "status")
            if status in ("done", "skipped", "failed"):
                self.tree.delete(item["iid"])
            else:
                remaining.append(item)
        removed = len(self.queue) - len(remaining)
        self.queue = remaining
        self._log(f"Cleared {removed} entries.")

    # ── server sync ───────────────────────────────────────────────────────────

    def sync_from_server(self):
        server_url = get_config_value("server_url")
        api_key    = get_config_value("api_key")
        if not server_url or not api_key:
            messagebox.showerror("Error", "Configure server URL and API key in Settings first.",
                                 parent=self.win)
            return
        try:
            resp = requests.get(
                f"{server_url}/api/my_submissions",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=15,
            )
            if resp.status_code != 200:
                messagebox.showerror("Error",
                                     f"Server returned HTTP {resp.status_code}",
                                     parent=self.win)
                return
            server_filenames = set(resp.json().get("filenames", []))
            marked = 0
            for item in self.queue:
                basename = Path(item["path"]).name
                if basename in server_filenames:
                    current = self.tree.set(item["iid"], "status")
                    if current == "pending":
                        self.tree.item(item["iid"], values=(item["path"],
                                                             self._fmt_size(item["size"]),
                                                             "skipped"),
                                       tags=("skipped",))
                        self._mark_submitted(item["path"], item["size"],
                                             item["mtime"], recording_id=None)
                        marked += 1
            self._log(f"Synced from server: {len(server_filenames)} submissions found, "
                      f"{marked} queue item(s) marked skipped.")
        except Exception as e:
            messagebox.showerror("Error", f"Sync failed: {e}", parent=self.win)

    # ── processing loop ───────────────────────────────────────────────────────

    def start_processing(self):
        pending = [i for i in self.queue
                   if self.tree.set(i["iid"], "status") == "pending"]
        if not pending:
            messagebox.showinfo("Batch", "No pending files to process.", parent=self.win)
            return
        if self.submit_var.get():
            if not get_config_value("server_url") or not get_config_value("api_key"):
                messagebox.showerror("Error",
                                     "Configure server URL and API key in Settings first.",
                                     parent=self.win)
                return
        self.stop_flag = False
        self.is_running = True
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        thread = threading.Thread(
            target=self._processing_loop, args=(pending,), daemon=True
        )
        thread.start()

    def stop_processing(self):
        self.stop_flag = True
        self._log("Stop requested — finishing current file...")

    def _processing_loop(self, pending):
        total = len(pending)
        analyse_path = get_config_value("analyse_path", "analyse.py")
        if not os.path.isabs(analyse_path):
            analyse_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), analyse_path
            )
        base_dir = os.path.dirname(analyse_path)
        submit    = self.submit_var.get()
        annotated = self.annotated_var.get()
        server_url = get_config_value("server_url")
        api_key    = get_config_value("api_key")

        for idx, item in enumerate(pending):
            if self.stop_flag:
                self._log("Stopped.")
                break

            path = item["path"]
            filename = Path(path).name
            self._set_status(item["iid"], "processing")
            self._update_progress(idx, total, f"({idx+1}/{total}) {filename}")
            self._log(f"\n--- {filename} ---")

            # build command
            results_path = os.path.join(base_dir, "results.json")
            cmd = [sys.executable, analyse_path,
                   "--input", path,
                   "--no-show",
                   "--output-json", results_path]
            if annotated:
                stem = Path(path).stem
                out_video = os.path.join(base_dir, f"{stem}_annotated.mp4")
                cmd += ["--output", out_video]

            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace",
                    bufsize=1, cwd=base_dir,
                )
                for line in proc.stdout:
                    self._log(line.rstrip())
                proc.wait()

                if proc.returncode != 0:
                    self._log(f"Analysis failed (exit {proc.returncode})")
                    self._set_status(item["iid"], "failed")
                    continue

                if not os.path.exists(results_path):
                    self._log("results.json not found after analysis")
                    self._set_status(item["iid"], "failed")
                    continue

                with open(results_path, "r", encoding="utf-8") as f:
                    results = json.load(f)

                vehicle_count = len(results.get("vehicles", []))

                if submit:
                    results["filename"] = filename
                    location = get_config_value("last_location", "")
                    if location:
                        results["location_name"] = location
                    results["camera_name"] = "Desktop App"
                    resp = requests.post(
                        f"{server_url}/api/submit_results",
                        headers={"Authorization": f"Bearer {api_key}",
                                 "Content-Type": "application/json"},
                        json=results, timeout=60,
                    )
                    if resp.status_code == 201:
                        recording_id = resp.json().get("recording_id")
                        self._log(f"Submitted OK — recording #{recording_id}, "
                                  f"{vehicle_count} vehicle(s)")
                        self._mark_submitted(path, item["size"], item["mtime"],
                                             recording_id)
                    else:
                        err = resp.text
                        try:
                            err = resp.json().get("error", err)
                        except Exception:
                            pass
                        self._log(f"Submission failed HTTP {resp.status_code}: {err}")
                        self._set_status(item["iid"], "failed")
                        continue
                else:
                    self._log(f"Analysis complete — {vehicle_count} vehicle(s) (not submitted)")
                    self._mark_submitted(path, item["size"], item["mtime"])

                self._set_status(item["iid"], "done")

            except Exception as e:
                self._log(f"Error: {e}")
                self._set_status(item["iid"], "failed")

        self._update_progress(total, total, "Done")
        self.win.after(0, lambda: self.start_btn.config(state=tk.NORMAL))
        self.win.after(0, lambda: self.stop_btn.config(state=tk.DISABLED))
        self.is_running = False

    # ── helpers ───────────────────────────────────────────────────────────────

    def _set_status(self, iid, status):
        def _do():
            vals = list(self.tree.item(iid, "values"))
            vals[2] = status
            self.tree.item(iid, values=vals, tags=(status,))
            self.tree.see(iid)
        self.win.after(0, _do)

    def _update_progress(self, done, total, label=""):
        pct = (done / total * 100) if total else 0
        def _do():
            self.progress_var.set(pct)
            self.progress_label.config(text=label)
        self.win.after(0, _do)

    def _log(self, msg):
        def _do():
            self.log.config(state=tk.NORMAL)
            self.log.insert(tk.END, msg + "\n")
            self.log.see(tk.END)
            self.log.config(state=tk.DISABLED)
        self.win.after(0, _do)

    @staticmethod
    def _fmt_size(n):
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.0f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"


# ─── Zone Editor ──────────────────────────────────────────────────────────────

class ZoneEditorWindow:
    """
    Interactive zone polygon editor embedded in tkinter.
    Opens a video or image, lets the user draw and manage zone polygons,
    and saves to zones.json in the project directory.
    """

    def __init__(self, parent):
        self.parent = parent
        self.win = tk.Toplevel(parent)
        self.win.title("Zone Editor")
        self.win.geometry("1150x700")
        self.win.minsize(800, 500)

        self.zones        = []
        self.active_idx   = None
        self.hover_pt_idx = None
        self.base_frame   = None   # numpy BGR array
        self.cap          = None   # cv2.VideoCapture (video mode)
        self.total_frames = 0
        self.cur_pos      = 0

        # Canvas display geometry (recalculated on every render)
        self.display_w = 1
        self.display_h = 1
        self.offset_x  = 0
        self.offset_y  = 0
        self._photo    = None   # keep PhotoImage reference alive

        self._load_zones()
        self._build_ui()
        self.win.after(100, self._prompt_open_file)   # open file picker after window appears

    # ── Persistence ──────────────────────────────────────────────────────────

    def _load_zones(self):
        if ZONES_PATH.exists():
            try:
                data = json.loads(ZONES_PATH.read_text(encoding="utf-8"))
                self.zones = data.get("zones", [])
                for z in self.zones:
                    z["polygon"] = [list(p) for p in z["polygon"]]
            except Exception:
                self.zones = []
        self.active_idx = 0 if self.zones else None

    def _save(self):
        for z in self.zones:
            if len(z["polygon"]) < 3:
                messagebox.showerror(
                    "Cannot save",
                    f"Zone '{z['name']}' needs at least 3 points.",
                    parent=self.win,
                )
                return
        try:
            ZONES_PATH.write_text(
                json.dumps({"zones": self.zones}, indent=2), encoding="utf-8"
            )
            messagebox.showinfo(
                "Saved",
                f"Saved {len(self.zones)} zone(s) to zones.json",
                parent=self.win,
            )
            self.win.destroy()
        except Exception as e:
            messagebox.showerror("Error", f"Could not save: {e}", parent=self.win)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self.win.columnconfigure(0, weight=1)
        self.win.columnconfigure(1, minsize=270, weight=0)
        self.win.rowconfigure(1, weight=1)

        # Toolbar
        tb = tk.Frame(self.win, bg="#2c3e50", height=42)
        tb.grid(row=0, column=0, columnspan=2, sticky="ew")
        tb.pack_propagate(False)

        btn_s = dict(bg="#3d5a80", fg="white", relief=tk.FLAT, padx=10, pady=5, font=("Arial", 9))
        tk.Button(tb, text="📂 Open File", command=self._prompt_open_file, **btn_s).pack(side=tk.LEFT, padx=6, pady=5)

        self.prev_btn = tk.Button(tb, text="◀ Prev",   command=self._prev_frame, state=tk.DISABLED, **btn_s)
        self.next_btn = tk.Button(tb, text="Next ▶",   command=self._next_frame, state=tk.DISABLED, **btn_s)
        self.skip_btn = tk.Button(tb, text="Skip 5s ⏩", command=self._skip_5s,  state=tk.DISABLED, **btn_s)
        for b in (self.prev_btn, self.next_btn, self.skip_btn):
            b.pack(side=tk.LEFT, padx=2, pady=5)

        self.frame_lbl = tk.Label(tb, text="", bg="#2c3e50", fg="#aaaaaa", font=("Arial", 9))
        self.frame_lbl.pack(side=tk.LEFT, padx=10)

        # Canvas
        cf = tk.Frame(self.win, bg="#111")
        cf.grid(row=1, column=0, sticky="nsew")
        self.canvas = tk.Canvas(cf, bg="#111", cursor="crosshair", highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.canvas.bind("<Button-1>",  self._on_click)
        self.canvas.bind("<Motion>",    self._on_motion)
        self.canvas.bind("<Configure>", lambda _e: self._render())

        # Right panel
        rp = tk.Frame(self.win, bg="#ecf0f1", width=270)
        rp.grid(row=1, column=1, sticky="nsew")
        rp.pack_propagate(False)

        tk.Label(rp, text="Zones", font=("Arial", 12, "bold"), bg="#ecf0f1").pack(pady=(14, 4))

        lb_frame = tk.Frame(rp, bg="#ecf0f1")
        lb_frame.pack(fill=tk.X, padx=10)
        self.listbox = tk.Listbox(
            lb_frame, font=("Courier", 9), height=9,
            selectmode=tk.SINGLE, activestyle="dotbox", exportselection=False,
        )
        self.listbox.pack(fill=tk.X)
        self.listbox.bind("<<ListboxSelect>>", self._on_list_select)

        pb = dict(font=("Arial", 9), relief=tk.FLAT, padx=8, pady=5)
        btn_area = tk.Frame(rp, bg="#ecf0f1")
        btn_area.pack(fill=tk.X, padx=10, pady=6)
        tk.Button(btn_area, text="+ Add Zone",     bg="#27ae60", fg="white", command=self._add_zone,    **pb).pack(fill=tk.X, pady=2)
        tk.Button(btn_area, text="🗑 Delete Zone", bg="#e74c3c", fg="white", command=self._delete_zone, **pb).pack(fill=tk.X, pady=2)
        tk.Button(btn_area, text="✕ Clear Points", bg="#e67e22", fg="white", command=self._clear_zone,  **pb).pack(fill=tk.X, pady=2)

        tk.Label(rp, text="Properties", font=("Arial", 10, "bold"), bg="#ecf0f1").pack(pady=(10, 2))
        self.props_lbl = tk.Label(
            rp, text="No zone selected", font=("Arial", 9), bg="#ecf0f1",
            fg="#7f8c8d", justify=tk.LEFT, wraplength=240,
        )
        self.props_lbl.pack(padx=12, anchor="w")

        tk.Label(
            rp,
            text="\nClick canvas to add points\nClick a point to delete it\nSelect zone in list to switch",
            font=("Arial", 9), bg="#ecf0f1", fg="#95a5a6", justify=tk.LEFT,
        ).pack(padx=12, anchor="w")

        # Save / Cancel at bottom
        bot = tk.Frame(rp, bg="#ecf0f1")
        bot.pack(side=tk.BOTTOM, fill=tk.X, padx=10, pady=12)
        tk.Button(bot, text="💾 Save & Close", bg="#2980b9", fg="white", command=self._save, **pb).pack(fill=tk.X, pady=2)
        tk.Button(bot, text="Cancel",          bg="#95a5a6", fg="white", command=self.win.destroy, **pb).pack(fill=tk.X, pady=2)

        self._refresh_list()

    # ── File loading ──────────────────────────────────────────────────────────

    def _prompt_open_file(self):
        path = filedialog.askopenfilename(
            parent=self.win,
            title="Open a video or image for zone calibration",
            filetypes=[
                ("Video / Image", "*.mp4 *.avi *.mov *.jpg *.jpeg *.png *.bmp"),
                ("All Files", "*.*"),
            ],
        )
        if not path:
            return
        if Path(path).suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp"):
            self._load_image(path)
        else:
            self._load_video(path)

    def _normalize_zones(self, fw, fh):
        """
        Convert zones stored in pixel coordinates (legacy format from tune_zones.py)
        to fractional (0-1) coordinates used by this editor.
        Detects pixel format by checking if any coordinate value exceeds 1.
        """
        for z in self.zones:
            if not z["polygon"]:
                continue
            if any(abs(v) > 1 for pt in z["polygon"] for v in pt):
                z["polygon"] = [
                    [round(x / fw, 4), round(y / fh, 4)]
                    for x, y in z["polygon"]
                ]

    def _load_image(self, path):
        import cv2
        frame = cv2.imread(path)
        if frame is None:
            messagebox.showerror("Error", f"Could not read image:\n{path}", parent=self.win)
            return
        if self.cap:
            self.cap.release()
            self.cap = None
        self.base_frame = frame
        fh, fw = frame.shape[:2]
        self._normalize_zones(fw, fh)
        self._set_nav(False)
        self._render()

    def _load_video(self, path):
        import cv2
        if self.cap:
            self.cap.release()
        cap = cv2.VideoCapture(path)
        if not cap.isOpened():
            messagebox.showerror("Error", f"Could not open video:\n{path}", parent=self.win)
            return
        self.cap = cap
        fps = cap.get(cv2.CAP_PROP_FPS) or 25
        self.total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        # Start 10s in for a representative frame
        cap.set(cv2.CAP_PROP_POS_FRAMES, min(int(fps * 10), self.total_frames - 1))
        ret, frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ret, frame = cap.read()
        if not ret:
            messagebox.showerror("Error", "Could not read frame from video.", parent=self.win)
            return
        self.base_frame = frame
        fh, fw = frame.shape[:2]
        self._normalize_zones(fw, fh)
        self.cur_pos = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
        self._set_nav(True)
        self._render()

    def _set_nav(self, enabled):
        state = tk.NORMAL if enabled else tk.DISABLED
        for b in (self.prev_btn, self.next_btn, self.skip_btn):
            b.config(state=state)

    # ── Frame navigation ──────────────────────────────────────────────────────

    def _prev_frame(self):
        if not self.cap:
            return
        import cv2
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, max(0, self.cur_pos - 2))
        ret, frame = self.cap.read()
        if ret:
            self.base_frame = frame
            self.cur_pos = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES))
            self._render()

    def _next_frame(self):
        if not self.cap:
            return
        ret, frame = self.cap.read()
        if ret:
            self.base_frame = frame
            self.cur_pos = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES))
            self._render()

    def _skip_5s(self):
        if not self.cap:
            return
        import cv2
        fps = self.cap.get(cv2.CAP_PROP_FPS) or 25
        new_pos = min(self.cur_pos + int(fps * 5), self.total_frames - 1)
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, new_pos)
        ret, frame = self.cap.read()
        if ret:
            self.base_frame = frame
            self.cur_pos = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES))
            self._render()

    # ── Coordinate helpers ────────────────────────────────────────────────────

    def _to_canvas(self, fx, fy):
        """Fractional frame coords → canvas pixel coords."""
        return fx * self.display_w + self.offset_x, fy * self.display_h + self.offset_y

    def _nearest_point(self, cx, cy):
        """Return (pt_index, distance_px) of nearest polygon point in active zone."""
        if self.active_idx is None or not self.zones:
            return None, float("inf")
        best_i, best_d = None, float("inf")
        for i, (fx, fy) in enumerate(self.zones[self.active_idx]["polygon"]):
            px, py = self._to_canvas(fx, fy)
            d = ((cx - px) ** 2 + (cy - py) ** 2) ** 0.5
            if d < best_d:
                best_d, best_i = d, i
        return best_i, best_d

    # ── Canvas interaction ────────────────────────────────────────────────────

    def _on_click(self, event):
        if self.base_frame is None:
            return
        if self.active_idx is None:
            messagebox.showinfo(
                "No zone selected",
                "Click '+ Add Zone' to create a zone first.",
                parent=self.win,
            )
            return
        ni, nd = self._nearest_point(event.x, event.y)
        polygon = self.zones[self.active_idx]["polygon"]
        if ni is not None and nd <= SNAP_RADIUS:
            polygon.pop(ni)
            self.hover_pt_idx = None
        else:
            fx = max(0.0, min(1.0, round((event.x - self.offset_x) / self.display_w, 4)))
            fy = max(0.0, min(1.0, round((event.y - self.offset_y) / self.display_h, 4)))
            polygon.append([fx, fy])
        self._refresh_list()
        self._render()

    def _on_motion(self, event):
        if self.active_idx is None:
            return
        ni, nd = self._nearest_point(event.x, event.y)
        new_hover = ni if (ni is not None and nd <= SNAP_RADIUS) else None
        if new_hover != self.hover_pt_idx:
            self.hover_pt_idx = new_hover
            self._render()

    # ── Rendering ─────────────────────────────────────────────────────────────

    def _zone_colour(self, idx, active):
        r, g, b = PALETTE_RGB[idx % len(PALETTE_RGB)]
        if not active:
            r, g, b = r // 2, g // 2, b // 2
        return f"#{r:02x}{g:02x}{b:02x}"

    def _render(self):
        try:
            from PIL import Image, ImageTk
        except ImportError:
            self.canvas.delete("all")
            self.canvas.create_text(
                self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                text="Pillow not installed.\nRun: pip install Pillow",
                fill="#ff6666", font=("Arial", 13),
            )
            return

        import cv2

        cw = max(self.canvas.winfo_width(),  1)
        ch = max(self.canvas.winfo_height(), 1)
        self.canvas.delete("all")

        if self.base_frame is None:
            self.canvas.create_text(
                cw // 2, ch // 2,
                text="Open a video or image to begin",
                fill="#666", font=("Arial", 14),
            )
            return

        # Scale frame to fit canvas, preserving aspect ratio
        fh, fw = self.base_frame.shape[:2]
        scale = min(cw / fw, ch / fh)
        self.display_w = max(int(fw * scale), 1)
        self.display_h = max(int(fh * scale), 1)
        self.offset_x  = (cw - self.display_w) // 2
        self.offset_y  = (ch - self.display_h) // 2

        # Draw frame
        rgb = cv2.cvtColor(self.base_frame, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb).resize((self.display_w, self.display_h), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(pil)
        self.canvas.create_image(self.offset_x, self.offset_y, anchor="nw", image=self._photo)

        # Draw zones (inactive first, then active on top)
        order = [i for i in range(len(self.zones)) if i != self.active_idx]
        if self.active_idx is not None:
            order.append(self.active_idx)

        for i in order:
            zone     = self.zones[i]
            is_active = (i == self.active_idx)
            col      = self._zone_colour(i, is_active)
            pts      = zone["polygon"]
            lw       = 2 if is_active else 1

            # Filled polygon (stipple for semi-transparency)
            if len(pts) >= 3:
                coords = []
                for fx, fy in pts:
                    px, py = self._to_canvas(fx, fy)
                    coords += [px, py]
                self.canvas.create_polygon(
                    coords, fill=col, outline=col,
                    stipple="gray25" if not is_active else "gray50",
                    width=lw,
                )
                self.canvas.create_polygon(coords, fill="", outline=col, width=lw)

            # Edges (dashed while < 3 points)
            if len(pts) >= 2:
                dash = (5, 4) if len(pts) < 3 else ()
                for j in range(len(pts)):
                    x1, y1 = self._to_canvas(*pts[j])
                    x2, y2 = self._to_canvas(*pts[(j + 1) % len(pts)])
                    if len(pts) >= 3 or j < len(pts) - 1:
                        self.canvas.create_line(x1, y1, x2, y2, fill=col, width=lw, dash=dash)

            # Points
            for j, (fx, fy) in enumerate(pts):
                px, py  = self._to_canvas(fx, fy)
                r       = 7 if is_active else 4
                is_hov  = is_active and j == self.hover_pt_idx
                fill    = "#ff3333" if is_hov else col
                outline = "white"   if is_hov else col
                self.canvas.create_oval(px - r, py - r, px + r, py + r,
                                        fill=fill, outline=outline, width=2)

            # Zone label at centroid
            if pts:
                lx = sum(self._to_canvas(fx, fy)[0] for fx, fy in pts) / len(pts)
                ly = sum(self._to_canvas(fx, fy)[1] for fx, fy in pts) / len(pts)
                marker = "▶ " if is_active else ""
                txt = f"{marker}{zone['name']}"
                self.canvas.create_text(lx + 1, ly + 1, text=txt, fill="black", font=("Arial", 9, "bold"))
                self.canvas.create_text(lx,     ly,     text=txt, fill=col,     font=("Arial", 9, "bold"))

        if self.cap:
            self.frame_lbl.config(text=f"Frame {self.cur_pos} / {self.total_frames}")

    # ── Zone list management ──────────────────────────────────────────────────

    def _refresh_list(self):
        self.listbox.delete(0, tk.END)
        for i, z in enumerate(self.zones):
            marker = "▶" if i == self.active_idx else " "
            label  = f"{marker} [{i+1}] {z['name']} ({z['type']})  {len(z['polygon'])}pts"
            self.listbox.insert(tk.END, label)
            self.listbox.itemconfig(i, fg=self._zone_colour(i, True))
        if self.active_idx is not None and self.zones:
            self.listbox.selection_set(self.active_idx)
            self.listbox.see(self.active_idx)
        self._update_props()

    def _update_props(self):
        if self.active_idx is None or not self.zones:
            self.props_lbl.config(text="No zone selected")
            return
        z = self.zones[self.active_idx]
        lines = [f"Name:   {z['name']}", f"Type:   {z['type']}"]
        if z["type"] == "side_on":
            lines += [
                f"PPM L:  {z.get('ppm_left',  44.0)}",
                f"PPM R:  {z.get('ppm_right', 33.0)}",
            ]
        lines.append(f"Points: {len(z['polygon'])}")
        self.props_lbl.config(text="\n".join(lines))

    def _on_list_select(self, _event):
        sel = self.listbox.curselection()
        if sel:
            self.active_idx   = sel[0]
            self.hover_pt_idx = None
            self._refresh_list()
            self._render()

    def _add_zone(self):
        existing = {z["name"] for z in self.zones}
        dlg = _ZoneDialog(self.win, existing)
        self.win.wait_window(dlg.win)
        if dlg.result:
            self.zones.append(dlg.result)
            self.active_idx   = len(self.zones) - 1
            self.hover_pt_idx = None
            self._refresh_list()
            self._render()

    def _delete_zone(self):
        if self.active_idx is None or not self.zones:
            return
        name = self.zones[self.active_idx]["name"]
        if not messagebox.askyesno("Delete zone", f"Delete zone '{name}'?", parent=self.win):
            return
        self.zones.pop(self.active_idx)
        self.active_idx   = min(self.active_idx, len(self.zones) - 1) if self.zones else None
        self.hover_pt_idx = None
        self._refresh_list()
        self._render()

    def _clear_zone(self):
        if self.active_idx is None or not self.zones:
            return
        self.zones[self.active_idx]["polygon"] = []
        self.hover_pt_idx = None
        self._refresh_list()
        self._render()


# ─── New Zone Dialog ───────────────────────────────────────────────────────────

class _ZoneDialog:
    """Modal dialog for creating a new zone (name, type, PPM values)."""

    def __init__(self, parent, existing_names):
        self.result = None

        self.win = tk.Toplevel(parent)
        self.win.title("New Zone")
        self.win.geometry("360x310")
        self.win.resizable(False, False)
        self.win.transient(parent)
        self.win.grab_set()

        pad = dict(padx=16, pady=4)

        tk.Label(self.win, text="Zone Name:", font=("Arial", 10, "bold")).pack(anchor="w", **pad)
        self.name_var = tk.StringVar()
        tk.Entry(self.win, textvariable=self.name_var, font=("Arial", 10)).pack(fill="x", padx=16)

        tk.Label(self.win, text="Zone Type:", font=("Arial", 10, "bold")).pack(anchor="w", **pad)
        self.type_var = tk.StringVar(value="side_on")
        type_row = tk.Frame(self.win)
        type_row.pack(anchor="w", padx=16)
        for t in ZONE_TYPES:
            tk.Radiobutton(
                type_row, text=t, variable=self.type_var, value=t,
                command=self._on_type_change,
            ).pack(side="left", padx=6)

        # PPM fields (shown for side_on only)
        self.ppm_frame = tk.Frame(self.win)
        self.ppm_frame.pack(fill="x", padx=16, pady=6)
        tk.Label(self.ppm_frame, text="PPM Left  (pixels per metre — left-bound lane):",
                 font=("Arial", 9)).pack(anchor="w")
        self.ppm_l = tk.StringVar(value="44.0")
        tk.Entry(self.ppm_frame, textvariable=self.ppm_l, font=("Arial", 10)).pack(fill="x")
        tk.Label(self.ppm_frame, text="PPM Right (pixels per metre — right-bound lane):",
                 font=("Arial", 9)).pack(anchor="w", pady=(6, 0))
        self.ppm_r = tk.StringVar(value="33.0")
        tk.Entry(self.ppm_frame, textvariable=self.ppm_r, font=("Arial", 10)).pack(fill="x")

        btns = tk.Frame(self.win)
        btns.pack(pady=14)
        tk.Button(
            btns, text="Create Zone", bg="#27ae60", fg="white",
            font=("Arial", 10), relief="flat", padx=16, pady=6,
            command=lambda: self._ok(existing_names),
        ).pack(side="left", padx=6)
        tk.Button(
            btns, text="Cancel", bg="#95a5a6", fg="white",
            font=("Arial", 10), relief="flat", padx=16, pady=6,
            command=self.win.destroy,
        ).pack(side="left", padx=6)

    def _on_type_change(self):
        if self.type_var.get() == "side_on":
            self.ppm_frame.pack(fill="x", padx=16, pady=6)
        else:
            self.ppm_frame.pack_forget()

    def _ok(self, existing_names):
        name = self.name_var.get().strip()
        if not name:
            messagebox.showerror("Error", "Zone name is required.", parent=self.win)
            return
        if name in existing_names:
            messagebox.showerror("Error", f"'{name}' already exists.", parent=self.win)
            return
        zone = {"name": name, "type": self.type_var.get(), "polygon": []}
        if self.type_var.get() == "side_on":
            try:
                zone["ppm_left"]  = float(self.ppm_l.get())
                zone["ppm_right"] = float(self.ppm_r.get())
            except ValueError:
                messagebox.showerror("Error", "PPM values must be numbers.", parent=self.win)
                return
        self.result = zone
        self.win.destroy()


# ─── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    app = TrafficAnalyzerApp(root)
    root.mainloop()
