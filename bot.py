"""
Viking Cinema - Telegram Movie Info Bot
----------------------------------------
Search for movies and get posters, synopsis, cast, rating, trailers, and
legal "where to watch" links, using data from The Movie Database (TMDB).

Features:
  - Force-subscribe: channel membership is re-checked on every /start and
    every gated action, not just once
  - Button-driven main menu (reply keyboard) instead of relying on slash commands
  - Language picker (inline flag menu, stored per user)
  - Mood-based movie recommendations
  - Trending / popular movies shortcut
  - Personal watchlist (add/remove movies)
  - Referral bonus (invite a friend, both of you get bonus free searches)
  - Free tier: 2 actions/day. Premium: unlimited. Owner: always unlimited.
  - Real subscriptions paid with Telegram Stars (weekly / monthly / yearly),
    auto-activated on successful payment - no manual screenshot approval needed
  - "Where to Watch" button linking to legal streaming providers (TMDB data),
    instead of any in-bot download

NOTE ON SCOPE: this bot deliberately does NOT let users pick a video quality
and download a movie file. TMDB only provides metadata (posters, synopsis,
cast, trailers, legal-provider links) - never actual video files - so a
"pick HD / estimate size / download" flow can only be built on top of pirated
video sources, which isn't something this script does.

SETUP:
1. Paste your Telegram Bot Token below (from @BotFather).
2. Paste your TMDB API Key below (from themoviedb.org -> Settings -> API).
3. Fill in CHANNEL_USERNAME, ADMIN_USER_IDS, OWNER_USERNAME, SUPPORT_* and
   star prices below.
4. Run:  python3 bot.py

Requirements (already installed):
    pip3 install python-telegram-bot requests
"""

import json
import os
import requests
from datetime import date, timedelta
from functools import wraps

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    LabeledPrice,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

# ============================================================
# 1) PUT YOUR KEYS / CONFIG HERE
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

# The channel users must join before they can use the bot.
CHANNEL_USERNAME = "@vikingcinema"
CHANNEL_INVITE_LINK = "https://t.me/vikingcinema"

# Telegram user IDs allowed to use admin commands (get your ID from @userinfobot)
ADMIN_USER_IDS = {123456789}                              # <-- TODO: set this

# The bot owner's @username (no leading "@" here) always gets unlimited
# access, regardless of premium status or daily quota. Telegram usernames
# are case-insensitive, so comparisons below are done in lowercase.
OWNER_USERNAME = "VikingFounder"                           # <-- your handle

# ---- Support / contact info ----
SUPPORT_USERNAME = "@your_support_handle"                 # <-- TODO
SUPPORT_EMAIL = "support@example.com"                     # <-- TODO
SUPPORT_HOURS = "Mon-Sat, 9am-6pm (WAT)"                   # <-- TODO

# ---- Subscription plans, paid in Telegram Stars (currency code "XTR") ----
# Telegram Stars have their own exchange rate (set by Telegram, not you) - check
# the current rate in BotFather/Telegram docs and adjust these amounts so they
# land close to your target USD prices.
PLAN_INFO = {
    "weekly":  {"days": 7,   "stars": 150,  "label": "Weekly"},    # ~ a few dollars
    "monthly": {"days": 30,  "stars": 500,  "label": "Monthly"},   # target ~$5
    "yearly":  {"days": 365, "stars": 2999, "label": "Yearly"},
}

FREE_DAILY_LIMIT = 2   # free actions (searches / recommendations) per day
REFERRAL_BONUS = 2     # bonus free actions granted to referrer + new user

DEFAULT_WATCH_REGION = "US"   # region code used for "Where to Watch" lookups

USERS_FILE = "users.json"

# ============================================================
# Language options
# ============================================================
LANGUAGES = {
    "en": ("English", "🇬🇧"),
    "es": ("Español", "🇪🇸"),
    "fr": ("Français", "🇫🇷"),
    "de": ("Deutsch", "🇩🇪"),
    "pt": ("Português", "🇵🇹"),
    "ar": ("العربية", "🇸🇦"),
    "hi": ("हिन्दी", "🇮🇳"),
}

TRANSLATIONS = {
    "en": {
        "welcome": "🎬 *Welcome to Viking Cinema!*\n\nUse the menu below, or just type a movie name to search.",
        "choose_language": "🌐 Please choose your preferred language:",
        "join_required": "🔒 To use this bot, please join our channel first, then tap \"I've joined\".",
        "not_joined_yet": "⚠️ We couldn't verify your membership yet. Please join the channel and try again.",
        "searching": "🔎 Searching for \"{query}\"...",
        "no_results": "No movies found for \"{query}\". Try a different spelling.",
        "found_results": "Found {count} result(s) for \"{query}\". Tap one to see details:",
        "limit_reached": "🚫 You've used your {limit} free actions for today.\nUpgrade to Premium for unlimited access — tap ⭐ Premium below.",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    text = TRANSLATIONS.get(lang, {}).get(key) or TRANSLATIONS["en"].get(key, key)
    return text.format(**kwargs) if kwargs else text


# ============================================================
# Simple JSON-backed user store
# ============================================================

def _load_users() -> dict:
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_users(users: dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)


_users_cache = _load_users()


def get_user(user_id: int) -> dict:
    key = str(user_id)
    if key not in _users_cache:
        _users_cache[key] = {
            "language": "en",
            "premium": False,
            "premium_until": None,     # ISO date string
            "actions_today": 0,
            "last_action_date": None,
            "bonus_actions": 0,
            "watchlist": [],           # list of {"id": int, "title": str}
            "referred_by": None,
        }
        _save_users(_users_cache)
    return _users_cache[key]


def update_user(user_id: int, **fields):
    user = get_user(user_id)
    user.update(fields)
    _save_users(_users_cache)


def is_owner(username: str | None) -> bool:
    """True if this Telegram @username belongs to the bot owner."""
    if not username or not OWNER_USERNAME:
        return False
    return username.lstrip("@").lower() == OWNER_USERNAME.lstrip("@").lower()


def is_premium(user_id: int) -> bool:
    user = get_user(user_id)
    if not user.get("premium"):
        return False
    until = user.get("premium_until")
    if until and date.fromisoformat(until) < date.today():
        user["premium"] = False
        user["premium_until"] = None
        _save_users(_users_cache)
        return False
    return True


def days_left_of_premium(user_id: int) -> int:
    user = get_user(user_id)
    until = user.get("premium_until")
    if not until:
        return 0
    return max(0, (date.fromisoformat(until) - date.today()).days)


def activate_premium(user_id: int, plan_key: str):
    user = get_user(user_id)
    days = PLAN_INFO[plan_key]["days"]
    base = date.today()
    current_until = user.get("premium_until")
    if current_until and date.fromisoformat(current_until) > base:
        base = date.fromisoformat(current_until)
    new_until = base + timedelta(days=days)
    user["premium"] = True
    user["premium_until"] = new_until.isoformat()
    _save_users(_users_cache)


def check_and_use_action_quota(user_id: int, username: str | None = None) -> bool:
    """Returns True if allowed to do a search/recommend action now (and records it).

    The bot owner (matched by @username, see OWNER_USERNAME above) and any
    admin in ADMIN_USER_IDS always pass, same as premium users - none of
    them are ever subject to the daily free-action limit.
    """
    if is_premium(user_id) or is_owner(username) or user_id in ADMIN_USER_IDS:
        return True

    user = get_user(user_id)
    today = date.today().isoformat()
    if user.get("last_action_date") != today:
        user["actions_today"] = 0
        user["last_action_date"] = today

    limit = FREE_DAILY_LIMIT + user.get("bonus_actions", 0)
    if user["actions_today"] >= limit:
        _save_users(_users_cache)
        return False

    user["actions_today"] += 1
    _save_users(_users_cache)
    return True


# ============================================================
# Channel membership gate
# ============================================================

async def is_channel_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status not in ("left", "kicked")
    except TelegramError:
        return False


def join_gate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_INVITE_LINK)],
            [InlineKeyboardButton("✅ I've joined", callback_data="check_membership")],
        ]
    )


