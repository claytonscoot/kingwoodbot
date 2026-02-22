"""
Xero Integration for Astro Outdoor Designs
Handles OAuth2 auth, contact creation, project creation, and quote generation
from chat session data.
"""

import os
import json
import requests
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ----------------------------
# XERO CONFIG
# ----------------------------
XERO_CLIENT_ID = os.getenv("XERO_CLIENT_ID", "")
XERO_CLIENT_SECRET = os.getenv("XERO_CLIENT_SECRET", "")
XERO_REDIRECT_URI = os.getenv("XERO_REDIRECT_URI", "https://astro-fence-assistant.onrender.com/xero/callback")
XERO_SCOPES = "openid profile email accounting.contacts accounting.transactions projects offline_access"

XERO_TOKEN_FILE = "xero_token.json"  # stored on disk between restarts

# ----------------------------
# TOKEN MANAGEMENT
# ----------------------------
def save_token(token_data: dict):
    """Save token to disk"""
    try:
        with open(XERO_TOKEN_FILE, "w") as f:
            json.dump(token_data, f)
        logger.info("✅ Xero token saved")
    except Exception as e:
        logger.error(f"Token save error: {e}")

def load_token() -> Optional[dict]:
    """Load token from disk"""
    try:
        if os.path.exists(XERO_TOKEN_FILE):
            with open(XERO_TOKEN_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Token load error: {e}")
    return None

def refresh_access_token(token_data: dict) -> Optional[dict]:
    """Refresh expired access token using refresh token"""
    try:
        resp = requests.post(
            "https://identity.xero.com/connect/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token_data["refresh_token"],
                "client_id": XERO_CLIENT_ID,
                "client_secret": XERO_CLIENT_SECRET,
            },
            timeout=15
        )
        resp.raise_for_status()
        new_token = resp.json()
        new_token["saved_at"] = datetime.now().isoformat()
        save_token(new_token)
        logger.info("✅ Xero token refreshed")
        return new_token
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        return None

def get_valid_token() -> Optional[dict]:
    """Get a valid token, refreshing if needed"""
    token = load_token()
    if not token:
        return None
    # Check if expired (access tokens last 30 min)
    saved_at = datetime.fromisoformat(token.get("saved_at", datetime.now().isoformat()))
    age_minutes = (datetime.now() - saved_at).total_seconds() / 60
    if age_minutes > 25:  # refresh before 30 min expiry
        token = refresh_access_token(token)
    return token

def get_tenant_id(token_data: dict) -> Optional[str]:
    """Get the Xero tenant (organisation) ID"""
    try:
        resp = requests.get(
            "https://api.xero.com/connections",
            headers={"Authorization": f"Bearer {token_data['access_token']}"},
            timeout=10
        )
        resp.raise_for_status()
        connections = resp.json()
        if connections:
            return connections[0]["tenantId"]
    except Exception as e:
        logger.error(f"Tenant ID error: {e}")
    return None

# ----------------------------
# OAUTH FLOW
# ----------------------------
def get_auth_url() -> str:
    """Build the Xero OAuth2 authorization URL"""
    return (
        f"https://login.xero.com/identity/connect/authorize"
        f"?response_type=code"
        f"&client_id={XERO_CLIENT_ID}"
        f"&redirect_uri={XERO_REDIRECT_URI}"
        f"&scope={XERO_SCOPES.replace(' ', '%20')}"
        f"&state=astrofencebot"
    )

def exchange_code_for_token(code: str) -> Optional[dict]:
    """Exchange auth code for access + refresh tokens"""
    try:
        resp = requests.post(
            "https://identity.xero.com/connect/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": XERO_REDIRECT_URI,
                "client_id": XERO_CLIENT_ID,
                "client_secret": XERO_CLIENT_SECRET,
            },
            timeout=15
        )
        resp.raise_for_status()
        token = resp.json()
        token["saved_at"] = datetime.now().isoformat()
        save_token(token)
        return token
    except Exception as e:
        logger.error(f"Token exchange error: {e}")
        return None

