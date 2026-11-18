# KVM layout: monitors, not just computers

Status: step 1 of 4 in progress (monitor reporting).

## The problem

Bragi's KVM setup puts each computer in one cell of a 5×3 grid and writes a
Deskflow link to each direct neighbour. That can't describe a common desk:

```
[ sage, left monitor ][ sage, right monitor ]
[ sagedeck          ][ sage-dev            ]
```

Deskflow's GUI has the same limit (`kServerGridWidth = 5`,
`kServerGridHeight = 3`, full-edge links only), and so do Synergy 1, Barrier
and Input Leap. It's the oldest open layout request upstream:
[deskflow#6257](https://github.com/deskflow/deskflow/issues/6257) (2018), and
a maintainer's answer is to edit the config file by hand
([deskflow#5837](https://github.com/deskflow/deskflow/issues/5837)). The
other recurring complaints:

- The cursor lands at the wrong height after crossing, because positions
  scale to the whole computer
  ([#3744](https://github.com/deskflow/deskflow/issues/3744),
  [#4292](https://github.com/deskflow/deskflow/issues/4292)).
- Dead zones where a computer's monitors don't fill its bounding box, and
  screens that can't be reached when their edges don't line up
  ([#5902](https://github.com/deskflow/deskflow/issues/5902),
  [#6150](https://github.com/deskflow/deskflow/issues/6150)).
- Layouts that look right in the editor but behave differently.

The best-liked editors (ShareMouse's Monitor Manager, Windows' Display
settings) draw one rectangle per monitor, sized like the real screen, that
snap together.

## What Deskflow can already do

`src/lib/server/Config.cpp` accepts partial edges:
`<side>(start,end) = <computer>(start,end)`, in percent of the computer's
whole bounding box, half-open. Omitted ranges mean 0–100. The position maps
linearly between the two ranges, uncovered parts of an edge are walls, and
each direction is its own line. So the desk above is:

```
sage:
    down(0,60)   = sagedeck(0,100)
    down(60,100) = sage-dev(0,100)
sagedeck:
    up    = sage(0,60)
    right = sage-dev
sage-dev:
    up    = sage(60,100)
    left  = sagedeck
```

(60 is an example: the left monitor taking 60% of the width.) The Deskflow GUI
can't write this; Bragi can.

## Design

### 1. Tray apps report their monitors (this step)

Each Bragi tray app sends `{"type": "monitors", "source", "monitors": [...]}`
on the peer socket after it connects and whenever its monitors change
(checked every 3 s on Windows, 5 s on Linux; only changes are sent). Each
monitor has:

- `x, y, width, height`: its rectangle in the computer's desktop
  coordinates, the space Deskflow's client uses there: physical pixels on
  Windows (read per-monitor DPI aware), logical pixels on KDE
  (`kscreen-doctor -j`, position as given, size divided by the scale).
- `pixel_width, pixel_height, scale, rotation, primary`.
- `id, name, width_mm, height_mm` from the EDID (Windows registry under
  `Enum\DISPLAY\...\Device Parameters`, Linux `/sys/class/drm/card*-*/edid`,
  both readable without admin rights). The id is maker, product code and
  serial, so the same monitor is recognised on any computer: the TV is the
  TV whichever computer its cable is in. Without an EDID the id is
  `connector-<name>`.

Mirrored monitors (KDE's "Replica of", Windows' "Duplicate these
displays") show a copy of another monitor, so they have no desktop area of
their own. They're listed under the monitor they copy, with their own
identity, so Bragi still knows where the TV is: sagedeck in its dock shows
the MSI monitor and mirrors it on the LG TV. KDE keeps reporting a
replica's old position, which means nothing while it mirrors.

EDID sizes are a hint only. TVs put an aspect ratio in the detailed timing
(an LG TV reports 160×90 mm and 160×90 cm), and some panels put nonsense
there (the Legion Go's 12×19 cm screen reports 698×392 mm). The base
block's centimetres win unless the timing's millimetres agree with them.

The host (`app/monitors.py`) keeps the latest report per computer and every
monitor seen, by id, with the computer it was last on, in
`data/monitors.yaml`. KVM setup lists them.

### 2. The editor: computers as groups of monitors

- Each computer is drawn as its monitors, arranged inside the group exactly
  as its own display settings have them. That arrangement is the one
  Deskflow works in, so Bragi doesn't let it be changed here.
- The user places the groups relative to each other on a snapping canvas.
- Group size has three settings: as the OS shows it (default: logical size,
  so 4K at 150% looks like 2560 wide), physical size from EDID, or resized by
  hand. Physical position matters to some desks and not to others.
- Where groups touch is where the mouse crosses. The editor highlights those
  edges and warns about computers or monitors nothing can reach.
- Bragi turns each shared edge into Deskflow ranges, converting from the
  canvas to each computer's own desktop coordinates monitor by monitor, so a
  split lands exactly on a seam.
- A computer without a tray app (or that hasn't reported) is a plain tile,
  sized by hand. People with simple desks see a grid of tiles as today.

### 3. Real monitor shapes in the Deskflow server (our fork)

The mouse is on the Bragi host, so its Deskflow server moves the cursor on
every computer. It only knows a computer as one rectangle, which is where
dead zones and unreachable edges come from. With each computer's monitors
from step 1, the fork can treat a computer as the union of its monitors:

- Keep the cursor on real screens inside a computer, moving between its
  monitors as the OS does.
- Leave a computer from the outer edge of the monitor the cursor is on, even
  when that edge is above or below the computer's bounding box edge
  (L-shapes, offset monitors).
- Enter onto the nearest real screen on the side the cursor came from, never
  into an empty corner.

This changes only the server; the Deskflow clients on the computers stay
as they are. Without the fork, links fall back to plain Deskflow edges.

### 4. Layouts per setup

Setups change: monitors switched off or unplugged, a handheld docked or not,
a TV on a long HDMI cable moved between computers. A setup is the set of
connected computers and each one's monitors (by EDID id; a monitor that is
only asleep doesn't count as a change). Each setup can have its own saved
layout; when the setup changes, Bragi switches to its layout and reloads
Deskflow (the computers stay connected). A new setup keeps the current
layout and offers to arrange one, starting from the closest saved layout.
The dashboard shows which layout is active and allows a manual switch.
