import discord
from discord.ext import commands
from discord import app_commands, Interaction, Embed, ButtonStyle
from discord.ui import View, Button
import asyncio
import json
from datetime import datetime, timedelta, timezone
import random
from flask import Flask
from threading import Thread
import os

TOKEN = os.getenv("DISCORD_TOKEN")

app = Flask('')

@app.route('/')
def home():
    return "Duty Bot is running!"

def run():
    app.run(host='0.0.0.0', port=8080)

def keep_alive():
    t = Thread(target=run)
    t.start()

# --- Configuration ---
AUTHORIZED_MODS_FILE = "authorized_mods.json"
POINTS_FILE = "points.json"
ACTIVE_DUTIES = {}
REMINDER_TASKS = {}  # Track reminder tasks to prevent duplicates
MAX_DUTY_DURATION = timedelta(hours=12)

MOD_ROLE_ID = 1399148894566354985
ADMIN_ROLE_ID = MOD_ROLE_ID
LOG_CHANNEL_ID = 1399171018630889472

# --- Bot setup ---
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree
client = bot

# --- Logging Helper ---
def log_to_console(event_type, user=None, details=None):
    """Log events to console for debugging"""
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    log_message = f"[{timestamp}] {event_type}"
    
    if user:
        log_message += f" - User: {user} (ID: {user.id})"
    
    if details:
        for key, value in details.items():
            log_message += f" | {key}: {value}"
    
    print(log_message)

# --- File Handling ---
def load_authorized_mods():
    try:
        with open(AUTHORIZED_MODS_FILE, 'r') as f:
            data = json.load(f)
            log_to_console("SYSTEM", details={"Action": "Loaded authorized mods", "Count": len(data)})
            return data
    except FileNotFoundError:
        log_to_console("SYSTEM", details={"Action": "Created new authorized mods file"})
        return []

def save_authorized_mods(mods):
    with open(AUTHORIZED_MODS_FILE, 'w') as f:
        json.dump(mods, f)
    log_to_console("SYSTEM", details={"Action": "Saved authorized mods", "Count": len(mods)})

def load_points():
    try:
        with open(POINTS_FILE, 'r') as f:
            data = json.load(f)
            log_to_console("SYSTEM", details={"Action": "Loaded points data", "Users": len(data)})
            return data
    except FileNotFoundError:
        log_to_console("SYSTEM", details={"Action": "Created new points file"})
        return {}

def save_points(points):
    with open(POINTS_FILE, 'w') as f:
        json.dump(points, f)
    log_to_console("SYSTEM", details={"Action": "Saved points data", "Users": len(points)})

points = load_points()
authorized_mods = load_authorized_mods()

# --- Checks ---
def is_admin(interaction: Interaction):
    return any(role.id == ADMIN_ROLE_ID for role in interaction.user.roles) if hasattr(interaction.user, 'roles') else False

def is_authorized_mod(user_id: int):
    return user_id in authorized_mods

# --- Reminder View ---
class ReminderView(View):
    def __init__(self, user_id):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.responded = False

    @discord.ui.button(label="Continue Duty", style=ButtonStyle.blurple)
    async def continue_duty(self, interaction: Interaction, button: Button):
        if interaction.user.id != self.user_id:
            try:
                await interaction.response.send_message("You cannot respond to this duty.", ephemeral=True)
            except discord.errors.NotFound:
                pass  # Interaction already handled or expired
            return
        
        self.responded = True
        duty = ACTIVE_DUTIES.get(self.user_id)
        if duty:
            duty['last_continue'] = datetime.now(timezone.utc)
            duty['continues'] += 1
            
            log_to_console("DUTY_CONTINUED", interaction.user, {
                "Continue Count": duty['continues'],
                "Total Duration": str(datetime.now(timezone.utc) - duty['start_time'])[:-7]
            })
            
            await send_log_embed("Duty Continued", interaction.user, {
                "User": f"{interaction.user} ({interaction.user.id})",
                "Continue Time": datetime.now(timezone.utc).strftime('%A, %d %B %Y %H:%M %p'),
                "Continue Count": duty['continues'],
                "Total Duration": str(datetime.now(timezone.utc) - duty['start_time'])[:-7]
            })
        
        try:
            await interaction.response.send_message("Duty continued.", ephemeral=True)
        except discord.errors.NotFound:
            pass  # Interaction already handled or expired
        self.stop()

    @discord.ui.button(label="End Duty", style=ButtonStyle.danger)
    async def end_duty(self, interaction: Interaction, button: Button):
        if interaction.user.id != self.user_id:
            try:
                await interaction.response.send_message("You cannot end this duty.", ephemeral=True)
            except discord.errors.NotFound:
                pass  # Interaction already handled or expired
            return
        
        self.responded = True
        await end_duty_session(interaction.user, auto=False)
        try:
            await interaction.response.send_message("Duty ended.", ephemeral=True)
        except discord.errors.NotFound:
            pass  # Interaction already handled or expired
        self.stop()

    async def on_timeout(self):
        """Handle timeout when user doesn't respond to reminder"""
        log_to_console("REMINDER_TIMEOUT", details={"User ID": self.user_id, "Responded": self.responded, "In Active Duties": self.user_id in ACTIVE_DUTIES})
        
        if not self.responded and self.user_id in ACTIVE_DUTIES:
            user = ACTIVE_DUTIES[self.user_id]['user']
            log_to_console("DUTY_AUTO_ENDED", user, {"Reason": "No response to reminder", "Timeout": "2 minutes"})
            await end_duty_session(user, auto=True, reason="No response to reminder (2 minute timeout)")

