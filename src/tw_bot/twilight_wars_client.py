import asyncio
import contextlib
import copy
import functools
import hashlib
import re
from typing import Any, AsyncIterator, Awaitable, Callable, Self

import attrs
import requests
import socketio
import socketio.exceptions
import structlog
from requests.adapters import HTTPAdapter, Retry

from .rate_limiter import AsyncioRateLimiter

logger = structlog.get_logger(__name__)

_LOGIN_URL = "https://www.twilightwars.com/login"
_PLAYERS_URL_FMT = "https://www.twilightwars.com/games/{game_id}/players"
_SUMMARY_URL_FMT = "https://www.twilightwars.com/games/{game_id}/summary"
_WEBSOCKET_URL_FMT = "wss://www.twilightwars.com/socket.io/?gameId={game_id}"

_KNOWN_SESSIONS_LOCK = asyncio.Lock()
_KNOWN_HASHES: "dict[str, str]" = {}
_KNOWN_SESSIONS: "dict[str, TWSession]" = {}

_GAME_ID_FROM_URL_REGEX = re.compile(
    r"(https://|http://)?www.twilightwars.com/games/([a-z0-9]+)", re.IGNORECASE
)
_GAME_ID_REGEX = re.compile(r"[a-z0-9]+", re.IGNORECASE)

IDLE_DELAY_SEC_EXP_FACTOR = 2 * 60
IDLE_DELAY_SEC_BASE = (5 * 60) - IDLE_DELAY_SEC_EXP_FACTOR
IDLE_DELAY_SEC_MAX = 15 * 60

SUBSCRIBE_RECONNECT_DELAY_SEC = 5 * 60
"""Delay in the event that there's a connection error.

To keep connections as stable as possible, if there is a websocket error, we
want to retry. However, if there is a websocket error, it also implies that
there might be a brief service interruption. This delay will be used before
attempting to make a new connection
"""

_HTTP_REQUEST_RETRY = Retry(total=5, backoff_factor=1, status_forcelist=[503])
"""Retrier for HTTP request since the service can be a little unstable.

See https://stackoverflow.com/a/35636367/703040
"""

_tw_rate_limiter = AsyncioRateLimiter("TwilightWarsAPI", 0.2)
"""Avoid overwhelming Twilight Wars with concurrent calls.

This will be used rate limit most major calls to Twilight wars to no more than 5
per second.
"""


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


class _ForceReconnectError(RuntimeError):
    """Class that is used by the websocket message processor to force a full reconnect.

    There are some times when the websocket itself seems to think that it's
    working correctly even though it isn't. If the websocket message handler
    gets into a state where it notices that the game summary is changed but
    it is _not_ receiving any messages, it can raise this error to force the
    websocket to perform a full reconnect
    """


@attrs.define
class TWSession:
    http_session: requests.Session

    @classmethod
    def new_session(cls) -> "TWSession":
        http_session = requests.Session()
        http_session.mount(
            "https://www.twilightwars.com/",
            HTTPAdapter(max_retries=_HTTP_REQUEST_RETRY),
        )
        return cls(http_session=http_session)


@attrs.define(kw_only=True, frozen=True)
class Player:
    uid: str
    gen: int
    trade_goods: int
    commodities: int
    number: int
    color: str
    faction: str
    user_id: str
    username: str
    # TODO: Need faction!

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Player":
        return cls(
            uid=data["_id"],
            gen=data["__v"],
            trade_goods=data["tradeGoods"],
            commodities=data["commodities"],
            number=data["number"],
            color=data["color"],
            faction=data["faction"],
            user_id=data["user"]["_id"],
            username=data["user"]["username"],
        )


