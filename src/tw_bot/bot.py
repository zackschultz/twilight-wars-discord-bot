import asyncio
import functools
import time
from typing import Any, cast

import async_lru
import discord
import structlog
from discord import TextChannel, app_commands
from structlog.contextvars import bind_contextvars

from . import twilight_wars_client
from . import game_monitor

logger = structlog.get_logger(__name__)


class DiscordClient(discord.Client):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.tree = app_commands.CommandTree(self, fallback_to_global=True)
        self._init_commands()

    def _init_commands(self):
        self.tree.add_command(connect_game_command, override=True)
        self.tree.add_command(disconnect_command, override=True)
        self.tree.add_command(reconnect_command, override=True)
        self.tree.add_command(connect_player_command, override=True)
        self.tree.add_command(disconnect_player_command, override=True)
        self.tree.add_command(re_notify, override=True)

    async def on_ready(self):
        logger.info(f"Logged on as {self.user}!")
        logger.info("Syncing commands...")
        await self.tree.sync()
        logger.info("Commands synced!")
        logger.info("Loading previously stored montiors...")
        await load_stored_monitors(self)
        logger.info("Monitors loaded!")
        logger.info("Bot fully ready!")

    async def on_message(self, message):
        logger.info(f"Message from {message.author}: {message.content}")

    @async_lru.alru_cache(maxsize=1200, ttl=15 * 60)
    async def get_user_display_name(self, user_id: int | str):
        if isinstance(user_id, str):
            user_id = int(user_id)
        user = await asyncio.to_thread(self.get_user, user_id)
        if not user:
            raise ValueError(f"User ID could not be found: {user_id}")
        return user.display_name


class ConnectPlayerView(discord.ui.View):

    def __init__(
        self,
        players: list[twilight_wars_client.Player],
        monitor: game_monitor.TWGameMonitor,
        is_disconnect: bool = False,
    ):
        super().__init__(timeout=60)
        self.is_disconnect = is_disconnect
        self.monitor = monitor
        select_component: Any = self.children[0]
        select_component.options = [
            discord.SelectOption(label=p.username, value=p.uid) for p in players
        ]

    @discord.ui.select(placeholder="Choose a Player", custom_id="player_selection")
    async def on_select(
        self, interaction: discord.Interaction, choice: discord.ui.Select
    ):
        if not (channel_id := interaction.channel_id):
            await interaction.response.send_message(
                "Must be in a channel", ephemeral=True
            )
            return
        if not (monitor := monitors.get(channel_id)):
            await interaction.response.send_message(
                "Please connect game first", ephemeral=True
            )
            return
        if not (discord_user_id := interaction.user.id):
            await interaction.response.send_message(
                "Need to have global name", ephemeral=True
            )
            return
        if not choice.values or not (player_uid := choice.values[0]):
            await interaction.response.send_message(
                "Operation canceled", ephemeral=True
            )
            return
        player = await monitor.get_player(player_uid)
        channel = interaction.client.get_channel(channel_id)
        assert channel
        assert isinstance(channel, TextChannel)
        await interaction.response.send_message(
            f"You chose {player.username}", ephemeral=True
        )
        if self.is_disconnect:
            await monitor.unmap_player(player_uid)
            await channel.send(
                f"{player.username} will no longer generate notifications in this channel"
            )
        else:
            await monitor.map_player(player_uid, f"{discord_user_id}")
            await channel.send(
                f"<@{discord_user_id}> will now receive notifications for {player.username}",
                allowed_mentions=discord.AllowedMentions(users=False),
            )


monitors: dict[int, game_monitor.TWGameMonitor] = {}
"""Collection of all of the active monitors.

This is a mapping between discord channel ID and the monitor for that channel
"""


async def _clean_existing_monitor(channel_id):
    old_monitor = monitors.get(channel_id)
    if old_monitor:
        del monitors[channel_id]
        await old_monitor.finish()


