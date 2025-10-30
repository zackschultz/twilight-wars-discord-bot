import asyncio
import base64
import functools
import json
import os
import pickle
import re
import time
from typing import Any, Awaitable, Callable

import attrs
import requests
import structlog
import websockets

from . import twilight_wars_client

logger = structlog.get_logger(__name__)


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
            # TODO: Discord user -> player mapping
            # TODO: Other settings
        )
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
        data = {
            "cookies": self._dump_cookies(),
            "cur_player_no": self.cur_player_no,
            "game_id": self.game_id,
            "last_activity": self.last_activity,
            "owner_username": self.owner_username,
            "player_to_discord": self.player_to_discord,
            # TODO: Other settings
        }
        b_logger.info("Saving monitor to disk")
        await asyncio.to_thread(_overwrite_file_with_json, storage_path, data)
        b_logger.info("Monitor config stored")

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