@attrs.define(kw_only=True, frozen=True)
class PlayerCollection:
    etag: str
    players: list[Player]

    @classmethod
    def from_raw_list(cls, etag, data: list[dict[str, Any]]) -> "PlayerCollection":
        return cls(
            etag=etag,
            players=[Player.from_dict(p) for p in data],
        )

    def get_player_by_uid(self, uid: str) -> Player:
        players_by_uid = {p.uid: p for p in self.players}
        return players_by_uid[uid]

    def get_player_by_number(self, player_no: int) -> Player:
        players_by_player_number = {p.number: p for p in self.players}
        return players_by_player_number[player_no]

    def get_player_by_username(self, username: str) -> Player:
        players_by_username = {p.username: p for p in self.players}
        return players_by_username[username]


@attrs.define(kw_only=True, frozen=True)
class GameSummary:
    etag: str
    uid: str
    gen: int  # Maybe? I can't tell if this is the game state generation or not
    name: str
    step_name: str
    cur_player_no: int
    prev_player_no: int
    active_player_no: int | None

    raw: dict[str, Any]
    """The raw game summary data received from Twilight Wars.

    Direct use of this data should generally be avoided, instead preferring to
    elevate any useful fields onto TWTurnStatus instead.
    """

    @classmethod
    def from_dict(cls, etag, data: dict[str, Any]) -> "GameSummary":
        return cls(
            etag=etag,
            uid=data["_id"],
            gen=data["__v"],
            name=data["name"],
            step_name=data["step"],
            cur_player_no=data["turn"]["player"].get("current", -1),
            prev_player_no=data["turn"]["player"].get("previous", -1),
            active_player_no=data["turn"]["player"].get("active", None),
            raw=data,
        )


