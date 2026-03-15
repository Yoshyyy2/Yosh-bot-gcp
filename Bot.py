#!/usr/bin/env python3
"""
🚀 Yosh GC Bot — Fixed Version
- No gcloud config set project (was timing out)
- Uses --project flag directly in every command
- Random UUID per deploy
- Yosh branding on paths and labels
"""

import asyncio
import logging
import os
import re
import uuid
import json
import time
import tempfile
import urllib.parse
from datetime import datetime
from typing import Optional
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────
#  Config
# ─────────────────────────────────────────
BOT_TOKEN    = os.environ.get("BOT_TOKEN", "8755676259:AAFQQZzPmmT7fVQW2NWk8-eFo9bP4ExBgVY")
ADMIN_IDS    = [int(x) for x in os.environ.get("6601184733", "0").split(",") if x.strip().isdigit()]
CHANNEL_TAG  = os.environ.get("CHANNEL_TAG", "@YoshVPN")

DEFAULT_REGION  = os.environ.get("DEFAULT_REGION", "us-central1")
DEFAULT_CPU     = os.environ.get("DEFAULT_CPU", "2")
DEFAULT_MEMORY  = os.environ.get("DEFAULT_MEMORY", "2Gi")
SERVICE_PREFIX  = os.environ.get("SERVICE_PREFIX", "yosh-vip")
MAX_RETRIES     = int(os.environ.get("MAX_RETRIES", "3"))
DEPLOY_TIMEOUT  = int(os.environ.get("DEPLOY_TIMEOUT", "600"))
GCLOUD_PATH     = os.environ.get("GCLOUD_PATH", "/root/google-cloud-sdk/bin/gcloud")

# ─────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("/tmp/yosh_bot.log")
    ]
)
log = logging.getLogger("YoshBot")

# ─────────────────────────────────────────
#  State
# ─────────────────────────────────────────
active_jobs: dict = {}
user_stats: dict  = defaultdict(lambda: {"total": 0, "success": 0, "fail": 0})

# ─────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────
def gen_uuid() -> str:
    return str(uuid.uuid4())

def gen_service_name() -> str:
    suffix = str(int(time.time()))[-5:]
    return f"{SERVICE_PREFIX}{suffix}"

def extract_project_id(url: str) -> Optional[str]:
    """Extract project ID from Qwiklabs URL."""
    # Try decoded version
    decoded = urllib.parse.unquote(url)
    patterns = [
        r"project=(qwiklabs-gcp-[a-z0-9-]+)",
        r"project%3D(qwiklabs-gcp-[a-z0-9-]+)",
        r"(qwiklabs-gcp-[a-z0-9-]+)",
    ]
    for pat in patterns:
        m = re.search(pat, decoded)
        if m:
            return m.group(1)
    return None

def parse_credentials(text: str) -> Optional[dict]:
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    email = password = url = None

    for token in lines:
        if re.match(r"[^@\s]+@[^@\s]+\.[^@\s]+", token):
            email = token
        elif token.startswith("http"):
            url = token
        elif email and not password and token != email:
            password = token

    if email and password and url:
        return {
            "email": email,
            "password": password,
            "console_url": url,
            "project_id": extract_project_id(url),
        }
    return None

def build_vless_grpc_uri(host: str, vless_uuid: str) -> str:
    label = urllib.parse.quote(f"Yosh-VLESS-gRPC | {CHANNEL_TAG}")
    return (
        f"vless://{vless_uuid}@vpn.googleapis.com:443"
        f"?mode=gun&security=tls&encryption=none"
        f"&type=grpc&serviceName=grpc-yosh"
        f"&sni={host}#{label}"
    )

def build_dark_config(host: str, vless_uuid: str) -> str:
    return json.dumps({
        "server": host,
        "port": 443,
        "protocol": "vless-grpc",
        "uuid": vless_uuid,
        "serviceName": "grpc-yosh",
        "tls": True,
        "sni": host,
        "allowInsecure": False,
        "mux": False,
        "remark": f"Yosh-VIP | {host}"
    }, indent=2)