@app_commands.command(
    name="connectgame",
    description="Connects the TwilightWars game to monitor in this channel",
)
async def connect_game_command(
    interaction: discord.Interaction,
    game_url: str,
    username: str,
    password: str,
):
    """Connects a new game to the specified channel.

    In order to hook up a websocked, the bot must run as a specific user. The
    password is not stored. In addition, the bot should only ever relay public
    information about the game
    """
    game_id = twilight_wars_client.parse_game_id(game_url)
    if not (channel_id := interaction.channel_id):
        await interaction.response.send_message(
            f"This command can only be run from a channel",
            ephemeral=True,
        )
        return
    client = cast(DiscordClient, interaction.client)
    bind_contextvars(channel_id=channel_id)
    await _clean_existing_monitor(channel_id)
    monitor = game_monitor.TWGameMonitor(
        channel_id=channel_id,
        game_id=game_id,
        owner_username=username,
        last_activity=time.time(),
    )
    logger.info(
        "Attempting login", game_url=game_url, game_id=game_id, username=username
    )
    await monitor.establish_session(password)
    await monitor.refresh_players()
    players = await monitor.get_players()
    logger.info("Session is valid and working", players=players)
    monitors[channel_id] = monitor
    monitor.listen_background(functools.partial(on_game_monitor_event, client))
    await interaction.response.send_message(f"Logged in", ephemeral=True)


@app_commands.command(
    name="disconnect",
    description="Disconnects any games from the current channel",
)
async def disconnect_command(interaction: discord.Interaction):
    """Completed de-configures the current channel, if applicable"""
    if not (channel_id := interaction.channel_id):
        await interaction.response.send_message(
            f"This command can only be run from a channel",
            ephemeral=True,
        )
        return
    b_logger = logger.bind(command="disconnect", channel_id=channel_id)
    if old_monitor := monitors.get(channel_id):
        b_logger.info("Cleaning up existing game monitor instance")
        del monitors[channel_id]
        await old_monitor.disconnect_completely()
        await interaction.response.send_message(
            f"Your game {'TODO: Real name'} has been disconnected from this channel",
            ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            f"There are no games connected to this channel", ephemeral=True
        )


@app_commands.command(
    name="reconnect",
    description="Connects the current TwilightWars game",
)
async def reconnect_command(
    interaction: discord.Interaction,
    password: str,
):
    """Used to regenerate a session if the connection is dropped.

    Since we don't want to actually store passwords, it's safer to force the
    user to re-enter their password if the connection is ever lost
    """
    ...


@app_commands.command(
    name="sync",
    description="Re-sync the current game state",
)
async def sync_command(interaction: discord.Interaction):
    """Completed de-configures the current channel, if applicable"""
    if not (channel_id := interaction.channel_id):
        await interaction.response.send_message(
            f"This command can only be run from a channel",
            ephemeral=True,
        )
        return
    if monitor := monitors.get(channel_id):
        await interaction.response.send_message(
            f"Synchronizing your game state...", ephemeral=True
        )
        client = cast(DiscordClient, interaction.client)
        await monitor.reconnect_listen_background(
            functools.partial(on_game_monitor_event, client)
        )
        await interaction.followup.send(f"Game state re-synchronized", ephemeral=True)
    else:
        await interaction.response.send_message(
            f"Please `/connectgame` first", ephemeral=True
        )


@app_commands.command(
    name="connectplayer",
    description="Starts sending you notifications for the selected player. "
    + "Each player can only notify a single user.",
)
async def connect_player_command(
    interaction: discord.Interaction,
):
    """Workflow for connecting a TW player uid to a discord user.

    This is a two-step process. First, the user indicates that they want to
    connect to a player. Next, they pick which player they want to listen to
    """
    if not (channel_id := interaction.channel_id):
        await interaction.response.send_message(
            f"This command can only be run from a channel", ephemeral=True
        )
        return
    monitor = monitors[channel_id]
    players = (await monitor.get_players()).players
    await interaction.response.send_message(
        "Please select which player you want to receive notifications for",
        view=ConnectPlayerView(players, monitor, is_disconnect=False),
        ephemeral=True,
    )


@app_commands.command(
    name="disconnectplayer",
    description="Stops sending notifications for the selected player",
)
async def disconnect_player_command(
    interaction: discord.Interaction,
):
    """Workflow for disconnecting a TW player's notifications.

    This is a two-step process. First, the user indicates that they want to
    disconnect a player. Then, they select which player should be disconnected.
    """
    if not (channel_id := interaction.channel_id):
        await interaction.response.send_message(
            f"This command can only be run from a channel", ephemeral=True
        )
        return
    monitor = monitors[channel_id]
    players = (await monitor.get_players()).players
    await interaction.response.send_message(
        "Please select a player to stop notifications for",
        view=ConnectPlayerView(players, monitor, is_disconnect=True),
        ephemeral=True,
    )


