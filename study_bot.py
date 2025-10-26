"""
Discord Study Competition Bot - With Late Joiner Support
=========================================================
Features:
- Late joiners can participate and earn credit
- Credit adjusted based on join time (-x minutes if joined x minutes late)
- Slash commands, instant credit, @ mentions in spoilers

Setup:
1. pip install discord.py python-dotenv
2. Create .env file with DISCORD_TOKEN=your_token_here
3. python study_bot.py
"""

import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
from discord.ui import Select, View
import pickle
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
import json
from pathlib import Path
import shutil

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_TOKEN')
ANNOUNCEMENT_CHANNEL_ID = int(os.getenv('ANNOUNCEMENT_CHANNEL_ID', 0))

# Bot setup with app commands
intents = discord.Intents.all()
bot = commands.Bot(command_prefix='!', intents=intents)

# Active sessions per channel
active_sessions = {}

# Organizations
ORGANIZATIONS = [
    "Wina", "VTK", "Atmosphere", "Chemica", "BIOS", 
    "Atlas", "Politeia", "VRG", "Ekonomika", "Medica",
]

# =============================================================================
# DATABASE
# =============================================================================

class FileSystemDB:
    def __init__(self, base_dir="database"):
        self.base_dir = Path(base_dir)
        self.setup_directories()

    def setup_directories(self):
        directories = [
            self.base_dir,
            self.base_dir / "study_sessions",
            self.base_dir / "monthly_stats",
            self.base_dir / "users",
            self.base_dir / "organizations",
            self.base_dir / "archived"
        ]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)

    async def save_study_session(self, session_data):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{session_data['year']}-{session_data['month']:02d}-{session_data['session_start'].day:02d}_{session_data['user_id']}_{timestamp}.pkl"
        filepath = self.base_dir / "study_sessions" / filename

        try:
            with open(filepath, 'wb') as f:
                pickle.dump(session_data, f)
            return True
        except Exception as e:
            print(f"Error saving session: {e}")
            return False

    async def get_user_sessions(self, user_id, year=None, month=None):
        sessions = []
        session_dir = self.base_dir / "study_sessions"

        for file_path in session_dir.glob("*.pkl"):
            if f"_{user_id}_" in file_path.name:
                if year and month:
                    date_prefix = f"{year}-{month:02d}"
                    if not file_path.name.startswith(date_prefix):
                        continue
                try:
                    with open(file_path, 'rb') as f:
                        sessions.append(pickle.load(f))
                except Exception as e:
                    print(f"Error reading session: {e}")
        return sessions

    async def get_monthly_stats(self, month_id):
        filepath = self.base_dir / "monthly_stats" / f"{month_id}.pkl"

        if not filepath.exists():
            return {
                "_id": month_id,
                "organizations": {org: {"total_minutes": 0} for org in ORGANIZATIONS},
                "top_individuals": [],
                "last_updated": datetime.now()
            }

        try:
            with open(filepath, 'rb') as f:
                return pickle.load(f)
        except Exception as e:
            print(f"Error reading stats: {e}")
            return None

    async def save_monthly_stats(self, month_id, stats_data):
        filepath = self.base_dir / "monthly_stats" / f"{month_id}.pkl"
        stats_data["last_updated"] = datetime.now()

        try:
            with open(filepath, 'wb') as f:
                pickle.dump(stats_data, f)
            return True
        except Exception as e:
            print(f"Error saving stats: {e}")
            return False

    async def update_monthly_stats(self, org, minutes, user_id, username):
        month_id = datetime.now().strftime("%Y-%m")
        stats = await self.get_monthly_stats(month_id)

        if not stats:
            return False

        if org not in stats["organizations"]:
            stats["organizations"][org] = {"total_minutes": 0}

        stats["organizations"][org]["total_minutes"] += minutes
        user_total = await self.get_user_monthly_total(user_id, month_id)

        stats["top_individuals"] = [
            ind for ind in stats["top_individuals"] 
            if ind["user_id"] != str(user_id)
        ]

        stats["top_individuals"].append({
            "user_id": str(user_id),
            "username": username,
            "org": org,
            "minutes": user_total
        })

        stats["top_individuals"].sort(key=lambda x: x["minutes"], reverse=True)
        stats["top_individuals"] = stats["top_individuals"][:100]

        return await self.save_monthly_stats(month_id, stats)

    async def get_user_monthly_total(self, user_id, month_id):
        year, month = month_id.split("-")
        sessions = await self.get_user_sessions(user_id, int(year), int(month))
        return sum(session.get("duration_minutes", 0) for session in sessions)

    async def save_user_data(self, user_id, user_data):
        filepath = self.base_dir / "users" / f"{user_id}.pkl"
        user_data["last_updated"] = datetime.now()

        try:
            with open(filepath, 'wb') as f:
                pickle.dump(user_data, f)
            return True
        except Exception as e:
            print(f"Error saving user: {e}")
            return False

    def get_database_stats(self):
        return {
            "total_sessions": len(list((self.base_dir / "study_sessions").glob("*.pkl"))),
            "total_users": len(list((self.base_dir / "users").glob("*.pkl"))),
            "disk_usage_mb": self.get_directory_size() / (1024 * 1024)
        }

    def get_directory_size(self):
        total = 0
        for dirpath, dirnames, filenames in os.walk(self.base_dir):
            for filename in filenames:
                total += os.path.getsize(os.path.join(dirpath, filename))
        return total

