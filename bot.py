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
  - Trending / Top Rated / Upcoming / Now Playing / "Surprise Me" shortcuts
  - Browse by genre, and search for an actor/actress's filmography
  - Similar-movies and "full collection" (franchise) lookups from any movie card
  - Quick 👍 / 👎 rating on any movie card
  - Personal watchlist (add/remove movies)
  - Preferred "Where to Watch" region picker
  - Referral bonus (invite a friend, both of you get bonus free searches)
  - Free tier: 2 actions/day. Premium: unlimited. Owner/admins: always unlimited.
  - Two ways to pay: Telegram Stars (auto-activated) or an external card/bank
    link (Paystack-style) confirmed manually by an admin
  - Inactivity win-back: users who go quiet for 3+ days get a single
    "come back" nudge with a Trending shortcut
  - EVERY message the bot sends auto-deletes ~2 minutes after being sent, and
    says so up front so the user can forward/save it to Telegram Saved
    Messages first if they want to keep it - keeps the chat from filling up
  - A simple in-flight lock so a double-tap or duplicate update can never
    trigger two searches / two result cards for the same request
  - "Where to Watch" button linking to legal streaming providers (TMDB data),
    instead of any in-bot download
  - Admin broadcast + stats commands

NOTE ON SCOPE: this bot deliberately does NOT let users pick a video quality
and download a movie file. TMDB only provides metadata (posters, synopsis,
cast, trailers, legal-provider links) - never actual video files - so a
"pick HD / estimate size / download" flow can only be built on top of pirated
video sources, which isn't something this script does.

SETUP:
1. Paste your Telegram Bot Token below (from @BotFather).
2. Paste your TMDB API Key below (from themoviedb.org -> Settings -> API).
3. Fill in CHANNEL_USERNAME, ADMIN_USER_IDS, OWNER_USERNAME, SUPPORT_*,
   star prices, and (optionally) PAYSTACK_PAYMENT_LINKS below.
4. Run:  python3 bot.py

Requirements (already installed):
    pip3 install python-telegram-bot requests
