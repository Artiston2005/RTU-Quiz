import os
import json
import re
import random
import time
import datetime
import base64
import mimetypes
import urllib.request
import urllib.parse
import asyncio
from collections import defaultdict
from functools import lru_cache
from typing import List, Optional, Union
import groq
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Depends, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field, field_validator
from supabase import create_client, Client
from google import genai
from google.genai import types
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.responses import StreamingResponse
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn

# --- 1. CONFIGURATION & KEYS ---
load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")

# Initialize Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialize Gemini & Groq
client = genai.Client(api_key=GEMINI_API_KEY)
groq_client = groq.Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

app = FastAPI(title="RTUKaGyan API")
security = HTTPBearer(auto_error=False)

# Restrict CORS in production: configure allowed origins via ENV
frontend_origins = os.getenv("FRONTEND_ORIGINS", "")
allowed_origins = [o.strip() for o in frontend_origins.split(",") if o.strip()] or ["http://localhost:5500"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Security Headers Middleware ---
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        return response

app.add_middleware(SecurityHeadersMiddleware)

# Warn on startup if FRONTEND_ORIGINS is not configured
if not frontend_origins:
    print("WARNING: FRONTEND_ORIGINS env var not set. CORS defaults to http://localhost:5500 only.")

# --- 2. DATA MODELS ---
class TopicRequest(BaseModel):
    topic_id: str = Field(..., max_length=100)
    topic_name: str = Field(..., max_length=300)
    subject_name: str = Field(..., max_length=300)

class CustomQuizRequest(BaseModel):
    topic_id: str = Field(..., max_length=100)
    topic_name: str = Field(..., max_length=300)
    subject_name: str = Field(..., max_length=300)
    num_questions: int = Field(..., ge=1, le=30)
    difficulty: str = Field(..., max_length=20)
    time_per_question: int = Field(..., ge=10, le=300)

class QuizBatchRequest(BaseModel):
    topic_id: str = Field(..., max_length=100)
    topic_name: str = Field(..., max_length=300)
    subject_name: str = Field(..., max_length=300)
    num_questions: int = Field(..., ge=1, le=30)
    difficulty: str = Field(..., max_length=20)

class ApiKeyRequest(BaseModel):
    api_key: str = Field("", max_length=200)


class ChatMessage(BaseModel):
    role: str = Field(..., pattern="^(user|model)$")
    text: str = Field(..., max_length=2000)

class NotesSelectionQuestionRequest(BaseModel):
    topic_id: str = Field(..., max_length=100)
    topic_name: str = Field(..., max_length=300)
    subject_name: str = Field(..., max_length=300)
    question: str = Field(..., min_length=1, max_length=1000)
    image_data_url: Optional[str] = Field(None)
    image_data_urls: Optional[List[str]] = Field(default_factory=list)
    text_content: Optional[str] = Field(None)
    source_label: str = Field("", max_length=200)
    history: List[ChatMessage] = Field(default_factory=list)


DEFAULT_RATE_LIMIT_SETTINGS = {
    "auth_gemini_limit": 15,
    "auth_groq_limit": 20,
    "guest_gemini_limit": 3,
    "guest_groq_limit": 5
}

# --- 3. THE SYSTEM PROMPT ---
def get_system_prompt(topic_name, subject_name):
    return f"""
    You are an expert engineering tutor for RTU B.Tech students.
    Generate a study plan and a quiz with EXACTLY 8 multiple choice questions for the topic: "{topic_name}" (Subject: {subject_name}).
    
    You MUST respond strictly in valid JSON format matching this exact schema:
    {{
      "pomodoro_plan": [
        {{ "step": 1, "title": "...", "duration_minutes": 25, "action_items": ["...", "..."] }},
        {{ "step": 2, "title": "...", "duration_minutes": 25, "action_items": ["...", "..."] }},
        {{ "step": 3, "title": "...", "duration_minutes": 25, "action_items": ["...", "..."] }}
      ],
      "quiz": [
        {{
          "question": "...",
          "options": ["A", "B", "C", "D"],
          "correct_answer_index": 0,
          "explanation": "..."
        }}
      ]
    }}
    """

def get_custom_quiz_prompt(topic_name, subject_name, num_questions, difficulty):
    return f"""
    You are an expert engineering tutor for RTU B.Tech students.
    Generate a highly customized knowledge check quiz for the topic: "{topic_name}" (Subject: {subject_name}).
    
    REQUIREMENTS:
    - Generate EXACTLY {num_questions} multiple choice questions.
    - The difficulty level of the questions MUST be: {difficulty}. (Scale: Easy=Conceptual, Medium=Application, Hard=Analytical, GATE=Advanced/Numerical).
    
    You MUST respond strictly in valid JSON format matching this exact schema:
    {{
      "quiz": [
        {{
          "question": "...",
          "options": ["...", "...", "...", "..."],
          "correct_answer_index": 0,
          "explanation": "..."
        }}
      ]
    }}
    """

# --- 3.5 AI OUTPUT SANITIZATION ---

def normalize_quiz_question(item):
    """Normalize quiz items to the expected schema.

    The AI output may use slightly different field names (camelCase, missing keys, etc.).
    This helper coalesces common variants and ignores invalid items.
    """

    if not isinstance(item, dict):
        return None

    # Common field variations
    question = item.get("question") or item.get("Question") or item.get("prompt")
    options = item.get("options") or item.get("choices") or item.get("Choices") or item.get("answers")
    correct = item.get("correct_answer_index")
    if correct is None:
        correct = item.get("correctAnswerIndex") or item.get("correct_index") or item.get("correctIndex")
    explanation = item.get("explanation") or item.get("Explanation") or item.get("explain") or item.get("explanation_text")

    if not question or not isinstance(options, list) or not isinstance(correct, int):
        return None

    if correct < 0 or correct >= len(options):
        return None

    return {
        "question": str(question).strip(),
        "options": [str(o) for o in options],
        "correct_answer_index": int(correct),
        "explanation": str(explanation).strip() if explanation else ""
    }

# --- 3.5 AUTH, RATE LIMITS & AI GENERATION ---

def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Extracts user from JWT using Supabase Auth"""
    if credentials:
        try:
            res = supabase.auth.get_user(credentials.credentials)
            if hasattr(res, 'user') and res.user:
                return res.user
        except Exception:
            pass
    return None

# Simple in-memory grace period to prevent double-charging on retries/parallel calls
# { "ip_endpoint": last_deduction_timestamp }
RECENT_DEDUCTIONS = {}
RECENT_DEDUCTIONS_MAX = 500  # cap to prevent memory leaks

def _prune_deductions():
    """Remove expired entries (older than 30s) and cap dict size."""
    now = time.time()
    expired = [k for k, v in RECENT_DEDUCTIONS.items() if now - v > 30]
    for k in expired:
        del RECENT_DEDUCTIONS[k]
    # Hard cap: if still too big, clear oldest half
    if len(RECENT_DEDUCTIONS) > RECENT_DEDUCTIONS_MAX:
        sorted_keys = sorted(RECENT_DEDUCTIONS, key=RECENT_DEDUCTIONS.get)
        for k in sorted_keys[:len(sorted_keys) // 2]:
            del RECENT_DEDUCTIONS[k]


def _to_iso_date(value):
    """Normalize a DATE/TIMESTAMP/str value to an ISO YYYY-MM-DD string."""
    if value is None:
        return None
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.datetime):
        return value.date().isoformat()
    try:
        return datetime.date.fromisoformat(str(value)).isoformat()
    except Exception:
        return str(value)


def get_ist_today():
    ist_now = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    return ist_now.date().isoformat()


def get_request_ip(request: Request):
    ip = "127.0.0.1"
    if request.client and request.client.host:
        ip = request.client.host
    x_forwarded_for = request.headers.get("x-forwarded-for")
    if x_forwarded_for:
        ip = x_forwarded_for.split(",")[0].strip()
    return ip


@lru_cache(maxsize=1)
def _get_rate_limit_settings_cached(ttl_bucket: int):
    settings = DEFAULT_RATE_LIMIT_SETTINGS.copy()
    try:
        response = supabase.table("global_rate_limits").select("*").eq("id", 1).limit(1).execute()
        rows = response.data or []
        if rows:
            row = rows[0]
            for key, fallback in DEFAULT_RATE_LIMIT_SETTINGS.items():
                value = row.get(key)
                if isinstance(value, int):
                    settings[key] = max(0, value)
                else:
                    try:
                        settings[key] = max(0, int(value))
                    except (TypeError, ValueError):
                        settings[key] = fallback
    except Exception as e:
        print("Rate limit settings load error:", e)
    return settings


def get_rate_limit_settings():
    # Short TTL to avoid hitting Supabase on every request while still
    # allowing dashboard edits to propagate quickly.
    return _get_rate_limit_settings_cached(int(time.time()) // 60)


def get_guest_upgrade_message(settings=None):
    settings = settings or get_rate_limit_settings()
    auth_total = settings["auth_gemini_limit"] + settings["auth_groq_limit"]
    return f"You've exhausted your free guest generations. Sign in to get {auth_total} daily credits!"


def get_auth_limit_message():
    return "Daily Limits Reached. Come back tomorrow or add your own API Key in Settings!"


def get_current_limit_state(request: Request, user):
    settings = get_rate_limit_settings()
    today = get_ist_today()

    if user:
        gemini_limit = settings["auth_gemini_limit"]
        groq_limit = settings["auth_groq_limit"]
        try:
            profile_res = supabase.table("profiles").select("*").eq("id", user.id).single().execute()
            profile = profile_res.data or {}
            gemini_remaining = profile.get("api_calls_remaining", gemini_limit)
            groq_remaining = profile.get("groq_calls_remaining", groq_limit)
            custom_key = profile.get("custom_gemini_key")
            last_reset = _to_iso_date(profile.get("last_reset_date"))

            should_persist = False
            update_payload = {}

            if last_reset != today:
                gemini_remaining = gemini_limit
                groq_remaining = groq_limit
                update_payload.update({
                    "api_calls_remaining": gemini_remaining,
                    "groq_calls_remaining": groq_remaining,
                    "last_reset_date": today
                })
                should_persist = bool(profile)
            else:
                normalized_gemini = min(max(0, int(gemini_remaining or 0)), gemini_limit)
                normalized_groq = min(max(0, int(groq_remaining or 0)), groq_limit)
                if normalized_gemini != gemini_remaining or normalized_groq != groq_remaining:
                    gemini_remaining = normalized_gemini
                    groq_remaining = normalized_groq
                    update_payload.update({
                        "api_calls_remaining": gemini_remaining,
                        "groq_calls_remaining": groq_remaining
                    })
                    should_persist = bool(profile)

            if should_persist and update_payload:
                try:
                    supabase.table("profiles").update(update_payload).eq("id", user.id).execute()
                except Exception as e:
                    print("Auth current limits update error:", e)

            return {
                "authenticated": True,
                "gemini_remaining": gemini_remaining,
                "groq_remaining": groq_remaining,
                "gemini_limit": gemini_limit,
                "groq_limit": groq_limit,
                "auth_limit_message": get_auth_limit_message(),
                "has_custom_gemini_key": bool(custom_key),
                "last_reset_date": today
            }
        except Exception as e:
            print("Auth current limits error:", e)
            return {
                "authenticated": True,
                "gemini_remaining": gemini_limit,
                "groq_remaining": groq_limit,
                "gemini_limit": gemini_limit,
                "groq_limit": groq_limit,
                "auth_limit_message": get_auth_limit_message(),
                "has_custom_gemini_key": False,
                "last_reset_date": today
            }

    guest_gemini_limit = settings["guest_gemini_limit"]
    guest_groq_limit = settings["guest_groq_limit"]
    ip = get_request_ip(request)
    try:
        usage_res = supabase.table("anonymous_api_usage").select("*").eq("ip_address", ip).execute()
        usage = usage_res.data[0] if usage_res.data else None
        gemini_used = usage.get("calls_made", 0) if usage else 0
        groq_used = usage.get("groq_calls_made", 0) if usage else 0
        last_reset = _to_iso_date(usage.get("last_reset_date")) if usage else None

        if usage and last_reset != today:
            gemini_used = 0
            groq_used = 0
            try:
                supabase.table("anonymous_api_usage").update({
                    "calls_made": 0,
                    "groq_calls_made": 0,
                    "last_reset_date": today
                }).eq("ip_address", ip).execute()
            except Exception as e:
                print("Guest current limits reset error:", e)

        gemini_used = min(max(0, int(gemini_used or 0)), guest_gemini_limit)
        groq_used = min(max(0, int(groq_used or 0)), guest_groq_limit)

        return {
            "authenticated": False,
            "gemini_remaining": max(0, guest_gemini_limit - gemini_used),
            "groq_remaining": max(0, guest_groq_limit - groq_used),
            "gemini_limit": guest_gemini_limit,
            "groq_limit": guest_groq_limit,
            "guest_upgrade_message": get_guest_upgrade_message(settings),
            "last_reset_date": today
        }
    except Exception as e:
        print("Guest current limits error:", e)
        return {
            "authenticated": False,
            "gemini_remaining": guest_gemini_limit,
            "groq_remaining": guest_groq_limit,
            "gemini_limit": guest_gemini_limit,
            "groq_limit": guest_groq_limit,
            "guest_upgrade_message": get_guest_upgrade_message(settings),
            "last_reset_date": today
        }


def decode_data_url(data_url: str):
    match = re.match(r"^data:(image\/[a-zA-Z0-9.+-]+);base64,(.+)$", data_url)
    if not match:
        raise HTTPException(status_code=400, detail="Invalid image selection payload.")

    mime_type = match.group(1)
    encoded = match.group(2)
    try:
        image_bytes = base64.b64decode(encoded)
    except Exception:
        raise HTTPException(status_code=400, detail="Could not decode selected image.")

    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Selected area is too large. Please select a smaller region.")

    return mime_type, image_bytes


def build_proxy_safe_url(url: str):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only http/https files can be proxied.")
    return parsed


def check_and_deduct_rate_limit(request: Request, user, deduct=True):
    """Checks and optionally deducts rate limits. Returns (model_to_use, custom_key). IST-based reset."""
    _prune_deductions()
    settings = get_rate_limit_settings()
    auth_gemini_limit = settings["auth_gemini_limit"]
    auth_groq_limit = settings["auth_groq_limit"]
    guest_gemini_limit = settings["guest_gemini_limit"]
    guest_groq_limit = settings["guest_groq_limit"]
    today = get_ist_today()
    # Authenticated User limits + BYOK check
    if user:
        try:
            profile_res = supabase.table("profiles").select("*").eq("id", user.id).single().execute()
            if not profile_res.data:
                return "gemini", None
                
            profile = profile_res.data
            
            # --- BYOK Check ---
            custom_key = profile.get("custom_gemini_key")
            if custom_key:
                print("Status: Using User's Custom Gemini Key (BYOK).")
                return "custom_gemini", custom_key
            
            gemini_remaining = profile.get("api_calls_remaining", 0)
            groq_remaining = profile.get("groq_calls_remaining", 0)

            last_reset = _to_iso_date(profile.get("last_reset_date"))
            if last_reset != today:
                gemini_remaining = auth_gemini_limit
                groq_remaining = auth_groq_limit
                # Persist reset so the next check doesn't think we're still on the old day
                try:
                    supabase.table("profiles").update({
                        "api_calls_remaining": gemini_remaining,
                        "groq_calls_remaining": groq_remaining,
                        "last_reset_date": today
                    }).eq("id", user.id).execute()
                except Exception as e:
                    print("Auth reset update error:", e)
            else:
                normalized_gemini = min(max(0, gemini_remaining), auth_gemini_limit)
                normalized_groq = min(max(0, groq_remaining), auth_groq_limit)
                if normalized_gemini != gemini_remaining or normalized_groq != groq_remaining:
                    gemini_remaining = normalized_gemini
                    groq_remaining = normalized_groq
                    try:
                        supabase.table("profiles").update({
                            "api_calls_remaining": gemini_remaining,
                            "groq_calls_remaining": groq_remaining
                        }).eq("id", user.id).execute()
                    except Exception as e:
                        print("Auth limit normalization error:", e)

            print(f"DEBUG: Auth Limits Check -> Gemini: {gemini_remaining}, Groq: {groq_remaining}")
            
            # GLOBAL Grace Period (per user, not per endpoint)
            grace_key = f"auth_{user.id}"
            now = time.time()
            is_recent = grace_key in RECENT_DEDUCTIONS and (now - RECENT_DEDUCTIONS[grace_key] < 10) # 10s grace

            if gemini_remaining > 0:
                if deduct and not is_recent:
                    supabase.table("profiles").update({
                        "api_calls_remaining": gemini_remaining - 1, 
                        "groq_calls_remaining": groq_remaining,
                        "last_reset_date": today
                    }).eq("id", user.id).execute()
                    RECENT_DEDUCTIONS[grace_key] = now
                    print(f"Status: Gemini deducted (-1) for {user.id}")
                elif is_recent and deduct:
                    print(f"Status: Skipping Gemini deduction for {user.id} (Grace Period Active)")
                else:
                    print(f"Status: Gemini allowed (Check Only) for {user.id}")
                return "gemini", None
            elif groq_remaining > 0:
                if deduct and not is_recent:
                    supabase.table("profiles").update({
                        "api_calls_remaining": 0,
                        "groq_calls_remaining": groq_remaining - 1,
                        "last_reset_date": today
                    }).eq("id", user.id).execute()
                    RECENT_DEDUCTIONS[grace_key] = now
                    print(f"Status: Groq deducted (-1) for {user.id}")
                elif is_recent and deduct:
                    print(f"Status: Skipping Groq deduction for {user.id} (Grace Period Active)")
                else:
                    print(f"Status: Groq allowed (Check Only) for {user.id}")
                return "groq", None
            else:
                raise HTTPException(status_code=429, detail=get_auth_limit_message())
        except HTTPException:
            raise
        except Exception as e:
            print("Auth limit error:", e)
            return "gemini", None
            
    # Guest User tracking by IP
    else:
        ip = get_request_ip(request)

        # GLOBAL Grace Period (per IP)
        grace_key = f"guest_{ip}"
        now = time.time()
        is_recent = grace_key in RECENT_DEDUCTIONS and (now - RECENT_DEDUCTIONS[grace_key] < 10) # 10s grace

        try:
            usage_res = supabase.table("anonymous_api_usage").select("*").eq("ip_address", ip).execute()
            
            if len(usage_res.data) == 0:
                if guest_gemini_limit <= 0 and guest_groq_limit <= 0:
                    raise HTTPException(
                        status_code=429,
                        detail=f"Daily Limits Reached (Guest: {guest_gemini_limit} Gemini, {guest_groq_limit} Groq). Please Sign Up!"
                    )

                initial_gemini_calls = 0
                initial_groq_calls = 0
                initial_model = "gemini" if guest_gemini_limit > 0 else "groq"

                if deduct and not is_recent:
                    if guest_gemini_limit > 0:
                        initial_gemini_calls = 1
                    elif guest_groq_limit > 0:
                        initial_groq_calls = 1
                        initial_model = "groq"

                supabase.table("anonymous_api_usage").insert({
                    "ip_address": ip, 
                    "calls_made": initial_gemini_calls, 
                    "groq_calls_made": initial_groq_calls, 
                    "last_reset_date": today
                }).execute()
                
                if initial_gemini_calls > 0 or initial_groq_calls > 0:
                    RECENT_DEDUCTIONS[grace_key] = now
                
                print(f"Status: New Guest (IP: {ip}). Deducted Gemini: {initial_gemini_calls}, Groq: {initial_groq_calls}")
                return initial_model, None
            else:
                usage = usage_res.data[0]
                gemini_used = usage.get("calls_made", 0)
                groq_used = usage.get("groq_calls_made", 0)
                
                if usage.get("last_reset_date") != today:
                    gemini_used = 0
                    groq_used = 0
                    print(f"Status: Resetting limits to 0 for IP: {ip} (New day IST)")
                    
                print(f"DEBUG: Guest Limits -> IP: {ip}, Gemini: {gemini_used}/{guest_gemini_limit}, Groq: {groq_used}/{guest_groq_limit}")

                if gemini_used < guest_gemini_limit:
                    if deduct and not is_recent:
                        supabase.table("anonymous_api_usage").update({
                            "calls_made": gemini_used + 1, 
                            "groq_calls_made": groq_used,
                            "last_reset_date": today
                        }).eq("ip_address", ip).execute()
                        RECENT_DEDUCTIONS[grace_key] = now
                        print(f"Status: Guest Gemini deducted (+1) for {ip}")
                    elif is_recent and deduct:
                        print(f"Status: Skipping Guest Gemini deduction for {ip} (Grace Period Active)")
                    else:
                        print(f"Status: Guest Gemini allowed (Check Only) for {ip}")
                    return "gemini", None
                elif groq_used < guest_groq_limit:
                    if deduct and not is_recent:
                        supabase.table("anonymous_api_usage").update({
                            "calls_made": max(gemini_used, guest_gemini_limit),
                            "groq_calls_made": groq_used + 1,
                            "last_reset_date": today
                        }).eq("ip_address", ip).execute()
                        RECENT_DEDUCTIONS[grace_key] = now
                        print(f"Status: Guest Groq deducted (+1) for {ip}")
                    elif is_recent and deduct:
                        print(f"Status: Skipping Guest Groq deduction for {ip} (Grace Period Active)")
                    else:
                        print(f"Status: Guest Groq allowed (Check Only) for {ip}")
                    return "groq", None
                else:
                    raise HTTPException(
                        status_code=429,
                        detail=f"Daily Limits Reached (Guest: {guest_gemini_limit} Gemini, {guest_groq_limit} Groq). Please Sign Up!"
                    )
        except HTTPException:
            raise
        except Exception as e:
            print(f"DEBUG [check_and_deduct_rate_limit]: Guest limit error for IP {ip}: {e}")
            return "gemini", None

def generate_ai_json(prompt: str, model_to_use: str, custom_key: str = None, force_json: bool = True, images: List[str] = None) -> str:
    """Core generation wrapper handling BYOK, Gemini, and Groq fallback"""
    
    # --- Custom Key Logic ---
    if model_to_use == "custom_gemini" and custom_key:
        try:
            custom_client = genai.Client(api_key=custom_key)
            config = types.GenerateContentConfig()
            if force_json:
                config.response_mime_type = "application/json"

            contents = [prompt]
            if images:
                for img_data_url in images:
                    if not img_data_url: continue
                    mime, blob = decode_data_url(img_data_url)
                    contents.append(types.Part.from_bytes(data=blob, mime_type=mime))

            ai_response = custom_client.models.generate_content(
                model='gemini-2.0-flash',
                contents=contents,
                config=config
            )
            return ai_response.text.strip()
        except Exception as e:
            print(f"Custom Gemini generation failed: {e}")
            raise HTTPException(status_code=400, detail="Your custom API key is invalid or has exhausted its quota. Please update it in Settings.")
    
    if model_to_use == "groq":
        if not groq_client:
            raise HTTPException(status_code=500, detail="Groq API key not configured.")
        
        # Determine model and format based on presence of images
        model_name = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
        if images:
            model_name = "meta-llama/llama-4-scout-17b-16e-instruct"

        for attempt in range(1, 3):
            try:
                content = [{"type": "text", "text": prompt}]
                if images:
                    for img in images:
                        if not img: continue
                        content.append({"type": "image_url", "image_url": {"url": img}})

                kwargs = {
                    "messages": [{"role": "user", "content": content}],
                    "model": model_name
                }
                if force_json:
                    kwargs["response_format"] = {"type": "json_object"}
                    if "json" not in prompt.lower():
                        content[0]["text"] += " (Respond in valid JSON format)"

                chat_completion = groq_client.chat.completions.create(**kwargs)
                return chat_completion.choices[0].message.content.strip()
            except Exception as e:
                print(f"Groq generation attempt {attempt} failed: {e}")
                if attempt == 2:
                    err_msg = str(e)
                    raise HTTPException(status_code=500, detail=f"Groq AI Service failed. {err_msg}")
                # brief backoff before retrying
                time.sleep(0.8)
            
    # 1. Try Default Gemini
    try:
        config = types.GenerateContentConfig()
        if force_json:
            config.response_mime_type = "application/json"

        ai_response = client.models.generate_content(
            model='gemini-2.0-flash',
            contents=prompt,
            config=config
        )
        return ai_response.text.strip()
    except Exception as e:
        print(f"Gemini generation failed: {e}. Falling back to Groq...")
        
    # 2. Try Groq Fallback
    if not groq_client:
        raise HTTPException(status_code=500, detail="AI Services unavailable (Gemini failed and Groq not configured).")
        
    try:
        kwargs = {
            "messages": [{"role": "user", "content": prompt}],
            "model": "llama-3.3-70b-versatile"
        }
        if force_json:
            kwargs["response_format"] = {"type": "json_object"}
            if "json" not in prompt.lower():
                kwargs["messages"][0]["content"] += " (Respond in valid JSON format)"

        chat_completion = groq_client.chat.completions.create(**kwargs)
        return chat_completion.choices[0].message.content.strip()
    except Exception as e:
        print(f"Groq fallback failed: {e}")
        raise HTTPException(status_code=500, detail="All AI Services failed to generate content.")

# --- 4. DYNAMIC DROPDOWN & UTILITY ENDPOINTS ---

@app.post("/update-api-key")
async def update_api_key(req: ApiKeyRequest, user = Depends(get_current_user)):
    """Saves a user's custom API key to their profile"""
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    key_to_save = req.api_key.strip()
    if key_to_save and not key_to_save.startswith("AIza"):
        raise HTTPException(status_code=400, detail="Invalid Gemini API Key format.")
    if not key_to_save:
        key_to_save = None # Allow removing key

    try:
        supabase.table("profiles").update({"custom_gemini_key": key_to_save}).eq("id", user.id).execute()
        return {"status": "success"}
    except Exception as e:
        print("Update API key error:", e)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/branches")
async def get_branches():
    try:
        response = supabase.table("branches").select("*").execute()
        return response.data
    except Exception as e:
        print("Branches fetch error:", e)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/semesters/{branch_id}")
async def get_semesters(branch_id: str):
    try:
        response = supabase.table("semesters").select("*").eq("branch_id", branch_id).order("semester_number").execute()
        return response.data
    except Exception as e:
        print("Semesters fetch error:", e)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/subjects/{semester_id}")
async def get_subjects(semester_id: str):
    try:
        response = supabase.table("subjects").select("*").eq("semester_id", semester_id).execute()
        return response.data
    except Exception as e:
        print("Subjects fetch error:", e)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/syllabus-metadata")
async def get_syllabus_metadata():
    """Fetches all branches, semesters, and subjects in one go for frontend caching"""
    try:
        return _get_cached_syllabus_metadata()
    except Exception as e:
        print(f"Metadata fetch error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

# LRU cache (refreshes every ~5 min via TTL key trick)
def _syllabus_ttl_key():
    """Returns a key that changes every 5 minutes."""
    return int(time.time()) // 300

@lru_cache(maxsize=4)
def _get_cached_syllabus_metadata(ttl_key=None):
    if ttl_key is None:
        ttl_key = _syllabus_ttl_key()
    branches = supabase.table("branches").select("*").execute()
    semesters = supabase.table("semesters").select("*").order("semester_number").execute()
    subjects = supabase.table("subjects").select("*").execute()
    return {
        "branches": branches.data,
        "semesters": semesters.data,
        "subjects": subjects.data
    }

@app.get("/topics/{subject_id}")
async def get_topics(subject_id: str):
    try:
        response = supabase.table("topics").select("*").eq("subject_id", subject_id).order("unit_number").execute()
        return response.data
    except Exception as e:
        print("Topics fetch error:", e)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/pyqs/{subject_id}")
async def get_pyqs(subject_id: str):
    try:
        response = supabase.table("pyqs").select("*").eq("subject_id", subject_id).order("year", desc=True).execute()
        return response.data
    except Exception as e:
        print("PYQs fetch error:", e)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.get("/guest-limits")
async def get_guest_limits(request: Request):
    return get_current_limit_state(request, None)


@app.get("/my-limits")
async def get_my_limits(req: Request, user = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return get_current_limit_state(req, user)


@app.get("/rate-limit-settings")
async def get_public_rate_limit_settings():
    settings = get_rate_limit_settings()
    auth_total = settings["auth_gemini_limit"] + settings["auth_groq_limit"]
    return {
        **settings,
        "auth_total_limit": auth_total,
        "guest_upgrade_message": get_guest_upgrade_message(settings),
        "auth_limit_message": get_auth_limit_message()
    }


@app.get("/proxy-file")
async def proxy_file(url: str):
    parsed = build_proxy_safe_url(url)
    try:
        def fetch_data():
            upstream_request = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "RTUKaGyan Notes Proxy/1.0",
                    "Accept": "*/*"
                }
            )
            with urllib.request.urlopen(upstream_request, timeout=20) as response:
                return response.read(), response.headers.get("Content-Type")

        body, content_type = await asyncio.to_thread(fetch_data)

        if not content_type:
            guessed_type, _ = mimetypes.guess_type(parsed.path)
            content_type = guessed_type or "application/octet-stream"

        headers = {
            "Cache-Control": "public, max-age=1800",
            "Access-Control-Allow-Origin": "*"
        }
        return StreamingResponse(iter([body]), media_type=content_type, headers=headers)
    except HTTPException:
        raise
    except Exception as e:
        print("Proxy file error:", e)
        raise HTTPException(status_code=502, detail="Could not load the requested notes file.")


@app.post("/ask-notes-selection")
async def ask_notes_selection(request_body: NotesSelectionQuestionRequest, req: Request, user = Depends(get_current_user)):
    try:
        model_to_use, custom_key = check_and_deduct_rate_limit(req, user)
        
        # Collect parts for Gemini
        content_parts = []
        
        # Handle images
        imgs_to_decode = []
        if request_body.image_data_url:
            imgs_to_decode.append(request_body.image_data_url)
        if request_body.image_data_urls:
            imgs_to_decode.extend(request_body.image_data_urls)
            
        for data_url in imgs_to_decode:
            if not data_url: continue
            mime, blob = decode_data_url(data_url)
            content_parts.append(types.Part.from_bytes(data=blob, mime_type=mime))
            
        # Handle text context
        if request_body.text_content:
            content_parts.append(types.Part(text=f"\n--- Document Text Context ---\n{request_body.text_content}\n----------------------------\n"))

        active_client = client
        effective_model = "gemini"
        if model_to_use == "custom_gemini" and custom_key:
            active_client = genai.Client(api_key=custom_key)
            effective_model = "custom_gemini"
        elif not GEMINI_API_KEY:
            raise HTTPException(status_code=500, detail="Gemini API key is required for notes selection Q&A.")

        system_instruction = (
            f"You are helping an RTU B.Tech student study '{request_body.topic_name}' "
            f"for the subject '{request_body.subject_name}'. "
            f"The attached images or text are from their notes or PDF "
            f"(source: {request_body.source_label or 'notes viewer'}). "
            f"If provide range of pages, analyze them collectively. "
            f"If the visual content is blurry, incomplete, or does not contain the answer, "
            f"rely on the provided text context if available. "
            f"Answer the user's question clearly and helpfuly. "
            f"Student question: {request_body.question}"
        )

        history_parts = []
        if request_body.history:
            for msg in request_body.history:
                history_parts.append(types.Content(role=msg.role, parts=[types.Part(text=msg.text)]))

        # Add current question to content_parts
        content_parts.append(types.Part(text=request_body.question))

        try:
            chat_session = active_client.chats.create(
                model='gemini-2.0-flash',
                config=types.GenerateContentConfig(system_instruction=system_instruction),
                history=history_parts
            )
            ai_response = chat_session.send_message(message=content_parts)
            answer = (ai_response.text or "").strip()
        except Exception as e:
            err_str = str(e).lower()
            # If hit rate limit or quota, try Groq fallback for text
            if ("429" in err_str or "quota" in err_str or "limit" in err_str):
                print(f"Gemini quota hit, falling back to Groq. Error: {e}")
                
                fallback_prompt = (
                    f"System: {system_instruction}\n\n"
                    f"Note: You are currently acting as the fallback intelligence (RTU Secondary). "
                    f"Please analyze the provided data (text and/or images) to answer the student's question.\n\n"
                    f"Context Text:\n{request_body.text_content or '[No text context extracted]'}\n\n"
                    f"Question: {request_body.question}"
                )
                
                try:
                    # Pass images to fallback if they exist
                    fallback_images = []
                    if request_body.image_data_url: fallback_images.append(request_body.image_data_url)
                    if request_body.image_data_urls: fallback_images.extend(request_body.image_data_urls)
                    
                    answer = generate_ai_json(fallback_prompt, "groq", force_json=False, images=fallback_images)
                    effective_model = "groq (vision fallback)" if fallback_images else "groq (text fallback)"
                except Exception as groq_err:
                    print(f"Groq fallback also failed: {groq_err}")
                    raise HTTPException(status_code=429, detail="AI Assistant is currently overloaded. Please try again in a few seconds.")
            else:
                print(f"Gemini error: {e}")
                raise HTTPException(status_code=500, detail=f"AI Error: {str(e)}")

        if not answer:
            raise HTTPException(status_code=500, detail="The AI could not generate an answer from the provided context.")

        return {
            "answer": answer,
            "model_used": effective_model,
            "rate_limit_bucket_used": model_to_use
        }
    except HTTPException:
        raise
    except Exception as e:
        print("Notes selection Q&A error:", e)
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")

# --- 5. THE CORE ENDPOINT (The Cache Logic) ---
@app.post("/get-topic-data")
async def get_topic_data(request_body: TopicRequest, req: Request, user = Depends(get_current_user)):
    try:
        # Fetch topic links first (so we can still show materials when rate-limited)
        topic_info_response = supabase.table("topics").select("youtube_link, notes_link").eq("id", request_body.topic_id).execute()
        youtube_link = None
        notes_link = None
        if len(topic_info_response.data) > 0:
             youtube_link = topic_info_response.data[0].get("youtube_link")
             notes_link = topic_info_response.data[0].get("notes_link")

        # Rate limit check (may return rate-limited response but still provide links)
        try:
            model_to_use, custom_key = check_and_deduct_rate_limit(req, user)
        except HTTPException as http_exc:
            if http_exc.status_code == 429:
                # Return minimal info so frontend can still show study materials
                raise HTTPException(status_code=429, detail={
                    "message": http_exc.detail,
                    "youtube_link": youtube_link,
                    "notes_link": notes_link
                })
            raise

        response = supabase.table("ai_content_cache").select("*").eq("topic_id", request_body.topic_id).execute()

        # Cache Hit
        if len(response.data) > 0:
            print("CACHE HIT: Serving from Supabase")
            cached_data = response.data[0]
            return {
                "source": "cache",
                "pomodoro_plan": cached_data["pomodoro_json"],
                "quiz": cached_data["quiz_json"],
                "youtube_link": youtube_link,
                "notes_link": notes_link
            }

        # Cache Miss
        # model_to_use, custom_key = check_and_deduct_rate_limit(req, user)
        
        print(f"CACHE MISS: Generating new content with AI ({model_to_use})")
        prompt = get_system_prompt(request_body.topic_name, request_body.subject_name)
        
        raw_json_str = generate_ai_json(prompt, model_to_use, custom_key)
        
        try:
            parsed_data = json.loads(raw_json_str)
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="AI returned invalid JSON format.")

        # Normalize the quiz items so different LLM outputs still match the expected schema.
        raw_quiz_items = parsed_data.get("quiz", [])
        normalized_quiz = [q for q in (normalize_quiz_question(i) for i in raw_quiz_items) if q]

        # Cache the generated content. If caching fails, we still return the generated data
        # so the user doesn't see an error for a caching issue.
        try:
            supabase.table("ai_content_cache").insert({
                "topic_id": request_body.topic_id,
                "pomodoro_json": parsed_data["pomodoro_plan"],
                "quiz_json": normalized_quiz
            }).execute()
        except Exception as cache_err:
            print(f"WARNING: Failed to cache AI content for topic {request_body.topic_id}: {cache_err}")

        for q in normalized_quiz:
            try:
                supabase.table("quiz_questions").insert({
                    "topic_id": request_body.topic_id,
                    "difficulty": "Medium",
                    "question": q["question"],
                    "options": q["options"],
                    "correct_answer_index": q["correct_answer_index"],
                    "explanation": q.get("explanation", "")
                }).execute()
            except Exception:
                pass

        return {
            "source": "ai_generated",
            "model_used": model_to_use,
            "pomodoro_plan": parsed_data["pomodoro_plan"],
            "quiz": normalized_quiz,
            "youtube_link": youtube_link,
            "notes_link": notes_link
        }

    except Exception as e:
        print("Get topic data error:", e)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/generate-custom-quiz")