def region_flag(region: str) -> str:
    return {
        "us-central1":     "🇺🇸 Iowa",
        "asia-southeast1": "🇸🇬 Singapore",
        "asia-southeast2": "🇮🇩 Indonesia",
        "asia-northeast1": "🇯🇵 Japan",
    }.get(region, f"🌍 {region}")

def fmt_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%d.%m.%Y %I:%M %p")

# ─────────────────────────────────────────
#  Shell Command Runner
# ─────────────────────────────────────────
async def run_cmd(cmd: list, timeout: int = 60) -> tuple[int, str, str]:
    env = {**os.environ, "PATH": f"/root/google-cloud-sdk/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode().strip(), stderr.decode().strip()
    except asyncio.TimeoutError:
        try: proc.kill()
        except: pass
        return -1, "", f"Timed out after {timeout}s"
    except Exception as e:
        return -1, "", str(e)

async def run_retry(cmd: list, label: str, retries: int = MAX_RETRIES,
                    delay: int = 5, timeout: int = 120) -> tuple[bool, str]:
    for attempt in range(1, retries + 1):
        rc, out, err = await run_cmd(cmd, timeout=timeout)
        if rc == 0:
            return True, out or err
        msg = err or out or "unknown error"
        log.warning(f"[{label}] attempt {attempt}/{retries} failed: {msg[:150]}")
        if attempt < retries:
            await asyncio.sleep(delay)
    return False, msg

# ─────────────────────────────────────────
#  GCP Steps — all use --project flag
# ─────────────────────────────────────────
async def get_project_number(project_id: str) -> Optional[str]:
    rc, out, err = await run_cmd(
        [GCLOUD_PATH, "projects", "describe", project_id,
         "--format=value(projectNumber)", "--quiet"],
        timeout=30
    )
    return out.strip() if rc == 0 and out.strip().isdigit() else None

async def enable_apis(project_id: str) -> tuple[bool, str]:
    return await run_retry(
        [GCLOUD_PATH, "services", "enable",
         "run.googleapis.com", "cloudbuild.googleapis.com",
         f"--project={project_id}", "--quiet"],
        label="enable-apis",
        timeout=120
    )

async def deploy_service(service_name: str, project_id: str) -> tuple[bool, str]:
    return await run_retry(
        [GCLOUD_PATH, "run", "deploy", service_name,
         "--image=docker.io/n4pro/vlessgrpc:latest",
         "--platform=managed",
         f"--region={DEFAULT_REGION}",
         f"--memory={DEFAULT_MEMORY}",
         f"--cpu={DEFAULT_CPU}",
         f"--project={project_id}",
         "--timeout=3600",
         "--allow-unauthenticated",
         "--port=8080",
         "--min-instances=1",
         "--quiet"],
        label="deploy",
        retries=MAX_RETRIES,
        timeout=DEPLOY_TIMEOUT
    )

async def verify_url(url: str) -> bool:
    import aiohttp
    for _ in range(3):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=10), ssl=False) as r:
                    if r.status in [200, 301, 302, 400, 401, 403, 404]:
                        return True
        except Exception:
            pass
        await asyncio.sleep(8)
    return False

