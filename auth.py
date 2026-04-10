"""
auth.py — Authentication system for remote submissions.

Provides simple API-key based authentication for remote users.
Admins have full access; regular users can only view/submit their own data.

Usage:
    from auth import require_auth, get_current_user
    
    @app.route("/api/protected", methods=["POST"])
    @require_auth
    def protected_endpoint(user):
        # user is a dict with keys: id, username, is_admin
        pass
        
    # For optional auth (public endpoint with optional filtering):
    @app.route("/api/public")
    def public_endpoint():
        user = get_current_user()  # returns None if no valid API key
        ...
"""

from functools import wraps
from flask import request, jsonify
import database as db


def get_current_user():
    """
    Extract and validate API key from request headers.
    Returns user dict or None if invalid/missing.
    
    Expected header: Authorization: Bearer <api_key>
    or query param: ?api_key=<api_key>
    """
    api_key = None
    
    # Check Authorization header
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        api_key = auth_header[7:]
    
    # Check query parameter
    if not api_key:
        api_key = request.args.get("api_key")
    
    # Check request body (for POST/PUT)
    if not api_key and request.method in ("POST", "PUT"):
        try:
            data = request.get_json() or {}
            api_key = data.get("api_key")
        except:
            pass
    
    if not api_key:
        return None
    
    user = db.validate_api_key(api_key)
    return user


def require_auth(f):
    """
    Decorator for endpoints that require authentication.
    Passes the user dict as the first argument after self/cls.
    
    Example:
        @app.route("/api/protected")
        @require_auth
        def protected(user):
            return jsonify({"username": user["username"]})
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"error": "Unauthorized — valid API key required"}), 401
        return f(user, *args, **kwargs)
    return decorated


def require_admin(f):
    """
    Decorator for endpoints that require admin privileges.
    Only users with is_admin=True can access.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({"error": "Unauthorized — valid API key required"}), 401
        if not user["is_admin"]:
            return jsonify({"error": "Forbidden — admin access required"}), 403
        return f(user, *args, **kwargs)
    return decorated


def require_auth_or_public(allow_public=True):
    """
    Decorator factory for endpoints that are public but show different data
    based on authentication status.
    
    When decorated, user is passed as first argument (may be None).
    
    Example:
        @app.route("/api/results")
        @require_auth_or_public(allow_public=True)
        def results(user):
            if user:
                return get_user_results(user["id"])
            else:
                return get_public_results()
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            user = get_current_user()
            return f(user, *args, **kwargs)
        return decorated
    return decorator


def get_recording_owner(recording_id):
    """
    Get the user_id for a recording. Returns (user_id, username) or (None, None).
    """
    with db.get_connection() as conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT u.id, u.username
            FROM recordings r
            LEFT JOIN users u ON r.user_id = u.id
            WHERE r.id = %s
        """, (recording_id,))
        row = cursor.fetchone()
        if row:
            return row["id"], row["username"]
        return None, None


def user_can_access_recording(user, recording_id):
    """
    Check if a user can access a recording.
    Admins can access all; regular users only their own.
    """
    if user["is_admin"]:
        return True
    
    owner_id, _ = get_recording_owner(recording_id)
    return owner_id == user["id"]


def user_can_access_vehicle(user, vehicle_id):
    """
    Check if a user can access vehicle data.
    Admins can access all; regular users only see their own recordings' vehicles.
    """
    if user["is_admin"]:
        return True
    
    with db.get_connection() as conn:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT r.user_id
            FROM vehicles v
            JOIN recordings r ON v.recording_id = r.id
            WHERE v.id = %s
        """, (vehicle_id,))
        row = cursor.fetchone()
        if row:
            return row["user_id"] == user["id"]
        return False
