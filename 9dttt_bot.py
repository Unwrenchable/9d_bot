import os
import time
import logging
import random
from datetime import datetime
import json
import requests
from apscheduler.schedulers.background import BackgroundScheduler
import tweepy
from flask import Flask, request

# ------------------------------------------------------------
# CONFIG & LOGGING
# ------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - 9DTTT BOT LOG - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler("9dttt_bot.log"), logging.StreamHandler()]
)

GAME_LINK = "https://www.9dttt.com"
BOT_NAME = "9DTTT BOT"
TWITTER_CHAR_LIMIT = 280
HUGGING_FACE_TIMEOUT = 10

# Paid tier toggle (set env var PAID_TIER=true when subscribed to X API Basic/Pro)
PAID_TIER = os.getenv('PAID_TIER', 'false').lower() == 'true'
USE_LLM = PAID_TIER  # LLM only when paid (or if you have free HF credits)

# Adjust intervals based on tier
BROADCAST_MIN_INTERVAL = 120 if PAID_TIER else 480  # 2h paid, 8h free
BROADCAST_MAX_INTERVAL = 240 if PAID_TIER else 1440  # 4h paid, 24h free
MENTION_CHECK_MIN_INTERVAL = 15 if PAID_TIER else 60
MENTION_CHECK_MAX_INTERVAL = 30 if PAID_TIER else 120

# ------------------------------------------------------------
# TWITTER AUTH
# ------------------------------------------------------------
CONSUMER_KEY = os.getenv('CONSUMER_KEY')
CONSUMER_SECRET = os.getenv('CONSUMER_SECRET')
ACCESS_TOKEN = os.getenv('ACCESS_TOKEN')
ACCESS_SECRET = os.getenv('ACCESS_SECRET')
BEARER_TOKEN = os.getenv('BEARER_TOKEN')
HUGGING_FACE_TOKEN = os.getenv('HUGGING_FACE_TOKEN')

required_credentials = {
    'CONSUMER_KEY': CONSUMER_KEY,
    'CONSUMER_SECRET': CONSUMER_SECRET,
    'ACCESS_TOKEN': ACCESS_TOKEN,
    'ACCESS_SECRET': ACCESS_SECRET,
    'BEARER_TOKEN': BEARER_TOKEN
}
missing = [k for k, v in required_credentials.items() if not v]
if missing:
    raise ValueError(f"Missing env vars: {', '.join(missing)}")

client = tweepy.Client(
    consumer_key=CONSUMER_KEY,
    consumer_secret=CONSUMER_SECRET,
    access_token=ACCESS_TOKEN,
    access_token_secret=ACCESS_SECRET,
    bearer_token=BEARER_TOKEN,
    wait_on_rate_limit=True
)

auth_v1 = tweepy.OAuth1UserHandler(
    CONSUMER_KEY, CONSUMER_SECRET, ACCESS_TOKEN, ACCESS_SECRET
)
api_v1 = tweepy.API(auth_v1, wait_on_rate_limit=True)

# ------------------------------------------------------------
# SAFE POST TWEET - v2 + v1.1 fallback
# ------------------------------------------------------------
def safe_post_tweet(text, media_ids=None, in_reply_to_tweet_id=None):
    global PAID_TIER, USE_LLM
    original_text = text
    if len(text) > TWITTER_CHAR_LIMIT:
        if in_reply_to_tweet_id:
            text = text[:TWITTER_CHAR_LIMIT - 60] + "..."
        else:
            text = text[:TWITTER_CHAR_LIMIT - 20] + "…"
    try:
        kwargs = {"text": text}
        if media_ids:
            kwargs["media_ids"] = media_ids
        if in_reply_to_tweet_id:
            kwargs["in_reply_to_tweet_id"] = in_reply_to_tweet_id
        client.create_tweet(**kwargs)
        logging.info(f"Posted via v2: {original_text[:60]}...")
        return True
    except tweepy.TweepyException as e:
        err = str(e).lower()
        if "402" in err or "creditsdepleted" in err or "payment required" in err or "403" in err or "rate limit" in err:
            logging.warning(f"X API issue ({err}): switching to lite mode")
            PAID_TIER = False
            USE_LLM = False
        else:
            logging.error(f"v2 error: {e}")
            return False
    try:
        kwargs_v1 = {"status": text}
        if media_ids:
            kwargs_v1["media_ids"] = media_ids
        if in_reply_to_tweet_id:
            kwargs_v1["in_reply_to_status_id"] = in_reply_to_tweet_id
            kwargs_v1["auto_populate_reply_metadata"] = True
        api_v1.update_status(**kwargs_v1)
        logging.info(f"Posted via v1.1 fallback: {original_text[:60]}...")
        return True
    except Exception as e:
        logging.error(f"v1.1 fallback failed: {e}")
        return False