"""

import asyncio
import json
import os
import random
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
    Application,
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
OWNER_USERNAME = "VikingFounder"

# ---- Support / contact info ----
SUPPORT_USERNAME = "@VikingFounder"
SUPPORT_EMAIL = "officialvikingstudio@gmail.com"
SUPPORT_HOURS = "Mon-Sat, 9am-6pm (WAT)"

# ---- Subscription plans, paid in Telegram Stars (currency code "XTR") ----
# Telegram Stars have their own exchange rate (set by Telegram, not you) - check
# the current rate in BotFather/Telegram docs before launch. These were lowered
# from the original prices to be more affordable (roughly $1.3 / $4 / $19.5 at
# typical Stars rates - re-check the live rate and adjust if it drifts).
PLAN_INFO = {
    "weekly":  {"days": 7,   "stars": 99,   "label": "Weekly"},
    "monthly": {"days": 30,  "stars": 299,  "label": "Monthly"},
    "yearly":  {"days": 365, "stars": 1499, "label": "Yearly"},
}

# ---- Second payment option: a card/bank link (e.g. Paystack payment page),
# confirmed manually. Paste a real payment-page link per plan to show the
# button; leave a value blank ("") to hide that plan's card/bank option.
PAYSTACK_PAYMENT_LINKS = {
    "weekly": "",     # e.g. "https://paystack.com/pay/your-weekly-link"
    "monthly": "",
    "yearly": "",
}
PAYSTACK_DISPLAY_PRICES = {   # shown next to each card/bank button - lowered to match Stars pricing
    "weekly": "₦1,500",
    "monthly": "₦3,500",
    "yearly": "₦25,000",
}

FREE_DAILY_LIMIT = 2   # free actions (searches / recommendations) per day
REFERRAL_BONUS = 2     # bonus free actions granted to referrer + new user

DEFAULT_WATCH_REGION = "US"   # fallback region for "Where to Watch" lookups
WATCH_REGIONS = {
    "US": "🇺🇸 United States",
    "GB": "🇬🇧 United Kingdom",
    "NG": "🇳🇬 Nigeria",
    "CA": "🇨🇦 Canada",
    "IN": "🇮🇳 India",
    "DE": "🇩🇪 Germany",
    "FR": "🇫🇷 France",
    "ZA": "🇿🇦 South Africa",
}

# Every bot message auto-deletes this many seconds after being sent, and
# says so up front so the user has time to forward/save it first.
AUTO_DELETE_SECONDS = 120
AUTO_DELETE_NOTICE = (
    "\n\n🗑 _This message disappears in 2 minutes — forward it to your "
    "Saved Messages if you want to keep it._"
)

# Inactivity win-back: nudge a user once they've been quiet this many days.
INACTIVITY_REMINDER_DAYS = 3
# How often the background job checks for inactive users.
INACTIVITY_CHECK_INTERVAL_SECONDS = 6 * 60 * 60  # every 6 hours

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
            "region": None,            # preferred "Where to Watch" region
            "premium": False,
            "premium_until": None,     # ISO date string
            "actions_today": 0,
            "last_action_date": None,
            "bonus_actions": 0,
            "watchlist": [],           # list of {"id": int, "title": str}
            "ratings": {},             # {"<movie_id>": "up" | "down"}
            "referred_by": None,
            "last_active": None,       # ISO date string, updated on every interaction
            "last_reengaged": None,    # ISO date string, last win-back ping sent
        }
        _save_users(_users_cache)
    return _users_cache[key]


def update_user(user_id: int, **fields):
    user = get_user(user_id)
    user.update(fields)
    _save_users(_users_cache)


def touch_last_active(user_id: int):
    """Call this from any handler that represents real user activity, so the
    inactivity win-back job knows the user is still around."""
    update_user(user_id, last_active=date.today().isoformat())


def get_watch_region(user_id: int) -> str:
    return get_user(user_id).get("region") or DEFAULT_WATCH_REGION


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
# Universal auto-delete: every bot-sent message uses one of these two
# helpers, which (a) appends the "this will vanish in 2 minutes" notice and
# (b) schedules the message for deletion. This is the ONLY place that sends
# messages, so there's no risk of a message slipping through un-deleted.
# ============================================================

async def _delete_after_delay(bot, chat_id: int, message_id: int, delay: int):
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass  # already gone, or the bot lacks delete rights in this chat - fine either way


def schedule_delete(bot, chat_id: int, message_id: int, delay: int = AUTO_DELETE_SECONDS):
    asyncio.create_task(_delete_after_delay(bot, chat_id, message_id, delay))


async def send_text(target_message, text: str, delay: int = AUTO_DELETE_SECONDS, notice: bool = True, **kwargs):
    """Reply with text that auto-deletes after `delay` seconds. Set notice=False
    only for messages you deliberately want to keep (admin tool output, etc.)."""
    final_text = text + AUTO_DELETE_NOTICE if notice else text
    if notice and "parse_mode" not in kwargs:
        kwargs["parse_mode"] = "Markdown"
    sent = await target_message.reply_text(final_text, **kwargs)
    if delay:
        schedule_delete(sent.get_bot(), sent.chat_id, sent.message_id, delay)
    return sent


async def send_photo(target_message, photo: str, caption: str, delay: int = AUTO_DELETE_SECONDS, notice: bool = True, **kwargs):
    final_caption = caption + AUTO_DELETE_NOTICE if notice else caption
    if notice and "parse_mode" not in kwargs:
        kwargs["parse_mode"] = "Markdown"
    sent = await target_message.reply_photo(photo=photo, caption=final_caption, **kwargs)
    if delay:
        schedule_delete(sent.get_bot(), sent.chat_id, sent.message_id, delay)
    return sent


async def finalize_edit(edited_message, delay: int = AUTO_DELETE_SECONDS):
    """Call after editing a message (e.g. the 'Searching...' -> results edit)
    to schedule the now-final version for deletion too."""
    schedule_delete(edited_message.get_bot(), edited_message.chat_id, edited_message.message_id, delay)


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
        await send_text(message, t(lang, "join_required"), reply_markup=join_gate_keyboard())
    return wrapped


# ============================================================
# Main menu (persistent reply keyboard)
# ============================================================

MENU_SEARCH = "🔍 Search Movie"
MENU_ACTOR = "🧑‍🎤 Search Actor"
MENU_MOOD = "🎭 Mood Recommend"
MENU_GENRES = "🗂 Browse Genres"
MENU_TRENDING = "📺 Trending"
MENU_TOP_RATED = "🏆 Top Rated"
MENU_UPCOMING = "📅 Upcoming"
MENU_NOW_PLAYING = "🎥 Now Playing"
MENU_SURPRISE = "🎲 Surprise Me"
MENU_WATCHLIST = "🎬 My Watchlist"
MENU_REGION = "🌍 Watch Region"
MENU_LANGUAGE = "🌐 Language"
MENU_PREMIUM = "⭐ Premium"
MENU_INVITE = "🎁 Invite Friends"
MENU_SUPPORT = "🛟 Support"


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [MENU_SEARCH, MENU_ACTOR],
            [MENU_MOOD, MENU_GENRES],
            [MENU_TRENDING, MENU_TOP_RATED],
            [MENU_UPCOMING, MENU_NOW_PLAYING],
            [MENU_SURPRISE, MENU_WATCHLIST],
            [MENU_REGION, MENU_LANGUAGE],
            [MENU_PREMIUM, MENU_INVITE],
            [MENU_SUPPORT],
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


def _tmdb_get(path: str, params: dict | None = None) -> dict:
    url = f"{TMDB_BASE_URL}{path}"
    full_params = {"api_key": TMDB_API_KEY}
    if params:
        full_params.update(params)
    r = requests.get(url, params=full_params, timeout=10)
    r.raise_for_status()
    return r.json()


def tmdb_search_movies(query: str, language: str = "en"):
    return _tmdb_get("/search/movie", {"query": query, "include_adult": False, "language": language}).get("results", [])


def tmdb_discover_by_genres(genre_ids: list, language: str = "en"):
    return _tmdb_get(
        "/discover/movie",
        {
            "with_genres": ",".join(str(g) for g in genre_ids),
            "sort_by": "popularity.desc",
            "include_adult": False,
            "language": language,
        },
    ).get("results", [])


def tmdb_genre_list(language: str = "en"):
    return _tmdb_get("/genre/movie/list", {"language": language}).get("genres", [])


def tmdb_trending_movies(language: str = "en"):
    return _tmdb_get("/trending/movie/day", {"language": language}).get("results", [])


def tmdb_top_rated_movies(language: str = "en"):
    return _tmdb_get("/movie/top_rated", {"language": language}).get("results", [])


def tmdb_upcoming_movies(language: str = "en", region: str = DEFAULT_WATCH_REGION):
    return _tmdb_get("/movie/upcoming", {"language": language, "region": region}).get("results", [])


def tmdb_now_playing_movies(language: str = "en", region: str = DEFAULT_WATCH_REGION):
    return _tmdb_get("/movie/now_playing", {"language": language, "region": region}).get("results", [])


def tmdb_similar_movies(movie_id: int, language: str = "en"):
    return _tmdb_get(f"/movie/{movie_id}/similar", {"language": language}).get("results", [])


def tmdb_collection_details(collection_id: int, language: str = "en"):
    return _tmdb_get(f"/collection/{collection_id}", {"language": language})


def tmdb_search_person(query: str, language: str = "en"):
    return _tmdb_get("/search/person", {"query": query, "language": language}).get("results", [])


def tmdb_person_movie_credits(person_id: int, language: str = "en"):
    return _tmdb_get(f"/person/{person_id}/movie_credits", {"language": language}).get("cast", [])


def tmdb_get_movie_details(movie_id: int, language: str = "en"):
    return _tmdb_get(f"/movie/{movie_id}", {"append_to_response": "credits,videos", "language": language})


def tmdb_get_watch_link(movie_id: int, region: str = DEFAULT_WATCH_REGION):
    """Returns (provider_names_str, link) for legal streaming options, or (None, None)."""
    try:
        data = _tmdb_get(f"/movie/{movie_id}/watch/providers").get("results", {}).get(region)
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


def genre_keyboard(genres: list) -> InlineKeyboardMarkup:
    rows, row = [], []
    for g in genres[:20]:
        row.append(InlineKeyboardButton(g["name"], callback_data=f"genre:{g['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


# ============================================================
# Shared "movie card" renderer (used by search results, similar-movies,
# watchlist taps, and Surprise Me, so there's exactly one code path that
# actually sends a movie card - no risk of two different flows each
# sending their own copy)
# ============================================================

async def send_movie_card(target_message, movie_id: int, lang: str, region: str = DEFAULT_WATCH_REGION):
    try:
        details = tmdb_get_movie_details(movie_id, language=lang)
    except requests.RequestException:
        await send_text(target_message, "⚠️ Couldn't load movie details. Please try again.")
        return

    title = details.get("title", "Untitled")
    year = (details.get("release_date") or "Unknown")[:4]
    overview = details.get("overview") or "No synopsis available."
    if len(overview) > 400:
        overview = overview[:397] + "..."
    rating = details.get("vote_average", 0)
    genres = ", ".join(g["name"] for g in details.get("genres", [])) or "Unknown"
    runtime = details.get("runtime")
    runtime_text = f"{runtime} min" if runtime else "Unknown"
    cast_text = get_top_cast(details)
    trailer_url = get_trailer_url(details)
    poster_path = details.get("poster_path")
    provider_names, watch_link = tmdb_get_watch_link(movie_id, region=region)
    collection = details.get("belongs_to_collection")

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

    top_row = []
    if trailer_url:
        top_row.append(InlineKeyboardButton("▶️ Trailer", url=trailer_url))
    if watch_link:
        top_row.append(InlineKeyboardButton("📺 Where to Watch", url=watch_link))

    buttons = []
    if top_row:
        buttons.append(top_row)
    buttons.append(
        [
            InlineKeyboardButton("➕ Watchlist", callback_data=f"watchlist_add:{movie_id}"),
            InlineKeyboardButton("🔁 Similar", callback_data=f"similar:{movie_id}"),
        ]
    )
    if collection:
        buttons.append([InlineKeyboardButton(f"🎞 Full \"{collection['name']}\" Collection", callback_data=f"collection:{collection['id']}")])
    buttons.append(
        [
            InlineKeyboardButton("👍", callback_data=f"rate:{movie_id}:up"),
            InlineKeyboardButton("👎", callback_data=f"rate:{movie_id}:down"),
        ]
    )
    keyboard = InlineKeyboardMarkup(buttons)

    if poster_path:
        await send_photo(target_message, f"{TMDB_IMAGE_BASE}{poster_path}", caption, reply_markup=keyboard)
    else:
        await send_text(target_message, caption, reply_markup=keyboard)


# ============================================================
# Command handlers
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    get_user(user_id)  # ensure record exists
    touch_last_active(user_id)

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
        await send_text(update.message, t(lang, "join_required"), reply_markup=join_gate_keyboard())
        return

    lang = get_user(user_id).get("language", "en")
    await send_text(update.message, t(lang, "welcome"), reply_markup=main_menu_keyboard())


async def membership_recheck_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    touch_last_active(user_id)
    lang = get_user(user_id).get("language", "en")
    if await is_channel_member(context, user_id):
        await query.answer("✅ Verified!")
        await query.edit_message_text(t(lang, "welcome"), parse_mode="Markdown")
        await finalize_edit(query.message)
        await send_text(query.message, "Menu ready 👇", reply_markup=main_menu_keyboard())
    else:
        await query.answer(t(lang, "not_joined_yet"), show_alert=True)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_last_active(update.effective_user.id)
    await send_text(
        update.message,
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
    touch_last_active(query.from_user.id)
    code = query.data.split(":")[1]
    if code not in LANGUAGES:
        return
    update_user(query.from_user.id, language=code)
    label, flag = LANGUAGES[code]
    await query.edit_message_text(f"Language set to {label} {flag}")
    await finalize_edit(query.message)


# ---- Watch region ----

def region_keyboard() -> InlineKeyboardMarkup:
    rows, row = [], []
    for code, label in WATCH_REGIONS.items():
        row.append(InlineKeyboardButton(label, callback_data=f"region:{code}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def region_set_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    touch_last_active(query.from_user.id)
    code = query.data.split(":")[1]
    if code not in WATCH_REGIONS:
        return
    update_user(query.from_user.id, region=code)
    await query.edit_message_text(f"🌍 Watch region set to {WATCH_REGIONS[code]}. \"Where to Watch\" links will use this from now on.")
    await finalize_edit(query.message)


# ---- Search / details ----
# In-flight lock: prevents a double-tap, a slow network retry, or a
# duplicate Telegram update from ever starting a second search (and
# therefore a second results/movie card) for the same user at the same time.
_active_actions: set[int] = set()

# Simple pending-input state for flows that need a follow-up free-text
# message (currently just "search for an actor").
_awaiting_input: dict[int, str] = {}


async def run_search(update: Update, context: ContextTypes.DEFAULT_TYPE, query: str):
    user_id = update.effective_user.id
    if user_id in _active_actions:
        return  # a search is already in flight for this user - ignore the duplicate trigger
    _active_actions.add(user_id)
    try:
        touch_last_active(user_id)
        username = update.effective_user.username
        lang = get_user(user_id).get("language", "en")

        if not check_and_use_action_quota(user_id, username):
            await send_text(update.message, t(lang, "limit_reached", limit=FREE_DAILY_LIMIT))
            return

        searching_msg = await update.message.reply_text(t(lang, "searching", query=query))
        try:
            results = tmdb_search_movies(query, language=lang)
        except requests.RequestException:
            await searching_msg.edit_text("⚠️ Couldn't reach the movie database right now. Please try again in a moment.")
            await finalize_edit(searching_msg)
            return

        if not results:
            await searching_msg.edit_text(t(lang, "no_results", query=query) + AUTO_DELETE_NOTICE, parse_mode="Markdown")
            await finalize_edit(searching_msg)
            return

        # Edit the same "searching..." message into the results list, so
        # exactly one message is ever shown for a single search - never two.
        await searching_msg.edit_text(
            t(lang, "found_results", count=min(len(results), 8), query=query) + AUTO_DELETE_NOTICE,
            parse_mode="Markdown",
            reply_markup=results_keyboard(results),
        )
        await finalize_edit(searching_msg)
    finally:
        _active_actions.discard(user_id)


async def movie_details_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    touch_last_active(user_id)
    lang = get_user(user_id).get("language", "en")
    region = get_watch_region(user_id)
    movie_id = int(query.data.split(":")[1])
    await send_movie_card(query.message, movie_id, lang, region=region)


async def similar_movies_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    touch_last_active(user_id)
    lang = get_user(user_id).get("language", "en")
    movie_id = int(query.data.split(":")[1])
    try:
        results = tmdb_similar_movies(movie_id, language=lang)
    except requests.RequestException:
        await send_text(query.message, "⚠️ Couldn't load similar movies. Please try again.")
        return
    if not results:
        await send_text(query.message, "No similar movies found for that one.")
        return
    await send_text(query.message, "🔁 *Similar movies:*", reply_markup=results_keyboard(results))


async def collection_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    touch_last_active(user_id)
    lang = get_user(user_id).get("language", "en")
    collection_id = int(query.data.split(":")[1])
    try:
        data = tmdb_collection_details(collection_id, language=lang)
    except requests.RequestException:
        await send_text(query.message, "⚠️ Couldn't load the collection. Please try again.")
        return
    parts = data.get("parts", [])
    if not parts:
        await send_text(query.message, "No other movies found in this collection.")
        return
    await send_text(
        query.message,
        f"🎞 *{data.get('name', 'Collection')}:*",
        reply_markup=results_keyboard(parts),
    )


async def rate_movie_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    touch_last_active(user_id)
    _, movie_id_str, direction = query.data.split(":")
    user = get_user(user_id)
    user.setdefault("ratings", {})[movie_id_str] = direction
    _save_users(_users_cache)
    await query.answer("👍 Thanks for rating!" if direction == "up" else "Thanks, noted!")


# ---- Genre browsing ----

async def send_genre_picker(message, lang: str):
    try:
        genres = tmdb_genre_list(language=lang)
    except requests.RequestException:
        await send_text(message, "⚠️ Couldn't reach the movie database right now. Please try again.")
        return
    if not genres:
        await send_text(message, "No genres available right now.")
        return
    await send_text(message, "🗂 Pick a genre:", reply_markup=genre_keyboard(genres))


async def genre_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    touch_last_active(user_id)
    lang = get_user(user_id).get("language", "en")
    genre_id = int(query.data.split(":")[1])
    try:
        results = tmdb_discover_by_genres([genre_id], language=lang)
    except requests.RequestException:
        await send_text(query.message, "⚠️ Couldn't reach the movie database right now. Please try again.")
        return
    if not results:
        await send_text(query.message, "No movies found for that genre right now.")
        return
    await send_text(query.message, "🗂 *Movies in this genre:*", reply_markup=results_keyboard(results))


# ---- Actor / actress search ----

async def prompt_actor_search(message, user_id: int):
    _awaiting_input[user_id] = "actor_search"
    await send_text(message, "🧑‍🎤 Type the actor or actress's name:")


async def run_actor_search(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str):
    user_id = update.effective_user.id
    if user_id in _active_actions:
        return
    _active_actions.add(user_id)
    try:
        touch_last_active(user_id)
        username = update.effective_user.username
        lang = get_user(user_id).get("language", "en")

        if not check_and_use_action_quota(user_id, username):
            await send_text(update.message, t(lang, "limit_reached", limit=FREE_DAILY_LIMIT))
            return

        try:
            people = tmdb_search_person(name, language=lang)
        except requests.RequestException:
            await send_text(update.message, "⚠️ Couldn't reach the movie database right now. Please try again.")
            return
        if not people:
            await send_text(update.message, f"No actor/actress found for \"{name}\".")
            return

        person = people[0]
        try:
            credits = tmdb_person_movie_credits(person["id"], language=lang)
        except requests.RequestException:
            await send_text(update.message, "⚠️ Couldn't load their filmography. Please try again.")
            return

        credits = sorted(credits, key=lambda m: m.get("popularity", 0), reverse=True)
        if not credits:
            await send_text(update.message, f"No movies found for {person.get('name', name)}.")
            return

        await send_text(
            update.message,
            f"🧑‍🎤 *{person.get('name', name)}* — top movies:",
            reply_markup=results_keyboard(credits),
        )
    finally:
        _active_actions.discard(user_id)


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
    await send_mood_recommendations(
        query.message, query.from_user.id, mood, username=query.from_user.username, edit=True
    )


async def send_mood_recommendations(message, user_id: int, mood: str, username: str | None = None, edit: bool = False):
    if user_id in _active_actions:
        return
    _active_actions.add(user_id)
    try:
        touch_last_active(user_id)
        lang = get_user(user_id).get("language", "en")
        if not check_and_use_action_quota(user_id, username):
            text = t(lang, "limit_reached", limit=FREE_DAILY_LIMIT)
            if edit:
                await message.edit_text(text + AUTO_DELETE_NOTICE, parse_mode="Markdown")
                await finalize_edit(message)
            else:
                await send_text(message, text)
            return

        try:
            results = tmdb_discover_by_genres(MOOD_GENRES[mood], language=lang)
        except requests.RequestException:
            await send_text(message, "⚠️ Couldn't reach the movie database right now. Please try again.")
            return

        if not results:
            await send_text(message, f"No recommendations found for \"{mood}\" right now.")
            return

        text = f"Here's what fits a *{mood}* mood:"
        markup = results_keyboard(results)
        if edit:
            await message.edit_text(text + AUTO_DELETE_NOTICE, parse_mode="Markdown", reply_markup=markup)
            await finalize_edit(message)
        else:
            await send_text(message, text, reply_markup=markup)
    finally:
        _active_actions.discard(user_id)


# ---- Trending / Top Rated / Upcoming / Now Playing / Surprise Me ----

async def send_trending(message, user_id: int):
    lang = get_user(user_id).get("language", "en")
    try:
        results = tmdb_trending_movies(language=lang)
    except requests.RequestException:
        await send_text(message, "⚠️ Couldn't reach the movie database right now. Please try again.")
        return
    if not results:
        await send_text(message, "No trending movies found right now.")
        return
    await send_text(message, "🔥 *Trending today:*", reply_markup=results_keyboard(results))


async def send_top_rated(message, user_id: int):
    lang = get_user(user_id).get("language", "en")
    try:
        results = tmdb_top_rated_movies(language=lang)
    except requests.RequestException:
        await send_text(message, "⚠️ Couldn't reach the movie database right now. Please try again.")
        return
    if not results:
        await send_text(message, "No top-rated movies found right now.")
        return
    await send_text(message, "🏆 *Top rated of all time:*", reply_markup=results_keyboard(results))


async def send_upcoming(message, user_id: int):
    lang = get_user(user_id).get("language", "en")
    region = get_watch_region(user_id)
    try:
        results = tmdb_upcoming_movies(language=lang, region=region)
    except requests.RequestException:
        await send_text(message, "⚠️ Couldn't reach the movie database right now. Please try again.")
        return
    if not results:
        await send_text(message, "No upcoming releases found for your region right now.")
        return
    await send_text(message, "📅 *Coming soon:*", reply_markup=results_keyboard(results))


async def send_now_playing(message, user_id: int):
    lang = get_user(user_id).get("language", "en")
    region = get_watch_region(user_id)
    try:
        results = tmdb_now_playing_movies(language=lang, region=region)
    except requests.RequestException:
        await send_text(message, "⚠️ Couldn't reach the movie database right now. Please try again.")
        return
    if not results:
        await send_text(message, "No movies currently in theaters for your region.")
        return
    await send_text(message, "🎥 *Now playing in theaters:*", reply_markup=results_keyboard(results))


async def send_surprise(message, user_id: int, username: str | None = None):
    if user_id in _active_actions:
        return
    _active_actions.add(user_id)
    try:
        touch_last_active(user_id)
        lang = get_user(user_id).get("language", "en")
        if not check_and_use_action_quota(user_id, username):
            await send_text(message, t(lang, "limit_reached", limit=FREE_DAILY_LIMIT))
            return

        try:
            pool = tmdb_trending_movies(language=lang) + tmdb_top_rated_movies(language=lang)
        except requests.RequestException:
            await send_text(message, "⚠️ Couldn't reach the movie database right now. Please try again.")
            return
        if not pool:
            await send_text(message, "Couldn't find a surprise pick right now, try again in a bit.")
            return

        pick = random.choice(pool)
        region = get_watch_region(user_id)
        await send_text(message, "🎲 Your surprise pick:", delay=AUTO_DELETE_SECONDS)
        await send_movie_card(message, pick["id"], lang, region=region)
    finally:
        _active_actions.discard(user_id)


# ---- Watchlist ----

async def watchlist_add_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    touch_last_active(user_id)
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
        await send_text(message, "Your watchlist is empty. Add movies with the ➕ button on any movie card.")
        return
    buttons = [
        [
            InlineKeyboardButton(item["title"], callback_data=f"movie:{item['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"watchlist_remove:{item['id']}"),
        ]
        for item in watchlist[:20]
    ]
    await send_text(message, "🎬 *Your Watchlist:*", reply_markup=InlineKeyboardMarkup(buttons))


async def watchlist_remove_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    touch_last_active(user_id)
    movie_id = int(query.data.split(":")[1])
    user = get_user(user_id)
    user["watchlist"] = [m for m in user["watchlist"] if m["id"] != movie_id]
    _save_users(_users_cache)
    await send_watchlist(query.message, user_id)


# ---- Referral ----

async def invite_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    touch_last_active(user_id)
    bot_username = context.bot.username
    link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    await send_text(
        update.message,
        "🎁 *Invite friends, get bonus searches!*\n\n"
        f"Share your link:\n{link}\n\n"
        f"You and your friend each get +{REFERRAL_BONUS} bonus free actions when they join.",
    )


# ---- Premium / payments ----

def premium_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"⚡ Weekly - {PLAN_INFO['weekly']['stars']}⭐", callback_data="buy:weekly")],
        [InlineKeyboardButton(f"🔥 Monthly - {PLAN_INFO['monthly']['stars']}⭐", callback_data="buy:monthly")],
        [InlineKeyboardButton(f"🌟 Yearly - {PLAN_INFO['yearly']['stars']}⭐", callback_data="buy:yearly")],
    ]
    # Card/bank option per plan, only shown once you paste a real link above.
    for plan_key, link in PAYSTACK_PAYMENT_LINKS.items():
        if link:
            price = PAYSTACK_DISPLAY_PRICES.get(plan_key, "")
            label = PLAN_INFO[plan_key]["label"]
            rows.append([InlineKeyboardButton(f"💳 {label} (Card/Bank) - {price}", url=link)])
    rows.append([InlineKeyboardButton("🧾 Paid by card/bank? Send proof", callback_data="pay_proof")])
    return InlineKeyboardMarkup(rows)


async def premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username
    touch_last_active(user_id)

    if is_owner(username):
        await send_text(update.message, "👑 You're the bot owner — you already have unlimited access, no plan needed.")
        return

    if is_premium(user_id):
        days = days_left_of_premium(user_id)
        await send_text(update.message, f"✅ You already have Premium — {days} day(s) left.")
        return

    text = (
        "🌟 *Cinema Premium*\n\n"
        f"• Unlimited searches & recommendations (free tier: {FREE_DAILY_LIMIT}/day)\n"
        "• Priority support\n\n"
        "Pay with Telegram Stars (instant) or card/bank (manual confirmation):"
    )
    await send_text(update.message, text, reply_markup=premium_keyboard())


async def pay_proof_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    touch_last_active(query.from_user.id)
    await send_text(
        query.message,
        "🧾 *Manual payment confirmation*\n\n"
        f"Send your payment screenshot/reference to {SUPPORT_USERNAME} along with your Telegram "
        "username and the plan you paid for. An admin will activate your Premium shortly after verifying.",
    )


async def buy_plan_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    touch_last_active(query.from_user.id)
    plan_key = query.data.split(":")[1]
    plan = PLAN_INFO[plan_key]
    user_id = query.from_user.id

    # Left un-auto-deleted on purpose: this opens Telegram's own payment
    # sheet, and the user may need a moment to complete or revisit it.
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
        await send_text(update.message, "⚠️ Payment received but couldn't be matched to a plan. Contact support.")
        return

    activate_premium(user_id, plan_key)
    days_left = days_left_of_premium(user_id)
    # Kept as a permanent receipt rather than auto-deleted.
    await update.message.reply_text(
        f"✅ Payment received! Premium is active — {days_left} day(s) remaining. Enjoy unlimited access!"
    )


async def grant_premium_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only manual override: /grant <user_id> <weekly|monthly|yearly>
    Use this to activate Premium for someone who paid via the card/bank link."""
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
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"✅ Your Premium ({PLAN_INFO[context.args[1]]['label']}) has been activated. Enjoy unlimited access!",
        )
    except TelegramError:
        pass


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


