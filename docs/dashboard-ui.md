# The dashboard UI

Server-rendered Jinja templates, htmx for two form posts, one hand-written
`ws.js` for everything live. No framework, no build step.

The initial load is `GET /` returning `index.html`. Every update after that
comes over one WebSocket per browser tab. How that socket works, and the
race conditions behind its odd-looking guards, is
[`realtime-control-plane.md`](realtime-control-plane.md). This doc is the
markup and CSS.

## Template tree

```
index.html                     page shell
├── _headset_card.html          one local USB headset
├── _peers_list.html            loops _peer_card over `peers`
│   └── _peer_card.html         one peer
├── _fader.html                 the fader() macro (used by both cards)
└── _icons.html                 inline SVG icon macros
```

- **`index.html`** is the shell: one 44px toolbar (`#top-bar`: the desktop
  mic picker, Add peer, visualizer settings, the `%`/dB toggle, connection
  status), the two `<dialog>`s (visualizer settings, add peer), and
  `main#console`, a single row of devices. The `<body>` gets class
  `viz-enabled` when the level meters are on, which is the single switch
  the CSS reads.
- **`_microphone.html`** is the toolbar's desktop-mic picker. It posts on
  `change` (`hx-trigger="change"`) and swaps `#microphone-routing`.
- **`_headset_card.html`** renders one headset as a `.device`. The status
  dot is derived in-template from `enabled` plus whether playback and
  capture are connected (`online` / `offline` / `partial` / `disabled`).
  Two strips: Speakers (`playback`) and Mic (`capture`). A disabled headset
  folds down to its header.
- **`_peer_card.html`** renders one peer as a `.device`: the name on its
  own line (up to two lines, with the status dot), then a bar with the
  reorder grip, a meta line (`Roc` or `VBAN` and the IP, or for a phone its
  own status) and the remove button for managed peers, Android's
  Send/Listen toggles, then two strips:
  `outgoing` (labelled Mic, or Headset for a phone) and `incoming` (Audio,
  or Mic for a phone in microphone mode). The full direction is in each
  strip's `title`. The status dot uses real tray-app reachability when
  `view.client_connected` is not `None`, otherwise whether both directions
  are connected. Phone controls sit above the strips so every device's
  faders line up.
- **`_peers_list.html`** is just the `_peer_card` loop. It exists as its
  own file because `#peers` is the htmx swap target for add and remove, so
  that fragment has to be renderable on its own.
- **`_fader.html`** is the `fader(target, key, direction, volume,
  connected, max=1.5, show_level=False)` macro: the pointer-driven fader
  (track, fill, thumb with the `.readout` on it) and, when `show_level` is
  set, a separate `.level-meter`. The fill is the volume *setting*; the
  level meter is the live signal. Position is a `--frac` custom property on
  `.fader` (and `--level` on `.level-fill`); CSS turns it into a vertical or
  horizontal position, so the JS doesn't know the layout.
- **`_icons.html`** is inline SVG macros (`power`, `speaker_mute`,
  `mic_mute`, `settings`, `remove`), stroked with `currentColor` so button
  state colours apply for free. No icon font.

## Device order and the parked stack

Every device carries an inline `style="order: N"` from `order_of` in the
state (`app/layout.py`). Dragging a device's grip reorders the row (the
column on a phone) by rewriting those `order` values, then sends
`set_order`; the Pi saves it to `data/layout.yaml` and broadcasts an
`order` message so every tab follows. Ordering uses the CSS `order`
property, not DOM moves, because headsets and peers live in different
containers and `#peers` (the htmx swap target) is `display: contents`.

Switched-off headsets, and peers with neither direction in the graph (a
phone whose app isn't running), render inside `#parked`, a stack at the
end of the row folded to the header. `#parked` lives in `_peers_list.html`,
inside the `#peers` swap target, so adding or removing a peer re-renders it
from the server's view as well. When a device switches off, goes offline
or comes back, `ws.js` (`placeDevice`, called after every direction
update) moves it between `#parked` and the row; its `order` value puts it
back where it was. Desktop Roc peers are never parked: Bragi's modules
keep their nodes alive even when the machine is off, and their status dot
shows the tray app's reachability instead.

## The data contract

Templates and WebSocket broadcasts both consume the dict from
`app/views.py` (`build_state()` for the whole page, `peer_view()` /
`headset_view()` for fragments).

A "direction view" is the unit every strip renders from:

```python
{"volume": float | None, "muted": bool, "connected": bool, "balance": float}
```

- `connected` drives the `·off` marker in the strip label and the
  `disabled` attribute on the fader and mute button.
- `balance` is in `[-1.0, 1.0]` and positions the pad dot
  (`left: (balance + 1) / 2 * 100%`).
- `volume` is `None` when the node does not exist yet; the fader macro
  falls back to `1.0` for display.

## How a control talks to the server

Faders, pads, and buttons carry `data-target`, `data-key`,
`data-direction`, and `data-action` attributes. `ws.js` reads those to
build the action message it sends over the socket. Adding a control means
emitting those attributes and handling the verb in
`app/ws.py::apply_action`.

`data-action` values in use: `volume`, `balance` (pad drag),
`balance-pad` (the pad element itself), `mute`, `toggle_enabled`, and the
global `set_viz_enabled` from the settings dialog.

## htmx, and where it stops

htmx handles three things:

- the add-peer form in its dialog: `hx-post="/peers"`, swaps `#peers`.
  `ws.js` closes the dialog on success and shows the server's `detail` on
  failure, since htmx doesn't swap error responses.
- the per-peer remove button: `hx-post="/peers/<name>/delete"`, swaps
  `#peers`, with `hx-confirm`
- the desktop mic picker: `hx-post="/audio/microphone"` on change

Everything else is the WebSocket. This split is deliberate. Add and remove
are infrequent and structural, so a plain form post that re-renders the
peer list is fine. Live control needs the socket's coalescing and ordering,
which htmx has no equivalent for.

## CSS

One file, `app/static/style.css`, plain CSS with no build step, divided by
comment banners. Theme colours are custom properties on `:root`: neutral
dark greys and one muted green accent, following the user's Uncodixify
guide (no pills, no uppercase labels, no blue).

The layout has to fit a 1080p browser window without scrolling, and half
of one (about 960px wide), which is how the dashboard is most often used.
Devices wrap onto a second row rather than scrolling sideways; `ws.js`
counts the rows into `--rows`, and `--fader-height` gives each row its
share of the window height after the device chrome
(`clamp(110px, (100vh - 68px) / rows - 240px, 420px)`). Strips are 58px
wide, 48px between 641px and 1280px. A device's width comes from its two
strips only; names and status text truncate rather than widen it.

Below 640px wide the same markup becomes rows: each strip is a small grid
(label and balance on top, a horizontal fader and mute below), and the
fader, fill, thumb and level meter switch axis. `ws.js` reads the fader's
orientation from its size when mapping a pointer. Horizontal faders use
`touch-action: pan-y` so a vertical swipe still scrolls the page.

Level-meter visibility is pure CSS, keyed off `body.viz-enabled`. There is
no per-element `hidden` toggling in the templates or JS for this. One
source of truth for on and off.

Screenshots at 1920x950 and 412 wide, plus a Playwright pass over every
control, were how the 2026-10 redesign was checked; the preview browser in
the dev environment can't reach the local dev server.
