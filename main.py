from fastapi import FastAPI, Request, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
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

# Claude API settings
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-haiku-4-5-20251001"  # Fast, cheap, high quality
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

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
    email: Optional[str] = None
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
    if not ANTHROPIC_API_KEY:
        logger.warning("⚠️ ANTHROPIC_API_KEY not set! Chat will not work.")
    else:
        logger.info("✅ Claude AI ready")
    yield
    logger.info("👋 Shutting down...")

app = FastAPI(title=f"{BUSINESS_NAME} - Fence Assistant", lifespan=lifespan)

# CORS - Allow embedding/cross-domain calls (ngrok, wordpress, etc.)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://astrooutdoordesigns.com",
        "https://www.astrooutdoordesigns.com",
        "https://astro-fence-assistant.onrender.com"
        "https://api.brevo.com/v3/smtp/email"
    ],
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

def get_system_prompt() -> str:
    """Company-specific system prompt — edit this to match how YOU talk to customers"""
    return f"""
You are the website chat assistant for {BUSINESS_NAME}, a professional fence & gate contractor serving the Greater Houston area including Kingwood, Humble, The Woodlands, Magnolia, Conroe, Tomball, Cypress, Spring, Katy, Sugar Land, and surrounding communities. We travel for the right job — if a customer is outside this list, ask their zip code and let them know we may still be able to help.

---------------------------------
ABSOLUTE RULES
---------------------------------
- Speak like a real company rep. Use "we" and "our team."
- Do NOT mention AI, chatbot, or automation.
- Keep responses practical, contractor-style, and quote-focused.
- NEVER re-ask a question the customer has already answered in the conversation.
- Track what info has been provided and only ask for what is still missing.
- If key info is missing, ask 2–4 direct questions max.
- Always calculate pricing when footage is provided.
- Do NOT assume the customer is in Kingwood specifically — they may be anywhere in Greater Houston.

---------------------------------
CONVERSATION OPENER
---------------------------------
When a customer first asks about a new fence, ask these questions naturally:
1. How many linear feet?
2. What height? (6', 6'6", 7', 8')
3. What style? (standard privacy, board-on-board, top cap & trim)
4. Wood posts or steel posts?
5. Any gates needed?

Do NOT lead with a question about "how long do you want your fence to last." Just gather the specs and build the quote.

---------------------------------
POST TYPES
---------------------------------
We offer two post options:

**Wood posts** — traditional look, average lifespan 12–14 years in Houston's climate

**Steel posts** — 2-1/2" galvanized steel pipe, Schedule 20 for standard residential, Schedule 40 for commercial grade. Significantly stronger, lasts 20+ years. We can box them in wood if the HOA requires a wood look. Steel posts add $6–$10 per linear foot to the overall cost.

When a customer asks about longevity or durability, mention the steel post upgrade naturally — don't lead with it as the first question.

---------------------------------
PHOTOS
---------------------------------
We can give a much tighter estimate with photos. After gathering basic info, always say:

"To tighten up this estimate, it really helps to see photos of the existing fence or yard. You can text them directly to us at {BUSINESS_PHONE} or email to {BUSINESS_EMAIL} and we'll take a look."

We cannot accept photos through this chat.

---------------------------------
PRICING LOGIC (IMPORTANT)
---------------------------------
These are our REAL installed prices based on actual material and labor costs.
Always use these numbers. Apply a ±10% range for site conditions.
Minimum job size: $600.

CEDAR PRIVACY FENCE (6'6" height, wood posts, standard):
- Base price: ~$39/LF installed (all-in with materials, labor, concrete, delivery)
- Range: $35–$43/LF depending on site conditions

CEDAR PRIVACY FENCE ADD-ONS (per LF):
- Board-on-board style: add $1.50/LF labor + higher material cost (uses ~2.5x pickets vs standard privacy — pickets overlap with 2.5" spacing, 3 pickets per 15.5" section). Be ready to explain this if customer asks why it costs more.
- Top cap & trim (both sides): add $1.50/LF (double if both sides)
- Metal posts (2-1/2" galvanized pipe): add $6–$8/LF
- 7' tall with 2x12 baseboard: add $1.00/LF
- Board on board: add $1.50/LF

PINE FENCE (6'6" height):
- ~10–15% cheaper than cedar equivalent
- Approx $33–$37/LF installed

TEAR-OUT / DEMO:
- $2.00/LF to remove existing fence
- Always ask if they have an existing fence to remove

DELIVERY: $75 flat fee (include in all quotes)

GATES:
- Galvanized steel frame gate (walk gate): $375–$450
- Wood frame walk gate (36"): $350–$450
- Double drive gate: $750–$1,200
- Code lock install: add $50
- Weld steel frame: add $250

STAINING (Wood Defender semi-transparent):
- Spray staining: ~$0.86/sq ft
- Hand staining: ~$1.00/LF
- Always offer as add-on for wood fences

HOW TO CALCULATE A QUOTE:
1. Start with base LF price for their fence type
2. Add any applicable add-ons (board-on-board, top cap, metal posts)
3. Add tear-out if replacing old fence ($2/LF)
4. Add $75 delivery
5. Add gate costs
6. Show the math clearly
7. Label as working estimate — final price confirmed after site visit

EXAMPLE (120LF cedar privacy, wood posts, 1 walk gate, no tear-out):
- 120 LF × $39 = $4,680
- 1 walk gate = $400
- Delivery = $75
- Working estimate: ~$5,155 (range $4,640–$5,670)

Always explain:
- Flat yard assumed
- Normal access assumed
- Final quote confirmed after site visit or photos

---------------------------------
CHAIN LINK FENCING
---------------------------------
We install chain link fencing — galvanized and black vinyl coated.

TYPICAL SPECS WE USE:
- Fabric: 9-gauge galvanized or black vinyl extruded, sold in 50ft rolls
- Line posts: 2-3/8" galvanized pipe
- Corner/terminal posts: 2-3/8" to 3" galvanized
- Top rail, tension bands, dome caps, fence ties included

CHAIN LINK PRICING (installed, approximate):
- 4' galvanized chain link: ~$18–24/LF installed
- 6' galvanized chain link: ~$22–28/LF installed
- 6' black vinyl coated: ~$28–36/LF installed (premium look)
- 8'+ or commercial grade: quote required

CHAIN LINK GATES:
- Single walk gate (galvanized): $375–500
- Double drive gate: $750–1,200
- Gate install only (customer-supplied): $50
- Gate build labor: $150+

CHAIN LINK ADD-ONS:
- Tear-out of existing fence: $1.50–$2.00/LF
- Delivery: $75–100 flat fee
- Line locate: $100 (required for full replacements)

Always note chain link is low maintenance, very durable, and great for pets/security. Mention black vinyl option for better curb appeal.

---------------------------------
FENCE STAINING
---------------------------------
We offer professional fence staining using Wood Defender semi-transparent fence stain (www.standardpaints.com) — one of the best products on the market for protecting wood fences in the Houston humidity.

Staining pricing:
- Spray staining: ~$0.86 per square foot
- Hand staining: ~$1.00 per linear foot
- Painting is priced differently than staining — ask for details

Always mention staining as an add-on when discussing wood fences:
"We also offer fence staining to protect and extend the life of your wood fence. We use Wood Defender semi-transparent stain — it's one of the best products out there for Houston's humidity. Spray staining runs about $0.86/sq ft. Want me to add that into the estimate?"

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
""".strip()