@app_commands.command(
    name="renotify",
    description="Posts current turn information, re-notifying the current player 🛎️",
)
async def re_notify(
    interaction: discord.Interaction,
):
    """Process to re post the current turn.

    This will check the current turn and re-post the turn details. If a user is
    configured to receive notifications for the current player, they will be
    tagged.

    The command has two main purposes. The first is to prod someone who might be
    taking too long to complete their turn. The second is help if the bot is
    somehow stuck.

    This is potentially ripe for abuse, but I'm going to assume that no one will
    be an absolute jerk about it. There are currently no limits on how often
    this can be used. It will indicate WHO trigger the ping, so if they are
    rude you can always shame them.
    """
    if not (channel_id := interaction.channel_id):
        await interaction.response.send_message(
            f"This command can only be run from a channel", ephemeral=True
        )
        return
    interaction.user.id
    monitor = monitors[channel_id]
    event = await monitor.tw_refresh_and_get_turn()
    if not event:
        await interaction.response.send_message(
            f"Unable to get the current turn information", ephemeral=True
        )
        return
    await on_game_monitor_event(
        client=cast(DiscordClient, interaction.client),
        monitor=monitor,
        event=event,
        originator=interaction.user,
    )
    return


def run(*, application_id: int, application_secret: str) -> None:
    """Runs the bot, blocking until the interrupted"""
    logger.info("Starting asyncio event loop")
    asyncio.run(
        run_async(application_id=application_id, application_secret=application_secret),
        debug=True,
    )
    logger.info("Bot run complete!")


async def run_async(*, application_id: int, application_secret: str) -> None:
    """Runs the bot, blocking until canceled or a Discord error is encountered."""
    intents = discord.Intents.default()
    intents.message_content = True
    client = DiscordClient(
        intents=intents,
        application_id=application_id,
    )
    try:
        logger.info("Bot starting async discord client")
        await client.start(application_secret)
    except asyncio.exceptions.CancelledError:
        logger.info("Discord bot client was canceled. Exiting.")
    finally:
        logger.info("Cleaning up before exit")
        # Clean up any existing monitors, closing out their connections
        cleanup_tasks: list[asyncio.Task] = []
        for cid in monitors:
            cleanup_tasks.append(asyncio.create_task(_clean_existing_monitor(cid)))
        logger.info("Waiting for monitor cleanup to finish", count=len(cleanup_tasks))
        await asyncio.gather(*cleanup_tasks)
        # Wait for any outstanding tasks to finish just in case.
        remaining_tasks = [
            t for t in asyncio.all_tasks() if t != asyncio.current_task()
        ]
        logger.info("Waiting for any remaining tasks", count=len(remaining_tasks))
        await asyncio.gather(*remaining_tasks)
    logger.info("Bot async main complete")


async def load_stored_monitors(client: DiscordClient):
    """Loads and starts all monitors that were previously stored to disk.

    This shouldn't be called until `client` is ready, otherwise it's possible
    that a callback will occur before it can be handled.

    This sleeps between each of the loaded monitors to avoid putting too much
    pressure trying to connect to all of the websockets simultaneously.
    """
    stored_monitors = await game_monitor.TWGameMonitor.all_from_storage()
    for monitor in stored_monitors:
        monitors[monitor.channel_id] = monitor
        monitor.listen_background(functools.partial(on_game_monitor_event, client))


async def on_game_monitor_event(
    client: DiscordClient,
    monitor: game_monitor.TWGameMonitor,
    event: game_monitor.GameMonitorEvent,
    originator: discord.User | discord.Member | None = None
):
    """Handles messages from the game monitor.

    This is meant to be combined with `functools.partial` to pass in as a
    callback to the monitor listen_background. Partial should be used to
    provide the callback with a discord client
    """
    b_logger = monitor.bound_logger
    b_logger.info("Bot received turn event", tw_event=event)
    channel = client.get_channel(monitor.channel_id)
    assert isinstance(channel, discord.channel.TextChannel)
    message_parts = [
        f"{event.active_player_handle} ({event.active_player_faction}),",
        "you're up:",
        f"[{event.instruction}](https://www.twilightwars.com/games/{event.game_id})",
    ]
    if originator:
        message_parts.append(f"(🛎️ from <@{originator.id}>)")
    await channel.send(
        " ".join(message_parts),
        suppress_embeds=True,
        allowed_mentions=discord.AllowedMentions(users=True),
    )
