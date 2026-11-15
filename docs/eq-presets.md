# EQ presets for the HyperX Cloud III S Wireless

Where the built-in presets in `app/eq_presets_builtin.yaml` come from, what
was measured, and the limits they respect. Researched 2026-11-15.

Bands (index 0-9): 32, 64, 125, 250, 500, 1k, 2k, 4k, 8k, 16k Hz. Gains in
dB. The dongle takes dB × 100 as a signed 16-bit value, one op `0x5F`
report per band, with a commit byte on band 9 [S1][S2]. See
`app/headset_hid.py`.

Each preset's basis is one of:
- **Sourced**: values copied from a named source.
- **Derived**: computed from a cited measurement or standard, by the
  method given here. Nobody else has published these exact numbers.
- **Reasoned**: a judgment call based on the cited principles, not a
  published preset.

## The presets

| Name | Description | 32 | 64 | 125 | 250 | 500 | 1k | 2k | 4k | 8k | 16k | Basis |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Flat | The headset's own sound, unchanged. | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | Sourced: NGENUITY's "EQ off" sends all bands at 0 [S1] |
| Balanced (measured) | Small fixes for this headset's own quirks, so it sounds more natural. | 2.7 | 1.1 | -0.4 | 2.3 | -1.0 | -0.2 | -0.9 | 2.8 | -1.6 | 0 | Derived: fitted to SoundGuys' B&K 5128 measurement of the Cloud III S ([measurement](#measurement-based-correction)) |
| Bass boost | More thump and rumble for music, explosions and engines. | 5.0 | 4.5 | 3.0 | 1.0 | 0 | 0 | 0 | 0 | 0 | 0 | Reasoned. In Harman's research about 15% of listeners want 3-6 dB more bass below ~300 Hz than the target [S10]. Close to HyperHeadset's and Nogenuity's "Bass" presets [S3][S5] |
| Less bass | Lighter, less boomy low end. Good if bass feels heavy or tiring. | -3.0 | -3.0 | -2.0 | -1.0 | 0 | 0 | 0 | 0 | 0 | 0 | Reasoned. In Harman's research about 21% of listeners prefer 2-3 dB less bass than the target [S10] |
| Voice clarity | Makes speech easier to follow in podcasts, calls and videos. | -4.0 | -3.0 | -1.5 | 0 | 0 | 1.0 | 3.0 | 2.5 | 0.5 | 0 | Reasoned. The 2 kHz octave band carries the most speech intelligibility [S13]. Cutting low bass removes rumble that masks speech |
| Footsteps | Quieter booms, crisper small sounds such as steps and reloads in games. | -4.0 | -3.0 | -2.0 | -1.0 | 0 | 0.5 | 2.0 | 3.5 | 2.0 | 0 | Reasoned. Gaming guides agree on +2 to +4 dB at 2-4 kHz, without measurements behind it [S16]. Same shape as Nogenuity's "Footsteps" [S5] |
| Music | A gentle smile curve: fuller bass and a little extra sparkle. | 4.0 | 3.0 | 1.0 | 0 | -1.0 | -1.5 | -0.5 | 1.0 | 2.5 | 2.0 | Reasoned. A milder version of HyperHeadset's and Nogenuity's V-shapes [S3][S5] |
| Movies | Bigger effects and rumble, with dialogue still clear. | 4.0 | 3.5 | 1.5 | 0 | -0.5 | 0 | 1.5 | 1.5 | 0.5 | 0 | Reasoned. Close to Nogenuity's "Cinema" [S5] |
| Late night | Keeps bass and detail audible when listening quietly. | 4.5 | 4.0 | 2.5 | 1.5 | 0.5 | 0 | -0.5 | -0.5 | 0 | 1.5 | Derived: about half the ISO 226:2003 difference between the 60-phon and 80-phon equal-loudness contours ([basis](#basis-for-the-general-presets)) [S11][S12] |
| Smooth | Tames sharp s sounds and shrill highs for long sessions. | 0 | 0 | 0 | 0 | 0 | 0 | -1.0 | -3.0 | -3.5 | -2.0 | Reasoned. Sibilance sits around 4-10 kHz [S14][S15], and the Cloud III S measures about +3 dB above target near 6 kHz and 10 kHz [S7] |
| Brighter | More detail and air in the highs. | 0 | 0 | 0 | 0 | 0 | 0 | 1.0 | 2.0 | 3.0 | 3.0 | Reasoned. Rtings rates the Cloud III Wireless treble "slightly underemphasized (-3 dB)" [S8]. The SoundGuys Cloud III S unit isn't dark, so this is a taste option, not a fix |

Every band stays within ±6 dB. Boosts stop at +5 dB, and Late night peaks
at +4.5 dB at 32 Hz. Five presets use only cuts or small boosts.

Neighbouring bands add up, so the combined change can be larger than any
one slider. This table predicts the combined response, assuming the bands
are standard peaking filters. The headset's filter Q is unknown (see
[Range, filter shape and limits](#range-filter-shape-and-limits)), so it's
computed at three Q values:

| Preset | Largest single band | Predicted combined peak, Q 1.41 / 1.0 / 0.7 |
|---|---|---|
| Bass boost | +5.0 | +6.1 / +7.1 / +8.6 dB around 60 Hz |
| Late night | +4.5 | +5.4 / +6.4 / +7.7 dB around 55-63 Hz. A draft with +6 at 32 Hz came to +7.1 / +8.2 / +10.0, so it was lowered |
| Music | +4.0 | +4.6 / +5.1 / +5.9 dB around 35-40 Hz |
| Movies | +4.0 | +4.7 / +5.3 / +6.4 dB |
| Footsteps | +3.5 | +4.2 / +4.6 / +5.3 dB at 4 kHz |
| Voice clarity | +3.0 | +3.6 / +4.1 / +4.8 dB at 2 kHz |
| Brighter | +3.0 | +3.5 / +4.0 / +4.6 dB at 8 kHz |
| Balanced (measured) | +2.8 | +2.9 / +3.1 / +3.3 dB |

## NGENUITY's own presets

No source publishes the band values of NGENUITY's built-in presets for the
Cloud III S or any other Cloud III model.

Some names are known from secondary sources. 9to5Toys, reviewing the wired
Cloud III, lists "bass boost and cut, clarity, and my personal favorite,
optimizer", and notes the UI shows no dB scale [S9]. On the wired Cloud
III the EQ runs in a Windows audio driver, not in the headset [S5], so its
list may differ from the Cloud III S, whose EQ runs in the dongle. A
Reddit thread (blocked to automated access, so unverified) mentions "Bass
Boost, Treble Cut, and Clarity".

The only EQ capture from the Cloud III S is a user-made "Test Preset" in
the zip attached to HeadsetControl issue 486 (NGENUITY 2.1.2, dongle
firmware 4.1.2.4, PID 0x02CC) [S1]:

| 32 | 64 | 125 | 250 | 500 | 1k | 2k | 4k | 8k | 16k |
|---|---|---|---|---|---|---|---|---|---|
| -1.25 | +2.76 | +5.11 | +5.65 | +6.09 | +5.85 | +3.69 | +2.76 | +1.88 | -1.25 |

It shows that NGENUITY uses centi-dB resolution in practice, and that "EQ
off" sends every band at 0. The device can't be asked for its current EQ.
HyperHeadset's maintainers call that "an inherent firmware limitation (no
EQ query support)" [S4], which is why Bragi remembers the EQ it last sent.
Captures of each factory preset, taken with the Frida hook in that zip
(`scripts/hidhook.js`), would fill this gap.

## Measurement-based correction

### Cloud III S Wireless, measured by SoundGuys (B&K 5128)

SoundGuys measured the Cloud III S itself on a B&K 5128 head, against
their Headphone Preference Curve [S7]. The page doesn't say which
connection mode was used. Both curves were read from the page's SVG chart.

Measured minus target (positive means louder than target):

| Hz | 32 | 64 | 125 | 250 | 500 | 1k | 2k | 3k | 4k | 6k | 8k | 10k | 12.5k | 16k |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| dB | -2.3 | -1.6 | -0.2 | -1.5 | +1.6 | +0.4 | +1.0 | -2.2 | -2.7 | +3.1 | +0.5 | +3.0 | -1.5 | -2.9 |

It's already close to the target, about 1.5 dB RMS over 20 Hz-15 kHz.
Bass is slightly shy, with a small dip around 200-315 Hz. There's a slight
dip at 3-4 kHz and narrow excess near 6 kHz and 10 kHz.

The 10-band fits use a least-squares fit of RBJ peaking filters at Q 1.41
(AutoEq's convention for graphic EQs), over 20 Hz-15 kHz at 1/12-octave
resolution:
- Against the SoundGuys target: `2.9, 1.0, -0.3, 2.7, -1.3, -0.2, -0.3, 1.9, -2.1, 2.2`.
  RMS error drops from 1.48 to 0.93 dB.
- Against AutoEq's Harman-type 5128 target, from the AutoEq Cloud III
  Wireless CSV: `2.4, 1.2, -0.5, 1.9, -0.7, -0.2, -1.4, 3.7, -1.1, -4.6`.

The two agree within about 1 dB except at 16 kHz, where the targets differ
and 5128 data is unreliable. "Balanced (measured)" is their average with
16 kHz set to 0: `2.7, 1.1, -0.4, 2.3, -1.0, -0.2, -0.9, 2.8, -1.6, 0.0`.

As a check, the same fitting run on the Rtings Cloud III Wireless data
reproduced AutoEq's published fixed-band result within about 0.3 dB, apart
from a constant offset and the 16 kHz band.

### AutoEq entries (Rtings measurements) [S6]

These are not the S model. AutoEq has no Cloud III S entry, and neither
oratory1990 nor Crinacle has a Cloud III entry.

**HyperX Cloud III Wireless**, Rtings, B&K 5128 (`results/Rtings/Bruel & Kjaer 5128 over-ear/HyperX Cloud III Wireless/`, changed 2025-05-14):
- Fixed-band EQ (Q 1.41, preamp -11.8 dB): `+3.0, -1.0, -0.4, +2.3, -1.6, -3.1, +0.5, +3.4, -2.0, +12.0`.
- Parametric EQ (preamp -6.0 dB): LS 105 Hz +2.0 Q0.7, PK 80 Hz -4.0
  Q2.39, PK 274 Hz +3.3 Q0.93, PK 1280 Hz -3.7 Q0.23, PK 2989 Hz +7.8 Q1.35,
  PK 35 Hz +0.7 Q3.3, PK 102 Hz -0.1 Q3.15, PK 680 Hz +0.4 Q5.05, PK 6436 Hz
  -3.6 Q5.93, HS 10 kHz +6.0 Q0.7.
- Ignore the +12 dB at 16 kHz. The Rtings unit rolls off steeply above
  10 kHz (-12.8 dB at 16 kHz relative to 1 kHz) and the SoundGuys Cloud III
  S unit doesn't (-4.2 dB). Upper-treble readings on a 5128 also depend
  heavily on fit. That band is what forces the -11.8 dB preamp.

**HyperX Cloud III (wired)**, Rtings, HMS II.3 (an older rig and target):
fixed-band EQ (preamp -7.6 dB) `+7.1, +1.7, +3.5, +0.9, -0.7, -2.3, -1.3, +4.8, -5.3, -2.4`.

### Do the S and the non-S sound alike?

Tom's Hardware says the Cloud III S uses the same 53 mm drivers as the
Cloud III and Cloud III Wireless (seen in search summaries; the article
was truncated when fetched). The two 5128 measurements, each normalised to
0 dB at 1 kHz, agree within about 2-4 dB below 8 kHz, and both dip around
200-300 Hz. The Cloud III S unit is about 3 dB hotter at 2-3 kHz and
9-10 dB hotter at 10-16 kHz.

Some of that is unit and fit variation. Rtings calls the Cloud III
Wireless's consistency "poor": glasses can noticeably reduce bass, and
larger heads can increase it [S8]. Rtings sums it up as "warm", with bass
"slightly underemphasized (-2 dB)" and treble "slightly underemphasized
(-3 dB)" [S8]. That's why "Balanced (measured)" is based on the Cloud III S
measurement, with AutoEq's Wireless numbers as a cross-check only.

No measured comparison of wired and wireless exists for the S. The EQ goes
over the dongle's vendor HID collection [S1], so it presumably applies
only on the 2.4 GHz dongle. For the wired Cloud III, 9to5Toys reports that
presets don't apply over 3.5 mm [S9].

## Basis for the general presets

- **Bass amount.** Harman's research (Olive et al.) puts most listeners
  near the Harman target. About 15% want 3-6 dB more bass below ~300 Hz,
  and about 21% want 2-3 dB less; that group skews older and female [S10].
  These figures come from secondary reports (Headphonesty, PS Audio
  Copper) of Olive's 2019 update, not the paywalled AES paper. Bass boost
  and Less bass use these sizes.
- **Late night.** As volume drops, hearing loses bass sensitivity faster
  than midrange (ISO 226:2003 equal-loudness contours) [S11][S12]. The
  difference between the 60-phon and 80-phon contours, the full correction
  for listening about 20 dB quieter, is 32 Hz +9.4, 63 +7.6, 125 +5.5,
  250 +3.2, 500 +1.2, 1k 0, 2k -0.6, 4k -0.7, 8k +0.3, 12.5k +3.2 (ISO 226
  stops at 12.5 kHz, so that value stands in for 16k). A full correction
  would exceed the ±6 dB budget, so the preset uses about half. Real
  loudness compensation follows the volume setting, and a fixed preset is
  a compromise.
- **Speech.** In octave-band intelligibility tests the 2 kHz band alone
  scored highest (55%), with scores falling further from 2 kHz [S13]. The
  Speech Intelligibility Index weights 0.25-4 kHz. Voice clarity and
  Footsteps boost 2-4 kHz modestly and cut the bass that masks it.
- **Footsteps.** Every gaming guide names 2-4 kHz, but no published
  measurement of footstep spectra turned up [S16]. The preset is a guess
  and stays moderate. One guide warns that more than +6 dB hurts
  localisation, without data.
- **Harshness and sibilance.** Mixing references put sibilance at about
  4-10 kHz. Sound On Sound's example has its main energy at 4-6 kHz with
  more above 10 kHz [S14], and Music Guy Mixing gives 5-8 kHz typical,
  4-10 kHz overall [S15]. Smooth cuts 4k and 8k, which also lowers the
  Cloud III S's measured +3 dB excess near 6 and 10 kHz [S7].
- **Other open-source presets for the same bands**, as a cross-check.
  Neither project gives a source for its values. Nogenuity designs its
  presets as Q-1 peaking filters on these octave centres, for a different
  HyperX DSP family.

  | Preset | 32 | 64 | 125 | 250 | 500 | 1k | 2k | 4k | 8k | 16k |
  |---|---|---|---|---|---|---|---|---|---|---|
  | HyperHeadset Bass Boost [S3] | 6 | 5 | 3 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
  | HyperHeadset Treble Boost [S3] | 0 | 0 | 0 | 0 | 0 | 1 | 2 | 4 | 5 | 6 |
  | HyperHeadset V-Shape [S3] | 5 | 4 | 2 | 0 | -2 | -2 | 0 | 2 | 4 | 5 |
  | HyperHeadset Vocal [S3] | -2 | -1 | 0 | 2 | 4 | 4 | 3 | 1 | 0 | -1 |
  | Nogenuity Bass [S5] | 5 | 5 | 3 | 1 | 0 | 0 | 0 | 0 | 0 | 0 |
  | Nogenuity Footsteps [S5] | -3 | -3 | -2 | -1 | 0 | 1 | 3 | 5 | 3 | 0 |
  | Nogenuity Voice [S5] | -4 | -3 | -1 | 0 | 1 | 3 | 4 | 2 | 0 | -1 |
  | Nogenuity Music [S5] | 4 | 3 | 1 | 0 | -1 | -1 | 0 | 2 | 3 | 3 |
  | Nogenuity Cinema [S5] | 4 | 4 | 2 | 0 | 0 | 1 | 2 | 1 | 1 | 0 |
  | Nogenuity Bright [S5] | 0 | 0 | 0 | 0 | 0 | 0 | 1 | 3 | 4 | 4 |
  | Nogenuity Warm [S5] | 2 | 2 | 2 | 2 | 1 | 0 | -1 | -2 | -3 | -3 |
  | Nogenuity Soft [S5] | 0 | 0 | 0 | 0 | 0 | 0 | -1 | -3 | -4 | -4 |

## Range, filter shape and limits

**Gain range.** Nothing documents NGENUITY's slider range for the Cloud
III S, and its UI shows no dB scale [S9]. The firmware field is signed
16-bit, which could encode ±327 dB, so it says nothing about the real
limit. The largest value seen from NGENUITY is +6.09 dB, in a user-made
curve [S1]. **Bragi's ±12 dB slider limit is HyperHeadset's constant, not
HyperX's** (`DB_MIN`/`DB_MAX` in `src/eq/mod.rs`, clamped at ±1200 in
`cloud_iii_s_wireless.rs`) [S3]. Nogenuity draws ±12 dB but never sends
more than ±9 dB, saying "wider values have not been tried", and that's on
other HyperX DSPs [S5]. So the built-ins stay within ±6 dB, and anything
above about +6 dB on the sliders is untested on this dongle: nobody knows
yet whether it clamps, distorts or limits.

**Filter type and Q.** Not documented. The protocol carries only a gain
per fixed band [S1]. AutoEq assumes peaking filters at Q 1.41 for graphic
EQs [S6], Nogenuity Q 1 [S5]. The combined-peak table above shows how much
it matters: neighbouring boosts add 1-4 dB more as Q goes down.

**Clipping and headroom.** A boost pushes loud passages towards digital
full scale. On a PC the usual fix is a negative preamp about equal to the
largest boost, which is why AutoEq ships one with every EQ [S6][S18]. The
dongle protocol has no preamp or master-gain field [S1]. So favour cuts
and keep boosts moderate. Lowering every band by the same amount should
work roughly like a preamp, though it's less exact and untested on this
device. HyperHeadset PR 25 reports audible cracking while a preset is
applied, because each band goes in its own packet [S4]. That's a
transient during the change, not steady clipping, but it's a reason not to
switch presets during loud audio. A community guide says "Ngenuity EQ
above 0 dB can cause distortion on some units", which is why many
community presets only cut [S19]. That's anecdotal, but it matches the
general advice.

**How big a change is audible.** Peaks are much easier to hear than dips
of the same size (Bücklein 1981) [S20]. Narrow peaks are detectable at
about 1-2 dB, broad two-octave changes need about 3.5-7 dB, and broad dips
more [S21]. In practice, steps under 1 dB rarely matter to a non-expert,
2-3 dB broad changes are clearly audible, and 4-6 dB is a strong effect.
Because seal consistency is poor, ±2-3 dB of bass can come from how the
headset sits or from glasses [S8]. Bass presets are taste, not precision.

**What Bragi does about it** (`app/headset_hid.py`):
- Every EQ change sends all 10 bands, one report each, with the commit
  byte on band 9 [S1]. The firmware ignores extra bands packed into one
  report [S3].
- The device can't report its EQ [S4], so Bragi keeps the last EQ it sent
  (`data/headset_eq.yaml`) and works out the active preset from it.
- Sliders are limited to ±12 dB. The built-in presets stay within ±6 dB.

## Sources

- [S1] HeadsetControl issue 486, neocult-de's protocol write-up and its attachment `cloud3s-contribution.zip` (`captures/EVIDENCE.md`, `captures/eq-preset.log`, `scripts/eq_set.py`): https://github.com/Sapd/HeadsetControl/issues/486, https://github.com/user-attachments/files/30478915/cloud3s-contribution.zip
- [S2] HeadsetControl issue 486, freefakesmile's comment on the vendor collection (same URL as S1).
- [S3] LennardKittner/HyperHeadset at commit 3652a5d: `src/eq/mod.rs` (±12 dB), `src/eq/presets.rs` (built-in presets), `src/devices/cloud_iii_s_wireless.rs`: https://github.com/LennardKittner/HyperHeadset
- [S4] HyperHeadset issue 56 and PR 25: https://github.com/LennardKittner/HyperHeadset/issues/56, https://github.com/LennardKittner/HyperHeadset/pull/25
- [S5] esdedev/nogenuity at commit de71cc6: `src/eq.rs`, `docs/DEVICES.md`, `docs/PROTOCOL.md`: https://github.com/esdedev/nogenuity
- [S6] AutoEq, Rtings results for the HyperX Cloud III Wireless (B&K 5128) and Cloud III (HMS II.3): https://github.com/jaakkopasanen/AutoEq/tree/master/results/Rtings/Bruel%20%26%20Kjaer%205128%20over-ear/HyperX%20Cloud%20III%20Wireless, https://github.com/jaakkopasanen/AutoEq/tree/master/results/Rtings/HMS%20II.3%20over-ear/HyperX%20Cloud%20III, README: https://github.com/jaakkopasanen/AutoEq
- [S7] SoundGuys, HyperX Cloud III S product page and lab data (B&K 5128, SoundGuys Headphone Preference Curve): https://www.soundguys.com/product/hyperx-cloud-iii-s/; curve: https://www.soundguys.com/soundguys-house-curve-54290/
- [S8] Rtings, HyperX Cloud III Wireless review: https://www.rtings.com/headphones/reviews/hyperx/cloud-iii-wireless; Cloud III review: https://www.rtings.com/headphones/reviews/hyperx/cloud-iii
- [S9] 9to5Toys, HyperX Cloud III review (2023): https://9to5toys.com/2023/06/07/hyperx-cloud-iii-review/
- [S10] Harman listener-preference groups (Olive): Headphonesty, https://www.headphonesty.com/2020/04/harman-target-curves-part-3/; PS Audio Copper interview, https://www.psaudio.com/blogs/copper/engaging-and-enthralling-music-for-all-talking-with-dr-sean-olive-of-harman-international-part-one; AES e-library, https://aes2.org/publications/elibrary-page/?id=22696
- [S11] Equal-loudness contours: https://en.wikipedia.org/wiki/Equal-loudness_contour; loudness compensation plugin reference: https://sotf.spinorama.org/reference/plugins/loudness-compensation/; Adaptive Loudness Compensation (SMC 2019): https://www.smc2019.uma.es/articles/S2/S2_01_SMC2019_paper.pdf
- [S12] ISO 226:2003 parameter tables, as used in jcarrano/rtfi `scripts/iso226.py`: https://github.com/jcarrano/rtfi/blob/master/scripts/iso226.py
- [S13] Octave-band speech intelligibility (2 kHz band highest): https://pmc.ncbi.nlm.nih.gov/articles/PMC2811548; SII band importance: https://pmc.ncbi.nlm.nih.gov/articles/pmid/20822302
- [S14] Sound On Sound, "Managing Sibilance": https://www.soundonsound.com/techniques/managing-sibilance
- [S15] Music Guy Mixing, "How to Treat Vocal Sibilance": https://www.musicguymixing.com/vocal-sibilance/
- [S16] Footstep EQ guides (weak evidence, consensus only): https://evezone.evetech.co.za/quick-bytes/eq-for-footsteps-the-frequencies-that-actually-matter, https://joltfly.com/best-eq-settings-for-footsteps-dominate-competitive-games
- [S17] HyperX NGENUITY iOS listing (written for the Cloud MIX Buds): https://apps.apple.com/us/app/-/id1604497487
- [S18] Preamp and clipping, Head-Fi "EQ Settings for 700+ Headphones": https://www.head-fi.org/threads/eq-settings-for-700-headphones.885196/page-4
- [S19] TopDailyBlog Cloud III Wireless settings guide (anecdotal distortion note): https://topdailyblog.com/mic-and-sound-settings-for-hyperx-cloud-3/
- [S20] Bücklein, "The Audibility of Frequency Response Irregularities", JAES 1981: https://aes2.org/publications/elibrary-page/?id=10291
- [S21] Perception thresholds for spectral changes: https://books.publisso.de/en/publisso_gold/export/journal/11/article/5699; Rtings peaks and dips test notes: https://www.rtings.com/headphones/tests/sound-quality/peaks-dips
- Checked, no EQ content: HeyOkay/HaloBattery `docs/protocols.md` and `providers/hyperx_cloud3s.py`, https://github.com/HeyOkay/HaloBattery; HyperX NGENUITY page, https://hyperx.com/pages/ngenuity
