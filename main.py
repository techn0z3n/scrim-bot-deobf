import os
import random
import asyncio
from dotenv import load_dotenv
import discord
from discord.ext import commands
from discord.ui import View, Button
from discord import Embed
import json
import time
import uuid  # for unique match IDs


DATA_FILE = "queue_data.json"
ELO_FILE = "elo_data.json"
WIN_ELO = 10  # default ELO for winning a match


load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="=", intents=intents, help_command=None)
# --- Activity tracking ---
last_active = {}  # {user_id: timestamp}
INACTIVITY_LIMIT_DEFAULT = 300  # 5 minutes default
timeouts = {}  # {channel_id: inactivity_seconds}



# --- Data ---
queues = {}
games = {}  # {match_id: {"channel": int, "players": list[int], "status": str, "map": str | None, "winner": int | None}}
registered_channels = {}
drafts = {}
# --- Queue Ban System ---
queue_bans = {}  # {user_id: ban_expiry_timestamp}

def save_bans():
    with open("queue_bans.json", "w") as f:
        json.dump(queue_bans, f, indent=2)

def load_bans():
    global queue_bans
    if os.path.exists("queue_bans.json"):
        with open("queue_bans.json", "r") as f:
            queue_bans = {int(k): v for k, v in json.load(f).items()}


# --- Helper functions ---
def find_user_in_queues(user_id):
    """Return the channel ID where a user is queued, or None if not queued anywhere."""
    for channel_id, members in queues.items():
        if user_id in members:
            return channel_id
    return None

def save_data():
    data = {
        "registered_channels": registered_channels,
        "queues": queues,
        "games": games
    }
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

if os.path.exists(ELO_FILE):
    with open(ELO_FILE, "r") as f:
        elo_data = json.load(f)
else:
    elo_data = {}

def save_elo():
    with open(ELO_FILE, "w") as f:
        json.dump(elo_data, f, indent=2)
        
def load_data():
    global registered_channels, queues, games
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
            registered_channels = {int(k): v for k, v in data.get("registered_channels", {}).items()}
            queues = {int(k): v for k, v in data.get("queues", {}).items()}
            games = data.get("games", {})
            load_bans()

def is_registered(ctx):
    return ctx.channel.id in registered_channels

def get_queue(ctx):
    return queues.setdefault(ctx.channel.id, [])

def get_all_players(channel_id):
    """Return list of all players from active draft."""
    if channel_id not in drafts:
        return []
    draft = drafts[channel_id]
    all_players = []
    for team_members in draft["teams"].values():
        all_players.extend(team_members)
    all_players.extend(draft["teams"].keys())  # include captains
    return all_players

# --- Queue Commands ---
@bot.command(aliases=["t"])
async def teams(ctx, game_id: str = None):
    """Show teams for the current channel or a specific game ID."""
    channel_id = ctx.channel.id

    # Check if a specific game ID was provided
    if game_id:
        game = games.get(game_id)
        if not game:
            return await ctx.send(f"❌ No game found with ID `{game_id}`.")
        channel_id = game["channel"]
        # Find the draft/teams structure (if exists)
        draft = drafts.get(channel_id)
        if draft:
            team_data = draft["teams"]
        else:
            team_data = game.get("teams", {})
        title = f"📋 Teams for Game `{game_id}`"
    else:
        # Default to showing the ongoing draft in this channel
        draft = drafts.get(channel_id)
        if not draft:
            # Try to find the latest game for this channel
            latest_game = None
            for g_id, g_data in games.items():
                if g_data["channel"] == channel_id:
                    latest_game = g_data
            if not latest_game:
                return await ctx.send("❌ No active draft or recent game found in this channel.")
            team_data = latest_game.get("teams", {})
            title = f"📋 Teams for Last Game (`{g_id}`)"
        else:
            team_data = draft["teams"]
            title = "📋 Current Draft Teams"

    if not team_data:
        return await ctx.send("❌ No teams found for this game or draft.")

    # Format output
    embed = discord.Embed(title=title, color=discord.Color.dark_theme())
    for captain_id, members in team_data.items():
        try:
            captain = await ctx.guild.fetch_member(captain_id)
            captain_name = captain.display_name
        except:
            captain_name = f"Unknown ({captain_id})"

        member_mentions = []
        for m_id in members:
            member = ctx.guild.get_member(m_id)
            member_mentions.append(member.mention if member else f"<@{m_id}>")

        embed.add_field(
            name=f"🏴‍☠️ Team {captain_name}",
            value="\n".join(member_mentions) if member_mentions else "*No members yet*",
            inline=False
        )

    await ctx.send(embed=embed)

