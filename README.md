# 🛡️ RoadGuard AI | Real-Time Pothole Detection

> **Empowering drivers to automatically detect, catalog, and report road infrastructure hazards in real-time.** 

RoadGuard AI is a decentralized, crowdsourced smart-mapping platform. By utilizing edge-computing and computer vision, it transforms standard commuting dashcams into a network of automated hazard-detection sensors. 

---
## 🔗 Live Demo
https://tinyurl.com/Asmi-Katke-Pothole-Detection

## 🌟 Key Features

* **📷 Real-Time Vision Processing:** Utilizes OpenCV and YOLO-architecture object detection to analyze live video feeds for road surface anomalies.
* **🗺️ Interactive Admin Command Center:** A live, geospatial dashboard plotting hazard coordinates, severity levels, and captured frame evidence on an interactive map.
* **📱 WhatsApp Telemetry Alerts:** Seamlessly integrated with the Twilio API to send instant SMS/WhatsApp notifications when critical road hazards are logged.
* **☁️ Cloud-Optimized Architecture:** Features a dynamic dependency fallback (`HAS_YOLO` toggle), allowing the system to run purely on lightweight CPU environments (like Render Free Tier) while maintaining full dashboard and database functionality.
* **🔒 Seamless Rider Onboarding:** Glassmorphism UI with local browser storage to persist rider sessions and notification preferences.

---

## 💻 Tech Stack

**Frontend**
* HTML5 / CSS3 (Glassmorphism & Neon-glow UI)
* Vanilla JavaScript 
* Leaflet.js / Interactive Web Mapping

**Backend & AI**
* Python 3
* Flask & Gunicorn (WSGI Server)
* SQLite (Trip & Hazard Database)
* OpenCV (Computer Vision Headless)
* Twilio API (Automated Notifications)

---

## 🚀 Run it Locally

Follow these steps to run the full, GPU-accelerated version of RoadGuard AI on your local machine:

### 1. Clone the repository
```bash
git clone [https://github.com/AsmiKatke/Pothole-Detection.git](https://github.com/AsmiKatke/Pothole-Detection.git)
cd Pothole-Detection

### 2. Install dependencies
(Note: To run real-time YOLO detection locally, ensure you install the full ultralytics and torch packages, not just the cloud-optimized requirements).
```
Bash
pip install -r requirements.txt
pip install ultralytics torch
3. Configure Environment Variables
You will need your own Twilio API credentials to enable WhatsApp notifications.

Bash
# Add your Twilio SID and Auth Token to the app environment
TWILIO_ACCOUNT_SID=your_sid_here
TWILIO_AUTH_TOKEN=your_token_here
4. Boot the server
Bash
python app.py
Visit http://localhost:5000 in your browser to access the Rider Dashboard!

☁️ Live Cloud Deployment
This project is actively configured for continuous deployment on Render. The production environment runs a lightweight, dependency-optimized configuration designed to operate strictly within a 512MB memory limit while maintaining full REST API and dashboard uptime.

👨‍💻 Developed By
Asmi Katke

AI Engineering & Computer Vision
