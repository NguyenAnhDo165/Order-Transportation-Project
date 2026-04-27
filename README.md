🚗 GoRide AI – Smart Ride-Hailing Simulation

A smart ride-hailing web application inspired by Grab, integrating AI (Fuzzy Logic), map routing, and real-time simulation.

🌟 Features
📍 Address Search & Routing
Geocoding with Nominatim
Shortest path using OSMnx + NetworkX
🤖 Smart Driver Matching (AI)
Fuzzy Logic based on:
Rating ⭐
Distance 📏
Experience 🚗
💰 Dynamic Pricing
Surge pricing based on time & demand
Discount for long trips
🚗 Vehicle Simulation
Car / Bike / 7-seat options
Real movement along route (Leaflet animation)
📡 Live Tracking
Driver moves along actual road (not teleport)
Camera follows vehicle
⭐ Rating System
User can rate driver
Save review to CSV
🌐 Public Access
Integrated with ngrok for sharing localhost
🧠 Tech Stack
Backend: Python, Flask
Map & Routing: OSMnx, NetworkX, Folium
Frontend: HTML, CSS, JavaScript (Leaflet)
AI Logic: Fuzzy Logic (custom implementation)
API & Geocoding: Geopy (Nominatim)
📂 Project Structure
Project Grab/
│── app.py
│── drivers.csv
│── ratings.csv
│── hcm_graph.pkl
│
├── templates/
│   ├── index.html
│   ├── result.html
│   └── _map_inner.html
│
└── static/
    ├── css/
    └── js/
⚙️ Installation
1. Clone project
git clone https://github.com/your-username/goride-ai.git
cd goride-ai
2. Create virtual environment
python -m venv venv
venv\Scripts\activate
3. Install dependencies
pip install -r requirements.txt
▶️ Run Application
python app.py
