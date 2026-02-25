"""
Xero Integration for Astro Outdoor Designs
Handles OAuth2 auth, contact creation, project creation, and quote generation
from chat session data.

FIXES APPLIED:
1. Phone number splits area code into PhoneAreaCode field correctly
2. Address goes to POBOX (billing) instead of STREET (delivery)
3. Quote is linked to the project via ProjectID so it appears inside the project
4. Total parser improved — handles comma-formatted numbers like $3,000
5. Fallback single line item created if no line items parsed from conversational message
6. Project name now includes fence type from session data
"""

import os
import json
import requests
import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# ----------------------------
# XERO CONFIG
# ----------------------------
XERO_CLIENT_ID = os.getenv("XERO_CLIENT_ID", "").strip()
XERO_CLIENT_SECRET = os.getenv("XERO_CLIENT_SECRET", "").strip()
XERO_REDIRECT_URI = os.getenv("XERO_REDIRECT_URI", "https://astro-fence-assistant.onrender.com/xero/callback").strip()
XERO_SCOPES = "openid profile email accounting.contacts accounting.transactions projects offline_access"

XERO_TOKEN_FILE = "xero_token.json"

TOKEN_URL = "https://identity.xero.com/connect/token"
CONNECTIONS_URL = "https://api.xero.com/connections"

# ----------------------------
# TOKEN MANAGEMENT
# Tokens are stored in the XERO_TOKEN_DATA environment variable so they
# survive Render restarts (free tier has no persistent disk).
#
# HOW TO SET UP AFTER FIRST AUTH:
# 1. Deploy this file
# 2. Go to https://astro-fence-assistant.onrender.com/xero/auth
# 3. Complete Xero login
# 4. Check your Render logs — find the line starting with:
#    "COPY THIS INTO RENDER ENV VAR 'XERO_TOKEN_DATA'"
# 5. Copy that entire JSON string into a Render env var named XERO_TOKEN_DATA
# 6. Done — token now survives every restart
# ----------------------------

def save_token(token_data: dict):
    token_data["acquired_at"] = time.time()
    encoded = json.dumps(token_data)
    try:
        with open(XERO_TOKEN_FILE, "w") as f:
            f.write(encoded)
    except Exception as e:
        logger.warning(f"Local token file save failed: {e}")
    logger.info("Xero token saved")
    logger.info(f"COPY THIS INTO RENDER ENV VAR 'XERO_TOKEN_DATA' -> {encoded}")


def load_token() -> Optional[dict]:
    env_token = os.getenv("XERO_TOKEN_DATA", "").strip()
    if env_token:
        try:
            return json.loads(env_token)
        except Exception as e:
            logger.warning(f"Failed to parse XERO_TOKEN_DATA env var: {e}")
    try:
        if os.path.exists(XERO_TOKEN_FILE):
            with open(XERO_TOKEN_FILE, "r") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Token load error: {e}")
    return None


def _token_is_expired(token_data: dict, buffer_seconds: int = 60) -> bool:
    try:
        acquired_at = float(token_data.get("acquired_at", 0))
        expires_in = float(token_data.get("expires_in", 0))
        if acquired_at <= 0 or expires_in <= 0:
            return True
        return (time.time() + buffer_seconds) >= (acquired_at + expires_in)
    except Exception:
        return True


def refresh_access_token(token_data: dict) -> Optional[dict]:
    try:
        if not token_data.get("refresh_token"):
            logger.error("No refresh_token available; re-auth required.")
            return None
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": token_data["refresh_token"],
            },
            auth=(XERO_CLIENT_ID, XERO_CLIENT_SECRET),
            timeout=15
        )
        resp.raise_for_status()
        new_token = resp.json()
        if "refresh_token" not in new_token and token_data.get("refresh_token"):
            new_token["refresh_token"] = token_data["refresh_token"]
        save_token(new_token)
        logger.info("Xero token refreshed")
        return new_token
    except Exception as e:
        logger.error(f"Token refresh error: {e}")
        return None


def get_valid_token() -> Optional[dict]:
    token = load_token()
    if not token:
        return None
    if _token_is_expired(token):
        token = refresh_access_token(token)
    return token


def get_tenant_id(token_data: dict) -> Optional[str]:
    try:
        resp = requests.get(
            CONNECTIONS_URL,
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
    return (
        f"https://login.xero.com/identity/connect/authorize"
        f"?response_type=code"
        f"&client_id={XERO_CLIENT_ID}"
        f"&redirect_uri={XERO_REDIRECT_URI}"
        f"&scope={XERO_SCOPES.replace(' ', '%20')}"
        f"&state=astrofencebot"
    )


def exchange_code_for_token(code: str) -> Optional[dict]:
    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": XERO_REDIRECT_URI,
            },
            auth=(XERO_CLIENT_ID, XERO_CLIENT_SECRET),
            timeout=15
        )
        resp.raise_for_status()
        token = resp.json()
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


