import discord
import requests
import os
import json
import asyncio
import logging
from io import BytesIO
from PIL import Image
from dotenv import load_dotenv
from discord import app_commands
from discord.ext import tasks

# ==================== SETUP ====================
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
RIOT_API_KEY = os.getenv("RIOT_API_KEY")

# Guild ID for instant command sync. Commands synced to a specific guild show
# up immediately instead of waiting up to an hour for global propagation.
TEST_GUILD_ID = int(os.getenv("GUILD_ID", "1543320002692907170"))
TEST_GUILD = discord.Object(id=TEST_GUILD_ID)

# How often (minutes) to check registered players for new ranked games.
# Each check costs 2 Riot API calls per player — raise this if you hit 429s.
TFT_POLL_INTERVAL_MINUTES = int(os.getenv("TFT_POLL_INTERVAL_MINUTES", "5"))

# Where players.json / config.json live. Point this at a mounted volume
# (e.g. /data on Railway) or data is wiped on every redeploy.
DATA_DIR = os.getenv("DATA_DIR", "data")

if not os.path.exists("logs"):
    os.makedirs("logs")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("logs/discord_bot.log"), logging.StreamHandler()]
)

bot = discord.Client(intents=discord.Intents.default())
tree = app_commands.CommandTree(bot)

# ==================== REGION MAPPING ====================
# Riot ID lookups (Account-V1) use "regional" routing.
# Summoner/League lookups use "platform" routing.
PLATFORM_TO_REGIONAL = {
    "na1": "americas", "br1": "americas", "la1": "americas", "la2": "americas", "oc1": "americas",
    "euw1": "europe", "eun1": "europe", "tr1": "europe", "ru": "europe",
    "kr": "asia", "jp1": "asia",
}

VALID_PLATFORMS = set(PLATFORM_TO_REGIONAL.keys())

TIER_ORDER = [
    "IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM",
    "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"
]
DIVISION_ORDER = {"IV": 0, "III": 1, "II": 2, "I": 3}
APEX_TIERS = {"MASTER", "GRANDMASTER", "CHALLENGER"}

# ==================== RIOT ASSETS (Community Dragon) ====================
RANK_EMBLEM_URL = (
    "https://raw.communitydragon.org/latest/plugins/rcp-fe-lol-static-assets/"
    "global/default/ranked-emblem/emblem-{tier}.png"
)
PROFILE_ICON_URL = (
    "https://raw.communitydragon.org/latest/plugins/rcp-be-lol-game-data/"
    "global/default/v1/profile-icons/{icon_id}.jpg"
)

def get_rank_emblem_url(tier: str) -> str:
    return RANK_EMBLEM_URL.format(tier=tier.lower())

def get_profile_icon_url(icon_id: int) -> str:
    return PROFILE_ICON_URL.format(icon_id=icon_id)

def _crop_emblem_sync(tier: str) -> tuple[BytesIO, str]:
    """Download a rank emblem and trim its transparent padding.
    Runs synchronously — call via asyncio.to_thread to avoid blocking the event loop."""
    resp = requests.get(get_rank_emblem_url(tier), timeout=10)
    resp.raise_for_status()
    img = Image.open(BytesIO(resp.content)).convert("RGBA")

    alpha = img.split()[-1]
    bbox = alpha.getbbox()
    if bbox:
        img = img.crop(bbox)

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf, f"emblem_{tier.lower()}.png"

async def get_cropped_emblem_file(tier: str) -> tuple[discord.File, str]:
    """Return (discord.File, attachment filename) for a cropped rank emblem."""
    buf, filename = await asyncio.to_thread(_crop_emblem_sync, tier)
    return discord.File(buf, filename=filename), filename


# ==================== STORAGE ====================
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