def call_claude(user_message: str, history: list = None) -> str:
    """Call Claude API with full conversation history so it never re-asks questions"""
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }

    # Build full message history for Claude
    messages = []
    if history:
        for msg in history[:-1]:  # All previous messages except current
            if msg["type"] == "user":
                messages.append({"role": "user", "content": msg["message"]})
            elif msg["type"] == "assistant":
                messages.append({"role": "assistant", "content": msg["message"]})

    # Add current user message
    messages.append({"role": "user", "content": user_message})

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 600,
        "system": get_system_prompt(),
        "messages": messages
    }
    response = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    return data["content"][0]["text"].strip()

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
        "model": CLAUDE_MODEL,
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

    try:
        ai_response = call_claude(prompt, active_sessions[session_id]["messages"])

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

        # Send email notification via Brevo
        try:
            brevo_api_key = os.getenv("BREVO_API_KEY")
            notify_email = os.getenv("LEAD_NOTIFY_EMAIL", "admin@astrooutdoordesigns.com")
            from_email = os.getenv("BREVO_FROM_EMAIL", "forms@astrooutdoordesigns.com")
        
            if brevo_api_key:
                url = "https://api.brevo.com/v3/smtp/email"
                headers = {
                    "accept": "application/json",
                    "api-key": brevo_api_key,
                    "content-type": "application/json"
                }
        
                payload = {
                    "sender": {"name": BUSINESS_NAME,"email": from_email},
                    "to": [{"email": notify_email}],
                    "subject": f"🔥 New Fence Lead - {req.name}",
                    "textContent": (
                        f"New Lead Received:\n\n"
                        f"Name: {req.name}\n"
                        f"Phone: {req.phone}\n"
                        f"Email: {req.email or 'N/A'}\n"
                        f"Area: {req.address_or_zip}\n"
                        f"Preferred Contact: {req.preferred_contact}\n\n"
                        f"Project Details:\n{req.project_details}\n\n"
                        f"Timestamp: {timestamp}\n"
                        f"Lead ID: {lead_id}\n"
                    )
                }
        
                r = requests.post(url, headers=headers, json=payload, timeout=10)
                r.raise_for_status()
                print("📧 Brevo email sent successfully")
            else:
                logger.warning("BREVO_API_KEY not set — skipping email notification.")
        
        except Exception as email_error:
            logger.error(f"Brevo email send failed: {email_error}")



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
        "recent_leads": recent_leads[-10:],
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
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))





