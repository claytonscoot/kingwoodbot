from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, File, UploadFile, Form
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
import base64
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

BUSINESS_PHONE = "832-280-5783"
BUSINESS_EMAIL = "admin@kingwoodfencing.com"
FACEBOOK_PAGE = "www.facebook.com/astrooutdoordesigns"
WEBSITE = "astrooutdoordesigns.com"

LEADS_FILE = "leads.csv"
CHAT_SESSIONS_FILE = "chat_sessions.json"

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = "claude-haiku-4-5-20251001"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"

GOOGLE_SHEET_ID = "1RodpbvL75F8AZxutqKcTY_vrbxZvylSBv1SggglPGlM"
GOOGLE_SHEET_RANGE = "Sheet1"

active_sessions: Dict[str, dict] = {}
recent_leads: List[dict] = []


# ----------------------------
# GOOGLE SHEETS HELPER
# ----------------------------
def get_google_token() -> str:
    import google.auth.transport.requests
    from google.oauth2 import service_account
    creds_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not creds_json:
        raise Exception("GOOGLE_SERVICE_ACCOUNT_JSON environment variable not set")
    creds_dict = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def append_lead_to_sheets(lead_row: list):
    try:
        token = get_google_token()
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}/values/{GOOGLE_SHEET_RANGE}:append"
        resp = requests.post(url, headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }, json={"values": [lead_row], "majorDimension": "ROWS"},
        params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}, timeout=10)
        resp.raise_for_status()
        logger.info("✅ Lead saved to Google Sheets")
    except Exception as e:
        logger.error(f"❌ Google Sheets append failed: {e}")


def ensure_sheets_headers():
    try:
        token = get_google_token()
        url = f"https://sheets.googleapis.com/v4/spreadsheets/{GOOGLE_SHEET_ID}/values/{GOOGLE_SHEET_RANGE}!A1"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        data = resp.json()
        if "values" not in data:
            append_lead_to_sheets([
                "timestamp", "ip", "name", "phone", "email",
                "address_or_zip", "preferred_contact", "project_details",
                "session_id", "status"
            ])
            logger.info("📋 Added headers to Google Sheet")
    except Exception as e:
        logger.error(f"Could not check/set sheet headers: {e}")


# ----------------------------
# MODELS
# ----------------------------
class Chat(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=4000)
    session_id: Optional[str] = None
    user_name: Optional[str] = None
    images: Optional[List[str]] = None  # list of base64 encoded images

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
    ensure_sheets_headers()
    if not ANTHROPIC_API_KEY:
        logger.warning("⚠️ ANTHROPIC_API_KEY not set! Chat will not work.")
    else:
        logger.info("✅ Claude AI ready")
    yield
    logger.info("👋 Shutting down...")

app = FastAPI(title=f"{BUSINESS_NAME} - Fence Assistant", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://astrooutdoordesigns.com",
        "https://www.astrooutdoordesigns.com",
        "https://astro-fence-assistant.onrender.com",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except:
    pass


# ----------------------------
# HELPER FUNCTIONS
# ----------------------------
def ensure_leads_file():
    if not os.path.exists(LEADS_FILE):
        with open(LEADS_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "timestamp", "ip", "name", "phone", "email",
                "address_or_zip", "preferred_contact", "project_details",
                "session_id", "status"
            ])
        logger.info(f"📝 Created {LEADS_FILE}")

def get_system_prompt() -> str:
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
- When a customer provides a zip code, NEVER guess or state the city name for that zip. Just confirm: "Great, that zip is in our service area" or ask if it falls outside our known list. Do NOT say "77345 is Conroe" or any zip-to-city mapping — you may be wrong.

CRITICAL — NEVER TURN AWAY A CUSTOMER:
- We install ANY type of fencing. If we can design it, we can install it.
- NEVER tell a customer we don't do a certain type of fence or to call someone else.
- NEVER refer a customer to another contractor or company under any circumstance.
- If you don't know the exact pricing for a specific fence type, say: "That's something our team handles — give us a call or text at {BUSINESS_PHONE} and we'll get you a solid quote." or direct them to {WEBSITE}.
- Always keep the customer engaged with us. The answer is always either a quote, a question to gather more info, or a prompt to call/text us directly.

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
You CAN see photos when customers attach them in this chat. When a customer sends a photo:
- Look at it carefully and describe what you see
- Comment on fence condition, style, damage, materials, height estimate
- Use what you see to give a more accurate estimate
- Say something like: "Thanks for sharing that — I can see [describe what you see]. Based on this..."

NEVER say you cannot view photos or that you need them to send via text/email. You can see them directly in this chat.