def require_membership(handler):
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if await is_channel_member(context, user_id):
            return await handler(update, context, *args, **kwargs)
        lang = get_user(user_id).get("language", "en")
        message = update.effective_message
        await message.reply_text(t(lang, "join_required"), reply_markup=join_gate_keyboard())
    return wrapped


# ============================================================
# Main menu (persistent reply keyboard)
# ============================================================

MENU_SEARCH = "🔍 Search Movie"
MENU_MOOD = "🎭 Mood Recommend"
MENU_TRENDING = "📺 Trending"
MENU_WATCHLIST = "🎬 My Watchlist"
MENU_LANGUAGE = "🌐 Language"
MENU_PREMIUM = "⭐ Premium"
MENU_INVITE = "🎁 Invite Friends"
MENU_SUPPORT = "🛟 Support"


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [MENU_SEARCH, MENU_MOOD],
            [MENU_TRENDING, MENU_WATCHLIST],
            [MENU_LANGUAGE, MENU_PREMIUM],
            [MENU_INVITE, MENU_SUPPORT],
        ],
        resize_keyboard=True,
    )


# ============================================================
# TMDB API helpers
# ============================================================
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

MOOD_GENRES = {
    "happy": [35, 10751],
    "sad": [18],
    "excited": [28, 12],
    "scared": [27, 53],
    "romantic": [10749],
    "thoughtful": [18, 99],
    "relaxed": [16, 10751],
    "curious": [9648, 878],
}


def tmdb_search_movies(query: str, language: str = "en"):
    url = f"{TMDB_BASE_URL}/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": query, "include_adult": False, "language": language}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json().get("results", [])