def _safe_xero_where_value(value: str) -> str:
    return (value or "").replace('"', '\\"').replace("'", "\\'")


def _xero_request_with_auto_refresh(method: str, url: str, headers: dict, token_data: dict, **kwargs):
    """If Xero returns 401, refresh token once and retry."""
    resp = requests.request(method, url, headers=headers, timeout=15, **kwargs)
    if resp.status_code == 401:
        logger.info("Xero 401 — refreshing token and retrying once...")
        new_token = refresh_access_token(token_data)
        if not new_token:
            return resp
        headers["Authorization"] = f"Bearer {new_token['access_token']}"
        resp = requests.request(method, url, headers=headers, timeout=15, **kwargs)
    return resp


# ----------------------------
# FIX 1: PHONE SPLITTING
# ----------------------------
def _split_phone(phone: str) -> tuple:
    """
    Returns (area_code, number) from formats like:
    '9176268839', '917-626-6839', '(917) 626-6839', '917.626.6839'
    """
    digits = re.sub(r'\D', '', phone or '')
    if len(digits) == 10:
        return digits[:3], digits[3:]
    elif len(digits) == 11 and digits[0] == '1':
        return digits[1:4], digits[4:]
    return '', digits


# ----------------------------
# CONTACT CREATE/FIND
# FIX 1: Phone splits into area code + number
# FIX 2: Address uses POBOX (billing) not STREET (delivery)
# ----------------------------
def find_or_create_contact(token_data: dict, tenant_id: str, contact_info: dict) -> Optional[str]:
    """
    Find existing contact or create new one.
    contact_info keys: first_name, last_name, email, phone, address, city, state, zip
    Returns ContactID.
    """
    headers = xero_headers(token_data, tenant_id)

    first = (contact_info.get("first_name", "") or "").strip()
    last = (contact_info.get("last_name", "") or "").strip()
    email = (contact_info.get("email", "") or "").strip()

    full_name = f"{first} {last}".strip()
    if not full_name:
        full_name = f"Chat Lead {datetime.now().strftime('%m%d%H%M')}"

    # Search by email first
    if email:
        try:
            safe_email = _safe_xero_where_value(email)
            url = f'https://api.xero.com/api.xro/2.0/Contacts?where=EmailAddress=="{safe_email}"'
            resp = _xero_request_with_auto_refresh("GET", url, headers, token_data)
            if resp.status_code == 200:
                contacts = resp.json().get("Contacts", [])
                if contacts:
                    logger.info(f"Found existing Xero contact by email: {email}")
                    return contacts[0]["ContactID"]
        except Exception as e:
            logger.warning(f"Contact search by email error: {e}")

    # Fallback: search by name
    try:
        safe_name = _safe_xero_where_value(full_name)
        url = f'https://api.xero.com/api.xro/2.0/Contacts?where=Name=="{safe_name}"'
        resp = _xero_request_with_auto_refresh("GET", url, headers, token_data)
        if resp.status_code == 200:
            contacts = resp.json().get("Contacts", [])
            if contacts:
                logger.info(f"Found existing Xero contact by name: {full_name}")
                return contacts[0]["ContactID"]
    except Exception as e:
        logger.warning(f"Contact search by name error: {e}")

    # FIX 1: Split phone into area code + number
    area_code, phone_number = _split_phone(contact_info.get("phone", ""))

    # FIX 2: POBOX = billing address (STREET = delivery address in Xero)
    contact_payload = {
        "FirstName": first,
        "LastName": last,
        "Name": full_name,
        "EmailAddress": email,
        "Phones": [{
            "PhoneType": "MOBILE",
            "PhoneAreaCode": area_code,
            "PhoneNumber": phone_number
        }],
        "Addresses": [{
            "AddressType": "POBOX",
            "AddressLine1": contact_info.get("address", ""),
            "City": contact_info.get("city", ""),
            "Region": contact_info.get("state", "TX"),
            "PostalCode": contact_info.get("zip", ""),
            "Country": "US"
        }]
    }

    try:
        resp = _xero_request_with_auto_refresh(
            "POST",
            "https://api.xero.com/api.xro/2.0/Contacts",
            headers,
            token_data,
            json={"Contacts": [contact_payload]}
        )
        resp.raise_for_status()
        contact_id = resp.json()["Contacts"][0]["ContactID"]
        logger.info(f"Created Xero contact: {full_name} ({contact_id})")
        return contact_id
    except Exception as e:
        logger.error(f"Contact create error: {e}")
        return None


# ----------------------------
# PROJECT CREATE
# FIX 6: Project name includes fence type
# Format: "42in Emily Aluminum Black - Cathy Quinn - Feb 25 2026"
# ----------------------------
def build_project_name(full_name: str, fence_type: str) -> str:
    date_str = datetime.now().strftime("%b %d %Y")
    if fence_type:
        return f"{fence_type} - {full_name} - {date_str}"
    return f"Fence - {full_name} - {date_str}"