async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /broadcast <message> - DMs every known user."""
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    text = " ".join(context.args)
    sent, failed = 0, 0
    for uid_str in list(_users_cache.keys()):
        try:
            await context.bot.send_message(chat_id=int(uid_str), text=text, parse_mode="Markdown")
            sent += 1
        except TelegramError:
            failed += 1
    await update.message.reply_text(f"Broadcast done — sent: {sent}, failed: {failed}.")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin-only: /stats - quick counts, useful alongside broadcast/grant."""
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    total = len(_users_cache)
    premium_count = sum(1 for u in _users_cache.values() if u.get("premium"))
    await update.message.reply_text(f"👥 Users: {total}\n⭐ Premium: {premium_count}")


# ---- Support ----

async def support_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_last_active(update.effective_user.id)
    text = (
        "🛟 Need help?\n\n"
        f"• Telegram: {SUPPORT_USERNAME}\n"
        f"• Email: {SUPPORT_EMAIL}\n"
        f"• Hours: {SUPPORT_HOURS}\n\n"
        "Include your Telegram username and a short description of the issue."
    )
    await send_text(update.message, text)


# ============================================================
# Inactivity win-back (background loop, no external scheduler needed)
# ============================================================

async def _send_inactivity_reminders(bot):
    today = date.today()
    for uid_str, user in list(_users_cache.items()):
        last_active = user.get("last_active")
        if not last_active:
            continue
        try:
            last_active_date = date.fromisoformat(last_active)
        except ValueError:
            continue

        days_inactive = (today - last_active_date).days
        if days_inactive < INACTIVITY_REMINDER_DAYS:
            continue

        last_reengaged = user.get("last_reengaged")
        if last_reengaged:
            try:
                if (today - date.fromisoformat(last_reengaged)).days < INACTIVITY_REMINDER_DAYS:
                    continue  # already nudged recently - don't spam
            except ValueError:
                pass

        try:
            sent = await bot.send_message(
                chat_id=int(uid_str),
                text=(
                    "👋 *We miss you at Viking Cinema!*\n\n"
                    "New movies have landed since you last checked in. Tap below to see what's trending 🎬"
                    + AUTO_DELETE_NOTICE
                ),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("🔥 See Trending Now", callback_data="reengage_trending")]]
                ),
            )
            schedule_delete(bot, sent.chat_id, sent.message_id, AUTO_DELETE_SECONDS)
            user["last_reengaged"] = today.isoformat()
            _save_users(_users_cache)
        except TelegramError:
            continue  # user blocked the bot, deleted their account, etc. - skip and move on