def tmdb_discover_by_genres(genre_ids: list, language: str = "en"):
    url = f"{TMDB_BASE_URL}/discover/movie"
    params = {
        "api_key": TMDB_API_KEY,
        "with_genres": ",".join(str(g) for g in genre_ids),
        "sort_by": "popularity.desc",
        "include_adult": False,
        "language": language,
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json().get("results", [])


def tmdb_trending_movies(language: str = "en"):
    url = f"{TMDB_BASE_URL}/trending/movie/day"
    params = {"api_key": TMDB_API_KEY, "language": language}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json().get("results", [])


def tmdb_get_movie_details(movie_id: int, language: str = "en"):
    url = f"{TMDB_BASE_URL}/movie/{movie_id}"
    params = {"api_key": TMDB_API_KEY, "append_to_response": "credits,videos", "language": language}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def tmdb_get_watch_link(movie_id: int, region: str = DEFAULT_WATCH_REGION):
    """Returns (provider_names_str, link) for legal streaming options, or (None, None)."""
    url = f"{TMDB_BASE_URL}/movie/{movie_id}/watch/providers"
    params = {"api_key": TMDB_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json().get("results", {}).get(region)
        if not data:
            return None, None
        providers = data.get("flatrate") or data.get("rent") or data.get("buy") or []
        names = ", ".join(p["provider_name"] for p in providers[:4])
        return (names or None), data.get("link")
    except requests.RequestException:
        return None, None


def get_trailer_url(movie_details: dict):
    videos = movie_details.get("videos", {}).get("results", [])
    for video in videos:
        if video.get("site") == "YouTube" and video.get("type") == "Trailer":
            return f"https://www.youtube.com/watch?v={video['key']}"
    return None


def get_top_cast(movie_details: dict, limit: int = 5):
    cast = movie_details.get("credits", {}).get("cast", [])
    names = [m["name"] for m in cast[:limit]]
    return ", ".join(names) if names else "Not available"


def results_keyboard(results: list) -> InlineKeyboardMarkup:
    buttons = []
    for movie in results[:8]:
        title = movie.get("title", "Untitled")
        year = (movie.get("release_date") or "")[:4]
        label = f"{title} ({year})" if year else title
        buttons.append([InlineKeyboardButton(label, callback_data=f"movie:{movie['id']}")])
    return InlineKeyboardMarkup(buttons)


# ============================================================
# Command handlers
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    get_user(user_id)  # ensure record exists

    # Handle referral deep-link: /start ref_<referrer_id>
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer_id = int(context.args[0][4:])
        except ValueError:
            referrer_id = None
        user = get_user(user_id)
        if referrer_id and referrer_id != user_id and user.get("referred_by") is None:
            user["referred_by"] = referrer_id
            user["bonus_actions"] = user.get("bonus_actions", 0) + REFERRAL_BONUS
            referrer = get_user(referrer_id)
            referrer["bonus_actions"] = referrer.get("bonus_actions", 0) + REFERRAL_BONUS
            _save_users(_users_cache)

    # Membership is re-checked every single /start, on purpose.
    if not await is_channel_member(context, user_id):
        lang = get_user(user_id).get("language", "en")
        await update.message.reply_text(t(lang, "join_required"), reply_markup=join_gate_keyboard())
        return

    lang = get_user(user_id).get("language", "en")
    await update.message.reply_text(t(lang, "welcome"), parse_mode="Markdown", reply_markup=main_menu_keyboard())


async def membership_recheck_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    lang = get_user(user_id).get("language", "en")
    if await is_channel_member(context, user_id):
        await query.answer("✅ Verified!")
        await query.edit_message_text(t(lang, "welcome"), parse_mode="Markdown")
        await query.message.reply_text("Menu ready 👇", reply_markup=main_menu_keyboard())
    else:
        await query.answer(t(lang, "not_joined_yet"), show_alert=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Use the menu buttons below, or just type a movie name to search.\n\n"
        "/premium - view subscription plans\n"
        "/invite - get your referral link\n"
        "/support - contact support",
        reply_markup=main_menu_keyboard(),
    )


# ---- Language ----

def language_keyboard() -> InlineKeyboardMarkup:
    rows, row = [], []
    for code, (label, flag) in LANGUAGES.items():
        row.append(InlineKeyboardButton(f"{label} {flag}", callback_data=f"lang:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def language_set_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.split(":")[1]
    if code not in LANGUAGES:
        return
    update_user(query.from_user.id, language=code)
    label, flag = LANGUAGES[code]
    await query.edit_message_text(f"Language set to {label} {flag}")


# ---- Search / details ----

async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    user_id = update.effective_user.id
    username = update.effective_user.username
    lang = get_user(user_id).get("language", "en")

    if not check_and_use_action_quota(user_id, username):
        await update.message.reply_text(t(lang, "limit_reached", limit=FREE_DAILY_LIMIT))
        return

    searching_msg = await update.message.reply_text(t(lang, "searching", query=query))
    try:
        results = tmdb_search_movies(query, language=lang)
    except requests.RequestException:
        await searching_msg.edit_text("⚠️ Couldn't reach the movie database right now. Please try again in a moment.")
        return

    if not results:
        await searching_msg.edit_text(t(lang, "no_results", query=query))
        return

    await searching_msg.edit_text(
        t(lang, "found_results", count=min(len(results), 8), query=query),
        reply_markup=results_keyboard(results),
    )


async def movie_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    lang = get_user(user_id).get("language", "en")
    movie_id = int(query.data.split(":")[1])

    try:
        details = tmdb_get_movie_details(movie_id, language=lang)
    except requests.RequestException:
        await query.message.reply_text("⚠️ Couldn't load movie details. Please try again.")
        return

    title = details.get("title", "Untitled")
    year = (details.get("release_date") or "Unknown")[:4]
    overview = details.get("overview") or "No synopsis available."
    rating = details.get("vote_average", 0)
    genres = ", ".join(g["name"] for g in details.get("genres", [])) or "Unknown"
    runtime = details.get("runtime")
    runtime_text = f"{runtime} min" if runtime else "Unknown"
    cast_text = get_top_cast(details)
    trailer_url = get_trailer_url(details)
    poster_path = details.get("poster_path")
    provider_names, watch_link = tmdb_get_watch_link(movie_id)

    caption = (
        f"🎬 *{title}* ({year})\n\n"
        f"⭐ Rating: {rating:.1f}/10\n"
        f"🎭 Genres: {genres}\n"
        f"⏱ Runtime: {runtime_text}\n"
        f"👥 Cast: {cast_text}\n\n"
        f"📝 {overview}"
    )
    if provider_names:
        caption += f"\n\n📺 Available on: {provider_names}"

    buttons = []
    row = []
    if trailer_url:
        row.append(InlineKeyboardButton("▶️ Trailer", url=trailer_url))
    if watch_link:
        row.append(InlineKeyboardButton("📺 Where to Watch", url=watch_link))
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("➕ Add to Watchlist", callback_data=f"watchlist_add:{movie_id}")])
    keyboard = InlineKeyboardMarkup(buttons)

    if poster_path:
        await query.message.reply_photo(
            photo=f"{TMDB_IMAGE_BASE}{poster_path}", caption=caption, parse_mode="Markdown", reply_markup=keyboard
        )
    else:
        await query.message.reply_text(caption, parse_mode="Markdown", reply_markup=keyboard)


# ---- Mood recommendations ----

def mood_keyboard() -> InlineKeyboardMarkup:
    rows, row = [], []
    for mood in MOOD_GENRES:
        row.append(InlineKeyboardButton(mood.capitalize(), callback_data=f"mood:{mood}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def mood_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mood = query.data.split(":")[1]
    await send_mood_recommendations(query.message, query.from_user.id, mood, username=query.from_user.username, edit=True)


async def send_mood_recommendations(message, user_id: int, mood: str, username: str | None = None, edit: bool = False):
    lang = get_user(user_id).get("language", "en")
    if not check_and_use_action_quota(user_id, username):
        text = t(lang, "limit_reached", limit=FREE_DAILY_LIMIT)
        await (message.edit_text(text) if edit else message.reply_text(text))
        return

    try:
        results = tmdb_discover_by_genres(MOOD_GENRES[mood], language=lang)
    except requests.RequestException:
        await message.reply_text("⚠️ Couldn't reach the movie database right now. Please try again.")
        return

    if not results:
        await message.reply_text(f"No recommendations found for \"{mood}\" right now.")
        return

    text = f"Here's what fits a *{mood}* mood:"
    markup = results_keyboard(results)
    if edit:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


# ---- Trending ----

async def send_trending(message, user_id: int):
    lang = get_user(user_id).get("language", "en")
    try:
        results = tmdb_trending_movies(language=lang)
    except requests.RequestException:
        await message.reply_text("⚠️ Couldn't reach the movie database right now. Please try again.")
        return
    if not results:
        await message.reply_text("No trending movies found right now.")
        return
    await message.reply_text("🔥 *Trending today:*", parse_mode="Markdown", reply_markup=results_keyboard(results))


# ---- Watchlist ----

async def watchlist_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    movie_id = int(query.data.split(":")[1])
    lang = get_user(user_id).get("language", "en")

    try:
        details = tmdb_get_movie_details(movie_id, language=lang)
    except requests.RequestException:
        await query.answer("Couldn't add right now, try again.", show_alert=True)
        return

    title = details.get("title", "Untitled")
    user = get_user(user_id)
    if not any(m["id"] == movie_id for m in user["watchlist"]):
        user["watchlist"].append({"id": movie_id, "title": title})
        _save_users(_users_cache)
        await query.answer(f"Added \"{title}\" to your watchlist ✅")
    else:
        await query.answer(f"\"{title}\" is already in your watchlist.")


async def send_watchlist(message, user_id: int):
    user = get_user(user_id)
    watchlist = user.get("watchlist", [])
    if not watchlist:
        await message.reply_text("Your watchlist is empty. Add movies with the ➕ button on any movie card.")
        return
    buttons = [
        [
            InlineKeyboardButton(item["title"], callback_data=f"movie:{item['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"watchlist_remove:{item['id']}"),
        ]
        for item in watchlist[:20]
    ]
    await message.reply_text("🎬 *Your Watchlist:*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


async def watchlist_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    movie_id = int(query.data.split(":")[1])
    user = get_user(user_id)
    user["watchlist"] = [m for m in user["watchlist"] if m["id"] != movie_id]
    _save_users(_users_cache)
    await send_watchlist(query.message, user_id)


# ---- Referral ----

async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    await update.message.reply_text(
        "🎁 *Invite friends, get bonus searches!*\n\n"
        f"Share your link:\n{link}\n\n"
        f"You and your friend each get +{REFERRAL_BONUS} bonus free actions when they join.",
        parse_mode="Markdown",
    )


# ---- Premium / Stars payments ----

def premium_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"⚡ Weekly - {PLAN_INFO['weekly']['stars']}⭐", callback_data="buy:weekly")],
            [InlineKeyboardButton(f"🔥 Monthly - {PLAN_INFO['monthly']['stars']}⭐", callback_data="buy:monthly")],
            [InlineKeyboardButton(f"🌟 Yearly - {PLAN_INFO['yearly']['stars']}⭐", callback_data="buy:yearly")],
        ]
    )


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username

    if is_owner(username):
        await update.message.reply_text("👑 You're the bot owner — you already have unlimited access, no plan needed.")
        return

    if is_premium(user_id):
        days = days_left_of_premium(user_id)
        await update.message.reply_text(f"✅ You already have Premium — {days} day(s) left.")
        return

    text = (
        "🌟 *Cinema Premium*\n\n"
        f"• Unlimited searches & recommendations (free tier: {FREE_DAILY_LIMIT}/day)\n"
        "• Priority support\n\n"
        "Pay securely with Telegram Stars — no card needed:"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=premium_keyboard())


async def buy_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    plan_key = query.data.split(":")[1]
    plan = PLAN_INFO[plan_key]
    user_id = query.from_user.id

    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=f"Viking Cinema Premium - {plan['label']}",
        description=f"Unlimited searches & recommendations for {plan['days']} days.",
        payload=f"premium:{plan_key}:{user_id}",
        currency="XTR",
        prices=[LabeledPrice(label=plan["label"], amount=plan["stars"])],
        provider_token="",  # empty string is required for Telegram Stars payments
    )


async def pre_checkout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    # Extra validation could go here (e.g. re-check payload format) before approving.
    await query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    try:
        _, plan_key, user_id_str = payment.invoice_payload.split(":")
        user_id = int(user_id_str)
    except (ValueError, KeyError):
        await update.message.reply_text("⚠️ Payment received but couldn't be matched to a plan. Contact support.")
        return

    activate_premium(user_id, plan_key)
    days_left = days_left_of_premium(user_id)
    await update.message.reply_text(
        f"✅ Payment received! Premium is active — {days_left} day(s) remaining. Enjoy unlimited access!"
    )


async def grant_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only manual override: /grant <user_id> <weekly|monthly|yearly>"""
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    if len(context.args) < 2 or context.args[1] not in PLAN_INFO:
        await update.message.reply_text("Usage: /grant <telegram_user_id> <weekly|monthly|yearly>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a valid user ID.")
        return
    activate_premium(target_id, context.args[1])
    await update.message.reply_text(f"✅ Premium ({context.args[1]}) granted to user {target_id}.")


async def revoke_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /revoke <telegram_user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a valid user ID.")
        return
    update_user(target_id, premium=False, premium_until=None)
    await update.message.reply_text(f"Premium revoked for user {target_id}.")


# ---- Support ----

async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛟 *Need help?*\n\n"
        f"• Telegram: {SUPPORT_USERNAME}\n"
        f"• Email: {SUPPORT_EMAIL}\n"
        f"• Hours: {SUPPORT_HOURS}\n\n"
        "Include your Telegram username and a short description of the issue."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ============================================================
# Text message router (menu buttons + free-typed searches)
# ============================================================

@require_membership
async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    lang = get_user(user_id).get("language", "en")

    routes = {
        MENU_MOOD: lambda: update.message.reply_text("How are you feeling? Pick a mood:", reply_markup=mood_keyboard()),
        MENU_TRENDING: lambda: send_trending(update.message, user_id),
        MENU_WATCHLIST: lambda: send_watchlist(update.message, user_id),
        MENU_LANGUAGE: lambda: update.message.reply_text(t(lang, "choose_language"), reply_markup=language_keyboard()),
        MENU_PREMIUM: lambda: premium_command(update, context),
        MENU_INVITE: lambda: invite_command(update, context),
        MENU_SUPPORT: lambda: support_command(update, context),
    }

    if text == MENU_SEARCH:
        await update.message.reply_text("Type the movie name you want to search:")
        return

    if text in routes:
        await routes[text]()
        return

    # Anything else typed is treated as a movie search query.
    await run_search(update, context, text)


@require_membership
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /search <movie name>")
        return
    await run_search(update, context, " ".join(context.args))


@require_membership
async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0].lower() in MOOD_GENRES:
        await send_mood_recommendations(
            update.message,
            update.effective_user.id,
            context.args[0].lower(),
            username=update.effective_user.username,
        )
        return
    await update.message.reply_text("How are you feeling? Pick a mood:", reply_markup=mood_keyboard())


# ============================================================
# Main entry point
# ============================================================

def main():
    if not TELEGRAM_BOT_TOKEN or not TMDB_API_KEY:
        print("ERROR: Set the TELEGRAM_BOT_TOKEN and TMDB_API_KEY environment variables before running.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("find", find_command))
    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler("invite", invite_command))
    app.add_handler(CommandHandler("support", support_command))
    app.add_handler(CommandHandler("grant", grant_premium_command))
    app.add_handler(CommandHandler("revoke", revoke_premium_command))

    app.add_handler(CallbackQueryHandler(membership_recheck_callback, pattern=r"^check_membership$"))
    app.add_handler(CallbackQueryHandler(language_set_callback, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(mood_pick_callback, pattern=r"^mood:"))
    app.add_handler(CallbackQueryHandler(movie_details_callback, pattern=r"^movie:"))
    app.add_handler(CallbackQueryHandler(watchlist_add_callback, pattern=r"^watchlist_add:"))
    app.add_handler(CallbackQueryHandler(watchlist_remove_callback, pattern=r"^watchlist_remove:"))
    app.add_handler(CallbackQueryHandler(buy_plan_callback, pattern=r"^buy:"))

    app.add_handler(PreCheckoutQueryHandler(pre_checkout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    print("Viking Cinema bot is running... Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
import json
import os
import requests
from datetime import date, timedelta
from functools import wraps

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    LabeledPrice,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    PreCheckoutQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import TelegramError

# ============================================================
# 1) PUT YOUR KEYS / CONFIG HERE
# ============================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")

# The channel users must join before they can use the bot.
CHANNEL_USERNAME = "@vikingcinema"
CHANNEL_INVITE_LINK = "https://t.me/vikingcinema"

# Telegram user IDs allowed to use admin commands (get your ID from @userinfobot)
ADMIN_USER_IDS = {123456789}                              # <-- TODO: set this

# ---- Support / contact info ----
SUPPORT_USERNAME = "@your_support_handle"                 # <-- TODO
SUPPORT_EMAIL = "support@example.com"                     # <-- TODO
SUPPORT_HOURS = "Mon-Sat, 9am-6pm (WAT)"                   # <-- TODO

# ---- Subscription plans, paid in Telegram Stars (currency code "XTR") ----
# Telegram Stars have their own exchange rate (set by Telegram, not you) - check
# the current rate in BotFather/Telegram docs and adjust these amounts so they
# land close to your target USD prices.
PLAN_INFO = {
    "weekly":  {"days": 7,   "stars": 150,  "label": "Weekly"},    # ~ a few dollars
    "monthly": {"days": 30,  "stars": 500,  "label": "Monthly"},   # target ~$5
    "yearly":  {"days": 365, "stars": 2999, "label": "Yearly"},
}

FREE_DAILY_LIMIT = 2   # free actions (searches / recommendations) per day
REFERRAL_BONUS = 2     # bonus free actions granted to referrer + new user

DEFAULT_WATCH_REGION = "US"   # region code used for "Where to Watch" lookups

USERS_FILE = "users.json"

# ============================================================
# Language options
# ============================================================
LANGUAGES = {
    "en": ("English", "🇬🇧"),
    "es": ("Español", "🇪🇸"),
    "fr": ("Français", "🇫🇷"),
    "de": ("Deutsch", "🇩🇪"),
    "pt": ("Português", "🇵🇹"),
    "ar": ("العربية", "🇸🇦"),
    "hi": ("हिन्दी", "🇮🇳"),
}

TRANSLATIONS = {
    "en": {
        "welcome": "🎬 *Welcome to Viking Cinema!*\n\nUse the menu below, or just type a movie name to search.",
        "choose_language": "🌐 Please choose your preferred language:",
        "join_required": "🔒 To use this bot, please join our channel first, then tap \"I've joined\".",
        "not_joined_yet": "⚠️ We couldn't verify your membership yet. Please join the channel and try again.",
        "searching": "🔎 Searching for \"{query}\"...",
        "no_results": "No movies found for \"{query}\". Try a different spelling.",
        "found_results": "Found {count} result(s) for \"{query}\". Tap one to see details:",
        "limit_reached": "🚫 You've used your {limit} free actions for today.\nUpgrade to Premium for unlimited access — tap ⭐ Premium below.",
    },
}


def t(lang: str, key: str, **kwargs) -> str:
    text = TRANSLATIONS.get(lang, {}).get(key) or TRANSLATIONS["en"].get(key, key)
    return text.format(**kwargs) if kwargs else text


# ============================================================
# Simple JSON-backed user store
# ============================================================

def _load_users() -> dict:
    if os.path.exists(USERS_FILE):
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_users(users: dict):
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, indent=2)


_users_cache = _load_users()


def get_user(user_id: int) -> dict:
    key = str(user_id)
    if key not in _users_cache:
        _users_cache[key] = {
            "language": "en",
            "premium": False,
            "premium_until": None,     # ISO date string
            "actions_today": 0,
            "last_action_date": None,
            "bonus_actions": 0,
            "watchlist": [],           # list of {"id": int, "title": str}
            "referred_by": None,
        }
        _save_users(_users_cache)
    return _users_cache[key]


def update_user(user_id: int, **fields):
    user = get_user(user_id)
    user.update(fields)
    _save_users(_users_cache)


def is_premium(user_id: int) -> bool:
    user = get_user(user_id)
    if not user.get("premium"):
        return False
    until = user.get("premium_until")
    if until and date.fromisoformat(until) < date.today():
        user["premium"] = False
        user["premium_until"] = None
        _save_users(_users_cache)
        return False
    return True


def days_left_of_premium(user_id: int) -> int:
    user = get_user(user_id)
    until = user.get("premium_until")
    if not until:
        return 0
    return max(0, (date.fromisoformat(until) - date.today()).days)


def activate_premium(user_id: int, plan_key: str):
    user = get_user(user_id)
    days = PLAN_INFO[plan_key]["days"]
    base = date.today()
    current_until = user.get("premium_until")
    if current_until and date.fromisoformat(current_until) > base:
        base = date.fromisoformat(current_until)
    new_until = base + timedelta(days=days)
    user["premium"] = True
    user["premium_until"] = new_until.isoformat()
    _save_users(_users_cache)


def check_and_use_action_quota(user_id: int) -> bool:
    """Returns True if allowed to do a search/recommend action now (and records it)."""
    if is_premium(user_id):
        return True

    user = get_user(user_id)
    today = date.today().isoformat()
    if user.get("last_action_date") != today:
        user["actions_today"] = 0
        user["last_action_date"] = today

    limit = FREE_DAILY_LIMIT + user.get("bonus_actions", 0)
    if user["actions_today"] >= limit:
        _save_users(_users_cache)
        return False

    user["actions_today"] += 1
    _save_users(_users_cache)
    return True


# ============================================================
# Channel membership gate
# ============================================================

async def is_channel_member(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return member.status not in ("left", "kicked")
    except TelegramError:
        return False


def join_gate_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📢 Join Channel", url=CHANNEL_INVITE_LINK)],
            [InlineKeyboardButton("✅ I've joined", callback_data="check_membership")],
        ]
    )


def require_membership(handler):
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if await is_channel_member(context, user_id):
            return await handler(update, context, *args, **kwargs)
        lang = get_user(user_id).get("language", "en")
        message = update.effective_message
        await message.reply_text(t(lang, "join_required"), reply_markup=join_gate_keyboard())
    return wrapped


# ============================================================
# Main menu (persistent reply keyboard)
# ============================================================

MENU_SEARCH = "🔍 Search Movie"
MENU_MOOD = "🎭 Mood Recommend"
MENU_TRENDING = "📺 Trending"
MENU_WATCHLIST = "🎬 My Watchlist"
MENU_LANGUAGE = "🌐 Language"
MENU_PREMIUM = "⭐ Premium"
MENU_INVITE = "🎁 Invite Friends"
MENU_SUPPORT = "🛟 Support"


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [MENU_SEARCH, MENU_MOOD],
            [MENU_TRENDING, MENU_WATCHLIST],
            [MENU_LANGUAGE, MENU_PREMIUM],
            [MENU_INVITE, MENU_SUPPORT],
        ],
        resize_keyboard=True,
    )


# ============================================================
# TMDB API helpers
# ============================================================
TMDB_BASE_URL = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

MOOD_GENRES = {
    "happy": [35, 10751],
    "sad": [18],
    "excited": [28, 12],
    "scared": [27, 53],
    "romantic": [10749],
    "thoughtful": [18, 99],
    "relaxed": [16, 10751],
    "curious": [9648, 878],
}


def tmdb_search_movies(query: str, language: str = "en"):
    url = f"{TMDB_BASE_URL}/search/movie"
    params = {"api_key": TMDB_API_KEY, "query": query, "include_adult": False, "language": language}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json().get("results", [])


def tmdb_discover_by_genres(genre_ids: list, language: str = "en"):
    url = f"{TMDB_BASE_URL}/discover/movie"
    params = {
        "api_key": TMDB_API_KEY,
        "with_genres": ",".join(str(g) for g in genre_ids),
        "sort_by": "popularity.desc",
        "include_adult": False,
        "language": language,
    }
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json().get("results", [])


def tmdb_trending_movies(language: str = "en"):
    url = f"{TMDB_BASE_URL}/trending/movie/day"
    params = {"api_key": TMDB_API_KEY, "language": language}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json().get("results", [])


def tmdb_get_movie_details(movie_id: int, language: str = "en"):
    url = f"{TMDB_BASE_URL}/movie/{movie_id}"
    params = {"api_key": TMDB_API_KEY, "append_to_response": "credits,videos", "language": language}
    r = requests.get(url, params=params, timeout=10)
    r.raise_for_status()
    return r.json()


def tmdb_get_watch_link(movie_id: int, region: str = DEFAULT_WATCH_REGION):
    """Returns (provider_names_str, link) for legal streaming options, or (None, None)."""
    url = f"{TMDB_BASE_URL}/movie/{movie_id}/watch/providers"
    params = {"api_key": TMDB_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        r.raise_for_status()
        data = r.json().get("results", {}).get(region)
        if not data:
            return None, None
        providers = data.get("flatrate") or data.get("rent") or data.get("buy") or []
        names = ", ".join(p["provider_name"] for p in providers[:4])
        return (names or None), data.get("link")
    except requests.RequestException:
        return None, None


def get_trailer_url(movie_details: dict):
    videos = movie_details.get("videos", {}).get("results", [])
    for video in videos:
        if video.get("site") == "YouTube" and video.get("type") == "Trailer":
            return f"https://www.youtube.com/watch?v={video['key']}"
    return None


def get_top_cast(movie_details: dict, limit: int = 5):
    cast = movie_details.get("credits", {}).get("cast", [])
    names = [m["name"] for m in cast[:limit]]
    return ", ".join(names) if names else "Not available"


def results_keyboard(results: list) -> InlineKeyboardMarkup:
    buttons = []
    for movie in results[:8]:
        title = movie.get("title", "Untitled")
        year = (movie.get("release_date") or "")[:4]
        label = f"{title} ({year})" if year else title
        buttons.append([InlineKeyboardButton(label, callback_data=f"movie:{movie['id']}")])
    return InlineKeyboardMarkup(buttons)


# ============================================================
# Command handlers
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    get_user(user_id)  # ensure record exists

    # Handle referral deep-link: /start ref_<referrer_id>
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer_id = int(context.args[0][4:])
        except ValueError:
            referrer_id = None
        user = get_user(user_id)
        if referrer_id and referrer_id != user_id and user.get("referred_by") is None:
            user["referred_by"] = referrer_id
            user["bonus_actions"] = user.get("bonus_actions", 0) + REFERRAL_BONUS
            referrer = get_user(referrer_id)
            referrer["bonus_actions"] = referrer.get("bonus_actions", 0) + REFERRAL_BONUS
            _save_users(_users_cache)

    # Membership is re-checked every single /start, on purpose.
    if not await is_channel_member(context, user_id):
        lang = get_user(user_id).get("language", "en")
        await update.message.reply_text(t(lang, "join_required"), reply_markup=join_gate_keyboard())
        return

    lang = get_user(user_id).get("language", "en")
    await update.message.reply_text(t(lang, "welcome"), parse_mode="Markdown", reply_markup=main_menu_keyboard())


async def membership_recheck_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    lang = get_user(user_id).get("language", "en")
    if await is_channel_member(context, user_id):
        await query.answer("✅ Verified!")
        await query.edit_message_text(t(lang, "welcome"), parse_mode="Markdown")
        await query.message.reply_text("Menu ready 👇", reply_markup=main_menu_keyboard())
    else:
        await query.answer(t(lang, "not_joined_yet"), show_alert=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Use the menu buttons below, or just type a movie name to search.\n\n"
        "/premium - view subscription plans\n"
        "/invite - get your referral link\n"
        "/support - contact support",
        reply_markup=main_menu_keyboard(),
    )


# ---- Language ----

def language_keyboard() -> InlineKeyboardMarkup:
    rows, row = [], []
    for code, (label, flag) in LANGUAGES.items():
        row.append(InlineKeyboardButton(f"{label} {flag}", callback_data=f"lang:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def language_set_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    code = query.data.split(":")[1]
    if code not in LANGUAGES:
        return
    update_user(query.from_user.id, language=code)
    label, flag = LANGUAGES[code]
    await query.edit_message_text(f"Language set to {label} {flag}")


# ---- Search / details ----

async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    user_id = update.effective_user.id
    lang = get_user(user_id).get("language", "en")

    if not check_and_use_action_quota(user_id):
        await update.message.reply_text(t(lang, "limit_reached", limit=FREE_DAILY_LIMIT))
        return

    searching_msg = await update.message.reply_text(t(lang, "searching", query=query))
    try:
        results = tmdb_search_movies(query, language=lang)
    except requests.RequestException:
        await searching_msg.edit_text("⚠️ Couldn't reach the movie database right now. Please try again in a moment.")
        return

    if not results:
        await searching_msg.edit_text(t(lang, "no_results", query=query))
        return

    await searching_msg.edit_text(
        t(lang, "found_results", count=min(len(results), 8), query=query),
        reply_markup=results_keyboard(results),
    )


async def movie_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    lang = get_user(user_id).get("language", "en")
    movie_id = int(query.data.split(":")[1])

    try:
        details = tmdb_get_movie_details(movie_id, language=lang)
    except requests.RequestException:
        await query.message.reply_text("⚠️ Couldn't load movie details. Please try again.")
        return

    title = details.get("title", "Untitled")
    year = (details.get("release_date") or "Unknown")[:4]
    overview = details.get("overview") or "No synopsis available."
    rating = details.get("vote_average", 0)
    genres = ", ".join(g["name"] for g in details.get("genres", [])) or "Unknown"
    runtime = details.get("runtime")
    runtime_text = f"{runtime} min" if runtime else "Unknown"
    cast_text = get_top_cast(details)
    trailer_url = get_trailer_url(details)
    poster_path = details.get("poster_path")
    provider_names, watch_link = tmdb_get_watch_link(movie_id)

    caption = (
        f"🎬 *{title}* ({year})\n\n"
        f"⭐ Rating: {rating:.1f}/10\n"
        f"🎭 Genres: {genres}\n"
        f"⏱ Runtime: {runtime_text}\n"
        f"👥 Cast: {cast_text}\n\n"
        f"📝 {overview}"
    )
    if provider_names:
        caption += f"\n\n📺 Available on: {provider_names}"

    buttons = []
    row = []
    if trailer_url:
        row.append(InlineKeyboardButton("▶️ Trailer", url=trailer_url))
    if watch_link:
        row.append(InlineKeyboardButton("📺 Where to Watch", url=watch_link))
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("➕ Add to Watchlist", callback_data=f"watchlist_add:{movie_id}")])
    keyboard = InlineKeyboardMarkup(buttons)

    if poster_path:
        await query.message.reply_photo(
            photo=f"{TMDB_IMAGE_BASE}{poster_path}", caption=caption, parse_mode="Markdown", reply_markup=keyboard
        )
    else:
        await query.message.reply_text(caption, parse_mode="Markdown", reply_markup=keyboard)


# ---- Mood recommendations ----

def mood_keyboard() -> InlineKeyboardMarkup:
    rows, row = [], []
    for mood in MOOD_GENRES:
        row.append(InlineKeyboardButton(mood.capitalize(), callback_data=f"mood:{mood}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def mood_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mood = query.data.split(":")[1]
    await send_mood_recommendations(query.message, query.from_user.id, mood, edit=True)


async def send_mood_recommendations(message, user_id: int, mood: str, edit: bool = False):
    lang = get_user(user_id).get("language", "en")
    if not check_and_use_action_quota(user_id):
        text = t(lang, "limit_reached", limit=FREE_DAILY_LIMIT)
        await (message.edit_text(text) if edit else message.reply_text(text))
        return

    try:
        results = tmdb_discover_by_genres(MOOD_GENRES[mood], language=lang)
    except requests.RequestException:
        await message.reply_text("⚠️ Couldn't reach the movie database right now. Please try again.")
        return

    if not results:
        await message.reply_text(f"No recommendations found for \"{mood}\" right now.")
        return

    text = f"Here's what fits a *{mood}* mood:"
    markup = results_keyboard(results)
    if edit:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=markup)
    else:
        await message.reply_text(text, parse_mode="Markdown", reply_markup=markup)


# ---- Trending ----

async def send_trending(message, user_id: int):
    lang = get_user(user_id).get("language", "en")
    try:
        results = tmdb_trending_movies(language=lang)
    except requests.RequestException:
        await message.reply_text("⚠️ Couldn't reach the movie database right now. Please try again.")
        return
    if not results:
        await message.reply_text("No trending movies found right now.")
        return
    await message.reply_text("🔥 *Trending today:*", parse_mode="Markdown", reply_markup=results_keyboard(results))


# ---- Watchlist ----

async def watchlist_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    movie_id = int(query.data.split(":")[1])
    lang = get_user(user_id).get("language", "en")

    try:
        details = tmdb_get_movie_details(movie_id, language=lang)
    except requests.RequestException:
        await query.answer("Couldn't add right now, try again.", show_alert=True)
        return

    title = details.get("title", "Untitled")
    user = get_user(user_id)
    if not any(m["id"] == movie_id for m in user["watchlist"]):
        user["watchlist"].append({"id": movie_id, "title": title})
        _save_users(_users_cache)
        await query.answer(f"Added \"{title}\" to your watchlist ✅")
    else:
        await query.answer(f"\"{title}\" is already in your watchlist.")


async def send_watchlist(message, user_id: int):
    user = get_user(user_id)
    watchlist = user.get("watchlist", [])
    if not watchlist:
        await message.reply_text("Your watchlist is empty. Add movies with the ➕ button on any movie card.")
        return
    buttons = [
        [
            InlineKeyboardButton(item["title"], callback_data=f"movie:{item['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"watchlist_remove:{item['id']}"),
        ]
        for item in watchlist[:20]
    ]
    await message.reply_text("🎬 *Your Watchlist:*", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))


async def watchlist_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    movie_id = int(query.data.split(":")[1])
    user = get_user(user_id)
    user["watchlist"] = [m for m in user["watchlist"] if m["id"] != movie_id]
    _save_users(_users_cache)
    await send_watchlist(query.message, user_id)


# ---- Referral ----

async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    await update.message.reply_text(
        "🎁 *Invite friends, get bonus searches!*\n\n"
        f"Share your link:\n{link}\n\n"
        f"You and your friend each get +{REFERRAL_BONUS} bonus free actions when they join.",
        parse_mode="Markdown",
    )


# ---- Premium / Stars payments ----

def premium_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(f"⚡ Weekly - {PLAN_INFO['weekly']['stars']}⭐", callback_data="buy:weekly")],
            [InlineKeyboardButton(f"🔥 Monthly - {PLAN_INFO['monthly']['stars']}⭐", callback_data="buy:monthly")],
            [InlineKeyboardButton(f"🌟 Yearly - {PLAN_INFO['yearly']['stars']}⭐", callback_data="buy:yearly")],
        ]
    )


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_premium(user_id):
        days = days_left_of_premium(user_id)
        await update.message.reply_text(f"✅ You already have Premium — {days} day(s) left.")
        return

    text = (
        "🌟 *Cinema Premium*\n\n"
        f"• Unlimited searches & recommendations (free tier: {FREE_DAILY_LIMIT}/day)\n"
        "• Priority support\n\n"
        "Pay securely with Telegram Stars — no card needed:"
    )
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=premium_keyboard())


async def buy_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    plan_key = query.data.split(":")[1]
    plan = PLAN_INFO[plan_key]
    user_id = query.from_user.id

    await context.bot.send_invoice(
        chat_id=query.message.chat_id,
        title=f"Viking Cinema Premium - {plan['label']}",
        description=f"Unlimited searches & recommendations for {plan['days']} days.",
        payload=f"premium:{plan_key}:{user_id}",
        currency="XTR",
        prices=[LabeledPrice(label=plan["label"], amount=plan["stars"])],
        provider_token="",  # empty string is required for Telegram Stars payments
    )


async def pre_checkout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    # Extra validation could go here (e.g. re-check payload format) before approving.
    await query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    try:
        _, plan_key, user_id_str = payment.invoice_payload.split(":")
        user_id = int(user_id_str)
    except (ValueError, KeyError):
        await update.message.reply_text("⚠️ Payment received but couldn't be matched to a plan. Contact support.")
        return

    activate_premium(user_id, plan_key)
    days_left = days_left_of_premium(user_id)
    await update.message.reply_text(
        f"✅ Payment received! Premium is active — {days_left} day(s) remaining. Enjoy unlimited access!"
    )


async def grant_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only manual override: /grant <user_id> <weekly|monthly|yearly>"""
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    if len(context.args) < 2 or context.args[1] not in PLAN_INFO:
        await update.message.reply_text("Usage: /grant <telegram_user_id> <weekly|monthly|yearly>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a valid user ID.")
        return
    activate_premium(target_id, context.args[1])
    await update.message.reply_text(f"✅ Premium ({context.args[1]}) granted to user {target_id}.")


async def revoke_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /revoke <telegram_user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("That doesn't look like a valid user ID.")
        return
    update_user(target_id, premium=False, premium_until=None)
    await update.message.reply_text(f"Premium revoked for user {target_id}.")


# ---- Support ----

async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🛟 *Need help?*\n\n"
        f"• Telegram: {SUPPORT_USERNAME}\n"
        f"• Email: {SUPPORT_EMAIL}\n"
        f"• Hours: {SUPPORT_HOURS}\n\n"
        "Include your Telegram username and a short description of the issue."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ============================================================
# Text message router (menu buttons + free-typed searches)
# ============================================================

@require_membership
async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    lang = get_user(user_id).get("language", "en")

    routes = {
        MENU_MOOD: lambda: update.message.reply_text("How are you feeling? Pick a mood:", reply_markup=mood_keyboard()),
        MENU_TRENDING: lambda: send_trending(update.message, user_id),
        MENU_WATCHLIST: lambda: send_watchlist(update.message, user_id),
        MENU_LANGUAGE: lambda: update.message.reply_text(t(lang, "choose_language"), reply_markup=language_keyboard()),
        MENU_PREMIUM: lambda: premium_command(update, context),
        MENU_INVITE: lambda: invite_command(update, context),
        MENU_SUPPORT: lambda: support_command(update, context),
    }

    if text == MENU_SEARCH:
        await update.message.reply_text("Type the movie name you want to search:")
        return

    if text in routes:
        await routes[text]()
        return

    # Anything else typed is treated as a movie search query.
    await run_search(update, context, text)


@require_membership
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /search <movie name>")
        return
    await run_search(update, context, " ".join(context.args))


@require_membership
async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.args and context.args[0].lower() in MOOD_GENRES:
        await send_mood_recommendations(update.message, update.effective_user.id, context.args[0].lower())
        return
    await update.message.reply_text("How are you feeling? Pick a mood:", reply_markup=mood_keyboard())


# ============================================================
# Main entry point
# ============================================================

def main():
    if not TELEGRAM_BOT_TOKEN or not TMDB_API_KEY:
        print("ERROR: Set the TELEGRAM_BOT_TOKEN and TMDB_API_KEY environment variables before running.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("find", find_command))
    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler("invite", invite_command))
    app.add_handler(CommandHandler("support", support_command))
    app.add_handler(CommandHandler("grant", grant_premium_command))
    app.add_handler(CommandHandler("revoke", revoke_premium_command))

    app.add_handler(CallbackQueryHandler(membership_recheck_callback, pattern=r"^check_membership$"))
    app.add_handler(CallbackQueryHandler(language_set_callback, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(mood_pick_callback, pattern=r"^mood:"))
    app.add_handler(CallbackQueryHandler(movie_details_callback, pattern=r"^movie:"))
    app.add_handler(CallbackQueryHandler(watchlist_add_callback, pattern=r"^watchlist_add:"))
    app.add_handler(CallbackQueryHandler(watchlist_remove_callback, pattern=r"^watchlist_remove:"))
    app.add_handler(CallbackQueryHandler(buy_plan_callback, pattern=r"^buy:"))

    app.add_handler(PreCheckoutQueryHandler(pre_checkout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    print("Viking Cinema bot is running... Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
