"""CommandDispatcher for orchestrating command execution using handler pattern."""

import logging
import time
import asyncio
from typing import Any, Dict, Optional, List, Tuple
from collections import deque
import heapq
import json
import threading

from ..core.exceptions import CommandError, TransportError, CircuitBreakerOpenError, RetryExhaustedError
from .transport import Transport
from ..models.models import BatchRequest, BatchResult, BatchCommandResult, BatchCommand, PriorityLevel
from ..core.advanced import ClientRetryPolicyHandler, CommandAnalyticsTracker

logger = logging.getLogger(__name__)


class CommandResult:
    """Represents the standardized response from command handlers.

    Provides a consistent interface for success/error responses with optional data payload
    and reliability metadata from the Java bridge.
    """

    def __init__(self, success: bool, data: Optional[Dict[str, Any]] = None, error_message: Optional[str] = None,
                 retry_status: Optional[Dict[str, Any]] = None,
                 circuit_breaker_state: Optional[Dict[str, Any]] = None,
                 priority_level: Optional[str] = None):
        """
        Initialize a CommandResult.

        Args:
            success: Whether the command succeeded
            data: Response data payload (optional)
            error_message: Error message if failed (optional)
            retry_status: Information about retry attempts (optional)
            circuit_breaker_state: Current circuit breaker state and failure counts (optional)
            priority_level: Priority level used for command execution (optional)
        """
        self.success = success
        self.data = data or {}
        self.error_message = error_message if not success else None
        self.retry_status = retry_status
        self.circuit_breaker_state = circuit_breaker_state
        self.priority_level = priority_level

    @classmethod
    def success(cls, data: Optional[Dict[str, Any]] = None, retry_status: Optional[Dict[str, Any]] = None, circuit_breaker_state: Optional[Dict[str, Any]] = None, priority_level: Optional[str] = None) -> "CommandResult":
        """Create a successful result with optional data."""
        return cls(success=True, data=data, retry_status=retry_status, circuit_breaker_state=circuit_breaker_state, priority_level=priority_level)

    @classmethod
    def error(cls, error_message: str, retry_status: Optional[Dict[str, Any]] = None, circuit_breaker_state: Optional[Dict[str, Any]] = None, priority_level: Optional[str] = None) -> "CommandResult":
        """Create an error result with message."""
        return cls(success=False, error_message=error_message, retry_status=retry_status, circuit_breaker_state=circuit_breaker_state, priority_level=priority_level)

    def is_success(self) -> bool:
        """Check if the command result indicates success."""
        return self.success

    def get_data(self) -> Dict[str, Any]:
        """Get the response data payload."""
        return self.data

    def get_error_message(self) -> Optional[str]:
        """Get the error message if command failed."""
        return self.error_message

    def get_retry_status(self) -> Optional[Dict[str, Any]]:
        """Get retry status information if available."""
        return self.retry_status

    def get_circuit_breaker_state(self) -> Optional[Dict[str, Any]]:
        """Get circuit breaker state information if available."""
        return self.circuit_breaker_state

    def get_priority_level(self) -> Optional[str]:
        """Get the priority level used for command execution."""
        return self.priority_level

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary suitable for network transmission."""
        result = {"status": "ok" if self.success else "error", "data": self.data}
        if not self.success and self.error_message:
            result["error"] = self.error_message
        # Include reliability metadata if available
        if self.retry_status:
            result["retry_status"] = self.retry_status
        if self.circuit_breaker_state:
            result["circuit_breaker_state"] = self.circuit_breaker_state
        if self.priority_level:
            result["priority_level"] = self.priority_level
        return result


class CommandDispatcher:
    """Orchestrates command execution using handler pattern with rate limiting and timeout management.

    This class serves as the main entry point for command processing, replacing the monolithic
    handleCommand logic by dispatching to appropriate handlers on the bridge side.
    """

    def __init__(
        self,
        transport: Transport,
        rate_limit_requests: int = 500,
        rate_limit_window_ms: int = 10000,
        default_timeout: Optional[float] = None,
        max_batch_size: int = 50,
        retry_base_delay: float = 1.0,
        retry_max_delay: float = 30.0,
        retry_backoff_multiplier: float = 2.0,
        max_retry_attempts: int = 3,
        retry_policy: Optional[ClientRetryPolicyHandler] = None,
        analytics_tracker: Optional[CommandAnalyticsTracker] = None,
        max_inflight_requests: int = 32,
        observation_cache_ttl: float = 0.25,
    ):
        """
        Initialize the CommandDispatcher.

        Args:
            transport: Transport instance for communication with bridge
            rate_limit_requests: Maximum requests per rate limit window
            rate_limit_window_ms: Rate limit window in milliseconds
            default_timeout: Default timeout for commands in seconds
            max_batch_size: Maximum number of commands in a batch
            retry_base_delay: Base delay for retry backoff in seconds
            retry_max_delay: Maximum delay for retry backoff in seconds
            retry_backoff_multiplier: Multiplier for exponential backoff
            max_retry_attempts: Maximum number of retry attempts per command
        """
        self.transport = transport
        self.rate_limit_requests = rate_limit_requests
        self.rate_limit_window_ms = rate_limit_window_ms
        self.default_timeout = default_timeout
        self.max_batch_size = max_batch_size
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self.retry_backoff_multiplier = retry_backoff_multiplier
        self.max_retry_attempts = max_retry_attempts
        self.retry_policy = retry_policy
        self.analytics_tracker = analytics_tracker
        self.max_inflight_requests = max(1, int(max_inflight_requests))
        self.observation_cache_ttl = max(0.0, float(observation_cache_ttl))
        self._admission = threading.BoundedSemaphore(self.max_inflight_requests)
        self._dedupe_lock = threading.Lock()
        self._inflight_observations: Dict[str, Dict[str, Any]] = {}
        self._observation_cache: Dict[str, Tuple[float, CommandResult]] = {}

        # Rate limiting state
        self._last_request_times: Dict[str, float] = {}
        self._request_counts: Dict[str, int] = {}

        # Priority queue for batch commands (min-heap: [priority_value, command])
        self._priority_queue: List[Tuple[int, BatchCommand]] = []
        self._priority_values = {
            PriorityLevel.CRITICAL: 0,
            PriorityLevel.HIGH: 1,
            PriorityLevel.NORMAL: 2,
            PriorityLevel.LOW: 3,
        }

        # For rate limiting, we'll use a simple client identifier
        # In a real implementation, this could be per-client or per-IP
        self._client_id = "default"

    _OBSERVATION_COMMANDS = frozenset({
        "get_state", "state", "get_inventory", "get_view", "get_block",
        "get_entities", "get_combat_snapshot", "get_dimension", "get_version", "get_events",
        "get_recipes", "get_screen", "get_death_location", "status",
        "mission/status", "process/status", "goal/status",
    })

    def _request_key(self, command: str, params: Optional[Dict[str, Any]]) -> str:
        canonical = json.dumps(params or {}, sort_keys=True, separators=(",", ":"), default=str)
        return f"{command}:{canonical}"

    @staticmethod
    def _deduplicated_result(result: CommandResult) -> CommandResult:
        data = dict(result.get_data())
        data["deduplicated"] = True
        if result.is_success():
            return CommandResult.success(
                data,
                retry_status=result.get_retry_status(),
                circuit_breaker_state=result.get_circuit_breaker_state(),
                priority_level=result.get_priority_level(),
            )
        return CommandResult.error(
            result.get_error_message() or "Command failed",
            retry_status=result.get_retry_status(),
            circuit_breaker_state=result.get_circuit_breaker_state(),
            priority_level=result.get_priority_level(),
        )

    def dispatch(
        self,
        command: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """Dispatch with bounded admission and safe observation coalescing."""
        is_observation = command in self._OBSERVATION_COMMANDS
        key = self._request_key(command, params) if is_observation else None
        owner = True
        inflight = None

        if is_observation and key is not None:
            now = time.monotonic()
            with self._dedupe_lock:
                cached = self._observation_cache.get(key)
                if cached and now - cached[0] <= self.observation_cache_ttl:
                    return self._deduplicated_result(cached[1])
                inflight = self._inflight_observations.get(key)
                if inflight is None:
                    inflight = {"event": threading.Event(), "result": None}
                    self._inflight_observations[key] = inflight
                else:
                    owner = False

            if not owner:
                wait_timeout = timeout or self.default_timeout or 30.0
                if not inflight["event"].wait(wait_timeout):
                    return CommandResult.error("Backpressure timeout waiting for duplicate observation")
                return self._deduplicated_result(inflight["result"])

        if not self._admission.acquire(blocking=False):
            if is_observation and key is not None and owner:
                with self._dedupe_lock:
                    self._inflight_observations.pop(key, None)
            return CommandResult.error("Backpressure: dispatcher capacity reached; retry later")

        if not is_observation:
            with self._dedupe_lock:
                self._observation_cache.clear()

        result: CommandResult
        try:
            result = self._dispatch_uncached(command, params, timeout)
            return result
        finally:
            self._admission.release()
            if is_observation and key is not None and owner:
                result = locals().get("result", CommandResult.error("Observation dispatch aborted"))
                with self._dedupe_lock:
                    self._observation_cache[key] = (time.monotonic(), result)
                    completed = self._inflight_observations.pop(key, inflight)
                    completed["result"] = result
                    completed["event"].set()

    def _dispatch_uncached(
        self,
        command: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> CommandResult:
        """
        Dispatch a command for execution.

        Uses CommandHandlerFactory on bridge side to get appropriate handlers,
        falls back to legacy processing for commands not yet extracted.

        Args:
            command: Command name to execute
            params: Command parameters
            timeout: Override default timeout in seconds

        Returns:
            CommandResult with execution outcome

        Raises:
            CommandError: If command execution fails
            TransportError: If transport fails
        """
        # Apply rate limiting
        if not self._check_rate_limit():
            return CommandResult.error("Rate limit exceeded. Please try again later.")

        effective_timeout = timeout or self.default_timeout

        # Track execution time for analytics
        start_time = time.time() * 1000  # milliseconds
        success = False
        error_type = None

        try:
            # Dispatch to bridge - bridge will use CommandHandlerFactory if available,
            # otherwise fallback to legacy handleCommand logic
            payload = {"command": command, "params": params or {}}

            logger.info(f"Dispatching command '{command}' with params {params}")

            # Use retry policy if available
            if self.retry_policy:
                response = self.retry_policy.execute_with_retry(
                    lambda: self.transport.dispatch("command", payload, timeout=effective_timeout)
                )
            else:
                response = self.transport.dispatch("command", payload, timeout=effective_timeout)

            success = True

            # transport.dispatch() 已经把 bridge 的信封剥掉了（并在 status 为
            # "error" 时抛异常），所以这里的 response **就是数据本身**。
            #
            # ⚠ 坑：不要拿 response["status"] 当"成功与否"的唯一判据。
            #   · 大部分 route 的 data 里根本没有 status；
            #   · 但 `sequence` 的 data **自带** status:"completed" ——
            #     上一版只认 "ok"，于是每次 sequence 都被判成失败：
            #     mine_quantity 静默返回 {}，而且 run() 还会回退到 chat 再发一次
            #     "#mine ..."，同一个挖矿被执行两遍。
            # 现在只把明确的错误状态当失败，其余（None / ok / completed）都算成功，
            # 并且始终把整个 response 当数据返回，不再去剥 response["data"]。
            status = response.get("status")
            if status is None or status in ("ok", "completed"):
                # 分两种情况，靠"有没有独立的 data 字段"来区分：
                #   · 信封：{"status":"ok", "data":{...}}  → 真数据在 data 里
                #   · 已经是数据：sequence 的 {"partial_results":..., "status":"completed"}
                #     或 get_state 的 {"position":...}  → 整个 response 就是数据
                # 之前两版各错一边：只看 status=="ok" 会把 sequence 判成失败；
                # 无条件返回整个 response 又会让信封形态的调用方拿到多一层壳。
                if isinstance(response.get("data"), (dict, list)):
                    payload_data = response["data"]
                else:
                    payload_data = response
                return CommandResult.success(
                    payload_data,
                    retry_status=response.get("retry_status"),
                    circuit_breaker_state=response.get("circuit_breaker_state"),
                    priority_level=response.get("priority_level")
                )
            else:
                error_msg = response.get("error", "Unknown error")
                error_code = response.get("error_code")
                retry_status = response.get("retry_status")
                circuit_breaker_state = response.get("circuit_breaker_state")

                # Check for specific error types and raise appropriate exceptions
                if error_code == "CIRCUIT_BREAKER_OPEN" or "circuit breaker" in error_msg.lower():
                    raise CircuitBreakerOpenError(error_msg, circuit_breaker_state)
                elif error_code == "RETRY_EXHAUSTED" or "retry attempts" in error_msg.lower():
                    raise RetryExhaustedError(error_msg, retry_status)
                else:
                    # For backward compatibility, return CommandResult for other errors
                    return CommandResult.error(
                        error_msg,
                        retry_status=retry_status,
                        circuit_breaker_state=circuit_breaker_state,
                        priority_level=response.get("priority_level")
                    )

        except (CircuitBreakerOpenError, RetryExhaustedError):
            raise
        except TransportError as e:
            logger.error(f"Transport error dispatching command {command}: {e}")
            success = False
            error_type = "TransportError"
            return CommandResult.error(f"Transport error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error dispatching command {command}: {e}")
            success = False
            error_type = type(e).__name__
            return CommandResult.error(f"Unexpected error: {e}")
        finally:
            # Record analytics if tracker is available
            if self.analytics_tracker:
                execution_time_ms = (time.time() * 1000) - start_time
                self.analytics_tracker.record_command_call(
                    command_name=command,
                    success=success,
                    execution_time_ms=execution_time_ms,
                    error_type=error_type
                )

    def _check_rate_limit(self) -> bool:
        """Check if request is within rate limits."""
        now = time.time() * 1000  # milliseconds
        last_time = self._last_request_times.get(self._client_id, 0)

        if now - last_time > self.rate_limit_window_ms:
            # New window
            self._last_request_times[self._client_id] = now
            self._request_counts[self._client_id] = 1
            return True

        count = self._request_counts.get(self._client_id, 0) + 1
        self._request_counts[self._client_id] = count

        if count > self.rate_limit_requests:
            return False

        return True

    def dispatch_batch(self, batch_request: BatchRequest) -> BatchResult:
        """
        Dispatch a batch of commands with transaction semantics and priority queuing.

        Args:
            batch_request: BatchRequest containing commands to execute

        Returns:
            BatchResult with overall success/failure and individual command results

        Raises:
            ValueError: If batch validation fails
        """
        start_time = time.time() * 1000  # milliseconds

        # Validate batch
        batch_request.validate_batch()
        if len(batch_request.commands) > self.max_batch_size:
            raise ValueError(f"Batch size {len(batch_request.commands)} exceeds maximum {self.max_batch_size}")

        logger.info(f"Starting batch execution for batch_id={batch_request.batch_id} with {len(batch_request.commands)} commands")

        # Sort commands by priority for execution order
        sorted_commands = sorted(
            batch_request.commands,
            key=lambda cmd: self._priority_values[cmd.priority]
        )

        command_results: List[BatchCommandResult] = []
        successful_commands: List[BatchCommand] = []
        failed_commands: List[BatchCommand] = []
        rollback_performed = False

        try:
            # Execute commands in priority order
            for command in sorted_commands:
                result = self._execute_command_with_retry(command, batch_request.overall_timeout)
                command_results.append(result)

                if result.success:
                    successful_commands.append(command)
                    logger.debug(f"Command {command.id} succeeded")
                else:
                    failed_commands.append(command)
                    logger.warning(f"Command {command.id} failed: {result.error_message}")

                    # In transaction mode, stop on first failure
                    if batch_request.transaction_mode:
                        logger.error(f"Transaction failed on command {command.id}, initiating rollback")
                        rollback_performed = self._rollback_commands(successful_commands)
                        break

            # Determine overall success
            overall_success = len(failed_commands) == 0

            if overall_success:
                logger.info(f"Batch {batch_request.batch_id} completed successfully")
            else:
                logger.error(f"Batch {batch_request.batch_id} failed with {len(failed_commands)} failed commands")

        except Exception as e:
            logger.error(f"Unexpected error during batch execution: {e}")
            overall_success = False
            # If we get here, some commands may have succeeded
            rollback_performed = self._rollback_commands(successful_commands) if batch_request.transaction_mode else False

        total_execution_time = (time.time() * 1000) - start_time
        failed_command_ids = [cmd.id for cmd in failed_commands]

        error_summary = None
        if not overall_success:
            failed_count = len(failed_commands)
            error_summary = f"{failed_count} of {len(batch_request.commands)} commands failed"
            if rollback_performed:
                error_summary += " (rollback performed)"

        return BatchResult(
            batch_id=batch_request.batch_id,
            overall_success=overall_success,
            command_results=command_results,
            total_execution_time_ms=total_execution_time,
            failed_commands=failed_command_ids,
            rollback_performed=rollback_performed,
            error_summary=error_summary
        )

    def _execute_command_with_retry(self, command: BatchCommand, batch_timeout: Optional[float]) -> BatchCommandResult:
        """
        Execute a single command with retry logic and exponential backoff.

        Args:
            command: The command to execute
            batch_timeout: Overall batch timeout (if any)

        Returns:
            BatchCommandResult for this command
        """
        start_time = time.time() * 1000
        attempts = 0
        last_error = None

        while attempts <= self.max_retry_attempts:
            try:
                # Check if we've exceeded batch timeout
                if batch_timeout:
                    elapsed = (time.time() * 1000 - start_time) / 1000.0
                    if elapsed >= batch_timeout:
                        return BatchCommandResult(
                            command_id=command.id,
                            success=False,
                            error_message=f"Batch timeout exceeded ({batch_timeout}s)",
                            execution_time_ms=time.time() * 1000 - start_time,
                            retry_attempts=attempts,
                            priority_level=command.priority.value
                        )

                # Apply rate limiting
                if not self._check_rate_limit():
                    return BatchCommandResult(
                        command_id=command.id,
                        success=False,
                        error_message="Rate limit exceeded",
                        execution_time_ms=time.time() * 1000 - start_time,
                        retry_attempts=attempts,
                        priority_level=command.priority.value
                    )

                # Execute command
                timeout = command.timeout or self.default_timeout
                result = self.dispatch(command.command, command.params, timeout=timeout)

                # Success
                execution_time = time.time() * 1000 - start_time
                return BatchCommandResult(
                    command_id=command.id,
                    success=True,
                    data=result.data,
                    execution_time_ms=execution_time,
                    retry_attempts=attempts,
                    priority_level=command.priority.value
                )

            except (TransportError, CommandError, CircuitBreakerOpenError) as e:
                last_error = str(e)
                attempts += 1

                if attempts <= self.max_retry_attempts:
                    # Calculate backoff delay
                    delay = min(
                        self.retry_base_delay * (self.retry_backoff_multiplier ** (attempts - 1)),
                        self.retry_max_delay
                    )
                    logger.warning(f"Command {command.id} failed (attempt {attempts}/{self.max_retry_attempts + 1}), retrying in {delay}s: {e}")
                    time.sleep(delay)
                else:
                    logger.error(f"Command {command.id} exhausted all retry attempts: {e}")

            except Exception as e:
                # Non-retryable error
                last_error = str(e)
                logger.error(f"Non-retryable error for command {command.id}: {e}")
                break

        # All attempts failed
        execution_time = time.time() * 1000 - start_time
        return BatchCommandResult(
            command_id=command.id,
            success=False,
            error_message=last_error or "Unknown error",
            execution_time_ms=execution_time,
            retry_attempts=attempts,
            priority_level=command.priority.value
        )

    def _rollback_commands(self, commands: List[BatchCommand]) -> bool:
        """
        Attempt to rollback successful commands in reverse order.

        Args:
            commands: List of commands to rollback

        Returns:
            True if rollback succeeded, False otherwise
        """
        if not commands:
            return True

        logger.info(f"Attempting rollback of {len(commands)} commands")

        # Rollback in reverse order
        rollback_success = True
        for command in reversed(commands):
            try:
                # For now, we log the rollback attempt. In a real implementation,
                # this might involve calling undo commands or compensating actions
                logger.info(f"Rolling back command {command.id}")
                # TODO: Implement actual rollback logic based on command type
                # This could involve storing reverse operations or calling undo endpoints

            except Exception as e:
                logger.error(f"Failed to rollback command {command.id}: {e}")
                rollback_success = False

        return rollback_success

    def get_rate_limit_info(self) -> Dict[str, Any]:
        """Get current rate limiting information."""
        now = time.time() * 1000
        last_time = self._last_request_times.get(self._client_id, 0)
        count = self._request_counts.get(self._client_id, 0)

        return {
            "current_count": count,
            "limit": self.rate_limit_requests,
            "window_ms": self.rate_limit_window_ms,
            "time_until_reset_ms": max(0, self.rate_limit_window_ms - (now - last_time)),
        }