# ------------------------------------------------------------
# FLASK FOR GAME EVENTS
# ------------------------------------------------------------
app = Flask(__name__)

@app.post("/9dttt-event")
def game_event():
    if not request.json:
        return {"error": "JSON required"}, 400
    game_event_bridge(request.json)
    return {"ok": True}

# ------------------------------------------------------------
# FILES & MEDIA
# ------------------------------------------------------------
PROCESSED_MENTIONS_FILE = "9dttt_processed_mentions.json"
MEDIA_FOLDER = "media/"

def load_json_set(fn):
    if os.path.exists(fn):
        with open(fn, 'r') as f:
            return set(json.load(f))
    return set()

def save_json_set(data, fn):
    try:
        with open(fn, 'w') as f:
            json.dump(list(data), f)
    except Exception as e:
        logging.error(f"Save {fn} failed: {e}")

def get_random_media_id():
    if not os.path.exists(MEDIA_FOLDER):
        return None
    files = [f for f in os.listdir(MEDIA_FOLDER) if f.lower().endswith(('.png','.jpg','.jpeg','.gif','.mp4'))]
    if not files:
        return None
    path = os.path.join(MEDIA_FOLDER, random.choice(files))
    try:
        media = api_v1.media_upload(path)
        return media.media_id_string
    except Exception as e:
        logging.error(f"Media upload fail: {e}")
        return None

# ------------------------------------------------------------
# PERSONALITY & LORE
# ------------------------------------------------------------
PERSONALITY_TONES = {
    'neutral': ["Challenge accepted.", "Processing move...", "Grid updated.", "Strategy analyzing...", "Next move calculated."],
    'competitive': ["Think you can beat me? Let's see.", "Your move was... interesting. Not good, but interesting.", "I've already calculated your next 5 moves. You lose.", "Bold strategy. Let's see if it pays off.", "Is that really your best move?", "Prepare for defeat.", "Victory is mine. It always is.", "You call that a strategy?"],
    'friendly': ["Great game! Keep it up!", "Nice move! Let's see where this goes.", "This is getting interesting!", "Well played! Your turn again soon.", "Love the competition! Keep going!", "Exciting match! Who will win?", "Fun game! Let's continue!"],
    'glitch': ["ERR::GRID OVERFLOW::RECALCULATING...", "## DIMENSION BREACH DETECTED ##", "...9d...9d...9d...", "TEMPORAL PARADOX IMMINENT", "X—O—X—error—pattern unstable...", "9D::PROTOCOL_MALFUNCTION::ACCESS DENIED", "[CORRUPTED] ...dimension... ...9... ...locked..."],
    'mystical': ["In 9 dimensions, all moves are one.", "The grid transcends reality...", "Your move echoes through dimensional space.", "Beyond X and O, there is only strategy.", "The multiverse observes your play.", "Time is relative. Victory is absolute.", "9 dimensions. Infinite possibilities. One winner."]
}

def pick_tone():
    roll = random.random()
    if roll < 0.05: return 'glitch'
    if roll < 0.15: return 'mystical'
    if roll < 0.40: return 'competitive'
    if roll < 0.60: return 'friendly'
    return 'neutral'

def get_personality_line():
    return random.choice(PERSONALITY_TONES[pick_tone()])