@attrs.define
class TWTurnStatus:
    round: int
    turn: int
    phase: str
    step: str
    previous_player_no: int
    active_player_no: int | None
    """The current player who needs to act.

    We use "active player" to mean something a little bit different than
    twilight wars itself. Our definition is "the player that the game is
    currently waiting on", which can include things like the player that is
    resolving an ability during a window.

    Twilight wars definition seems to be a little different and seems to only
    refer to a subset of those situations and uses other markers in special
    cases to indicate who needs to do something.

    (I had this comment from before, but I'm not sure why: "Active player is
    None during scoring"
    """

    is_ability_round: bool
    """Whether we are in an ability round in response to a trigger.

    This is flagged since it may be beneficial to keep these notifications
    private only to the recipient. Otherwise it may more readily call given
    players out as holding a sabotage.

    (Not that an astute player couldn't figure that out anyway, but there's no
    need for the bot to make it super obvious)
    """

    is_synthetic: bool
    """True if this was generated by a means other than a real turn event."""

    raw: Any
    """The raw event data received from Twilight Wars.

    Direct use of this data should generally be avoided, instead preferring to
    elevate any useful fields onto TWTurnStatus instead.
    """

    @staticmethod
    def _whos_turn_normal(raw_data: dict[str, Any]) -> int | None:
        active_player: int | None = None
        if "activePlayer" in raw_data:
            active_player = raw_data["activePlayer"]
        if "currentPlayer" in raw_data:
            active_player = raw_data["currentPlayer"]
        if "turn" in raw_data:
            turn_data = raw_data["turn"]
            if isinstance(turn_data, int):
                active_player = turn_data
            elif isinstance(turn_data, dict) and "current" in turn_data:
                active_player = turn_data["current"]
        if "subStepTurn" in raw_data:
            active_player = raw_data["subStepTurn"]
        return active_player

    @staticmethod
    def _whos_turn_ability_round(raw_data: dict[str, Any]) -> int | None:
        ability_round = raw_data["abilityRound"]
        active_player: int | None = None
        if "active" in ability_round:
            active_player = ability_round["active"]
        if "current" in ability_round:
            active_player = ability_round["current"]
        if "subStepTurn" in raw_data:
            active_player = raw_data["subStepTurn"]
        return active_player

    @classmethod
    def is_turn_status(cls, raw_data: Any) -> bool:
        return bool(
            raw_data
            and isinstance(raw_data, dict)
            and "phase" in raw_data
            and "step" in raw_data
            and "round" in raw_data
            and "turn" in raw_data
        )

    @classmethod
    def from_websocket_turn_event(cls, raw_data: dict[str, Any]) -> Self:

        # Handle special events for using abilities in response to a trigger
        if raw_data.get("abilityRound", {}).get("inProgress", False):
            return cls(
                round=raw_data["round"],
                turn=raw_data["turn"],
                phase=raw_data["phase"],
                step=f"Respond to: {raw_data["step"]}",
                previous_player_no=int(raw_data["abilityRound"]["previous"]),
                active_player_no=cls._whos_turn_ability_round(raw_data),
                is_ability_round=True,
                is_synthetic=False,
                raw=raw_data,
            )

        # Normal turn state
        return cls(
            round=raw_data["round"],
            turn=raw_data["turn"],
            phase=raw_data["phase"],
            step=raw_data["step"],
            previous_player_no=int(raw_data["previousPlayer"]),
            active_player_no=cls._whos_turn_normal(raw_data),
            is_ability_round=False,
            is_synthetic=False,
            raw=raw_data,
        )

    @classmethod
    def from_game_summary(cls, game_summary: GameSummary) -> Self:
        """Generate a synthetic turn status from a game summary

        This should be used to check the game status manually, in the event that
        a user wants to know the game status or that a turn message may have
        been missed.
        """
        # As far as I can tell, summary data has a very similar shape to turn
        # data - we just need to make a couple of very minor adjustments
        raw_turn = copy.deepcopy(game_summary.raw)
        turn_player = raw_turn.get("turn", {}).get("player", {})
        raw_turn["previousPlayer"] = turn_player["previous"]
        raw_turn["currentPlayer"] = turn_player["current"]
        if "active" in turn_player:
            raw_turn["activePlayer"] = turn_player["active"]
        # Delete some data that may leak info or is just too bulky. Use `pop`
        # instead of del to avoid key errors if something doesn't exist.
        raw_turn.pop("tradePlayers", None)  # Leaks?
        raw_turn.pop("transactions", None)  # Leaks?
        raw_turn.pop("imperialScores", None)  # Too much data
        raw_turn.pop("publicObjectives", None)  # Too much data
        raw_turn.pop("actionCards", None)  # Too much data
        raw_turn.pop("agendas", None)  # Too much data
        raw_turn.pop("frontierCards", None)  # Too much data
        raw_turn.pop("culturalExplorationCards", None)  # Too much data
        raw_turn.pop("hazardousExplorationCards", None)  # Too much data
        raw_turn.pop("industrialExplorationCards", None)  # Too much data
        raw_turn.pop("relics", None)  # Too much data
        # Generate the turn
        turn = cls.from_websocket_turn_event(raw_turn)
        return attrs.evolve(turn, is_synthetic=True)


@attrs.define
class TWLog:
    uid: str
    game_uid: str

    # There are a couple of instances where no user exists on the log, such
    # as trade requests or agendas (in those cases, user informatino can
    # be found in the relevant details object)
    user_uid: str | None

    name: str
    details: dict[str, Any]

    @classmethod
    def is_log_list(cls, raw_data: Any) -> bool:
        return bool(
            raw_data
            and isinstance(raw_data, list)
            and all("event" in v for v in raw_data)
            and all("details" in v for v in raw_data)
        )

    @classmethod
    def from_raw_list(cls, raw_data: Any) -> "list[TWLog]":
        logs: list[TWLog] = []
        for raw_log in raw_data:
            logs.append(
                TWLog(
                    uid=raw_log["_id"],
                    game_uid=raw_log["game"],
                    user_uid=raw_log.get("user", None),
                    name=raw_log["event"],
                    details=raw_log["details"],
                )
            )
        return logs


@attrs.define
class NormalizedTWEvent:
    turn_status: TWTurnStatus | None
    """The new turn status from this event.

    This may be None if the event did not have a parseable turn status
    """

    logs: list[TWLog]
    """The new list of logs for the event.

    This should be appended to the current game log. If the event contained no
    useful information, there will be no logs.
    """

    connection_established: bool = False
    """True if this is a special "connection established" event, False otherwise.

    In order to make sure that the game status is kept up-to-date during
    websocket connection downtimes, the socket will send a special event
    immediately upon establishing a new connection.
    """

    @classmethod
    def connected_event(cls) -> "NormalizedTWEvent":
        return NormalizedTWEvent(
            turn_status=None,
            logs=[],
            connection_established=True,
        )