# ─────────────────────────────────────────
#  Main Deploy Flow
# ─────────────────────────────────────────
async def deploy_for_user(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           creds: dict, user_id: int):
    chat_id     = update.effective_chat.id
    start_epoch = time.time()
    vless_uuid  = gen_uuid()
    service_name = gen_service_name()
    project_id  = creds["project_id"]

    async def msg(text: str):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning(f"msg send failed: {e}")

    try:
        # ── Step 1: Validate project ID ──
        active_jobs[user_id] = {"status": "authenticating", "started": start_epoch}
        await msg("🔐 <b>Step 1/5</b> — Validating project...")

        if not project_id:
            await msg("❌ <b>Could not extract project ID from URL.</b>\n\nMake sure you send the full console URL!")
            active_jobs.pop(user_id, None)
            user_stats[user_id]["fail"] += 1
            return

        await msg(f"✅ <b>Step 1/5</b> — Project found!\n<code>{project_id}</code>")

        # ── Step 2: Get project number ──
        active_jobs[user_id]["status"] = "getting_project_number"
        project_number = await get_project_number(project_id)
        if not project_number:
            # Fallback — generate from service name only
            project_number = "000000000000"
            log.warning(f"Could not get project number for {project_id}, using fallback")

        canonical_host = f"{service_name}-{project_number}.{DEFAULT_REGION}.run.app"
        service_url    = f"https://{canonical_host}"

        # ── Step 3: Enable APIs ──
        active_jobs[user_id]["status"] = "enabling_apis"
        await msg("⚙️ <b>Step 2/5</b> — Enabling Cloud Run APIs...")
        ok, out = await enable_apis(project_id)
        if not ok:
            await msg(f"❌ <b>API enable failed:</b>\n<code>{out[:300]}</code>\n\n💡 Make sure your lab is still active!")
            active_jobs.pop(user_id, None)
            user_stats[user_id]["fail"] += 1
            return
        await msg("✅ <b>Step 2/5</b> — APIs enabled!")

        # ── Step 4: Deploy ──
        active_jobs[user_id]["status"] = "deploying"
        await msg(
            f"🚀 <b>Step 3/5</b> — Deploying Cloud Run...\n"
            f"<blockquote>"
            f"📦 <b>Service:</b> <code>{service_name}</code>\n"
            f"🌍 <b>Region:</b> {region_flag(DEFAULT_REGION)}\n"
            f"🧮 <b>CPU/RAM:</b> {DEFAULT_CPU} vCPU / {DEFAULT_MEMORY}"
            f"</blockquote>\n"
            f"⏳ This takes 2-3 minutes..."
        )
        ok, out = await deploy_service(service_name, project_id)
        if not ok:
            await msg(
                f"❌ <b>Deploy failed</b> after {MAX_RETRIES} attempts.\n"
                f"<code>{out[:400]}</code>"
            )
            active_jobs.pop(user_id, None)
            user_stats[user_id]["fail"] += 1
            return
        await msg("✅ <b>Step 3/5</b> — Deployed!")

        # ── Step 5: Get real URL from gcloud ──
        active_jobs[user_id]["status"] = "verifying"
        rc, real_url, _ = await run_cmd(
            [GCLOUD_PATH, "run", "services", "describe", service_name,
             f"--region={DEFAULT_REGION}",
             f"--project={project_id}",
             "--format=value(status.url)",
             "--quiet"],
            timeout=30
        )
        if rc == 0 and real_url.startswith("https://"):
            service_url    = real_url.strip()
            canonical_host = service_url.replace("https://", "")
            log.info(f"Got real URL: {service_url}")

        await msg("🔎 <b>Step 4/5</b> — Verifying service...")
        is_live = await verify_url(service_url)
        verify_status = "🟢 Online" if is_live else "🟡 Starting up"
        await msg(f"{'✅' if is_live else '⚠️'} <b>Step 4/5</b> — {verify_status}")

        # ── Step 6: Generate keys ──
        active_jobs[user_id]["status"] = "generating_keys"
        await msg("🔑 <b>Step 5/5</b> — Generating VLESS key...")

        vless_uri   = build_vless_grpc_uri(canonical_host, vless_uuid)
        dark_config = build_dark_config(canonical_host, vless_uuid)
        end_epoch   = start_epoch + 5 * 3600

        # ── Final result ──
        result_msg = (
            f"✅ <b>Deploy Success!</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<blockquote>"
            f"📦 <b>Service:</b> <code>{service_name}</code>\n"
            f"🌍 <b>Region:</b> {region_flag(DEFAULT_REGION)}\n"
            f"⚡ <b>Protocol:</b> VLESS gRPC\n"
            f"🧮 <b>CPU/RAM:</b> {DEFAULT_CPU} vCPU / {DEFAULT_MEMORY}\n"
            f"🔗 <b>URL:</b> <a href='{service_url}'>{canonical_host}</a>\n"
            f"📡 <b>Status:</b> {verify_status}"
            f"</blockquote>\n\n"
            f"🔑 <b>VLESS gRPC Key:</b>\n"
            f"<pre><code>{vless_uri}</code></pre>\n\n"
            f"<blockquote>"
            f"🕒 <b>Start:</b> {fmt_time(start_epoch)}\n"
            f"⏳ <b>Expires:</b> {fmt_time(end_epoch)}\n"
            f"🆔 <b>Project:</b> {project_id}"
            f"</blockquote>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<i>🚀 Powered by Yosh Bot | {CHANNEL_TAG}</i>"
        )

        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{CHANNEL_TAG.lstrip('@')}")],
            [InlineKeyboardButton("🔄 Deploy Another", callback_data="deploy_new")]
        ])

        await context.bot.send_message(
            chat_id=chat_id,
            text=result_msg,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )

        # Send DarkTunnel file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".dark",
                                          prefix=f"{service_name}_", delete=False) as f:
            f.write(dark_config)
            dark_path = f.name

        await context.bot.send_document(
            chat_id=chat_id,
            document=open(dark_path, "rb"),
            filename=f"{service_name}.dark",
            caption=f"✅ <b>DarkTunnel Config</b>\n<code>{canonical_host}</code>",
            parse_mode=ParseMode.HTML
        )
        os.unlink(dark_path)

        # Update stats
        user_stats[user_id]["total"]   += 1
        user_stats[user_id]["success"] += 1
        active_jobs[user_id] = {
            "status": "done", "host": canonical_host,
            "project": project_id, "started": start_epoch
        }

        # Notify admins
        for admin_id in ADMIN_IDS:
            if admin_id:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"📊 <b>New Deploy!</b>\n"
                            f"👤 User: <code>{user_id}</code>\n"
                            f"📦 Service: <code>{service_name}</code>\n"
                            f"🆔 Project: <code>{project_id}</code>\n"
                            f"🕒 Time: {fmt_time(start_epoch)}"
                        ),
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass

    except Exception as e:
        log.exception(f"Error for user {user_id}: {e}")
        active_jobs.pop(user_id, None)
        user_stats[user_id]["fail"] += 1
        await msg(
            f"💥 <b>Error:</b>\n<code>{str(e)[:300]}</code>\n\n"
            f"Please try again or contact {CHANNEL_TAG}"
        )