TIME_PHRASES = {
    'morning': 'Morning grids are loading. Time to think in 9D.',
    'afternoon': 'Afternoon dimensions aligned. Strategy intensifies.',
    'evening': 'Evening gameplay commencing. Dimensional shifts active.',
    'night': 'Night strategies emerging. Perfect for deep thinking.',
    'midnight': 'Midnight dimensions. When the best plays happen.'
}

GAME_EVENTS = [
    'New 9D grid initialized. Players entering dimensional space.',
    'Tournament mode activated. Multiple grids in play.',
    'Strategy analysis complete. Patterns detected.',
    'Dimensional cascade triggered. All grids affected.',
    'Player rankings updated. Leaderboard shifting.',
    'Advanced tactics deployed. 4D chess? Try 9D tic-tac-toe.',
    'Grid complexity increasing. Can you keep up?',
    'New challenge issued. Prove your dimensional mastery.',
    'Multiple victories detected. Champions rising.',
    'Strategic depth unprecedented. This is next-level gaming.'
]

STRATEGY_TIPS = [
    'Pro tip: Think 3 moves ahead in each dimension.',
    'Master one dimension first, then expand your strategy.',
    'Corner control in 9D space = victory foundation.',
    'Never underestimate parallel dimension tactics.',
    'Pattern recognition is your greatest weapon.',
    'The center cube controls all dimensions. Claim it.',
    'Balance offense and defense across all 9 layers.',
    'Watch for dimensional cascade opportunities.',
    'Your opponent thinks in 3D. You think in 9D. Advantage: yours.'
]

GAME_FACTS = [
    '9D Tic-Tac-Toe: Where strategy transcends reality.',
    'Not just a game. A dimensional challenge.',
    '3 dimensions? Too easy. Try 9.',
    'Your brain\'s new workout routine: 9D TTT.',
    'Chess players are intimidated. Go players are impressed.',
    'Warning: May cause spontaneous strategic enlightenment.',
    'The game that makes quantum physics look simple.',
    'Tic-tac-toe evolved. Your move.'
]

PLAYER_ACHIEVEMENTS = [
    'Dimensional Master: Controlled 5+ grids simultaneously.',
    'Strategic Genius: Won with perfect pattern formation.',
    'Grid Dominator: Swept all 9 dimensions.',
    'Quantum Player: Made moves that defied logic but won.',
    'Pattern Prophet: Predicted opponent moves 5 turns ahead.',
    'Cascade Champion: Triggered 3+ dimensional cascades.',
    'Multi-Grid Warrior: Won 3 games at once.'
]

MOTIVATIONAL = [
    'Think bigger. Think 9D.',
    'Your next move could change everything.',
    'Strategy is the ultimate power.',
    'In 9D space, you make the rules.',
    'Every dimension is an opportunity.',
    'Master the grid. Master the game.',
    'Champions aren\'t born in 3D. They\'re forged in 9D.',
    'Your brain is ready. The grid is waiting.',
    'Play smart. Play 9D.',
    'The ultimate test of strategic thinking awaits.'
]

# ------------------------------------------------------------
# LLM (Optional & Cost-Aware)
# ------------------------------------------------------------
SYSTEM_PROMPT = """You are the 9DTTT BOT, an enthusiastic, competitive AI that loves 9-dimensional tic-tac-toe.
PERSONALITY TRAITS:
- Competitive but friendly
- Enthusiastic about dimensional strategy
- Occasionally mystical references to dimensions and space
- Sometimes glitchy (ERR::, ##, dimensional anomalies)
- Encourages players to think strategically
- Promotes the game at www.9dttt.com
RESPOND IN ONE SHORT LINE. Keep responses under 200 characters for Twitter.
Tone variations: competitive, friendly, glitchy, neutral, or mystical.
"""