DATA_FILE = os.path.join(DATA_DIR, "players.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

def load_players():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_players(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ==================== HELPERS ====================
class LPLookupError(Exception):
    """Expected, user-facing lookup failure (bad Riot ID, wrong region, etc.)."""
    pass

def rank_sort_key(entry):
    tier_index = TIER_ORDER.index(entry["tier"]) if entry["tier"] in TIER_ORDER else -1
    division_index = DIVISION_ORDER.get(entry["division"], 0)
    return (tier_index, division_index, entry["lp"])

async def fetch_profile_icon_id(puuid: str, region: str) -> int | None:
    """Fetch just the summoner's profile icon ID. Returns None (never
    raises) on failure — this is decorative, never something to block on."""
    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        summoner_url = f"https://{region}.api.riotgames.com/tft/summoner/v1/summoners/by-puuid/{puuid}"
        resp = requests.get(summoner_url, headers=headers)
        if resp.ok:
            return resp.json().get("profileIconId")
    except Exception:
        logging.warning("Could not fetch profile icon", exc_info=True)
    return None

async def fetch_puuid(name: str, region: str) -> str:
    """Resolve a Riot ID (GameName#Tag) to a puuid. Raises LPLookupError
    with a user-facing message on a bad region/format or unknown Riot ID."""
    region = region.lower()
    if region not in VALID_PLATFORMS:
        raise LPLookupError(f"Unknown region '{region}'. Valid options: {', '.join(sorted(VALID_PLATFORMS))}")

    if "#" not in name:
        raise LPLookupError("Riot ID must be in the form `GameName#Tag` (e.g. `Player#1234`).")

    game_name, tag_line = name.split("#", 1)
    game_name = game_name.strip()
    tag_line = tag_line.strip()
    regional = PLATFORM_TO_REGIONAL[region]
    headers = {"X-Riot-Token": RIOT_API_KEY}

    account_url = (
        f"https://{regional}.api.riotgames.com/riot/account/v1/accounts/"
        f"by-riot-id/{game_name}/{tag_line}"
    )
    account_resp = requests.get(account_url, headers=headers)
    if account_resp.status_code == 404:
        raise LPLookupError(f"Riot ID '{name}' not found.")
    account_resp.raise_for_status()
    return account_resp.json()["puuid"]

def get_rank_color(rank):
    color_dict = {
        "IRON": 0x808080,
        "BRONZE": 0xCD7F32,
        "SILVER": 0xC0C0C0,
        "GOLD": 0xFFD700,
        "PLATINUM": 0x00FFFF,
        "EMERALD": 0x50C878,
        "DIAMOND": 0xB19CD9,
        "MASTER": 0x800080,
        "GRANDMASTER": 0x800080,
        "CHALLENGER": 0x800080
    }
    return color_dict.get(rank, 0x808080)

# ==================== TFT ====================
TFT_RANKED_QUEUE_ID = 1100  # Match-V1 queue_id for Ranked TFT (Hyper Roll=1130, Double Up=1160)

async def fetch_tft_ranked(name: str, region: str, fetch_icon: bool = False) -> dict:
    """Look up a Riot ID's Ranked TFT entry.
    Raises LPLookupError with a user-facing message on expected failures.
    NOTE: TFT's wins/losses mean something different from LoL's — 'wins' is
    1st-place finishes, 'losses' is every 2nd-8th place finish (per Riot's
    own field docs), not literal wins/losses. Labeled accordingly below."""
    region = region.lower()
    headers = {"X-Riot-Token": RIOT_API_KEY}

    puuid = await fetch_puuid(name, region)
    profile_icon_id = await fetch_profile_icon_id(puuid, region) if fetch_icon else None

    # Note the endpoint shape here differs from LoL's League-V4 — no "/entries/" segment.
    ranked_url = f"https://{region}.api.riotgames.com/tft/league/v1/by-puuid/{puuid}"
    ranked_resp = requests.get(ranked_url, headers=headers)
    if ranked_resp.status_code == 404:
        raise LPLookupError(f"No TFT account found for '{name}' on {region.upper()}.")
    ranked_resp.raise_for_status()
    entries = ranked_resp.json()

    if not entries:
        raise LPLookupError(f"{name} has no TFT ranked games on {region.upper()}.")

    ranked = next((e for e in entries if e.get("queueType") == "RANKED_TFT"), None)
    if not ranked:
        raise LPLookupError(f"{name} has no Ranked TFT games on {region.upper()}.")

    first_places = ranked.get("wins", 0)
    other_places = ranked.get("losses", 0)
    total = first_places + other_places
    first_rate = (first_places / total) * 100 if total else 0

    promo = None
    mini_series = ranked.get("miniSeries")
    if mini_series:
        icon_map = {"W": "🟢", "L": "🔴", "N": "⚪"}
        promo = {
            "wins": mini_series.get("wins", 0),
            "losses": mini_series.get("losses", 0),
            "target": mini_series.get("target", 0),
            "progress_icons": "".join(icon_map.get(c, "⚪") for c in mini_series.get("progress", "")),
        }

    return {
        "tier": ranked["tier"],
        "division": ranked.get("rank", ""),
        "lp": ranked["leaguePoints"],
        "first_places": first_places,
        "other_places": other_places,
        "first_rate": first_rate,
        "profile_icon_id": profile_icon_id,
        "puuid": puuid,
        "promo": promo,
    }

TFT_STATS_GAME_COUNT = int(os.getenv("TFT_STATS_GAME_COUNT", "10"))

async def _fetch_tft_match_placement(match_id: str, regional: str, headers: dict, puuid: str):
    match_url = f"https://{regional}.api.riotgames.com/tft/match/v1/matches/{match_id}"
    resp = await asyncio.to_thread(requests.get, match_url, headers=headers)
    if not resp.ok:
        return None
    match_data = resp.json()
    # TFT Match-V1 uses snake_case field names, unlike LoL's Match-V5 (camelCase).
    if match_data.get("info", {}).get("queue_id") != TFT_RANKED_QUEUE_ID:
        return None
    return next((p for p in match_data["info"]["participants"] if p["puuid"] == puuid), None)

async def fetch_recent_tft_stats(puuid: str, region: str) -> dict | None:
    """Average placement over the player's last TFT_STATS_GAME_COUNT Ranked
    TFT games. Returns None (never raises) if this can't be computed."""
    regional = PLATFORM_TO_REGIONAL[region]
    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        ids_url = (
            f"https://{regional}.api.riotgames.com/tft/match/v1/matches/by-puuid/"
            f"{puuid}/ids?count={TFT_STATS_GAME_COUNT}"
        )
        ids_resp = requests.get(ids_url, headers=headers)
        ids_resp.raise_for_status()
        match_ids = ids_resp.json()
        if not match_ids:
            return None

        participants = await asyncio.gather(
            *[_fetch_tft_match_placement(mid, regional, headers, puuid) for mid in match_ids]
        )
        participants = [p for p in participants if p is not None]
        if not participants:
            return None

        placements = [p["placement"] for p in participants]
        avg_placement = sum(placements) / len(placements)
        top4_rate = (sum(1 for p in placements if p <= 4) / len(placements)) * 100

        return {
            "avg_placement": avg_placement,
            "top4_rate": top4_rate,
            "games_analyzed": len(placements),
        }
    except Exception:
        logging.warning(f"Could not fetch TFT stats for puuid {puuid}", exc_info=True)
        return None

async def fetch_last_tft_match(puuid: str, region: str) -> dict | None:
    """Fetch placement for the player's most recent Ranked TFT match.
    Returns None (never raises) — supplementary flavor, not critical."""
    regional = PLATFORM_TO_REGIONAL[region]
    headers = {"X-Riot-Token": RIOT_API_KEY}
    try:
        # No server-side queue filter on this endpoint, so pull a small
        # window and find the most recent one that was actually Ranked TFT.
        ids_url = f"https://{regional}.api.riotgames.com/tft/match/v1/matches/by-puuid/{puuid}/ids?count=5"
        ids_resp = requests.get(ids_url, headers=headers)
        ids_resp.raise_for_status()
        match_ids = ids_resp.json()
        for match_id in match_ids:
            participant = await _fetch_tft_match_placement(match_id, regional, headers, puuid)
            if participant:
                return {"match_id": match_id, "placement": participant["placement"]}
        return None
    except Exception:
        logging.warning(f"Could not fetch last TFT match for puuid {puuid}", exc_info=True)
        return None

PLACEMENT_EMOJI = {1: "🥇", 2: "🥈", 3: "🥉"}

def format_tft_placement(placement: int) -> str:
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(placement if placement < 10 else placement % 10, "th")
    emoji = PLACEMENT_EMOJI.get(placement, "🔹")
    return f"{emoji} {placement}{suffix} Place"


# ==================== EVENTS ====================
@bot.event
async def on_ready():
    logging.info(f"✅ BOT ONLINE! Logged in as {bot.user}")
    tree.copy_global_to(guild=TEST_GUILD)
    synced = await tree.sync(guild=TEST_GUILD)
    logging.info(f"Slash commands synced to guild ({len(synced)} commands)!")
    # Remove any stale global registrations so commands don't show up twice.
    tree.clear_commands(guild=None)
    await tree.sync(guild=None)
    if not poll_for_tft_updates.is_running():
        poll_for_tft_updates.start()
        logging.info(f"Started TFT auto-update polling loop (every {TFT_POLL_INTERVAL_MINUTES} min).")

# ==================== COMMANDS ====================
@tree.command(name="lp", description="Check your or someone's Teamfight Tactics rank")
@app_commands.describe(
    name="Riot ID in the form GameName#Tag — leave blank to use your (or member's) registered account",
    member="A registered Discord member to check instead of yourself (ignored if 'name' is given)",
    region="Platform region — leave blank to use the registered region, or a default of na1"
)
async def lp(interaction: discord.Interaction, name: str = None, member: discord.Member = None, region: str = None):
    await interaction.response.defer(thinking=True)

    if not RIOT_API_KEY:
        await interaction.followup.send("❌ No RIOT_API_KEY found!")
        return

    if name is None:
        target = member or interaction.user
        guild_id = str(interaction.guild_id)
        entry = load_players().get(guild_id, {}).get(str(target.id))
        if not entry:
            who = "You aren't" if target.id == interaction.user.id else f"{target.mention} isn't"
            await interaction.followup.send(f"❌ {who} registered for TFT. Use `/register` first, or provide a Riot ID directly.")
            return
        name = entry["name"]
        region = region or entry["region"]
    else:
        region = region or "na1"

    try:
        data = await fetch_tft_ranked(name, region, fetch_icon=True)
    except LPLookupError as e:
        await interaction.followup.send(f"❌ {e}")
        return
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        logging.error(f"Riot API HTTP error ({status}): {e}")
        await interaction.followup.send(f"❌ Riot API error ({status}). Check your API key or try again shortly.")
        return
    except Exception as e:
        logging.exception("Unexpected error in /lp")
        await interaction.followup.send(f"❌ Error: {str(e)}")
        return

    division = "" if data["tier"] in APEX_TIERS else f" {data['division']}"
    embed = discord.Embed(
        title=f"{data['tier'].title()}{division} • {data['lp']} LP",
        color=get_rank_color(data['tier'])
    )
    embed.set_author(
        name=f"{name} ({region.upper()})",
        icon_url=get_profile_icon_url(data["profile_icon_id"]) if data.get("profile_icon_id") else None
    )

    tft_stats = await fetch_recent_tft_stats(data["puuid"], region)

    embed.add_field(name="1st Place Finishes", value=str(data["first_places"]), inline=True)
    embed.add_field(name="1st Place Rate", value=f"{data['first_rate']:.1f}%", inline=True)
    if tft_stats:
        embed.add_field(name="Avg. Placement", value=f"{tft_stats['avg_placement']:.2f}", inline=True)

    if data.get("promo"):
        promo = data["promo"]
        embed.add_field(
            name=f"Promotion Series (first to {promo['target']})",
            value=f"{promo['progress_icons']} ({promo['wins']}W - {promo['losses']}L)",
            inline=False
        )

    if tft_stats:
        embed.set_footer(text=f"Avg. placement & Top 4 rate ({tft_stats['top4_rate']:.0f}%) based on last {tft_stats['games_analyzed']} ranked games")

    # No confirmed TFT-specific rank emblem asset path — reusing the LoL
    # emblem art as a stand-in since tier names match (Iron-Challenger).
    try:
        emblem_file, emblem_filename = await get_cropped_emblem_file(data['tier'])
        embed.set_thumbnail(url=f"attachment://{emblem_filename}")
        await interaction.followup.send(embed=embed, file=emblem_file)
    except Exception:
        logging.exception("Could not crop emblem image, falling back to raw URL")
        embed.set_thumbnail(url=get_rank_emblem_url(data['tier']))
        await interaction.followup.send(embed=embed)

@tree.command(name="register", description="Link your Discord account to a Riot ID for the TFT leaderboard")
@app_commands.describe(
    name="Riot ID in the form GameName#Tag (e.g. 'Player#1234')",
    region="Platform region (default na1): na1, euw1, eun1, kr, jp1, br1, la1, la2, oc1, tr1, ru"
)
async def register(interaction: discord.Interaction, name: str, region: str = "na1"):
    await interaction.response.defer(thinking=True)

    if not RIOT_API_KEY:
        await interaction.followup.send("❌ No RIOT_API_KEY found!")
        return

    baseline = None
    try:
        baseline = await fetch_tft_ranked(name, region, fetch_icon=True)
    except LPLookupError as e:
        msg = str(e)
        if "ranked games" not in msg:
            await interaction.followup.send(f"❌ {msg}")
            return
        baseline = None  # account/region is valid, they're just unranked
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        await interaction.followup.send(f"❌ Riot API error ({status}). Try again shortly.")
        return
    except Exception as e:
        logging.exception("Unexpected error in /register")
        await interaction.followup.send(f"❌ Error: {str(e)}")
        return

    guild_id = str(interaction.guild_id)
    players = load_players()
    players.setdefault(guild_id, {})
    entry = {"name": name, "region": region.lower()}
    if baseline:
        entry.update({
            "last_tier": baseline["tier"],
            "last_division": baseline["division"],
            "last_lp": baseline["lp"],
            "last_first_places": baseline["first_places"],
            "last_other_places": baseline["other_places"],
        })
    players[guild_id][str(interaction.user.id)] = entry
    save_players(players)

    embed = discord.Embed(
        title="✅ Registered for TFT",
        description=f"Linked to {interaction.user.mention} — you'll now show up on `/leaderboard`.",
        color=get_rank_color(baseline["tier"]) if baseline else 0x2ECC71
    )
    embed.set_author(
        name=f"{name} ({region.upper()})",
        icon_url=get_profile_icon_url(baseline["profile_icon_id"]) if baseline and baseline.get("profile_icon_id") else None
    )
    if baseline:
        division = "" if baseline["tier"] in APEX_TIERS else f" {baseline['division']}"
        embed.add_field(name="Current Rank", value=f"{baseline['tier'].title()}{division} • {baseline['lp']} LP", inline=False)
    else:
        embed.add_field(name="Current Rank", value="Unranked", inline=False)

    await interaction.followup.send(embed=embed)

@tree.command(name="unregister", description="Remove yourself from this server's TFT leaderboard")
async def unregister(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    players = load_players()
    if guild_id not in players or str(interaction.user.id) not in players[guild_id]:
        await interaction.response.send_message("You're not registered for TFT here.", ephemeral=True)
        return
    del players[guild_id][str(interaction.user.id)]
    save_players(players)
    await interaction.response.send_message("✅ You've been removed from the TFT leaderboard.", ephemeral=True)

@tree.command(name="leaderboard", description="Show the TFT ranked leaderboard for registered server members")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    if not RIOT_API_KEY:
        await interaction.followup.send("❌ No RIOT_API_KEY found!")
        return

    guild_id = str(interaction.guild_id)
    players = load_players().get(guild_id, {})

    if not players:
        await interaction.followup.send("No one has registered for TFT yet — use `/register` first!")
        return

    results = []
    failures = []

    for user_id, info in players.items():
        try:
            data = await fetch_tft_ranked(info["name"], info["region"])
            results.append((user_id, info["name"], data))
        except LPLookupError as e:
            failures.append(f"{info['name']}: {e}")
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            failures.append(f"{info['name']}: Riot API error ({status})")
        except Exception:
            logging.exception(f"Unexpected error fetching TFT leaderboard entry for {info['name']}")
            failures.append(f"{info['name']}: unexpected error")
        await asyncio.sleep(0.3)

    if not results:
        await interaction.followup.send("Couldn't fetch data for any registered players right now. Try again shortly.")
        return

    results.sort(key=lambda r: rank_sort_key(r[2]), reverse=True)

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    for i, (user_id, name, data) in enumerate(results, start=1):
        member = interaction.guild.get_member(int(user_id))
        display = member.mention if member else name
        division = "" if data["tier"] in APEX_TIERS else f" {data['division']}"
        rank_label = medals.get(i, f"`{i}.`")
        lines.append(f"{rank_label} {display} — {data['tier'].title()}{division} • {data['lp']} LP")

    footer_text = f"{len(results)} player{'s' if len(results) != 1 else ''} ranked"
    if failures:
        footer_text += f" • Couldn't fetch: {', '.join(failures)}"

    embed = discord.Embed(
        title=f"🏆 {interaction.guild.name} TFT Leaderboard",
        description="\n".join(lines)[:4096],
        color=get_rank_color(results[0][2]["tier"])
    )
    embed.set_footer(text=footer_text[:2048])

    try:
        emblem_file, emblem_filename = await get_cropped_emblem_file(results[0][2]["tier"])
        embed.set_thumbnail(url=f"attachment://{emblem_filename}")
        await interaction.followup.send(embed=embed, file=emblem_file)
    except Exception:
        logging.exception("Could not crop TFT leaderboard emblem, falling back to raw URL")
        embed.set_thumbnail(url=get_rank_emblem_url(results[0][2]["tier"]))
        await interaction.followup.send(embed=embed)

@tree.command(name="setchannel", description="Set the channel for automatic TFT update announcements (admin only)")
@app_commands.describe(channel="The channel where TFT placement/LP updates should be posted")
@app_commands.checks.has_permissions(manage_guild=True)
async def setchannel(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)
    config = load_config()
    config.setdefault(guild_id, {})
    config[guild_id]["tft_update_channel_id"] = channel.id
    save_config(config)
    await interaction.response.send_message(
        f"✅ TFT update announcements will now be posted in {channel.mention} "
        f"(checked every {TFT_POLL_INTERVAL_MINUTES} min).",
        ephemeral=True
    )

@setchannel.error
async def setchannel_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the 'Manage Server' permission to set the TFT update channel.", ephemeral=True
        )
    else:
        logging.exception("Error in /setchannel")
        await interaction.response.send_message("❌ Something went wrong.", ephemeral=True)


# ==================== TFT AUTOMATIC UPDATES ====================
async def do_poll_tft_updates():
    """Check every registered TFT player for new ranked games and post any
    placement + LP change to that guild's configured TFT update channel."""
    players = load_players()
    config = load_config()
    any_changed = False

    for guild_id, guild_players in players.items():
        channel_cfg = config.get(guild_id)
        if not channel_cfg or not channel_cfg.get("tft_update_channel_id"):
            continue

        channel = bot.get_channel(channel_cfg["tft_update_channel_id"])
        if channel is None:
            continue

        for user_id, info in guild_players.items():
            try:
                data = await fetch_tft_ranked(info["name"], info["region"])
            except LPLookupError:
                await asyncio.sleep(0.3)
                continue
            except Exception:
                logging.exception(f"TFT polling error for {info['name']}")
                await asyncio.sleep(0.3)
                continue

            prev_first = info.get("last_first_places")
            prev_other = info.get("last_other_places")

            if prev_first is not None:
                first_diff = data["first_places"] - prev_first
                other_diff = data["other_places"] - prev_other

                if first_diff > 0 or other_diff > 0:
                    same_rank = (
                        info.get("last_tier") == data["tier"]
                        and info.get("last_division") == data["division"]
                    )
                    lp_note = ""
                    if same_rank:
                        lp_diff = data["lp"] - info.get("last_lp", data["lp"])
                        lp_note = f" ({'+' if lp_diff >= 0 else ''}{lp_diff} LP)"

                    division = "" if data["tier"] in APEX_TIERS else f" {data['division']}"
                    last_match = await fetch_last_tft_match(data["puuid"], info["region"])
                    title = format_tft_placement(last_match["placement"]) if last_match else "New Ranked Game"

                    embed = discord.Embed(
                        title=title,
                        description=f"{data['tier'].title()}{division} • {data['lp']} LP{lp_note}",
                        color=get_rank_color(data["tier"])
                    )
                    profile_icon_id = await fetch_profile_icon_id(data["puuid"], info["region"])
                    embed.set_author(
                        name=f"{info['name']} ({info['region'].upper()})",
                        icon_url=get_profile_icon_url(profile_icon_id) if profile_icon_id else None
                    )
                    try:
                        await channel.send(embed=embed)
                    except discord.Forbidden:
                        logging.warning(f"No permission to post TFT updates in channel {channel.id}")

            info["last_tier"] = data["tier"]
            info["last_division"] = data["division"]
            info["last_lp"] = data["lp"]
            info["last_first_places"] = data["first_places"]
            info["last_other_places"] = data["other_places"]
            any_changed = True
            await asyncio.sleep(0.3)

    if any_changed:
        save_players(players)

@tasks.loop(minutes=TFT_POLL_INTERVAL_MINUTES)
async def poll_for_tft_updates():
    await do_poll_tft_updates()

@poll_for_tft_updates.before_loop
async def before_poll_for_tft_updates():
    await bot.wait_until_ready()

@tree.command(name="forceupdate", description="Manually trigger a TFT update check right now (admin only)")
@app_commands.checks.has_permissions(manage_guild=True)
async def forceupdate(interaction: discord.Interaction):
    await interaction.response.send_message("🔄 Running a TFT update check now...", ephemeral=True)
    await do_poll_tft_updates()

@forceupdate.error
async def forceupdate_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You need the 'Manage Server' permission to force a TFT update check.", ephemeral=True
        )
    else:
        logging.error(f"Error in /forceupdate: {error}", exc_info=error)
        try:
            await interaction.followup.send("❌ Something went wrong — check the bot logs for details.", ephemeral=True)
        except discord.HTTPException:
            pass


# ==================== START ====================
if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("No DISCORD_TOKEN found in environment!")
    logging.info("Starting bot...")
    bot.run(TOKEN)
