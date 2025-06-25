from fastapi import FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles  # Added for static files
from fastapi.responses import FileResponse  # Added for serving index.html
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import asyncio
import json
from datetime import datetime, timedelta
import math
import paho.mqtt.client as mqtt
import threading
import os  # Added for path handling
import uvicorn

app = FastAPI()

# Mount static files directory
app.mount("/static", StaticFiles(directory="static"), name="static")

# Serve index.html at root
@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")

# Enable CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Update to specific frontend URL in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global variables for vehicle tracking
active_vehicles = {}
mqtt_client = None
mqtt_connected = False

# Vehicle capacity assumptions
BUS_CAPACITY = 40
TRUCK_VOLUME = 15_000_000  # cm³ (15m³)
TRUCK_WEIGHT = 1000  # kg
TROOP_CARRIER_CAPACITY = 20

# Transit camps and checkpoints data
transit_camps = []
traffic_checkpoints = []

# Request models
class LoadRequest(BaseModel):
    loadType: str
    soldierCount: Optional[int] = None
    gearLoad: Optional[str] = None
    vehiclePreference: Optional[str] = None
    boxCount: Optional[int] = None
    boxLength: Optional[float] = None
    boxWidth: Optional[float] = None
    boxHeight: Optional[float] = None
    boxWeight: Optional[float] = None
    isFragile: Optional[bool] = None
    isStackable: Optional[bool] = None
    startCoords: Optional[List[float]] = None
    endCoords: Optional[List[float]] = None
    vehicleSpeed: Optional[float] = 40
    priority: Optional[str] = "normal"

class VehicleAsset(BaseModel):
    vehicleId: str
    vehicleType: str
    capacity: int
    currentLocation: List[float]
    status: str  # available, in_transit, maintenance
    maxWeight: Optional[float] = None
    maxVolume: Optional[float] = None

class ConvoyRequest(BaseModel):
    convoyId: str
    vehicles: List[str]
    route: List[List[float]]
    priority: str
    loadType: str
    estimatedDuration: int

class TransitCamp(BaseModel):
    campId: str
    name: str
    coordinates: List[float]
    capacity: int
    facilities: List[str]

class TrafficCheckpoint(BaseModel):
    tcpId: str
    name: str
    coordinates: List[float]
    status: str  # operational, closed, congested

class MQTTConfig(BaseModel):
    broker: str
    port: int
    topic: str
    username: Optional[str] = None
    password: Optional[str] = None

# MQTT Functions
def on_connect(client, userdata, flags, rc):
    global mqtt_connected
    if rc == 0:
        mqtt_connected = True
        print("Connected to MQTT Broker")
        client.subscribe("vehicles/+/location")
        client.subscribe("vehicles/+/status")
    else:
        mqtt_connected = False
        print(f"Failed to connect to MQTT Broker: {rc}")

def on_message(client, userdata, msg):
    try:
        topic_parts = msg.topic.split('/')
        vehicle_id = topic_parts[1]
        data_type = topic_parts[2]
        
        payload = json.loads(msg.payload.decode())
        
        if vehicle_id not in active_vehicles:
            active_vehicles[vehicle_id] = {}
        
        if data_type == "location":
            active_vehicles[vehicle_id].update({
                "lat": payload.get("lat"),
                "lng": payload.get("lng"),
                "timestamp": datetime.now().isoformat(),
                "speed": payload.get("speed", 0),
                "heading": payload.get("heading", 0)
            })
        elif data_type == "status":
            active_vehicles[vehicle_id].update({
                "status": payload.get("status"),
                "fuel_level": payload.get("fuel_level"),
                "engine_status": payload.get("engine_status")
            })
            
    except Exception as e:
        print(f"Error processing MQTT message: {e}")

# Priority calculation
def calculate_priority_score(load_type: str, priority: str, urgency_factors: Dict = None):
    base_scores = {
        "ammo": 100,
        "fuel": 90,
        "troops": 80,
        "medical": 95,
        "supplies": 60,
        "cargo": 50
    }
    
    priority_multiplier = {
        "critical": 2.0,
        "high": 1.5,
        "normal": 1.0,
        "low": 0.7
    }
    
    base_score = base_scores.get(load_type, 50)
    multiplier = priority_multiplier.get(priority, 1.0)
    
    return base_score * multiplier

