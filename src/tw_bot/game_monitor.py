import asyncio
import base64
import functools
import json
import os
import pickle
import re
import time
import random
from typing import Any, Awaitable, Callable, Self

import attrs
import requests
import structlog

from . import twilight_wars_client

logger = structlog.get_logger(__name__)

WHISPER_CHAN_DEBOUNCE_SEC = 15 * 60
WHISPER_CHAN_MIN_NOTIFICATION_SEC = 30
WHISPER_CHAN_REESTABLISH_SEC = 2 * 24 * 60 * 60  # 2 days


@attrs.define(kw_only=True, frozen=True)
class ActivePlayerChangeEvent:
    """Published when the game monitor detects a change in active player.

    Clients should typically use this as an opportunity to notify the new
    player that it is their turn.
    """

    channel_id: int
    game_id: str
    active_player_no: int
    active_player: twilight_wars_client.Player
    active_player_discord: str | None
    instruction: str
    turn: twilight_wars_client.TWTurnStatus

    @property
    def active_player_handle(self) -> str:
        """Gets the active player's discord handle, if available"""
        if self.active_player_discord:
            return f"<@{self.active_player_discord}>"
        return self.active_player_username

    @property
    def active_player_username(self) -> str:
        return self.active_player.username

    @property
    def active_player_faction(self) -> str:
        return self.active_player.faction


GameMonitorEvent = ActivePlayerChangeEvent
"""TODO: In the future, include other event types"""


def _overwrite_file_with_json(file_path: str, data: Any) -> None:
    """Dumps `data` as json to `file_path`, overwriting it.

    This is a separate function to make it easier to schedule with
    `asyncio.to_thread`.
    """
    with open(file_path, "w") as f:
        json.dump(data, f)


@functools.cache
def _get_storage_root() -> str:
    """Gets the root for all Game Monitor config storage.

    This has a side effect of creating the directory if it does not yet exist.
    The function is cached to avoid performing this side effect multiple times.
    """
    storage_root_path = os.getenv("STORAGE_PATH")
    if not storage_root_path:
        storage_root_path = "./vol_bot"  # Local storage similar to docker bind mount
    os.makedirs(storage_root_path, exist_ok=True)
    return storage_root_path