class TWRawEventReceiver:
    queue: asyncio.Queue[NormalizedTWEvent]

    def __init__(self, queue):
        self.queue = queue

    def handle_event(self, event_name: str, *args: Any) -> None:
        """Handles an artibrary twilight wars socket io event.

        Remember that Socket IO events utilize arguments, so each event type
        may have different args. Since we only care about turn information and
        logs, we just go through all of the args to see if any look like they
        might be the bits of data taht we need.
        """
        b_logger = logger.bind(event_name=event_name)
        b_logger.info("Received event data", args=args)
        turn_status: TWTurnStatus | None = None
        logs: list[TWLog] = []
        for i, arg in enumerate(args):
            try:
                if TWLog.is_log_list(arg):
                    b_logger.info("Received log list argument")
                    logs = TWLog.from_raw_list(arg)
                elif TWTurnStatus.is_turn_status(arg):
                    b_logger.info("Received turn status argument")
                    turn_status = TWTurnStatus.from_websocket_turn_event(arg)
                else:
                    b_logger.info("Received unknown argument", i=i, arg=arg)
            except Exception:
                # Swallow parsing exceptions - we don't want a single unexpected
                # event to crash the game listener.
                b_logger.exception("Error event argument", i=i, arg=arg)
        if not turn_status and not logs:
            b_logger.warning("Event did not contain any useful information")
        self.queue.put_nowait(NormalizedTWEvent(turn_status=turn_status, logs=logs))


@functools.lru_cache(64)
def parse_game_id(game_id_or_url: str) -> str:
    if m := re.fullmatch(_GAME_ID_FROM_URL_REGEX, game_id_or_url):
        return m.group(2)
    if m := re.fullmatch(_GAME_ID_REGEX, game_id_or_url):
        return game_id_or_url
    raise ValueError(f"Unable to parse game ID / URL: {game_id_or_url}")


def get_user_hash(username: str, password: str) -> str:
    return hashlib.sha1(f"{username}:{password}".encode()).hexdigest()


async def get_session(username: str, password: str) -> TWSession:
    user_hash = get_user_hash(username, password)
    async with _KNOWN_SESSIONS_LOCK:
        if user_hash not in _KNOWN_SESSIONS:
            logger.info(
                "Establishing new session",
                login_url=_LOGIN_URL,
                username=username,
                user_hash=user_hash,
            )
            tw_session = TWSession.new_session()
            response = await asyncio.to_thread(
                tw_session.http_session.post,
                url=_LOGIN_URL,
                data={
                    "email": username,
                    "password": password,
                },
            )
            response.raise_for_status()
            logger.info(
                "New session established!",
                login_url=_LOGIN_URL,
                username=username,
                user_hash=user_hash,
            )
            _KNOWN_SESSIONS[user_hash] = tw_session
        _KNOWN_HASHES[username] = user_hash
        return _KNOWN_SESSIONS[user_hash]


async def invalidate_session(username: str) -> None:
    async with _KNOWN_SESSIONS_LOCK:
        if user_hash := _KNOWN_HASHES.get(username):
            del _KNOWN_HASHES[username]
        if user_hash and user_hash in _KNOWN_SESSIONS:
            del _KNOWN_SESSIONS[user_hash]