@bot.command(aliases=["j"])
async def join(ctx):
    """Join the queue, but only one queue per user globally."""
        # --- Check if user is queue-banned ---
    now = time.time()
    ban_expiry = queue_bans.get(ctx.author.id)
    if ban_expiry and now < ban_expiry:
        remaining = int((ban_expiry - now) / 60)
        return await ctx.send(f"🚫 You are queue-banned for another **{remaining} minute(s)**.")

    if not is_registered(ctx):
        return await ctx.send("❌ This channel is not registered for queueing.")

    queue = get_queue(ctx)
    size = registered_channels[ctx.channel.id]["size"]

    # Check if the user is already queued elsewhere
    existing_channel = find_user_in_queues(ctx.author.id)
    if existing_channel and existing_channel != ctx.channel.id:
        return await ctx.send(
            f"🚫 You’re already in a queue in <#{existing_channel}>. Leave there first with `=leave`."
        )

    # Prevent duplicate joins in same channel
    if ctx.author.id in queue:
        return await ctx.send("You're already in the queue!")

    # If queue is full, start the match and clear it
    if len(queue) >= size:
        await ctx.send("⚠️ Current queue is full — starting a new match!")
        await start_draft(ctx, queue.copy())
        queues[ctx.channel.id] = []
        save_data()

    # Add player (to possibly new queue)
    queue = get_queue(ctx)
    queue.append(ctx.author.id)
    await ctx.send(f"✅ {ctx.author.mention} joined the queue! ({len(queue)}/{size})")

    # Start new match if queue fills up after join
    if len(queue) >= size:
        await start_draft(ctx, queue.copy())
        queues[ctx.channel.id] = []
        save_data()



@bot.command(aliases=["l"])
async def leave(ctx):
    """Leave the queue."""
    queue = get_queue(ctx)
    if ctx.author.id not in queue:
        return await ctx.send("You're not in the queue.")
    queue.remove(ctx.author.id)
    await ctx.send(f"👋 {ctx.author.mention} left the queue. You can now join another queue!")
    save_data()


@bot.command(aliases=["q"])
async def queue(ctx):
    """Show queue members."""
    queue = get_queue(ctx)
    if not queue:
        return await ctx.send("🕳️ The queue is empty.")
    members = [f"<@{m_id}>" for m_id in queue]
    await ctx.send("🎯 **Current Queue:**\n" + "\n".join(members))
    
@bot.command()
async def gameslist(ctx, count: int = 5):
    """Show recent matches."""
    if not games:
        return await ctx.send("📭 No games recorded yet.")
    recent = list(games.items())[-count:]
    lines = []
    for match_id, info in recent:
        status = info["status"]
        map_name = info.get("map", "Unknown")
        lines.append(f"`{match_id}` — {status} — Map: **{map_name}**")
    await ctx.send("🎮 **Recent Games:**\n" + "\n".join(lines))

# --- Admin Queue Control Commands ---
@commands.has_permissions(administrator=True)
@bot.command()
async def queueban(ctx, member: discord.Member, minutes: int = 10):
    """
    Ban or unban a user from joining any queues.
    Running again while banned will unban them.
    """
    user_id = member.id
    now = time.time()

    # If already banned -> unban them
    if user_id in queue_bans and now < queue_bans[user_id]:
        queue_bans.pop(user_id, None)
        save_bans()
        return await ctx.send(f"✅ {member.mention} has been **unbanned** from queueing.")

    # Otherwise, apply a new ban
    expiry = now + (minutes * 60)
    queue_bans[user_id] = expiry
    save_bans()

    await ctx.send(f"🚷 {member.mention} is now **queue-banned** for {minutes} minute(s).")

    # Remove them from any queue they're currently in
    for ch_id, queue in queues.items():
        if user_id in queue:
            queue.remove(user_id)
            channel = bot.get_channel(ch_id)
            if channel:
                await channel.send(f"🧹 {member.mention} was removed from the queue due to a queue ban.")
    save_data()

@commands.has_permissions(administrator=True)
@bot.command()
async def resetelo(ctx):
    """(Admin) Reset ELO for all players to 1000."""
    data_file = "elo_data.json"
    elos = {}

    # Optionally, you can initialize all registered users to 0
    for channel_id in registered_channels.keys():
        queue = queues.get(channel_id, [])
        for user_id in queue:
            elos[str(user_id)] = 0
        # Include players in drafts/games
        draft_players = get_all_players(channel_id)
        for user_id in draft_players:
            elos[str(user_id)] = 0

    # Save empty or reset data
    with open(data_file, "w") as f:
        json.dump(elos, f, indent=2)

    await ctx.send("💠 All ELO balances have been reset to 0.")

