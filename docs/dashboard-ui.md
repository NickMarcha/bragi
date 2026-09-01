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

- **`index.html`** is the shell: a top bar (settings button, `%`/dB unit
  toggle, WebSocket status pill), the visualizer-settings `<dialog>`, the
  card row, and the add-peer form. The `<body>` gets class `viz-enabled`
  when the level meters are on, which is the single switch the CSS reads.
- **`_headset_card.html`** renders one headset. The status dot is derived
  in-template from `enabled` plus whether playback and capture are
  connected (`online` / `offline` / `partial` / `disabled`). Two strips:
  Speakers (`playback`) and Mic (`capture`).
- **`_peer_card.html`** renders one peer. The status dot uses real tray-app
  reachability when `view.client_connected` is not `None`, otherwise it
  falls back to whether both directions are connected. Two strips: `Mic →
  <peer>` (`outgoing`) and `<peer> → headset` (`incoming`), each with a
  balance pad above the fader. A `managed` peer also gets a Remove button.
- **`_peers_list.html`** is just the `_peer_card` loop. It exists as its
  own file because `#peers` is the htmx swap target for add and remove, so
  that fragment has to be renderable on its own.
- **`_fader.html`** is the `fader(target, key, direction, volume,
  connected, max=1.5, show_level=False)` macro. It draws the custom
  pointer-driven vertical fader (track, fill, thumb) and, when `show_level`
  is set, a separate `.level-meter` element next to it. The fill is the
  volume *setting*; the level meter is the live signal. They are
  deliberately different elements.
- **`_icons.html`** is inline SVG macros (`power`, `speaker_mute`,
  `mic_mute`, `settings`, and so on), stroked with `currentColor` so button
  state colours apply for free. No icon font.

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

htmx handles exactly two things:

- the add-peer form: `hx-post="/peers"`, swaps `#peers`
- the per-peer Remove button: `hx-post="/peers/<name>/delete"`, swaps
  `#peers`, with `hx-confirm`

Everything else is the WebSocket. This split is deliberate. Add and remove
are infrequent and structural, so a plain form post that re-renders the
peer list is fine. Live control needs the socket's coalescing and ordering,
which htmx has no equivalent for.

## CSS

One file, `app/static/style.css`, around 550 lines, divided by comment
banners (`/* --- Voicemeeter-style vertical strips --- */` and so on).
Theme colours are custom properties on `:root`. The fader, the pad, and the
mute button share sizing variables so the three line up vertically in a
strip.

Level-meter visibility is pure CSS, keyed off `body.viz-enabled`. There is
no per-element `hidden` toggling in the templates or JS for this. One
source of truth for on and off.