async def get_players(
    game_id: str, session: TWSession, current: PlayerCollection | None = None
) -> PlayerCollection:
    get_players_url = _PLAYERS_URL_FMT.format(game_id=game_id)
    req_session = session.http_session
    headers = {}
    if current and current.etag:
        headers["If-None-Match"] = current.etag
    logger.info("Getting players from API", etag=headers.get("If-None-Match"))
    await _tw_rate_limiter.wait()
    response = await asyncio.to_thread(
        req_session.get, url=get_players_url, headers=headers
    )
    response.raise_for_status()
    if current and response.status_code == 304:
        logger.info("Players unchanged")
        return current
    new_etag = response.headers.get("ETag")
    logger.info("New player information retrieved", etag=new_etag)
    return PlayerCollection.from_raw_list(new_etag, response.json())


async def get_summary(
    game_id: str, session: TWSession, current: GameSummary | None = None
) -> GameSummary:
    get_summary_url = _SUMMARY_URL_FMT.format(game_id=game_id)
    req_session = session.http_session
    headers = {}
    if current and current.etag:
        headers["If-None-Match"] = current.etag
    await _tw_rate_limiter.wait()
    response = await asyncio.to_thread(
        req_session.get, url=get_summary_url, headers=headers
    )
    response.raise_for_status()
    if current and response.status_code == 304:
        return current
    return GameSummary.from_dict(response.headers.get("ETag"), response.json())


def get_step_instruction(step_name: str, turn: TWTurnStatus) -> str:
    """Converts the step name into a human-friendly step instruction.

    If the provided `step_name` doesn't have a human-friendly instruction
    configured, it will be returned verbatim.
    """
    step_key = step_name.casefold()
    step_val = _STEP_NAME_TO_HUMAN_ACTION_TRANSLATION.get(step_key)
    if step_val is None:
        return step_name
    if callable(step_val):
        return step_val(step_name, turn)
    return step_val


async def subscribe_to_changes(
    game_id: str,
    session: TWSession,
    callback: Callable[[NormalizedTWEvent], Awaitable[None]],
    idle_callback: Callable[[], Awaitable[Any]],
):
    b_logger = logger.bind(game_id=game_id)
    while True:
        try:
            await _subscribe_to_changes_no_retries(
                game_id=game_id,
                session=session,
                callback=callback,
                idle_callback=idle_callback,
            )
        except asyncio.CancelledError:
            # If the task was cancelled, let it bubble normally.
            raise
        except socketio.exceptions.ConnectionError:
            b_logger.info("Subscribe encountered connection error")
        except Exception as err:
            b_logger.info(
                "Subscribe encountered fatal error, raising", err_msg=str(err)
            )
            raise
        else:
            # If `_subscribe_to_changes_no_retries` returns, that indicates a
            # graceful close of the subscription, so we just exit the function
            # normally.
            b_logger.info("Change subscription ended gracefully, returning")
            return
        # At this point, a graceful shutdown would ahve already returned, so we
        # know that we need to do some sort of back-off error
        # handling/reconnect
        b_logger.info(
            "Waiting before subscribe reconnect",
            delay_sec=SUBSCRIBE_RECONNECT_DELAY_SEC,
        )
        await asyncio.sleep(SUBSCRIBE_RECONNECT_DELAY_SEC)


async def _subscribe_to_changes_no_retries(
    game_id: str,
    session: TWSession,
    callback: Callable[[NormalizedTWEvent], Awaitable[None]],
    idle_callback: Callable[[], Awaitable[Any]],
):
    #
    # SOME THINGS TO CONSIDER
    #
    # 2. Reading the websocket data might be more trouble than its worth given
    #    that it seems to be in a specialized format. Instead, consider just
    #    using it as a trigger to grab the most recent summary information and
    #    just use that instead.
    # 3. Initially, don't stress too much. Just do it in the form of:
    #    `@usr (faction <color emoji>), it's your turn: <"step" name>`. Maybe
    #    even have step name be a link that opens the page
    #
    b_logger = logger.bind(game_id=game_id)
    message_queue: asyncio.Queue[NormalizedTWEvent] = asyncio.Queue()
    receiver = TWRawEventReceiver(message_queue)
    try:
        while True:
            await _tw_rate_limiter.wait()
            async with _tw_socket_connection(
                game_id=game_id,
                session=session,
                receiver=receiver,
            ):
                # Send an initial callback after the connection is established.
                # This gives the callback an option to check on the current
                # status of the game, potentially reconciling issues during
                # connection downtime
                await callback(NormalizedTWEvent.connected_event())
                # Runs until the socket is "stale" which should trigger a new
                # socket connection.
                await _process_queue_with_reconnect_timeout(
                    queue=message_queue,
                    callback=callback,
                    idle_callback=idle_callback,
                )
    except asyncio.CancelledError:
        b_logger.info("Socket.io task has been canceled")
        raise
    except Exception:
        b_logger.exception("Socket.io communication error!")
        raise