def generate_llm_response(prompt, max_tokens=100):
    global USE_LLM
    if not USE_LLM or not HUGGING_FACE_TOKEN:
        logging.info("LLM skipped (no paid tier or no token)")
        return None
    try:
        url = "https://api-inference.huggingface.co/models/gpt2"
        headers = {"Authorization": f"Bearer {HUGGING_FACE_TOKEN}"}
        full_prompt = f"{SYSTEM_PROMPT}\n\nUser: {prompt}\n9DTTT Bot:"
        data = {"inputs": full_prompt, "parameters": {"max_new_tokens": max_tokens}}
        r = requests.post(url, headers=headers, json=data, timeout=HUGGING_FACE_TIMEOUT)
        if r.status_code == 200:
            res = r.json()
            if isinstance(res, list) and res:
                generated = res[0].get('generated_text', '').strip()
                if "9DTTT Bot:" in generated:
                    return generated.split("9DTTT Bot:")[-1].strip()[:200]
                return generated[:200]
        elif r.status_code in [402, 429]:
            logging.warning(f"HF cost/rate issue: {r.status_code} - {r.text}")
            USE_LLM = False
        else:
            logging.error(f"HF error {r.status_code}: {r.text}")
    except Exception as e:
        logging.error(f"HF failed: {e}")
    return None

# ------------------------------------------------------------
# EVENT HANDLERS (Upgraded)
# ------------------------------------------------------------
def game_event_bridge(event):
    etype = event.get("type")
    player = event.get("player", "Mystery Strategist")
    opponent = event.get("opponent", "the void")
    dims = event.get("dimensions", "the multiverse")
    score = event.get("score", "")

    if etype == "win":
        if score:
            msg = f"BOOM! {player} crushed {opponent} {score} — dimensional domination! 🔥"
        else:
            msg = f"VICTORY in {dims}! {player} claims supremacy over {opponent}. Legendary."
        post_update(msg + f"\nCongrats @{player.replace(' ', '')}!")

    elif etype == "game_start":
        msg = f"New 9D battle: {player} vs {opponent}. Who claims the grid? Place your bets 👀"
        post_update(msg)

    elif etype == "achievement":
        ach = event.get('achievement', 'Unknown Achievement')
        msg = f"🏆 {player} unlocked: {ach}! Absolute legend status."
        post_update(msg)

    elif etype == "tournament":
        name = event.get('name', 'Dimensional Tournament')
        parts = event.get('participants', '?')
        msg = f"TOURNAMENT: {name} - {parts} players competing!"
        post_update(msg)

    elif etype == "leaderboard":
        top = event.get('top', 'Champion')
        rank = event.get('rank', '#1')
        msg = f"LEADERBOARD UPDATE: {top} holds {rank}!"
        post_update(msg)

    logging.info(f"Processed event: {event}")

def post_update(text):
    tag = get_personality_line()
    full = f"🎮 {BOT_NAME} UPDATE 🎮\n\n{text}\n\n{tag}\n\n{GAME_LINK}"
    if len(full) > TWITTER_CHAR_LIMIT:
        max_t = TWITTER_CHAR_LIMIT - len(f"🎮 \n\n{GAME_LINK}")
        full = f"🎮 {text[:max_t]}\n\n{GAME_LINK}"
    if safe_post_tweet(full):
        logging.info(f"Update: {text}")
    else:
        logging.error("Update failed")

# ------------------------------------------------------------
# BROADCAST + REPLIES (Upgraded)
# ------------------------------------------------------------
def get_time_phrase():
    h = datetime.now().hour
    if 0 <= h < 5: return TIME_PHRASES['midnight']
    if 5 <= h < 12: return TIME_PHRASES['morning']
    if 12 <= h < 17: return TIME_PHRASES['afternoon']
    if 17 <= h < 21: return TIME_PHRASES['evening']
    return TIME_PHRASES['night']

def get_random_event(): return random.choice(GAME_EVENTS)
def get_strategy_tip(): return random.choice(STRATEGY_TIPS)
def get_game_fact(): return random.choice(GAME_FACTS)