async def _inactivity_reminder_loop(bot):
    while True:
        try:
            await _send_inactivity_reminders(bot)
        except Exception:
            pass  # never let one bad run kill the loop
        await asyncio.sleep(INACTIVITY_CHECK_INTERVAL_SECONDS)


async def reengage_trending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    touch_last_active(user_id)
    await send_trending(query.message, user_id)


async def _on_startup(app: Application):
    app.create_task(_inactivity_reminder_loop(app.bot))


# ============================================================
# Text message router (menu buttons + free-typed searches)
# ============================================================

@require_membership
async def text_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = update.effective_user.id
    username = update.effective_user.username
    touch_last_active(user_id)
    lang = get_user(user_id).get("language", "en")

    # A pending "type a name" flow (currently just actor search) takes
    # priority over everything else, including menu button labels.
    pending = _awaiting_input.pop(user_id, None)
    if pending == "actor_search":
        await run_actor_search(update, context, text)
        return

    routes = {
        MENU_MOOD: lambda: send_text(update.message, "How are you feeling? Pick a mood:", reply_markup=mood_keyboard()),
        MENU_GENRES: lambda: send_genre_picker(update.message, lang),
        MENU_ACTOR: lambda: prompt_actor_search(update.message, user_id),
        MENU_TRENDING: lambda: send_trending(update.message, user_id),
        MENU_TOP_RATED: lambda: send_top_rated(update.message, user_id),
        MENU_UPCOMING: lambda: send_upcoming(update.message, user_id),
        MENU_NOW_PLAYING: lambda: send_now_playing(update.message, user_id),
        MENU_SURPRISE: lambda: send_surprise(update.message, user_id, username),
        MENU_WATCHLIST: lambda: send_watchlist(update.message, user_id),
        MENU_REGION: lambda: send_text(update.message, "🌍 Choose your preferred watch region:", reply_markup=region_keyboard()),
        MENU_LANGUAGE: lambda: send_text(update.message, t(lang, "choose_language"), reply_markup=language_keyboard()),
        MENU_PREMIUM: lambda: premium_command(update, context),
        MENU_INVITE: lambda: invite_command(update, context),
        MENU_SUPPORT: lambda: support_command(update, context),
    }

    if text == MENU_SEARCH:
        await send_text(update.message, "Type the movie name you want to search:")
        return

    if text in routes:
        await routes[text]()
        return

    # Anything else typed is treated as a movie search query.
    await run_search(update, context, text)


