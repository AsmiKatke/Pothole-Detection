import os
import cv2
import time
import requests
import math
import uuid
import threading
import sqlite3
import base64
import numpy as np
from flask import Flask, render_template, Response, request, jsonify, session, redirect, url_for, send_from_directory
from flask_cors import CORS
from twilio.rest import Client

# ==========================================
# PYTORCH DEFAULTS MONKEY-PATCH (PyTorch 2.6+)
# ==========================================
try:
    import torch
    _original_torch_load = torch.load
    def safe_torch_load(*args, **kwargs):
        kwargs['weights_only'] = False
        return _original_torch_load(*args, **kwargs)
    torch.load = safe_torch_load
    print("[SYSTEM] PyTorch load monkey-patched successfully.")
except ImportError:
    print("[SYSTEM] PyTorch not available. Skipping monkey-patch.")
except Exception as e:
    print(f"[SYSTEM WARNING] PyTorch load patch failed: {e}")

try:
    from ultralytics import YOLO
    HAS_YOLO = True
except ImportError:
    HAS_YOLO = False
    print("[SYSTEM WARNING] ultralytics not installed. YOLO inference disabled.")

# ==========================================
# FLASK CONFIGURATION & CORS
# ==========================================
app = Flask(__name__)
app.secret_key = "super_secret_roadguard_key"

# Enable CORS for all routes to accept cross-origin requests from GitHub Pages
CORS(app, resources={r"/api/*": {"origins": "*"}})

# ==========================================
# TWILIO WHATSAPP CONFIGURATION
# ==========================================
import os

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "YOUR_TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "YOUR_TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = "whatsapp:+14155238886"
TARGET_PHONE_NUMBER = "whatsapp:+918169233423"

# Dict mapping target numbers to last alert timestamp to prevent notification flooding
alert_cooldowns = {}
COOLDOWN_SECONDS = 10  # 10s cooldown for rider sessions

# Global variable to track the last known Flask server host (important for background video analysis alerts)
last_known_host = "http://localhost:5000"

# ==========================================
# DATABASE INITIALIZATION (SQLite & MongoDB Fallback)
# ==========================================
DB_PATH = "roadguard.db"
HAS_MONGO = False
potholes_col = None

# 1. MongoDB Setup (Kept for legacy backend compatibility)
MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017/")
try:
    from pymongo import MongoClient
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=1000)
    mongo_client.server_info()
    db = mongo_client["roadguard_db"]
    potholes_col = db["potholes"]
    HAS_MONGO = True
    print("[DB SUCCESS] Connected successfully to MongoDB.")
except Exception as e:
    print(f"[DB WARNING] MongoDB unavailable ({e}). Running in SQLite mode.")