# ----------------------------
# XERO API HELPERS
# ----------------------------
def xero_headers(token_data: dict, tenant_id: str) -> dict:
    return {
        "Authorization": f"Bearer {token_data['access_token']}",
        "Xero-tenant-id": tenant_id,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

def find_or_create_contact(token_data: dict, tenant_id: str, name: str, phone: str = "", email: str = "") -> Optional[str]:
    """Find existing contact by phone/email or create new one. Returns ContactID."""
    headers = xero_headers(token_data, tenant_id)

    # Search for existing contact by name
    try:
        search_name = name.replace("&", "%26")
        resp = requests.get(
            f"https://api.xero.com/api.xro/2.0/Contacts?where=Name.Contains(\"{name}\")",
            headers=headers,
            timeout=10
        )
        if resp.status_code == 200:
            contacts = resp.json().get("Contacts", [])
            if contacts:
                logger.info(f"Found existing Xero contact: {name}")
                return contacts[0]["ContactID"]
    except Exception as e:
        logger.warning(f"Contact search error: {e}")

    # Build contact payload
    contact_payload = {"Name": name}
    if phone:
        contact_payload["Phones"] = [{"PhoneType": "MOBILE", "PhoneNumber": phone}]
    if email:
        contact_payload["EmailAddress"] = email

    try:
        resp = requests.post(
            "https://api.xero.com/api.xro/2.0/Contacts",
            headers=headers,
            json={"Contacts": [contact_payload]},
            timeout=10
        )
        resp.raise_for_status()
        contact_id = resp.json()["Contacts"][0]["ContactID"]
        logger.info(f"✅ Created Xero contact: {name} ({contact_id})")
        return contact_id
    except Exception as e:
        logger.error(f"Contact create error: {e}")
        return None

def create_xero_quote(token_data: dict, tenant_id: str, contact_id: str, session_data: dict, line_items: list) -> Optional[dict]:
    """
    Create a Quote in Xero with line items.
    line_items = [{"description": str, "quantity": float, "unitAmount": float, "lineAmount": float}, ...]
    """
    headers = xero_headers(token_data, tenant_id)

    name = session_data.get("soft_lead_name", "Customer")
    quote_number = f"AOD-{datetime.now().strftime('%y%m%d-%H%M')}"

    # Build Xero line items
    xero_lines = []
    for item in line_items:
        xero_lines.append({
            "Description": item.get("description", ""),
            "Quantity": item.get("quantity", 1),
            "UnitAmount": round(item.get("unitAmount", 0), 2),
            "LineAmount": round(item.get("lineAmount", 0), 2),
            "AccountCode": "200"  # standard sales account
        })

    quote_payload = {
        "QuoteNumber": quote_number,
        "Contact": {"ContactID": contact_id},
        "LineItems": xero_lines,
        "Date": datetime.now().strftime("%Y-%m-%d"),
        "ExpiryDate": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
        "Status": "DRAFT",
        "Title": f"Fence Project — {name}",
        "Summary": session_data.get("project_summary", "Fence installation quote from chat session"),
        "Terms": "Quote valid for 30 days. Final price confirmed after site visit. Includes materials, labor, and delivery.",
        "LineAmountTypes": "EXCLUSIVE"
    }

    try:
        resp = requests.post(
            "https://api.xero.com/api.xro/2.0/Quotes",
            headers=headers,
            json={"Quotes": [quote_payload]},
            timeout=15
        )
        resp.raise_for_status()
        quote = resp.json()["Quotes"][0]
        logger.info(f"✅ Created Xero quote: {quote_number}")
        return quote
    except Exception as e:
        logger.error(f"Quote create error: {e}: {resp.text if 'resp' in dir() else ''}")
        return None

def create_xero_project(token_data: dict, tenant_id: str, contact_id: str, session_data: dict, total_estimate: float) -> Optional[dict]:
    """Create a Project in Xero linked to the contact"""
    headers = {
        "Authorization": f"Bearer {token_data['access_token']}",
        "Xero-tenant-id": tenant_id,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    name = session_data.get("soft_lead_name", "Customer")
    project_name = f"Fence — {name} — {datetime.now().strftime('%b %d %Y')}"

    project_payload = {
        "contactId": contact_id,
        "name": project_name,
        "estimateAmount": round(total_estimate, 2),
        "deadlineUtc": (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%dT00:00:00Z"),
        "currencyCode": "USD"
    }

    try:
        resp = requests.post(
            "https://api.xero.com/projects.xro/2.0/Projects",
            headers=headers,
            json=project_payload,
            timeout=15
        )
        resp.raise_for_status()
        project = resp.json()
        logger.info(f"✅ Created Xero project: {project_name}")
        return project
    except Exception as e:
        logger.error(f"Project create error: {e}")
        return None

# ----------------------------
# QUOTE PARSER — Extract line items from chat transcript
# ----------------------------
def parse_quote_from_transcript(messages: list) -> dict:
    """
    Parse the chat transcript to extract quote line items and contact info.
    Looks for the last bot message that contains dollar amounts and fence specs.
    """
    result = {
        "line_items": [],
        "total": 0.0,
        "project_summary": "",
        "contact_name": "",
        "contact_phone": "",
        "contact_email": ""
    }

    # Find the last assistant message with pricing
    last_quote_msg = ""
    for msg in reversed(messages):
        if msg.get("type") == "assistant" and "$" in msg.get("message", ""):
            last_quote_msg = msg["message"]
            break

    if not last_quote_msg:
        return result

    result["project_summary"] = last_quote_msg[:300].replace("\n", " ").strip()

    # Extract line items — look for patterns like "Something: $X,XXX" or "- Item: $XXX"
    line_pattern = re.findall(
        r'[-•*]?\s*([A-Za-z][^:$\n]{3,60}):\s*\$?([\d,]+(?:\.\d{2})?)',
        last_quote_msg
    )

    seen = set()
    for desc, amount_str in line_pattern:
        desc = desc.strip().strip("*").strip()
        if len(desc) < 4:
            continue
        # Skip totals and headers — we'll add total separately
        skip_words = ["total", "quote", "option", "savings", "includes", "note"]
        if any(w in desc.lower() for w in skip_words):
            continue
        if desc in seen:
            continue
        seen.add(desc)

        try:
            amount = float(amount_str.replace(",", ""))
            if amount > 0:
                result["line_items"].append({
                    "description": desc,
                    "quantity": 1,
                    "unitAmount": amount,
                    "lineAmount": amount
                })
        except ValueError:
            continue

    # Try to find grand total
    total_patterns = [
        r'(?:TOTAL|Grand Total|Total)[^\$]*\$?([\d,]+(?:\.\d{2})?)',
        r'\*\*.*?TOTAL.*?\*\*[^\$]*\$?([\d,]+(?:\.\d{2})?)'
    ]
    for pattern in total_patterns:
        total_match = re.search(pattern, last_quote_msg, re.IGNORECASE)
        if total_match:
            try:
                result["total"] = float(total_match.group(1).replace(",", ""))
                break
            except ValueError:
                pass

    # If no total found, sum line items
    if result["total"] == 0 and result["line_items"]:
        result["total"] = sum(i["lineAmount"] for i in result["line_items"])

    return result

# ----------------------------
# MAIN FUNCTION — Called when a qualified lead session ends
# ----------------------------
def push_session_to_xero(session_id: str, session_data: dict) -> dict:
    """
    Full pipeline: parse session → create contact → create quote → create project
    Returns dict with status and links.
    """
    result = {"success": False, "quote_number": None, "project_name": None, "error": None}

    # Check if Xero is connected
    token = get_valid_token()
    if not token:
        result["error"] = "Xero not connected — visit /xero/auth to connect"
        logger.warning("Xero not connected — skipping push")
        return result

    tenant_id = get_tenant_id(token)
    if not tenant_id:
        result["error"] = "Could not get Xero tenant ID"
        return result

    # Parse quote from transcript
    messages = session_data.get("messages", [])
    parsed = parse_quote_from_transcript(messages)

    # Get contact details from session
    name = session_data.get("soft_lead_name", "").strip()
    phone = session_data.get("soft_lead_phone", "").strip()
    email = session_data.get("soft_lead_email", "").strip()

    # If no name captured, use a fallback
    if not name:
        name = f"Chat Lead {session_id[:6]}"

    parsed["contact_name"] = name
    parsed["contact_phone"] = phone
    parsed["contact_email"] = email

    # Skip if no pricing was found in chat
    if not parsed["line_items"] and parsed["total"] == 0:
        result["error"] = "No pricing found in chat — quote not created"
        logger.info(f"Session {session_id[:8]} had no pricing — skipping Xero push")
        return result

    # Step 1: Find or create contact
    contact_id = find_or_create_contact(token, tenant_id, name, phone, email)
    if not contact_id:
        result["error"] = "Failed to create Xero contact"
        return result

    # Step 2: Create quote
    quote = create_xero_quote(token, tenant_id, contact_id, parsed, parsed["line_items"])
    if quote:
        result["quote_number"] = quote.get("QuoteNumber")
        result["quote_id"] = quote.get("QuoteID")

    # Step 3: Create project
    total = parsed["total"] or sum(i["lineAmount"] for i in parsed["line_items"])
    project = create_xero_project(token, tenant_id, contact_id, {"soft_lead_name": name, "project_summary": parsed["project_summary"]}, total)
    if project:
        result["project_name"] = project.get("name")
        result["project_id"] = project.get("projectId")

    result["success"] = bool(quote or project)
    result["contact_name"] = name
    result["total_estimate"] = total

    logger.info(f"✅ Xero push complete for {name}: quote={result.get('quote_number')}, project={result.get('project_name')}")
    return result