@attrs.define(kw_only=True)
class WhisperChannel:
    channel_id: int
    """The discord channel acting as the whisper channel"""

    init_user: str | None = None
    """The user id of the user that established the channel (sent first message)"""

    participants: set[str] = attrs.field(factory=set)
    """Set of discord handles of participating users"""

    last_notified: float | None = None
    """The time.time() when last there was a notification about channel's activity."""

    last_activity: float | None = None
    """The time.time() when last there was activity in this channel"""

    new_message_count: int = 0
    """The number of messages sent on the channel since last notified"""

    new_messages_false_len: int = 0
    """The "length" of new messages.

    The actual length of messages isn't captured. This captures a deliberately
    random representation of the length of messages to give a sense of volume
    without indicating the actual length of messages sent
    """

    notify_cb: Callable[[WhisperChannel, bool], Awaitable[None]]
    """The callback invoked when it's time to notify about this"""

    _notify_task: asyncio.Task | None = None
    """The async-io task scheduled for the next notification"""

    _parent: "TWGameMonitor"

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        parent: "TWGameMonitor",
        notify_cb: Callable[[WhisperChannel, bool], Awaitable[None]],
    ) -> Self:
        whisper_channel = cls(
            channel_id=data["channel_id"],
            init_user=data.get("init_user"),
            participants=set(data.get("participants", [])),
            last_notified=data.get("last_notified"),
            last_activity=data.get("last_activity"),
            new_message_count=data.get("new_message_count", 0),
            new_messages_false_len=data.get("new_messages_false_len", 0),
            notify_cb=notify_cb,
            _parent=parent,
        )
        # If necessary, trigger an initial notification. This can happen if the
        # bot is restarted while there are outstanding notification tasks.
        if whisper_channel.new_message_count:
            asyncio.create_task(
                whisper_channel.notify_debounced(),
                name=f"bot.game_monitor.whisper_channel_init_notify({whisper_channel.channel_id})",
            )
        return whisper_channel

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "init_user": self.init_user,
            "participants": sorted(self.participants),
            "last_notified": self.last_notified,
            "last_activity": self.last_activity,
            "new_message_count": self.new_message_count,
            "new_messages_false_len": self.new_messages_false_len,
        }

    async def notify_debounced(self) -> None:
        # If future (debounced) notification task already scheduled, skip
        if self._notify_task:
            return
        # If never notified, notify
        if self.last_notified is None:
            await self.notify(False)
            return
        # If haven't notified in a long time, notify
        cur_time = time.time()
        time_since_notify = cur_time - self.last_notified
        if time_since_notify > WHISPER_CHAN_DEBOUNCE_SEC:
            await self.notify(False)
            return
        # Otherwise, schedule a notification task
        notification_time_delta = max(
            WHISPER_CHAN_DEBOUNCE_SEC - time_since_notify,
            WHISPER_CHAN_MIN_NOTIFICATION_SEC,
        )
        self._start_delayed_notify_task(notification_time_delta)

    def _start_delayed_notify_task(self, delay: float) -> None:
        if self._notify_task:
            logger.warning(
                "Starting delayed notify task, but a task is already configured",
                channel_id=self.channel_id,
                old_task=self._notify_task,
            )

        async def delayed_notify():
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return  # Cancelled, nothing to do
            await self.notify(False, from_task=True)

        self._notify_task = asyncio.create_task(
            delayed_notify(),
            name=f"bot.game_monitor.whisper_channel_notify({self.channel_id})",
        )

    async def notify(self, is_newly_established: bool, from_task: bool = False) -> None:
        if not from_task:
            self.cancel_notify_task_if_running()
        try:
            await self.notify_cb(self, is_newly_established)
        except Exception as exc:
            logger.exception(
                "Failed to execute whisper channel notification callback",
                whisper_channel_id=self.channel_id,
                bot_channel_id=self._parent.channel_id,
                exc_msg=str(exc),
            )
        else:
            self._reset_after_notify()

    def cancel_notify_task_if_running(self) -> None:
        old_notification_task = self._notify_task
        self._notify_task = None
        if old_notification_task:
            old_notification_task.cancel()

    def _reset_after_notify(self) -> None:
        self.last_notified = time.time()
        self.new_message_count = 0
        self.new_messages_false_len = 0

    async def register_message(
        self,
        sender: str,
        participants: set[str],
        message: str,
    ) -> None:
        # Newly established channels are handled slightly differently than new
        # activity on existing channels.
        is_newly_established = self._is_newly_established
        # Check if there is a change in the participants. If so, we assume that
        # the old channel is closed and a new one has been established
        if participants != self.participants:
            logger.info(
                "Whisper channel participants changed",
                channel_id=self.channel_id,
                old_participants=self.participants,
                new_participants=participants,
            )
            await self.notify(False)
            self.cancel_notify_task_if_running()
            is_newly_established = True
        # Update the internal state
        if is_newly_established:
            self.init_user = sender
            self.participants = set(participants)
        self.new_message_count += 1
        self.new_messages_false_len += self._get_false_len(message)
        self.last_activity = time.time()
        # Set up notifications for this message
        if is_newly_established:
            logger.info(
                "Incorporated message into newly established channel",
                channel_id=self.channel_id,
                whisper_channel=self,
            )
            await self.notify(True)
        else:
            logger.info(
                "Incorporated message into existing channel",
                channel_id=self.channel_id,
                whisper_channel=self,
            )
            await self.notify_debounced()

    @property
    def _is_newly_established(self) -> bool:
        """Defines if the NEXT message indicates a newly established channel.

        The result of this function is only valid before the message is
        incorporated into the current state. It should be checked before
        updated state to determine how the message should be handled.
        """
        return self.new_message_count == 0 and (
            self.last_notified is None
            or self.last_activity is None
            or time.time() - self.last_activity > WHISPER_CHAN_REESTABLISH_SEC
        )

    def _get_false_len(self, message: str) -> int:
        if len(message) < 8:
            return random.choice(range(5, 13))
        else:
            multi = 0.7 + (0.6 * random.random())  # 0.7x ~ 1.3x
            return int(len(message) * multi)


