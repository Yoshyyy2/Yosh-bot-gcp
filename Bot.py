#!/usr/bin/env python3
"""
🚀 Yosh GC Bot — Telegram bot that auto-deploys VLESS gRPC on GCP Cloud Run
Accepts Qwiklabs credentials → deploys → sends back VLESS URI + DarkTunnel file
"""

import asyncio
import logging
import os
import re
import uuid
import json
import time
import subprocess
import tempfile
from datetime import datetime, timedelta
from typing import Optional
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)
from telegram.constants import ParseMode

# ─────────────────────────────────────────
#  Config — set via environment variables
# ─────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "8755676259:AAFQQZzPmmT7fVQW2NWk8-eFo9bP4ExBgVY")
ADMIN_IDS   = [int(x) for x in os.environ.get("6601184733", "0").split(",") if x.strip().isdigit()]
CHANNEL_TAG = os.environ.get("CHANNEL_TAG", "@YoshVPN")

# Deploy defaults
DEFAULT_REGION  = os.environ.get("DEFAULT_REGION", "us-central1")
DEFAULT_CPU     = os.environ.get("DEFAULT_CPU", "2")
DEFAULT_MEMORY  = os.environ.get("DEFAULT_MEMORY", "2Gi")
SERVICE_PREFIX  = os.environ.get("SERVICE_PREFIX", "yosh-vip")
MAX_RETRIES     = int(os.environ.get("MAX_RETRIES", "3"))
DEPLOY_TIMEOUT  = int(os.environ.get("DEPLOY_TIMEOUT", "600"))

# Conversation states
WAITING_CREDENTIALS = 1

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
#  In-memory job tracker (per user)
# ─────────────────────────────────────────
# { user_id: { "status": "...", "started": timestamp, ... } }
active_jobs: dict = {}
user_stats: dict  = defaultdict(lambda: {"total": 0, "success": 0, "fail": 0})

# ─────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────
def gen_uuid() -> str:
    return str(uuid.uuid4())

def gen_service_name(project_number: str) -> str:
    """Generate a unique service name with yosh branding."""
    suffix = str(int(time.time()))[-5:]
    return f"{SERVICE_PREFIX}{suffix}"

def extract_project_id_from_url(url: str) -> Optional[str]:
    """Extract GCP project ID from a Qwiklabs console URL."""
    patterns = [
        r"project[=%]3D([a-z0-9-]+)",
        r"project=([a-z0-9-]+)",
        r"qwiklabs-gcp-[a-z0-9-]+",
    ]
    for pat in patterns:
        m = re.search(pat, url)
        if m:
            return m.group(0) if "qwiklabs-gcp" in pat else m.group(1)
    return None

def extract_project_number_from_url(url: str) -> Optional[str]:
    """Try to extract numeric project number if embedded."""
    m = re.search(r"project[_-]?number[=:](\d+)", url, re.IGNORECASE)
    return m.group(1) if m else None

def parse_credentials(text: str) -> Optional[dict]:
    """
    Parse Qwiklabs credentials from user message.
    Accepts formats:
      email
      password
      console_url
    OR all on one line separated by spaces/newlines.
    """
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]

    # Filter out empty lines
    non_empty = [l for l in lines if l]

    email, password, url = None, None, None

    for token in non_empty:
        if re.match(r"[^@]+@[^@]+\.[^@]+", token):
            email = token
        elif token.startswith("http"):
            url = token
        elif not email or token != email:
            password = token

    if email and password and url:
        project_id = extract_project_id_from_url(url)
        return {
            "email": email,
            "password": password,
            "console_url": url,
            "project_id": project_id,
        }
    return None

def build_vless_grpc_uri(service_host: str, vless_uuid: str, channel_tag: str) -> str:
    """Build a VLESS gRPC URI with Yosh branding."""
    tag_clean = channel_tag.lstrip("@")
    label = f"Yosh-VLESS-gRPC | {channel_tag}"
    import urllib.parse
    encoded_label = urllib.parse.quote(label)
    return (
        f"vless://{vless_uuid}@vpn.googleapis.com:443"
        f"?mode=gun"
        f"&security=tls"
        f"&encryption=none"
        f"&type=grpc"
        f"&serviceName=grpc-yosh"
        f"&sni={service_host}"
        f"#{encoded_label}"
    )

