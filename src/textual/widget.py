"""
This module contains the `Widget` class, the base class for all widgets.

"""

from __future__ import annotations

from asyncio import create_task, gather, wait
from collections import Counter
from contextlib import asynccontextmanager
from fractions import Fraction
from time import monotonic
from types import TracebackType
from typing import (
    TYPE_CHECKING,
    AsyncGenerator,
    Callable,
    ClassVar,
    Collection,
    Generator,
    Iterable,
    NamedTuple,
    Sequence,
    TypeVar,
    cast,
    overload,
)

import rich.repr
from rich.console import (
    Console,
    ConsoleOptions,
    ConsoleRenderable,
    JustifyMethod,
    RenderableType,
)
from rich.console import RenderResult as RichRenderResult
from rich.measure import Measurement
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from typing_extensions import Self

from textual.css.styles import StylesBase

if TYPE_CHECKING:
    from textual.app import RenderResult

from textual import constants, errors, events, messages
from textual._animator import DEFAULT_EASING, Animatable, BoundAnimator, EasingFunction
from textual._arrange import DockArrangeResult, arrange
from textual._context import NoActiveAppError
from textual._debug import get_caller_file_and_line
from textual._dispatch_key import dispatch_key
from textual._easing import DEFAULT_SCROLL_EASING
from textual._extrema import Extrema
from textual._styles_cache import StylesCache
from textual._types import AnimationLevel
from textual.actions import SkipAction
from textual.await_remove import AwaitRemove
from textual.box_model import BoxModel
from textual.cache import FIFOCache
from textual.color import Color
from textual.compose import compose
from textual.content import Content, ContentType
from textual.css.match import match
from textual.css.parse import parse_selectors
from textual.css.query import NoMatches, WrongType
from textual.css.scalar import Scalar, ScalarOffset
from textual.dom import DOMNode, NoScreen
from textual.geometry import (
    NULL_REGION,
    NULL_SIZE,
    NULL_SPACING,
    Offset,
    Region,
    Size,
    Spacing,
    clamp,
)
from textual.layout import Layout
from textual.layouts.vertical import VerticalLayout
from textual.message import Message
from textual.messages import CallbackType, Prune
from textual.notifications import SeverityLevel
from textual.reactive import Reactive
from textual.renderables.blank import Blank
from textual.rlock import RLock
from textual.selection import Selection
from textual.strip import Strip
from textual.style import Style as VisualStyle
from textual.visual import Visual, VisualType, visualize

if TYPE_CHECKING:
    from textual.app import App, ComposeResult
    from textual.css.query import QueryType
    from textual.message_pump import MessagePump
    from textual.scrollbar import (
        ScrollBar,
        ScrollBarCorner,
        ScrollDown,
        ScrollLeft,
        ScrollRight,
        ScrollTo,
        ScrollUp,
    )

_JUSTIFY_MAP: dict[str, JustifyMethod] = {
    "start": "left",
    "end": "right",
    "justify": "full",
}


_MOUSE_EVENTS_DISALLOW_IF_DISABLED = (events.MouseEvent, events.Enter, events.Leave)
_MOUSE_EVENTS_ALLOW_IF_DISABLED = (
    events.MouseScrollDown,
    events.MouseScrollUp,
    events.MouseScrollRight,
    events.MouseScrollLeft,
)


@rich.repr.auto
class AwaitMount:
    """An *optional* awaitable returned by [mount][textual.widget.Widget.mount] and [mount_all][textual.widget.Widget.mount_all].

    Example:
        ```python
        await self.mount(Static("foo"))
        ```
    """

    def __init__(self, parent: Widget, widgets: Sequence[Widget]) -> None:
        self._parent = parent
        self._widgets = widgets
        self._caller = get_caller_file_and_line()

    def __rich_repr__(self) -> rich.repr.Result:
        yield "parent", self._parent
        yield "widgets", self._widgets
        yield "caller", self._caller, None

    async def __call__(self) -> None:
        """Allows awaiting via a call operation."""
        await self

    def __await__(self) -> Generator[None, None, None]:
        async def await_mount() -> None:
            if self._widgets:
                aws = [
                    create_task(widget._mounted_event.wait(), name="await mount")
                    for widget in self._widgets
                ]
                if aws:
                    await wait(aws)
                    self._parent.refresh(layout=True)
                    try:
                        self._parent.app._update_mouse_over(self._parent.screen)
                    except NoScreen:
                        pass

        return await_mount().__await__()


class _Styled:
    """Apply a style to a renderable.

    Args:
        renderable: Any renderable.
        style: A style to apply across the entire renderable.
    """

    def __init__(
        self, renderable: "ConsoleRenderable", style: Style, link_style: Style | None
    ) -> None:
        self.renderable = renderable
        self.style = style
        self.link_style = link_style

    def __rich_console__(
        self, console: "Console", options: "ConsoleOptions"
    ) -> "RichRenderResult":
        style = console.get_style(self.style)
        result_segments = console.render(self.renderable, options)

        _Segment = Segment
        if style:
            apply = style.__add__
            result_segments = (
                _Segment(text, apply(_style), None)
                for text, _style, control in result_segments
            )
        link_style = self.link_style
        if link_style:
            result_segments = (
                _Segment(
                    text,
                    (
                        style
                        if style._meta is None
                        else (style + link_style if "@click" in style.meta else style)
                    ),
                    control,
                )
                for text, style, control in result_segments
                if style is not None
            )
        return result_segments

    def __rich_measure__(
        self, console: "Console", options: "ConsoleOptions"
    ) -> Measurement:
        return Measurement.get(console, options, self.renderable)


class _RenderCache(NamedTuple):
    """Stores results of a previous render."""

    size: Size
    """The size of the render."""
    lines: list[Strip]
    """Contents of the render."""


class WidgetError(Exception):
    """Base widget error."""


class MountError(WidgetError):
    """Error raised when there was a problem with the mount request."""


class PseudoClasses(NamedTuple):
    """Used for render/render_line based widgets that use caching. This structure can be used as a
    cache-key."""

    enabled: bool
    """Is 'enabled' applied?"""
    focus: bool
    """Is 'focus' applied?"""
    hover: bool
    """Is 'hover' applied?"""


class _BorderTitle:
    """Descriptor to set border titles."""

    def __set_name__(self, owner: Widget, name: str) -> None:
        # The private name where we store the real data.
        self._internal_name = f"_{name}"

    def __set__(self, obj: Widget, title: Text | ContentType | None) -> None:
        """Setting a title accepts a str, Text, or None."""
        if isinstance(title, Text):
            title = Content.from_rich_text(title)
        if title is None:
            setattr(obj, self._internal_name, None)
        else:
            # We store the title as Text
            new_title = obj.render_str(title).expand_tabs(4)
            new_title = new_title.split()[0]
            setattr(obj, self._internal_name, new_title)
        obj.refresh()

    def __get__(self, obj: Widget, objtype: type[Widget] | None = None) -> str | None:
        """Getting a title will return None or a str as console markup."""
        title: Text | None = getattr(obj, self._internal_name, None)
        if title is None:
            return None
        # If we have a title, convert from Text to console markup
        return title.markup


class BadWidgetName(Exception):
    """Raised when widget class names do not satisfy the required restrictions."""