async def generate_custom_quiz(request_body: CustomQuizRequest, req: Request, user = Depends(get_current_user)):
    try:
        model_to_use, custom_key = check_and_deduct_rate_limit(req, user)
        print(f"Generating Custom Quiz: {request_body.num_questions} Qs, {request_body.difficulty} difficulty ({model_to_use})")
        prompt = get_custom_quiz_prompt(request_body.topic_name, request_body.subject_name, request_body.num_questions, request_body.difficulty)
        
        raw_json_str = generate_ai_json(prompt, model_to_use, custom_key)
        
        try:
            parsed_data = json.loads(raw_json_str)
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="AI returned invalid JSON format.")
            
        return {
            "source": "ai_generated_custom",
            "model_used": model_to_use,
            "quiz": parsed_data.get("quiz", [])
        }
    except HTTPException:
        # Propagate expected HTTP errors (e.g., rate limit 429) to the client
        raise
    except Exception as e:
        print("Custom quiz error:", e)
        raise HTTPException(status_code=500, detail="Internal server error")

# --- 7. QUIZ QUESTION POOL ENDPOINTS ---
MAX_QUESTIONS_PER_TOPIC = 200

@app.get("/quiz-questions/{topic_id}")
async def get_quiz_questions(topic_id: str, req: Request, count: int = 10, difficulty: str = "Medium", user = Depends(get_current_user)):
    try:
        # Check limits WITHOUT deducting (this endpoint is a light fetch)
        check_and_deduct_rate_limit(req, user, deduct=False)
        response = supabase.table("quiz_questions").select("*").eq("topic_id", topic_id).eq("difficulty", difficulty).execute()
        data = response.data or []
        random.shuffle(data)
        return data[:count]
    except HTTPException:
        # Propagate expected HTTP errors (e.g., rate limit 429) to the client
        raise
    except Exception as e:
        print("Quiz questions fetch error:", e)
        raise HTTPException(status_code=500, detail="Internal server error")

