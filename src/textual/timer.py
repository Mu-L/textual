"""

Contains the `Timer` class.
Timer objects are created by [set_interval][textual.message_pump.MessagePump.set_interval] or
    [set_timer][textual.message_pump.MessagePump.set_timer].
"""

from __future__ import annotations

import weakref
from asyncio import CancelledError, Event, Task, create_task, gather
from typing import Any, Awaitable, Callable, Iterable, Union

from rich.repr import Result, rich_repr

from textual import _time, events
from textual._callback import invoke
from textual._compat import cached_property
from textual._context import active_app
from textual._time import sleep
from textual._types import MessageTarget

TimerCallback = Union[Callable[[], Awaitable[Any]], Callable[[], Any]]
"""Type of valid callbacks to be used with timers."""


class EventTargetGone(Exception):
    """Raised if the timer event target has been deleted prior to the timer event being sent."""


@rich_repr
class Timer:
    """A class to send timer-based events.

    Args:
        event_target: The object which will receive the timer events.
        interval: The time between timer events, in seconds.
        name: A name to assign the event (for debugging).
        callback: A optional callback to invoke when the event is handled.
        repeat: The number of times to repeat the timer, or None to repeat forever.
        skip: Enable skipping of scheduled events that couldn't be sent in time.
        pause: Start the timer paused.
    """

    _timer_count: int = 1

    def __init__(
        self,
        event_target: MessageTarget,
        interval: float,
        *,
        name: str | None = None,
        callback: TimerCallback | None = None,
        repeat: int | None = None,
        skip: bool = True,
        pause: bool = False,
    ) -> None:
        self._target_repr = repr(event_target)
        self._target = weakref.ref(event_target)
        self._interval = interval
        self.name = f"Timer#{self._timer_count}" if name is None else name
        self._timer_count += 1
        self._callback = callback
        self._repeat = repeat
        self._skip = skip
        self._task: Task | None = None
        self._reset: bool = False
        self._original_pause = pause

    @cached_property
    def _active(self) -> Event:
        event = Event()
        if not self._original_pause:
            event.set()
        return event

    def __rich_repr__(self) -> Result:
        yield self._interval
        yield "name", self.name
        yield "repeat", self._repeat, None

    @property
    def target(self) -> MessageTarget:
        target = self._target()
        if target is None:
            raise EventTargetGone()
        return target

    def _start(self) -> None:
        """Start the timer."""
        self._task = create_task(self._run_timer(), name=self.name)

    def stop(self) -> None:
        """Stop the timer."""
        if self._task is None:
            return

        self._active.set()
        self._task.cancel()
        self._task = None

    @classmethod
    async def _stop_all(cls, timers: Iterable[Timer]) -> None:
        """Stop a number of timers, and await their completion.

        Args:
            timers: A number of timers.
        """

        async def stop_timer(timer: Timer) -> None:
            """Stop a timer and wait for it to finish.

            Args:
                timer: A Timer instance.
            """
            if timer._task is not None:
                timer._active.set()
                timer._task.cancel()
                try:
                    await timer._task
                except CancelledError:
                    pass
                timer._task = None

        await gather(*[stop_timer(timer) for timer in list(timers)])

    def pause(self) -> None:
        """Pause the timer.

        A paused timer will not send events until it is resumed.
        """
        self._active.clear()

    def reset(self) -> None:
        """Reset the timer, so it starts from the beginning."""
        self._active.set()
        self._reset = True

    def resume(self) -> None:
        """Resume a paused timer."""
        self._active.set()

    async def _run_timer(self) -> None:
        """Run the timer task."""
        try:
            await self._run()
        except CancelledError:
            pass

    async def _run(self) -> None:
        """Run the timer."""
        count = 0
        _repeat = self._repeat
        _interval = self._interval
        self._active  # Force instantiation in same thread
        await self._active.wait()
        start = _time.get_time()

        while _repeat is None or count <= _repeat:
            next_timer = start + ((count + 1) * _interval)
            now = _time.get_time()
            if self._skip and next_timer < now:
                count = int((now - start) / _interval + 1)
                continue
            now = _time.get_time()
            wait_time = max(0, next_timer - now)
            await sleep(wait_time)
            count += 1
            await self._active.wait()
            if self._reset:
                start = _time.get_time()
                count = 0
                self._reset = False
                continue
            try:
                await self._tick(next_timer=next_timer, count=count)
            except EventTargetGone:
                break

    async def _tick(self, *, next_timer: float, count: int) -> None:
        """Triggers the Timer's action: either call its callback, or sends an event to its target"""

        app = active_app.get()
        if app._exit:
            return

        if self._callback is not None:
            try:
                await invoke(self._callback)
            except CancelledError:
                # https://github.com/Textualize/textual/pull/2895
                # Re-raise CancelledErrors that would be caught by the following exception block in Python 3.7
                raise
            except Exception as error:
                app._handle_exception(error)
        else:
            event = events.Timer(
                timer=self,
                time=next_timer,
                count=count,
                callback=self._callback,
            )
            self.target.post_message(event)