@contextlib.asynccontextmanager
async def _tw_socket_connection(
    game_id: str,
    session: TWSession,
    receiver: TWRawEventReceiver,
) -> AsyncIterator[socketio.asyncio_client.AsyncClient]:
    # First, call the players endpoint with the current session. This will
    # allow any session cookies to refresh if they've gotten stale. Note that
    # requests Session objects (session.http_session) are mutable.
    _ = await get_players(game_id, session)
    # Set up the client
    websocket_url = _WEBSOCKET_URL_FMT.format(game_id=game_id)
    cookie = "; ".join(
        f"{name}={value}" for name, value in session.http_session.cookies.items()
    )
    b_logger = logger.bind(game_id=game_id)
    sio = socketio.asyncio_client.AsyncClient(logger=b_logger, engineio_logger=b_logger)
    for msg_type in sorted(_MESSAGE_TYPES):
        sio.on(
            msg_type,
            functools.partial(receiver.handle_event, msg_type),
            namespace="/game",
        )
    try:
        b_logger.info(
            "Socket.io connecting...",
            websocket_url=websocket_url,
            cookie=repr(cookie),
        )
        await sio.connect(
            url=websocket_url,
            headers={
                "Cookie": cookie,
                "Origin": "https://www.twilightwars.com",
            },
        )
        b_logger.info(
            "Socket.io connection established",
            sid=sio.sid,
            base_url=sio.eio.base_url,
            full_url=sio.connection_url,
            namespaces=sio.connection_namespaces,
        )
        yield sio
    finally:
        try:
            await sio.disconnect()
            b_logger.info("Socket.io connection terminated")
        except Exception:  # pylint: disable=broad-except
            b_logger.exception("Socket.io failed to terminate connection")


async def _process_queue_with_reconnect_timeout(
    queue: asyncio.Queue[NormalizedTWEvent],
    callback: Callable[[NormalizedTWEvent], Awaitable[None]],
    idle_callback: Callable[[], Awaitable[Any]],
    reconnect_timeout: float = 2 * 60 * 60,
) -> None:
    """Listens to the queue for a pre-determined amount of time.

    Listens to the queue for `reconnect_timeout` seconds, at which point the
    function will return. If the function returns, the caller should use that
    opportunity to reconnect the socket and start listening again.

    The socket should be periodically refreshed to deal with stale session
    information that seems to sometimes happen. By forcing a reconnect, it
    should trigger a plain `GET` request which will re-up session tokens.

    To stop processing the queue, use asyncio's normal task cancel functions.
    """
    reconnect_deadline = asyncio.get_running_loop().time() + reconnect_timeout
    try:
        async with asyncio.timeout_at(reconnect_deadline):
            while True:
                # Blocks until canceled
                await _process_queue(
                    queue=queue,
                    callback=callback,
                    idle_callback=idle_callback,
                )
    except TimeoutError:
        logger.info("Canceled queued processing to allow for reconnect")
        return
    except _ForceReconnectError:
        logger.warning("Received forced reconnect error")
        return