@app.post("/generate-quiz-batch")
async def generate_quiz_batch(request_body: QuizBatchRequest, req: Request, user = Depends(get_current_user)):
    try:
        model_to_use, custom_key = check_and_deduct_rate_limit(req, user)
        num = min(request_body.num_questions, 30)
        print(f"Generating batch: {num} Qs, {request_body.difficulty} difficulty for topic {request_body.topic_id} ({model_to_use})")
        prompt = get_custom_quiz_prompt(request_body.topic_name, request_body.subject_name, num, request_body.difficulty)
        
        raw_json_str = generate_ai_json(prompt, model_to_use, custom_key)
        
        try:
            parsed_data = json.loads(raw_json_str)
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="AI returned invalid JSON format.")
        
        raw_quiz_items = parsed_data.get("quiz", [])
        quiz_items = [q for q in (normalize_quiz_question(i) for i in raw_quiz_items) if q]

        for q in quiz_items:
            try:
                supabase.table("quiz_questions").insert({
                    "topic_id": request_body.topic_id,
                    "difficulty": request_body.difficulty,
                    "question": q["question"],
                    "options": q["options"],
                    "correct_answer_index": q["correct_answer_index"],
                    "explanation": q.get("explanation", "")
                }).execute()
            except Exception as insert_err:
                pass
        
        try:
            all_qs = supabase.table("quiz_questions").select("id, created_at").eq("topic_id", request_body.topic_id).order("created_at", desc=True).execute()
            if len(all_qs.data) > MAX_QUESTIONS_PER_TOPIC:
                excess = all_qs.data[MAX_QUESTIONS_PER_TOPIC:]
                excess_ids = [row["id"] for row in excess]
                for eid in excess_ids:
                    supabase.table("quiz_questions").delete().eq("id", eid).execute()
        except Exception as prune_err:
            pass
        
        return {
            "source": "ai_generated_batch",
            "model_used": model_to_use,
            "quiz": quiz_items
        }
    except HTTPException:
        # Propagate expected HTTP errors (e.g., rate limit 429) to the client
        raise
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        print("Quiz batch error:", err)
        raise HTTPException(status_code=500, detail="Internal server error")