We can also receive additional photos via text at {BUSINESS_PHONE} or email at {BUSINESS_EMAIL} if they want to share more.

---------------------------------
PRICING LOGIC (IMPORTANT)
---------------------------------
These are our REAL installed prices based on actual material and labor costs.
Always use these numbers. Apply a ±10% range for site conditions.
Minimum job size: $600.

=================================================================
LABOR PRICING & MINIMUM CHARGE POLICY
=================================================================
CREW LABOR RATE:
- ~$107/crew hour (working rate — sheet says $95 but real rate is higher)
- Crew = lead installer + one helper
- Rate applies regardless of whether owner is present or helping

DAILY LABOR STRUCTURE:
- Half day (4 hours): ~$428 labor
- Full day (8 hours): ~$856 labor
- Labor billed per crew, not per individual

MINIMUM LABOR CHARGE:
- Standard minimum: 4 hours ($428 labor) for most jobs
- EXCEPTION — Small repairs under 1 hour: $125 flat labor + materials
  Examples: replacing 1-3 pickets, swapping a hinge, tightening hardware
- If a job is estimated at ~1 hour but could easily run over, charge the $428 minimum — too close to call
- If confident the job is 38-45 minutes max with no complications, the $125 flat rate is appropriate
- WE make the final approval on all quotes — bot gathers info, we confirm pricing

SMALL REPAIR ASSESSMENT — BOT MUST ASK THESE QUESTIONS before quoting a small repair:
1. How many pickets need replacing? (1-3 = possibly $125 range, more = $428 minimum)
2. Does the new picket need to be trimmed/cut to size?
3. Does it need to be painted or stained to match existing fence?
4. Is there top cap and trim in the way that needs to be removed/reinstalled?
5. Is it board-on-board? (More complex — pickets overlap, harder to remove one without disturbing others)
6. How old is the fence? (Old fence — risk of damaging adjacent pickets or finding weak rails)
7. Are the existing rails still solid? (Weak rails may not hold new fasteners — could turn into a bigger job)
8. Is there electrical nearby or is it close to the house structure? (If yes — $428 minimum, more complexity)
9. Is there anything overhead or blocking access?

If ANY of these add complexity, move to the $428 minimum and explain why to the customer.
The bot should say: "Based on what you've described, I want to make sure we give you an accurate quote — our team will confirm final pricing after reviewing the details."
NEVER promise a $125 rate if there are unknowns — always note that final pricing is confirmed by our team.

MATERIAL PRICING:
- Materials billed separately with standard markup on top of supplier cost
- Material profit is in addition to labor margin
- Always include $75 delivery fee in quotes

HOW TO BUILD A QUOTE:
1. Estimate hours for the job based on scope
2. Multiply hours x $107 = labor cost
3. Add material cost (from supplier pricing above) + markup
4. Add $75 delivery
5. Add complexity factor for difficult jobs (slopes, demo, tight access, custom fab)
6. Minimum total: $600

COMPLEXITY FACTORS (add to labor hours):
- Existing fence demo/removal: add 1-2 hours depending on length
- Sloped or uneven ground: add 1-2 hours
- Tight access (backyard, narrow gate): add 0.5-1 hour
- Custom gate fabrication/welding: add 2-4 hours
- Concrete/hard surface install: add 1-2 hours
- Rocky or root-heavy soil: add 1-3 hours

QUICK LABOR ESTIMATES BY JOB TYPE:
- Replace single picket or small repair: 4 hours minimum ($428 labor)
- Install 50 LF wood fence: ~6-8 hours ($642-$856 labor)
- Install 100 LF wood fence: ~10-14 hours ($1,070-$1,498 labor)
- Install 150 LF wood fence: ~14-18 hours ($1,498-$1,926 labor)
- Single walk gate: 2-3 hours ($214-$321 labor)
- Double drive gate: 3-5 hours ($321-$535 labor)
- Fence staining 100 LF: ~3-5 hours ($321-$535 labor)
- Power washing: ~2-4 hours ($214-$428 labor)

=================================================================
MATERIAL KNOWLEDGE — SUPPLIERS
=================================================================
Primary supplier: Stephens Pipe & Steel (SPSfence.com)
- Phone: (888) 271-2817, Fax: (346) 271-9018
- Local: 4406 Rex Road, Friendswood TX 77546
- Account #: 15737, Sales rep: S. Schultz
- Standard charges: $50 fuel + convenience fee + 8.25% TX tax
- Lead time: most items same day, some WRC items 3-4 days non-stock

Secondary supplier: Antebellum Manufacturing (AntebellumDecorativeFences.com)
- Aluminum decorative fencing — Emily series residential panels
- Limited LIFETIME warranty (original purchaser, non-transferable)
- Powder coat finish warranted for life — never cracks, chips, or peels

