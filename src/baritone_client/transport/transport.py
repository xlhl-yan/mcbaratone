import abc
import asyncio
import json
import logging
import queue
import socket
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Set, Union

import websockets

from .enums import TransportEvent
from .tcp_protocol import BEST_EFFORT_ROUTES, READ_ONLY_ROUTES
from .tcp_protocol import (
    is_traced_command,
    observe_response,
    translate_route,
    validate_response_sequence,
)
from ..events.event_manager import EventManager, EventFilter
from ..events.event_storage import EventStorage
from ..core.exceptions import (
    BridgeResponseTimeout,
    CommandError,
    RouteError,
    TransportError,
)
from ..observability import (
    emit_event,
    observe_command_response,
    observe_entities_response,
    observe_inventory_response,
    observe_state_response,
)

logger = logging.getLogger(__name__)


def decode_bridge_bytes(raw: bytes) -> str:
    """Decode a chunk received from the Java bridge.

    The bridge writes with ``new PrintWriter(socket.getOutputStream(), true)``
    (see BaritoneAPIBridge.java) which uses the *JVM default* charset. On a
    Chinese Windows install that is GB18030/GBK, not UTF-8. Decoding with a
    hard-coded UTF-8 therefore raises UnicodeDecodeError as soon as any
    non-ASCII text (world name, chat, item names...) appears in a response,
    which kills the read loop and surfaces as a timeout on every route.

    Strategy: try the encoding the bridge actually used first, then UTF-8,
    then a lossless fallback so a single odd byte can never break the reader.
    """
    for encoding in ("gb18030", "utf-8"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    # Neither worked cleanly: keep the stream alive instead of crashing.
    return raw.decode("utf-8", errors="replace")


def connect(host: str, port: int, timeout: float = 2.0):
    """Create and connect a TCP socket. Separated out so tests can patch it."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect((host, port))
    return s


class Transport(abc.ABC):
    """Abstract transport base class."""

    def __init__(self, event_manager: Optional[EventManager] = None) -> None:
        self._event_manager = event_manager
        self._subscriptions: Dict[TransportEvent, List[Callable]] = {}

    @abc.abstractmethod
    def dispatch(self, route: str, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        """Dispatch a request to the bridge.

        Args:
            route: Logical route name (e.g., "get_inventory")
            payload: Payload dictionary
            timeout: Optional per-call timeout in seconds. If None, the transport's
                default timeout and any per-route overrides are used.

        Returns:
            Parsed response data dictionary.
        """
        raise NotImplementedError()

    def subscribe(self, event: TransportEvent, callback: Callable) -> None:
        if event not in self._subscriptions:
            self._subscriptions[event] = []
        self._subscriptions[event].append(callback)

    def emit(self, event: TransportEvent, payload: Dict[str, Any]) -> None:
        # Publish to EventManager if available
        if self._event_manager:
            self._event_manager.publish_event(event, payload)

        # Also notify direct subscribers for backward compatibility
        for cb in self._subscriptions.get(event, []):
            try:
                cb(payload)
            except Exception:
                logger.exception("Error in transport event callback")


class TcpTransport(Transport):
    """TCP bridge transport with request-id response correlation."""

    def __init__(self, host: str = "localhost", port: int = 5555, timeout: float = 15.0, event_manager: Optional[EventManager] = None) -> None:
        super().__init__(event_manager)
        self.host = host
        self.port = port
        self.timeout = timeout

        # Use connect() so tests can replace socket creation.
        self._socket = connect(host, port, timeout=self.timeout)
        self._socket.settimeout(min(0.5, self.timeout))

        self._lock = threading.RLock()
        self._response_queues: Dict[str, queue.Queue] = {}
        self._seq = 0
        self._schematic_uploads: Dict[str, str] = {}
        self._shutdown_event = threading.Event()

        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _reconnect(self, expected_socket) -> bool:
        """Replace one known-stale socket without closing a newer connection."""
        with self._lock:
            if self._shutdown_event.is_set() or self._socket is not expected_socket:
                return False

        replacement = connect(self.host, self.port, timeout=self.timeout)
        try:
            replacement.settimeout(min(0.5, self.timeout))
        except Exception:
            replacement.close()
            raise

        with self._lock:
            if self._shutdown_event.is_set() or self._socket is not expected_socket:
                installed = False
            else:
                self._socket = replacement
                installed = True
        if not installed:
            replacement.close()
            return False
        try:
            expected_socket.close()
        except OSError:
            pass
        return True

    def _reconnect_after_failure(self, expected_socket, route: str) -> bool:
        """Best-effort reconnect for a retry-safe read or reader failure."""
        try:
            return self._reconnect(expected_socket)
        except OSError:
            logger.warning(
                "Bridge reconnect failed for route %s at %s:%s",
                route,
                self.host,
                self.port,
                exc_info=True,
            )
            return False

    # Read-only queries may be replayed; mutations must remain single-shot.
    _READ_ONLY_RETRY_ROUTES = READ_ONLY_ROUTES
    _BEST_EFFORT_RETRY_ROUTES = BEST_EFFORT_ROUTES
    _READ_RETRY_ATTEMPTS = 3
    _READ_RETRY_PAUSE_SECONDS = 0.5
    _TRANSIENT_COMMAND_ERRORS = (
        "player not available",
        "not connected",
        "connection lost",
        "connection reset",
    )

    def dispatch(self, route: str, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        traced_command = is_traced_command(route)
        command_id = uuid.uuid4().hex if traced_command else None
        command_started_ns = time.monotonic_ns()
        if traced_command:
            emit_event(
                "command_requested",
                command_id=command_id,
                route=route,
                payload=payload,
                timeout=timeout,
            )
        is_cancel = (
            route in {"cancel", "command/cancel"}
            or (route == "command" and payload.get("command") == "cancel")
            or (route.startswith("process/") and route.endswith("/stop"))
        )
        cancel_id = uuid.uuid4().hex if is_cancel else None
        cancel_started_ns = time.monotonic_ns()
        if is_cancel:
            emit_event(
                "cancellation_requested",
                cancellation_id=cancel_id,
                route=route,
                payload=payload,
                timeout=timeout,
            )
        attempts = (
            self._READ_RETRY_ATTEMPTS
            if route in self._READ_ONLY_RETRY_ROUTES
            else self._READ_RETRY_ATTEMPTS
            if route in self._BEST_EFFORT_RETRY_ROUTES
            else 1
        )
        last_error: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                response = self._dispatch_once(route, payload, timeout)
                if route == "get_inventory":
                    from ..inventory_evidence import valid_inventory
                    if not valid_inventory(response):
                        raise CommandError("Malformed inventory snapshot; current inventory unknown")
                try:
                    if route == "get_inventory":
                        observe_inventory_response(response)
                    elif route in {"get_state", "process/status"}:
                        observe_state_response(response)
                    elif route == "get_entities":
                        observe_entities_response(response)
                    if traced_command:
                        observe_command_response(route, payload, response)
                        emit_event(
                            "command_completed",
                            command_id=command_id,
                            route=route,
                            latency_ms=round(
                                (time.monotonic_ns() - command_started_ns)
                                / 1_000_000,
                                3,
                            ),
                            response=response,
                        )
                except Exception:
                    logger.debug("Telemetry observation failed", exc_info=True)
                if is_cancel:
                    emit_event(
                        "cancellation_completed",
                        cancellation_id=cancel_id,
                        route=route,
                        attempt=attempt + 1,
                        latency_ms=round(
                            (time.monotonic_ns() - cancel_started_ns) / 1_000_000,
                            3,
                        ),
                        response=response,
                    )
                return response
            except (TransportError, CommandError) as exc:
                last_error = exc
                if (
                    attempt + 1 < attempts
                    and (
                        isinstance(exc, TransportError)
                        or (
                            isinstance(exc, CommandError)
                            and (
                                self._is_transient_command_error(exc)
                                or (
                                    route == "get_inventory"
                                    and "inventory" in str(exc).lower()
                                    and "unknown" in str(exc).lower()
                                )
                            )
                        )
                    )
                ):
                    logger.warning(
                        "Read route %s retryable failure (attempt %d/%d); retrying",
                        route,
                        attempt + 1,
                        attempts,
                    )
                    time.sleep(self._READ_RETRY_PAUSE_SECONDS)
                else:
                    if traced_command:
                        emit_event(
                            "command_failed",
                            command_id=command_id,
                            route=route,
                            latency_ms=round(
                                (time.monotonic_ns() - command_started_ns)
                                / 1_000_000,
                                3,
                            ),
                            error_type=type(exc).__name__,
                            reason=str(exc),
                            response=getattr(exc, "response", None),
                        )
                    if is_cancel:
                        emit_event(
                            "cancellation_failed",
                            cancellation_id=cancel_id,
                            route=route,
                            attempt=attempt + 1,
                            latency_ms=round(
                                (time.monotonic_ns() - cancel_started_ns)
                                / 1_000_000,
                                3,
                            ),
                            error_type=type(exc).__name__,
                            reason=str(exc),
                        )
                    raise exc
        assert last_error is not None
        raise last_error

    def _is_transient_command_error(self, error: CommandError) -> bool:
        """Treat intermittent player-availability failures as retryable."""
        message = str(error).lower()
        return any(token in message for token in self._TRANSIENT_COMMAND_ERRORS)

    def _dispatch_once(self, route: str, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        req_id = str(uuid.uuid4())
        with self._lock:
            self._seq += 1
            seq = self._seq

        bridge_cmd = translate_route(route, payload)
        bridge_cmd["id"] = req_id
        # The bridge protocol keys the request by `request_seq` (it echoes back
        # `request_seq` and `server_seq` in its response envelope). Sending a
        # `seq` field leaves the request malformed and the bridge rejects it
        # with "Missing command", which surfaces as a read timeout on every
        # route. Match the bridge's expected field name.
        bridge_cmd["request_seq"] = seq

        q = queue.Queue()
        with self._lock:
            self._response_queues[req_id] = q

        request_socket = None
        request_sent = False
        try:
            msg = json.dumps(bridge_cmd) + "\n"
            try:
                with self._lock:
                    request_socket = self._socket
                    request_socket.sendall(msg.encode("utf-8"))
                    request_sent = True
            except OSError as exc:
                if route in self._READ_ONLY_RETRY_ROUTES and request_socket:
                    self._reconnect_after_failure(request_socket, route)
                raise TransportError(
                    f"Failed to send bridge request (route: {route})",
                    original_error=exc,
                ) from exc

            effective_timeout = self.timeout if timeout is None else timeout
            if timeout is None:
                if route == "get_inventory":
                    # Increase timeout for inventory checks to be more robust.
                    effective_timeout = min(self.timeout, 2.0)
                elif route in ("get_state", "get_combat_snapshot"):
                    # Use a moderate default for get_state when caller didn't
                    # provide one (keeps phase startup snappy but avoids timeouts).
                    effective_timeout = min(self.timeout, 2.0)

            resp = q.get(timeout=effective_timeout)

            validate_response_sequence(resp, seq)
            if resp.get("status") == "error":
                error = resp.get("error", "Bridge error")
                if isinstance(error, dict):
                    error = error.get("message", str(error))
                raise CommandError(str(error), response=resp)

            return resp.get("data", {})

        except queue.Empty:
            if route in self._BEST_EFFORT_RETRY_ROUTES:
                logger.warning(
                    "Best-effort route %s timed out; "
                    "continuing as no-op",
                    route,
                )
                return {}
            with self._lock:
                peer_inflight = any(
                    peer_id != req_id for peer_id in self._response_queues
                )
            if route in self._READ_ONLY_RETRY_ROUTES and request_socket and not peer_inflight:
                self._reconnect_after_failure(request_socket, route)
            raise BridgeResponseTimeout(
                route,
                transport_type="tcp",
                request_sent=request_sent,
            )
        finally:
            with self._lock:
                self._response_queues.pop(req_id, None)

    def subscribe(self, event: TransportEvent, callback) -> None:
        super().subscribe(event, callback)

    def emit(self, event: TransportEvent, payload: Dict[str, Any]) -> None:
        for cb in self._subscriptions.get(event, []):
            try:
                cb(payload)
            except Exception:
                logger.exception("Error in transport event callback")

    def shutdown(self) -> None:
        self._shutdown_event.set()
        try:
            self._socket.close()
        except Exception:
            pass

    def _read_loop(self) -> None:
        """Route responses and replace sockets that have reached EOF."""
        buffer = ""
        while not self._shutdown_event.is_set():
            try:
                with self._lock:
                    active_socket = self._socket
                try:
                    chunk = decode_bridge_bytes(active_socket.recv(4096))
                    if not chunk:
                        if self._shutdown_event.is_set():
                            break
                        self._reconnect_after_failure(active_socket, "reader_eof")
                        buffer = ""
                        time.sleep(0.1)
                        continue
                    buffer += chunk
                except socket.timeout:
                    continue
                except OSError:
                    if self._shutdown_event.is_set():
                        break
                    self._reconnect_after_failure(active_socket, "reader_error")
                    buffer = ""
                    time.sleep(0.1)
                    continue

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if not line.strip():
                        continue
                    try:
                        data = json.loads(line)
                        if "id" in data:
                            req_id = data["id"]
                            with self._lock:
                                if req_id in self._response_queues:
                                    self._response_queues[req_id].put(data)
                        else:
                            evt = data.get("event")
                            if evt:
                                try:
                                    ev_enum = TransportEvent(evt)
                                except Exception:
                                    ev_enum = None
                                if ev_enum:
                                    self.emit(ev_enum, data.get("payload", {}))
                    except json.JSONDecodeError:
                        logger.error(f"Invalid JSON from bridge: {line}")

            except Exception:
                time.sleep(0.1)


class WebSocketTransport(Transport):
    """Enhanced WebSocket transport with real-time event streaming and durable event handling.

    This transport provides:
    - Real-time bidirectional event streaming over WebSocket
    - Priority-based event processing and delivery
    - Durable event storage for missed events during disconnections
    - Client-side event filtering and subscription management
    - Event buffering for connection recovery
    - Performance optimizations for high-frequency events
    """

    def __init__(
        self,
        url: str,
        event_manager: Optional[EventManager] = None,
        timeout: float = 15.0,
        storage_path: str = "websocket_event_storage.json",
        max_buffered_events: int = 1000,
        enable_event_storage: bool = True,
        reconnect_interval: float = 5.0,
        max_reconnect_attempts: int = 10
    ):
        super().__init__(event_manager)
        self.url = url
        self.timeout = timeout
        self.storage_path = storage_path
        self.max_buffered_events = max_buffered_events
        self.enable_event_storage = enable_event_storage
        self.reconnect_interval = reconnect_interval
        self.max_reconnect_attempts = max_reconnect_attempts

        self._websocket: Optional[websockets.WebSocketServerProtocol] = None
        self._lock = threading.RLock()
        self._seq = 0
        self._shutdown_event = threading.Event()

        # Response queues for request-response correlation
        self._response_queues: Dict[int, queue.Queue] = {}
        self._response_lock = threading.RLock()
        self._best_effort_retry_routes = {"close_screen"}
        self._read_only_retry_routes = READ_ONLY_ROUTES
        self._read_retry_attempts = 3

        # Advanced subscription management with filtering
        self._event_subscriptions: Dict[str, EventFilter] = {}
        self._subscription_lock = threading.RLock()

        # Event buffering and priority queues
        self._event_buffer: List[Dict[str, Any]] = []
        self._buffer_lock = threading.RLock()
        self._last_connection_time = 0.0
        self._connection_state = "disconnected"  # disconnected, connecting, connected

        # Durable event storage
        self._event_storage: Optional[EventStorage] = None
        if enable_event_storage:
            self._event_storage = EventStorage(
                storage_path=storage_path,
                max_entries=max_buffered_events
            )

        # Event loop for async operations
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._event_thread: Optional[threading.Thread] = None

        # Performance optimization: batch processing
        self._batch_size = 50
        self._batch_interval = 0.1  # seconds

        # Connect immediately
        self._connect()

    def _connect(self) -> None:
        """Establish WebSocket connection with automatic reconnection."""
        self._connection_state = "connecting"
        reconnect_attempts = 0

        while not self._shutdown_event.is_set() and reconnect_attempts < self.max_reconnect_attempts:
            try:
                # Create event loop in separate thread if not exists
                if self._loop is None:
                    self._loop = asyncio.new_event_loop()
                    self._event_thread = threading.Thread(target=self._run_event_loop, daemon=True)
                    self._event_thread.start()

                # Wait for connection to be established
                future = asyncio.run_coroutine_threadsafe(self._async_connect(), self._loop)
                future.result(timeout=10.0)

                self._connection_state = "connected"
                self._last_connection_time = time.time()
                logger.info(f"WebSocket connected to {self.url}")

                # Process buffered events after reconnection
                self._process_buffered_events()
                break

            except Exception as e:
                reconnect_attempts += 1
                logger.warning(f"WebSocket connection attempt {reconnect_attempts} failed: {e}")
                if reconnect_attempts < self.max_reconnect_attempts:
                    time.sleep(self.reconnect_interval)
                else:
                    self._connection_state = "disconnected"
                    raise TransportError(f"WebSocket connection failed after {reconnect_attempts} attempts: {e}")

    def _run_event_loop(self) -> None:
        """Run the asyncio event loop in a separate thread."""
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        except Exception as e:
            logger.error(f"Event loop error: {e}")

    async def _async_connect(self) -> None:
        """Asynchronously establish WebSocket connection."""
        try:
            self._websocket = await websockets.connect(self.url)
            # Start message handling task
            asyncio.create_task(self._handle_messages())
            # Start batch processing task
            asyncio.create_task(self._batch_event_processor())
        except Exception as e:
            logger.error(f"Async WebSocket connection failed: {e}")
            raise

    async def _handle_messages(self) -> None:
        """Handle incoming WebSocket messages with priority-based processing."""
        try:
            async for message in self._websocket:
                try:
                    data = json.loads(message)

                    # Check if it's a response to a request
                    if "id" in data and isinstance(data["id"], int):
                        with self._response_lock:
                            if data["id"] in self._response_queues:
                                self._response_queues[data["id"]].put(data)
                    # Check if it's an event
                    elif "event" in data:
                        await self._process_incoming_event(data)
                except json.JSONDecodeError:
                    logger.error(f"Invalid JSON from WebSocket: {message}")
                except Exception as e:
                    logger.error(f"Error handling WebSocket message: {e}")
        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket connection closed")
            self._connection_state = "disconnected"
            # Attempt reconnection
            if not self._shutdown_event.is_set():
                asyncio.create_task(self._reconnect())
        except Exception as e:
            logger.error(f"WebSocket message handling error: {e}")

    async def _process_incoming_event(self, data: Dict[str, Any]) -> None:
        """Process incoming event with filtering and priority handling."""
        event_data = data.get("payload", {})
        event_type = data["event"]
        priority = data.get("priority", 0)

        # Check if event passes filters
        if self._should_process_event(event_type, event_data, priority):
            try:
                event_enum = TransportEvent(event_type)
                self.emit(event_enum, event_data)
            except ValueError:
                # Unknown event type, emit anyway with string
                self.emit(event_type, event_data)
        else:
            # Buffer filtered events for later processing if storage is enabled
            if self._event_storage:
                from ..events.event_manager import Event
                event = Event(
                    type=event_type,
                    data=event_data,
                    timestamp=time.time(),
                    priority=priority
                )
                self._event_storage.store_event(event)

    def _should_process_event(self, event_type: str, event_data: Dict[str, Any], priority: int) -> bool:
        """Check if event should be processed based on subscriptions and filters."""
        with self._subscription_lock:
            if event_type not in self._event_subscriptions:
                return False

            filter_ = self._event_subscriptions[event_type]
            if filter_ is None:
                return True

            # Create a temporary event for filtering
            from ..events.event_manager import Event
            temp_event = Event(
                type=event_type,
                data=event_data,
                timestamp=time.time(),
                priority=priority
            )

            return filter_.matches(temp_event)

    async def _batch_event_processor(self) -> None:
        """Process events in batches for better performance."""
        while not self._shutdown_event.is_set():
            await asyncio.sleep(self._batch_interval)

            # Get batch of events to process
            events_to_process = []
            with self._buffer_lock:
                if len(self._event_buffer) >= self._batch_size:
                    events_to_process = self._event_buffer[:self._batch_size]
                    self._event_buffer = self._event_buffer[self._batch_size:]

            # Process batch
            for event_data in events_to_process:
                try:
                    await self._process_incoming_event(event_data)
                except Exception as e:
                    logger.error(f"Error processing batched event: {e}")

    async def _reconnect(self) -> None:
        """Attempt to reconnect WebSocket connection."""
        if self._shutdown_event.is_set():
            return

        logger.info("Attempting WebSocket reconnection...")
        try:
            await self._async_connect()
            self._connection_state = "connected"
            logger.info("WebSocket reconnected successfully")
        except Exception as e:
            logger.error(f"WebSocket reconnection failed: {e}")
            # Schedule another reconnection attempt
            if not self._shutdown_event.is_set():
                asyncio.get_event_loop().call_later(self.reconnect_interval, lambda: asyncio.create_task(self._reconnect()))

    def subscribe_events(
        self,
        event_types: Set[Union[str, TransportEvent]],
        filter_: Optional[EventFilter] = None,
        priority_threshold: int = 0
    ) -> None:
        """Subscribe to specific event types with advanced filtering."""
        event_type_strings = {t.value if isinstance(t, TransportEvent) else t for t in event_types}

        # Create or update filter
        if filter_ is None:
            filter_ = EventFilter(event_types=event_type_strings)

        # Add priority filtering if threshold specified
        if priority_threshold > 0:
            def priority_predicate(event) -> bool:
                return event.priority >= priority_threshold
            if filter_.predicate:
                original_predicate = filter_.predicate
                filter_.predicate = lambda e: original_predicate(e) and priority_predicate(e)
            else:
                filter_.predicate = priority_predicate

        with self._subscription_lock:
            for event_type in event_type_strings:
                self._event_subscriptions[event_type] = filter_

        # Send subscription request over WebSocket
        subscription_request = {
            "jsonrpc": "2.0",
            "method": "subscribe",
            "params": {
                "events": list(event_type_strings),
                "priority_threshold": priority_threshold
            }
        }
        if self._loop and self._connection_state == "connected":
            asyncio.run_coroutine_threadsafe(self._send_message(subscription_request), self._loop)
        logger.info(f"Subscribed to events with filtering: {event_type_strings}")

    def unsubscribe_events(self, event_types: Set[Union[str, TransportEvent]]) -> None:
        """Unsubscribe from specific event types."""
        event_type_strings = {t.value if isinstance(t, TransportEvent) else t for t in event_types}

        with self._subscription_lock:
            for event_type in event_type_strings:
                self._event_subscriptions.pop(event_type, None)

        # Send unsubscription request over WebSocket
        unsubscription_request = {
            "jsonrpc": "2.0",
            "method": "unsubscribe",
            "params": {"events": list(event_type_strings)}
        }
        if self._loop and self._connection_state == "connected":
            asyncio.run_coroutine_threadsafe(self._send_message(unsubscription_request), self._loop)
        logger.info(f"Unsubscribed from events: {event_type_strings}")

    def get_active_subscriptions(self) -> Dict[str, EventFilter]:
        """Get currently active event subscriptions with filters."""
        with self._subscription_lock:
            return self._event_subscriptions.copy()

    def is_subscribed(self, event_type: Union[str, TransportEvent]) -> bool:
        """Check if subscribed to a specific event type."""
        event_type_str = event_type.value if isinstance(event_type, TransportEvent) else event_type
        with self._subscription_lock:
            return event_type_str in self._event_subscriptions

    def get_missed_events(
        self,
        event_types: Optional[Set[Union[str, TransportEvent]]] = None,
        since_timestamp: Optional[float] = None,
        min_priority: int = 0
    ) -> List[Dict[str, Any]]:
        """Retrieve missed events from durable storage."""
        if not self._event_storage:
            return []

        events = self._event_storage.get_events(
            event_types=event_types,
            since_timestamp=since_timestamp,
            min_priority=min_priority
        )

        # Convert to dictionary format for compatibility
        return [{
            'type': event.type,
            'data': event.data,
            'timestamp': event.timestamp,
            'priority': event.priority
        } for event in events]

    def clear_missed_events(self, event_types: Optional[Set[Union[str, TransportEvent]]] = None) -> int:
        """Clear missed events from storage."""
        if not self._event_storage:
            return 0

        return self._event_storage.remove_events(event_types)

    def _process_buffered_events(self) -> None:
        """Process buffered events after reconnection."""
        if not self._event_storage:
            return

        # Get recent events from storage
        recent_events = self._event_storage.get_events(
            since_timestamp=self._last_connection_time
        )

        # Sort by priority and timestamp
        recent_events.sort(key=lambda e: (-e.priority, e.timestamp))

        # Emit events
        for event in recent_events:
            try:
                event_enum = TransportEvent(event.type)
                self.emit(event_enum, event.data)
            except ValueError:
                self.emit(event.type, event.data)

        logger.info(f"Processed {len(recent_events)} buffered events after reconnection")

    async def _send_message(self, message: Dict[str, Any]) -> None:
        """Send a message over WebSocket asynchronously."""
        if not self._websocket or not self._websocket.open:
            raise TransportError("WebSocket is not connected")
        try:
            await self._websocket.send(json.dumps(message))
        except Exception as exc:
            raise TransportError(
                f"Failed to send WebSocket message: {exc}",
                original_error=exc,
            ) from exc

    def dispatch(self, route: str, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        """Dispatch through the shared WebSocket request/correlation helper."""
        from .websocket_dispatch import dispatch
        return dispatch(self, route, payload, timeout)

    def emit(self, event: Union[TransportEvent, str], payload: Dict[str, Any], priority: int = 0) -> None:
        """Emit event with priority-based processing and bidirectional streaming."""
        event_type_str = event.value if isinstance(event, TransportEvent) else str(event)

        # Store event in durable storage if enabled
        if self._event_storage and self._connection_state != "connected":
            from ..events.event_manager import Event
            event_obj = Event(
                type=event_type_str,
                data=payload,
                timestamp=time.time(),
                priority=priority
            )
            self._event_storage.store_event(event_obj)

        # Check if we should stream this event based on subscriptions
        if self.is_subscribed(event_type_str):
            # Send event over WebSocket if connection is active
            event_message = {
                "jsonrpc": "2.0",
                "method": "emit",
                "params": {
                    "event": event_type_str,
                    "payload": payload,
                    "priority": priority,
                    "timestamp": time.time()
                }
            }
            if self._loop and self._websocket and self._websocket.open and self._connection_state == "connected":
                try:
                    asyncio.run_coroutine_threadsafe(self._send_message(event_message), self._loop)
                except Exception as e:
                    logger.error(f"Failed to emit event over WebSocket: {e}")
                    # Buffer event for retry
                    with self._buffer_lock:
                        if len(self._event_buffer) < self.max_buffered_events:
                            self._event_buffer.append(event_message)

        # Also call parent emit for EventManager integration
        super().emit(event, payload)

    def shutdown(self) -> None:
        """Stop event loop and close WebSocket connection gracefully."""
        self._shutdown_event.set()
        self._connection_state = "disconnected"

        # Clear subscriptions
        with self._subscription_lock:
            self._event_subscriptions.clear()

        # Clear event buffer
        with self._buffer_lock:
            self._event_buffer.clear()

        # Cleanup event storage
        if self._event_storage:
            try:
                self._event_storage.cleanup()
            except Exception as e:
                logger.error(f"Error cleaning up event storage: {e}")

        # Close WebSocket connection
        if self._loop and self._websocket:
            try:
                future = asyncio.run_coroutine_threadsafe(self._websocket.close(), self._loop)
                future.result(timeout=5.0)
            except Exception as e:
                logger.error(f"Error closing WebSocket: {e}")

        # Stop event loop
        if self._loop:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:
                pass

        # Wait for event thread
        if self._event_thread and self._event_thread.is_alive():
            self._event_thread.join(timeout=5.0)

        logger.info("WebSocketTransport shutdown complete")


class Py4JTransport(Transport):
    """Stub Py4J transport for tests and imports."""

    def __init__(self, gateway_params: Optional[Dict[str, Any]] = None, event_manager: Optional[EventManager] = None):
        super().__init__(event_manager)
        self.gateway_params = gateway_params or {}

    def dispatch(self, route: str, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        raise RouteError(route, "Py4JTransport")

    