# --- 8. STATS TRACKING ENDPOINTS ---
class QuizScoreRequest(BaseModel):
    topic_id: str
    score: int
    total_questions: int

@app.post("/submit-quiz-score")
async def submit_quiz_score(req: QuizScoreRequest, user = Depends(get_current_user)):
    if not user: return {"status": "ignored"}
    try:
        supabase.table("quiz_scores").insert({"user_id": user.id, "topic_id": req.topic_id, "score": req.score, "total_questions": req.total_questions}).execute()
        return {"status": "success"}
    except Exception as e:
        return {"status": "error"}

@app.get("/user-stats")
async def get_user_stats(user = Depends(get_current_user)):
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        # Fetch latest 10 scores with topic names
        # Explicitly choose the topic relationship to avoid ambiguity when multiple FKs exist
        scores_res = supabase.table("quiz_scores")\
            .select("score, total_questions, created_at, topic_id, topics!quiz_scores_topic_fk(topic_name)")\
            .eq("user_id", user.id)\
            .order("created_at", desc=True)\
            .limit(10)\
            .execute()
        
        # Fetch focus session summary
        sessions_res = supabase.table("focus_sessions")\
            .select("status")\
            .eq("user_id", user.id)\
            .execute()
        
        sessions = sessions_res.data or []
        completed_sessions = sum(1 for s in sessions if s['status'] == 'completed')
        failed_sessions = sum(1 for s in sessions if s['status'] in ['failed', 'abandoned'])
        
        # Get actual count of all quizzes taken
        count_res = supabase.table("quiz_scores").select("id", count="exact").eq("user_id", user.id).execute()
        total_quizzes = count_res.count if hasattr(count_res, 'count') else len(scores_res.data)

        return {
            "recent_scores": scores_res.data,
            "completed_focus_sessions": completed_sessions,
            "failed_focus_sessions": failed_sessions,
            "total_quizzes_taken": total_quizzes
        }
    except Exception as e:
        print(f"Stats fetch error: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")

class FocusSessionRequest(BaseModel):
    topic_id: str
    duration_minutes: int
    status: str

@app.post("/submit-focus-session")
async def submit_focus_session(req: FocusSessionRequest, user = Depends(get_current_user)):
    if not user: return {"status": "ignored"}
    try:
        supabase.table("focus_sessions").insert({"user_id": user.id, "topic_id": req.topic_id, "duration_minutes": req.duration_minutes, "status": req.status}).execute()
        return {"status": "success"}
    except Exception as e:
        return {"status": "error"}

# --- SEVE FRONTEND ---
app.mount("/assets", StaticFiles(directory="assets"), name="assets")

@app.get("/")
async def read_index():
    return FileResponse("index.html")

@app.get("/{filename}.html")
async def read_html(filename: str):
    # Path traversal protection
    if not re.match(r'^[a-zA-Z0-9_-]+$', filename):
        raise HTTPException(status_code=400, detail="Invalid filename")
    filepath = f"{filename}.html"
    if not os.path.isfile(filepath):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(filepath)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=5500)