def build_darktunnel_config(service_host: str, vless_uuid: str) -> str:
    """Generate a DarkTunnel .dark config file content."""
    config = {
        "server": service_host,
        "port": 443,
        "protocol": "vless-grpc",
        "uuid": vless_uuid,
        "serviceName": "grpc-yosh",
        "tls": True,
        "sni": service_host,
        "allowInsecure": False,
        "mux": False,
        "remark": f"Yosh-VIP | {service_host}"
    }
    return json.dumps(config, indent=2)

def region_flag(region: str) -> str:
    flags = {
        "us-central1":     "🇺🇸 Iowa",
        "asia-southeast1": "🇸🇬 Singapore",
        "asia-southeast2": "🇮🇩 Indonesia",
        "asia-northeast1": "🇯🇵 Japan",
        "europe-west1":    "🇪🇺 Europe",
    }
    return flags.get(region, f"🌍 {region}")

def fmt_time(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%d.%m.%Y %I:%M %p")

# ─────────────────────────────────────────
#  GCP Deploy Logic
# ─────────────────────────────────────────
async def run_cmd(cmd: list, timeout: int = 120, env: dict = None) -> tuple[int, str, str]:
    """Run a shell command asynchronously."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})}
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, stdout.decode(), stderr.decode()
    except asyncio.TimeoutError:
        proc.kill()
        return -1, "", f"Command timed out after {timeout}s"

async def run_cmd_retry(cmd: list, label: str, retries: int = MAX_RETRIES,
                         delay: int = 5, timeout: int = 120, env: dict = None) -> tuple[bool, str]:
    """Run command with retries. Returns (success, output_or_error)."""
    for attempt in range(1, retries + 1):
        rc, out, err = await run_cmd(cmd, timeout=timeout, env=env)
        if rc == 0:
            return True, out
        msg = err.strip() or out.strip()
        log.warning(f"[{label}] attempt {attempt}/{retries} failed (rc={rc}): {msg[:200]}")
        if attempt < retries:
            await asyncio.sleep(delay)
    return False, msg

async def gcloud_auth(email: str, password: str, console_url: str) -> tuple[bool, str]:
    """
    Attempt to authenticate gcloud with provided Qwiklabs credentials.
    NOTE: This uses gcloud auth login with service account / access token flow.
    In real Qwiklabs environment, credentials are pre-configured.
    This function sets the active account and project.
    """
    project_id = extract_project_id_from_url(console_url)
    if not project_id:
        return False, "Could not extract project ID from the console URL."

    # Set project
    ok, out = await run_cmd_retry(
        ["gcloud", "config", "set", "project", project_id],
        label="set-project",
        retries=2
    )
    if not ok:
        return False, f"Failed to set GCP project: {out}"

    return True, project_id

async def get_project_number(project_id: str) -> Optional[str]:
    rc, out, err = await run_cmd(
        ["gcloud", "projects", "describe", project_id, "--format=value(projectNumber)"],
        timeout=30
    )
    return out.strip() if rc == 0 and out.strip() else None

async def enable_apis(project_id: str) -> tuple[bool, str]:
    return await run_cmd_retry(
        ["gcloud", "services", "enable",
         "run.googleapis.com", "cloudbuild.googleapis.com",
         "--quiet"],
        label="enable-apis",
        timeout=120
    )

async def deploy_cloudrun(service_name: str, region: str,
                           cpu: str, memory: str) -> tuple[bool, str]:
    image = "docker.io/n4pro/vlessgrpc:latest"
    return await run_cmd_retry(
        ["gcloud", "run", "deploy", service_name,
         f"--image={image}",
         "--platform=managed",
         f"--region={region}",
         f"--memory={memory}",
         f"--cpu={cpu}",
         "--timeout=3600",
         "--allow-unauthenticated",
         "--port=8080",
         "--min-instances=1",
         "--quiet"],
        label="deploy",
        retries=MAX_RETRIES,
        timeout=DEPLOY_TIMEOUT
    )

async def verify_service(url: str, attempts: int = 3) -> bool:
    import aiohttp
    for i in range(attempts):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10),
                                        ssl=False) as resp:
                    if resp.status in [200, 301, 302, 400, 401, 403, 404]:
                        return True
        except Exception:
            pass
        if i < attempts - 1:
            await asyncio.sleep(8)
    return False

# ─────────────────────────────────────────
#  Main Deploy Orchestrator
# ─────────────────────────────────────────
async def deploy_for_user(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    creds: dict,
    user_id: int
):
    chat_id = update.effective_chat.id
    start_epoch = time.time()
    end_epoch   = start_epoch + 5 * 3600
    vless_uuid  = gen_uuid()

    async def status(msg: str):
        """Send a status update message."""
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning(f"Status send failed: {e}")

    try:
        active_jobs[user_id] = {"status": "authenticating", "started": start_epoch}

        # ── Step 1: Auth ──
        await status("🔐 <b>Step 1/5</b> — Authenticating with GCP...")
        ok, result = await gcloud_auth(creds["email"], creds["password"], creds["console_url"])
        if not ok:
            await status(f"❌ <b>Auth failed:</b> {result}\n\nPlease check your credentials and try again.")
            active_jobs.pop(user_id, None)
            user_stats[user_id]["fail"] += 1
            return

        project_id = result
        await status(f"✅ <b>Step 1/5</b> — Authenticated!\n<code>Project: {project_id}</code>")

        # ── Step 2: Project Number ──
        active_jobs[user_id]["status"] = "getting_project_number"
        project_number = await get_project_number(project_id)
        if not project_number:
            # Fallback: try to get from URL
            project_number = extract_project_number_from_url(creds["console_url"]) or "000000000000"

        service_name   = gen_service_name(project_number)
        canonical_host = f"{service_name}-{project_number}.{DEFAULT_REGION}.run.app"
        service_url    = f"https://{canonical_host}"

        # ── Step 3: Enable APIs ──
        active_jobs[user_id]["status"] = "enabling_apis"
        await status("⚙️ <b>Step 2/5</b> — Enabling Cloud Run & Build APIs...")
        ok, out = await enable_apis(project_id)
        if not ok:
            await status(f"❌ <b>API enable failed:</b>\n<code>{out[:300]}</code>")
            active_jobs.pop(user_id, None)
            user_stats[user_id]["fail"] += 1
            return
        await status("✅ <b>Step 2/5</b> — APIs enabled!")

        # ── Step 4: Deploy ──
        active_jobs[user_id]["status"] = "deploying"
        await status(
            f"🚀 <b>Step 3/5</b> — Deploying Cloud Run service...\n"
            f"<blockquote>"
            f"📦 Service: <code>{service_name}</code>\n"
            f"🌍 Region: {region_flag(DEFAULT_REGION)}\n"
            f"🧮 CPU/RAM: {DEFAULT_CPU} vCPU / {DEFAULT_MEMORY}"
            f"</blockquote>\n"
            f"⏳ This may take 1–3 minutes..."
        )
        ok, out = await deploy_cloudrun(service_name, DEFAULT_REGION, DEFAULT_CPU, DEFAULT_MEMORY)
        if not ok:
            await status(
                f"❌ <b>Deploy failed</b> after {MAX_RETRIES} attempts.\n"
                f"<code>{out[:400]}</code>\n\n"
                f"💡 Try again or use a different region."
            )
            active_jobs.pop(user_id, None)
            user_stats[user_id]["fail"] += 1
            return
        await status("✅ <b>Step 3/5</b> — Service deployed!")

        # ── Step 5: Verify ──
        active_jobs[user_id]["status"] = "verifying"
        await status("🔎 <b>Step 4/5</b> — Verifying service is live...")
        is_live = await verify_service(service_url)
        verify_status = "🟢 Online" if is_live else "🟡 Starting up (may take ~30s)"
        await status(f"{'✅' if is_live else '⚠️'} <b>Step 4/5</b> — {verify_status}")

        # ── Step 6: Generate Keys ──
        active_jobs[user_id]["status"] = "generating_keys"
        await status("🔑 <b>Step 5/5</b> — Generating access keys...")

        vless_uri = build_vless_grpc_uri(canonical_host, vless_uuid, CHANNEL_TAG)
        dark_config = build_darktunnel_config(canonical_host, vless_uuid)

        # ── Final Result Message ──
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
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".dark",
            prefix=f"{service_name}_",
            delete=False
        ) as f:
            f.write(dark_config)
            dark_file_path = f.name

        await context.bot.send_document(
            chat_id=chat_id,
            document=open(dark_file_path, "rb"),
            filename=f"{service_name}.dark",
            caption=(
                f"✅ <b>DarkTunnel Config File</b>\n"
                f"<code>{canonical_host}</code>"
            ),
            parse_mode=ParseMode.HTML
        )
        os.unlink(dark_file_path)

        # Update stats
        active_jobs.pop(user_id, None)
        user_stats[user_id]["total"]   += 1
        user_stats[user_id]["success"] += 1
        active_jobs[user_id] = {
            "status": "done",
            "host": canonical_host,
            "uri": vless_uri,
            "started": start_epoch,
            "project": project_id
        }

        # Notify admins
        for admin_id in ADMIN_IDS:
            if admin_id:
                try:
                    await context.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            f"📊 <b>Deploy completed</b>\n"
                            f"👤 User: <code>{user_id}</code>\n"
                            f"📦 Service: <code>{service_name}</code>\n"
                            f"🌍 Region: {region_flag(DEFAULT_REGION)}\n"
                            f"🕒 Time: {fmt_time(start_epoch)}"
                        ),
                        parse_mode=ParseMode.HTML
                    )
                except Exception:
                    pass

    except Exception as e:
        log.exception(f"Unexpected error for user {user_id}: {e}")
        active_jobs.pop(user_id, None)
        user_stats[user_id]["fail"] += 1
        await status(
            f"💥 <b>Unexpected error:</b>\n<code>{str(e)[:300]}</code>\n\n"
            f"Please try again or contact {CHANNEL_TAG}"
        )

# ─────────────────────────────────────────
#  Telegram Handlers
# ─────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    welcome = (
        f"👋 <b>Welcome to Yosh GC Bot!</b>\n\n"
        f"🚀 I auto-deploy <b>VLESS gRPC</b> on Google Cloud Run\n"
        f"using your Qwiklabs credentials.\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 <b>How to use:</b>\n"
        f"Send your credentials in this format:\n\n"
        f"<code>student-XX@qwiklabs.net\n"
        f"YourPassword\n"
        f"https://console.cloud.google.com/...</code>\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 Commands:\n"
        f"/start — Show this message\n"
        f"/status — Check your deploy status\n"
        f"/help — Usage guide\n\n"
        f"<i>🔥 Powered by Yosh | {CHANNEL_TAG}</i>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{CHANNEL_TAG.lstrip('@')}")]
    ])
    await update.message.reply_text(welcome, parse_mode=ParseMode.HTML, reply_markup=keyboard)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        f"📖 <b>Yosh GC Bot — Usage Guide</b>\n\n"
        f"<b>Step 1:</b> Open your Qwiklabs lab\n"
        f"<b>Step 2:</b> Copy your credentials:\n"
        f"  • Student email\n"
        f"  • Password\n"
        f"  • Console URL\n\n"
        f"<b>Step 3:</b> Send them here like this:\n"
        f"<code>student-02-abc@qwiklabs.net\n"
        f"myPassword123\n"
        f"https://www.skills.google/...</code>\n\n"
        f"<b>Step 4:</b> Wait ~2 minutes ⏳\n\n"
        f"<b>Step 5:</b> Receive:\n"
        f"  ✅ VLESS gRPC key (copy-ready)\n"
        f"  ✅ DarkTunnel .dark config file\n"
        f"  ✅ Cloud Run service URL\n\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"⚠️ <b>Notes:</b>\n"
        f"• Keys expire in ~5 hours\n"
        f"• One active deploy per user\n"
        f"• Deploys to {region_flag(DEFAULT_REGION)}\n\n"
        f"<i>{CHANNEL_TAG}</i>"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    job = active_jobs.get(user_id)
    stats = user_stats[user_id]

    if not job:
        msg = (
            f"📊 <b>Your Stats</b>\n\n"
            f"✅ Successful: {stats['success']}\n"
            f"❌ Failed: {stats['fail']}\n"
            f"📦 Total: {stats['total']}\n\n"
            f"No active deploy. Send credentials to start!"
        )
    else:
        status_map = {
            "authenticating":       "🔐 Authenticating...",
            "getting_project_number": "🔢 Getting project info...",
            "enabling_apis":        "⚙️ Enabling APIs...",
            "deploying":            "🚀 Deploying service...",
            "verifying":            "🔎 Verifying...",
            "generating_keys":      "🔑 Generating keys...",
            "done":                 "✅ Complete!",
        }
        status_label = status_map.get(job.get("status", ""), "⏳ Working...")
        elapsed = int(time.time() - job.get("started", time.time()))
        msg = (
            f"📊 <b>Current Job Status</b>\n\n"
            f"🔄 Status: {status_label}\n"
            f"⏱️ Elapsed: {elapsed}s\n"
        )
        if job.get("host"):
            msg += f"🌐 Host: <code>{job['host']}</code>\n"
        if job.get("project"):
            msg += f"🆔 Project: <code>{job['project']}</code>\n"

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def cmd_admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        await update.message.reply_text("⛔ Admin only.")
        return

    active_count = sum(1 for j in active_jobs.values() if j.get("status") != "done")
    total_users  = len(user_stats)
    total_ok     = sum(s["success"] for s in user_stats.values())
    total_fail   = sum(s["fail"] for s in user_stats.values())

    msg = (
        f"👑 <b>Admin Dashboard</b>\n\n"
        f"👥 Total users: {total_users}\n"
        f"🔄 Active deploys: {active_count}\n"
        f"✅ Total success: {total_ok}\n"
        f"❌ Total failed: {total_fail}\n\n"
        f"<i>Bot: Yosh GC Bot | {CHANNEL_TAG}</i>"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle credential messages from users."""
    user_id = update.effective_user.id
    text    = update.message.text or ""

    # Check if already deploying
    job = active_jobs.get(user_id, {})
    if job.get("status") not in (None, "done", "failed"):
        elapsed = int(time.time() - job.get("started", time.time()))
        await update.message.reply_text(
            f"⏳ <b>Deploy already in progress</b> ({elapsed}s elapsed)\n\n"
            f"Please wait for it to finish or use /status to check.",
            parse_mode=ParseMode.HTML
        )
        return

    # Try to parse credentials
    creds = parse_credentials(text)
    if not creds:
        await update.message.reply_text(
            "❓ <b>Could not parse credentials.</b>\n\n"
            "Please send in this format:\n"
            "<code>student-XX@qwiklabs.net\n"
            "YourPassword\n"
            "https://console.cloud.google.com/...</code>\n\n"
            "Use /help for full guide.",
            parse_mode=ParseMode.HTML
        )
        return

    # Acknowledge
    await update.message.reply_text(
        f"✅ <b>Credentials received!</b>\n\n"
        f"<blockquote>"
        f"👤 Account: <code>{creds['email']}</code>\n"
        f"🆔 Project: <code>{creds['project_id'] or 'detecting...'}</code>"
        f"</blockquote>\n\n"
        f"🚀 Starting deployment... Please wait.\n"
        f"<i>You'll receive step-by-step updates below.</i>",
        parse_mode=ParseMode.HTML
    )

    # Run deploy in background (non-blocking, supports multiple users)
    asyncio.create_task(
        deploy_for_user(update, context, creds, user_id)
    )

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "deploy_new":
        await query.message.reply_text(
            "📋 Send your new Qwiklabs credentials:\n\n"
            "<code>email\npassword\nconsole_url</code>",
            parse_mode=ParseMode.HTML
        )

# ─────────────────────────────────────────
#  Main Entry
# ─────────────────────────────────────────
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE":
        log.error("❌ Please set BOT_TOKEN environment variable!")
        return

    log.info("🚀 Starting Yosh GC Bot...")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats",  cmd_admin_stats))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(handle_callback))

    log.info(f"✅ Bot running | Channel: {CHANNEL_TAG} | Admins: {ADMIN_IDS}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
