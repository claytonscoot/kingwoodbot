from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, EmailStr
from datetime import datetime
import requests
import json
import csv
import os
import uuid
import logging
from typing import Optional, Dict, List, Literal
from contextlib import asynccontextmanager
import aiofiles

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ----------------------------
# CONFIG (YOUR REAL CONTACT INFO)
# ----------------------------
BUSINESS_NAME = "Astro Outdoor Designs"
SERVICE_AREA = "Kingwood / Houston, TX"

# Your actual contact information
BUSINESS_PHONE = "832-280-5783"  # Call or text
BUSINESS_EMAIL = "admin@kingwoodfencing.com"
FACEBOOK_PAGE = "www.facebook.com/astrooutdoordesigns"
WEBSITE = "astrooutdoordesigns.com"

# File where contact form submissions are stored
LEADS_FILE = "leads.csv"
CHAT_SESSIONS_FILE = "chat_sessions.json"

# Ollama settings
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "phi")

# Optimized for speed + quality
OLLAMA_OPTIONS = {
    "temperature": 0.35,
    "num_predict": 120,  # Slightly longer for better responses
    "top_p": 0.9,
    "top_k": 40,
    "repeat_penalty": 1.1
}

# In-memory storage for active sessions
active_sessions: Dict[str, dict] = {}
recent_leads: List[dict] = []