# 2. SQLite Auto-Initialization
def init_db():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Core schema creation
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS potholes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                latitude REAL,
                longitude REAL,
                severity TEXT,
                timestamp REAL,
                detection_count INTEGER DEFAULT 1,
                rider_id TEXT,
                image_data TEXT
            )
        ''')
        
        # Defensive check: if database exists from older versions, alter table to include rider_id and image_data
        cursor.execute("PRAGMA table_info(potholes)")
        columns = [info[1] for info in cursor.fetchall()]
        if 'rider_id' not in columns:
            cursor.execute("ALTER TABLE potholes ADD COLUMN rider_id TEXT")
            print("[DB SUCCESS] Altered potholes table: Added rider_id column.")
        if 'image_data' not in columns:
            cursor.execute("ALTER TABLE potholes ADD COLUMN image_data TEXT")
            print("[DB SUCCESS] Altered potholes table: Added image_data column.")
            
        conn.commit()
        conn.close()
        print("[DB SUCCESS] SQLite database auto-initialized successfully with rider_id and image_data support.")
    except Exception as e:
        print(f"[DB ERROR] SQLite database initialization failed: {e}")

init_db()

# ==========================================
# YOLO MODEL LOADER
# ==========================================
MODEL_PATH = "best.pt"
model = None

if HAS_YOLO:
    if os.path.exists(MODEL_PATH):
        try:
            model = YOLO(MODEL_PATH)
            print(f"[MODEL SUCCESS] Loaded custom YOLOv8 model from '{MODEL_PATH}'.")
        except Exception as e:
            print(f"[MODEL ERROR] Failed loading YOLO weights: {e}")
    else:
        print(f"[MODEL WARNING] Weights file '{MODEL_PATH}' not found in root. Inference will run in fallback mock mode.")
else:
    print("[MODEL WARNING] Ultralytics framework is missing. Running in mock inference mode.")

# ==========================================
# TELEMETRY HELPERS (HAVERSINE DISTANCE)
# ==========================================
def haversine_distance(lat1, lon1, lat2, lon2):
    """Calculate distance in meters between two GPS coordinates."""
    if None in (lat1, lon1, lat2, lon2):
        return 999.0
    R = 6371e3  # Earth radius in meters
    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi/2) * math.sin(delta_phi/2) + \
        math.cos(phi1) * math.cos(phi2) * \
        math.sin(delta_lambda/2) * math.sin(delta_lambda/2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

    return R * c  # Distance in meters

# ==========================================
# DATABASE REPORTERS & CLUSTERING
# ==========================================
def register_pothole_in_db(lat, lng, severity, rider_id="UNKNOWN_RIDER", image_data=None):
    """
    Saves a pothole coordinate to SQLite and MongoDB.
    Implements a 15-meter proximity clustering check to avoid duplicate recordings.
    """
    if lat is None or lng is None:
        return None

    pothole_id = None

    # ---- 1. SQLite Database Proximity clustering ----
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Load active potholes to check proximity
        cursor.execute("SELECT id, latitude, longitude, detection_count FROM potholes")
        potholes = cursor.fetchall()
        
        existing_id = None
        current_count = 1
        
        for row in potholes:
            db_id, db_lat, db_lng, db_count = row
            dist = haversine_distance(lat, lng, db_lat, db_lng)
            if dist <= 15.0:  # Proximity threshold: 15 meters
                existing_id = db_id
                current_count = db_count
                break

        if existing_id:
            cursor.execute(
                "UPDATE potholes SET detection_count = ?, timestamp = ?, severity = ?, rider_id = ?, image_data = ? WHERE id = ?",
                (current_count + 1, time.time(), severity, rider_id, image_data, existing_id)
            )
            pothole_id = existing_id
            print(f"[DB SQLite] Cluster hit: Incremented pothole ID {pothole_id} (Rider: {rider_id}) count to {current_count + 1}")
        else:
            cursor.execute(
                "INSERT INTO potholes (latitude, longitude, severity, timestamp, rider_id, detection_count, image_data) VALUES (?, ?, ?, ?, ?, 1, ?)",
                (lat, lng, severity, time.time(), rider_id, image_data)
            )
            pothole_id = cursor.lastrowid
            print(f"[DB SQLite] Saved new pothole ID {pothole_id} at ({lat:.5f}, {lng:.5f}) by rider: {rider_id}")

        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[DB ERROR] SQLite operation failed: {e}")

    # ---- 2. MongoDB Legacy Fallback sync ----
    if HAS_MONGO and potholes_col is not None:
        try:
            # Check proximity in MongoDB
            existing_found = False
            for doc in potholes_col.find():
                if doc.get('latitude') is not None and doc.get('longitude') is not None:
                    dist = haversine_distance(lat, lng, doc['latitude'], doc['longitude'])
                    if dist <= 15.0:
                        potholes_col.update_one(
                            {"_id": doc["_id"]},
                            {"$inc": {"detection_count": 1}, "$set": {"timestamp": time.time(), "rider_id": rider_id}}
                        )
                        existing_found = True
                        break
            
            if not existing_found:
                record = {
                    "pothole_id": str(pothole_id) if pothole_id else str(uuid.uuid4())[:8],
                    "latitude": lat,
                    "longitude": lng,
                    "severity": severity,
                    "timestamp": time.time(),
                    "rider_id": rider_id,
                    "detection_count": 1
                }
                potholes_col.insert_one(record)
                print("[DB MongoDB] Synced new pothole to MongoDB cloud cluster.")
        except Exception as m_err:
            print(f"[DB ERROR] MongoDB sync failed: {m_err}")

    return pothole_id

def get_total_potholes_count():
    """Returns the total number of unique potholes mapped in the system."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM potholes")
        count = cursor.fetchone()[0]
        conn.close()
        return count
    except Exception as e:
        print(f"[DB ERROR] Failed to count database entries: {e}")
        return 0

# ==========================================
# TWILIO WHATSAPP INTEGRATION UPLINK
# ==========================================
def send_whatsapp_alert(latitude, longitude, severity, depth_score, media_url, override_to=None):
    """
    Sends a formatted WhatsApp message containing severity, simulated depth score,
    Google Maps URL, and attaches the processed image frame with bounding box overlay.
    Uses the Twilio Sandbox API for WhatsApp.
    """
    if not TWILIO_ACCOUNT_SID or TWILIO_ACCOUNT_SID.startswith("YOUR_"):
        print("[TWILIO] Account SID not configured. Skipping WhatsApp alert dispatch.")
        return False

    to_number = override_to if override_to else TARGET_PHONE_NUMBER
    # Safeguard formatting of to_number: ensure it has the "whatsapp:" prefix
    if to_number and not to_number.startswith("whatsapp:"):
        to_number = f"whatsapp:{to_number}"

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        maps_link = f"https://www.google.com/maps?q={latitude},{longitude}"
        
        message_body = (
            f"🚨 *RoadGuard AI Pothole Alert* 🚨\n\n"
            f"⚠️ *Severity Class:* {severity.upper()}\n"
            f"🕳️ *Simulated Depth Score:* {depth_score}/10\n"
            f"📍 *GPS Position:* `{latitude:.6f}, {longitude:.6f}`\n"
            f"⏱️ *Alert Timestamp:* {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}\n\n"
            f"🔗 *Google Maps directions:* {maps_link}"
        )

        message = client.messages.create(
            from_=TWILIO_FROM_NUMBER,
            body=message_body,
            to=to_number,
            media_url=[media_url] if media_url else None
        )
        print(f"[TWILIO SUCCESS] WhatsApp alert dispatched. SID: {message.sid} to {to_number}")
        return True
    except Exception as e:
        print(f"[TWILIO EXCEPTION] Failed to dispatch WhatsApp alert: {e}")
        raise e

# ==========================================
# IMAGE DECODER & ENCODER PIPELINE
# ==========================================
def decode_base64_image(base64_str):
    """Decodes a base64 encoded string into a CV2 standard image array."""
    try:
        # Strip header if present (e.g. data:image/jpeg;base64,)
        if ',' in base64_str:
            base64_str = base64_str.split(',', 1)[1]
            
        # Clean any whitespace, newlines, or url-encoded spaces
        base64_str = base64_str.replace(' ', '+').replace('\n', '').replace('\r', '').strip()
        
        # Add padding if missing (base64 string length must be multiple of 4)
        missing_padding = len(base64_str) % 4
        if missing_padding:
            base64_str += '=' * (4 - missing_padding)
            
        img_data = base64.b64decode(base64_str)
        np_arr = np.frombuffer(img_data, np.uint8)
        frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
        return frame
    except Exception as e:
        print(f"[DECODER ERROR] Failed translating robust base64 string to OpenCV buffer: {e}")
        return None

def encode_image_to_base64(frame):
    """Encodes a CV2 standard image array into a base64 encoded JPG data URI string."""
    try:
        _, buffer = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        base64_str = base64.b64encode(buffer).decode('utf-8')
        return "data:image/jpeg;base64," + base64_str
    except Exception as e:
        print(f"[ENCODER ERROR] Failed translating OpenCV frame to base64: {e}")
        return None

# ==========================================
# YOLO INFERENCE & BOUNDING BOX DRAWING
# ==========================================
def run_inference(frame):
    """
    Runs YOLO inference, draws bounding boxes on the frame,
    evaluates pothole severity, and calculates simulated depth scores (1-10)
    based on the bounding box area relative to the total frame size.
    """
    print("[Scanner] Frame received. Running inference...")

    if model is None:
        # Mockup inference fallback if model loading failed
        print("[YOLO] No objects detected (YOLO model offline, running mock fallback).")
        return False, "LOW", 0.0, 0, 1

    try:
        # Explicitly set conf=0.20 for highly sensitive detection during testing
        results = model(frame, conf=0.20, verbose=False)
        pothole_spotted = False
        max_confidence = 0.0
        max_area = 0
        h_frame, w_frame = frame.shape[:2]
        frame_area = h_frame * w_frame

        boxes_to_draw = []
        for r in results:
            boxes = r.boxes
            if len(boxes) > 0:
                pothole_spotted = True
            for box in boxes:
                x1, y1, x2, y2 = box.xyxy[0].cpu().numpy()
                confidence = float(box.conf[0])
                w, h = int(x2 - x1), int(y2 - y1)
                box_area = w * h
                
                # Coverage ratio
                coverage = box_area / frame_area if frame_area > 0 else 0
                
                # Severity and Depth logic based on coverage area ratio
                import random
                if coverage > 0.15:
                    box_severity = 'HIGH'
                    box_depth = random.randint(7, 10)
                elif coverage > 0.05:
                    box_severity = 'MEDIUM'
                    box_depth = random.randint(4, 6)
                else:
                    box_severity = 'LOW'
                    box_depth = random.randint(1, 3)
                
                if confidence > max_confidence:
                    max_confidence = confidence
                if box_area > max_area:
                    max_area = box_area

                # Print exact details to console immediately
                print(f"[YOLO] Object detected! Confidence: {confidence:.2f}, Coordinates: [{int(x1)}, {int(y1)}, {int(x2)}, {int(y2)}], Coverage: {coverage:.4f}, Severity: {box_severity}, Calculated Depth Score: {box_depth}/10")

                boxes_to_draw.append({
                    'coords': (int(x1), int(y1), int(x2), int(y2)),
                    'severity': box_severity,
                    'depth_score': box_depth,
                    'confidence': confidence,
                    'area': box_area
                })

        if not pothole_spotted:
            print("[YOLO] No objects detected.")

        # Calculate overall severity and depth score based on the largest box
        if pothole_spotted and len(boxes_to_draw) > 0:
            largest_box = max(boxes_to_draw, key=lambda b: b['area'])
            overall_severity = largest_box['severity']
            overall_depth = largest_box['depth_score']
            max_confidence = largest_box['confidence']
            max_area = largest_box['area']
        else:
            overall_severity = "LOW"
            overall_depth = 1
            max_confidence = 0.0
            max_area = 0

        # Draw bounding boxes and text directly on the image frame
        for box in boxes_to_draw:
            x1, y1, x2, y2 = box['coords']
            box_sev = box['severity']
            box_depth = box['depth_score']
            confidence_val = box['confidence']
            
            # Bright premium BGR colors matching the frontend
            if box_sev == "HIGH":
                color = (114, 75, 255)  # Cyber Pink
            elif box_sev == "MEDIUM":
                color = (20, 255, 57)   # Cyber Neon Green
            else:
                color = (255, 240, 0)   # Cyber Neon Cyan
                
            # Draw standard cv2 rectangle bounding box
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 3)
            
            # Text layout showing Severity, Depth, and Confidence Score
            label = f"Severity: {box_sev} | Depth: {box_depth}/10 | Conf: {confidence_val:.2f}"
            
            # Semi-transparent overlay style for high readability
            (w_text, h_text), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 2)
            cv2.rectangle(frame, (x1, y1 - h_text - 15), (x1 + w_text + 10, y1), color, -1)
            cv2.putText(frame, label, (x1 + 5, y1 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 2, cv2.LINE_AA)

        return pothole_spotted, overall_severity, max_confidence, max_area, overall_depth
    except Exception as e:
        print(f"[INFERENCE ERROR] YOLO computation crashed: {e}")
        return False, "LOW", 0.0, 0, 1

# ==========================================
# PRIMARY DECOUPLED API ROUTE
# ==========================================
@app.route('/api/analyze', methods=['POST'])
def api_analyze():
    """
    Primary POST endpoint accepting payloads from Rider Dashboard.
    Payload Schema:
    {
       "image": "data:image/jpeg;base64,...",
       "latitude": 37.77492,
       "longitude": -122.41945,
       "userId": "RoadGuardRider_G",
       "whatsappNumber": "+918169233423"
    }
    """
    global last_known_host
    data = request.json
    if not data or 'image' not in data:
        return jsonify({'status': 'error', 'message': 'Payload invalid. "image" field required.'}), 400

    img_base64 = data['image']
    lat = data.get('latitude')
    lng = data.get('longitude')
    rider_id = data.get('userId', 'UNKNOWN_RIDER')
    # Support overriding WhatsApp Target Number from client settings
    whatsapp_target = data.get('whatsappNumber') or TARGET_PHONE_NUMBER

    # Capture the active host URL dynamically to pass as media link base for WhatsApp
    last_known_host = request.host_url.rstrip('/')

    # Convert coordinates to floats if they arrive as strings
    try:
        lat = float(lat) if lat is not None else None
        lng = float(lng) if lng is not None else None
    except (ValueError, TypeError):
        return jsonify({'status': 'error', 'message': 'Coordinates must be valid floats.'}), 400

    # 1. Decode base64 frame
    frame = decode_base64_image(img_base64)
    if frame is None or frame.size == 0:
        print("[ERROR] OpenCV failed to decode the image array!")
        return jsonify({'status': 'error', 'message': 'Base64 image decoding failed.'}), 400

    # 2. Image Downscaling (The Fix for AI Speed)
    h, w = frame.shape[:2]
    if w > 640:
        new_w = 640
        new_h = int(h * (640 / w))
        frame = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_AREA)
        print(f"[Performance] Downscaled image from {w}x{h} to {new_w}x{new_h} to optimize inference speed.")

    # Force cv2.imwrite('debug_frame.jpg', image) immediately after successful decode & resize
    try:
        cv2.imwrite('debug_frame.jpg', frame)
        print("[DEBUG] Successfully saved raw frame to 'debug_frame.jpg'")
    except Exception as write_err:
        print(f"[DEBUG ERROR] Failed to save debug_frame.jpg: {write_err}")

    # 3. Run model predictions with Execution Timers (The Diagnostic Upgrade)
    start_time = time.time()
    pothole_spotted, severity, confidence, area, depth_score = run_inference(frame)
    inference_time = time.time() - start_time
    print(f"[Performance] YOLO Inference took {inference_time:.4f} seconds")

    pothole_id = None
    processed_base64 = img_base64

    # 4. Save coordinates directly to SQLite database if confirmed
    if pothole_spotted and lat is not None and lng is not None:
        # Convert processed frame with bounding boxes back into base64 for history/admin UI renders
        processed_base64 = encode_image_to_base64(frame) or img_base64
        pothole_id = register_pothole_in_db(lat, lng, severity, rider_id, processed_base64)

        # 5. Trigger Twilio WhatsApp notifications (HIGH/MEDIUM only) with Background Threading (The Fix for WhatsApp)
        if severity in ("HIGH", "MEDIUM"):
            current_time = time.time()
            last_alert = alert_cooldowns.get(whatsapp_target, 0)
            
            # Anti-spam protection: 10s cooldown per target phone number
            if current_time - last_alert >= COOLDOWN_SECONDS:
                # Set cooldown immediately to block duplicate requests from triggering threads
                alert_cooldowns[whatsapp_target] = current_time
                
                # Build the dynamic public image path
                media_url = f"{last_known_host}/api/potholes/{pothole_id}/image"
                
                # Dispatch Twilio alert asynchronously in background thread
                def run_alert_async():
                    try:
                        send_whatsapp_alert(
                            latitude=lat,
                            longitude=lng,
                            severity=severity,
                            depth_score=depth_score,
                            media_url=media_url,
                            override_to=whatsapp_target
                        )
                    except Exception as twilio_err:
                        print(f"[TWILIO FAILSAFE] Asynchronous WhatsApp alert failed: {twilio_err}")
                
                threading.Thread(target=run_alert_async, daemon=True).start()
                print(f"[Asynchronous Dispatch] Spawned background thread for Twilio alert to {whatsapp_target}.")

    # Grab fresh count from db to feed dynamic counts
    total_found = get_total_potholes_count()

    return jsonify({
        "status": "success",
        "pothole_detected": pothole_spotted,
        "pothole_spotted": pothole_spotted,                          # Keep for rider dashboard compatibility
        "pothole_id": pothole_id,                                    # Keep for rider dashboard compatibility
        "severity": severity if pothole_spotted else None,
        "depth": depth_score if pothole_spotted else None,
        "depth_score": depth_score if pothole_spotted else None,      # Keep for rider dashboard compatibility
        "lat": lat,
        "lng": lng,
        "potholes_count": total_found
    })

# Fallback route for legacy client scripts
@app.route('/api/analyze-frame', methods=['POST'])
def api_analyze_frame_fallback():
    return api_analyze()

@app.route('/api/health', methods=['GET'])
def api_health():
    """
    Simple health check route to verify uplink connectivity.
    """
    return jsonify({"status": "online"})

# ==========================================
# PHASE 3 API RETRIEVAL ENDPOINT (GET /api/potholes)
# ==========================================
@app.route('/api/potholes', methods=['GET'])
def api_get_potholes():
    """
    Returns a clean, structured JSON catalog of all logged potholes in the system database.
    Supports Flask-CORS so that external decoupled frontends can call this directly.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, latitude, longitude, severity, timestamp, rider_id, detection_count, image_data FROM potholes")
        rows = cursor.fetchall()
        conn.close()
        
        potholes = []
        for row in rows:
            potholes.append({
                "id": row[0],
                "lat": row[1],
                "lng": row[2],
                "severity": row[3],
                "timestamp": row[4],
                "rider_id": row[5] or "UNKNOWN_RIDER",
                "detection_count": row[6],
                "image_data": row[7]
            })
        return jsonify(potholes)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Database read failure: {e}"}), 500

# ==========================================
# PHASE 6 DYNAMIC IMAGE RETRIEVAL ENDPOINT (GET /api/potholes/<int:pothole_id>/image)
# ==========================================
@app.route('/api/potholes/<int:pothole_id>/image', methods=['GET'])
def api_get_pothole_image(pothole_id):
    """
    Serves the binary image file dynamically from the database for a given pothole ID.
    Used by Twilio Sandbox to fetch and transmit the processed image with overlays.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT image_data FROM potholes WHERE id = ?", (pothole_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row and row[0]:
            img_data = row[0]
            if ',' in img_data:
                img_data = img_data.split(',')[1]
            binary_img = base64.b64decode(img_data)
            return Response(binary_img, mimetype='image/jpeg')
        return "Image not found", 404
    except Exception as e:
        return f"Database error: {e}", 500

# ==========================================
# PHASE 4 PERSONAL HISTORY ENDPOINT (GET /api/history/<username>)
# ==========================================
@app.route('/api/history/<username>', methods=['GET'])
def api_get_history(username):
    """
    Returns a catalog of all potholes detected by a specific rider.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, latitude, longitude, severity, timestamp, detection_count FROM potholes WHERE rider_id = ?",
            (username,)
        )
        rows = cursor.fetchall()
        conn.close()
        
        history = []
        for row in rows:
            history.append({
                "id": row[0],
                "lat": row[1],
                "lng": row[2],
                "severity": row[3],
                "timestamp": row[4],
                "detection_count": row[5]
            })
        return jsonify(history)
    except Exception as e:
        return jsonify({"status": "error", "message": f"History read failure: {e}"}), 500

# ==========================================
# PHASE 8 ADMIN DATA MANAGEMENT ENDPOINTS
# ==========================================
@app.route('/api/admin/users', methods=['GET'])
def api_admin_users():
    """
    Returns a list of all unique rider_ids along with a count of how many potholes each rider has reported,
    and their latest recorded GPS coordinates.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        # Group by rider_id, select count, latitude, and longitude from the row matching MAX(timestamp)
        cursor.execute("""
            SELECT 
                COALESCE(NULLIF(rider_id, ''), 'UNKNOWN_RIDER') as clean_rider,
                COUNT(*),
                latitude,
                longitude,
                MAX(timestamp)
            FROM potholes 
            GROUP BY clean_rider
        """)
        rows = cursor.fetchall()
        conn.close()
        
        users = []
        for row in rows:
            users.append({
                "rider_id": row[0],
                "pothole_count": row[1],
                "lat": row[2],
                "lng": row[3]
            })
        return jsonify(users)
    except Exception as e:
        return jsonify({"status": "error", "message": f"Database read failure: {e}"}), 500

@app.route('/api/admin/users/<rider_id>', methods=['DELETE'])
def api_delete_user(rider_id):
    """
    Deletes all rows from the potholes table where the rider_id matches the parameter.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # If UNKNOWN_RIDER is deleted, also delete records where rider_id is NULL or empty
        if rider_id == "UNKNOWN_RIDER":
            cursor.execute("DELETE FROM potholes WHERE rider_id IS NULL OR rider_id = '' OR rider_id = 'UNKNOWN_RIDER'")
        else:
            cursor.execute("DELETE FROM potholes WHERE rider_id = ?", (rider_id,))
            
        conn.commit()
        changes = conn.total_changes
        conn.close()
        
        # Keep MongoDB fallback in sync if available
        if HAS_MONGO and potholes_col is not None:
            try:
                if rider_id == "UNKNOWN_RIDER":
                    potholes_col.delete_many({"$or": [{"rider_id": {"$exists": False}}, {"rider_id": ""}, {"rider_id": "UNKNOWN_RIDER"}]})
                else:
                    potholes_col.delete_many({"rider_id": rider_id})
            except Exception as m_err:
                print(f"[DB ERROR] MongoDB delete sync failed: {m_err}")
                
        return jsonify({
            "status": "success", 
            "message": f"Successfully deleted {changes} records for rider: {rider_id}"
        })
    except Exception as e:
        return jsonify({"status": "error", "message": f"Database delete failure: {e}"}), 500

# ==========================================
# LOCAL VIDEO RUNNER / BACKGROUND TESTER
# ==========================================
video_lock = threading.Lock()
is_processing_video = False

def run_video_analysis():
    """Worker function executing frame-by-frame testing in background thread."""
    global is_processing_video, last_known_host
    video_path = "test.mp4"

    if not os.path.exists(video_path):
        print(f"[VIDEO TEST ERROR] '{video_path}' file not found in root workspace directory!")
        with video_lock:
            is_processing_video = False
        return

    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        print(f"[VIDEO TEST ERROR] Failed opening OpenCV reader for '{video_path}'")
        with video_lock:
            is_processing_video = False
        return

    print(f"\n[VIDEO TEST] Success: Opened local video file '{video_path}'. Starting analysis...")
    print(f"[VIDEO TEST] Targeting Twilio WhatsApp: {TARGET_PHONE_NUMBER}")
    
    last_whatsapp_time = 0
    potholes_spotted_count = 0
    frame_index = 0

    try:
        while cap.isOpened():
            ret, frame = cap.read()
            if not ret:
                break

            frame_index += 1
            
            # Analyze every 5th frame to optimize inference speed during local testing
            if frame_index % 5 == 0:
                pothole_spotted, severity, confidence, area, depth_score = run_inference(frame)
                
                if pothole_spotted:
                    potholes_spotted_count += 1
                    current_time = time.time()
                    
                    # Mock locations drifting around a testing course in San Francisco
                    mock_lat = 37.7749 + (np.random.rand() - 0.5) * 0.004
                    mock_lng = -122.4194 + (np.random.rand() - 0.5) * 0.004
                    
                    # Save coordinates directly to SQLite database using 'Local_Test_Rider'
                    # Convert the processed frame with bounding boxes back into base64 first
                    processed_base64 = encode_image_to_base64(frame)
                    pothole_id = register_pothole_in_db(
                        mock_lat, mock_lng, severity, 
                        rider_id="Local_Test_Rider", 
                        image_data=processed_base64
                    )

                    # Cooldown limit: 5 seconds for video test alerts
                    if current_time - last_whatsapp_time >= 5.0:
                        print(f"[VIDEO TEST DETECT] Frame {frame_index}: Spotted {severity} severity anomaly! Triggering alert...")
                        # Build the dynamic public image path
                        media_url = f"{last_known_host}/api/potholes/{pothole_id}/image" if pothole_id else None
                        
                        send_whatsapp_alert(
                            latitude=mock_lat,
                            longitude=mock_lng,
                            severity=f"{severity} (Video Test)",
                            depth_score=depth_score,
                            media_url=media_url
                        )
                        last_whatsapp_time = current_time

            # Small sleep to throttle execution speed
            time.sleep(0.01)

        cap.release()
        print(f"[VIDEO TEST COMPLETE] Finished processing local video. Total detections: {potholes_spotted_count}\n")
    except Exception as e:
        print(f"[VIDEO TEST EXCEPTION] Processing crashed: {e}")
    finally:
        with video_lock:
            is_processing_video = False

@app.route('/test_video', methods=['GET'])
def test_video():
    """Trigger endpoint starting background local video testing."""
    global is_processing_video
    
    # Check configurations before launch
    if not TWILIO_ACCOUNT_SID or TWILIO_ACCOUNT_SID == "YOUR_ACCOUNT_SID_HERE":
        return jsonify({
            'status': 'config_warning',
            'message': 'Twilio SID is not set. Please set TWILIO_ACCOUNT_SID in app.py to receive active alerts.'
        }), 400

    # Thread check synchronization
    already_running = False
    with video_lock:
        if is_processing_video:
            already_running = True
        else:
            is_processing_video = True
            
    if already_running:
        return jsonify({
            'status': 'active',
            'message': 'A local video testing thread is already executing in the background.'
        })

    # Spawn background thread runner
    worker = threading.Thread(target=run_video_analysis, daemon=True)
    worker.start()

    return jsonify({
        'status': 'success',
        'message': 'Background local video processing started successfully. Check your server console/logs and WhatsApp.'
    })

# ==========================================
# RESTORED LEGACY FLASK HTML PAGES
# ==========================================
@app.route('/')
def landing():
    return render_template('index.html')

@app.route('/live_cam')
def live_cam():
    return render_template('live_cam.html', chat_id=session.get('chat_id', ''), stream_url=session.get('stream_url', ''))

@app.route('/map')
def map_view():
    return render_template('map.html')
    
@app.route('/trip_stats')
def trip_stats():
    return render_template('trip_stats.html')

@app.route('/frontend/<path:path>')
def send_frontend_file(path):
    return send_from_directory('frontend', path)

if __name__ == '__main__':
    print("[SYSTEM] Starting RoadGuard AI Backend Engine...")
    print(f"[SYSTEM] Local Database Path: {os.path.abspath(DB_PATH)}")
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