@commands.has_permissions(administrator=True)
@bot.command()
async def elo(ctx, member: discord.Member, action: str, amount: int):
    """Admin-only: modify a user's ELO: add/subtract/set"""
    user_id = str(member.id)
    action = action.lower()

    if action not in ["add", "subtract", "set"]:
        return await ctx.send("⚠️ Invalid action. Use `add`, `subtract`, or `set`.")

    current = elo_data.get(user_id, 0)
    if action == "add":
        current += amount
        current = max(0, current)
    elif action == "subtract":
        current -= amount
        current = max(0, current)
    elif action == "set":
        current = amount
        current = max(0, current)

    elo_data[user_id] = current
    save_elo()
    await ctx.send(f"✅ {member.mention}'s ELO is now **{current}**.")

    # --- Set Win ELO ---
@commands.has_permissions(administrator=True)
@bot.command()
async def setwinelo(ctx, amount: int):
    """Admin-only: set ELO awarded to winners."""
    global WIN_ELO
    if amount < 0:
        return await ctx.send("⚠️ Win ELO must be positive.")
    WIN_ELO = amount
    await ctx.send(f"🏆 Winning team will now receive **{WIN_ELO} ELO** per player.")

# --- Declare Winner (with auto-finish) ---
@commands.has_permissions(administrator=True)
@bot.command()
async def winner(ctx, captain: discord.Member):
    """Admin-only: award ELO to winning captain's team and finish the game."""
    channel_id = ctx.channel.id
    if channel_id not in drafts:
        return await ctx.send("❌ No active draft in this channel.")

    draft = drafts[channel_id]
    captain_id = captain.id

    if captain_id not in draft["teams"]:
        return await ctx.send(f"⚠️ {captain.mention} is not a captain in this draft.")

    # --- Determine Teams ---
    winners = [captain_id] + draft["teams"][captain_id]
    losers = []
    for cpt, members in draft["teams"].items():
        if cpt != captain_id:
            losers.append(cpt)
            losers.extend(members)

    # --- Apply ELO changes ---
    for user_id in winners:
        user_id_str = str(user_id)
        elo_data[user_id_str] = elo_data.get(user_id_str, 0) + WIN_ELO

    for user_id in losers:
        user_id_str = str(user_id)
        current = elo_data.get(user_id_str, 0)
        elo_data[user_id_str] = max(0, current - 10)  # cannot go below 0

    save_elo()

    # --- Format output ---
    winner_mentions = ", ".join(f"<@{uid}>" for uid in winners)
    loser_mentions = ", ".join(f"<@{uid}>" for uid in losers)

    embed = discord.Embed(
        title="🏆 Match Results",
        color=discord.Color.gold()
    )
    embed.add_field(name="Winning Team (+10 ELO)", value=winner_mentions, inline=False)
    embed.add_field(name="Losing Team (-10 ELO)", value=loser_mentions, inline=False)

    await ctx.send(embed=embed)

    # --- Update game status ---
    match_id = draft.get("id")
    if match_id and match_id in games:
        games[match_id]["status"] = "finished"
        games[match_id]["winner"] = captain_id

    # --- Cleanup ---
    drafts.pop(channel_id, None)
    if channel_id in registered_channels:
        registered_channels[channel_id]["active_game"] = None
    save_data()

    await ctx.send("✅ Game marked as finished and draft cleared.")

    
    # --- Check ELO Balance ---
@bot.command()
async def elobalance(ctx, member: discord.Member = None):
    """Check your or another user's ELO balance."""
    member = member or ctx.author
    balance = elo_data.get(str(member.id), 0)
    await ctx.send(f"💠 {member.mention} has **{balance} ELO**.")