def create_xero_project(
    token_data: dict,
    tenant_id: str,
    contact_id: str,
    project_name: str,
    total_estimate: float
) -> Optional[dict]:
    """Create a Project in Xero linked to the contact."""
    headers = {
        "Authorization": f"Bearer {token_data['access_token']}",
        "Xero-tenant-id": tenant_id,
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    project_payload = {
        "contactId": contact_id,
        "name": project_name,
        "estimateAmount": round(total_estimate, 2),
        "deadlineUtc": (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%dT00:00:00Z"),
        "currencyCode": "USD"
    }
    try:
        resp = _xero_request_with_auto_refresh(
            "POST",
            "https://api.xero.com/projects.xro/2.0/Projects",
            headers,
            token_data,
            json=project_payload
        )
        resp.raise_for_status()
        project = resp.json()
        logger.info(f"Created Xero project: {project_name}")
        return project
    except Exception as e:
        logger.error(f"Project create error: {e}")
        return None


# ----------------------------
# QUOTE CREATE
# FIX 3: Quote linked to project via ProjectID
# ----------------------------
def create_xero_quote(
    token_data: dict,
    tenant_id: str,
    contact_id: str,
    quote_title: str,
    line_items: list,
    summary: str,
    project_id: Optional[str] = None
) -> Optional[dict]:
    """
    Create a Draft Quote in Xero linked to the contact.
    Pass project_id to link the quote inside the project tab.
    """
    headers = xero_headers(token_data, tenant_id)
    quote_number = f"AOD-{datetime.now().strftime('%y%m%d-%H%M')}"

    xero_lines = []
    for item in line_items:
        xero_lines.append({
            "Description": item.get("description", ""),
            "Quantity": item.get("quantity", 1),
            "UnitAmount": round(item.get("unitAmount", 0), 2),
            "LineAmount": round(item.get("lineAmount", 0), 2),
            "AccountCode": "200"
        })

    quote_payload = {
        "QuoteNumber": quote_number,
        "Contact": {"ContactID": contact_id},
        "LineItems": xero_lines,
        "Date": datetime.now().strftime("%Y-%m-%d"),
        "ExpiryDate": (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d"),
        "Status": "DRAFT",
        "Title": quote_title,
        "Summary": summary[:500] if summary else "Fence installation quote from chat session",
        "Terms": "Quote valid for 30 days. Final price confirmed after site visit. Includes materials, labor, and delivery.",
        "LineAmountTypes": "EXCLUSIVE"
    }

    # FIX 3: Link quote to project
    if project_id:
        quote_payload["ProjectID"] = project_id

    try:
        resp = _xero_request_with_auto_refresh(
            "POST",
            "https://api.xero.com/api.xro/2.0/Quotes",
            headers,
            token_data,
            json={"Quotes": [quote_payload]}
        )
        resp.raise_for_status()
        quote = resp.json()["Quotes"][0]
        logger.info(f"Created Xero quote: {quote_number} (project_id={project_id})")
        return quote
    except Exception as e:
        logger.error(f"Quote create error: {e}")
        return None


# ----------------------------
# QUOTE PARSER
# FIX 4: Better total extraction handles $3,000 comma formatting
# FIX 5: Fallback single line item for conversational messages
# ----------------------------
def parse_quote_from_transcript(messages: list) -> dict:
    result = {
        "line_items": [],
        "total": 0.0,
        "project_summary": ""
    }

    # Find last assistant message with a dollar amount
    last_quote_msg = ""
    for msg in reversed(messages):
        role = msg.get("type") or msg.get("role") or ""
        content = msg.get("message") or msg.get("content") or ""
        if role == "assistant" and "$" in content:
            last_quote_msg = content
            break

    if not last_quote_msg:
        return result

    result["project_summary"] = last_quote_msg[:500].replace("\n", " ").strip()

    # FIX 4: Extract total — handles comma-formatted numbers like $3,000
    total_patterns = [
        r'(?:TOTAL|Grand Total|Total Quote|Total Cost|Total Price)[^\$\d]*\$?([\d,]+(?:\.\d{2})?)',
        r'\*\*.*?(?:TOTAL|Total).*?\*\*[^\$\d]*\$?([\d,]+(?:\.\d{2})?)',
        r'\$\s*([\d,]+(?:\.\d{2})?)\s*(?:total|for the job|for this job)',
    ]
    for pattern in total_patterns:
        match = re.search(pattern, last_quote_msg, re.IGNORECASE)
        if match:
            try:
                val = float(match.group(1).replace(",", ""))
                if val > 100:
                    result["total"] = val
                    break
            except ValueError:
                pass

    # Fallback: grab largest dollar amount in message
    if result["total"] == 0:
        all_amounts = re.findall(r'\$\s*([\d,]+(?:\.\d{2})?)', last_quote_msg)
        parsed_amounts = []
        for a in all_amounts:
            try:
                val = float(a.replace(",", ""))
                if val > 100:
                    parsed_amounts.append(val)
            except ValueError:
                pass
        if parsed_amounts:
            result["total"] = max(parsed_amounts)

    # Extract line items from bullet/dash formatted messages
    line_pattern = re.findall(
        r'[-•*]?\s*([A-Za-z][^:$\n]{3,60}):\s*\$?([\d,]+(?:\.\d{2})?)',
        last_quote_msg
    )
    seen = set()
    for desc, amount_str in line_pattern:
        desc = desc.strip().strip("*").strip()
        if len(desc) < 4:
            continue
        skip_words = ["total", "quote", "option", "savings", "includes", "note", "valid", "price"]
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

    # FIX 5: Fallback single line item if none parsed
    if not result["line_items"] and result["total"] > 0:
        summary_short = result["project_summary"][:300] if result["project_summary"] else "Fence installation"
        result["line_items"] = [{
            "description": summary_short,
            "quantity": 1,
            "unitAmount": result["total"],
            "lineAmount": result["total"]
        }]
        logger.info(f"No line items parsed — created single line item for ${result['total']}")

    return result


# ----------------------------
# MAIN PIPELINE — Contact > Project > Quote
# ----------------------------
def push_to_xero_with_contact(contact_info: dict, session_data: dict) -> dict:
    """
    Full pipeline:
    1. Create Contact (split phone, billing address)
    2. Create Project (fence type in name, correct estimate)
    3. Create Quote (linked to contact AND project, with line items)
    """
    result = {"success": False, "quote_number": None, "project_name": None, "error": None}

    token = get_valid_token()
    if not token:
        result["error"] = "Xero not connected"
        return result

    tenant_id = get_tenant_id(token)
    if not tenant_id:
        result["error"] = "Could not get Xero tenant ID"
        return result

    messages = session_data.get("messages", [])
    parsed = parse_quote_from_transcript(messages)

    first = (contact_info.get("first_name", "") or "").strip()
    last = (contact_info.get("last_name", "") or "").strip()
    full_name = f"{first} {last}".strip()
    if not full_name:
        full_name = "Chat Lead"

    # FIX 6: Pull fence type from session data
    fence_type = session_data.get("fence_type", "").strip()

    # STEP 1: Create Contact
    contact_id = find_or_create_contact(token, tenant_id, contact_info)
    if not contact_id:
        result["error"] = "Failed to create Xero contact"
        return result

    total = parsed["total"] or sum(i["lineAmount"] for i in parsed["line_items"]) or 0

    # FIX 6: Descriptive project name
    project_name = build_project_name(full_name, fence_type)
    quote_title = f"Fence Project - {full_name}"

    # STEP 2: Create Project
    project_id = None
    project = create_xero_project(token, tenant_id, contact_id, project_name, total)
    if project:
        result["project_name"] = project.get("name")
        project_id = project.get("projectId")
        result["project_id"] = project_id

    # STEP 3: Create Quote linked to project
    if parsed["line_items"]:
        quote = create_xero_quote(
            token, tenant_id, contact_id,
            quote_title,
            parsed["line_items"],
            parsed["project_summary"],
            project_id=project_id
        )
        if quote:
            result["quote_number"] = quote.get("QuoteNumber")
            result["quote_id"] = quote.get("QuoteID")

    result["success"] = bool(contact_id)
    result["contact_name"] = full_name
    result["total_estimate"] = total
    result["fence_type"] = fence_type

    logger.info(
        f"Xero pipeline complete for {full_name}: "
        f"fence={fence_type}, project={result.get('project_name')}, "
        f"quote={result.get('quote_number')}, project_id={project_id}"
    )
    return result


# ----------------------------
# BACKWARD COMPAT — auto-push from transcript timer
# ----------------------------
def push_session_to_xero(session_id: str, session_data: dict) -> dict:
    """Auto-push from transcript timer — uses contact info captured in chat."""
    name = session_data.get("soft_lead_name", "")
    name_parts = name.split(" ", 1) if name else ["", ""]
    contact_info = {
        "first_name": name_parts[0],
        "last_name": name_parts[1] if len(name_parts) > 1 else "",
        "email": session_data.get("soft_lead_email", ""),
        "phone": session_data.get("soft_lead_phone", ""),
        "address": session_data.get("soft_lead_address", ""),
        "city": session_data.get("soft_lead_city", ""),
        "state": "TX",
        "zip": session_data.get("soft_lead_zip", ""),
    }
    return push_to_xero_with_contact(contact_info, session_data)