@attrs.define(kw_only=True)
class TWGameMonitor:
    channel_id: int
    game_id: str
    owner_username: str
    cur_player_no: int | None = None
    player_to_discord: dict[str, str] = {}
    """Maps TW player UID to the string-encoded Discord user ID.

    This uses strings instead of the native `int` for discord user ID to avoid
    any serialization issues. Also, when used to mention users, it does it as
    a string.
    """

    last_activity: float
    """The time.time() of the last time something happened.

    This is used as a way to time out games with no activity. If a game goes for
    a certain number of days with no activity, it is at risk of being removed
    from the monitor.
    """

    whisper_channels: dict[int, WhisperChannel] = attrs.field(factory=dict)

    _players: twilight_wars_client.PlayerCollection | None = None
    _session: twilight_wars_client.TWSession | None = None
    _stop_event: asyncio.Event = asyncio.Event()
    _listen_task: asyncio.Task | None = None
    _players_lock = asyncio.Lock()

    @classmethod
    def _get_storage_path(cls, channel_id: int) -> str:
        base_path = _get_storage_root()
        file_name = f"monitor_{channel_id}.json"
        return os.path.join(base_path, file_name)

    @classmethod
    async def all_from_storage(cls) -> "list[TWGameMonitor]":
        """Loads previously saved monitors from disk.

        This will load ALL monitors that were previously stored via their
        `write_to_disk` methods. The monitors will be initialized with most
        of their data but they will _not_ be started immediately.
        """
        channel_ids = await asyncio.to_thread(cls._get_stored_channel_ids_sync)
        monitors = []
        for channel_id in channel_ids:
            monitor = await TWGameMonitor.from_stored_file(channel_id)
            monitors.append(monitor)
        return monitors

    @classmethod
    def _get_stored_channel_ids_sync(cls) -> "list[int]":
        storage_root = _get_storage_root()
        channel_ids: list[int] = []
        for filename in os.listdir(storage_root):
            if m := re.fullmatch(r"monitor_(\d+).json", filename):
                channel_ids.append(int(m.group(1)))
        return channel_ids

    @classmethod
    async def from_stored_file(cls, channel_id: int) -> "TWGameMonitor":
        with open(cls._get_storage_path(channel_id), "r") as store_file:
            data = json.load(store_file)
        game_monitor = cls(
            channel_id=channel_id,
            cur_player_no=data.get("cur_player_no"),
            game_id=data["game_id"],
            last_activity=data.get("last_activity", time.time()),
            owner_username=data["owner_username"],
            player_to_discord=data.get("player_to_discord", {}),
        )
        for whisper_chan_data in data.get("whisper_channels", []):
            whisper_chan = WhisperChannel.from_dict(
                data=whisper_chan_data,
                parent=game_monitor,
                notify_cb=game_monitor._whisper_channel_notify,
            )
            game_monitor.whisper_channels[whisper_chan.channel_id] = whisper_chan

        game_monitor._load_cookies(data.get("cookies"))
        return game_monitor

    def _dump_cookies(self) -> str:
        if not self._session:
            return ""
        # Cookies has explicit pickle support. https://stackoverflow.com/a/13031628/703040
        return base64.b64encode(
            pickle.dumps(self._session.http_session.cookies)
        ).decode()

    def _load_cookies(self, value: str | None) -> None:
        if not value:
            return
        pickled_cookies = base64.b64decode(value)
        cookies = pickle.loads(pickled_cookies)
        if not self._session:
            self._session = twilight_wars_client.TWSession.new_session()
        self._session.http_session.cookies.update(cookies)

    async def _whisper_channel_notify(self, chan: WhisperChannel) -> None:
        """Called to post notifications about new whisper channel activity"""
        # TODO: Implement

        # For now, just log information about the whisper channel so that we
        # can make sure that it's working. Then we can connect it to actual
        # discord.
        logger.info(
            "Received whisper channel notification",
            whisper_channel=chan,
            bot_channel_id=self.channel_id,
        )
        raise NotImplementedError("TODO: Implement")

    @property
    def bound_logger(self) -> structlog.BoundLogger:
        return logger.bind(game_id=self.game_id, channel_id=self.channel_id)

    @property
    def session(self) -> twilight_wars_client.TWSession:
        if not self._session:
            raise RuntimeError("No connection established")
        return self._session

    async def write_to_disk(self):
        storage_path = self._get_storage_path(self.channel_id)
        b_logger = logger.bind(
            channel_id=self.channel_id,
            game_id=self.game_id,
            storage_path=storage_path,
        )
        data = self.to_dict()
        b_logger.info("Saving monitor to disk")
        await asyncio.to_thread(_overwrite_file_with_json, storage_path, data)
        b_logger.info("Monitor config stored")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cookies": self._dump_cookies(),
            "cur_player_no": self.cur_player_no,
            "game_id": self.game_id,
            "last_activity": self.last_activity,
            "owner_username": self.owner_username,
            "player_to_discord": self.player_to_discord,
            "whisper_channels": sorted(
                self.whisper_channels.values(), key=lambda v: v.channel_id
            ),
        }

    async def establish_session(self, password: str):
        b_logger = self.bound_logger
        b_logger.info("Establishing new connection", username=self.owner_username)
        self._session = await twilight_wars_client.get_session(
            self.owner_username, password
        )
        b_logger.info("Connection established")

    async def terminate_session(self):
        await twilight_wars_client.invalidate_session(self.owner_username)
        self._session = None

    async def disconnect_completely(self):
        """Stops this monitor and removes any trace of its configuration.

        This should be used only when a user wants to completely stop any
        monitoring taking place in the current channel.

        After calling this function, the monitor should NOT be used again, it
        will likely not work.
        """
        await self.finish()
        await asyncio.to_thread(os.remove, self._get_storage_path(self.channel_id))
        self.channel_id = -1
        self.game_id = ""
        self.owner_username = ""
        self._session = None

    async def test_connection(self):
        try:
            await twilight_wars_client.get_players(self.game_id, self.session)
        except requests.HTTPError:
            logger.warning("Failed to retriever players")
            await twilight_wars_client.invalidate_session(self.owner_username)
            raise

    async def refresh_players(self):
        async with self._players_lock:
            logger.info("Refreshing players", game_id=self.game_id)
            self._players = await twilight_wars_client.get_players(
                self.game_id, self.session, self._players
            )
            logger.info("Players refreshed", players=self._players)
            await self.write_to_disk()

    async def _refresh_players_init_only(self):
        async with self._players_lock:
            if self._players:
                return
            logger.info("Refreshing players (init)", game_id=self.game_id)
            self._players = await twilight_wars_client.get_players(
                self.game_id, self.session, self._players
            )
            logger.info("Players refreshed", players=self._players)
            await self.write_to_disk()

    async def get_players(self) -> twilight_wars_client.PlayerCollection:
        await self._refresh_players_init_only()
        assert self._players  # Guaranteed by `_refresh_players_init_only`
        return self._players

    async def get_player_by_number(self, player_no: int) -> twilight_wars_client.Player:
        players = await self.get_players()
        return players.get_player_by_number(player_no)

    async def get_player(self, player_uid: str) -> twilight_wars_client.Player:
        players = await self.get_players()
        return players.get_player_by_uid(player_uid)

    async def map_player(self, player_uid: str, discord_user_id: str) -> None:
        self.player_to_discord[player_uid] = discord_user_id
        await self.write_to_disk()

    async def unmap_player(self, player_uid: str) -> None:
        if player_uid in self.player_to_discord:
            del self.player_to_discord[player_uid]
        await self.write_to_disk()

    def listen_background(
        self,
        callback: "Callable[[TWGameMonitor, GameMonitorEvent], Awaitable[None]]",
    ) -> None:
        b_logger = self.bound_logger
        self._stop_event.clear()
        if self._listen_task:
            raise RuntimeError("Already listening to this monitor")
        listen_task = asyncio.create_task(
            twilight_wars_client.subscribe_to_changes(
                game_id=self.game_id,
                session=self.session,
                callback=functools.partial(self._tw_event_callback, callback),
                idle_callback=functools.partial(self._tw_event_idle_callback, callback),
            ),
            name=f"bot.game_monitor.listen_background({self.channel_id})",
        )
        self._listen_task = listen_task
        b_logger.info("Websocket listener task scheduled")

    async def reconnect_listen_background(
        self,
        callback: "Callable[[TWGameMonitor, GameMonitorEvent], Awaitable[None]]",
    ) -> None:
        b_logger = self.bound_logger
        b_logger.info("Restarting background listener")
        await self.finish()
        self.listen_background(callback)
        b_logger.info("Background listener restarted")

    async def _tw_event_callback(
        self,
        callback: "Callable[[TWGameMonitor, GameMonitorEvent], Awaitable[None]]",
        event: twilight_wars_client.NormalizedTWEvent,
    ) -> None:
        b_logger = logger.bind(game_id=self.game_id, channel_id=self.channel_id)
        b_logger.info("+++++ MONITOR RECEIVED EVENT", tw_event=event)
        # For now, nothing to do if there is no turn/active player
        if not (turn := event.turn_status):
            b_logger.info("Skipping game event: No turn", tw_event=event)
            return
        if not (active_player_no := turn.active_player_no):
            b_logger.info("Skipping game event: No active player", tw_event=event)
            return
        # Gather information about the previous player
        prev_player_no = self.cur_player_no
        if prev_player_no is None:
            prev_player_no = turn.previous_player_no
        # Since we don't currently do anything with Log Events, only call back
        # to the client if there's an actual change in active player
        if active_player_no != prev_player_no:
            # Generate the turn event
            turn_event = await self._tw_event_to_active_player_change_event(event)
            b_logger.info(
                "Game monitor event player information",
                monitor_cur_player_no=self.cur_player_no,
                previous_player_no=prev_player_no,
                active_player_no=active_player_no,
                active_player_username=turn_event.active_player_username,
                active_player_handle=turn_event.active_player_handle,
            )
            # Alert the client
            await callback(self, turn_event)
        # Update the known active player and update the persistent storage
        if active_player_no:
            b_logger.info(
                "Updating current player",
                cur_player_no=self.cur_player_no,
                active_player_no=active_player_no,
            )
            self.cur_player_no = active_player_no
        await self.write_to_disk()

    async def _tw_event_idle_callback(
        self,
        callback: "Callable[[TWGameMonitor, GameMonitorEvent], Awaitable[None]]",
    ) -> int | None:
        b_logger = logger.bind(game_id=self.game_id, channel_id=self.channel_id)
        b_logger.info("Received idle callback")
        # Attempt to get the current game summary
        try:
            summary = await twilight_wars_client.get_summary(self.game_id, self.session)
        except Exception as err:
            b_logger.exception(
                "Unable to get game summary",
                err_msg=str(err),
            )
            return None
        # Then generate a synthetic event
        try:
            turn = twilight_wars_client.TWTurnStatus.from_game_summary(summary)
            event = twilight_wars_client.NormalizedTWEvent(turn, [])
        except Exception as err:
            b_logger.exception(
                "Exception generating synthetic event",
                err_msg=str(err),
                summary=summary,
            )
            return None
        # Finally, process the event
        try:
            await self._tw_event_callback(callback, event)
        except Exception as err:
            b_logger.exception(
                "Exception handling synthetic event",
                err_msg=str(err),
                summary=summary,
                tw_event=event,
            )
            return None
        # Finally, return an idle "handle" that can be used to detect if
        # data is changing while the system is seemingly idle.
        return summary.gen

    async def tw_refresh_and_get_turn(self) -> ActivePlayerChangeEvent | None:
        b_logger = logger.bind(game_id=self.game_id, channel_id=self.channel_id)
        b_logger.info("Received re-notify request")
        # Attempt to get the current game summary
        try:
            summary = await twilight_wars_client.get_summary(self.game_id, self.session)
        except Exception as err:
            b_logger.exception(
                "Unable to get game summary",
                err_msg=str(err),
            )
            return None
        # Then generate a synthetic event
        try:
            turn = twilight_wars_client.TWTurnStatus.from_game_summary(summary)
            event = twilight_wars_client.NormalizedTWEvent(turn, [])
        except Exception as err:
            b_logger.exception(
                "Exception generating synthetic event",
                err_msg=str(err),
                summary=summary,
            )
            return None
        return await self._tw_event_to_active_player_change_event(event)

    async def _tw_event_to_active_player_change_event(
        self, event: twilight_wars_client.NormalizedTWEvent
    ) -> ActivePlayerChangeEvent:
        if not (turn := event.turn_status):
            raise ValueError("Player change event must have a turn")
        if not (active_player_no := turn.active_player_no):
            raise ValueError("Player change event requires an active player")
        active_player = await self.get_player_by_number(active_player_no)
        active_player_discord = self.player_to_discord.get(active_player.uid)
        return ActivePlayerChangeEvent(
            channel_id=self.channel_id,
            game_id=self.game_id,
            active_player_no=active_player_no,
            active_player=active_player,
            active_player_discord=active_player_discord,
            instruction=twilight_wars_client.get_step_instruction(turn.step, turn),
            turn=turn,
        )

    async def finish(self):
        """Cleans up the game monitor if anything is running.

        Waits until any resources are closed before returning.
        """
        if self._listen_task:
            listen_task = self._listen_task
            self._listen_task = None
            if not listen_task.done():
                listen_task.cancel()
            try:
                await listen_task
                _ = listen_task.result()  # Raises any exceptions in the task
            except asyncio.CancelledError:
                pass