class LeaderboardView(discord.ui.View):
    def __init__(self, entries, per_page=10):
        super().__init__(timeout=120)
        self.entries = entries
        self.per_page = per_page
        self.page = 0

    def format_page(self):
        start = self.page * self.per_page
        end = start + self.per_page
        page_entries = self.entries[start:end]

        desc = ""
        for i, (user_id, elo) in enumerate(page_entries, start=start + 1):
            desc += f"**#{i}** <@{user_id}> — `{elo} ELO`\n"

        embed = discord.Embed(
            title=f"🏅 ELO Leaderboard (Page {self.page + 1}/{(len(self.entries)-1)//self.per_page + 1})",
            description=desc or "No data available.",
            color=discord.Color.blue()
        )
        return embed

    @discord.ui.button(label="⬅️ Prev", style=discord.ButtonStyle.primary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            await interaction.response.edit_message(embed=self.format_page(), view=self)

    @discord.ui.button(label="Next ➡️", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if (self.page + 1) * self.per_page < len(self.entries):
            self.page += 1
            await interaction.response.edit_message(embed=self.format_page(), view=self)


@bot.command(aliases=["lb"])
async def leaderboard(ctx):
    """Show paginated ELO leaderboard."""
    if not elo_data:
        return await ctx.send("📭 No ELO data yet.")

    sorted_elos = sorted(elo_data.items(), key=lambda x: x[1], reverse=True)
    view = LeaderboardView(sorted_elos)
    await ctx.send(embed=view.format_page(), view=view)

@commands.has_permissions(administrator=True)
@bot.command()
async def sub(ctx, user_out: discord.Member, user_in: discord.Member, game_id: str):
    """Substitute a player in an ongoing game: =sub @out @in (game_id)."""
    # Validate game ID
    game = games.get(game_id)
    if not game:
        return await ctx.send(f"❌ No game found with ID `{game_id}`.")
    if game["status"] not in ("draft", "active"):
        return await ctx.send(f"⚠️ Game `{game_id}` is not active or draft phase ended.")

    channel_id = game["channel"]
    if channel_id != ctx.channel.id:
        return await ctx.send(f"🚫 This game belongs to <#{channel_id}>, not this channel.")

    # Make sure both users exist
    if user_out.id not in game["players"]:
        return await ctx.send(f"❌ {user_out.mention} is not in this game.")
    if user_in.id in game["players"]:
        return await ctx.send(f"⚠️ {user_in.mention} is already in this game.")

    # Update player list
    game["players"].remove(user_out.id)
    game["players"].append(user_in.id)

    # If draft data still exists, fix that too
    if channel_id in drafts:
        draft = drafts[channel_id]
        # Replace in teams
        for captain, members in draft["teams"].items():
            if user_out.id in members:
                members.remove(user_out.id)
                members.append(user_in.id)
                break
        # Also check if user_out was a captain
        if user_out.id in draft["captains"]:
            draft["captains"].remove(user_out.id)
            draft["captains"].append(user_in.id)
            # Move their team if necessary
            draft["teams"][user_in.id] = draft["teams"].pop(user_out.id, [])

    save_data()

    await ctx.send(
        f"🔁 **Substitution complete for game `{game_id}`**:\n"
        f"➡️ {user_out.mention} **out**, {user_in.mention} **in**."
    )

@commands.has_permissions(administrator=True)
@bot.command()
async def endgame(ctx):
    """Mark the current match as finished and record the winner."""
    match_id = registered_channels.get(ctx.channel.id, {}).get("active_game")
    if not match_id or match_id not in games:
        return await ctx.send("❌ No active game in this channel.")

    games[match_id]["status"] = "finished"
    registered_channels[ctx.channel.id]["active_game"] = None
    save_data()

    await ctx.send(f"🏆 **Game {match_id} finished!**")

@commands.has_permissions(administrator=True)
@bot.command(aliases=["fj"])
async def forcejoin(ctx, member: discord.Member):
    """(Admin) Force-add a user to the queue."""
    if not is_registered(ctx):
        return await ctx.send("❌ This channel is not registered for queueing.")
    queue = get_queue(ctx)
    if member.id in queue:
        return await ctx.send(f"{member.mention} is already in the queue.")
    queue.append(member.id)
    size = registered_channels[ctx.channel.id]["size"]
    await ctx.send(f"🛠️ Admin added {member.mention} to the queue. ({len(queue)}/{size})")

    # Auto-start draft if queue fills
    if len(queue) >= size:
        await start_draft(ctx, queue.copy())

@commands.has_permissions(administrator=True)
@bot.command(aliases=["fl"])
async def forceleave(ctx, member: discord.Member):
    """(Admin) Force-remove a user from the queue."""
    if not is_registered(ctx):
        return await ctx.send("❌ This channel is not registered for queueing.")
    queue = get_queue(ctx)
    if member.id not in queue:
        return await ctx.send(f"{member.mention} is not currently in the queue.")
    queue.remove(member.id)
    await ctx.send(f"🗑️ Admin removed {member.mention} from the queue. ({len(queue)} remain)")

# --- Admin Commands ---
@commands.has_permissions(administrator=True)
@bot.command()
async def register(ctx):
    registered_channels[ctx.channel.id] = {"size": 10, "active_game": None}
    queues[ctx.channel.id] = []
    save_data()  # <-- persist changes
    await ctx.send("✅ This channel is now registered for queueing.")

@commands.has_permissions(administrator=True)
@bot.command()
async def unregister(ctx):
    registered_channels.pop(ctx.channel.id, None)
    queues.pop(ctx.channel.id, None)
    save_data()  # <-- persist changes
    await ctx.send("❌ This channel has been unregistered from queueing.")

@commands.has_permissions(administrator=True)
@bot.command()
async def setup(ctx, number: int):
    if number < 2 or number > 12 or number % 2 != 0:
        return await ctx.send("⚠️ Queue size must be an even number between 2–12.")
    if not is_registered(ctx):
        return await ctx.send("❌ This channel is not registered yet. Use =register first.")
    registered_channels[ctx.channel.id]["size"] = number
    save_data()  # <-- persist changes
    await ctx.send(f"⚙️ Queue size set to {number} players.")


# --- Draft Phase ---
async def start_draft(ctx, queue_list):
    """Start a new draft when queue fills."""
    size = registered_channels[ctx.channel.id]["size"]
    match_id = str(uuid.uuid4())[:8]  # short unique ID

    # Pick captains
    captains = random.sample(queue_list, 2)
    remaining = [p for p in queue_list if p not in captains]

    drafts[ctx.channel.id] = {
        "id": match_id,
        "captains": captains,
        "teams": {captains[0]: [], captains[1]: []},
        "remaining": remaining,
        "turn": captains[0],
        "phase": "draft"
    }

    # ✅ Save to games list
    games[match_id] = {
        "channel": ctx.channel.id,
        "players": queue_list.copy(),
        "status": "draft",
        "map": None,
        "winner": None
    }
    registered_channels[ctx.channel.id]["active_game"] = match_id

    save_data()

    await ctx.send(
        f"🎯 **Draft Started!** (Match ID: `{match_id}`)\n"
        f"Captains: <@{captains[0]}> 🆚 <@{captains[1]}>\n"
        f"<@{drafts[ctx.channel.id]['turn']}> picks first using `=pick or =p @player`"
    )


@bot.command(aliases=["p"])
async def pick(ctx, member: discord.Member):
    """Pick a player during draft."""
    if ctx.channel.id not in drafts:
        return await ctx.send("No active draft in this channel.")

    draft = drafts[ctx.channel.id]
    if ctx.author.id != draft["turn"]:
        return await ctx.send("It's not your turn to pick!")

    if member.id not in draft["remaining"]:
        return await ctx.send("That player is not available to pick.")

    draft["teams"][ctx.author.id].append(member.id)
    draft["remaining"].remove(member.id)

    # Swap turn
    other_captain = [c for c in draft["captains"] if c != ctx.author.id][0]
    draft["turn"] = other_captain

    await ctx.send(f"✅ {ctx.author.mention} picked {member.mention}.")

    # Check if draft complete
    if not draft["remaining"]:
        await ctx.send("🏁 Draft complete! Time to vote for gamemode!")
        await start_gamemode_vote(ctx)

# --- Force Start ---
@commands.has_permissions(administrator=True)
@bot.command()
async def forcestart(ctx):
    """Force start game: randomize teams & skip draft."""
    queue = get_queue(ctx)
    if not queue:
        return await ctx.send("❌ No players in queue to start a game with.")

    # Random teams
    random.shuffle(queue)
    half = len(queue) // 2
    team1, team2 = queue[:half], queue[half:]

    drafts[ctx.channel.id] = {
        "captains": [team1[0], team2[0]],
        "teams": {team1[0]: team1[1:], team2[0]: team2[1:]},
        "phase": "voting"
    }

    team1_mentions = ", ".join([f"<@{p}>" for p in team1])
    team2_mentions = ", ".join([f"<@{p}>" for p in team2])

    await ctx.send(
        f"⚡ **Forced Start! Random Teams Assigned:**\n"
        f"🟥 Team 1: {team1_mentions}\n"
        f"🟦 Team 2: {team2_mentions}\n"
        f"➡️ Moving to gamemode voting..."
    )

    await start_gamemode_vote(ctx)

# --- Voting Phases ---
async def start_game(channel, map_name):
    """Begin the game phase and mark game as active."""
    view = GameInfoView(map_name)
    await channel.send("🏁 **Game Ready!**", view=view)

    # Get the current match ID
    match_id = registered_channels[channel.id].get("active_game")
    if match_id and match_id in games:
        games[match_id]["status"] = "active"
        games[match_id]["map"] = map_name
        save_data()

    # ✅ Clear queue after match setup
    queues[channel.id] = []
    save_data()

async def start_gamemode_vote(ctx):
    players = get_all_players(ctx.channel.id)
    view = VoteView(["KOTC", "Classic"],
                    next_step=start_region_vote, players=players)
    await ctx.send("🎮 **Vote for a Gamemode!** (10s or until all votes in)", view=view)
    await view.start_timer(ctx)

async def start_region_vote(ctx, gamemode="Classic"):
    players = get_all_players(ctx.channel.id)
    view = VoteView(
        ["US West", "US East", "US Central"],
        next_step=lambda c, r: start_map_vote(c, gamemode, region=r),
        players=players
    )
    await ctx.send("🌎 **Vote for a Region!** (10s or until all votes in)", view=view)
    await view.start_timer(ctx)

async def start_map_vote(ctx, gamemode="KOTC", region=None):
    players = get_all_players(ctx.channel.id)
    maps = ["Cluckgrounds", "Bastion", "2 Towers", "Helix"] if gamemode == "KOTC" else ["Castle", "Bastion", "Growler", "Road"]
    view = FinalVoteView(maps, players=players)
    
    # Save gamemode and region to game object before voting ends
    match_id = registered_channels[ctx.channel.id].get("active_game")
    if match_id in games:
        games[match_id]["gamemode"] = gamemode
        games[match_id]["region"] = region

    await ctx.send(f"🗺️ **Vote for a Map!** *(Gamemode: {gamemode})* (10s or until all votes in)", view=view)
    await view.start_timer(ctx)



# --- Voting UI Classes ---
class VoteView(discord.ui.View):
    def __init__(self, options, next_step=None, players=None):
        super().__init__(timeout=10)  # 10-second timer
        self.votes = {opt: 0 for opt in options}
        self.voted_users = set()
        self.players = players or []
        self.next_step = next_step
        for opt in options:
            self.add_item(VoteButton(label=opt))

    async def start_timer(self, ctx):
        await asyncio.sleep(10)
        if not self.is_finished():
            await self.end_vote(ctx)
        self.stop()

    async def end_vote(self, ctx):
        if not self.votes:
            return
        winner = max(self.votes, key=self.votes.get)
        await ctx.send(f"# 🗳️ Voting has ended! Winning option: **{winner}**")
        if self.next_step:
            await self.next_step(ctx, winner)

class VoteButton(discord.ui.Button):
    def __init__(self, label):
        super().__init__(label=label, style=discord.ButtonStyle.primary)

    async def callback(self, interaction: discord.Interaction):
        parent: VoteView = self.view
        if interaction.user.id in parent.voted_users:
            return await interaction.response.send_message(
                "⚠️ You already voted!", ephemeral=True
            )
        parent.voted_users.add(interaction.user.id)
        parent.votes[self.label] += 1
        await interaction.response.send_message(
            f"✅ You voted for **{self.label}**!", ephemeral=True
        )

        # End vote early if everyone voted
        if parent.players and len(parent.voted_users) >= len(parent.players):
            await parent.end_vote(interaction.channel)
            parent.stop()

class FinalVoteView(VoteView):
    async def end_vote(self, ctx):
        winner = max(self.votes, key=self.votes.get)
        await ctx.send(f"🗳️ Final map: **{winner}**! Game starting soon...")

        # Start the game
        await start_game(ctx.channel, winner)

        # Send a detailed game info embed
        match_id = registered_channels[ctx.channel.id].get("active_game")
        game = games.get(match_id)
        draft = drafts.get(ctx.channel.id)

        if not game or not draft:
            return  # safety check

        embed = discord.Embed(
            title=f"# 🎮 Game Started! (ID: {match_id})",
            color=discord.Color.green()
        )

        # Add teams
        for captain_id, members in draft["teams"].items():
            try:
                captain = await ctx.guild.fetch_member(captain_id)
                captain_name = captain.display_name
            except:
                captain_name = f"Unknown ({captain_id})"
            member_mentions = []
            for m_id in members:
                member = ctx.guild.get_member(m_id)
                member_mentions.append(member.mention if member else f"<@{m_id}>")
            embed.add_field(
                name=f"🏴‍☠️ Team {captain_name}",
                value="\n".join(member_mentions) if member_mentions else "*No members*",
                inline=False
            )

        # Add other game details
        embed.add_field(name="- 🗺️ Map", value=game.get("map", "Unknown"), inline=True)
        embed.add_field(name="- 🌎 Region", value=game.get("region", "Unknown"), inline=True)
        embed.add_field(name="- 🎮 Gamemode", value=game.get("gamemode", "Unknown"), inline=True)
        embed.add_field(name="- 🆔 Game ID", value=match_id, inline=True)

        await ctx.send(embed=embed)



# --- Start Game Phase ---
async def start_game(channel, map_name):
    """Begin the game phase and mark game as active."""
    view = GameInfoView(map_name)
    await channel.send("🏁 **Game Ready!**", view=view)

    # Get the current match ID
    match_id = registered_channels[channel.id].get("active_game")
    if match_id and match_id in games:
        games[match_id]["status"] = "active"
        games[match_id]["map"] = map_name
        save_data()

    # ✅ Clear queue after match setup
    queues[channel.id] = []
    save_data()



class GameInfoView(discord.ui.View):
    def __init__(self, map_name):
        super().__init__(timeout=None)
        self.map_name = map_name

    @discord.ui.button(label="Send Game Info", style=discord.ButtonStyle.success)
    async def send_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = GameInfoModal(self.map_name)
        await interaction.response.send_modal(modal)

class GameInfoModal(discord.ui.Modal, title="Send Game Info"):
    map_field = discord.ui.TextInput(label="Map", placeholder="Enter map name")
    code_field = discord.ui.TextInput(label="Game Code", placeholder="Enter game code")
    notes_field = discord.ui.TextInput(label="Notes", style=discord.TextStyle.paragraph, required=False)

    def __init__(self, map_name):
        super().__init__()
        self.map_field.default = map_name

    async def on_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(title="🎮 Game Info", color=discord.Color.green())
        embed.add_field(name="Map", value=self.map_field.value, inline=False)
        embed.add_field(name="Code", value=self.code_field.value, inline=False)
        if self.notes_field.value:
            embed.add_field(name="Notes", value=self.notes_field.value, inline=False)

        await interaction.response.send_message("✅ Info sent to all players!", ephemeral=True)

        # Send DM to every player in this game
        players = get_all_players(interaction.channel.id)
        for player_id in players:
            member = interaction.guild.get_member(player_id)
            if member:
                try:
                    await member.send(embed=embed)
                except discord.Forbidden:
                    pass  # can't DM this user

# --- Help Command ---
# --- Categorized Help Paginator ---
class HelpPaginator(View):
    def __init__(self, categories):
        super().__init__(timeout=180)
        self.categories = list(categories.items())  # list of tuples: (category_name, [commands])
        self.current_page = 0
        self.max_pages = len(self.categories)

        self.prev_button = Button(label="⬅️ Prev", style=discord.ButtonStyle.primary)
        self.next_button = Button(label="Next ➡️", style=discord.ButtonStyle.primary)

        self.prev_button.callback = self.prev_page
        self.next_button.callback = self.next_page

        self.add_item(self.prev_button)
        self.add_item(self.next_button)

    async def prev_page(self, interaction):
        self.current_page = (self.current_page - 1) % self.max_pages
        await interaction.response.edit_message(embed=self.get_embed())

    async def next_page(self, interaction):
        self.current_page = (self.current_page + 1) % self.max_pages
        await interaction.response.edit_message(embed=self.get_embed())

    def get_embed(self):
        category_name, cmds = self.categories[self.current_page]
        embed = Embed(
            title=f"📖 {category_name} (Page {self.current_page+1}/{self.max_pages})",
            description="all commands are lowercase — stay smooth 😎",
            color=discord.Color.dark_gray()
        )
        for cmd in cmds:
            embed.add_field(
                name=f"`={cmd.name}`",
                value=cmd.help or "No description",
                inline=False
            )
        embed.set_footer(text="developed with ❤️ by technozen")
        return embed

@bot.command(name="help", aliases=["h"])
async def help_command(ctx):
    """Paginated help command showing categorized commands."""
    # Define your categories
    categories = {
        "🎮 Queue & Games": [
            bot.get_command("queue"),
            bot.get_command("join"),
            bot.get_command("leave"),
            bot.get_command("endgame"),
            bot.get_command("sub"),
            bot.get_command("teams"),
            bot.get_command("gameslist"),
        ],
        "🏗️ Draft Phase": [
            bot.get_command("startdraft") if bot.get_command("startdraft") else None,
            bot.get_command("pick"),
            bot.get_command("lockteams") if bot.get_command("lockteams") else None,
            bot.get_command("forcestart")
        ],
        "🗳️ Voting & Misc": [
            bot.get_command("vote") if bot.get_command("vote") else None,
            bot.get_command("queueinfo") if bot.get_command("queueinfo") else None,
            bot.get_command("ping") if bot.get_command("ping") else None,
            bot.get_command("elobalance") if bot.get_command("elobalance") else None,
        ],
        "🛠️ Admin Commands": [
             bot.get_command("register"),
            bot.get_command("unregister"),
            bot.get_command("setup"),
            bot.get_command("forcejoin"),
            bot.get_command("forceleave"),
            bot.get_command("elo"),
            bot.get_command("setwinelo"),
            bot.get_command("winner")
        ]
    }

    # Remove None commands (in case some optional commands aren't implemented)
    categories = {k: [c for c in v if c] for k, v in categories.items()}

    paginator = HelpPaginator(categories)
    embed = paginator.get_embed()
    await ctx.send(embed=embed, view=paginator)

@bot.event
async def on_ready():
    load_data()
    print(f"✅ Logged in as {bot.user}")

    if not hasattr(bot, "_inactivity_tasks"):
        bot._inactivity_tasks = {}

    for channel_id in registered_channels.keys():
        channel = bot.get_channel(channel_id)
        if channel:
            existing_task = bot._inactivity_tasks.get(channel_id)
            if existing_task and not existing_task.done():
                existing_task.cancel()
            # use asyncio.create_task instead of bot.loop.create_task
            bot._inactivity_tasks[channel_id] = asyncio.create_task(check_inactivity(channel))


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Track user activity only in registered queue channels
    if message.channel.id in registered_channels:
        # use time.time() so it matches check_inactivity()
        last_active[message.author.id] = time.time()

    await bot.process_commands(message)


# --- Inactivity Checker ---
async def check_inactivity(channel):
    """Periodically check for inactive users and remove them (per-channel)."""
    await bot.wait_until_ready()
    while channel.id in registered_channels:
        queue = get_queue(channel)
        if not queue:
            await asyncio.sleep(60)
            continue

        now = time.time()
        timeout = timeouts.get(channel.id, INACTIVITY_LIMIT_DEFAULT)
        inactive = []
        previous_queue = queue.copy()

        for user_id in list(queue):
            last = last_active.get(user_id)
            if not last or now - last > timeout:
                inactive.append(user_id)

        for user_id in inactive:
            if user_id in queue:
                queue.remove(user_id)
                member = channel.guild.get_member(user_id)
                if member:
                    try:
                        await channel.send(
                            f"⌛ {member.mention} was removed from the queue for inactivity "
                            f"(**>{timeout // 60} min timeout**)."
                        )
                    except discord.HTTPException:
                        pass

        # If queue became empty after kicking people, remember what it used to have
        if not queue and previous_queue:
            last_queues[channel.id] = previous_queue

        if inactive:
            save_data()

        await asyncio.sleep(60)


# --- SetTimeOut Cmd ---
@commands.has_permissions(administrator=True)
@bot.command()
async def settimeout(ctx, seconds: int):
    """Set inactivity timeout for this channel (in seconds)."""
    if not is_registered(ctx):
        return await ctx.send("❌ This channel is not registered for queueing.")
    if seconds < 60:
        return await ctx.send("⚠️ Timeout must be at least 60 seconds.")

    # Update the timeout value
    timeouts[ctx.channel.id] = seconds
    save_data()

    await ctx.send(
        f"🕒 Inactivity timeout set to **{seconds // 60} minutes** "
        f"for this queue channel."
    )

    # Ensure the task dict exists
    if not hasattr(bot, "_inactivity_tasks"):
        bot._inactivity_tasks = {}

    # Cancel previous task if it exists
    task = bot._inactivity_tasks.get(ctx.channel.id)
    if task and not task.done():
        task.cancel()

    # Start a new inactivity task for this channel
    bot._inactivity_tasks[ctx.channel.id] = asyncio.create_task(check_inactivity(ctx.channel))


# (Removed the deprecated remove_inactive_from_queues() and bot.loop.create_task(...))

bot.run(TOKEN)