# Route optimization functions
def calculate_distance(coord1: List[float], coord2: List[float]) -> float:
    """Calculate distance between two coordinates using Haversine formula"""
    lat1, lon1 = math.radians(coord1[0]), math.radians(coord1[1])
    lat2, lon2 = math.radians(coord2[0]), math.radians(coord2[1])
    
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    
    a = math.sin(dlat/2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    r = 6371  # Earth's radius in kilometers
    
    return c * r

def optimize_vehicle_allocation(requests: List[LoadRequest], available_vehicles: List[VehicleAsset]):
    """Optimize vehicle allocation based on distance, capacity, and priority"""
    allocations = []
    
    # Sort requests by priority
    sorted_requests = sorted(requests, 
                           key=lambda x: calculate_priority_score(x.loadType, x.priority or "normal"), 
                           reverse=True)
    
    for request in sorted_requests:
        best_vehicles = []
        
        if request.loadType == "troops":
            vehicles_needed = calculate_troop_vehicles(request.soldierCount, request.gearLoad)
            suitable_vehicles = [v for v in available_vehicles 
                               if v.vehicleType in ["bus", "troop_carrier"] and v.status == "available"]
        else:
            vehicles_needed = calculate_cargo_vehicles(request)
            suitable_vehicles = [v for v in available_vehicles 
                               if v.vehicleType == "truck" and v.status == "available"]
        
        # Find closest available vehicles
        if request.startCoords:
            suitable_vehicles.sort(key=lambda v: calculate_distance(v.currentLocation, request.startCoords))
        
        selected_vehicles = suitable_vehicles[:vehicles_needed]
        
        if len(selected_vehicles) >= vehicles_needed:
            allocations.append({
                "request": request,
                "vehicles": selected_vehicles,
                "status": "allocated"
            })
            # Mark vehicles as assigned
            for vehicle in selected_vehicles:
                vehicle.status = "assigned"
        else:
            allocations.append({
                "request": request,
                "vehicles": selected_vehicles,
                "status": "partial",
                "shortage": vehicles_needed - len(selected_vehicles)
            })
    
    return allocations

def calculate_troop_vehicles(soldier_count: int, gear_load: str) -> int:
    gear_factors = {"light": 1.0, "medium": 1.5, "heavy": 2.0}
    factor = gear_factors.get(gear_load, 1.0)
    troops_per_vehicle = BUS_CAPACITY / factor
    return math.ceil(soldier_count / troops_per_vehicle)

def calculate_cargo_vehicles(request: LoadRequest) -> int:
    if not all([request.boxCount, request.boxLength, request.boxWidth, 
               request.boxHeight, request.boxWeight]):
        return 1
    
    total_volume = request.boxCount * request.boxLength * request.boxWidth * request.boxHeight
    total_weight = request.boxCount * request.boxWeight
    
    trucks_by_volume = math.ceil(total_volume / TRUCK_VOLUME)
    trucks_by_weight = math.ceil(total_weight / TRUCK_WEIGHT)
    
    return max(trucks_by_volume, trucks_by_weight)

# API Endpoints
@app.post("/connect-mqtt/")
async def connect_mqtt(config: MQTTConfig):
    global mqtt_client, mqtt_connected
    
    try:
        mqtt_client = mqtt.Client()
        mqtt_client.on_connect = on_connect
        mqtt_client.on_message = on_message
        
        if config.username and config.password:
            mqtt_client.username_pw_set(config.username, config.password)
        
        mqtt_client.connect(config.broker, config.port, 60)
        mqtt_client.loop_start()
        
        return {"status": "success", "message": "MQTT connection initiated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MQTT connection failed: {str(e)}")

@app.get("/mqtt-status/")
async def get_mqtt_status():
    return {
        "connected": mqtt_connected,
        "active_vehicles": len(active_vehicles),
        "vehicles": active_vehicles
    }

@app.post("/assign-vehicles/")
async def assign_vehicles(data: LoadRequest):
    try:
        response = {
            "status": "success",
            "message": "Load assignment processed",
            "data": data.dict(),
            "priority_score": calculate_priority_score(data.loadType, data.priority or "normal")
        }
        
        if data.loadType == "troops":
            vehicles_required = calculate_troop_vehicles(data.soldierCount, data.gearLoad)
            response["vehiclesRequired"] = vehicles_required
            response["recommendedVehicleType"] = "bus" if vehicles_required > 2 else "troop_carrier"
            
        elif data.loadType == "cargo":
            vehicles_required = calculate_cargo_vehicles(data)
            response["vehiclesRequired"] = vehicles_required
            response["recommendedVehicleType"] = "truck"
            
            # Add fragile handling recommendations
            if data.isFragile:
                response["specialHandling"] = "Fragile cargo - reduce speed, avoid rough terrain"
                
        elif data.loadType == "fuel":
            # Fuel transport calculations
            fuel_capacity_per_truck = 10000  # liters
            estimated_fuel = data.boxCount * 200 if data.boxCount else 5000  # estimate
            vehicles_required = math.ceil(estimated_fuel / fuel_capacity_per_truck)
            response["vehiclesRequired"] = vehicles_required
            response["recommendedVehicleType"] = "fuel_tanker"
            response["specialHandling"] = "Hazardous material - special permits required"
            
        elif data.loadType == "ammo":
            # Ammunition transport calculations
            ammo_capacity_per_truck = 5000  # kg
            estimated_weight = data.boxCount * data.boxWeight if data.boxCount and data.boxWeight else 2000
            vehicles_required = math.ceil(estimated_weight / ammo_capacity_per_truck)
            response["vehiclesRequired"] = vehicles_required
            response["recommendedVehicleType"] = "armored_truck"
            response["specialHandling"] = "Explosive material - special escort required"
        
        # Add route recommendations if coordinates provided
        if data.startCoords and data.endCoords:
            distance = calculate_distance(data.startCoords, data.endCoords)
            travel_time = distance / (data.vehicleSpeed or 40)
            
            response["routeAnalysis"] = {
                "distance_km": round(distance, 2),
                "estimated_time_hours": round(travel_time, 2),
                "fuel_consumption_estimate": round(distance * 0.3 * vehicles_required, 2),  # liters
                "recommended_speed": data.vehicleSpeed or 40
            }
        
        return response
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing request: {str(e)}")

@app.post("/register-vehicle/")
async def register_vehicle(vehicle: VehicleAsset):
    # Register a vehicle asset manually
    active_vehicles[vehicle.vehicleId] = {
        "vehicleType": vehicle.vehicleType,
        "capacity": vehicle.capacity,
        "lat": vehicle.currentLocation[0],
        "lng": vehicle.currentLocation[1],
        "status": vehicle.status,
        "maxWeight": vehicle.maxWeight,
        "maxVolume": vehicle.maxVolume,
        "timestamp": datetime.now().isoformat()
    }
    return {"status": "success", "message": "Vehicle registered"}

@app.get("/available-vehicles/")
async def get_available_vehicles():
    available = {k: v for k, v in active_vehicles.items() 
                if v.get("status") in ["available", None]}
    return {
        "total_vehicles": len(active_vehicles),
        "available_vehicles": len(available),
        "vehicles": available
    }

@app.post("/add-transit-camp/")
async def add_transit_camp(camp: TransitCamp):
    transit_camps.append(camp.dict())
    return {"status": "success", "message": "Transit camp added"}

@app.post("/add-checkpoint/")
async def add_checkpoint(tcp: TrafficCheckpoint):
    traffic_checkpoints.append(tcp.dict())
    return {"status": "success", "message": "Traffic checkpoint added"}

@app.get("/transit-camps/")
async def get_transit_camps():
    return {"camps": transit_camps}

@app.get("/checkpoints/")
async def get_checkpoints():
    return {"checkpoints": traffic_checkpoints}

@app.post("/optimize-convoy/")
async def optimize_convoy(requests: List[LoadRequest]):
    # Get available vehicles (in real scenario, this would be from MQTT data)
    available_vehicles = []
    for vehicle_id, vehicle_data in active_vehicles.items():
        if vehicle_data.get("status") in ["available", None]:
            vehicle = VehicleAsset(
                vehicleId=vehicle_id,
                vehicleType=vehicle_data.get("vehicleType", "truck"),
                capacity=vehicle_data.get("capacity", 40),
                currentLocation=[vehicle_data.get("lat", 0), vehicle_data.get("lng", 0)],
                status="available"
            )
            available_vehicles.append(vehicle)
    
    allocations = optimize_vehicle_allocation(requests, available_vehicles)
    
    return {
        "status": "success",
        "total_requests": len(requests),
        "allocations": len(allocations),
        "details": allocations
    }

@app.get("/convoy-timeline/{convoy_id}")
async def get_convoy_timeline(convoy_id: str):
    # Generate estimated timeline for convoy movement
    # This would integrate with real-time traffic and checkpoint data
    return {
        "convoy_id": convoy_id,
        "timeline": [
            {"checkpoint": "Start", "eta": "08:00", "status": "scheduled"},
            {"checkpoint": "TCP-1", "eta": "10:30", "status": "scheduled"},
            {"checkpoint": "Transit Camp Alpha", "eta": "14:00", "status": "scheduled"},
            {"checkpoint": "TCP-2", "eta": "16:30", "status": "scheduled"},
            {"checkpoint": "Destination", "eta": "18:00", "status": "scheduled"}
        ]
    }

@app.post("/generate-movement-instructions/")
async def generate_movement_instructions(convoy: ConvoyRequest):
    instructions = {
        "convoy_id": convoy.convoyId,
        "priority": convoy.priority,
        "load_type": convoy.loadType,
        "vehicle_count": len(convoy.vehicles),
        "route_distance": sum([calculate_distance(convoy.route[i], convoy.route[i+1]) 
                              for i in range(len(convoy.route)-1)]),
        "estimated_duration": convoy.estimatedDuration,
        "special_instructions": [],
        "checkpoint_instructions": [],
        "fol_requirements": {
            "fuel_stops": 2,
            "rest_stops": 1,
            "overnight_camp": "Transit Camp Alpha" if convoy.estimatedDuration > 480 else None
        }
    }
    
    # Add special instructions based on load type
    if convoy.loadType == "ammo":
        instructions["special_instructions"].extend([
            "Maintain 100m minimum distance between vehicles",
            "Escort vehicle required",
            "No smoking or open flames"
        ])
    elif convoy.loadType == "fuel":
        instructions["special_instructions"].extend([
            "Hazmat placards required",
            "Fire suppression equipment mandatory",
            "Avoid populated areas where possible"
        ])
    return instructions

# uvicorn main:app --host 0.0.0.0 --port 8000 --reload

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)
    # uvicorn main:app --host 0.0.0.0 --port 8000 --reload