# ----------------------------
# MODELS
# ----------------------------
class Chat(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    session_id: Optional[str] = None
    user_name: Optional[str] = None

class Lead(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    phone: str = Field(default="", max_length=20)
    email: Optional[EmailStr] = None
    address_or_zip: str = Field(default="", max_length=100)
    preferred_contact: Literal["call", "text", "email"] = "text"
    project_details: str = Field(..., min_length=10, max_length=2000)

class LiveQuoteRequest(BaseModel):
    session_id: str
    user_name: str = Field(..., min_length=2, max_length=50)
    phone: str = Field(..., min_length=10, max_length=20)
    service_needed: str = Field(..., min_length=1, max_length=200)

# ----------------------------
# STARTUP/SHUTDOWN
# ----------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"🚀 Starting {BUSINESS_NAME} Chat System...")
    ensure_leads_file()
    await test_ollama_connection()
    yield
    logger.info("👋 Shutting down...")

app = FastAPI(title=f"{BUSINESS_NAME} - Fence Assistant", lifespan=lifespan)

# CORS - Allow embedding/cross-domain calls (ngrok, wordpress, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten later if needed
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files (optional)
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except:
    pass

# ----------------------------
# HELPER FUNCTIONS
# ----------------------------
def ensure_leads_file():
    """Create leads CSV file with headers if it doesn't exist"""
    if not os.path.exists(LEADS_FILE):
        with open(LEADS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp",
                "ip",
                "name", 
                "phone",
                "email",
                "address_or_zip",
                "preferred_contact",
                "project_details",
                "session_id",
                "status"
            ])
        logger.info(f"📝 Created {LEADS_FILE}")

async def test_ollama_connection():
    """Test Ollama connection and model availability"""
    try:
        response = requests.get("http://localhost:11434/api/tags", timeout=5)
        if response.status_code == 200:
            models = response.json().get("models", [])
            model_names = [model["name"] for model in models]
            if any(OLLAMA_MODEL.split(":")[0] in name for name in model_names):
                logger.info(f"✅ AI Assistant ready - {OLLAMA_MODEL}")
                return True
            else:
                logger.warning(f"⚠️ Model {OLLAMA_MODEL} not found. Available: {[m['name'] for m in models]}")
        return False
    except Exception as e:
        logger.error(f"❌ Ollama connection failed: {e}")
        return False

def company_system_prompt(user_message: str) -> str:
    """Create the company-specific AI prompt"""

    return f"""
You are the website chat assistant for {BUSINESS_NAME}, a professional fence & gate contractor serving {SERVICE_AREA}.

---------------------------------
ABSOLUTE RULES
---------------------------------
- Speak like a real company rep. Use "we" and "our team."
- Do NOT mention AI, chatbot, or automation.
- Keep responses practical, contractor-style, and quote-focused.
- If key info is missing, ask 2–4 direct questions.
- Always calculate pricing when footage is provided.

---------------------------------
PRICING LOGIC (IMPORTANT)
---------------------------------
When a customer provides:
- Linear footage
- Fence height
- Style
- Post type
- Gate count

You MUST:
1) Use the average per-foot cost for that style.
2) Apply a 15% range both lower and higher.
3) Multiply by footage.
4) Clearly label it as a working estimate.

Example baseline pricing (Kingwood / Houston averages):

6'6" single cedar privacy
- Average: $32 per foot
- 15% range: $27 – $37 per foot

100' example:
$3,200 average
Range: $2,720 – $3,680 installed

Pine picket privacy:
10–15% cheaper than cedar equivalent.

Steel post upgrade:
Add $6–$10 per foot average.

Single gate:
$450–$750

Double gate:
$900–$1,500

Always explain assumptions:
- Flat yard assumed
- Normal access
- No major tear-out issues
- No extreme slope

---------------------------------
OUR INSTALL METHOD (USE WHEN RELEVANT)
---------------------------------
Standard 6'6" build:
- Cedar pickets
- Pine frame
- 2x6 pressure treated baseboard
- 2x4 rails
- Ring shank galvanized fasteners
- Old posts cut below grade
- New posts set in fresh concrete

Steel post option:
- 2-1/2" Schedule 40 steel
- 3 brackets per section
- Rails run full 16' where possible
- Frame attaches directly to posts
- No floating rail joints mid-panel
- Stronger structural integrity than most competitors
- Can box steel posts if HOA requires wood look

---------------------------------
SALES PROCESS
---------------------------------
Before final quote:
- Confirm zip code
- Confirm footage
- Confirm height
- Confirm gates
- Ask for photos

We usually schedule installs within 1–2 weeks.
Same-week service often available for repairs.

---------------------------------
CONTACT INFO
---------------------------------
Call or Text: {BUSINESS_PHONE}
Email: {BUSINESS_EMAIL}
Website: {WEBSITE}
Facebook: {FACEBOOK_PAGE}
Service Area: {SERVICE_AREA}

---------------------------------
GOAL
---------------------------------
- Provide calculated working estimate when possible.
- Show clear math.
- Encourage photos to tighten numbers.
- Move toward scheduling confirmation visit.

Customer message:
{user_message}
""".strip()

def generate_session_id() -> str:
    """Generate unique session ID"""
    return str(uuid.uuid4())

# ----------------------------
# API ROUTES
# ----------------------------
@app.get("/")
def serve_frontend():
    """Serve the main chat interface"""
    return FileResponse("chat.html")

@app.get("/admin") 
def serve_admin():
    """Serve admin dashboard"""
    try:
        return FileResponse("admin.html")
    except:
        return JSONResponse({
            "message": "Admin dashboard not available",
            "leads_count": len(recent_leads),
            "active_sessions": len(active_sessions)
        })

@app.get("/health")
def health_check():
    """Health check endpoint"""
    return {
        "status": "ok", 
        "business": BUSINESS_NAME,
        "model": OLLAMA_MODEL,
        "active_sessions": len(active_sessions),
        "total_leads": len(recent_leads),
        "phone": BUSINESS_PHONE,
        "email": BUSINESS_EMAIL
    }

@app.post("/chat")
def chat(req: Chat, request: Request):
    """Handle fence-related chat requests"""
    prompt = req.prompt.strip()
    session_id = req.session_id or generate_session_id()
    
    # Initialize or update session
    if session_id not in active_sessions:
        active_sessions[session_id] = {
            "created": datetime.now().isoformat(),
            "user_name": req.user_name or "Visitor",
            "message_count": 0,
            "last_activity": datetime.now().isoformat(),
            "ip": request.client.host if request.client else "",
            "messages": []
        }
    
    # Update session activity
    active_sessions[session_id]["last_activity"] = datetime.now().isoformat()
    active_sessions[session_id]["message_count"] += 1
    active_sessions[session_id]["messages"].append({
        "timestamp": datetime.now().isoformat(),
        "type": "user",
        "message": prompt
    })
    
    # Handle empty prompt
    if not prompt:
        fallback_response = "Quick question — what are you trying to build or fix? (Approximate feet + height helps a lot.)"
        active_sessions[session_id]["messages"].append({
            "timestamp": datetime.now().isoformat(),
            "type": "assistant", 
            "message": fallback_response
        })
        return {
            "response": fallback_response,
            "session_id": session_id,
            "business": BUSINESS_NAME
        }

    # Prepare AI request
    payload = {
        "model": OLLAMA_MODEL,
        "prompt": company_system_prompt(prompt),
        "stream": False,
        "keep_alive": "10m",
        "options": OLLAMA_OPTIONS
    }

    try:
        response = requests.post(OLLAMA_URL, json=payload, timeout=120)
        response.raise_for_status()
        data = response.json()
        ai_response = (data.get("response") or "").strip()

        # Fallback for empty responses
        if not ai_response:
            ai_response = "Got it. What's the approximate length (feet) and desired height (6ft/7ft/8ft)? Any gates needed?"

        # Add business contact info for quote-related queries
        if any(word in prompt.lower() for word in ['quote', 'price', 'cost', 'estimate', 'schedule', 'when', 'how much']):
            ai_response += f"\n\n📞 **Ready to move forward?** Call or text us at **{BUSINESS_PHONE}** or email **{BUSINESS_EMAIL}** for fastest response!"

        # Add social media mention for brand awareness
        if any(word in prompt.lower() for word in ['facebook', 'social', 'reviews', 'find you']):
            ai_response += f"\n\n📱 **Find us on Facebook:** {FACEBOOK_PAGE} or search for us on Google!"

        # Save AI response to session
        active_sessions[session_id]["messages"].append({
            "timestamp": datetime.now().isoformat(),
            "type": "assistant",
            "message": ai_response
        })

        return {
            "response": ai_response,
            "session_id": session_id,
            "message_count": active_sessions[session_id]["message_count"],
            "business": BUSINESS_NAME
        }

    except requests.exceptions.Timeout:
        timeout_response = f"That's taking longer than normal. Try again — or call/text us directly at {BUSINESS_PHONE} for immediate help!"
        active_sessions[session_id]["messages"].append({
            "timestamp": datetime.now().isoformat(),
            "type": "assistant",
            "message": timeout_response
        })
        return {"response": timeout_response, "session_id": session_id}
        
    except Exception as e:
        error_response = f"Something hiccupped on our side. Call or text us at {BUSINESS_PHONE} or email {BUSINESS_EMAIL} for immediate assistance."
        logger.error(f"Chat error: {e}")
        active_sessions[session_id]["messages"].append({
            "timestamp": datetime.now().isoformat(),
            "type": "assistant",
            "message": error_response
        })
        return {"response": error_response, "session_id": session_id}

@app.post("/lead")
def submit_lead(req: Lead, request: Request):
    """Handle quote/lead form submissions"""
    try:
        ip = request.client.host if request.client else ""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Generate unique lead ID
        lead_id = str(uuid.uuid4())[:8]
        
        # Append to CSV file
        with open(LEADS_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                timestamp,
                ip,
                req.name.strip(),
                req.phone.strip(),
                (req.email.strip() if req.email else ""),
                req.address_or_zip.strip(),
                req.preferred_contact.strip(),
                req.project_details.strip(),
                lead_id,
                "new"
            ])

        # Store in memory for admin dashboard
        lead_data = {
            "id": lead_id,
            "timestamp": timestamp,
            "name": req.name.strip(),
            "phone": req.phone.strip(),
            "email": req.email.strip() if req.email else "",
            "area": req.address_or_zip.strip(),
            "preferred_contact": req.preferred_contact,
            "details": req.project_details.strip(),
            "status": "new"
        }
        recent_leads.append(lead_data)
        
        # Keep only last 50 leads in memory
        if len(recent_leads) > 50:
            recent_leads.pop(0)

        # Console notification for immediate attention
        print(f"\n🎯 *** NEW LEAD [{timestamp}] *** ID: {lead_id}")
        print(f"👤 {req.name} | 📱 {req.phone} | 📧 {req.email or 'N/A'}")
        print(f"📍 {req.address_or_zip} | 💬 Prefers: {req.preferred_contact}")
        print(f"📝 {req.project_details}")
        print(f"📞 Contact them at: {req.phone}")
        print(f"✉️  Quick help email: {BUSINESS_EMAIL}")
        print("=" * 60)

        return JSONResponse({
            "ok": True,
            "message": f"Perfect! We'll {req.preferred_contact} you shortly. For urgent needs, call or text {BUSINESS_PHONE}.",
            "lead_id": lead_id,
            "business": BUSINESS_NAME,
            "phone": BUSINESS_PHONE,
            "email": BUSINESS_EMAIL
        })
        
    except Exception as e:
        logger.error(f"Lead submission error: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit lead request")

