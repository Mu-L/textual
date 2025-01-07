---
draft: false
date: 2024-12-12
categories:
  - Release
title: "Algorithms for high performance terminal apps"
authors:
  - willmcgugan
---


I've had the fortune of being able to work fulltime on a FOSS project for the last three plus years.


<div style="width:250px;float:right;margin:10px;max-width:50%;">
<a href="https://github.com/textualize/textual-demo">
--8<-- "docs/blog/images/textual-demo.svg"
</a>
</div>


Textual has been a constant source of programming challenges.
Often frustrating but never boring, the challenges arise because the terminal "specification" says nothing about how to build a modern User Interface.
The building blocks are there: after some effort you can move the cursor, write colored text, read keys and mouse movements, but that's about it.
Everything else we had to build from scratch. From the most basic [button](https://textual.textualize.io/widget_gallery/#button) to a syntax highlighted [TextArea](https://textual.textualize.io/widget_gallery/#textarea), and everything along the way.

I wanted to write-up some of the solutions we came up with, and the 1.0 milestone we just passed makes this a perfect time.

<!-- more -->

If you haven't followed along with Textual's development, here is a demo of what it can do.
This is a Textual app, running remotely, served by your browser:

<a href="https://textual-web.io/textualize/demo" target="_blank">
Launch Textual Demo
</a>

I cheaped out on the VMs &mdash; so if the demo is down, you could run it locally (with [uv](https://docs.astral.sh/uv/)):

```
uvx --python 3.12 textual-demo
```



## The Compositor

The first component of Textual I want to cover is the [compositor](https://github.com/Textualize/textual/blob/main/src/textual/_compositor.py).
The job of the compositor is to combine content from multiple sources into a single view.

We do this because the terminal itself has no notion of overlapping windows in the way a desktop does.

Here's a video I generated over a year ago, demonstrating the compositor:

<div class="video-wrapper">
<iframe width="100%" height="auto" src="https://www.youtube.com/embed/T8PZjUVVb50" title="" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share" allowfullscreen></iframe>
</div>

### The algorithm

You could be forgiven in thinking that the terminal is regular grid of characters and we can treat it like a 2D array.
If that were the case, we could use [painter's algorithm](https://en.wikipedia.org/wiki/Painter's_algorithm) to handle the overlapping widgets.
In other words, sort them back to front and render them as though they were bitmaps.

Unfortunately the terminal is *not* a true grid.
Some characters such as those in Asian languages and many emoji are double the width of latin alphabet characters &mdash; which complicates things (to put it mildly).

Textual's way of handling this is inherited from [Rich](https://github.com/Textualize/rich).
Anything you print in Rich, first generates a list of [Segments](https://github.com/Textualize/rich/blob/master/rich/segment.py) which consist of a string and associated style.
These Segments are converted into text with [ansi escape codes](https://en.wikipedia.org/wiki/ANSI_escape_code) at the very end of the process.


The compositor takes lists of segments generated by widgets and further processes them, by dividing and combining, to produce the final output. 
In fact almost everything Textual does involves processing these segments in one way or another.

!!! tip "Switch the Primitive"
    
    If a problem is intractable, it can often be simplified by changing what you consider to be the atomic data and operations you are working with.
    I call this "switching the primitive".
    In Rich this was switching from thinking in characters to thinking in segments.

### Thinking in Segments 

In the following illustration we have an app with three widgets; the background "screen" (in blue) plus two floating widgets (in red and green).
There will be many more widgets in a typical app, but this is enough to show how it works.


<div class="excalidraw">
--8<-- "docs/blog/images/compositor/widgets.excalidraw.svg"
</div>

The lines are lists of Segments produced by the widget renderer.
The compositor will combine those lists in to a single list where nothing overlaps.

To illustrate how this process works, let's consider the highlighted line about a quarter of the way down.


### Compositing a line

Imagine you could view the terminal and widgets side on, so that you see a cross section of the terminal and the floating widgets.
It would appear something like the following:

<div class="excalidraw">
--8<-- "docs/blog/images/compositor/cuts0.excalidraw.svg"
</div>

We can't yet display the output as it would require writing each "layer" independently, potentially making the terminal flicker, and certainly writing more data than necessary.

We need a few more steps to combine these lines in to a single line.


### Step 1. Finding the cuts.

First thing the compositor does is to find every offset where a list of segments begins or ends.
We call these "cuts".

<div class="excalidraw">
--8<-- "docs/blog/images/compositor/cuts1.excalidraw.svg"
</div>

### Step 2. Applying the cuts.

The next step is to divide every list of segments at the cut offsets.
This will produce smaller lists of segments, which we refer to as *chops*.

<div class="excalidraw">
--8<-- "docs/blog/images/compositor/cuts2.excalidraw.svg"
</div>

After this step we have lists of chops where each chop is of the same size, and therefore nothing overlaps.
It's the non-overlapping property that makes the next step possible.

### Step 3. Discard chops.

Only the top-most chops will actually be visible to the viewer.
Anything not at the top will be occluded and can be thrown away.

<div class="excalidraw">
--8<-- "docs/blog/images/compositor/cuts3.excalidraw.svg"
</div>

### Step 4. Combine.

Now all that's left is to combine the top-most chops in to a single list of Segments.
It is this list of segments that becomes a line in the terminal.

<div class="excalidraw">
--8<-- "docs/blog/images/compositor/cuts4.excalidraw.svg"
</div>

As this is the final step in the process, these lines of segments will ultimately be converted to text plus escape sequences and written to the output.

### What I omitted

There is more going on than this explanation may suggest.
Widgets may contain other widgets which are clipped to their *parent's* boundaries, and widgets that contain other widgets may also scroll &mdash; the compositor must take all of this in to account.

!!! info "It's widgets all the way down"

    Not to mention there can be multiple "screens" of widgets stacked on top of each other, with a modal fade effect applied to lower screens. 

The compositor can also do partial updates.
In other words, if you click a button and it changes color, the compositor can update just the region occupied by the button.

The compositor does all of this fast enough to enable smooth scrolling, even with a metric tonne of widgets on screen.

## Spatial map

Textual apps typically contain many widgets of different sizes and at different locations within the terminal.
Not all of which widgets may be visible in the final view (if they are within a scrolling container).


!!! info "The smallest Widget"

    While it is possible to have a widget as small as a single character, I've never found a need for one.
    The closest we get in Textual is a [scrollbar corner](https://textual.textualize.io/api/scrollbar/#textual.scrollbar.ScrollBarCorner);
    a widget which exists to fill the space made when a vertical scrollbar and a horizontal scrollbar meet.
    It does nothing because it doesn't need to, but it is powered by an async task like all widgets and can receive input.
    I have often wondered if there could be something useful in there.
    A game perhaps?
    If you can think of a game that can be played in 2 characters &mdash; let me know!

The *spatial map*[^1] is a data structure used by the compositor to very quickly discard widgets that are not visible within a given region.
The algorithm it uses may be familiar if you have done any classic game-dev.


### The problem 

Consider the following arrangement of widgets:

<div class="excalidraw">
--8<-- "docs/blog/images/compositor/spatial-map.excalidraw.svg"
</div>

Here we have 8 widgets, where only 3 or 4 will be visible at any given time, depending on the position of the scrollbar.
We want to avoid doing work on widgets which will not be seen in the next frame.

A naive solution to this would be to check each widget's [Region][textual.geometry.Region] to see if it overlaps with the visible area.
This is a perfectly reasonable solution, but it won't scale well.
If we get in to the 1000s of widgets territory, it may become significant &mdash; and we may have to do this 30 times a second if we are scrolling.

### The Grid

The first step in the spatial map is to associate every widget with a tile in a regular grid[^2].

<div class="excalidraw">
--8<-- "docs/blog/images/compositor/spatial-map-grid.excalidraw.svg"
</div>

The size of the grid is fairly arbitrary, but it should be large enough to cover the viewable area with a relatively small number of grid tiles.
We use a grid size of 100 characters by 20 lines, which seems about right.

When the spatial map is first created it places each widget in one or more grid tiles.
At the end of that process we have a dict that maps every grid coordinate on to a list of widgets, which will look something like the following:

```python
{
    (0, 0): [widget1, widget2, widget3],
    (1, 0): [widget1, widget2, widget3],
    (0, 1): [widget4, widget5, widget6],
    (1, 1): [widget4, widget5, widget6],
    (0, 2): [widget7, widget8],
    (1, 2): [Widget7, widget8]
}
```

The up-front cost of [calculating](https://github.com/Textualize/textual/blob/main/src/textual/_spatial_map.py) this data is fairly low.
It is also very cacheable &mdash; we *do not* need to recalculate it when the user is just scrolling.

### Search the grid

The speedups from the spatial map come when we want to know which widgets are visible.
To do that, we first create a region that covers the area we want to consider &mdash; which may be the entire screen, or a smaller scrollable container.

In the following illustration we have scrolled the screen up[^3] a little so that Widget 3 is at the top of the screen:

<div class="excalidraw">
--8<-- "docs/blog/images/compositor/spatial-map-view1.excalidraw.svg"
</div>

We then determine which grid tiles overlap the viewable area.
In the above examples that would be the tiles with coordinates  `(0,0)`, `(1,0)`, `(0,1)`, and `(1,1)`.
Once we have that information, we can then then look up those coordinates in the spatial map data structure, which would retrieve 4 lists:

```python
[
  [widget1, widget2, widget3],
  [widget1, widget2, widget3],
  [widget4, widget5, widget6],
  [widget4, widget5, widget6],
]
```

Combining those together and de-duplicating we get:

```python
[widget1, widget2, widget3, widget4, widget5, widget6]
```

These widgets are either within the viewable area, or close by.
We can confidently conclude that the widgets *not* ion that list are hidden from view.
If we need to know precisely which widgets are visible we can check their regions individually.

The useful property of this algorithm is that as the number of widgets increases, the time it takes to figure out which are visible stays relatively constant. Scrolling a view of 8 widgets, takes much the same time as a view of 1000 widgets or more.

The code for our `SpatialMap` isn't part of the public API and therefore not in the docs, but if you are interested you can check it out here: [_spatial_map.py](https://github.com/Textualize/textual/blob/main/src/textual/_spatial_map.py).

## Wrapping up

If any of the code discussed here interests you, you have my blessing to [steal the code](./steal-this-code.md)!

As always, if you want to discuss this or Textual in general, we can be found on our [Discord server](https://discord.gg/Enf6Z3qhVr).



[^1]: A term I coined for the structure in Textual. There may be other unconnected things known as spatial maps.
[^2]: The [grid](https://www.youtube.com/watch?v=lILHEnz8fTk&ab_channel=DaftPunk-Topic).
[^3]: If you scroll the screen up, it moves *down* relative to the widgets.