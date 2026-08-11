# SoftVoice for NVDA

An NVDA add-on that brings the **SoftVoice** speech synthesizer to the [NVDA screen reader](https://www.nvaccess.org/).

Compatible with NVDA 2024.4 through 2026.1, including 64-bit NVDA.

## What is SoftVoice?

SoftVoice is a **formant synthesizer** — one of the oldest continuously surviving speech engines in personal computing.

Its lineage starts with **SAM (Software Automatic Mouth)**, written by Mark Barton and published in 1982 by Don't Ask Software, which later renamed itself SoftVoice, Inc. SAM was among the first commercial software-only speech synthesizers for home computers, running on machines with 64 KB of memory. The same technology went on to power the Commodore Amiga's built-in `narrator.device`, Apple's original MacinTalk, and speech on Atari machines.

The engine bundled here is the late-1990s 32-bit Windows build of that line.

### Formant synthesis

Unlike modern TTS, SoftVoice stores no recorded human speech at all. It builds every sound from scratch using a mathematical model of the vocal tract — resonances (formants), a glottal source, and noise for fricatives and aspiration.

That design is why this add-on exposes so many unusual controls. You are not selecting from prerecorded voices; you are adjusting the model itself. It is also why the whole engine fits in well under a megabyte, and why it stays intelligible at very high speaking rates — a property many blind users specifically prefer.

The trade-off is the sound: unmistakably synthetic, and to many ears characteristic of 1980s home computing.

## Installing

Download `softvoice-*.nvda-addon` from the [Releases](../../releases) page and open it, or use **NVDA menu → Tools → Add-on store → Install from external source**.

After NVDA restarts, select it under **NVDA menu → Preferences → Settings → Speech → Change**.

## Voices and variants

Two languages are available: **English** and **Spanish**.

Twenty voice personalities ship with the engine:

| | | | |
|---|---|---|---|
| Male | Female | Large Male | Child |
| Giant Male | Mellow Female | Mellow Male | Crisp Male |
| The Fly | Robotoid | Martian | Colossus |
| Fast Fred | Old Woman | Munchkin | Troll |
| Nerd | Milktoast | Tipsy | Choirboy |

Selecting a personality other than **Male** leaves that personality's own pitch and timbre in place. Those values are only overridden once you actually move a slider while that voice is selected, so the character voices sound as intended out of the box.

## Settings

Beyond NVDA's standard rate, pitch, and inflection, the add-on exposes the engine's synthesis parameters directly:

| Setting | What it does |
|---|---|
| **Perturbation** | Random jitter in the fundamental frequency. Small amounts sound less mechanical. |
| **Vowel Factor** | Scales vowel duration and prominence. |
| **Voicing Gain** | Level of the voiced (glottal) source. |
| **Frication Gain** | Level of fricative noise — `s`, `f`, `sh`. |
| **Aspiration Gain** | Level of breath noise — `h`, and breathiness generally. |
| **Intonation Style** | `normal1`, `normal2`, `monotone`, `sung`, or `random`. |
| **Voicing Mode** | `normal`, `breathy`, or `whispered`. |
| **Gender** | Vocal tract model: `male`, `female`, `child`, or `giant`. |
| **Glottal Source** | Excitation waveform: `default`, `male`, `female`, `child`, `high`, `mellow`, `impulse`, `odd`, `colossus`. |
| **Speaking Mode** | `Natural`, `Word-at-a-time`, or `Spelled`. |
| **Use abbreviations** | When off, short all-caps words are spelled out letter by letter, so `NVDA` is not read as a word. |
| **Number processing** | Expands digits into words. Useful because the engine otherwise spells long digit runs. Choose `Off`, large numbers only, or all numbers. |
| **Pause factor** | Length of pauses at punctuation. Below 50 the engine also trims leading silence, which reduces the delay before speech starts. |

## How it works

The SoftVoice engine is a 32-bit DLL and cannot be loaded into a 64-bit process.

- **32-bit NVDA** — `synthDrivers/sv.py` loads `softvoice_wrapper.dll` directly via `ctypes`, and audio is played through NVDA's `nvwave`.
- **64-bit NVDA (2026.1+)** — the same driver runs inside NVDA's built-in 32-bit synth driver host, and `sv.py` exposes a `SynthDriverProxy32` subclass in NVDA's main process. Audio is played from the host. NVDA's own proxy only implements accessors for six standard settings, so this add-on supplies the rest, routing them to the host by name.

Only the speech thread ever calls into the engine. Parameter changes made from NVDA's main thread (or from an RPC thread when bridged) are queued and applied immediately before the next utterance, because the engine is not thread-safe.

### Files

| File | Role |
|---|---|
| `synthDrivers/sv.py` | The NVDA synth driver, plus the 64-bit bridge proxy. |
| `synthDrivers/_softvoice.py` | Engine and audio layer: DLL loading, the read loop, `nvwave` playback. |
| `synthDrivers/softvoice_wrapper.dll` | Thin C++ wrapper exposing a streaming `sv_*` API over the engine. |
| `synthDrivers/tibase32.dll` | The SoftVoice engine itself. |
| `synthDrivers/tieng32.dll` | English voice data. |
| `synthDrivers/TISPAN32.DLL` | Spanish voice data. |

### Building

The add-on is a zip archive with `manifest.ini` at the root, renamed to `.nvda-addon`:

```bash
cd softvoice && zip -r softvoice.nvda-addon manifest.ini doc locale synthDrivers -x '*__pycache__*' '*.pyc'
```

## Credits

This add-on is derivative work, not original to this repository:

- The **SoftVoice** engine and its voice data are the work of **SoftVoice, Inc.** and are included here as unmodified binaries.
- The original NVDA add-on's author is unknown.
- It was subsequently updated by **Quin Marilyn** and **Tamas Geczy**. The upstream repository was `github.com/TheQuinbox/softvoice`, which is no longer available.

This repository continues that work, adding NVDA 2026.1 compatibility and a number of stability and responsiveness fixes.

## Licence

No licence has been established for this work.

The upstream repository is offline and stated no licence terms that could be carried forward, and the bundled engine binaries are the property of SoftVoice, Inc. and are redistributed here in the same form the add-on has always been distributed in. Nothing here is offered as a grant of rights over that engine.

If you are one of the original authors or a rights holder and would like attribution corrected or the material removed, please open an issue.