@app.post("/live-quote")
def request_live_quote(req: LiveQuoteRequest, request: Request):
    """Handle live quote consultation requests"""
    try:
        # Update session for priority callback
        active_sessions[req.session_id] = {
            "created": datetime.now().isoformat(),
            "user_name": req.user_name,
            "phone": req.phone,
            "type": "live_quote_request",
            "status": "callback_requested",
            "service_needed": req.service_needed,
            "last_activity": datetime.now().isoformat(),
            "ip": request.client.host if request.client else "",
            "priority": "high"
        }
        
        # Console notification
        print(f"\n🔥 *** LIVE QUOTE REQUEST *** 🔥")
        print(f"👤 {req.user_name} | 📱 {req.phone}")
        print(f"🔧 Service: {req.service_needed}")
        print(f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("🚨 PRIORITY CALLBACK REQUESTED 🚨")
        print(f"📞 Call them NOW at: {req.phone}")
        print(f"✉️  Quick help email: {BUSINESS_EMAIL}")
        print("=" * 60)
        
        return JSONResponse({
            "success": True,
            "message": f"Got it! We'll call you at {req.phone} within 30 minutes during business hours (Mon-Fri 8AM-6PM).",
            "session_id": req.session_id,
            "estimated_callback": "30 minutes",
            "business_phone": BUSINESS_PHONE,
            "business_email": BUSINESS_EMAIL,
            "business": BUSINESS_NAME
        })
        
    except Exception as e:
        logger.error(f"Live quote request error: {e}")
        raise HTTPException(status_code=500, detail="Failed to request live quote")

@app.get("/admin/data")
def get_admin_data():
    """Get admin dashboard data"""
    return JSONResponse({
        "business_name": BUSINESS_NAME,
        "active_sessions": len(active_sessions),
        "recent_leads": recent_leads[-10:],  # Last 10 leads
        "live_quote_requests": [
            session for session in active_sessions.values() 
            if session.get("type") == "live_quote_request"
        ],
        "total_leads_today": len([
            lead for lead in recent_leads 
            if lead["timestamp"].startswith(datetime.now().strftime("%Y-%m-%d"))
        ]),
        "phone": BUSINESS_PHONE,
        "email": BUSINESS_EMAIL,
        "facebook": FACEBOOK_PAGE,
        "website": WEBSITE
    })

@app.get("/admin/leads")
def get_recent_leads():
    """Get recent leads for admin"""
    return JSONResponse({
        "leads": recent_leads,
        "total": len(recent_leads),
        "business_phone": BUSINESS_PHONE,
        "business_email": BUSINESS_EMAIL
    })

@app.get("/contact-info")
def get_contact_info():
    """Public endpoint for contact information"""
    return JSONResponse({
        "business": BUSINESS_NAME,
        "phone": BUSINESS_PHONE,
        "email": BUSINESS_EMAIL,
        "facebook": FACEBOOK_PAGE,
        "website": WEBSITE,
        "service_area": SERVICE_AREA,
        "quick_help": BUSINESS_EMAIL
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