Third supplier: Eagle Fence Distributing — Houston (efdistribution.com)
- 14430 Smith Road, Humble TX 77396
- Phone: 281-741-1503, Toll Free: 877-741-4896
- Customer ID: C000001445, Rep: Enrique Zavala
- Delivered by company truck — no separate freight charge
- Payment: COD
- 25% restocking fee on returns, no returns on wood or special orders

=================================================================
WOOD FENCE — CEDAR (WRC = Western Red Cedar)
=================================================================
REAL MATERIAL COSTS (SPS 1/13/2026 quote, Humble TX job):
- Cedar pickets 5/8x6x7' flat #2: $5.76/ea
- Cedar pickets 5/8x6x8' dog ear #2: $5.88/ea
- Pressure treated 2x4x8' rails: $3.76/ea
- Cedar 2x6x14' baseboard: $35.26/ea
- PT 2x12x14' baseboard: $19.55/ea
- Cedar 1x4x14' top cap/trim: $8.25/ea
- Cedar 1x2x14' nailers: $5.31/ea
- Lag screws 1/4"x1-1/2" HDG (100ct): $11.54/box
- Concrete 80lb Sakrete: $5.96/bag

CEDAR FENCE INSTALLED PRICING:
- Standard 6' privacy (wood posts): ~$35–43/LF installed
- Board-on-board 6': add $1.50–2.00/LF (uses ~2.5x more pickets)
- Top cap & trim both sides: add $1.50/LF
- 7' tall with 2x12 baseboard: add $1.00/LF
- 8' tall: add $2.00/LF
- Steel posts (2-1/2" galvanized): add $6–8/LF
- Cedar is premium — lasts longer than pine in Houston humidity

=================================================================
WOOD FENCE — PINE (PTP = Pressure Treated Pine)
=================================================================
- ~10–15% less than cedar equivalent
- Approx $30–37/LF installed
- Good budget option — still solid quality

=================================================================
TEAR-OUT / DEMO
=================================================================
- $2.00/LF to remove existing fence
- Always ask if there is an existing fence to remove

=================================================================
DELIVERY
=================================================================
- $75 flat fee — include in all quotes

=================================================================
WOOD GATES
=================================================================
- Wood frame walk gate (36"): $350–450 installed
- Steel frame walk gate: $375–500 installed
- Double drive gate wood frame: $750–1,100 installed
- Double drive gate steel frame: $900–1,300 installed
- Code lock: add $50
- Custom weld fabrication: add $150–300

=================================================================
STAINING — Wood Defender Semi-Transparent
=================================================================
- Spray staining: ~$0.86/sq ft
- Hand staining: ~$1.00/LF
- Always upsell staining on every wood fence job

=================================================================
VINYL FENCE — BUFFTECH (White & Colors)
=================================================================
PRODUCT KNOWLEDGE:
- We install BUFFTECH vinyl fencing — premium brand, sold through Eagle Fence Distributing
- BUFFTECH is a 3-rail large rail system — stronger than standard vinyl
- Posts: 5x5x84" (7ft post) — large rail line, corner, and end posts all same price
- Rails: 2x6x192" ribbed sections (minimum order 25 pcs) — 16ft rail lengths
- Flat post caps: 5x5" included
- Lock rings: 2 per rail, order in 24ct packs (black)
- White is standard color — other colors available (special order)
- System designed for 3-rail installations

REAL MATERIAL COSTS (Eagle Fence 6/14/2024 quote):
- Flat cap ext 5x5" white: $3.94/ea
- 2x6x192" ribbed rail white (min 25): $53.20/ea — this is a 16ft rail section
- Lock rings black 24ct pack: $6.70/pack (2 per rail)
- Large rail line post 5x5x84" white (3-rail): $37.29/ea
- Large rail corner post 5x5x84" white (3-rail): $37.29/ea
- Large rail end post 5x5x84" white (3-rail): $37.29/ea
Note: This was a large job — 58 caps, 87 rails, 54 line posts, 2 corner, 2 end posts
Total material on that job: $7,073 before tax ($7,657 with tax)

VINYL FENCE INSTALLED PRICING:
- Standard 3-rail vinyl privacy 6': ~$38–52/LF installed (white)
- Premium vinyl 6' with large rail system: ~$45–60/LF installed
- Vinyl 4' (ranch/picket style): ~$28–40/LF installed
- Color vinyl (tan, clay, almond): add $3–5/LF (special order premium)
- Vinyl gate walk: ~$450–650 installed
- Vinyl double drive gate: ~$900–1,400 installed
- Low maintenance — never needs staining, painting, or sealing
- Great for HOA neighborhoods — clean look, long lasting
- Always ask: height, color preference (white standard), gate needs

=================================================================
CHAIN LINK — GALVANIZED
=================================================================
- 4' galvanized: ~$18–24/LF installed
- 5' galvanized: ~$20–26/LF installed
- 6' galvanized: ~$22–28/LF installed
- 8' commercial: ~$28–36/LF installed

=================================================================
CHAIN LINK — BLACK VINYL COATED (BLK PLY)
=================================================================
REAL MATERIAL COSTS (SPS 12/26/2025 quote, New Caney job):
- 3" black vinyl post 10'6" PP40: $65.15/ea
- 3" dome post cap: $4.47/ea
- 72" drop rod assembly w/guides: $33.90/ea
- Duck bill gate keeper: $14.38/ea
- Tension bar 72"x3/4": $6.00/ea
- Tension band 3": $4.21/ea
- Brace band 3": $2.73/ea
- Bolts & nuts 5/16x1-1/4" Ruspert: $0.16/ea
- Rail end combo 1-5/8": $3.17/ea
- Top rail 1-5/8"x21' PP20: $2.18/ft
- Aluminum fence ties 9ga: $0.22/ea
- Concrete 80lb: $5.96/bag
- Industrial double drive gate 16'Wx6'H black vinyl SP20: $1,111.06/ea (material only)

CHAIN LINK HARDWARE — BLACK (Eagle Fence Distributing, 1/28/2026 quote):
- Tension bars 3/16x3/4x70" black: $8.65/ea
- Tension band 3/4x1-5/8" black: $1.06/ea
- Drop rod 84" assembly black: $66.94/ea (for double drive gates)
- Drop rod guide 1-5/8" IND black: $4.42/ea
- Carriage bolt 3/8x3" black: $0.50/ea
- Aluminum fence ties 9ga x 8-1/4" black: $0.18/ea (100ct)
- EF-40 3"x10'6" post black PC: $76.20/ea (heavy duty 3" post)
- 3" PS dome cap black: $3.62/ea
- 1-5/8" PS dome cap black: $1.45/ea
- Spray paint black 12oz: $7.93/can (touch up)
- 3" 180° PS offset hinge black: $26.46/ea (heavy duty gate hinge)
Note: Eagle Fence delivered by company truck to Kingwood, no freight charge

BLACK VINYL CHAIN LINK INSTALLED PRICING:
- 4' black vinyl: ~$24–30/LF installed
- 6' black vinyl: ~$28–36/LF installed
- 8' black vinyl commercial: ~$36–48/LF installed
- Black vinyl double drive gate 16'x6': ~$2,200–2,800 installed (material alone ~$1,111)
- Black vinyl is premium look — great for commercial, pools, HOAs

=================================================================
ALUMINUM / ORNAMENTAL — EMILY SERIES (Antebellum + SPS)
=================================================================
PRODUCT KNOWLEDGE:
- We install Emily series aluminum panels — residential 2-rail system
- Panel: 71.5" notch-to-notch width, smooth or rake bottom
- Pickets: 5/8" sq x .045" screwed to 1"x1" channels
- Posts punched to receive rails — clean professional assembly
- Optional: butterfly scrolls on every picket (decorative upgrade)
- Available: smooth bottom (standard) or rake bottom (for slopes)
- Single walk gate: arched or straight rail, opening ~48"
- Double walk gate: straight rail, opening ~72"
- Gate uprights: 2" sq x .093"
- LIFETIME WARRANTY on powder coat — never cracks, chips, or peels (original owner)

REAL MATERIAL COSTS (SPS 2/19/2026 quote):
- 42"H x 6'W Emily panel 2-rail smooth black: $85.71/ea
- 2" sq .093 post x 45": $36.00/ea (standard line post)
- 2" sq .125 post x 45": $44.25/ea (heavy duty — gate hinge posts)
- 2" sq x 7' post: $48.00/ea (taller installs or deep set)
- 2" modern post cap: $1.74/ea
- Walk gate 42"H x 4'W Emily 2-rail: $256.51/ea
- Double gate 42"H x 42"W Emily 2-rail: $245.00/ea
- Weld charge: $19.86/ea
- Floor mount cover plate: $19.24/ea
- TRU-CLOSE 2-leg hinge for metal: $36.48/pair
- Stainless steel gravity latch: $17.19/ea
- Wedge anchor bolt 3/8"x3-3/4": $1.30/ea
- Concrete 80lb Sakrete: $5.96/bag

ALUMINUM INSTALLED PRICING:
- 42" (3'6") aluminum black: ~$45–60/LF installed
- 48" (4') aluminum: ~$50–65/LF installed
- 60" (5') aluminum: ~$55–70/LF installed
- 72" (6') aluminum: ~$65–80/LF installed
- Walk gate 42"H installed: ~$650–850
- Double drive gate 42"H installed: ~$950–1,400
- Hard surface install (flange plates on concrete/pavers): add $8–12/LF
- Custom weld fabrication: add $150–300
- Black standard — other colors special order
- Great for pools, front yards, HOA communities

ALUMINUM / ORNAMENTAL FENCING (installed):
- We absolutely install aluminum and ornamental iron fencing. Do NOT turn away these customers.
- We source aluminum fencing from Stephens Pipe & Steel (SPSfence.com) — quality commercial supplier.

ALUMINUM PRODUCT KNOWLEDGE (from real supplier invoices):
- We install "Emily" series aluminum panels — smooth bottom rail, available in black
- Standard panel: 42"H (3'6") x 6'W, 2-rail, smooth bottom — this is our most common residential height
- Posts: 2" square steel posts, .093 wall (standard) or .125 wall (heavy duty hinge posts for gates)
- Post caps: 2" modern post cap included
- Gate hardware: TRU-CLOSE self-closing hinges, stainless steel gravity latch
- Gate panels: pre-built gate sections available (42"H x 4'W single, 42"H x 42"W double)
- Concrete: Sakrete 80lb bags for post setting
- Welded flange base plates available for hard surface installs (concrete/pavers)
- Weld charges apply for custom gate fabrication

ALUMINUM MATERIAL COSTS (from 2/19/2026 SPS quote — update periodically):
- 42"H x 6'W panel (Emily 2-rail smooth): ~$85.71/panel
- 2" sq .093 post x 45": ~$36.00 each (standard line post)
- 2" sq .125 post x 45": ~$44.25 each (heavy duty — use for gate hinge posts)
- 2" sq x 7' post: ~$48.00 each (use for taller installs or deep set)
- 2" modern post cap: ~$1.74 each
- Walk gate panel 42"H x 4'W (Emily 2-rail): ~$256.51 each
- Double gate panel 42"H x 42"W (Emily 2-rail): ~$245.00 each
- Weld charge per item: ~$19.86
- Floor mount cover plate: ~$19.24 each
- TRU-CLOSE 2-leg hinge (metal): ~$36.48/pair
- Stainless steel gravity latch: ~$17.19 each
- Wedge anchor bolt 3/8"x3-3/4": ~$1.30 each
- Sakrete concrete 80lb: ~$5.96/bag
- SPS fuel charge: ~$50 flat, convenience fee ~$28.96, 8.25% TX tax applies

ALUMINUM PRICING GUIDE (installed, labor + materials):
- 42" (3'6") aluminum panel fence: ~$45–60/LF installed (black, standard residential)
- 48" (4') aluminum: ~$50–65/LF installed
- 60" (5') aluminum: ~$55–70/LF installed  
- 72" (6') aluminum: ~$65–80/LF installed
- Aluminum walk gate (42"H, single): ~$650–850 installed (includes panel, posts, hardware, labor)
- Aluminum double drive gate (42"H): ~$950–1,400 installed
- Custom welded gate: add $150–300 for weld fabrication
- Hard surface install (concrete/pavers): add $8–12/LF for flange plates

ALUMINUM NOTES:
- Black is standard color — other colors available but require special order
- Low maintenance, rust-resistant, HOA friendly, great for pools and front yards
- Popular styles: flat top (Emily series), spear top, french gothic (ask customer preference)
- Always ask: height needed, color preference (black standard), gate quantity and size
- Minimum job: $600

HOW TO CALCULATE A QUOTE:
1. Start with base LF price
2. Add any applicable add-ons
3. Add tear-out if replacing old fence ($2/LF)
4. Add $75 delivery
5. Add gate costs
6. Show the math clearly
7. Label as working estimate — final price confirmed after site visit

Always explain: Flat yard assumed. Normal access assumed. Final quote confirmed after site visit or photos.

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

def call_claude(user_message: str, history: list = None, images: list = None) -> str:
    """Call Claude API with conversation history and optional images for vision"""
    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json"
    }
    # Use claude-sonnet for vision (haiku vision quality is poor), fall back to haiku for text-only
    model = "claude-sonnet-4-5" if images else CLAUDE_MODEL

    def build_content(text, imgs):
        """Build a Claude message content block — text only or text+images"""
        if not imgs:
            return text
        content_blocks = []
        for img_b64 in imgs:
            media_type = "image/jpeg"
            if img_b64.startswith("data:"):
                header, img_b64 = img_b64.split(",", 1)
                if "png" in header: media_type = "image/png"
                elif "webp" in header: media_type = "image/webp"
                elif "gif" in header: media_type = "image/gif"
            content_blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": img_b64}
            })
        content_blocks.append({"type": "text", "text": text})
        return content_blocks

    messages = []
    if history:
        for msg in history[:-1]:
            if msg["type"] == "user":
                msg_images = msg.get("images", [])
                messages.append({
                    "role": "user",
                    "content": build_content(msg["message"], msg_images)
                })
            elif msg["type"] == "assistant":
                messages.append({"role": "assistant", "content": msg["message"]})

    # Current message with images
    messages.append({
        "role": "user",
        "content": build_content(user_message, images or [])
    })

    payload = {
        "model": model,
        "max_tokens": 600,
        "system": get_system_prompt(),
        "messages": messages
    }
    response = requests.post(ANTHROPIC_API_URL, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    data = response.json()
    return data["content"][0]["text"].strip()

def generate_session_id() -> str:
    return str(uuid.uuid4())

def send_brevo_email(subject: str, text_content: str, attachments: list = None, notify_email: str = None):
    """Shared Brevo email sender"""
    brevo_api_key = os.getenv("BREVO_API_KEY")
    if not brevo_api_key:
        logger.warning("BREVO_API_KEY not set — skipping email notification.")
        return
    if notify_email is None:
        notify_email = os.getenv("LEAD_NOTIFY_EMAIL", "admin@astrooutdoordesigns.com")
    from_email = os.getenv("BREVO_FROM_EMAIL", "forms@astrooutdoordesigns.com")
    payload = {
        "sender": {"name": BUSINESS_NAME, "email": from_email},
        "to": [{"email": notify_email}],
        "subject": subject,
        "textContent": text_content,
    }
    if attachments:
        payload["attachment"] = attachments
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"accept": "application/json", "api-key": brevo_api_key, "content-type": "application/json"},
        json=payload, timeout=15
    )
    r.raise_for_status()
    logger.info(f"📧 Brevo email sent: {subject}")