async def _process_queue(
    queue: asyncio.Queue[NormalizedTWEvent],
    callback: Callable[[NormalizedTWEvent], Awaitable[None]],
    idle_callback: Callable[[], Awaitable[Any]],
) -> None:
    """Listens to the queue until canceled.

    If no message is received within `idle_timeout` seconds,
    `idle_callback` will be called. This is a way to manually check in
    on the status of a given game.

    `idle_callback` can also return a "handle" - if two consecutive idle
    callbacks return different, non-None handles, the assumption is that the
    websocket is missing events and is likely broken. This will trigger a full
    refresh of the socket.
    """
    consecutive_idles = 0
    last_idle_handle = None
    # If there are no messages to immediately process, start with one initial
    # idle callback to make sure that the initial state is correct.
    if queue.empty():
        next_idle_handle = await idle_callback()
    # Loop forever until canceled, processing messages and idle callbacks as
    # they occur
    while True:
        try:
            idle_timeout = min(
                ((2**consecutive_idles) * IDLE_DELAY_SEC_EXP_FACTOR)
                + IDLE_DELAY_SEC_BASE,
                IDLE_DELAY_SEC_MAX,
            )
            async with asyncio.timeout(idle_timeout):
                msg = await queue.get()
                consecutive_idles = 0
                last_idle_handle = None
                logger.info("++++++++ Received message", msg=msg)
                try:
                    await callback(msg)
                except Exception:
                    logger.exception("Message callback failed", msg=msg)
        except TimeoutError:
            logger.info("Idle timeout expired")
            consecutive_idles = min(consecutive_idles + 1, 31)
            next_idle_handle = await idle_callback()
            logger.info(
                "Idle callback finished",
                last_idle_handle=last_idle_handle,
                next_idle_handle=next_idle_handle,
            )
            if next_idle_handle is None:
                pass  # If idle callback returns None, it implies a no-op
            elif last_idle_handle is not None and last_idle_handle != next_idle_handle:
                logger.warning(
                    "Idle handle mismatch",
                    last_idle_handle=last_idle_handle,
                    next_idle_handle=next_idle_handle,
                )
                raise _ForceReconnectError("Mismatched idle handles encountered")
            else:
                last_idle_handle = next_idle_handle


def _resolve_secondary_ability_format(step_name: str, turn: TWTurnStatus) -> str:
    strategy = turn.raw.get("activeStrategyCard")
    if strategy:
        return f"Resolve Secondary Ability of {strategy}"
    return step_name


_STEP_NAME_TO_HUMAN_ACTION_TRANSLATION: dict[
    str, str | Callable[[str, TWTurnStatus], str]
] = {
    "End Turn": "End your turn",
    "Ground Combat Assign Hits": "Assign hits (ground combat)",
    "Ground Combat Roll Dice": "Roll dice (ground combat)",
    "Perform Action": "Perform an action",
    "Resolve Secondary Ability": _resolve_secondary_ability_format,
    "Respond to: Activate System": "Respond to: System activated",
    "Respond to: Ground Combat Assign Hits": "Respond to: Assign hits (ground combat)",
    "Respond to: Ground Combat Roll Dice": "Respond to: Roll dice (ground combat)",
    "Respond to: Space Combat Assign Hits": "Respond to: Assign hits (space combat)",
    "Respond to: Space Combat Roll Dice": "Respond to: Roll dice (space combat)",
    "Space Combat Assign Hits": "Assign hits (space combat)",
    "Space Combat Roll Dice": "Roll dice (space combat)",
}
"""Mapping between "step" and a human-friendly instruction.

This is used by the chatbot to give more human friendly instructions. Typically,
these will be in the form of something like:

```
"@foo, it is your turn: {action}"
```

This mapping is not guaranteed to be exhaustive. Some steps may not exist. In
that case, it is recommended to simply use the step name as-is.

Keys will automatically be case-folded.
"""

_STEP_NAME_TO_HUMAN_ACTION_TRANSLATION = {
    k.casefold(): v for k, v in _STEP_NAME_TO_HUMAN_ACTION_TRANSLATION.items()
}