# --- Log Helper ---
async def send_log_embed(title=None, user=None, fields=None, embed=None):
    """Send embed to log channel and print to console"""
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if not log_channel:
        try:
            log_channel = await bot.fetch_channel(LOG_CHANNEL_ID)
        except Exception as e:
            log_to_console("LOG_CHANNEL_FETCH_FAILED", details={"Error": str(e)})
            return

    if embed is None:
        embed = Embed(title=title, color=discord.Color.blue())
        if fields:
            for key, value in fields.items():
                embed.add_field(name=key, value=value, inline=False)

    # Log to console
    if user:
        log_to_console(title or "LOG_EVENT", user, fields)
    else:
        log_to_console(title or "LOG_EVENT", details=fields)

    try:
        if hasattr(log_channel, 'send'):
            await log_channel.send(embed=embed)
        else:
            log_to_console("LOG_CHANNEL_INVALID", details={"Channel Type": type(log_channel).__name__})
    except Exception as e:
        log_to_console("LOG_SEND_FAILED", details={"Error": str(e)})

# --- Duty Management ---
async def end_duty_session(user, auto=False, reason=None):
    """End a duty session and award points"""
    if user.id not in ACTIVE_DUTIES:
        return

    duty_data = ACTIVE_DUTIES[user.id]
    duration = datetime.now(timezone.utc) - duty_data["start_time"]
    
    # Calculate points (1 point per 4 minutes)
    total_minutes = int(duration.total_seconds() // 60)
    awarded_points = total_minutes // 4
    
    # Add points to user
    user_id_str = str(user.id)
    if user_id_str not in points:
        points[user_id_str] = 0
    points[user_id_str] += awarded_points
    save_points(points)

    # Clean up active duty and reminder task
    del ACTIVE_DUTIES[user.id]
    if user.id in REMINDER_TASKS:
        REMINDER_TASKS[user.id].cancel()
        del REMINDER_TASKS[user.id]

    # Create embed for logging
    embed_title = "Duty Auto-Ended" if auto else "Duty Ended"
    embed_color = discord.Color.orange() if auto else discord.Color.red()
    
    log_fields = {
        "User": f"{user} ({user.id})",
        "End Time": datetime.now(timezone.utc).strftime('%A, %d %B %Y %H:%M %p'),
        "Duration": str(duration)[:-7],
        "Points Earned": awarded_points,
        "Total Points": points[user_id_str],
        "Continues": duty_data['continues']
    }
    
    if auto and reason:
        log_fields["Auto-End Reason"] = reason

    # Send log to channel
    embed = Embed(title=embed_title, color=embed_color)
    for key, value in log_fields.items():
        embed.add_field(name=key, value=value, inline=False)
    
    await send_log_embed(embed=embed)

    # Send DM to user
    try:
        dm_embed = Embed(
            title="Duty Ended" if not auto else "Duty Auto-Ended",
            color=embed_color
        )
        dm_embed.add_field(name="Duration", value=str(duration)[:-7], inline=False)
        
        if auto and reason:
            dm_embed.add_field(name="Reason", value=reason, inline=False)
            dm_embed.description = "Your duty was automatically ended."
        else:
            dm_embed.description = "Thank you for your service!"

        await user.send(embed=dm_embed)
        log_to_console("DM_SENT", user, {"Type": "Duty End Notification"})
    except discord.Forbidden:
        log_to_console("DM_FAILED", user, {"Reason": "DMs disabled or blocked"})
    except Exception as e:
        log_to_console("DM_FAILED", user, {"Error": str(e)})

async def schedule_reminder(user):
    """Schedule reminder for duty"""
    while user.id in ACTIVE_DUTIES:
        try:
            # Wait for 20-30 minutes randomly
            wait_time = random.randint(1200, 1800)  # 20-30 minutes in seconds
            await asyncio.sleep(wait_time)
            
            if user.id not in ACTIVE_DUTIES:
                break
                
            duty_data = ACTIVE_DUTIES[user.id]
            current_duration = datetime.now(timezone.utc) - duty_data["start_time"]
            
            # Check if duty has exceeded maximum duration
            if current_duration >= MAX_DUTY_DURATION:
                log_to_console("DUTY_AUTO_ENDED", user, {"Reason": "Maximum duration exceeded"})
                await end_duty_session(user, auto=True, reason="Maximum duty duration (12 hours) exceeded")
                break
            
            # Send reminder
            embed = Embed(
                title="Duty Reminder",
                description=f"You have been on duty for {str(current_duration)[:-7]}. Please choose an option:",
                color=discord.Color.yellow()
            )
            embed.add_field(name="Current Duration", value=str(current_duration)[:-7], inline=False)
            embed.add_field(name="Continue Count", value=duty_data['continues'], inline=False)

            view = ReminderView(user.id)
            
            try:
                await user.send(embed=embed, view=view)
                log_to_console("REMINDER_SENT", user, {
                    "Duration": str(current_duration)[:-7],
                    "Continue Count": duty_data['continues']
                })
                
                # Also send to log channel
                await send_log_embed("Duty Reminder Sent", user, {
                    "User": f"{user} ({user.id})",
                    "Duration": str(current_duration)[:-7],
                    "Continue Count": duty_data['continues'],
                    "Time": datetime.now(timezone.utc).strftime('%A, %d %B %Y %H:%M %p')
                })
            except discord.Forbidden:
                log_to_console("REMINDER_FAILED", user, {"Reason": "DMs disabled"})
                # If we can't send DM, auto-end the duty
                await end_duty_session(user, auto=True, reason="Unable to send reminder (DMs disabled)")
                break
            except Exception as e:
                log_to_console("REMINDER_FAILED", user, {"Error": str(e)})
                break
                
        except asyncio.CancelledError:
            log_to_console("REMINDER_TASK_CANCELLED", user)
            break
        except Exception as e:
            log_to_console("REMINDER_ERROR", user, {"Error": str(e)})
            break


# --- Commands ---
@tree.command(name="addmod", description="Add a moderator who can use duty commands (Admin only)")
async def addmod(interaction: Interaction, user_id: str):
    if not is_admin(interaction):
        return await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
    
    try:
        uid = int(user_id)
        if uid not in authorized_mods:
            authorized_mods.append(uid)
            save_authorized_mods(authorized_mods)
            log_to_console("MOD_ADDED", interaction.user, {"Added User ID": uid})
            await interaction.response.send_message(f"User ID {uid} added as authorized mod.", ephemeral=True)
        else:
            await interaction.response.send_message(f"User ID {uid} is already authorized.", ephemeral=True)
    except ValueError:
        await interaction.response.send_message("Invalid user ID.", ephemeral=True)

@tree.command(name="removemod", description="Remove a moderator's duty command access (Admin only)")
async def removemod(interaction: Interaction, user_id: str):
    if not is_admin(interaction):
        return await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
    
    try:
        uid = int(user_id)
        if uid in authorized_mods:
            authorized_mods.remove(uid)
            save_authorized_mods(authorized_mods)
            log_to_console("MOD_REMOVED", interaction.user, {"Removed User ID": uid})
            await interaction.response.send_message(f"User ID {uid} removed from authorized mods.", ephemeral=True)
        else:
            await interaction.response.send_message(f"User ID {uid} is not in the list.", ephemeral=True)
    except ValueError:
        await interaction.response.send_message("Invalid user ID.", ephemeral=True)

@tree.command(name="viewmods", description="View all authorized moderator IDs (Admin only)")
async def viewmods(interaction: Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        return

    embed = Embed(title="Authorized Moderators", color=discord.Color.orange())
    if not authorized_mods:
        embed.description = "No moderators added yet."
    else:
        for mod_id in authorized_mods:
            try:
                user = await bot.fetch_user(mod_id)
                embed.add_field(name=f"{user}", value=f"ID: {mod_id}", inline=False)
            except:
                embed.add_field(name="Unknown User", value=f"ID: {mod_id}", inline=False)

    log_to_console("VIEWMODS_COMMAND", interaction.user, {"Mod Count": len(authorized_mods)})
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="viewduties", description="View all current active duties (Admin only)")
async def viewduties(interaction: Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
        return

    embed = discord.Embed(title="Active Duties", color=discord.Color.teal())
    if not ACTIVE_DUTIES:
        embed.description = "There are no active duties."
    else:
        for user_id, data in ACTIVE_DUTIES.items():
            embed.add_field(
                name=f"{data['user']} (ID: {user_id})",
                value=f"Start: {data['start_time'].strftime('%Y-%m-%d %H:%M:%S')}",
                inline=False
            )

    log_to_console("VIEWDUTIES_COMMAND", interaction.user, {"Active Duties": len(ACTIVE_DUTIES)})
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="dutystart", description="Start your duty shift and begin receiving reminders")
async def dutystart(interaction: Interaction):
    if not is_authorized_mod(interaction.user.id):
        try:
            await interaction.response.send_message("You are not authorized to start duty.", ephemeral=True)
        except discord.errors.NotFound:
            pass
        return

    if interaction.user.id in ACTIVE_DUTIES:
        try:
            await interaction.response.send_message("You are already on duty.", ephemeral=True)
        except discord.errors.NotFound:
            pass
        return

    try:
        await interaction.response.defer(ephemeral=True)
    except discord.errors.NotFound:
        return

    # Cancel any existing reminder task
    if interaction.user.id in REMINDER_TASKS:
        REMINDER_TASKS[interaction.user.id].cancel()
        del REMINDER_TASKS[interaction.user.id]
        log_to_console("REMINDER_TASK_CANCELLED", interaction.user, {"Reason": "Starting new duty"})

    now = datetime.now(timezone.utc)

    ACTIVE_DUTIES[interaction.user.id] = {
        "user": interaction.user,
        "start_time": now,
        "last_continue": now,
        "continues": 0
    }

    embed = Embed(
        title="Duty Started",
        description=f"{interaction.user.mention} started their duty shift.",
        color=discord.Color.green()
    )
    embed.add_field(name="User", value=interaction.user.name)
    embed.add_field(name="User ID", value=str(interaction.user.id))
    embed.add_field(name="Start Time", value=now.strftime('%A, %d %B %Y %H:%M %p'))

    await interaction.followup.send(embed=embed, ephemeral=True)

    await send_log_embed("Duty Started", interaction.user, {
        "User": f"{interaction.user} ({interaction.user.id})",
        "Start Time": now.strftime('%A, %d %B %Y %H:%M %p')
    })

    # Start reminder task
    task = asyncio.create_task(schedule_reminder(interaction.user))
    REMINDER_TASKS[interaction.user.id] = task
    log_to_console("REMINDER_TASK_STARTED", interaction.user)


@tree.command(name="endduty", description="End your current duty shift")
async def endduty(interaction: Interaction):
    await interaction.response.defer(ephemeral=True)  # 🔁 Defer immediately

    if interaction.user.id not in ACTIVE_DUTIES:
        return await interaction.followup.send("You are not on duty.", ephemeral=True)

    await end_duty_session(interaction.user, auto=False)
    await interaction.followup.send("Duty ended.", ephemeral=True)

@tree.command(name="total", description="View a user's total points")
async def total(interaction: Interaction, user_id: str):
    if not is_admin(interaction):
        return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
    
    try:
        uid = str(int(user_id))
        user_points = points.get(uid, 0)
        log_to_console("TOTAL_COMMAND", interaction.user, {"Queried User ID": uid, "Points": user_points})
        await interaction.response.send_message(f"<@{uid}> has **{user_points}** points.", ephemeral=True)
    except ValueError:
        await interaction.response.send_message("Invalid user ID.", ephemeral=True)

@tree.command(name="resetpoints", description="Reset all points (Admin only)")
async def resetpoints(interaction: Interaction):
    if not is_admin(interaction):
        return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
    
    old_count = len(points)
    points.clear()
    save_points(points)
    
    log_to_console("POINTS_RESET", interaction.user, {"Previous User Count": old_count})
    await interaction.response.send_message("All points have been reset.", ephemeral=True)

@tree.command(name="addpoints", description="Add points to a user (Admin only)")
async def addpoints(interaction: Interaction, user_id: str, points_to_add: int):
    if not is_admin(interaction):
        return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
    
    try:
        uid = str(int(user_id))
        
        # Validate points amount
        if points_to_add <= 0:
            return await interaction.response.send_message("Points must be a positive number.", ephemeral=True)
        
        # Add points to user
        if uid not in points:
            points[uid] = 0
        
        old_points = points[uid]
        points[uid] += points_to_add
        save_points(points)
        
        log_to_console("ADDPOINTS_COMMAND", interaction.user, {
            "Target User ID": uid, 
            "Points Added": points_to_add,
            "Previous Points": old_points,
            "New Total": points[uid]
        })
        
        await send_log_embed("Points Manually Added", interaction.user, {
            "Admin": f"{interaction.user} ({interaction.user.id})",
            "Target User": f"<@{uid}> ({uid})",
            "Points Added": points_to_add,
            "New Total": points[uid],
            "Time": datetime.now(timezone.utc).strftime('%A, %d %B %Y %H:%M %p')
        })
        
        await interaction.response.send_message(
            f"Added **{points_to_add}** points to <@{uid}>. New total: **{points[uid]}** points.", 
            ephemeral=True
        )
        
    except ValueError:
        await interaction.response.send_message("Invalid user ID.", ephemeral=True)

@tree.command(name="leaderboard", description="View the points leaderboard (Admin only)")
async def leaderboard(interaction: Interaction):
    if not is_admin(interaction):
        return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
    
    if not points:
        return await interaction.response.send_message("No points data available.", ephemeral=True)
    
    # Sort users by points (descending)
    sorted_users = sorted(points.items(), key=lambda x: x[1], reverse=True)
    
    embed = Embed(title="Points Leaderboard", color=discord.Color.gold())
    
    for i, (user_id, user_points) in enumerate(sorted_users[:10], 1):  # Top 10
        try:
            user = await bot.fetch_user(int(user_id))
            embed.add_field(
                name=f"{i}. {user.display_name}",
                value=f"{user_points} points",
                inline=False
            )
        except:
            embed.add_field(
                name=f"{i}. Unknown User",
                value=f"{user_points} points (ID: {user_id})",
                inline=False
            )
    
    log_to_console("LEADERBOARD_COMMAND", interaction.user, {"Total Users": len(points)})
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="forceend", description="Force end a user's duty (Admin only)")
async def forceend(interaction: Interaction, user_id: str):
    if not is_admin(interaction):
        return await interaction.response.send_message("You are not authorized to use this command.", ephemeral=True)
    
    try:
        uid = int(user_id)
        if uid not in ACTIVE_DUTIES:
            return await interaction.response.send_message("User is not on duty.", ephemeral=True)
        
        user = ACTIVE_DUTIES[uid]['user']
        await end_duty_session(user, auto=True, reason=f"Force ended by {interaction.user}")
        
        log_to_console("FORCE_END", interaction.user, {"Target User ID": uid})
        await interaction.response.send_message(f"Force ended duty for <@{uid}>.", ephemeral=True)
        
    except ValueError:
        await interaction.response.send_message("Invalid user ID.", ephemeral=True)