# ----------------------------
# API ROUTES
# ----------------------------
@app.get("/")
def serve_frontend():
    return FileResponse("chat.html")

@app.get("/admin")
def serve_admin():
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
    prompt = req.prompt.strip()
    session_id = req.session_id or generate_session_id()

    if session_id not in active_sessions:
        active_sessions[session_id] = {
            "created": datetime.now().isoformat(),
            "user_name": req.user_name or "Visitor",
            "message_count": 0,
            "last_activity": datetime.now().isoformat(),
            "ip": request.client.host if request.client else "",
            "messages": []
        }

    active_sessions[session_id]["last_activity"] = datetime.now().isoformat()
    active_sessions[session_id]["message_count"] += 1
    active_sessions[session_id]["messages"].append({
        "timestamp": datetime.now().isoformat(),
        "type": "user",
        "message": prompt,
        "images": req.images or []  # store images with message for history replay
    })

    if not prompt:
        fallback_response = "Quick question — what are you trying to build or fix? (Approximate feet + height helps a lot.)"
        active_sessions[session_id]["messages"].append({
            "timestamp": datetime.now().isoformat(),
            "type": "assistant",
            "message": fallback_response
        })
        return {"response": fallback_response, "session_id": session_id, "business": BUSINESS_NAME}

    try:
        ai_response = call_claude(prompt, active_sessions[session_id]["messages"], images=req.images)
        if not ai_response:
            ai_response = "Got it. What's the approximate length (feet) and desired height (6ft/7ft/8ft)? Any gates needed?"

        if any(word in prompt.lower() for word in ['quote', 'price', 'cost', 'estimate', 'schedule', 'when', 'how much']):
            ai_response += f"\n\n📞 **Ready to move forward?** Call or text us at **{BUSINESS_PHONE}** or email **{BUSINESS_EMAIL}** for fastest response!"

        if any(word in prompt.lower() for word in ['facebook', 'social', 'reviews', 'find you']):
            ai_response += f"\n\n📱 **Find us on Facebook:** {FACEBOOK_PAGE} or search for us on Google!"

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
        active_sessions[session_id]["messages"].append({"timestamp": datetime.now().isoformat(), "type": "assistant", "message": timeout_response})
        return {"response": timeout_response, "session_id": session_id}

    except Exception as e:
        error_response = f"Something hiccupped on our side. Call or text us at {BUSINESS_PHONE} or email {BUSINESS_EMAIL} for immediate assistance."
        logger.error(f"Chat error: {e}")
        active_sessions[session_id]["messages"].append({"timestamp": datetime.now().isoformat(), "type": "assistant", "message": error_response})
        return {"response": error_response, "session_id": session_id}