db = FileSystemDB()

# =============================================================================
# ORGANIZATION SELECTION
# =============================================================================

class OrganizationSelect(Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=org, description=f"Join {org}", emoji="📚")
            for org in ORGANIZATIONS
        ]
        super().__init__(placeholder="Select your organization...", options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_org = self.values[0]
        role = discord.utils.get(interaction.guild.roles, name=selected_org)
        if not role:
            role = await interaction.guild.create_role(name=selected_org)

        for org in ORGANIZATIONS:
            old_role = discord.utils.get(interaction.guild.roles, name=org)
            if old_role and old_role in interaction.user.roles:
                await interaction.user.remove_roles(old_role)

        await interaction.user.add_roles(role)

        await db.save_user_data(interaction.user.id, {
            "user_id": str(interaction.user.id),
            "username": interaction.user.name,
            "organization": selected_org,
            "joined_date": datetime.now()
        })

        await interaction.response.send_message(f"✅ Joined **{selected_org}**!", ephemeral=True)

class OrganizationView(View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(OrganizationSelect())

# =============================================================================
# POMODORO SESSION WITH LATE JOINER SUPPORT
# =============================================================================

class PomodoroSession:
    """Session that supports late joiners with adjusted credit"""

    def __init__(self, channel, interaction, duration=25, break_time=5):
        self.channel = channel
        self.interaction = interaction
        self.duration = duration
        self.break_time = break_time
        self.participants = {}  # {user_id: {'user': Member, 'join_time': datetime, ...}}
        self.session_start = None
        self.is_running = True
        self.is_study_period = False

    async def start(self):
        """Start session and track all participants"""
        members = [m for m in self.channel.members if not m.bot]

        if not members:
            await self.interaction.followup.send("❌ Voice channel is empty!")
            return

        self.session_start = datetime.now()

        # Add initial participants
        for member in members:
            self.add_participant(member)

        # Create mention list
        mentions = " ".join([m.mention for m in members])

        embed = discord.Embed(
            title=f"🚀 Study Session Started in {self.channel.name}!",
            description=f"**{len(members)} students** are studying!",
            color=discord.Color.green()
        )
        embed.add_field(name="📚 Study Time", value=f"{self.duration} min", inline=True)
        embed.add_field(name="☕ Break Time", value=f"{self.break_time} min", inline=True)
        embed.add_field(
            name="ℹ️ Late Joiners Welcome!",
            value=f"• Join anytime during the session\n"
                  f"• Late joiners get credit for time they participate\n"
                  f"• Example: Join 10 min late = get {self.duration-10} min credit\n"
                  f"• Not muted - talk freely! 🎤",
            inline=False
        )

        await self.interaction.followup.send(f"||{mentions}||", embed=embed)

        # Start monitoring for late joiners
        monitor_task = asyncio.create_task(self.monitor_late_joiners())

        # Run study and break
        await self.study_period()
        await self.award_credit()
        await self.break_period()

        # Stop monitoring
        self.is_running = False
        monitor_task.cancel()

    def add_participant(self, member):
        """Add a participant (initial or late joiner)"""
        if member.id not in self.participants:
            org = self.get_user_org(member)
            join_time = datetime.now()

            self.participants[member.id] = {
                'user': member,
                'organization': org,
                'join_time': join_time,
                'late_join': join_time > self.session_start
            }

    async def monitor_late_joiners(self):
        """Monitor voice channel for late joiners during study period"""
        try:
            while self.is_running and self.is_study_period:
                await asyncio.sleep(10)  # Check every 10 seconds

                current_members = [m for m in self.channel.members if not m.bot]

                for member in current_members:
                    if member.id not in self.participants:
                        # New late joiner!
                        self.add_participant(member)

                        # Calculate how late they are
                        minutes_late = int((datetime.now() - self.session_start).total_seconds() / 60)
                        potential_credit = max(0, self.duration - minutes_late)

                        # Notify them
                        embed = discord.Embed(
                            title=f"👋 Welcome, {member.display_name}!",
                            description=f"You joined {minutes_late} minute(s) into the session",
                            color=discord.Color.blue()
                        )
                        embed.add_field(
                            name="⏱️ Potential Credit",
                            value=f"Stay until study ends to earn **{potential_credit} minutes**\n"
                                  f"(Full session: {self.duration} min - {minutes_late} min late)",
                            inline=False
                        )

                        await self.interaction.channel.send(
                            f"||{member.mention}||",
                            embed=embed
                        )
        except asyncio.CancelledError:
            pass

    async def study_period(self):
        """Study period (no muting)"""
        self.is_study_period = True

        timer_embed = discord.Embed(
            title=f"📚 Study Session - {self.channel.name}",
            description="💬 Chat freely! Late joiners still welcome!",
            color=discord.Color.red()
        )

        timer_msg = await self.interaction.channel.send(embed=timer_embed)

        total_sec = self.duration * 60
        update_int = 30

        for elapsed in range(0, total_sec, update_int):
            if not self.is_running:
                break

            remaining = total_sec - elapsed
            mins = remaining // 60
            secs = remaining % 60

            current_count = len([m for m in self.channel.members if not m.bot])

            progress = elapsed / total_sec
            bar_len = 20
            filled = int(bar_len * progress)
            bar = "█" * filled + "░" * (bar_len - filled)

            timer_embed.clear_fields()
            timer_embed.add_field(name="⏱️ Time", value=f"**{mins}:{secs:02d}**", inline=True)
            timer_embed.add_field(name=f"👥 In {self.channel.name}", value=f"{current_count} students", inline=True)
            timer_embed.add_field(name="📊 Progress", value=f"{bar} {int(progress*100)}%", inline=False)
            timer_embed.set_footer(text="Late joiners welcome! Join now to earn partial credit")

            try:
                await timer_msg.edit(embed=timer_embed)
            except:
                pass

            await asyncio.sleep(min(update_int, remaining))

        self.is_study_period = False

        if self.is_running:
            timer_embed.color = discord.Color.green()
            timer_embed.title = f"✅ Study Complete - {self.channel.name}!"
            timer_embed.set_footer(text="Calculating credit for all participants...")
            try:
                await timer_msg.edit(embed=timer_embed)
            except:
                pass

    async def award_credit(self):
        """Award credit based on participation time"""
        if not self.is_running:
            return

        current = {m.id: m for m in self.channel.members if not m.bot}
        study_end = datetime.now()

        credit_details = []
        awarded_users = []
        left_early = 0

        for user_id, data in self.participants.items():
            if user_id in current:
                # User stayed until end of study period
                awarded_users.append(data['user'])

                # Calculate actual time participated
                time_in_session = (study_end - data['join_time']).total_seconds() / 60
                minutes_to_award = min(int(time_in_session), self.duration)

                # For late joiners, cap at remaining time when they joined
                if data['late_join']:
                    session_elapsed = (data['join_time'] - self.session_start).total_seconds() / 60
                    max_possible = max(0, self.duration - int(session_elapsed))
                    minutes_to_award = min(minutes_to_award, max_possible)

                if minutes_to_award > 0:
                    # Save to database
                    session_doc = {
                        "user_id": str(user_id),
                        "username": data['user'].name,
                        "organization": data['organization'],
                        "session_start": data['join_time'],
                        "session_end": study_end,
                        "duration_minutes": minutes_to_award,
                        "month": datetime.now().month,
                        "year": datetime.now().year,
                        "channel_name": self.channel.name,
                        "late_join": data['late_join'],
                        "completed": True
                    }

                    await db.save_study_session(session_doc)
                    await db.update_monthly_stats(
                        data['organization'],
                        minutes_to_award,
                        user_id,
                        data['user'].name
                    )

                    credit_details.append({
                        'user': data['user'],
                        'minutes': minutes_to_award,
                        'late': data['late_join']
                    })
            else:
                left_early += 1

        # Create credit summary
        if awarded_users:
            mentions = " ".join([u.mention for u in awarded_users])

            summary = discord.Embed(
                title=f"💰 Credit Awarded - {self.channel.name}",
                color=discord.Color.gold()
            )

            # Group by credit amount
            credit_text = ""
            for detail in sorted(credit_details, key=lambda x: x['minutes'], reverse=True):
                late_tag = " 🕐" if detail['late'] else ""
                credit_text += f"• {detail['user'].mention}: **{detail['minutes']} min**{late_tag}\n"

            summary.add_field(
                name="✅ Credit Awarded",
                value=credit_text or "No credit awarded",
                inline=False
            )

            if any(d['late'] for d in credit_details):
                summary.add_field(
                    name="🕐 Late Joiner Info",
                    value="Late joiners received adjusted credit based on participation time",
                    inline=False
                )

            if left_early > 0:
                summary.add_field(
                    name="⚠️ Left Early",
                    value=f"{left_early} students (no credit)",
                    inline=False
                )

            summary.add_field(
                name="☕ Break Starting",
                value=f"{self.break_time} minute break begins now!",
                inline=False
            )

            await self.interaction.channel.send(f"||{mentions}||", embed=summary)

    async def break_period(self):
        """Break period"""
        if not self.is_running:
            return

        current_members = [m for m in self.channel.members if not m.bot]
        mentions = " ".join([m.mention for m in current_members]) if current_members else ""

        break_embed = discord.Embed(
            title=f"☕ Break Time - {self.channel.name}!",
            description="Relax and chat!",
            color=discord.Color.blue()
        )

        break_msg = await self.interaction.channel.send(
            f"||{mentions}||" if mentions else "",
            embed=break_embed
        )

        total_sec = self.break_time * 60
        update_int = 15

        for elapsed in range(0, total_sec, update_int):
            if not self.is_running:
                break

            remaining = total_sec - elapsed
            mins = remaining // 60
            secs = remaining % 60

            progress = elapsed / total_sec
            bar_len = 20
            filled = int(bar_len * progress)
            bar = "█" * filled + "░" * (bar_len - filled)

            break_embed.clear_fields()
            break_embed.add_field(name="⏱️ Break", value=f"**{mins}:{secs:02d}**", inline=True)
            break_embed.add_field(name="📊 Progress", value=f"{bar} {int(progress*100)}%", inline=False)

            try:
                await break_msg.edit(embed=break_embed)
            except:
                pass

            await asyncio.sleep(min(update_int, remaining))

        if self.is_running:
            final_members = [m for m in self.channel.members if not m.bot]
            final_mentions = " ".join([m.mention for m in final_members]) if final_members else ""

            break_embed.color = discord.Color.gold()
            break_embed.title = f"🎉 Session Complete - {self.channel.name}!"
            break_embed.description = "Great work everyone!"

            try:
                await break_msg.edit(embed=break_embed)
                if final_mentions:
                    await self.interaction.channel.send(f"||{final_mentions}|| Session finished!")
            except:
                pass

    def get_user_org(self, member):
        for role in member.roles:
            if role.name in ORGANIZATIONS:
                return role.name
        return "None"

# =============================================================================
# SLASH COMMANDS
# =============================================================================

@bot.tree.command(name="setup", description="Choose your student organization")
async def setup_slash(interaction: discord.Interaction):
    """Setup organization selection"""
    view = OrganizationView()
    embed = discord.Embed(
        title="🎓 Choose Your Organization",
        description="Select your vereniging!",
        color=discord.Color.blue()
    )
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="pomodoro", description="Start a study session in your voice channel")
@app_commands.describe(
    study_minutes="Study duration in minutes (default: 25)",
    break_minutes="Break duration in minutes (default: 5)"
)
async def pomodoro_slash(
    interaction: discord.Interaction,
    study_minutes: int = 25,
    break_minutes: int = 5
):
    """Start pomodoro session - late joiners welcome!"""
    await interaction.response.defer()

    if not interaction.user.voice:
        await interaction.followup.send("❌ You must be in a voice channel!")
        return

    channel = interaction.user.voice.channel

    non_bot_members = [m for m in channel.members if not m.bot]
    if not non_bot_members:
        await interaction.followup.send(f"❌ **{channel.name}** is empty!")
        return

    if channel.id in active_sessions:
        await interaction.followup.send(f"⚠️ **{channel.name}** already has an active session!")
        return

    if study_minutes > 120 or study_minutes < 1:
        await interaction.followup.send("⚠️ Study time must be 1-120 minutes!")
        return

    if break_minutes > 60 or break_minutes < 0:
        await interaction.followup.send("⚠️ Break time must be 0-60 minutes!")
        return

    session = PomodoroSession(channel, interaction, study_minutes, break_minutes)
    active_sessions[channel.id] = session

    try:
        await session.start()
    finally:
        if channel.id in active_sessions:
            del active_sessions[channel.id]

@bot.tree.command(name="sessions", description="View all active study sessions")
async def sessions_slash(interaction: discord.Interaction):
    """Show active sessions"""
    if not active_sessions:
        await interaction.response.send_message("📊 No active sessions!")
        return

    embed = discord.Embed(
        title="📚 Active Study Sessions",
        description=f"{len(active_sessions)} session(s) in progress",
        color=discord.Color.blue()
    )

    for channel_id, session in active_sessions.items():
        channel = bot.get_channel(channel_id)
        if channel:
            members = [m for m in channel.members if not m.bot]
            embed.add_field(
                name=f"🔊 {channel.name}",
                value=f"• {len(members)} students\n"
                      f"• {session.duration}min study + {session.break_time}min break\n"
                      f"• Late joiners welcome!",
                inline=False
            )

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="dashboard", description="View study competition leaderboard")
async def dashboard_slash(interaction: discord.Interaction):
    """Show leaderboard"""
    month_id = datetime.now().strftime("%Y-%m")
    stats = await db.get_monthly_stats(month_id)

    if not stats or not any(d["total_minutes"] > 0 for d in stats["organizations"].values()):
        await interaction.response.send_message("📊 No data yet! Start a session with /pomodoro")
        return

    embed = discord.Embed(
        title="📊 Study Competition",
        description=datetime.now().strftime('%B %Y'),
        color=discord.Color.gold()
    )

    sorted_orgs = sorted(
        stats['organizations'].items(),
        key=lambda x: x[1]["total_minutes"],
        reverse=True
    )

    leaderboard = ""
    medals = ["🥇", "🥈", "🥉"]

    for idx, (org, data) in enumerate(sorted_orgs[:10]):
        if data['total_minutes'] > 0:
            hours = data['total_minutes'] // 60
            mins = data['total_minutes'] % 60
            medal = medals[idx] if idx < 3 else f"{idx+1}."
            leaderboard += f"{medal} **{org}**: {hours}h {mins}m\n"

    if leaderboard:
        embed.add_field(name="🏆 Organizations", value=leaderboard, inline=False)

    if stats['top_individuals']:
        top = "\n".join([
            f"{medals[i]} {s['username']} ({s['org']}): {s['minutes']//60}h {s['minutes']%60}m"
            for i, s in enumerate(stats['top_individuals'][:3])
            if s['minutes'] > 0
        ])
        if top:
            embed.add_field(name="⭐ Top Students", value=top, inline=False)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="stats", description="View database statistics")