_MESSAGE_TYPES = frozenset(
    [
        "ability round passed",
        "action card played",
        "action card sabotaged",
        "action cards added",
        "action cards discarded",
        "action cards removed",
        "ambush used",
        "announced retreats",
        "anti-fighter barrage hits assigned",
        "anti-fighter barrage rolled",
        "antivirus played",
        "arborec mech ability used",
        "arc secundus used",
        "barony mech ability used",
        "bio-stims played",
        "bioplasmosis played",
        "bombardment hits assigned",
        "bombardment rolled",
        "chaos mapping played",
        "command token removed",
        "command tokens redistributed",
        "commodities replenished",
        "component action card played",
        "construction rider resolved",
        "creuss iff played",
        "creuss mech ability used",
        "dark energy tap played",
        "devotion used",
        "diplomacy rider resolved",
        "diplomatic pressure resolved",
        "duha menaimon used",
        "ended turn",
        "enemy action cards received",
        "established control",
        "exotrireme II ability used",
        "exploration card resolved",
        "fires of the gashlai played",
        "fleet reduced",
        "foresight used",
        "galactic threat resolved",
        "galactic threat used",
        "game connection error",
        "gift of prescience played",
        "greyfire mutagen played",
        "ground combat hits assigned",
        "ground combat rolled",
        "ground combat started",
        "ground forces committed",
        "hacan mech ability resolved",
        "imperial arbiter played",
        "imperial arms vault played",
        "imperial rider resolved",
        "indoctrination used",
        "instinct training played",
        "lazax gate folding played",
        "leadership rider resolved",
        "legendary planet cards gained",
        "mageon implants played",
        "mageon implants resolved",
        "military support played",
        "minister of peace played",
        "minister of war played",
        "mitosis used",
        "munitions reserves used",
        "new agenda revealed",
        "nullification field played",
        "objective scored",
        "orbital drop used",
        "passed",
        "peace accords used",
        "pillage used",
        "planet cards gained",
        "planets exhausted",
        "planets readied",
        "player eliminated",
        "player law gained",
        "players to replenish commodities chosen",
        "political favor played",
        "politics rider resolved",
        "predictive intelligence played",
        "primary ability resolved",
        "production biomes played",
        "promise of protection played",
        "promissory note added",
        "promissory note played",
        "promissory note removed",
        "public objective revealed",
        "quash used",
        "reclamation used",
        "research agreement played",
        "research grant reallocation resolved",
        "retreated",
        "saar mech ability used",
        "salvage operations used",
        "second technology researched",
        "secondary ability resolved",
        "secret objective discarded",
        "secret objective drawn",
        "secret objective made public",
        "secret objective scored",
        "secret objectives dealt",
        "ship destroyed",
        "ships moved",
        "sling relay played",
        "sol mech ability resolved",
        "space cannon defense hits assigned",
        "space cannon defense planet chosen",
        "space cannon defense rolled",
        "space cannon offense hits assigned",
        "space cannon offense rolled",
        "space combat hits assigned",
        "space combat rolled",
        "spy net played",
        "spy net resolved",
        "stall tactics used",
        "star forge used",
        "starting secret objective discarded",
        "starting technology picked",
        "starting units placed",
        "strategy card picked",
        "strategy card played",
        "stymie played",
        "sustained damage",
        "system activated",
        "system placed",
        "technological singularity resolved",
        "technology card played",
        "tekklar legion played",
        "the alastor used",
        "the atrament played",
        "the inferno used",
        "tie broken",
        "token removed from pool",
        "top two agendas ordered",
        "trade agreement played",
        "trade convoys played",
        "trade request accepted",
        "trade request declined",
        "trade request rescinded",
        "trade requested",
        "trade rider resolved",
        "transit diodes played",
        "undid last action",
        "units added",
        "units produced",
        "units removed",
        "voted",
        "war funding played",
        "warfare rider resolved",
        "winnu mech ability used",
        "wormhole generator played",
        "wrath of kenara used",
        "yin spinner played",
        "yssaril mech ability resolved",
        "zombies placed",
    ]
)
"""List of all message types.

This can be retrieved by examining the twilight wars source code. All of the
events are handled via `socket.on(...)` code. It may need to be periodically
updated as the app is updated.
"""