@rich.repr.auto
class Widget(DOMNode):
    """
    A Widget is the base class for Textual widgets.

    See also [static][textual.widgets._static.Static] for starting point for your own widgets.
    """

    DEFAULT_CSS = """
    Widget{
        scrollbar-background: $scrollbar-background;
        scrollbar-background-hover: $scrollbar-background-hover;
        scrollbar-background-active: $scrollbar-background-active;
        scrollbar-color: $scrollbar;
        scrollbar-color-active: $scrollbar-active;
        scrollbar-color-hover: $scrollbar-hover;
        scrollbar-corner-color: $scrollbar-corner-color;
        scrollbar-size-vertical: 2;
        scrollbar-size-horizontal: 1;
        link-background: $link-background;
        link-color: $link-color;
        link-style: $link-style;
        link-background-hover: $link-background-hover;
        link-color-hover: $link-color-hover;
        link-style-hover: $link-style-hover;
        background: transparent;
    }
    """
    COMPONENT_CLASSES: ClassVar[set[str]] = set()

    BORDER_TITLE: ClassVar[str] = ""
    """Initial value for border_title attribute."""

    BORDER_SUBTITLE: ClassVar[str] = ""
    """Initial value for border_subtitle attribute."""

    ALLOW_MAXIMIZE: ClassVar[bool | None] = None
    """Defines default logic to allow the widget to be maximized.
    
    - `None` Use default behavior (Focusable widgets may be maximized)
    - `False` Do not allow widget to be maximized
    - `True` Allow widget to be maximized
    
    """

    ALLOW_SELECT: ClassVar[bool] = True
    """Does this widget support automatic text selection? May be further refined with [Widget.allow_select][textual.widget.Widget.allow_select]"""

    can_focus: bool = False
    """Widget may receive focus."""
    can_focus_children: bool = True
    """Widget's children may receive focus."""
    expand: Reactive[bool] = Reactive(False)
    """Rich renderable may expand beyond optimal size."""
    shrink: Reactive[bool] = Reactive(True)
    """Rich renderable may shrink below optimal size."""
    auto_links: Reactive[bool] = Reactive(True)
    """Widget will highlight links automatically."""
    disabled: Reactive[bool] = Reactive(False)
    """Is the widget disabled? Disabled widgets can not be interacted with, and are typically styled to look dimmer."""

    hover_style: Reactive[Style] = Reactive(Style, repaint=False)
    """The current hover style (style under the mouse cursor). Read only."""
    highlight_link_id: Reactive[str] = Reactive("")
    """The currently highlighted link id. Read only."""
    loading: Reactive[bool] = Reactive(False)
    """If set to `True` this widget will temporarily be replaced with a loading indicator."""

    virtual_size: Reactive[Size] = Reactive(Size(0, 0), layout=True)
    """The virtual (scrollable) [size][textual.geometry.Size] of the widget."""

    has_focus: Reactive[bool] = Reactive(False, repaint=False)
    """Does this widget have focus? Read only."""

    mouse_hover: Reactive[bool] = Reactive(False, repaint=False)
    """Is the mouse over this widget? Read only."""

    scroll_x: Reactive[float] = Reactive(0.0, repaint=False, layout=False)
    """The scroll position on the X axis."""

    scroll_y: Reactive[float] = Reactive(0.0, repaint=False, layout=False)
    """The scroll position on the Y axis."""

    scroll_target_x = Reactive(0.0, repaint=False)
    """Scroll target destination, X coord."""

    scroll_target_y = Reactive(0.0, repaint=False)
    """Scroll target destination, Y coord."""

    show_vertical_scrollbar: Reactive[bool] = Reactive(False, layout=True)
    """Show a vertical scrollbar?"""

    show_horizontal_scrollbar: Reactive[bool] = Reactive(False, layout=True)
    """Show a horizontal scrollbar?"""

    border_title = _BorderTitle()  # type: ignore
    """A title to show in the top border (if there is one)."""
    border_subtitle = _BorderTitle()
    """A title to show in the bottom border (if there is one)."""

    # Default sort order, incremented by constructor
    _sort_order: ClassVar[int] = 0

    _PSEUDO_CLASSES: ClassVar[dict[str, Callable[[Widget], bool]]] = {
        "hover": lambda widget: widget.mouse_hover,
        "focus": lambda widget: widget.has_focus,
        "blur": lambda widget: not widget.has_focus,
        "can-focus": lambda widget: widget.allow_focus(),
        "disabled": lambda widget: widget.is_disabled,
        "enabled": lambda widget: not widget.is_disabled,
        "dark": lambda widget: widget.app.current_theme.dark,
        "light": lambda widget: not widget.app.current_theme.dark,
        "focus-within": lambda widget: widget.has_focus_within,
        "inline": lambda widget: widget.app.is_inline,
        "ansi": lambda widget: widget.app.ansi_color,
        "nocolor": lambda widget: widget.app.no_color,
        "first-of-type": lambda widget: widget.first_of_type,
        "last-of-type": lambda widget: widget.last_of_type,
        "first-child": lambda widget: widget.first_child,
        "last-child": lambda widget: widget.last_child,
        "odd": lambda widget: widget.is_odd,
        "even": lambda widget: widget.is_even,
        "empty": lambda widget: widget.is_empty,
    }  # type: ignore[assignment]

    def __init__(
        self,
        *children: Widget,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
        markup: bool = True,
    ) -> None:
        """Initialize a Widget.

        Args:
            *children: Child widgets.
            name: The name of the widget.
            id: The ID of the widget in the DOM.
            classes: The CSS classes for the widget.
            disabled: Whether the widget is disabled or not.
            markup: Enable content markup?
        """
        self._render_markup = markup
        _null_size = NULL_SIZE
        self._size = _null_size
        self._container_size = _null_size
        self._layout_required = False
        self._repaint_required = False
        self._scroll_required = False
        self._recompose_required = False
        self._refresh_styles_required = False
        self._default_layout = VerticalLayout()
        self._animate: BoundAnimator | None = None
        Widget._sort_order += 1
        self.sort_order = Widget._sort_order
        self.highlight_style: Style | None = None

        self._vertical_scrollbar: ScrollBar | None = None
        self._horizontal_scrollbar: ScrollBar | None = None
        self._scrollbar_corner: ScrollBarCorner | None = None

        self._border_title: Content | None = None
        self._border_subtitle: Content | None = None

        self._layout_cache: dict[str, object] = {}
        """A dict that is refreshed when the widget is resized / refreshed."""

        self._render_cache = _RenderCache(_null_size, [])
        # Regions which need to be updated (in Widget)
        self._dirty_regions: set[Region] = set()
        # Regions which need to be transferred from cache to screen
        self._repaint_regions: set[Region] = set()

        # Cache the auto content dimensions
        self._content_width_cache: tuple[object, int] = (None, 0)
        self._content_height_cache: tuple[object, int] = (None, 0)

        self._arrangement_cache: FIFOCache[
            tuple[Size, int, Widget], DockArrangeResult
        ] = FIFOCache(4)

        self._styles_cache = StylesCache()
        self._rich_style_cache: dict[tuple[str, ...], tuple[Style, Style]] = {}
        self._visual_style_cache: dict[tuple[str, ...], VisualStyle] = {}

        self._tooltip: VisualType | None = None
        """The tooltip content."""
        self.absolute_offset: Offset | None = None
        """Force an absolute offset for the widget (used by tooltips)."""

        self._scrollbar_changes: set[tuple[bool, bool]] = set()
        """Used to stabilize scrollbars."""
        super().__init__(
            name=name,
            id=id,
            classes=self.DEFAULT_CLASSES if classes is None else classes,
        )

        if self in children:
            raise WidgetError("A widget can't be its own parent")

        for child in children:
            if not isinstance(child, Widget):
                raise TypeError(
                    f"Widget positional arguments must be Widget subclasses; not {child!r}"
                )
        self._pending_children = list(children)
        self.set_reactive(Widget.disabled, disabled)
        if self.BORDER_TITLE:
            self.border_title = self.BORDER_TITLE
        if self.BORDER_SUBTITLE:
            self.border_subtitle = self.BORDER_SUBTITLE

        self.lock = RLock()
        """`asyncio` lock to be used to synchronize the state of the widget.

        Two different tasks might call methods on a widget at the same time, which
        might result in a race condition.
        This can be fixed by adding `async with widget.lock:` around the method calls.
        """
        self._anchored: bool = False
        """Has this widget been anchored?"""
        self._anchor_released: bool = False
        """Has the anchor been released?"""

        """Flag to enable animation when scrolling anchored widgets."""
        self._cover_widget: Widget | None = None
        """Widget to render over this widget (used by loading indicator)."""

        self._first_of_type: tuple[int, bool] = (-1, False)
        """Used to cache :first-of-type pseudoclass state."""
        self._last_of_type: tuple[int, bool] = (-1, False)
        """Used to cache :last-of-type pseudoclass state."""
        self._first_child: tuple[int, bool] = (-1, False)
        """Used to cache :first-child pseudoclass state."""
        self._last_child: tuple[int, bool] = (-1, False)
        """Used to cache :last-child pseudoclass state."""
        self._odd: tuple[int, bool] = (-1, False)
        """Used to cache :odd pseudoclass state."""
        self._last_scroll_time = monotonic()
        """Time of last scroll."""
        self._extrema = Extrema()
        """Optional minimum and maximum values for width and height."""

    @property
    def is_mounted(self) -> bool:
        """Check if this widget is mounted."""
        return self._is_mounted

    @property
    def siblings(self) -> list[Widget]:
        """Get the widget's siblings (self is removed from the return list).

        Returns:
            A list of siblings.
        """
        parent = self.parent
        if parent is not None:
            siblings = list(parent._nodes)
            siblings.remove(self)
            return siblings
        else:
            return []

    @property
    def visible_siblings(self) -> list[Widget]:
        """A list of siblings which will be shown.

        Returns:
            List of siblings.
        """
        siblings = [
            widget for widget in self.siblings if widget.visible and widget.display
        ]
        return siblings

    @property
    def allow_vertical_scroll(self) -> bool:
        """Check if vertical scroll is permitted.

        May be overridden if you want different logic regarding allowing scrolling.
        """
        if self._check_disabled():
            return False
        return self.is_scrollable and self.show_vertical_scrollbar

    @property
    def allow_horizontal_scroll(self) -> bool:
        """Check if horizontal scroll is permitted.

        May be overridden if you want different logic regarding allowing scrolling.
        """
        if self._check_disabled():
            return False
        return self.is_scrollable and self.show_horizontal_scrollbar

    @property
    def _allow_scroll(self) -> bool:
        """Check if both axis may be scrolled.

        Returns:
            True if horizontal and vertical scrolling is enabled.
        """
        return self.is_scrollable and (
            self.allow_horizontal_scroll or self.allow_vertical_scroll
        )

    @property
    def allow_maximize(self) -> bool:
        """Check if the widget may be maximized.

        Returns:
            `True` if the widget may be maximized, or `False` if it should not be maximized.
        """
        return self.can_focus if self.ALLOW_MAXIMIZE is None else self.ALLOW_MAXIMIZE

    @property
    def offset(self) -> Offset:
        """Widget offset from origin.

        Returns:
            Relative offset.
        """
        return self.styles.offset.resolve(self.size, self.app.size)

    @offset.setter
    def offset(self, offset: tuple[int, int]) -> None:
        self.styles.offset = ScalarOffset.from_offset(offset)

    @property
    def opacity(self) -> float:
        """Total opacity of widget."""
        opacity = 1.0
        for node in reversed(self.ancestors_with_self):
            opacity *= node.styles.opacity
            if not opacity:
                break
        return opacity

    @property
    def is_anchored(self) -> bool:
        """Is this widget anchored?

        See [anchor()][textual.widget.Widget.anchor] for an explanation of anchoring.

        """
        return self._anchored

    @property
    def is_mouse_over(self) -> bool:
        """Is the mouse currently over this widget?

        Note this will be `True` if the mouse pointer is within the widget's region, even if
        the mouse pointer is not directly over the widget (there could be another widget between
        the mouse pointer and self).

        """
        if not self.screen.is_active:
            return False
        for widget, _ in self.screen.get_widgets_at(*self.app.mouse_position):
            if widget is self:
                return True
        return False

    @property
    def is_maximized(self) -> bool:
        """Is this widget maximized?"""
        try:
            return self.screen.maximized is self
        except NoScreen:
            return False

    @property
    def is_in_maximized_view(self) -> bool:
        """Is this widget, or a parent maximized?"""
        maximized = self.screen.maximized
        if not maximized:
            return False
        for node in self.ancestors_with_self:
            if maximized is node:
                return True
        return False

    @property
    def _render_widget(self) -> Widget:
        """The widget the compositor should render."""
        # Will return the "cover widget" if one is set, otherwise self.
        return self._cover_widget if self._cover_widget is not None else self

    @property
    def text_selection(self) -> Selection | None:
        """Text selection information, or `None` if no text is selected in this widget."""
        return self.screen.selections.get(self, None)

    def preflight_checks(self) -> None:
        """Called in debug mode to do preflight checks.

        This is used by Textual to log some common errors, but you could implement this
        in custom widgets to perform additional checks.

        """

        if hasattr(self, "CSS"):
            from textual.screen import Screen

            if not isinstance(self, Screen):
                self.log.warning(
                    f"'{self.__class__.__name__}.CSS' will be ignored (use 'DEFAULT_CSS' class variable for widgets)"
                )

    def _cover(self, widget: Widget) -> None:
        """Set a widget used to replace the visuals of this widget (used for loading indicator).

        Args:
            widget: A newly constructed, but unmounted widget.
        """
        self._uncover()
        self._cover_widget = widget
        widget._parent = self
        widget._start_messages()
        widget._post_register(self.app)
        self.app.stylesheet.apply(widget)
        self.refresh(layout=True)

    def _uncover(self) -> None:
        """Remove any widget, previously set via [`_cover`][textual.widget.Widget._cover]."""
        if self._cover_widget is not None:
            self._cover_widget.remove()
            self._cover_widget = None
            self.refresh(layout=True)

    def anchor(self, anchor: bool = True) -> None:
        """Anchor a scrollable widget.

        An anchored widget will stay scrolled the bottom when new content is added, until
        the user moves the scroll position.

        Args:
            anchor: Anchor the widget if `True`, clear the anchor if `False`.

        """
        self._anchored = anchor
        if anchor:
            self.scroll_end(immediate=True, animate=False)

    def release_anchor(self) -> None:
        """Release the [anchor][textual.widget.Widget].

        If a widget is anchored, releasing the anchor will allow the user to scroll as normal.

        """
        self.scroll_target_y = self.scroll_y
        self._anchor_released = True

    def _check_anchor(self) -> None:
        """Check if the scroll position is near enough to the bottom to restore anchor."""
        if (
            self._anchored
            and self._anchor_released
            and self.scroll_y >= self.max_scroll_y
        ):
            self._anchor_released = False

    def _check_disabled(self) -> bool:
        """Check if the widget is disabled either explicitly by setting `disabled`,
        or implicitly by setting `loading`.

        Returns:
            True if the widget should be disabled.
        """
        return self.disabled or self.loading

    @property
    def tooltip(self) -> VisualType | None:
        """Tooltip for the widget, or `None` for no tooltip."""
        return self._tooltip

    @tooltip.setter
    def tooltip(self, tooltip: VisualType | None):
        self._tooltip = tooltip
        try:
            self.screen._update_tooltip(self)
        except NoScreen:
            pass

    def with_tooltip(self, tooltip: Visual | RenderableType | None) -> Self:
        """Chainable method to set a tooltip.

        Example:
            ```python
            def compose(self) -> ComposeResult:
                yield Label("Hello").with_tooltip("A greeting")
            ```

        Args:
            tooltip: New tooltip, or `None` to clear the tooltip.

        Returns:
            Self.
        """
        self.tooltip = tooltip
        return self

    def allow_focus(self) -> bool:
        """Check if the widget is permitted to focus.

        The base class returns [`can_focus`][textual.widget.Widget.can_focus].
        This method may be overridden if additional logic is required.

        Returns:
            `True` if the widget may be focused, or `False` if it may not be focused.
        """
        return self.can_focus

    def allow_focus_children(self) -> bool:
        """Check if a widget's children may be focused.

        The base class returns [`can_focus_children`][textual.widget.Widget.can_focus_children].
        This method may be overridden if additional logic is required.

        Returns:
            `True` if the widget's children may be focused, or `False` if the widget's children may not be focused.
        """
        return self.can_focus_children

    def compose_add_child(self, widget: Widget) -> None:
        """Add a node to children.

        This is used by the compose process when it adds children.
        There is no need to use it directly, but you may want to override it in a subclass
        if you want children to be attached to a different node.

        Args:
            widget: A Widget to add.
        """
        _rich_traceback_omit = True
        self._pending_children.append(widget)

    @property
    def is_disabled(self) -> bool:
        """Is the widget disabled either because `disabled=True` or an ancestor has `disabled=True`."""
        node: MessagePump | None = self
        while isinstance(node, Widget):
            if node.disabled:
                return True
            node = node._parent
        return False

    @property
    def has_focus_within(self) -> bool:
        """Are any descendants focused?"""
        try:
            focused = self.screen.focused
        except NoScreen:
            return False
        node = focused
        while node is not None:
            if node is self:
                return True
            node = node._parent
        return False

    @property
    def first_of_type(self) -> bool:
        """Is this the first widget of its type in its siblings?"""
        parent = self.parent
        if parent is None:
            return True
        # This pseudo classes only changes when the parent's nodes._updates changes
        if parent._nodes._updates == self._first_of_type[0]:
            return self._first_of_type[1]
        widget_type = type(self)
        for node in parent._nodes.displayed:
            if isinstance(node, widget_type):
                self._first_of_type = (parent._nodes._updates, node is self)
                return self._first_of_type[1]
        return False

    @property
    def last_of_type(self) -> bool:
        """Is this the last widget of its type in its siblings?"""
        parent = self.parent
        if parent is None:
            return True
        # This pseudo classes only changes when the parent's nodes._updates changes
        if parent._nodes._updates == self._last_of_type[0]:
            return self._last_of_type[1]
        widget_type = type(self)
        for node in parent._nodes.displayed_reverse:
            if isinstance(node, widget_type):
                self._last_of_type = (parent._nodes._updates, node is self)
                return self._last_of_type[1]
        return False

    @property
    def first_child(self) -> bool:
        """Is this the first widget in its siblings?"""
        parent = self.parent
        if parent is None:
            return True
        # This pseudo class only changes when the parent's nodes._updates changes
        if parent._nodes._updates == self._first_child[0]:
            return self._first_child[1]
        for node in parent._nodes.displayed:
            self._first_child = (parent._nodes._updates, node is self)
            return self._first_child[1]
        return False

    @property
    def last_child(self) -> bool:
        """Is this the last widget in its siblings?"""
        parent = self.parent
        if parent is None:
            return True
        # This pseudo class only changes when the parent's nodes._updates changes
        if parent._nodes._updates == self._last_child[0]:
            return self._last_child[1]
        for node in parent._nodes.displayed_reverse:
            self._last_child = (parent._nodes._updates, node is self)
            return self._last_child[1]
        return False

    @property
    def is_odd(self) -> bool:
        """Is this widget at an oddly numbered position within its siblings?"""
        parent = self.parent
        if parent is None:
            return True
        # This pseudo classes only changes when the parent's nodes._updates changes
        if parent._nodes._updates == self._odd[0]:
            return self._odd[1]
        try:
            is_odd = parent._nodes.index(self) % 2 == 0
            self._odd = (parent._nodes._updates, is_odd)
            return is_odd
        except ValueError:
            return False

    @property
    def is_even(self) -> bool:
        """Is this widget at an evenly numbered position within its siblings?"""
        return not self.is_odd

    def __enter__(self) -> Self:
        """Use as context manager when composing."""
        self.app._compose_stacks[-1].append(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        """Exit compose context manager."""
        compose_stack = self.app._compose_stacks[-1]
        composed = compose_stack.pop()
        if compose_stack:
            compose_stack[-1].compose_add_child(composed)
        else:
            self.app._composed[-1].append(composed)

    def clear_cached_dimensions(self) -> None:
        """Clear cached results of `get_content_width` and `get_content_height`.

        Call if the widget's renderable changes size after the widget has been created.

        !!! note

            This is not required if you are extending [`Static`][textual.widgets.Static].

        """
        self._content_width_cache = (None, 0)
        self._content_height_cache = (None, 0)

    def get_loading_widget(self) -> Widget:
        """Get a widget to display a loading indicator.

        The default implementation will defer to App.get_loading_widget.

        Returns:
            A widget in place of this widget to indicate a loading.
        """
        loading_widget = self.app.get_loading_widget()
        return loading_widget

    def set_loading(self, loading: bool) -> None:
        """Set or reset the loading state of this widget.

        A widget in a loading state will display a `LoadingIndicator` or a custom widget
        set through overriding the `get_loading_widget` method.

        Args:
            loading: `True` to put the widget into a loading state, or `False` to reset the loading state.
        """
        if loading:
            loading_indicator = self.get_loading_widget()
            loading_indicator.add_class("-textual-loading-indicator")
            self._cover(loading_indicator)
        else:
            self._uncover()

    def _watch_loading(self, loading: bool) -> None:
        """Called when the 'loading' reactive is changed."""
        self.set_loading(loading)

    ExpectType = TypeVar("ExpectType", bound="Widget")

    if TYPE_CHECKING:

        @overload
        def get_child_by_id(self, id: str) -> Widget: ...

        @overload
        def get_child_by_id(
            self, id: str, expect_type: type[ExpectType]
        ) -> ExpectType: ...

    def get_child_by_id(
        self, id: str, expect_type: type[ExpectType] | None = None
    ) -> ExpectType | Widget:
        """Return the first child (immediate descendent) of this node with the given ID.

        Args:
            id: The ID of the child.
            expect_type: Require the object be of the supplied type, or None for any type.

        Returns:
            The first child of this node with the ID.

        Raises:
            NoMatches: if no children could be found for this ID
            WrongType: if the wrong type was found.
        """
        child = self._get_dom_base()._nodes._get_by_id(id)
        if child is None:
            raise NoMatches(f"No child found with id={id!r}")
        if expect_type is None:
            return child
        if not isinstance(child, expect_type):
            raise WrongType(
                f"Child with id={id!r} is the wrong type; expected type {expect_type.__name__!r}, found {child}"
            )
        return child

    if TYPE_CHECKING:

        @overload
        def get_widget_by_id(self, id: str) -> Widget: ...

        @overload
        def get_widget_by_id(
            self, id: str, expect_type: type[ExpectType]
        ) -> ExpectType: ...

    def get_widget_by_id(
        self, id: str, expect_type: type[ExpectType] | None = None
    ) -> ExpectType | Widget:
        """Return the first descendant widget with the given ID.

        Performs a depth-first search rooted at this widget.

        Args:
            id: The ID to search for in the subtree.
            expect_type: Require the object be of the supplied type, or None for any type.

        Returns:
            The first descendant encountered with this ID.

        Raises:
            NoMatches: if no children could be found for this ID.
            WrongType: if the wrong type was found.
        """

        widget = self.query_one(f"#{id}")
        if expect_type is not None and not isinstance(widget, expect_type):
            raise WrongType(
                f"Descendant with id={id!r} is the wrong type; expected type {expect_type.__name__!r}, found {widget}"
            )
        return widget

    def get_child_by_type(self, expect_type: type[ExpectType]) -> ExpectType:
        """Get the first immediate child of a given type.

        Only returns exact matches, and so will not match subclasses of the given type.

        Args:
            expect_type: The type of the child to search for.

        Raises:
            NoMatches: If no matching child is found.

        Returns:
            The first immediate child widget with the expected type.
        """
        for child in self._nodes:
            # We want the child with the exact type (not subclasses)
            if type(child) is expect_type:
                assert isinstance(child, expect_type)
                return child
        raise NoMatches(f"No immediate child of type {expect_type}; {self._nodes}")

    def get_component_rich_style(self, *names: str, partial: bool = False) -> Style:
        """Get a *Rich* style for a component.

        Args:
            names: Names of components.
            partial: Return a partial style (not combined with parent).

        Returns:
            A Rich style object.
        """

        if names not in self._rich_style_cache:
            component_styles = self.get_component_styles(*names)
            style = component_styles.rich_style
            text_opacity = component_styles.text_opacity
            if text_opacity < 1 and style.bgcolor is not None:
                style += Style.from_color(
                    (
                        Color.from_rich_color(style.bgcolor)
                        + component_styles.color.multiply_alpha(text_opacity)
                    ).rich_color
                )
            partial_style = component_styles.partial_rich_style
            self._rich_style_cache[names] = (style, partial_style)

        style, partial_style = self._rich_style_cache[names]

        return partial_style if partial else style

    def get_visual_style(
        self, *component_classes: str, partial: bool = False
    ) -> VisualStyle:
        """Get the visual style for the widget, including any component styles.

        Args:
            component_classes: Optional component styles.
            partial: Return a partial style (not combined with parent).

        Returns:
            A Visual style instance.

        """
        cache_key = (self._pseudo_classes_cache_key, component_classes, partial)
        if (visual_style := self._visual_style_cache.get(cache_key, None)) is None:
            background = Color(0, 0, 0, 0)
            color = Color(255, 255, 255, 0)

            style = Style()
            opacity = 1.0

            def iter_styles() -> Iterable[StylesBase]:
                """Iterate over the styles from the DOM and additional components styles."""
                if partial:
                    node = self
                else:
                    for node in reversed(self.ancestors_with_self):
                        yield node.styles
                for name in component_classes:
                    yield node.get_component_styles(name)

            for styles in iter_styles():
                has_rule = styles.has_rule
                opacity *= styles.opacity
                if has_rule("background"):
                    text_background = background + styles.background.tint(
                        styles.background_tint
                    )
                    if partial:
                        background_tint = styles.background.tint(styles.background_tint)
                        background = background.blend(
                            background_tint, 1 - background_tint.a
                        ).multiply_alpha(opacity)
                    else:
                        background += (
                            styles.background.tint(styles.background_tint)
                        ).multiply_alpha(opacity)
                else:
                    text_background = background
                if has_rule("color"):
                    color = styles.color.multiply_alpha(styles.text_opacity)
                style += styles.text_style
                if has_rule("auto_color") and styles.auto_color:
                    color = text_background.get_contrast_text(color.a)

            visual_style = VisualStyle(
                background,
                color,
                bold=style.bold,
                dim=style.dim,
                italic=style.italic,
                underline=style.underline,
                strike=style.strike,
            )
            self._visual_style_cache[cache_key] = visual_style

        return visual_style

    def _get_style(self, style: VisualStyle | str) -> VisualStyle:
        """A get_style method for use in Content.

        Args:
            style: A style prefixed with a dot.

        Returns:
            A visual style if one is fund, otherwise `None`.
        """
        if isinstance(style, VisualStyle):
            return style
        visual_style = VisualStyle.null()
        if style.startswith("."):
            for node in self.ancestors_with_self:
                if not isinstance(node, Widget):
                    break
                try:
                    visual_style = node.get_visual_style(style[1:], partial=True)
                    break
                except KeyError:
                    continue
            else:
                raise KeyError(f"No matching component class found for '{style}'")
            return visual_style
        try:
            visual_style = VisualStyle.parse(style)
        except Exception:
            pass
        return visual_style

    @overload
    def render_str(self, text_content: str) -> Content: ...

    @overload
    def render_str(self, text_content: Content) -> Content: ...

    def render_str(self, text_content: str | Content) -> Content:
        """Convert str into a [Content][textual.content.Content] instance.

        If you pass in an existing Content instance it will be returned unaltered.

        Args:
            text_content: Content or str.

        Returns:
            Content object.
        """
        if isinstance(text_content, Content):
            return text_content
        return Content.from_markup(text_content)

    def _arrange(self, size: Size, optimal: bool = False) -> DockArrangeResult:
        """Arrange children.

        Args:
            size: Size of container.

        Returns:
            Widget locations.
        """
        cache_key = (size, self._nodes._updates, optimal)
        cached_result = self._arrangement_cache.get(cache_key)
        if cached_result is not None:
            return cached_result

        arrangement = self._arrangement_cache[cache_key] = arrange(
            self, self._nodes, size, self.screen.size, optimal=optimal
        )

        return arrangement

    def _clear_arrangement_cache(self) -> None:
        """Clear arrangement cache, forcing a new arrange operation."""
        self._arrangement_cache.clear()

    def _get_virtual_dom(self) -> Iterable[Widget]:
        """Get widgets not part of the DOM.

        Returns:
            An iterable of Widgets.
        """
        if self._horizontal_scrollbar is not None:
            yield self._horizontal_scrollbar
        if self._vertical_scrollbar is not None:
            yield self._vertical_scrollbar
        if self._scrollbar_corner is not None:
            yield self._scrollbar_corner

    def _find_mount_point(self, spot: int | str | "Widget") -> tuple["Widget", int]:
        """Attempt to locate the point where the caller wants to mount something.

        Args:
            spot: The spot to find.

        Returns:
            The parent and the location in its child list.

        Raises:
            MountError: If there was an error finding where to mount a widget.

        The rules of this method are:

        - Given an ``int``, parent is ``self`` and location is the integer value.
        - Given a ``Widget``, parent is the widget's parent and location is
          where the widget is found in the parent's ``children``. If it
          can't be found a ``MountError`` will be raised.
        - Given a string, it is used to perform a ``query_one`` and then the
          result is used as if a ``Widget`` had been given.
        """

        # A numeric location means at that point in our child list.
        if isinstance(spot, int):
            return self, spot

        # If we've got a string, that should be treated like a query that
        # can be passed to query_one. So let's use that to get a widget to
        # work on.
        if isinstance(spot, str):
            spot = self.query_exactly_one(spot, Widget)

        # At this point we should have a widget, either because we got given
        # one, or because we pulled one out of the query. First off, does it
        # have a parent? There's no way we can use it as a sibling to make
        # mounting decisions if it doesn't have a parent.
        if spot.parent is None:
            raise MountError(
                f"Unable to find relative location of {spot!r} because it has no parent"
            )

        # We've got a widget. It has a parent. It has (zero or more)
        # children. We should be able to go looking for the widget's
        # location amongst its parent's children.
        try:
            return cast("Widget", spot.parent), spot.parent._nodes.index(spot)
        except ValueError:
            raise MountError(f"{spot!r} is not a child of {self!r}") from None

    def mount(
        self,
        *widgets: Widget,
        before: int | str | Widget | None = None,
        after: int | str | Widget | None = None,
    ) -> AwaitMount:
        """Mount widgets below this widget (making this widget a container).

        Args:
            *widgets: The widget(s) to mount.
            before: Optional location to mount before. An `int` is the index
                of the child to mount before, a `str` is a `query_one` query to
                find the widget to mount before.
            after: Optional location to mount after. An `int` is the index
                of the child to mount after, a `str` is a `query_one` query to
                find the widget to mount after.

        Returns:
            An awaitable object that waits for widgets to be mounted.

        Raises:
            MountError: If there is a problem with the mount request.

        Note:
            Only one of ``before`` or ``after`` can be provided. If both are
            provided a ``MountError`` will be raised.
        """
        if self._closing or self._pruning:
            return AwaitMount(self, [])
        if not self.is_attached:
            raise MountError(f"Can't mount widget(s) before {self!r} is mounted")
        # Check for duplicate IDs in the incoming widgets
        ids_to_mount = [
            widget_id for widget in widgets if (widget_id := widget.id) is not None
        ]
        if len(set(ids_to_mount)) != len(ids_to_mount):
            counter = Counter(ids_to_mount)
            for widget_id, count in counter.items():
                if count > 1:
                    raise MountError(
                        f"Tried to insert {count!r} widgets with the same ID {widget_id!r}. "
                        "Widget IDs must be unique."
                    )

        # Saying you want to mount before *and* after something is an error.
        if before is not None and after is not None:
            raise MountError(
                "Only one of `before` or `after` can be handled -- not both"
            )

        # Decide the final resting place depending on what we've been asked
        # to do.
        insert_before: int | None = None
        insert_after: int | None = None
        if before is not None:
            parent, insert_before = self._find_mount_point(before)
        elif after is not None:
            parent, insert_after = self._find_mount_point(after)
        else:
            parent = self

        mounted = self.app._register(
            parent, *widgets, before=insert_before, after=insert_after
        )

        def update_styles(children: list[DOMNode]) -> None:
            """Update order related CSS"""
            if before is not None or after is not None:
                # If the new children aren't at the end.
                # we need to update both odd/even, first-of-type/last-of-type and first-child/last-child
                for child in children:
                    if child._has_order_style or child._has_odd_or_even:
                        child._update_styles()
            else:
                for child in children:
                    if child._has_order_style:
                        child._update_styles()

        self.call_later(update_styles, list(self.children))
        await_mount = AwaitMount(self, mounted)
        self.call_next(await_mount)

        return await_mount

    def _refresh_styles(self) -> None:
        """Request refresh of styles on idle."""
        self._refresh_styles_required = True
        self.check_idle()

    def mount_all(
        self,
        widgets: Iterable[Widget],
        *,
        before: int | str | Widget | None = None,
        after: int | str | Widget | None = None,
    ) -> AwaitMount:
        """Mount widgets from an iterable.

        Args:
            widgets: An iterable of widgets.
            before: Optional location to mount before. An `int` is the index
                of the child to mount before, a `str` is a `query_one` query to
                find the widget to mount before.
            after: Optional location to mount after. An `int` is the index
                of the child to mount after, a `str` is a `query_one` query to
                find the widget to mount after.

        Returns:
            An awaitable object that waits for widgets to be mounted.

        Raises:
            MountError: If there is a problem with the mount request.

        Note:
            Only one of ``before`` or ``after`` can be provided. If both are
            provided a ``MountError`` will be raised.
        """
        if self.app._exit:
            return AwaitMount(self, [])
        await_mount = self.mount(*widgets, before=before, after=after)
        return await_mount

    if TYPE_CHECKING:

        @overload
        def move_child(
            self,
            child: int | Widget,
            *,
            before: int | Widget,
            after: None = None,
        ) -> None: ...

        @overload
        def move_child(
            self,
            child: int | Widget,
            *,
            after: int | Widget,
            before: None = None,
        ) -> None: ...

    def move_child(
        self,
        child: int | Widget,
        *,
        before: int | Widget | None = None,
        after: int | Widget | None = None,
    ) -> None:
        """Move a child widget within its parent's list of children.

        Args:
            child: The child widget to move.
            before: Child widget or location index to move before.
            after: Child widget or location index to move after.

        Raises:
            WidgetError: If there is a problem with the child or target.

        Note:
            Only one of `before` or `after` can be provided. If neither
            or both are provided a `WidgetError` will be raised.
        """

        # One or the other of before or after are required. Can't do
        # neither, can't do both.
        if before is None and after is None:
            raise WidgetError("One of `before` or `after` is required.")
        elif before is not None and after is not None:
            raise WidgetError("Only one of `before` or `after` can be handled.")

        def _to_widget(child: int | Widget, called: str) -> Widget:
            """Ensure a given child reference is a Widget."""
            if isinstance(child, int):
                try:
                    child = self._nodes[child]
                except IndexError:
                    raise WidgetError(
                        f"An index of {child} for the child to {called} is out of bounds"
                    ) from None
            else:
                # We got an actual widget, so let's be sure it really is one of
                # our children.
                try:
                    _ = self._nodes.index(child)
                except ValueError:
                    raise WidgetError(f"{child!r} is not a child of {self!r}") from None
            return child

        # Ensure the child and target are widgets.
        child = _to_widget(child, "move")
        target = _to_widget(
            cast("int | Widget", before if after is None else after), "move towards"
        )

        if child is target:
            return  # Nothing to be done.

        # At this point we should know what we're moving, and it should be a
        # child; where we're moving it to, which should be within the child
        # list; and how we're supposed to move it. All that's left is doing
        # the right thing.
        self._nodes._remove(child)
        if before is not None:
            self._nodes._insert(self._nodes.index(target), child)
        else:
            self._nodes._insert(self._nodes.index(target) + 1, child)

        # Request a refresh.
        self.refresh(layout=True)

    def compose(self) -> ComposeResult:
        """Called by Textual to create child widgets.

        This method is called when a widget is mounted or by setting `recompose=True` when
        calling [`refresh()`][textual.widget.Widget.refresh].

        Note that you don't typically need to explicitly call this method.

        Example:
            ```python
            def compose(self) -> ComposeResult:
                yield Header()
                yield Label("Press the button below:")
                yield Button()
                yield Footer()
            ```
        """
        yield from ()

    async def _check_recompose(self) -> None:
        """Check if a recompose is required."""
        if self._recompose_required:
            self._recompose_required = False
            await self.recompose()

    async def recompose(self) -> None:
        """Recompose the widget.

        Recomposing will remove children and call `self.compose` again to remount.
        """
        if not self.is_attached or self._pruning:
            return

        async with self.batch():
            await self.query_children("*").exclude(".-textual-system").remove()
            if self.is_attached:
                compose_nodes = compose(self)
                await self.mount_all(compose_nodes)

    def _post_register(self, app: App) -> None:
        """Called when the instance is registered.

        Args:
            app: App instance.
        """
        # Parse the Widget's CSS
        for read_from, css, tie_breaker, scope in self._get_default_css():
            self.app.stylesheet.add_source(
                css,
                read_from=read_from,
                is_default_css=True,
                tie_breaker=tie_breaker,
                scope=scope,
            )
        if app.debug:
            app.call_next(self.preflight_checks)

    def _get_box_model(
        self,
        container: Size,
        viewport: Size,
        width_fraction: Fraction,
        height_fraction: Fraction,
        constrain_width: bool = False,
        greedy: bool = True,
    ) -> BoxModel:
        """Process the box model for this widget.

        Args:
            container: The size of the container widget (with a layout).
            viewport: The viewport size.
            width_fraction: A fraction used for 1 `fr` unit on the width dimension.
            height_fraction: A fraction used for 1 `fr` unit on the height dimension.
            constrain_width: Restrict the width to the container width.

        Returns:
            The size and margin for this widget.
        """
        styles = self.styles
        is_border_box = styles.box_sizing == "border-box"
        gutter = styles.gutter  # Padding plus border
        margin = styles.margin

        styles_width = styles.width
        if not greedy and styles_width is not None and styles_width.is_fraction:
            styles_width = Scalar.parse("auto")
        is_auto_width = styles_width and styles_width.is_auto
        is_auto_height = styles.height and styles.height.is_auto

        # Container minus padding and border
        content_container = container - gutter.totals

        extrema = self._extrema = self._resolve_extrema(
            container, viewport, width_fraction, height_fraction
        )
        min_width, max_width, min_height, max_height = extrema

        if styles_width is None:
            # No width specified, fill available space
            content_width = Fraction(content_container.width - margin.width)
        elif is_auto_width:
            # When width is auto, we want enough space to always fit the content
            content_width = Fraction(
                self.get_content_width(content_container - margin.totals, viewport)
            )
            if (
                styles.overflow_x == "auto" and styles.scrollbar_gutter == "stable"
            ) or self.show_vertical_scrollbar:
                content_width += styles.scrollbar_size_vertical
            if (
                content_width < content_container.width
                and self._has_relative_children_width
            ):
                content_width = Fraction(content_container.width)
        else:
            # An explicit width
            content_width = styles_width.resolve(
                container - margin.totals, viewport, width_fraction
            )
            if is_border_box:
                content_width -= gutter.width

        if min_width is not None:
            # Restrict to minimum width, if set
            content_width = max(content_width, min_width, Fraction(0))

        if max_width is not None and not (
            container.width == 0
            and not styles.max_width.is_cells
            and self._parent is not None
            and self._parent.styles.is_auto_width
        ):
            # Restrict to maximum width, if set
            content_width = min(content_width, max_width)

        content_width = max(Fraction(0), content_width)

        if constrain_width:
            content_width = min(Fraction(container.width - gutter.width), content_width)

        if styles.height is None:
            # No height specified, fill the available space
            content_height = Fraction(content_container.height - margin.height)
        elif is_auto_height:
            # Calculate dimensions based on content
            content_height = Fraction(
                self.get_content_height(
                    content_container - margin.totals,
                    viewport,
                    int(content_width),
                )
            )
            if (
                styles.overflow_y == "auto" and styles.scrollbar_gutter == "stable"
            ) or self.show_horizontal_scrollbar:
                content_height += styles.scrollbar_size_horizontal
            if (
                content_height < content_container.height
                and self._has_relative_children_height
            ):
                content_height = Fraction(content_container.height)
        else:
            styles_height = styles.height
            # Explicit height set
            content_height = styles_height.resolve(
                container - margin.totals, viewport, height_fraction
            )
            if is_border_box:
                content_height -= gutter.height

        if min_height is not None:
            # Restrict to minimum height, if set
            content_height = max(content_height, min_height, Fraction(0))

        if max_height is not None and not (
            container.height == 0
            and not styles.max_height.is_cells
            and self._parent is not None
            and self._parent.styles.is_auto_height
        ):
            content_height = min(content_height, max_height)

        content_height = max(Fraction(0), content_height)
        model = BoxModel(
            content_width + gutter.width, content_height + gutter.height, margin
        )
        return model

    def get_content_width(self, container: Size, viewport: Size) -> int:
        """Called by textual to get the width of the content area. May be overridden in a subclass.

        Args:
            container: Size of the container (immediate parent) widget.
            viewport: Size of the viewport.

        Returns:
            The optimal width of the content.
        """

        if self.is_container:
            width = self.layout.get_content_width(self, container, viewport)
            return width

        cache_key = container.width
        if self._content_width_cache[0] == cache_key:
            return self._content_width_cache[1]

        visual = self._render()
        width = visual.get_optimal_width(self.styles, container.width)

        if self.expand:
            width = max(container.width, width)
        if self.shrink:
            width = min(width, container.width)

        self._content_width_cache = (cache_key, width)

        return width

    def get_content_height(self, container: Size, viewport: Size, width: int) -> int:
        """Called by Textual to get the height of the content area. May be overridden in a subclass.

        Args:
            container: Size of the container (immediate parent) widget.
            viewport: Size of the viewport.
            width: Width of renderable.

        Returns:
            The height of the content.
        """
        if not width:
            return 0
        if self.is_container:
            assert self.layout is not None
            height = self.layout.get_content_height(
                self,
                container,
                viewport,
                width,
            )
        else:
            cache_key = width

            if self._content_height_cache[0] == cache_key:
                return self._content_height_cache[1]

            visual = self._render()
            height = visual.get_height(self.styles, width)
            self._content_height_cache = (cache_key, height)

        return height

    def watch_hover_style(
        self, previous_hover_style: Style, hover_style: Style
    ) -> None:
        # TODO: This will cause the widget to refresh, even when there are no links
        # Can we avoid this?
        if self.auto_links:
            self.highlight_link_id = hover_style.link_id

    def watch_scroll_x(self, old_value: float, new_value: float) -> None:
        self.horizontal_scrollbar.position = new_value
        if round(old_value) != round(new_value):
            self._refresh_scroll()

    def watch_scroll_y(self, old_value: float, new_value: float) -> None:
        self.vertical_scrollbar.position = new_value
        if self._anchored and self._anchor_released:
            self._check_anchor()
        if round(old_value) != round(new_value):
            self._refresh_scroll()

    def validate_scroll_x(self, value: float) -> float:
        return clamp(value, 0, self.max_scroll_x)

    def validate_scroll_target_x(self, value: float) -> float:
        return round(clamp(value, 0, self.max_scroll_x))

    def validate_scroll_y(self, value: float) -> float:
        return clamp(value, 0, self.max_scroll_y)

    def validate_scroll_target_y(self, value: float) -> float:
        return round(clamp(value, 0, self.max_scroll_y))

    @property
    def max_scroll_x(self) -> int:
        """The maximum value of `scroll_x`."""
        return max(
            0,
            self.virtual_size.width
            - (self.container_size.width - self.scrollbar_size_vertical),
        )

    @property
    def max_scroll_y(self) -> int:
        """The maximum value of `scroll_y`."""
        return max(
            0,
            self.virtual_size.height
            - (self.container_size.height - self.scrollbar_size_horizontal),
        )

    @property
    def is_vertical_scroll_end(self) -> bool:
        """Is the vertical scroll position at the maximum?"""
        return self.scroll_offset.y == self.max_scroll_y or not self.size

    @property
    def is_horizontal_scroll_end(self) -> bool:
        """Is the horizontal scroll position at the maximum?"""
        return self.scroll_offset.x == self.max_scroll_x or not self.size

    @property
    def is_vertical_scrollbar_grabbed(self) -> bool:
        """Is the user dragging the vertical scrollbar?"""
        return bool(self._vertical_scrollbar and self._vertical_scrollbar.grabbed)

    @property
    def is_horizontal_scrollbar_grabbed(self) -> bool:
        """Is the user dragging the vertical scrollbar?"""
        return bool(self._horizontal_scrollbar and self._horizontal_scrollbar.grabbed)

    @property
    def scrollbar_corner(self) -> ScrollBarCorner:
        """The scrollbar corner.

        Note:
            This will *create* a scrollbar corner if one doesn't exist.

        Returns:
            ScrollBarCorner Widget.
        """
        from textual.scrollbar import ScrollBarCorner

        if self._scrollbar_corner is not None:
            return self._scrollbar_corner
        self._scrollbar_corner = ScrollBarCorner()
        self.app._start_widget(self, self._scrollbar_corner)
        return self._scrollbar_corner

    @property
    def vertical_scrollbar(self) -> ScrollBar:
        """The vertical scrollbar (create if necessary).

        Note:
            This will *create* a scrollbar if one doesn't exist.

        Returns:
            ScrollBar Widget.
        """
        from textual.scrollbar import ScrollBar

        if self._vertical_scrollbar is not None:
            return self._vertical_scrollbar
        self._vertical_scrollbar = scroll_bar = ScrollBar(
            vertical=True, name="vertical", thickness=self.scrollbar_size_vertical
        )
        self._vertical_scrollbar.display = False
        self.app._start_widget(self, scroll_bar)
        return scroll_bar

    @property
    def horizontal_scrollbar(self) -> ScrollBar:
        """The horizontal scrollbar.

        Note:
            This will *create* a scrollbar if one doesn't exist.

        Returns:
            ScrollBar Widget.
        """

        from textual.scrollbar import ScrollBar

        if self._horizontal_scrollbar is not None:
            return self._horizontal_scrollbar
        self._horizontal_scrollbar = scroll_bar = ScrollBar(
            vertical=False, name="horizontal", thickness=self.scrollbar_size_horizontal
        )
        self._horizontal_scrollbar.display = False
        self.app._start_widget(self, scroll_bar)
        return scroll_bar

    def _refresh_scrollbars(self) -> None:
        """Refresh scrollbar visibility."""
        if not self.is_scrollable or not self.container_size:
            return

        styles = self.styles
        overflow_x = styles.overflow_x
        overflow_y = styles.overflow_y

        width, height = self._container_size

        show_horizontal = False
        if overflow_x == "hidden":
            show_horizontal = False
        elif overflow_x == "scroll":
            show_horizontal = True
        elif overflow_x == "auto":
            show_horizontal = self.virtual_size.width > width

        show_vertical = False
        if overflow_y == "hidden":
            show_vertical = False
        elif overflow_y == "scroll":
            show_vertical = True
        elif overflow_y == "auto":
            show_vertical = self.virtual_size.height > height

        _show_horizontal = show_horizontal
        _show_vertical = show_vertical

        if not (
            overflow_x == "auto"
            and overflow_y == "auto"
            and (show_horizontal, show_vertical) in self._scrollbar_changes
        ):
            # When a single scrollbar is shown, the other dimension changes, so we need to recalculate.
            if overflow_x == "auto" and show_vertical and not show_horizontal:
                show_horizontal = self.virtual_size.width > (
                    width - styles.scrollbar_size_vertical
                )
            if overflow_y == "auto" and show_horizontal and not show_vertical:
                show_vertical = self.virtual_size.height > (
                    height - styles.scrollbar_size_horizontal
                )

        if (
            self.show_horizontal_scrollbar != show_horizontal
            or self.show_vertical_scrollbar != show_vertical
        ):
            self._scrollbar_changes.add((_show_horizontal, _show_vertical))
        else:
            self._scrollbar_changes.clear()

        self.show_horizontal_scrollbar = show_horizontal
        self.show_vertical_scrollbar = show_vertical

        if self._horizontal_scrollbar is not None or show_horizontal:
            self.horizontal_scrollbar.display = show_horizontal
        if self._vertical_scrollbar is not None or show_vertical:
            self.vertical_scrollbar.display = show_vertical

    @property
    def scrollbars_enabled(self) -> tuple[bool, bool]:
        """A tuple of booleans that indicate if scrollbars are enabled.

        Returns:
            A tuple of (<vertical scrollbar enabled>, <horizontal scrollbar enabled>)
        """
        if not self.is_scrollable:
            return False, False

        return (self.show_vertical_scrollbar, self.show_horizontal_scrollbar)

    @property
    def scrollbars_space(self) -> tuple[int, int]:
        """The number of cells occupied by scrollbars for width and height"""
        return (self.scrollbar_size_vertical, self.scrollbar_size_horizontal)

    @property
    def scrollbar_size_vertical(self) -> int:
        """Get the width used by the *vertical* scrollbar.

        Returns:
            Number of columns in the vertical scrollbar.
        """
        styles = self.styles
        if styles.scrollbar_gutter == "stable" and styles.overflow_y == "auto":
            return styles.scrollbar_size_vertical
        return styles.scrollbar_size_vertical if self.show_vertical_scrollbar else 0

    @property
    def scrollbar_size_horizontal(self) -> int:
        """Get the height used by the *horizontal* scrollbar.

        Returns:
            Number of rows in the horizontal scrollbar.
        """
        styles = self.styles
        return styles.scrollbar_size_horizontal if self.show_horizontal_scrollbar else 0

    @property
    def scrollbar_gutter(self) -> Spacing:
        """Spacing required to fit scrollbar(s).

        Returns:
            Scrollbar gutter spacing.
        """
        return Spacing(
            0, self.scrollbar_size_vertical, self.scrollbar_size_horizontal, 0
        )

    @property
    def gutter(self) -> Spacing:
        """Spacing for padding / border / scrollbars.

        Returns:
            Additional spacing around content area.
        """
        return self.styles.gutter + self.scrollbar_gutter

    @property
    def size(self) -> Size:
        """The size of the content area.

        Returns:
            Content area size.
        """
        return self.content_region.size

    @property
    def scrollable_size(self) -> Size:
        """The size of the scrollable content.

        Returns:
            Scrollable content size.
        """
        return self.scrollable_content_region.size

    @property
    def outer_size(self) -> Size:
        """The size of the widget (including padding and border).

        Returns:
            Outer size.
        """
        return self._size

    @property
    def container_size(self) -> Size:
        """The size of the container (parent widget).

        Returns:
            Container size.
        """
        return self._container_size

    @property
    def content_region(self) -> Region:
        """Gets an absolute region containing the content (minus padding and border).

        Returns:
            Screen region that contains a widget's content.
        """
        content_region = self.region.shrink(self.styles.gutter)
        return content_region

    @property
    def scrollable_content_region(self) -> Region:
        """Gets an absolute region containing the scrollable content (minus padding, border, and scrollbars).

        Returns:
            Screen region that contains a widget's content.
        """
        content_region = self.region.shrink(self.styles.gutter).shrink(
            self.scrollbar_gutter
        )
        return content_region

    @property
    def content_offset(self) -> Offset:
        """An offset from the Widget origin where the content begins.

        Returns:
            Offset from widget's origin.
        """
        x, y = self.gutter.top_left
        return Offset(x, y)

    @property
    def content_size(self) -> Size:
        """The size of the content area.

        Returns:
            Content area size.
        """
        return self.region.shrink(self.styles.gutter).size

    @property
    def region(self) -> Region:
        """The region occupied by this widget, relative to the Screen.

        Raises:
            NoScreen: If there is no screen.
            errors.NoWidget: If the widget is not on the screen.

        Returns:
            Region within screen occupied by widget.
        """
        try:
            return self.screen.find_widget(self).region
        except (NoScreen, errors.NoWidget):
            return NULL_REGION

    @property
    def dock_gutter(self) -> Spacing:
        """Space allocated to docks in the parent.

        Returns:
            Space to be subtracted from scrollable area.
        """
        try:
            return self.screen.find_widget(self).dock_gutter
        except (NoScreen, errors.NoWidget):
            return NULL_SPACING

    @property
    def container_viewport(self) -> Region:
        """The viewport region (parent window).

        Returns:
            The region that contains this widget.
        """
        if self.parent is None:
            return self.size.region
        assert isinstance(self.parent, Widget)
        return self.parent.region

    @property
    def virtual_region(self) -> Region:
        """The widget region relative to its container (which may not be visible,
        depending on scroll offset).


        Returns:
            The virtual region.
        """
        try:
            return self.screen.find_widget(self).virtual_region
        except NoScreen:
            return Region()
        except errors.NoWidget:
            return Region()

    @property
    def window_region(self) -> Region:
        """The region within the scrollable area that is currently visible.

        Returns:
            New region.
        """
        window_region = self.region.at_offset(self.scroll_offset)
        return window_region

    @property
    def virtual_region_with_margin(self) -> Region:
        """The widget region relative to its container (*including margin*), which may not be visible,
        depending on the scroll offset.

        Returns:
            The virtual region of the Widget, inclusive of its margin.
        """
        return self.virtual_region.grow(self.styles.margin)

    @property
    def _self_or_ancestors_disabled(self) -> bool:
        """Is this widget or any of its ancestors disabled?"""

        node: Widget | None = self
        while isinstance(node, Widget) and not node.is_dom_root:
            if node.disabled:
                return True
            node = node._parent  # type:ignore[assignment]
        return False

    @property
    def focusable(self) -> bool:
        """Can this widget currently be focused?"""
        return (
            not self.loading
            and self.allow_focus()
            and self.visible
            and not self._self_or_ancestors_disabled
        )

    @property
    def _focus_sort_key(self) -> tuple[int, int]:
        """Key function to sort widgets into focus order."""
        x, y, _, _ = self.virtual_region
        top, _, _, left = self.styles.margin
        return y - top, x - left

    @property
    def scroll_offset(self) -> Offset:
        """Get the current scroll offset.

        Returns:
            Offset a container has been scrolled by.
        """
        return Offset(round(self.scroll_x), round(self.scroll_y))

    @property
    def container_scroll_offset(self) -> Offset:
        """The scroll offset the nearest container ancestor."""
        for node in self.ancestors:
            if isinstance(node, Widget) and node.is_scrollable:
                return node.scroll_offset
        return Offset()

    @property
    def _console(self) -> Console:
        """Get the current console.

        Returns:
            A Rich console object.
        """
        return self.app.console

    @property
    def _has_relative_children_width(self) -> bool:
        """Do any children (or progeny) have a relative width?"""
        if not self.is_container:
            return False
        for child in self.children:
            if child.styles.expand == "optimal":
                continue
            styles = child.styles
            if styles.display == "none":
                continue
            width = styles.width
            if width is None:
                continue
            if styles.is_relative_width or (
                width.is_auto and child._has_relative_children_width
            ):
                return True
        return False

    @property
    def _has_relative_children_height(self) -> bool:
        """Do any children (or progeny) have a relative height?"""

        if not self.is_container:
            return False
        for child in self.children:
            styles = child.styles
            if styles.display == "none":
                continue
            height = styles.height
            if height is None:
                continue
            if styles.is_relative_height or (
                height.is_auto and child._has_relative_children_height
            ):
                return True
        return False

    @property
    def is_on_screen(self) -> bool:
        """Check if the node was displayed in the last screen update."""
        try:
            self.screen.find_widget(self)
        except (NoScreen, errors.NoWidget):
            return False
        return True

    def _resolve_extrema(
        self,
        container: Size,
        viewport: Size,
        width_fraction: Fraction,
        height_fraction: Fraction,
    ) -> Extrema:
        """Resolve minimum and maximum values for width and height.

        Args:
            container: Size of outer widget.
            viewport: Viewport size.
            width_fraction: Size of 1fr width.
            height_fraction: Size of 1fr height.

        Returns:
            Extrema object.
        """

        min_width: Fraction | None = None
        max_width: Fraction | None = None
        min_height: Fraction | None = None
        max_height: Fraction | None = None

        styles = self.styles
        container -= styles.margin.totals
        if styles.box_sizing == "border-box":
            gutter_width, gutter_height = styles.gutter.totals
        else:
            gutter_width = gutter_height = 0

        if styles.min_width is not None:
            min_width = (
                styles.min_width.resolve(container, viewport, width_fraction)
                - gutter_width
            )

        if styles.max_width is not None:
            max_width = (
                styles.max_width.resolve(container, viewport, width_fraction)
                - gutter_width
            )
        if styles.min_height is not None:
            min_height = (
                styles.min_height.resolve(container, viewport, height_fraction)
                - gutter_height
            )

        if styles.max_height is not None:
            max_height = (
                styles.max_height.resolve(container, viewport, height_fraction)
                - gutter_height
            )

        extrema = Extrema(min_width, max_width, min_height, max_height)
        return extrema

    def animate(
        self,
        attribute: str,
        value: float | Animatable,
        *,
        final_value: object = ...,
        duration: float | None = None,
        speed: float | None = None,
        delay: float = 0.0,
        easing: EasingFunction | str = DEFAULT_EASING,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "full",
    ) -> None:
        """Animate an attribute.

        Args:
            attribute: Name of the attribute to animate.
            value: The value to animate to.
            final_value: The final value of the animation. Defaults to `value` if not set.
            duration: The duration (in seconds) of the animation.
            speed: The speed of the animation.
            delay: A delay (in seconds) before the animation starts.
            easing: An easing method.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
        """
        if self._animate is None:
            self._animate = self.app.animator.bind(self)
        assert self._animate is not None
        self._animate(
            attribute,
            value,
            final_value=final_value,
            duration=duration,
            speed=speed,
            delay=delay,
            easing=easing,
            on_complete=on_complete,
            level=level,
        )

    async def stop_animation(self, attribute: str, complete: bool = True) -> None:
        """Stop an animation on an attribute.

        Args:
            attribute: Name of the attribute whose animation should be stopped.
            complete: Should the animation be set to its final value?

        Note:
            If there is no animation scheduled or running, this is a no-op.
        """
        await self.app.animator.stop_animation(self, attribute, complete)

    @property
    def layout(self) -> Layout:
        """Get the layout object if set in styles, or a default layout.

        Returns:
            A layout object.
        """
        return self.styles.layout or self._default_layout

    @property
    def is_container(self) -> bool:
        """Is this widget a container (contains other widgets)?"""
        return self.styles.layout is not None or bool(self._nodes)

    @property
    def is_scrollable(self) -> bool:
        """Can this widget be scrolled?"""
        return self.styles.layout is not None or bool(self._nodes)

    @property
    def is_scrolling(self) -> bool:
        """Is this widget currently scrolling?"""
        current_time = monotonic()
        for node in self.ancestors:
            if not isinstance(node, Widget):
                break
            if (
                node.scroll_x != node.scroll_target_x
                or node.scroll_y != node.scroll_target_y
            ):
                return True
            if current_time - node._last_scroll_time < 0.1:
                # Scroll ended very recently
                return True
        return False

    @property
    def layer(self) -> str:
        """Get the name of this widgets layer.

        Returns:
            Name of layer.
        """
        return self.styles.layer or "default"

    @property
    def layers(self) -> tuple[str, ...]:
        """Layers of from parent.

        Returns:
            Tuple of layer names.
        """
        layers: tuple[str, ...] = ("default",)
        for node in self.ancestors_with_self:
            if not isinstance(node, Widget):
                break
            if node.styles.has_rule("layers"):
                layers = node.styles.layers
        return layers

    @property
    def link_style(self) -> Style:
        """Style of links.

        Returns:
            Rich style.
        """
        styles = self.styles
        _, background = self.background_colors
        link_background = background + styles.link_background
        link_color = link_background + (
            link_background.get_contrast_text(styles.link_color.a)
            if styles.auto_link_color
            else styles.link_color
        )
        style = styles.link_style + Style.from_color(
            link_color.rich_color,
            link_background.rich_color if styles.link_background.a else None,
        )
        return style

    @property
    def link_style_hover(self) -> Style:
        """Style of links underneath the mouse cursor.

        Returns:
            Rich Style.
        """
        styles = self.styles
        _, background = self.background_colors
        hover_background = background + styles.link_background_hover
        hover_color = hover_background + (
            hover_background.get_contrast_text(styles.link_color_hover.a)
            if styles.auto_link_color_hover
            else styles.link_color_hover
        )
        style = styles.link_style_hover + Style.from_color(
            hover_color.rich_color,
            hover_background.rich_color,
        )
        return style

    @property
    def select_container(self) -> Widget:
        """The widget's container used when selecting text..

        Returns:
            A widget which contains this widget.
        """
        container: Widget = self
        for widget in self.ancestors:
            if isinstance(widget, Widget) and widget.is_scrollable:
                return widget
        return container

    def _set_dirty(self, *regions: Region) -> None:
        """Set the Widget as 'dirty' (requiring re-paint).

        Regions should be specified as positional args. If no regions are added, then
        the entire widget will be considered dirty.

        Args:
            *regions: Regions which require a repaint.
        """
        if regions:
            content_offset = self.content_offset
            widget_regions = [region.translate(content_offset) for region in regions]
            self._dirty_regions.update(widget_regions)
            self._repaint_regions.update(widget_regions)
            self._styles_cache.set_dirty(*widget_regions)
        else:
            self._dirty_regions.clear()
            self._repaint_regions.clear()
            self._styles_cache.clear()

            outer_size = self.outer_size
            self._dirty_regions.add(outer_size.region)
            if outer_size:
                self._repaint_regions.add(outer_size.region)

    def _exchange_repaint_regions(self) -> Collection[Region]:
        """Get a copy of the regions which need a repaint, and clear internal cache.

        Returns:
            Regions to repaint.
        """
        regions = self._repaint_regions.copy()
        self._repaint_regions.clear()
        return regions

    def _scroll_to(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
        release_anchor: bool = True,
    ) -> bool:
        """Scroll to a given (absolute) coordinate, optionally animating.

        Args:
            x: X coordinate (column) to scroll to, or `None` for no change.
            y: Y coordinate (row) to scroll to, or `None` for no change.
            animate: Animate to new scroll position.
            speed: Speed of scroll if `animate` is `True`. Or `None` to use duration.
            duration: Duration of animation, if `animate` is `True` and speed is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
            release_anchor: If `True` call `release_anchor`.

        Returns:
            `True` if the scroll position changed, otherwise `False`.
        """
        if release_anchor:
            self.release_anchor()
        maybe_scroll_x = x is not None and (self.allow_horizontal_scroll or force)
        maybe_scroll_y = y is not None and (self.allow_vertical_scroll or force)
        scrolled_x = scrolled_y = False

        animator = self.app.animator
        animator.force_stop_animation(self, "scroll_x")
        animator.force_stop_animation(self, "scroll_y")

        def _animate_on_complete() -> None:
            """set last scroll time, and invoke callback."""
            self._last_scroll_time = monotonic()
            if on_complete is not None:
                self.call_next(on_complete)

        if animate:
            # TODO: configure animation speed
            if duration is None and speed is None:
                speed = 50

            if easing is None:
                easing = DEFAULT_SCROLL_EASING

            if maybe_scroll_x:
                assert x is not None
                self.scroll_target_x = x
                if x != self.scroll_x:
                    self.animate(
                        "scroll_x",
                        self.scroll_target_x,
                        speed=speed,
                        duration=duration,
                        easing=easing,
                        on_complete=_animate_on_complete,
                        level=level,
                    )
                    scrolled_x = True
            if maybe_scroll_y:
                assert y is not None
                self.scroll_target_y = y
                if y != self.scroll_y:
                    self.animate(
                        "scroll_y",
                        self.scroll_target_y,
                        speed=speed,
                        duration=duration,
                        easing=easing,
                        on_complete=_animate_on_complete,
                        level=level,
                    )
                    scrolled_y = True

        else:
            if maybe_scroll_x:
                assert x is not None
                scroll_x = self.scroll_x
                self.scroll_target_x = self.scroll_x = x
                scrolled_x = scroll_x != self.scroll_x
            if maybe_scroll_y:
                assert y is not None
                scroll_y = self.scroll_y
                self.scroll_target_y = self.scroll_y = y
                scrolled_y = scroll_y != self.scroll_y

            self._last_scroll_time = monotonic()
            if on_complete is not None:
                self.call_after_refresh(on_complete)

        return scrolled_x or scrolled_y

    @property
    def allow_select(self) -> bool:
        """Check if this widget permits text selection.

        Returns:
            `True` if the widget supports text selection, otherwise `False`.
        """
        return self.ALLOW_SELECT and not self.is_container

    def pre_layout(self, layout: Layout) -> None:
        """This method id called prior to a layout operation.

        Implement this method if you want to make updates that should impact
        the layout.

        Args:
            layout: The [Layout][textual.layout.Layout] instance that will be used to arrange this widget's children.

        """

    def set_scroll(self, x: float | None, y: float | None) -> None:
        """Set the scroll position without any validation.

        This is a low-level method for when you want to see the scroll position in the next frame.
        For a more fully featured method, see [`scroll_to`][textual.widget.Widget.scroll_to].

        Args:
            x: Desired `X` coordinate.
            y: Desired `Y` coordinate.
        """
        if x is not None:
            self.set_reactive(Widget.scroll_x, round(x))
        if y is not None:
            self.set_reactive(Widget.scroll_y, round(y))

    def scroll_to(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
        immediate: bool = False,
        release_anchor: bool = True,
    ) -> None:
        """Scroll to a given (absolute) coordinate, optionally animating.

        Args:
            x: X coordinate (column) to scroll to, or `None` for no change.
            y: Y coordinate (row) to scroll to, or `None` for no change.
            animate: Animate to new scroll position.
            speed: Speed of scroll if `animate` is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
            immediate: If `False` the scroll will be deferred until after a screen refresh,
                set to `True` to scroll immediately.
            release_anchor: If `True` call `release_anchor`.

        Note:
            The call to scroll is made after the next refresh.
        """
        if release_anchor:
            self.release_anchor()
        animator = self.app.animator
        if x is not None:
            animator.force_stop_animation(self, "scroll_x")
        if y is not None:
            animator.force_stop_animation(self, "scroll_y")
        if immediate:
            self._scroll_to(
                x,
                y,
                animate=animate,
                speed=speed,
                duration=duration,
                easing=easing,
                force=force,
                on_complete=on_complete,
                level=level,
            )
        else:
            self.call_after_refresh(
                self._scroll_to,
                x,
                y,
                animate=animate,
                speed=speed,
                duration=duration,
                easing=easing,
                force=force,
                on_complete=on_complete,
                level=level,
            )

    def scroll_relative(
        self,
        x: float | None = None,
        y: float | None = None,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
        immediate: bool = False,
    ) -> None:
        """Scroll relative to current position.

        Args:
            x: X distance (columns) to scroll, or ``None`` for no change.
            y: Y distance (rows) to scroll, or ``None`` for no change.
            animate: Animate to new scroll position.
            speed: Speed of scroll if `animate` is `True`. Or `None` to use `duration`.
            duration: Duration of animation, if animate is `True` and speed is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
            immediate: If `False` the scroll will be deferred until after a screen refresh,
                set to `True` to scroll immediately.
        """
        self.scroll_to(
            None if x is None else (self.scroll_x + x),
            None if y is None else (self.scroll_y + y),
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            on_complete=on_complete,
            level=level,
            immediate=immediate,
        )

    def scroll_home(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
        immediate: bool = False,
        x_axis: bool = True,
        y_axis: bool = True,
    ) -> None:
        """Scroll to home position.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if animate is `True`; or `None` to use duration.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
            immediate: If `False` the scroll will be deferred until after a screen refresh,
                set to `True` to scroll immediately.
            x_axis: Allow scrolling on X axis?
            y_axis: Allow scrolling on Y axis?
        """
        if speed is None and duration is None:
            duration = 1.0
        self.scroll_to(
            0 if x_axis else None,
            0 if y_axis else None,
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            on_complete=on_complete,
            level=level,
            immediate=immediate,
        )

    def scroll_end(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
        immediate: bool = False,
        x_axis: bool = True,
        y_axis: bool = True,
    ) -> None:
        """Scroll to the end of the container.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if `animate` is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
            immediate: If `False` the scroll will be deferred until after a screen refresh,
                set to `True` to scroll immediately.
            x_axis: Allow scrolling on X axis?
            y_axis: Allow scrolling on Y axis?

        """

        if speed is None and duration is None:
            duration = 1.0

        async def scroll_end_on_complete() -> None:
            """It's possible new content was added before we reached the end."""
            if on_complete is not None:
                self.call_next(on_complete)

        # In most cases we'd call self.scroll_to and let it handle the call
        # to do things after a refresh, but here we need the refresh to
        # happen first so that we can get the new self.max_scroll_y (that
        # is, we need the layout to work out and then figure out how big
        # things are). Because of this we'll create a closure over the call
        # here and make our own call to call_after_refresh.
        def _lazily_scroll_end() -> None:
            """Scroll to the end of the widget."""
            self._scroll_to(
                0 if x_axis else None,
                self.max_scroll_y if y_axis else None,
                animate=animate,
                speed=speed,
                duration=duration,
                easing=easing,
                force=force,
                on_complete=scroll_end_on_complete,
                level=level,
                release_anchor=False,
            )

        if self._anchored and self._anchor_released:
            self._anchor_released = False

        if immediate:
            _lazily_scroll_end()
        else:
            self.call_after_refresh(_lazily_scroll_end)

    def scroll_left(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
        immediate: bool = False,
    ) -> None:
        """Scroll one cell left.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if `animate` is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
            immediate: If `False` the scroll will be deferred until after a screen refresh,
                set to `True` to scroll immediately.
        """
        self.scroll_to(
            x=self.scroll_target_x - 1,
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            on_complete=on_complete,
            level=level,
            immediate=immediate,
        )

    def _scroll_left_for_pointer(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
    ) -> bool:
        """Scroll left one position, taking scroll sensitivity into account.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if `animate` is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).

        Returns:
            `True` if any scrolling was done.

        Note:
            How much is scrolled is controlled by
            [App.scroll_sensitivity_x][textual.app.App.scroll_sensitivity_x].
        """
        return self._scroll_to(
            x=self.scroll_target_x - self.app.scroll_sensitivity_x,
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            on_complete=on_complete,
            level=level,
        )

    def scroll_right(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
        immediate: bool = False,
    ) -> None:
        """Scroll one cell right.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if animate is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
            immediate: If `False` the scroll will be deferred until after a screen refresh,
                set to `True` to scroll immediately.
        """
        self.scroll_to(
            x=self.scroll_target_x + 1,
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            on_complete=on_complete,
            level=level,
            immediate=immediate,
        )

    def _scroll_right_for_pointer(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
    ) -> bool:
        """Scroll right one position, taking scroll sensitivity into account.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if animate is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).

        Returns:
            `True` if any scrolling was done.

        Note:
            How much is scrolled is controlled by
            [App.scroll_sensitivity_x][textual.app.App.scroll_sensitivity_x].
        """
        return self._scroll_to(
            x=self.scroll_target_x + self.app.scroll_sensitivity_x,
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            on_complete=on_complete,
            level=level,
        )

    def scroll_down(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
        immediate: bool = False,
    ) -> None:
        """Scroll one line down.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if `animate` is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
            immediate: If `False` the scroll will be deferred until after a screen refresh,
                set to `True` to scroll immediately.
        """
        self.scroll_to(
            y=self.scroll_target_y + 1,
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            on_complete=on_complete,
            level=level,
            immediate=immediate,
        )

    def _scroll_down_for_pointer(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
    ) -> bool:
        """Scroll down one position, taking scroll sensitivity into account.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if `animate` is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).

        Returns:
            `True` if any scrolling was done.

        Note:
            How much is scrolled is controlled by
            [App.scroll_sensitivity_y][textual.app.App.scroll_sensitivity_y].
        """
        return self._scroll_to(
            y=self.scroll_target_y + self.app.scroll_sensitivity_y,
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            on_complete=on_complete,
            level=level,
        )

    def scroll_up(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
        immediate: bool = False,
    ) -> None:
        """Scroll one line up.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if `animate` is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and speed is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
            immediate: If `False` the scroll will be deferred until after a screen refresh,
                set to `True` to scroll immediately.
        """
        self.scroll_to(
            y=self.scroll_target_y - 1,
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            on_complete=on_complete,
            level=level,
            immediate=immediate,
        )

    def _scroll_up_for_pointer(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
    ) -> bool:
        """Scroll up one position, taking scroll sensitivity into account.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if `animate` is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and speed is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).

        Returns:
            `True` if any scrolling was done.

        Note:
            How much is scrolled is controlled by
            [App.scroll_sensitivity_y][textual.app.App.scroll_sensitivity_y].
        """
        return self._scroll_to(
            y=self.scroll_target_y - self.app.scroll_sensitivity_y,
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            on_complete=on_complete,
            level=level,
        )

    def scroll_page_up(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
    ) -> None:
        """Scroll one page up.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if animate is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
        """
        self.scroll_to(
            y=self.scroll_y - self.scrollable_content_region.height,
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            on_complete=on_complete,
            level=level,
        )

    def scroll_page_down(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
    ) -> None:
        """Scroll one page down.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if animate is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
        """
        self.scroll_to(
            y=self.scroll_y + self.scrollable_content_region.height,
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            on_complete=on_complete,
            level=level,
        )

    def scroll_page_left(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
    ) -> None:
        """Scroll one page left.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if animate is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
        """
        if speed is None and duration is None:
            duration = 0.3
        self.scroll_to(
            x=self.scroll_x - self.scrollable_content_region.width,
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            on_complete=on_complete,
            level=level,
        )

    def scroll_page_right(
        self,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
    ) -> None:
        """Scroll one page right.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if animate is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
        """
        if speed is None and duration is None:
            duration = 0.3
        self.scroll_to(
            x=self.scroll_x + self.scrollable_content_region.width,
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            on_complete=on_complete,
            level=level,
        )

    def scroll_to_widget(
        self,
        widget: Widget,
        *,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        center: bool = False,
        top: bool = False,
        origin_visible: bool = True,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
        immediate: bool = False,
    ) -> bool:
        """Scroll scrolling to bring a widget into view.

        Args:
            widget: A descendant widget.
            animate: `True` to animate, or `False` to jump.
            speed: Speed of scroll if `animate` is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            top: Scroll widget to top of container.
            origin_visible: Ensure that the top left of the widget is within the window.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
            immediate: If `False` the scroll will be deferred until after a screen refresh,
                set to `True` to scroll immediately.

        Returns:
            `True` if any scrolling has occurred in any descendant, otherwise `False`.
        """
        # Grow the region by the margin so to keep the margin in view.
        region = widget.virtual_region_with_margin
        scrolled = False

        if not region.size:
            if on_complete is not None:
                self.call_after_refresh(on_complete)
            return False

        while isinstance(widget.parent, Widget) and widget is not self:
            container = widget.parent
            if widget.styles.dock != "none":
                scroll_offset = Offset(0, 0)
            else:
                scroll_offset = container.scroll_to_region(
                    region,
                    spacing=widget.dock_gutter,
                    animate=animate,
                    speed=speed,
                    duration=duration,
                    center=center,
                    top=top,
                    easing=easing,
                    origin_visible=origin_visible,
                    force=force,
                    on_complete=on_complete,
                    level=level,
                    immediate=immediate,
                )
                if scroll_offset:
                    scrolled = True

            # Adjust the region by the amount we just scrolled it, and convert to
            # its parent's virtual coordinate system.

            region = (
                (
                    region.translate(-scroll_offset)
                    .translate(container.styles.margin.top_left)
                    .translate(container.styles.border.spacing.top_left)
                    .translate(-widget.scroll_offset)
                    .translate(container.virtual_region_with_margin.offset)
                )
                .grow(container.styles.margin)
                .intersection(container.virtual_region_with_margin)
            )

            widget = container
        return scrolled

    def scroll_to_region(
        self,
        region: Region,
        *,
        spacing: Spacing | None = None,
        animate: bool = True,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        center: bool = False,
        top: bool = False,
        origin_visible: bool = True,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
        x_axis: bool = True,
        y_axis: bool = True,
        immediate: bool = False,
    ) -> Offset:
        """Scrolls a given region into view, if required.

        This method will scroll the least distance required to move `region` fully within
        the scrollable area.

        Args:
            region: A region that should be visible.
            spacing: Optional spacing around the region.
            animate: `True` to animate, or `False` to jump.
            speed: Speed of scroll if `animate` is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            top: Scroll `region` to top of container.
            origin_visible: Ensure that the top left of the widget is within the window.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
            x_axis: Allow scrolling on X axis?
            y_axis: Allow scrolling on Y axis?
            immediate: If `False` the scroll will be deferred until after a screen refresh,
                set to `True` to scroll immediately.

        Returns:
            The distance that was scrolled.
        """
        window = self.scrollable_content_region.at_offset(self.scroll_offset)
        if spacing is not None:
            window = window.shrink(spacing)

        if window in region and not (top or center):
            if on_complete is not None:
                self.call_after_refresh(on_complete)
            return Offset()

        def clamp_delta(delta: Offset) -> Offset:
            """Clamp the delta to avoid scrolling out of range."""
            scroll_x, scroll_y = self.scroll_offset
            delta = Offset(
                clamp(scroll_x + delta.x, 0, self.max_scroll_x) - scroll_x,
                clamp(scroll_y + delta.y, 0, self.max_scroll_y) - scroll_y,
            )
            return delta

        if center:
            region_center_x, region_center_y = region.center
            window_center_x, window_center_y = window.center

            delta = clamp_delta(
                Offset(
                    int(region_center_x - window_center_x + 0.5),
                    int(region_center_y - window_center_y + 0.5),
                )
            )
            if origin_visible and (region.offset not in window.translate(delta)):
                delta = clamp_delta(
                    Region.get_scroll_to_visible(window, region, top=True)
                )
        else:
            delta = clamp_delta(
                Region.get_scroll_to_visible(window, region, top=top),
            )

        if not self.allow_horizontal_scroll and not force:
            delta = Offset(0, delta.y)

        if not self.allow_vertical_scroll and not force:
            delta = Offset(delta.x, 0)

        if delta:
            delta_x = delta.x if x_axis else 0
            delta_y = delta.y if y_axis else 0
            if speed is None and duration is None:
                duration = 0.2
            self.scroll_relative(
                delta_x or None,
                delta_y or None,
                animate=animate,
                speed=speed,
                duration=duration,
                easing=easing,
                force=force,
                on_complete=on_complete,
                level=level,
                immediate=immediate,
            )
        else:
            if on_complete is not None:
                self.call_after_refresh(on_complete)
        return delta

    def scroll_visible(
        self,
        animate: bool = True,
        *,
        speed: float | None = None,
        duration: float | None = None,
        top: bool = False,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
        immediate: bool = False,
    ) -> None:
        """Scroll the container to make this widget visible.

        Args:
            animate: Animate scroll.
            speed: Speed of scroll if animate is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            top: Scroll to top of container.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
            immediate: If `False` the scroll will be deferred until after a screen refresh,
                set to `True` to scroll immediately.
        """
        parent = self.parent
        if isinstance(parent, Widget):
            if self.region:
                self.screen.scroll_to_widget(
                    self,
                    animate=animate,
                    speed=speed,
                    duration=duration,
                    top=top,
                    easing=easing,
                    force=force,
                    on_complete=on_complete,
                    level=level,
                    immediate=immediate,
                )
            else:
                # self.region is falsy which may indicate the widget hasn't been through a layout operation
                # We can potentially make it do the right thing by postponing the scroll to after a refresh
                parent.call_after_refresh(
                    self.screen.scroll_to_widget,
                    self,
                    animate=animate,
                    speed=speed,
                    duration=duration,
                    top=top,
                    easing=easing,
                    force=force,
                    on_complete=on_complete,
                    level=level,
                    immediate=immediate,
                )

    def scroll_to_center(
        self,
        widget: Widget,
        animate: bool = True,
        *,
        speed: float | None = None,
        duration: float | None = None,
        easing: EasingFunction | str | None = None,
        force: bool = False,
        origin_visible: bool = True,
        on_complete: CallbackType | None = None,
        level: AnimationLevel = "basic",
        immediate: bool = False,
    ) -> None:
        """Scroll this widget to the center of self.

        The center of the widget will be scrolled to the center of the container.

        Args:
            widget: The widget to scroll to the center of self.
            animate: Whether to animate the scroll.
            speed: Speed of scroll if animate is `True`; or `None` to use `duration`.
            duration: Duration of animation, if `animate` is `True` and `speed` is `None`.
            easing: An easing method for the scrolling animation.
            force: Force scrolling even when prohibited by overflow styling.
            origin_visible: Ensure that the top left corner of the widget remains visible after the scroll.
            on_complete: A callable to invoke when the animation is finished.
            level: Minimum level required for the animation to take place (inclusive).
            immediate: If `False` the scroll will be deferred until after a screen refresh,
                set to `True` to scroll immediately.
        """

        self.scroll_to_widget(
            widget=widget,
            animate=animate,
            speed=speed,
            duration=duration,
            easing=easing,
            force=force,
            center=True,
            origin_visible=origin_visible,
            on_complete=on_complete,
            level=level,
            immediate=immediate,
        )

    def can_view_entire(self, widget: Widget) -> bool:
        """Check if a given widget is *fully* within the current view (scrollable area).

        Note: This doesn't necessarily equate to a widget being visible.
        There are other reasons why a widget may not be visible.

        Args:
            widget: A widget that is a descendant of self.

        Returns:
            `True` if the entire widget is in view, `False` if it is partially visible or not in view.
        """
        if widget is self:
            return True

        if widget not in self.screen._compositor.visible_widgets:
            return False

        region = widget.region
        node: Widget = widget

        while isinstance(node.parent, Widget) and node is not self:
            if region not in node.parent.scrollable_content_region:
                return False
            node = node.parent
        return True

    def can_view_partial(self, widget: Widget) -> bool:
        """Check if a given widget at least partially visible within the current view (scrollable area).

        Args:
            widget: A widget that is a descendant of self.

        Returns:
            `True` if any part of the widget is visible, `False` if it is outside of the viewable area.
        """
        if widget is self:
            return True

        if widget not in self.screen._compositor.visible_widgets or not widget.display:
            return False

        region = widget.region
        node: Widget = widget

        while isinstance(node.parent, Widget) and node is not self:
            if not region.overlaps(node.parent.scrollable_content_region):
                return False
            node = node.parent
        return True

    def __init_subclass__(
        cls,
        can_focus: bool | None = None,
        can_focus_children: bool | None = None,
        inherit_css: bool = True,
        inherit_bindings: bool = True,
    ) -> None:
        name = cls.__name__
        if not name[0].isupper() and not name.startswith("_"):
            raise BadWidgetName(
                f"Widget subclass {name!r} should be capitalized or start with '_'."
            )

        super().__init_subclass__(
            inherit_css=inherit_css,
            inherit_bindings=inherit_bindings,
        )
        base = cls.__mro__[0]
        if issubclass(base, Widget):
            cls.can_focus = base.can_focus if can_focus is None else can_focus
            cls.can_focus_children = (
                base.can_focus_children
                if can_focus_children is None
                else can_focus_children
            )

    def __rich_repr__(self) -> rich.repr.Result:
        try:
            yield "id", self.id, None
            if self.name:
                yield "name", self.name
            if self.classes:
                yield "classes", " ".join(self.classes)
        except AttributeError:
            pass

    def _get_scrollable_region(self, region: Region) -> Region:
        """Adjusts the Widget region to accommodate scrollbars.

        Args:
            region: A region for the widget.

        Returns:
            The widget region minus scrollbars.
        """
        show_vertical_scrollbar, show_horizontal_scrollbar = self.scrollbars_enabled

        styles = self.styles
        scrollbar_size_horizontal = styles.scrollbar_size_horizontal
        scrollbar_size_vertical = styles.scrollbar_size_vertical

        show_vertical_scrollbar = bool(
            show_vertical_scrollbar and scrollbar_size_vertical
        )
        show_horizontal_scrollbar = bool(
            show_horizontal_scrollbar and scrollbar_size_horizontal
        )

        if styles.scrollbar_gutter == "stable":
            # Let's _always_ reserve some space, whether the scrollbar is actually displayed or not:
            show_vertical_scrollbar = True
            scrollbar_size_vertical = styles.scrollbar_size_vertical

        if show_horizontal_scrollbar and show_vertical_scrollbar:
            (region, _, _, _) = region.split(
                -scrollbar_size_vertical,
                -scrollbar_size_horizontal,
            )
        elif show_vertical_scrollbar:
            region, _ = region.split_vertical(-scrollbar_size_vertical)
        elif show_horizontal_scrollbar:
            region, _ = region.split_horizontal(-scrollbar_size_horizontal)
        return region

    def _arrange_scrollbars(self, region: Region) -> Iterable[tuple[Widget, Region]]:
        """Arrange the 'chrome' widgets (typically scrollbars) for a layout element.

        Args:
            region: The containing region.

        Returns:
            Tuples of scrollbar Widget and region.
        """
        show_vertical_scrollbar, show_horizontal_scrollbar = self.scrollbars_enabled

        scrollbar_size_horizontal = self.scrollbar_size_horizontal
        scrollbar_size_vertical = self.scrollbar_size_vertical

        show_vertical_scrollbar = bool(
            show_vertical_scrollbar and scrollbar_size_vertical
        )
        show_horizontal_scrollbar = bool(
            show_horizontal_scrollbar and scrollbar_size_horizontal
        )

        if show_horizontal_scrollbar and show_vertical_scrollbar:
            (
                window_region,
                vertical_scrollbar_region,
                horizontal_scrollbar_region,
                scrollbar_corner_gap,
            ) = region.split(
                region.width - scrollbar_size_vertical,
                region.height - scrollbar_size_horizontal,
            )
            if scrollbar_corner_gap:
                yield self.scrollbar_corner, scrollbar_corner_gap
            if vertical_scrollbar_region:
                scrollbar = self.vertical_scrollbar
                scrollbar.window_virtual_size = self.virtual_size.height
                scrollbar.window_size = window_region.height
                yield scrollbar, vertical_scrollbar_region
            if horizontal_scrollbar_region:
                scrollbar = self.horizontal_scrollbar
                scrollbar.window_virtual_size = self.virtual_size.width
                scrollbar.window_size = window_region.width
                yield scrollbar, horizontal_scrollbar_region

        elif show_vertical_scrollbar:
            window_region, scrollbar_region = region.split_vertical(
                region.width - scrollbar_size_vertical
            )
            if scrollbar_region:
                scrollbar = self.vertical_scrollbar
                scrollbar.window_virtual_size = self.virtual_size.height
                scrollbar.window_size = window_region.height
                yield scrollbar, scrollbar_region
        elif show_horizontal_scrollbar:
            window_region, scrollbar_region = region.split_horizontal(
                region.height - scrollbar_size_horizontal
            )
            if scrollbar_region:
                scrollbar = self.horizontal_scrollbar
                scrollbar.window_virtual_size = self.virtual_size.width
                scrollbar.window_size = window_region.width
                yield scrollbar, scrollbar_region

    def get_pseudo_class_state(self) -> PseudoClasses:
        """Get an object describing whether each pseudo class is present on this object or not.

        Returns:
            A PseudoClasses object describing the pseudo classes that are present.
        """
        node: MessagePump | None = self
        disabled = False
        while isinstance(node, Widget):
            if node.disabled:
                disabled = True
                break
            node = node._parent

        pseudo_classes = PseudoClasses(
            enabled=not disabled,
            hover=self.mouse_hover,
            focus=self.has_focus,
        )
        return pseudo_classes

    @property
    def _pseudo_classes_cache_key(self) -> tuple[int, ...]:
        """A cache key that changes when the pseudo-classes change."""
        return (
            self.mouse_hover,
            self.has_focus,
            self.is_disabled,
        )

    def _get_justify_method(self) -> JustifyMethod | None:
        """Get the justify method that may be passed to a Rich renderable."""
        text_justify: JustifyMethod | None = None

        if self.styles.has_rule("text_align"):
            text_align: JustifyMethod = cast(JustifyMethod, self.styles.text_align)
            text_justify = _JUSTIFY_MAP.get(text_align, text_align)
        return text_justify

    def post_render(
        self, renderable: RenderableType, base_style: Style
    ) -> ConsoleRenderable:
        """Applies style attributes to the default renderable.

        This method is called by Textual itself.
        It is unlikely you will need to call or implement this method.

        Returns:
            A new renderable.
        """

        text_justify = self._get_justify_method()

        if isinstance(renderable, str):
            renderable = Text.from_markup(renderable, justify=text_justify)

        if (
            isinstance(renderable, Text)
            and text_justify is not None
            and renderable.justify != text_justify
        ):
            renderable = renderable.copy()
            renderable.justify = text_justify

        renderable = _Styled(
            cast(ConsoleRenderable, renderable),
            base_style,
            self.link_style if self.auto_links else None,
        )

        return renderable

    def watch_mouse_hover(self, value: bool) -> None:
        """Update from CSS if mouse over state changes."""
        if self._has_hover_style:
            self._update_styles()

    def watch_has_focus(self, value: bool) -> None:
        """Update from CSS if has focus state changes."""
        self._update_styles()

    def watch_disabled(self, disabled: bool) -> None:
        """Update the styles of the widget and its children when disabled is toggled."""
        from textual.app import ScreenStackError

        if disabled and self.mouse_hover and self.app.mouse_over is not None:
            # Ensure widget gets a Leave if it is disabled while hovered
            self._message_queue.put_nowait(events.Leave(self.app.mouse_over))
        try:
            screen = self.screen
            if (
                disabled
                and screen.focused is not None
                and self in screen.focused.ancestors_with_self
            ):
                screen.focused.blur()
        except (ScreenStackError, NoActiveAppError, NoScreen):
            pass

        self._update_styles()

    def _size_updated(
        self, size: Size, virtual_size: Size, container_size: Size, layout: bool = True
    ) -> bool:
        """Called when the widget's size is updated.

        Args:
            size: Screen size.
            virtual_size: Virtual (scrollable) size.
            container_size: Container size (size of parent).
            layout: Perform layout if required.

        Returns:
            True if a resize event should be sent, otherwise False.
        """

        self._layout_cache.clear()
        if (
            self._size != size
            or self.virtual_size != virtual_size
            or self._container_size != container_size
        ):
            if self._size != size:
                self._set_dirty()
            self._size = size
            if layout:
                self.virtual_size = virtual_size
            else:
                self.set_reactive(Widget.virtual_size, virtual_size)
            self._container_size = container_size
            if self.is_scrollable:
                self._scroll_update(virtual_size)
            return True
        else:
            return False

    def _scroll_update(self, virtual_size: Size) -> None:
        """Update scrollbars visibility and dimensions.

        Args:
            virtual_size: Virtual size.
        """
        self._refresh_scrollbars()
        width, height = self.container_size

        if self.show_vertical_scrollbar:
            self.vertical_scrollbar.window_virtual_size = virtual_size.height
            self.vertical_scrollbar.window_size = (
                height - self.scrollbar_size_horizontal
            )
            self.vertical_scrollbar.refresh()
        if self.show_horizontal_scrollbar:
            self.horizontal_scrollbar.window_virtual_size = virtual_size.width
            self.horizontal_scrollbar.window_size = width - self.scrollbar_size_vertical
            self.horizontal_scrollbar.refresh()

        self.scroll_x = self.validate_scroll_x(self.scroll_x)
        self.scroll_y = self.validate_scroll_y(self.scroll_y)

    @property
    def visual_style(self) -> VisualStyle:
        background = Color(0, 0, 0, 0)
        color = Color(255, 255, 255, 0)

        style = Style()
        opacity = 1.0

        for node in reversed(self.ancestors_with_self):
            styles = node.styles
            has_rule = styles.has_rule
            opacity *= styles.opacity
            if has_rule("background"):
                text_background = background + styles.background.tint(
                    styles.background_tint
                )
                background += (
                    styles.background.tint(styles.background_tint)
                ).multiply_alpha(opacity)
            else:
                text_background = background
            if has_rule("color"):
                color = styles.color
            style += styles.text_style
            if has_rule("auto_color") and styles.auto_color:
                color = text_background.get_contrast_text(color.a)

        return VisualStyle(
            background,
            color,
            bold=style.bold,
            dim=style.dim,
            italic=style.italic,
            reverse=style.reverse,
            underline=style.underline,
            strike=style.strike,
        )

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Get the text under the selection.

        Args:
            selection: Selection information.

        Returns:
            Tuple of extracted text and ending (typically "\n" or " "), or `None` if no text could be extracted.
        """
        visual = self._render()
        if isinstance(visual, (Text, Content)):
            text = str(visual)
        else:
            return None
        return selection.extract(text), "\n"

    def selection_updated(self, selection: Selection | None) -> None:
        """Called when the selection is updated.

        Args:
            selection: Selection information or `None` if no selection.
        """
        self.refresh()

    def _render_content(self) -> None:
        """Render all lines."""
        width, height = self.size
        visual = self._render()
        strips = Visual.to_strips(self, visual, width, height, self.visual_style)
        self._render_cache = _RenderCache(self.size, strips)
        self._dirty_regions.clear()

    def render_line(self, y: int) -> Strip:
        """Render a line of content.

        Args:
            y: Y Coordinate of line.

        Returns:
            A rendered line.
        """
        if self._dirty_regions:
            self._render_content()
        try:
            line = self._render_cache.lines[y]
        except IndexError:
            line = Strip.blank(self.size.width, self.rich_style)

        return line

    def render_lines(self, crop: Region) -> list[Strip]:
        """Render the widget into lines.

        Args:
            crop: Region within visible area to render.

        Returns:
            A list of list of segments.
        """
        strips = self._styles_cache.render_widget(self, crop)
        return strips

    def get_style_at(self, x: int, y: int) -> Style:
        """Get the Rich style in a widget at a given relative offset.

        Args:
            x: X coordinate relative to the widget.
            y: Y coordinate relative to the widget.

        Returns:
            A rich Style object.
        """
        offset = Offset(x, y)
        screen_offset = offset + self.region.offset

        widget, _ = self.screen.get_widget_at(*screen_offset)
        if widget is not self:
            return Style()
        return self.screen.get_style_at(*screen_offset)

    def suppress_click(self) -> None:
        """Suppress a click event.

        This will prevent a [Click][textual.events.Click] event being sent,
        if called after a mouse down event and before the click itself.

        """
        self.app._mouse_down_widget = None

    def _forward_event(self, event: events.Event) -> None:
        event._set_forwarded()
        self.post_message(event)

    def _refresh_scroll(self) -> None:
        """Refreshes the scroll position."""
        self._scroll_required = True
        self.check_idle()

    def refresh(
        self,
        *regions: Region,
        repaint: bool = True,
        layout: bool = False,
        recompose: bool = False,
    ) -> Self:
        """Initiate a refresh of the widget.

        This method sets an internal flag to perform a refresh, which will be done on the
        next idle event. Only one refresh will be done even if this method is called multiple times.

        By default this method will cause the content of the widget to refresh, but not change its size. You can also
        set `layout=True` to perform a layout.

        !!! warning

            It is rarely necessary to call this method explicitly. Updating styles or reactive attributes will
            do this automatically.

        Args:
            *regions: Additional screen regions to mark as dirty.
            repaint: Repaint the widget (will call render() again).
            layout: Also layout widgets in the view.
            recompose: Re-compose the widget (will remove and re-mount children).

        Returns:
            The `Widget` instance.
        """
        if layout:
            self._layout_required = True
            for ancestor in self.ancestors:
                if not isinstance(ancestor, Widget):
                    break
                ancestor._clear_arrangement_cache()

        if recompose:
            self._recompose_required = True
            self.call_next(self._check_recompose)
            return self

        if not self._is_mounted:
            self._repaint_required = True
            self.check_idle()
            return self

        self._layout_cache.clear()
        if repaint:
            self._set_dirty(*regions)
            self.clear_cached_dimensions()
            self._rich_style_cache.clear()
            self._repaint_required = True

        self.check_idle()
        return self

    def remove(self) -> AwaitRemove:
        """Remove the Widget from the DOM (effectively deleting it).

        Returns:
            An awaitable object that waits for the widget to be removed.
        """
        await_remove = self.app._prune(self, parent=self._parent)
        return await_remove

    def remove_children(
        self, selector: str | type[QueryType] | Iterable[Widget] = "*"
    ) -> AwaitRemove:
        """Remove the immediate children of this Widget from the DOM.

        Args:
            selector: A CSS selector or iterable of widgets to remove.

        Returns:
            An awaitable object that waits for the direct children to be removed.
        """

        if callable(selector) and issubclass(selector, Widget):
            selector = selector.__name__

        children_to_remove: Iterable[Widget]

        if isinstance(selector, str):
            parsed_selectors = parse_selectors(selector)
            children_to_remove = [
                child for child in self.children if match(parsed_selectors, child)
            ]
        else:
            children_to_remove = selector
        await_remove = self.app._prune(
            *children_to_remove, parent=cast(DOMNode, self._parent)
        )
        return await_remove

    @asynccontextmanager
    async def batch(self) -> AsyncGenerator[None, None]:
        """Async context manager that combines widget locking and update batching.

        Use this async context manager whenever you want to acquire the widget lock and
        batch app updates at the same time.

        Example:
            ```py
            async with container.batch():
                await container.remove_children(Button)
                await container.mount(Label("All buttons are gone."))
            ```
        """
        async with self.lock:
            with self.app.batch_update():
                yield

    def render(self) -> RenderResult:
        """Get [content](/guide/content) for the widget.

        Implement this method in a subclass for custom widgets.

        This method should return [markup](/guide/content#markup), a [Content][textual.content.Content] object, or a [Rich](https://github.com/Textualize/rich) renderable.

        Example:
            ```python
            from textual.app import RenderResult
            from textual.widget import Widget

            class CustomWidget(Widget):
                def render(self) -> RenderResult:
                    return "Welcome to [bold red]Textual[/]!"
            ```

        Returns:
            A string or object to render as the widget's content.
        """

        if self.is_container:
            if self.styles.layout and self.styles.keyline[0] != "none":
                return self.layout.render_keyline(self)
            else:
                return Blank(self.background_colors[1])
        return self.css_identifier_styled

    def _render(self) -> Visual:
        """Get renderable, promoting str to text as required.

        Returns:
            A Visual.
        """
        cache_key = "_render.visual"
        cached_visual = self._layout_cache.get(cache_key, None)
        if cached_visual is not None:
            assert isinstance(cached_visual, Visual)
            return cached_visual
        visual = visualize(self, self.render(), markup=self._render_markup)
        self._layout_cache[cache_key] = visual
        return visual

    async def run_action(self, action: str) -> None:
        """Perform a given action, with this widget as the default namespace.

        Args:
            action: Action encoded as a string.
        """
        await self.app.run_action(action, self)

    def post_message(self, message: Message) -> bool:
        """Post a message to this widget.

        Args:
            message: Message to post.

        Returns:
            True if the message was posted, False if this widget was closed / closing.
        """
        _rich_traceback_omit = True
        # Catch a common error.
        # This will error anyway, but at least we can offer a helpful message here.
        if not hasattr(message, "_prevent"):
            raise RuntimeError(
                f"{type(message)!r} is missing expected attributes; did you forget to call super().__init__() in the constructor?"
            )

        if constants.DEBUG and not self.is_running and not message.no_dispatch:
            try:
                self.log.warning(self, f"IS NOT RUNNING, {message!r} not sent")
            except NoActiveAppError:
                pass
        return super().post_message(message)

    async def on_prune(self, event: messages.Prune) -> None:
        """Close message loop when asked to prune."""
        await self._close_messages(wait=False)

    async def _message_loop_exit(self) -> None:
        """Clean up DOM tree."""
        parent = self._parent
        # Post messages to children, asking them to prune
        children = [*self.children, *self._get_virtual_dom()]
        for node in children:
            node.post_message(Prune())

        # Wait for child nodes to exit
        await gather(*[node._task for node in children if node._task is not None])
        # Send unmount event
        await self._dispatch_message(events.Unmount())
        assert isinstance(parent, DOMNode)
        # Finalize removal from DOM
        parent._nodes._remove(self)
        self.app._registry.discard(self)
        self._detach()
        self._arrangement_cache.clear()
        self._nodes._clear()
        self._render_cache = _RenderCache(NULL_SIZE, [])
        self._component_styles.clear()
        self._query_one_cache.clear()

    async def _on_idle(self, event: events.Idle) -> None:
        """Called when there are no more events on the queue.

        Args:
            event: Idle event.
        """
        self._check_refresh()

    def _check_refresh(self) -> None:
        """Check if a refresh was requested."""
        if self._parent is not None and not self._closing:
            try:
                screen = self.screen
            except NoScreen:
                pass
            else:
                if self._refresh_styles_required:
                    self._refresh_styles_required = False
                    self.call_later(self._update_styles)
                if self._scroll_required:
                    self._scroll_required = False
                    if self.styles.keyline[0] != "none":
                        # TODO: Feels like a hack
                        # Perhaps there should be an explicit mechanism for backgrounds to refresh when scrolled?
                        self._set_dirty()
                    screen.post_message(messages.UpdateScroll())
                if self._repaint_required:
                    self._repaint_required = False
                    screen.post_message(messages.Update(self))
                if self._layout_required:
                    self._layout_required = False
                    screen.post_message(messages.Layout())

    def focus(self, scroll_visible: bool = True) -> Self:
        """Give focus to this widget.

        Args:
            scroll_visible: Scroll parent to make this widget visible.

        Returns:
            The `Widget` instance.
        """

        def set_focus(widget: Widget) -> None:
            """Callback to set the focus."""
            try:
                widget.screen.set_focus(self, scroll_visible=scroll_visible)
            except NoScreen:
                pass

        self.app.call_later(set_focus, self)
        return self

    def blur(self) -> Self:
        """Blur (un-focus) the widget.

        Focus will be moved to the next available widget in the focus chain.

        Returns:
            The `Widget` instance.
        """
        try:
            self.screen._reset_focus(self)
        except NoScreen:
            pass
        return self

    def capture_mouse(self, capture: bool = True) -> None:
        """Capture (or release) the mouse.

        When captured, mouse events will go to this widget even when the pointer is not directly over the widget.

        Args:
            capture: True to capture or False to release.
        """
        self.app.capture_mouse(self if capture else None)

    def release_mouse(self) -> None:
        """Release the mouse.

        Mouse events will only be sent when the mouse is over the widget.
        """
        if self.app.mouse_captured is self:
            self.app.capture_mouse(None)

    def text_select_all(self) -> None:
        """Select the entire widget."""
        self.screen._select_all_in_widget(self)

    def begin_capture_print(self, stdout: bool = True, stderr: bool = True) -> None:
        """Capture text from print statements (or writes to stdout / stderr).

        If printing is captured, the widget will be sent an [`events.Print`][textual.events.Print] message.

        Call [`end_capture_print`][textual.widget.Widget.end_capture_print] to disable print capture.

        Args:
            stdout: Whether to capture stdout.
            stderr: Whether to capture stderr.
        """
        self.app.begin_capture_print(self, stdout=stdout, stderr=stderr)

    def end_capture_print(self) -> None:
        """End print capture (set with [`begin_capture_print`][textual.widget.Widget.begin_capture_print])."""
        self.app.end_capture_print(self)

    def check_message_enabled(self, message: Message) -> bool:
        """Check if a given message is enabled (allowed to be sent).

        Args:
            message: A message object

        Returns:
            `True` if the message will be sent, or `False` if it is disabled.
        """
        # Do the normal checking and get out if that fails.
        if not super().check_message_enabled(message) or self._is_prevented(
            type(message)
        ):
            return False

        # Mouse scroll events should always go through, this allows mouse
        # wheel scrolling to pass through disabled widgets.
        if isinstance(message, _MOUSE_EVENTS_ALLOW_IF_DISABLED):
            return True
        # Otherwise, if this is any other mouse event, the widget receiving
        # the event must not be disabled at this moment.
        return (
            not self._self_or_ancestors_disabled
            if isinstance(message, _MOUSE_EVENTS_DISALLOW_IF_DISABLED)
            else True
        )

    async def broker_event(self, event_name: str, event: events.Event) -> bool:
        return await self.app._broker_event(event_name, event, default_namespace=self)

    def notify_style_update(self) -> None:
        self._rich_style_cache.clear()
        self._visual_style_cache.clear()
        super().notify_style_update()

    async def _on_mouse_down(self, event: events.MouseDown) -> None:
        await self.broker_event("mouse.down", event)

    async def _on_mouse_up(self, event: events.MouseUp) -> None:
        await self.broker_event("mouse.up", event)

    async def _on_click(self, event: events.Click) -> None:
        if event.widget is self:
            if self.allow_select and self.screen.allow_select and self.app.ALLOW_SELECT:
                if event.chain == 2:
                    self.text_select_all()
                elif event.chain == 3 and self.parent is not None:
                    self.select_container.text_select_all()

        await self.broker_event("click", event)

    async def _on_key(self, event: events.Key) -> None:
        await self.handle_key(event)

    async def handle_key(self, event: events.Key) -> bool:
        return await dispatch_key(self, event)

    async def _on_compose(self, event: events.Compose) -> None:
        _rich_traceback_omit = True
        event.prevent_default()
        await self._compose()

    async def _compose(self) -> None:
        try:
            widgets = [*self._pending_children, *compose(self)]
            self._pending_children.clear()
        except TypeError as error:
            raise TypeError(
                f"{self!r} compose() method returned an invalid result; {error}"
            ) from error
        except Exception as error:
            self.app._handle_exception(error)
        else:
            self._extend_compose(widgets)
            await self.mount_composed_widgets(widgets)

    async def mount_composed_widgets(self, widgets: list[Widget]) -> None:
        """Called by Textual to mount widgets after compose.

        There is generally no need to implement this method in your application.
        See [Lazy][textual.lazy.Lazy] for a class which uses this method to implement
        *lazy* mounting.

        Args:
            widgets: A list of child widgets.
        """
        if widgets:
            await self.mount_all(widgets)

    def _extend_compose(self, widgets: list[Widget]) -> None:
        """Hook to extend composed widgets.

        Args:
            widgets: Widgets to be mounted.
        """

    def _on_mount(self, event: events.Mount) -> None:
        if self.styles.overflow_y == "scroll":
            self.show_vertical_scrollbar = True
        if self.styles.overflow_x == "scroll":
            self.show_horizontal_scrollbar = True

    def _on_leave(self, event: events.Leave) -> None:
        if event.node is self:
            self.mouse_hover = False
            self.hover_style = Style()

    def _on_enter(self, event: events.Enter) -> None:
        if event.node is self:
            self.mouse_hover = True

    def _on_focus(self, event: events.Focus) -> None:
        self.has_focus = True
        self.refresh()
        if self.parent is not None:
            self.parent.post_message(events.DescendantFocus(self))

    def _on_blur(self, event: events.Blur) -> None:
        self.has_focus = False
        self.refresh()
        if self.parent is not None:
            self.parent.post_message(events.DescendantBlur(self))

    def _on_mouse_scroll_down(self, event: events.MouseScrollDown) -> None:
        if event.ctrl or event.shift:
            if self.allow_horizontal_scroll:
                if self._scroll_right_for_pointer(animate=False):
                    event.stop()
        else:
            if self.allow_vertical_scroll:
                if self._scroll_down_for_pointer(animate=False):
                    event.stop()

    def _on_mouse_scroll_up(self, event: events.MouseScrollUp) -> None:
        if event.ctrl or event.shift:
            if self.allow_horizontal_scroll:
                if self._scroll_left_for_pointer(animate=False):
                    event.stop()
        else:
            if self.allow_vertical_scroll:
                if self._scroll_up_for_pointer(animate=False):
                    event.stop()

    def _on_mouse_scroll_right(self, event: events.MouseScrollRight) -> None:
        if self.allow_horizontal_scroll:
            if self._scroll_right_for_pointer():
                event.stop()

    def _on_mouse_scroll_left(self, event: events.MouseScrollLeft) -> None:
        if self.allow_horizontal_scroll:
            if self._scroll_left_for_pointer():
                event.stop()

    def _on_scroll_to(self, message: ScrollTo) -> None:
        if self._allow_scroll:
            self.scroll_to(message.x, message.y, animate=message.animate, duration=0.1)
            message.stop()

    def _on_scroll_up(self, event: ScrollUp) -> None:
        if self.allow_vertical_scroll:
            self.scroll_page_up()
            event.stop()

    def _on_scroll_down(self, event: ScrollDown) -> None:
        if self.allow_vertical_scroll:
            self.scroll_page_down()
            event.stop()

    def _on_scroll_left(self, event: ScrollLeft) -> None:
        if self.allow_horizontal_scroll:
            self.scroll_page_left()
            event.stop()

    def _on_scroll_right(self, event: ScrollRight) -> None:
        if self.allow_horizontal_scroll:
            self.scroll_page_right()
            event.stop()

    def _on_show(self, event: events.Show) -> None:
        if self.show_horizontal_scrollbar:
            self.horizontal_scrollbar.post_message(event)
        if self.show_vertical_scrollbar:
            self.vertical_scrollbar.post_message(event)

    def _on_hide(self, event: events.Hide) -> None:
        if self.show_horizontal_scrollbar:
            self.horizontal_scrollbar.post_message(event)
        if self.show_vertical_scrollbar:
            self.vertical_scrollbar.post_message(event)
        if self.has_focus:
            self.blur()

    def _on_scroll_to_region(self, message: messages.ScrollToRegion) -> None:
        self.scroll_to_region(message.region, animate=True)

    def _on_unmount(self) -> None:
        self._uncover()
        self.workers.cancel_node(self)

    def action_scroll_home(self) -> None:
        if not self._allow_scroll:
            raise SkipAction()
        self.scroll_home(x_axis=self.scroll_y == 0)

    def action_scroll_end(self) -> None:
        if not self._allow_scroll:
            raise SkipAction()
        self.scroll_end(x_axis=self.scroll_y == self.is_vertical_scroll_end)

    def action_scroll_left(self) -> None:
        if not self.allow_horizontal_scroll:
            raise SkipAction()
        self.scroll_left()

    def action_scroll_right(self) -> None:
        if not self.allow_horizontal_scroll:
            raise SkipAction()
        self.scroll_right()

    def action_scroll_up(self) -> None:
        if not self.allow_vertical_scroll:
            raise SkipAction()
        self.scroll_up()

    def action_scroll_down(self) -> None:
        if not self.allow_vertical_scroll:
            raise SkipAction()
        self.scroll_down()

    def action_page_down(self) -> None:
        if not self.allow_vertical_scroll:
            raise SkipAction()
        self.scroll_page_down()

    def action_page_up(self) -> None:
        if not self.allow_vertical_scroll:
            raise SkipAction()
        self.scroll_page_up()

    def action_page_left(self) -> None:
        if not self.allow_horizontal_scroll:
            raise SkipAction()
        self.scroll_page_left()

    def action_page_right(self) -> None:
        if not self.allow_horizontal_scroll:
            raise SkipAction()
        self.scroll_page_right()

    def notify(
        self,
        message: str,
        *,
        title: str = "",
        severity: SeverityLevel = "information",
        timeout: float | None = None,
        markup: bool = True,
    ) -> None:
        """Create a notification.

        !!! tip

            This method is thread-safe.

        Args:
            message: The message for the notification.
            title: The title for the notification.
            severity: The severity of the notification.
            timeout: The timeout (in seconds) for the notification, or `None` for default.
            markup: Render the message as content markup?

        See [`App.notify`][textual.app.App.notify] for the full
        documentation for this method.
        """
        if timeout is None:
            return self.app.notify(
                message,
                title=title,
                severity=severity,
                markup=markup,
            )
        else:
            return self.app.notify(
                message,
                title=title,
                severity=severity,
                timeout=timeout,
                markup=markup,
            )

    def action_notify(
        self,
        message: str,
        title: str = "",
        severity: str = "information",
        markup: bool = True,
    ) -> None:
        self.notify(
            message,
            title=title,
            severity=severity,
            markup=markup,
        )