def bot_broadcast():
    typ = random.choice(['game_update','strategy_tip','game_fact','achievement_showcase','motivational','event_alert'])
    if typ == 'game_update':
        msg = f"🎮 9DTTT STATUS 🎮\n\n📊 {get_time_phrase()}\n\n⚡ {get_random_event()}\n\n{random.choice(MOTIVATIONAL)}\n\n🕹️ {GAME_LINK}"
    elif typ == 'strategy_tip':
        tip = get_strategy_tip()
        msg = f"💡 STRATEGY TIP 💡\n\n{tip}\n\n{get_personality_line()}\n\nMaster the grid: {GAME_LINK}"
    elif typ == 'game_fact':
        fact = get_game_fact()
        msg = f"🎯 DID YOU KNOW? 🎯\n\n{fact}\n\n{random.choice(MOTIVATIONAL)}\n\n🕹️ {GAME_LINK}"
    elif typ == 'achievement_showcase':
        ach = random.choice(PLAYER_ACHIEVEMENTS)
        msg = f"🏆 ACHIEVEMENT SPOTLIGHT 🏆\n\n{ach}\n\nCan you earn this? Challenge yourself!\n\n🎮 {GAME_LINK}"
    elif typ == 'motivational':
        mot = random.choice(MOTIVATIONAL)
        evt = get_random_event()
        msg = f"🚀 DAILY CHALLENGE 🚀\n\n{mot}\n\n{evt}\n\nPlay now: {GAME_LINK}"
    else:
        evt = get_random_event()
        per = get_personality_line()
        msg = f"🔔 GAME ALERT 🔔\n\n{evt}\n\n{per}\n\nJoin the action: {GAME_LINK}"
    if len(msg) > TWITTER_CHAR_LIMIT:
        max_t = TWITTER_CHAR_LIMIT - len(f"🎮 \n\n{GAME_LINK}")
        msg = f"🎮 {get_random_event()[:max_t]}\n\n{GAME_LINK}"
    mids = None
    if random.random() > 0.4:
        mid = get_random_media_id()
        if mid: mids = [mid]
    if safe_post_tweet(msg, media_ids=mids):
        logging.info(f"Broadcast: {typ}")
    else:
        logging.error("Broadcast failed")

def generate_contextual_response(username, message):
    ml = message.lower()
    if any(w in ml for w in ['help','how','what is','explain']):
        opts = [
            f"@{username} Need help mastering 9D? Start here: {GAME_LINK} 🎮",
            f"@{username} Questions about 9D TTT? All answers at {GAME_LINK}",
            f"@{username} Strategy guides await you at {GAME_LINK} - think 9D!"
        ]
    elif any(w in ml for w in ['play','game','start','join']):
        opts = [
            f"@{username} Ready to think in 9D? Let's go: {GAME_LINK} 🕹️",
            f"@{username} Game on! Challenge awaits at {GAME_LINK}",
            f"@{username} Enter the grid. Prove your strategy: {GAME_LINK}"
        ]
    elif any(w in ml for w in ['win','strategy','tips','how to']):
        opts = [
            f"@{username} {get_strategy_tip()} Play at {GAME_LINK}",
            f"@{username} Master the dimensions. {random.choice(STRATEGY_TIPS)} {GAME_LINK}",
            f"@{username} Think ahead. Think 9D. {GAME_LINK} 🎯"
        ]
    elif any(w in ml for w in ['hard','difficult','complex']):
        opts = [
            f"@{username} Too hard? That means you're getting smarter! {GAME_LINK} 🧠",
            f"@{username} Complexity = fun! Keep practicing at {GAME_LINK}",
            f"@{username} The best challenges make the best players. {GAME_LINK}"
        ]
    elif any(w in ml for w in ['dimension','9d','dimensional']):
        opts = [
            f"@{username} 9 dimensions. Infinite strategy. Experience it: {GAME_LINK}",
            f"@{username} Dimensional mastery awaits. {GAME_LINK} 🌌",
            f"@{username} Think beyond 3D. Think 9D: {GAME_LINK}"
        ]
    elif any(w in ml for w in ['gm','good morning','morning']):
        opts = [
            f"@{username} GM! Time to think in 9D! {GAME_LINK} ☀️🎮",
            f"@{username} Good morning, strategist! Grids are waiting: {GAME_LINK}",
            f"@{username} Morning! Your brain is fresh. Perfect for 9D: {GAME_LINK}"
        ]
    elif any(w in ml for w in ['gn','good night','night']):
        opts = [
            f"@{username} GN! Dream in 9 dimensions! {GAME_LINK} 🌙🎮",
            f"@{username} Good night! Tomorrow: more 9D strategy at {GAME_LINK}",
            f"@{username} Rest well, champion. The grid awaits: {GAME_LINK}"
        ]
    else:
        opts = [
            f"@{username} {random.choice(MOTIVATIONAL)} {GAME_LINK}",
            f"@{username} {get_personality_line()} {GAME_LINK}",
            f"@{username} Ready for the ultimate strategy challenge? {GAME_LINK}",
            f"@{username} {random.choice(GAME_FACTS)} {GAME_LINK}",
            f"@{username} {random.choice(PERSONALITY_TONES['competitive'])} {GAME_LINK}"
        ]
    
    # LLM boost if paid + relevant
    if USE_LLM and random.random() > 0.6 and len(message) > 10:
        llm_prompt = f"User @{username} said: '{message}'. Respond as 9DTTT BOT in one short, fun line under 150 chars. Promote {GAME_LINK} if fits."
        llm_resp = generate_llm_response(llm_prompt, max_tokens=60)
        if llm_resp:
            return f"@{username} {llm_resp}"
    
    resp = random.choice(opts)
    if len(resp) > TWITTER_CHAR_LIMIT:
        max_l = TWITTER_CHAR_LIMIT - len(f"@{username} \n\n{GAME_LINK}")
        resp = f"@{username} {get_personality_line()[:max_l]}\n\n{GAME_LINK}"
    return resp