@require_membership
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await send_text(update.message, "Usage: /search <movie name>")
        return
    await run_search(update, context, " ".join(context.args))


@require_membership
async def find_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    touch_last_active(update.effective_user.id)
    if context.args and context.args[0].lower() in MOOD_GENRES:
        await send_mood_recommendations(
            update.message,
            update.effective_user.id,
            context.args[0].lower(),
            username=update.effective_user.username,
        )
        return
    await send_text(update.message, "How are you feeling? Pick a mood:", reply_markup=mood_keyboard())


# ============================================================
# Main entry point
# ============================================================

def main():
    if not TELEGRAM_BOT_TOKEN or not TMDB_API_KEY:
        print("ERROR: Set the TELEGRAM_BOT_TOKEN and TMDB_API_KEY environment variables before running.")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(_on_startup).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("find", find_command))
    app.add_handler(CommandHandler("premium", premium_command))
    app.add_handler(CommandHandler("invite", invite_command))
    app.add_handler(CommandHandler("support", support_command))
    app.add_handler(CommandHandler("grant", grant_premium_command))
    app.add_handler(CommandHandler("revoke", revoke_premium_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("stats", stats_command))

    app.add_handler(CallbackQueryHandler(membership_recheck_callback, pattern=r"^check_membership$"))
    app.add_handler(CallbackQueryHandler(language_set_callback, pattern=r"^lang:"))
    app.add_handler(CallbackQueryHandler(region_set_callback, pattern=r"^region:"))
    app.add_handler(CallbackQueryHandler(mood_pick_callback, pattern=r"^mood:"))
    app.add_handler(CallbackQueryHandler(genre_pick_callback, pattern=r"^genre:"))
    app.add_handler(CallbackQueryHandler(movie_details_callback, pattern=r"^movie:"))
    app.add_handler(CallbackQueryHandler(similar_movies_callback, pattern=r"^similar:"))
    app.add_handler(CallbackQueryHandler(collection_callback, pattern=r"^collection:"))
    app.add_handler(CallbackQueryHandler(rate_movie_callback, pattern=r"^rate:"))
    app.add_handler(CallbackQueryHandler(watchlist_add_callback, pattern=r"^watchlist_add:"))
    app.add_handler(CallbackQueryHandler(watchlist_remove_callback, pattern=r"^watchlist_remove:"))
    app.add_handler(CallbackQueryHandler(buy_plan_callback, pattern=r"^buy:"))
    app.add_handler(CallbackQueryHandler(pay_proof_callback, pattern=r"^pay_proof$"))
    app.add_handler(CallbackQueryHandler(reengage_trending_callback, pattern=r"^reengage_trending$"))

    app.add_handler(PreCheckoutQueryHandler(pre_checkout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message_handler))

    print("Viking Cinema bot is running... Press Ctrl+C to stop.")
    app.run_polling()


if __name__ == "__main__":
    main()