# ─────────────────────────────────────────
#  Handlers
# ─────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{CHANNEL_TAG.lstrip('@')}")]
    ])
    await update.message.reply_text(
        f"👋 <b>Welcome to Yosh GC Bot!</b>\n\n"
        f"🚀 Auto-deploy <b>VLESS gRPC</b> on Google Cloud Run\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>Send your credentials:</b>\n\n"
        f"<code>student-XX@qwiklabs.net\n"
        f"YourPassword\n"
        f"https://www.skills.google/...</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"/help — Guide | /status — Check deploy\n\n"
        f"<i>🔥 {CHANNEL_TAG}</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📖 <b>How to use Yosh GC Bot</b>\n\n"
        f"1️⃣ Open your Qwiklabs lab\n"
        f"2️⃣ Copy: email, password, console URL\n"
        f"3️⃣ Send all 3 here (one per line)\n"
        f"4️⃣ Wait ~3 minutes ⏳\n"
        f"5️⃣ Get VLESS gRPC key + DarkTunnel file!\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ Keys expire when lab ends (~1-5hrs)\n"
        f"⚠️ One deploy at a time per user\n\n"
        f"<i>{CHANNEL_TAG}</i>",
        parse_mode=ParseMode.HTML
    )

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    job     = active_jobs.get(user_id)
    stats   = user_stats[user_id]

    if not job or job.get("status") in ("done", "failed"):
        await update.message.reply_text(
            f"📊 <b>Your Stats</b>\n\n"
            f"✅ Success: {stats['success']}\n"
            f"❌ Failed: {stats['fail']}\n\n"
            f"No active deploy. Send credentials to start!",
            parse_mode=ParseMode.HTML
        )
        return

    elapsed = int(time.time() - job.get("started", time.time()))
    status_map = {
        "authenticating":         "🔐 Validating...",
        "getting_project_number": "🔢 Getting project...",
        "enabling_apis":          "⚙️ Enabling APIs...",
        "deploying":              "🚀 Deploying...",
        "verifying":              "🔎 Verifying...",
        "generating_keys":        "🔑 Generating keys...",
    }
    label = status_map.get(job.get("status", ""), "⏳ Working...")
    await update.message.reply_text(
        f"📊 <b>Deploy Status</b>\n\n"
        f"🔄 {label}\n"
        f"⏱️ Elapsed: {elapsed}s",
        parse_mode=ParseMode.HTML
    )

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return
    active = sum(1 for j in active_jobs.values() if j.get("status") not in ("done", "failed"))
    await update.message.reply_text(
        f"👑 <b>Admin Stats</b>\n\n"
        f"👥 Users: {len(user_stats)}\n"
        f"🔄 Active: {active}\n"
        f"✅ Success: {sum(s['success'] for s in user_stats.values())}\n"
        f"❌ Failed: {sum(s['fail'] for s in user_stats.values())}",
        parse_mode=ParseMode.HTML
    )

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text    = update.message.text or ""

    job = active_jobs.get(user_id, {})
    if job.get("status") not in (None, "done", "failed"):
        elapsed = int(time.time() - job.get("started", time.time()))
        await update.message.reply_text(
            f"⏳ <b>Deploy in progress</b> ({elapsed}s)\n\nPlease wait...",
            parse_mode=ParseMode.HTML
        )
        return

    creds = parse_credentials(text)
    if not creds:
        await update.message.reply_text(
            "❓ <b>Invalid format.</b>\n\n"
            "Send like this:\n"
            "<code>student-XX@qwiklabs.net\n"
            "Password\n"
            "https://www.skills.google/...</code>",
            parse_mode=ParseMode.HTML
        )
        return

    await update.message.reply_text(
        f"✅ <b>Credentials received!</b>\n\n"
        f"<blockquote>"
        f"👤 <b>Account:</b> <code>{creds['email']}</code>\n"
        f"🆔 <b>Project:</b> <code>{creds['project_id'] or 'detecting...'}</code>"
        f"</blockquote>\n\n"
        f"🚀 Starting deployment... Please wait.\n"
        f"<i>Step-by-step updates coming below!</i>",
        parse_mode=ParseMode.HTML
    )

    asyncio.create_task(deploy_for_user(update, context, creds, user_id))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    if update.callback_query.data == "deploy_new":
        await update.callback_query.message.reply_text(
            "📋 Send new credentials:\n\n"
            "<code>email\npassword\nconsole_url</code>",
            parse_mode=ParseMode.HTML
        )

# ─────────────────────────────────────────
#  Main
# ─────────────────────────────────────────
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        log.error("❌ Set BOT_TOKEN first!")
        return

    log.info(f"🚀 Yosh GC Bot starting | gcloud: {GCLOUD_PATH}")

    # Verify gcloud exists
    if not os.path.exists(GCLOUD_PATH):
        log.warning(f"⚠️ gcloud not found at {GCLOUD_PATH}!")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats",  cmd_stats))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    log.info(f"✅ Running | Channel: {CHANNEL_TAG} | Admins: {ADMIN_IDS}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
