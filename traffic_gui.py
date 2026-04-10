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
from tkinter import filedialog, messagebox, scrolledtext
import json
import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path

import requests


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
        
        # Check settings
        server_url = get_config_value("server_url")
        api_key = get_config_value("api_key")
        
        if not server_url or not api_key:
            messagebox.showerror("Error", "Please configure server URL and API key in Settings!")
            return
        
        # Save location name for next time
        location_name = self.location_var.get().strip()
        if location_name:
            set_config_value("last_location", location_name)
        
        # Run in background thread to avoid freezing UI
        thread = threading.Thread(
            target=self._process_and_submit_bg,
            args=(self.selected_file, location_name, server_url, api_key)
        )
        thread.daemon = True
        thread.start()
    
    def _process_and_submit_bg(self, filepath, location_name, server_url, api_key):
        """Background thread for processing and submission."""
        try:
            self.is_processing = True
            self.process_btn.config(state=tk.DISABLED)
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
            base_dir = os.path.dirname(analyse_path) if os.path.dirname(analyse_path) else "."
            
            cmd = [
                sys.executable,
                analyse_path,
                "--input", filepath,
                "--no-show"
            ]
            
            self.log_output(f"Running: {' '.join(cmd)}\n")
            
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
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
            
            # Step 2: Find and read results.json
            self.log_output("=" * 60)
            self.log_output("Step 2: Submitting results to server...")
            self.log_output("=" * 60 + "\n")
            
            # Look for results.json in the same directory or current directory
            possible_paths = [
                os.path.join(base_dir, "results.json"),
                "results.json",
                os.path.join(os.path.dirname(filepath), "results.json")
            ]
            
            results_path = None
            for path in possible_paths:
                if os.path.exists(path):
                    results_path = path
                    break
            
            if not results_path:
                self.log_output(f"❌ Results file not found. Looked in: {possible_paths}")
                self.status_var.set("Results file not found!")
                messagebox.showerror("Error", "Could not find results.json after analysis")
                return
            
            self.log_output(f"Found results: {results_path}\n")
            
            with open(results_path, 'r') as f:
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
                except:
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


# ─── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    root = tk.Tk()
    app = TrafficAnalyzerApp(root)
    root.mainloop()
