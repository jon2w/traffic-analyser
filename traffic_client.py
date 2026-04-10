#!/usr/bin/env python3
"""
traffic_client.py — Example client for submitting analysis results to the Traffic Analyser.

This demonstrates how to use the remote submission API to upload analysis results
from local video files (same workflow as worker.py, but for users submitting their own files).

Typical workflow:
    1. Record a video locally on your camera or device
    2. Run the traffic analyser scripts locally: python analyse.py --input video.mp4
    3. Submit the results here with the vehicles data

Usage:
    # Submit results from a local analysis
    python traffic_client.py --server https://your-domain.com \
        --api-key abc123... \
        --submit results.json \
        --location "Downtown Intersection"

    # Check your submitted jobs
    python traffic_client.py --server https://your-domain.com \
        --api-key abc123... \
        --list-jobs

    # Get results for a recording
    python traffic_client.py --server https://your-domain.com \
        --api-key abc123... \
        --results 42

The results.json format should match what analyse.py produces (see example below).
"""

import argparse
import json
import os
import requests
import sys
from datetime import datetime


class TrafficClient:
    def __init__(self, server_url, api_key):
        self.server = server_url.rstrip("/")
        self.api_key = api_key
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
    
    def submit_results(self, results_json_path, location_name=None, filename=None):
        """
        Submit analysis results from a local analysis.
        
        Args:
            results_json_path: Path to JSON file with results
            location_name: Optional friendly name for the location
            filename: Optional original video filename for reference
        
        Returns:
            dict with recording_id, vehicle_count, etc.
        """
        if not os.path.isfile(results_json_path):
            raise FileNotFoundError(f"File not found: {results_json_path}")
        
        # Load and validate results
        with open(results_json_path, 'r') as f:
            results = json.load(f)
        
        # Add location if provided
        if location_name:
            results["location_name"] = location_name
        
        # Add original filename if provided
        if filename:
            results["filename"] = filename
        
        try:
            resp = requests.post(
                f"{self.server}/api/submit_results",
                headers=self.headers,
                json=results,
                timeout=60
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.RequestException as e:
            print(f"Error: {e}")
            if hasattr(e, 'response') and e.response is not None:
                print(f"Response: {e.response.text}")
            raise
    
    def list_jobs(self, status=None, limit=50):
        """
        List user's submitted jobs.
        
        Args:
            status: Optional filter by 'pending', 'processing', 'done', 'failed'
            limit: Max results
        
        Returns:
            List of job dicts
        """
        params = {"limit": limit}
        if status:
            params["status"] = status
        
        resp = requests.get(
            f"{self.server}/api/user/jobs",
            headers=self.headers,
            params=params,
            timeout=30
        )
        resp.raise_for_status()
        return resp.json().get("jobs", [])
    
    def get_results(self, recording_id):
        """
        Get vehicle detection results for a recording.
        
        Args:
            recording_id: Recording ID from submit_results response
        
        Returns:
            dict with recording info and vehicle list
        """
        resp = requests.get(
            f"{self.server}/api/user/results/{recording_id}",
            headers=self.headers,
            timeout=30
        )
        resp.raise_for_status()
        return resp.json()


def main():
    parser = argparse.ArgumentParser(
        description="Remote submission client for Traffic Analyser",
        epilog="""
Example workflow:
  1. Record video locally or use existing file
  2. Run analysis: python analyse.py --input video.mp4 --no-show
     (saves results.json in current directory)
  3. Submit: python traffic_client.py --server https://your-domain.com \\
              --api-key YOUR_KEY --submit results.json --location "Main Street"
  4. Check: python traffic_client.py --server https://your-domain.com \\
             --api-key YOUR_KEY --results 42
        """
    )
    parser.add_argument("--server", required=True,
                       help="Server URL (e.g., https://your-domain.com)")
    parser.add_argument("--api-key", required=True,
                       help="Your API key")
    parser.add_argument("--submit",
                       help="Path to results.json file to submit")
    parser.add_argument("--location",
                       help="Location name for submission (optional)")
    parser.add_argument("--filename",
                       help="Original video filename for reference (optional)")
    parser.add_argument("--list-jobs", action="store_true",
                       help="List user's submitted jobs")
    parser.add_argument("--list-status",
                       help="Filter job list by status (pending/processing/done/failed)")
    parser.add_argument("--results", type=int,
                       help="Get results for a recording ID")
    
    args = parser.parse_args()
    
    client = TrafficClient(args.server, args.api_key)
    
    try:
        if args.submit:
            print(f"Submitting results from {args.submit}...")
            result = client.submit_results(
                args.submit,
                location_name=args.location,
                filename=args.filename
            )
            print(json.dumps(result, indent=2))
            
            recording_id = result.get("recording_id")
            if recording_id:
                print(f"\nRecording ID: {recording_id}")
                print(f"Vehicles detected: {result.get('vehicle_count', 0)}")
        
        elif args.list_jobs:
            jobs = client.list_jobs(status=args.list_status)
            print(f"Your jobs ({len(jobs)} total):")
            print()
            for job in jobs:
                status = job.get("job_status", "unknown").upper()
                location = job.get("location_name", "Unknown")
                vehicles = job.get("vehicle_count", 0)
                recorded = job.get("recorded_at", "Unknown")
                print(f"  ID {job['id']:5d}  [{status:10s}]  {location:30s}  "
                      f"{vehicles:3d} vehicles  {recorded}")
        
        elif args.results:
            data = client.get_results(args.results)
            print(json.dumps(data, indent=2, default=str))
        
        else:
            parser.print_help()
    
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