async def stats_slash(interaction: discord.Interaction):
    """Database stats"""
    stats = db.get_database_stats()
    embed = discord.Embed(title="🗄️ Database Stats", color=discord.Color.blue())
    embed.add_field(
        name="Storage",
        value=f"Sessions: {stats['total_sessions']}\n"
              f"Users: {stats['total_users']}\n"
              f"Size: {stats['disk_usage_mb']:.2f} MB"
    )
    await interaction.response.send_message(embed=embed)

# =============================================================================
# BOT EVENTS
# =============================================================================

@bot.event
async def on_ready():
    print(f'\n✅ {bot.user} is online!')
    print(f'📊 Connected to {len(bot.guilds)} server(s)')
    print(f'🗄️  Database: {db.base_dir.absolute()}')

    try:
        synced = await bot.tree.sync()
        print(f'✅ Synced {len(synced)} slash command(s)')
    except Exception as e:
        print(f'❌ Failed to sync commands: {e}')

    print('\n⚡ FEATURES:')
    print('  • Late joiners supported with adjusted credit')
    print('  • Credit awarded after study period')
    print('  • Slash commands')
    print('  • @ mentions in spoilers')
    print('  • Per-channel timers')
    print('  • No muting')
    print('\nSlash Commands:')
    print('  /setup - Choose organization')
    print('  /pomodoro [study] [break] - Start session')
    print('  /sessions - View active sessions')
    print('  /dashboard - Leaderboard')
    print('  /stats - Database info')
    print('\nBot ready!\n')

@bot.event
async def on_command_error(ctx, error):
    print(f"Error: {error}")

# =============================================================================
# MAIN
# =============================================================================

if __name__ == '__main__':
    if not TOKEN:
        print("❌ ERROR: DISCORD_TOKEN not found!")
        print("\nCreate .env file with:")
        print("DISCORD_TOKEN=your_token_here")
        input("\nPress Enter to exit...")
    else:
        print("🚀 Starting Discord Study Bot...")
        print("📢 Late Joiner Support + Adjusted Credit")
        try:
            bot.run(TOKEN)
        except discord.LoginFailure:
            print("❌ ERROR: Invalid token!")
            input("\nPress Enter to exit...")
        except Exception as e:
            print(f"❌ ERROR: {e}")
            input("\nPress Enter to exit...")