# --- Error Handling ---
@bot.event
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    """Handle application command errors"""
    log_to_console("COMMAND_ERROR", interaction.user if hasattr(interaction, 'user') else None, {
        "Command": interaction.command.name if interaction.command else "Unknown",
        "Error": str(error),
        "Error Type": type(error).__name__
    })
    
    if not interaction.response.is_done():
        await interaction.response.send_message(
            f"An error occurred: {str(error)}", 
            ephemeral=True
        )

# --- Events ---
@bot.event
async def on_ready():
    log_to_console("BOT_READY", details={"Bot User": str(bot.user), "Guild Count": len(bot.guilds)})
    
    try:
        synced = await tree.sync()
        log_to_console("COMMANDS_SYNCED", details={"Command Count": len(synced)})
    except Exception as e:
        log_to_console("COMMAND_SYNC_FAILED", details={"Error": str(e)})
    
    # Start the web server to keep the bot alive
    keep_alive()

# --- Main ---
if __name__ == "__main__":
    # Get bot token from environment variable
    TOKEN = os.getenv('DISCORD_TOKEN')
    if not TOKEN:
        print("ERROR: DISCORD_TOKEN environment variable not set")
        exit(1)
    
    try:
        bot.run(TOKEN)
    except discord.LoginFailure:
        print("ERROR: Invalid bot token")
    except Exception as e:
        print(f"ERROR: Failed to start bot: {e}")