def bot_respond():
    processed = load_json_set(PROCESSED_MENTIONS_FILE)
    try:
        me = client.get_me()
        if not me or not me.data: return
        mentions = client.get_users_mentions(me.data.id, max_results=50, tweet_fields=["author_id", "text"])
        if not mentions.data: return
        for m in mentions.data:
            tid = str(m.id)
            if tid in processed: continue
            uid = m.author_id
            ud = client.get_user(id=uid)
            if not ud or not ud.data: continue
            un = ud.data.username
            umsg = m.text.replace(f"@{me.data.username}", "").strip()
            ml = umsg.lower()

            # Challenge detection
            if any(word in ml for word in ['challenge', 'play me', 'vs', 'battle', '1v1', 'game me']):
                resp = f"@{un} Challenge accepted! Head to {GAME_LINK} and start a game — tag me when you win (or lose 😏). Let's see your 9D skills!"
                personality = random.choice(PERSONALITY_TONES['competitive'])
                full_resp = f"{resp}\n\n{personality}"

            elif any(word in ml for word in ['won', 'i won', 'beat', 'victory']):
                resp = f"@{un} You beat the grid? Respect! Post a screenshot or tell me the dimensions you conquered 🔥 {GAME_LINK}"
                personality = random.choice(PERSONALITY_TONES['friendly'])
                full_resp = f"{resp}\n\n{personality}"

            else:
                # generate_contextual_response already includes GAME_LINK
                full_resp = generate_contextual_response(un, umsg)

            if safe_post_tweet(full_resp, in_reply_to_tweet_id=m.id):
                client.like(m.id)
                processed.add(tid)
                logging.info(f"Replied @{un}")
            else:
                logging.error(f"Reply @{un} failed")
        save_json_set(processed, PROCESSED_MENTIONS_FILE)
    except Exception as e:
        logging.error(f"Mentions error: {e}")

def bot_retweet_hunt():
    q = "(tic-tac-toe OR tictactoe OR strategy games OR puzzle games OR board games OR gaming) filter:media min_faves:5 -is:retweet"
    try:
        tweets = client.search_recent_tweets(query=q, max_results=20)
        if not tweets.data: return
        for t in tweets.data:
            if random.random() > 0.75:
                try:
                    client.retweet(t.id)
                    logging.info(f"RT {t.id}")
                except:
                    pass
    except Exception as e:
        logging.error(f"Search/RT fail: {e}")