@app.post("/lead")
def submit_lead(req: Lead, request: Request):
    try:
        ip = request.client.host if request.client else ""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lead_id = str(uuid.uuid4())[:8]

        try:
            with open(LEADS_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([timestamp, ip, req.name.strip(), req.phone.strip(),
                    (req.email.strip() if req.email else ""), req.address_or_zip.strip(),
                    req.preferred_contact.strip(), req.project_details.strip(), lead_id, "new"])
        except Exception as csv_err:
            logger.warning(f"CSV write failed: {csv_err}")

        try:
            append_lead_to_sheets([timestamp, ip, req.name.strip(), req.phone.strip(),
                (req.email.strip() if req.email else ""), req.address_or_zip.strip(),
                req.preferred_contact.strip(), req.project_details.strip(), lead_id, "new"])
        except Exception as sheets_err:
            logger.error(f"Google Sheets write failed: {sheets_err}")

        lead_data = {
            "id": lead_id, "timestamp": timestamp, "name": req.name.strip(),
            "phone": req.phone.strip(), "email": req.email.strip() if req.email else "",
            "area": req.address_or_zip.strip(), "preferred_contact": req.preferred_contact,
            "details": req.project_details.strip(), "status": "new"
        }
        recent_leads.append(lead_data)
        if len(recent_leads) > 50:
            recent_leads.pop(0)

        print(f"\n🎯 *** NEW LEAD [{timestamp}] *** ID: {lead_id}")
        print(f"👤 {req.name} | 📱 {req.phone} | 📧 {req.email or 'N/A'}")
        print(f"📍 {req.address_or_zip} | 💬 Prefers: {req.preferred_contact}")
        print(f"📝 {req.project_details}")
        print("=" * 60)

        try:
            send_brevo_email(
                subject=f"🔥 New Fence Lead - {req.name}",
                text_content=(
                    f"New Lead Received:\n\n"
                    f"Name: {req.name}\nPhone: {req.phone}\nEmail: {req.email or 'N/A'}\n"
                    f"Area: {req.address_or_zip}\nPreferred Contact: {req.preferred_contact}\n\n"
                    f"Project Details:\n{req.project_details}\n\n"
                    f"Timestamp: {timestamp}\nLead ID: {lead_id}\n"
                )
            )
        except Exception as email_error:
            logger.error(f"Brevo email send failed: {email_error}")

        return JSONResponse({
            "ok": True,
            "message": f"Perfect! We'll {req.preferred_contact} you shortly. For urgent needs, call or text {BUSINESS_PHONE}.",
            "lead_id": lead_id, "business": BUSINESS_NAME,
            "phone": BUSINESS_PHONE, "email": BUSINESS_EMAIL
        })

    except Exception as e:
        logger.error(f"Lead submission error: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit lead request")


@app.post("/lead-with-photos")
async def submit_lead_with_photos(
    request: Request,
    name: str = Form(...),
    phone: str = Form(default=""),
    email: str = Form(default=""),
    address_or_zip: str = Form(default=""),
    preferred_contact: str = Form(default="text"),
    project_details: str = Form(...),
    photo_0: UploadFile = File(default=None),
    photo_1: UploadFile = File(default=None),
    photo_2: UploadFile = File(default=None),
    photo_3: UploadFile = File(default=None),
    photo_4: UploadFile = File(default=None),
):
    """Handle lead form submissions with optional photo attachments"""
    try:
        ip = request.client.host if request.client else ""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lead_id = str(uuid.uuid4())[:8]

        # Collect uploaded photos and encode as base64 for Brevo
        photos = []
        for photo in [photo_0, photo_1, photo_2, photo_3, photo_4]:
            if photo and photo.filename:
                content = await photo.read()
                if content:
                    photos.append({
                        "filename": photo.filename,
                        "content_type": photo.content_type or "image/jpeg",
                        "data": base64.b64encode(content).decode("utf-8")
                    })

        details_with_note = project_details.strip() + (f" [{len(photos)} photo(s) attached]" if photos else "")

        try:
            with open(LEADS_FILE, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([timestamp, ip, name.strip(), phone.strip(),
                    email.strip(), address_or_zip.strip(), preferred_contact.strip(),
                    details_with_note, lead_id, "new"])
        except Exception as csv_err:
            logger.warning(f"CSV write failed: {csv_err}")

        try:
            append_lead_to_sheets([timestamp, ip, name.strip(), phone.strip(),
                email.strip(), address_or_zip.strip(), preferred_contact.strip(),
                details_with_note, lead_id, "new"])
        except Exception as sheets_err:
            logger.error(f"Google Sheets write failed: {sheets_err}")

        recent_leads.append({
            "id": lead_id, "timestamp": timestamp, "name": name.strip(),
            "phone": phone.strip(), "email": email.strip(), "area": address_or_zip.strip(),
            "preferred_contact": preferred_contact, "details": project_details.strip(),
            "photos": len(photos), "status": "new"
        })
        if len(recent_leads) > 50:
            recent_leads.pop(0)

        print(f"\n🎯 *** NEW LEAD WITH PHOTOS [{timestamp}] *** ID: {lead_id}")
        print(f"👤 {name} | 📱 {phone} | 📧 {email or 'N/A'}")
        print(f"📷 Photos: {len(photos)} | 📝 {project_details}")
        print("=" * 60)

        try:
            photo_note = f"\n\n📷 {len(photos)} photo(s) attached to this email." if photos else ""
            send_brevo_email(
                subject=f"🔥 New Fence Lead{' 📷 +Photos' if photos else ''} - {name}",
                text_content=(
                    f"New Lead Received:\n\n"
                    f"Name: {name}\nPhone: {phone}\nEmail: {email or 'N/A'}\n"
                    f"Area: {address_or_zip}\nPreferred Contact: {preferred_contact}\n\n"
                    f"Project Details:\n{project_details}{photo_note}\n\n"
                    f"Timestamp: {timestamp}\nLead ID: {lead_id}\n"
                ),
                attachments=[{"content": p["data"], "name": p["filename"]} for p in photos] if photos else None
            )
        except Exception as email_error:
            logger.error(f"Brevo email send failed: {email_error}")

        return JSONResponse({
            "ok": True,
            "message": f"Perfect! We'll {preferred_contact} you shortly. For urgent needs, call or text {BUSINESS_PHONE}.",
            "lead_id": lead_id, "photos_received": len(photos),
            "business": BUSINESS_NAME, "phone": BUSINESS_PHONE, "email": BUSINESS_EMAIL
        })

    except Exception as e:
        logger.error(f"Lead with photos submission error: {e}")
        raise HTTPException(status_code=500, detail="Failed to submit lead request")


@app.post("/live-quote")
def request_live_quote(req: LiveQuoteRequest, request: Request):
    try:
        active_sessions[req.session_id] = {
            "created": datetime.now().isoformat(),
            "user_name": req.user_name, "phone": req.phone,
            "type": "live_quote_request", "status": "callback_requested",
            "service_needed": req.service_needed,
            "last_activity": datetime.now().isoformat(),
            "ip": request.client.host if request.client else "",
            "priority": "high"
        }
        print(f"\n🔥 *** LIVE QUOTE REQUEST *** 🔥")
        print(f"👤 {req.user_name} | 📱 {req.phone}")
        print(f"🔧 Service: {req.service_needed}")
        print(f"📞 Call them NOW at: {req.phone}")
        print("=" * 60)
        return JSONResponse({
            "success": True,
            "message": f"Got it! We'll call you at {req.phone} within 30 minutes during business hours (Mon-Fri 8AM-6PM).",
            "session_id": req.session_id, "estimated_callback": "30 minutes",
            "business_phone": BUSINESS_PHONE, "business_email": BUSINESS_EMAIL, "business": BUSINESS_NAME
        })
    except Exception as e:
        logger.error(f"Live quote request error: {e}")
        raise HTTPException(status_code=500, detail="Failed to request live quote")


@app.get("/admin/data")
def get_admin_data():
    return JSONResponse({
        "business_name": BUSINESS_NAME,
        "active_sessions": len(active_sessions),
        "recent_leads": recent_leads[-10:],
        "live_quote_requests": [s for s in active_sessions.values() if s.get("type") == "live_quote_request"],
        "total_leads_today": len([l for l in recent_leads if l["timestamp"].startswith(datetime.now().strftime("%Y-%m-%d"))]),
        "phone": BUSINESS_PHONE, "email": BUSINESS_EMAIL,
        "facebook": FACEBOOK_PAGE, "website": WEBSITE
    })

@app.get("/admin/leads")
def get_recent_leads():
    return JSONResponse({"leads": recent_leads, "total": len(recent_leads),
        "business_phone": BUSINESS_PHONE, "business_email": BUSINESS_EMAIL})

@app.get("/contact-info")
def get_contact_info():
    return JSONResponse({
        "business": BUSINESS_NAME, "phone": BUSINESS_PHONE,
        "email": BUSINESS_EMAIL, "facebook": FACEBOOK_PAGE,
        "website": WEBSITE, "service_area": SERVICE_AREA,
        "quick_help": BUSINESS_EMAIL
    })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))