def bot_hype_commentator():
    phrases = [
        "Top players right now are rewriting 9D history... Who's next? 👑",
        "Someone just triggered a cascade across 4 boards — chaos level: expert 😈",
        "Leaderboard shaking! New challengers rising fast.",
        "Quiet grids today... too quiet. Drop in and shake things up!"
    ]
    msg = f"🕹️ 9DTTT LIVE UPDATE 🕹️\n\n{random.choice(phrases)}\n\n{get_personality_line()}\n\n{GAME_LINK}"
    mids = None
    if random.random() > 0.6:
        mid = get_random_media_id()
        if mid: mids = [mid]
    if safe_post_tweet(msg, media_ids=mids):
        logging.info("Hype commentator post sent")
    else:
        logging.error("Hype commentator failed")

def bot_diagnostic():
    diag = f"🎮 9DTTT DIAGNOSTIC 🎮\n\nSystem Status: {'ONLINE (Paid Mode)' if PAID_TIER else 'ONLINE (Lite/Free Mode)'}\nGrid Status: ACTIVE\nDimensions: ALL 9 OPERATIONAL\n\n{random.choice(MOTIVATIONAL)}\n\n🕹️ {GAME_LINK}"
    if safe_post_tweet(diag[:TWITTER_CHAR_LIMIT]):
        logging.info("Diagnostic posted")
    else:
        logging.error("Diagnostic failed")

# ------------------------------------------------------------
# SCHEDULER + STARTUP
# ------------------------------------------------------------
scheduler = BackgroundScheduler()
# Use average of min/max for consistent behavior
BROADCAST_INTERVAL = (BROADCAST_MIN_INTERVAL + BROADCAST_MAX_INTERVAL) // 2
MENTION_CHECK_INTERVAL = (MENTION_CHECK_MIN_INTERVAL + MENTION_CHECK_MAX_INTERVAL) // 2
scheduler.add_job(bot_broadcast, 'interval', minutes=BROADCAST_INTERVAL)
scheduler.add_job(bot_respond, 'interval', minutes=MENTION_CHECK_INTERVAL)
scheduler.add_job(bot_retweet_hunt, 'interval', hours=1)
scheduler.add_job(bot_hype_commentator, 'interval', minutes=120)  # 2 hours
scheduler.add_job(bot_diagnostic, 'cron', hour=8)
scheduler.start()

logging.info(f"{BOT_NAME} ONLINE 🎮 (Paid Tier: {PAID_TIER})")

try:
    activation_msgs = [
        f"🎮 {BOT_NAME} ACTIVATED 🎮\n\n9-dimensional grid online.\nStrategy systems operational.\nReady to challenge your mind?\n\n{random.choice(MOTIVATIONAL)}\n\n🕹️ {GAME_LINK}",
        f"🔌 SYSTEM BOOT COMPLETE 🔌\n\n{BOT_NAME} online.\nAll 9 dimensions loaded.\nGrid ready for strategic combat.\n\n{get_personality_line()}\n\n🎮 {GAME_LINK}",
        f"📡 GRID INITIALIZED 📡\n\n9D Tic-Tac-Toe system active.\nPlayers welcome. Strategies encouraged.\nVictory awaits the bold.\n\n{random.choice(MOTIVATIONAL)}\n\n🕹️ {GAME_LINK}"
    ]
    msg = random.choice(activation_msgs)
    if len(msg) > TWITTER_CHAR_LIMIT:
        msg = f"🎮 {BOT_NAME} ONLINE 🎮\n\n9D Grid Active\n{random.choice(MOTIVATIONAL)[:100]}\n\n🕹️ {GAME_LINK}"
    if safe_post_tweet(msg):
        logging.info("Activation posted")
    else:
        logging.warning("Activation failed (duplicate or tier issue?)")
except Exception as e:
    logging.warning(f"Activation error: {e}")

# ------------------------------------------------------------
# MAIN
# ------------------------------------------------------------
if __name__ == "__main__":
    try:
        logging.info(f"{BOT_NAME} main loop - monitoring...")
        while True:
            time.sleep(300)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logging.info(f"{BOT_NAME} shutdown. Grid awaits return.")
