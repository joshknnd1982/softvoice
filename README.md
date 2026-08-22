# SoftVoice for NVDA (svWebspeak)

An NVDA add-on that brings the **SoftVoice** speech synthesizer to the [NVDA screen reader](https://www.nvaccess.org/) as a **fully self-contained** package.

Compatible with NVDA 2019.3 through 2026.1, on both 32-bit and 64-bit NVDA.

> **Nothing is written to or read from the Windows registry.** Earlier
> SoftVoice add-ons required you to import an `sv_license.reg` file into
> `HKEY_LOCAL_MACHINE` before the engine would speak for more than a fraction
> of a second. This build removes that requirement entirely — the engine is
> registered in-process by the add-on itself. Install the `.nvda-addon` and it
> just works.

## What is SoftVoice?

SoftVoice is a **formant synthesizer** — one of the oldest continuously surviving speech engines in personal computing.

Its lineage starts with **SAM (Software Automatic Mouth)**, written by Mark Barton and published in 1982 by Don't Ask Software, which later renamed itself SoftVoice, Inc. SAM was among the first commercial software-only speech synthesizers for home computers, running on machines with 64 KB of memory. The same technology went on to power the Commodore Amiga's built-in `narrator.device`, Apple's original MacinTalk, and speech on Atari machines.

The engine bundled here is the 1997 32-bit Windows build (`SVCTL32`) that shipped with pwWebSpeak.

### Formant synthesis

Unlike modern TTS, SoftVoice stores no recorded human speech at all. It builds every sound from scratch using a mathematical model of the vocal tract — resonances (formants), a glottal source, and noise for fricatives and aspiration.

That design is why this add-on exposes so many unusual controls. You are not selecting from prerecorded voices; you are adjusting the model itself. It is also why the whole engine fits in well under a megabyte, and why it stays intelligible at very high speaking rates — a property many blind users specifically prefer.

The trade-off is the sound: unmistakably synthetic, and to many ears characteristic of 1980s home computing.

## Installing

Download `svWebspeak-*.nvda-addon` from the [Releases](../../releases) page and open it, or use **NVDA menu → Tools → Add-on store → Install from external source**.

After NVDA restarts, select it under **NVDA menu → Preferences → Settings → Speech → Change**.

There is no registry file to import and no separate installer — the add-on is everything you need.

## Voices and variants

Two languages are available: **English** and **Spanish**.

Twenty voice personalities ship with the engine:

| | | | |
|---|---|---|---|
| Male | Female | Large Male | Child |
| Giant Male | Mellow Female | Mellow Male | Crisp Male |
| The Fly | Robotoid | Martian | Colossus |
| Fast Fred | Old Woman | Munchkin | Troll |
| Nerd | Milktoast | Tipsy | Choir Boy |

Selecting a personality other than **Male** leaves that personality's own pitch and timbre in place. Those values are only overridden once you actually move a slider while that voice is selected, so the character voices sound as intended out of the box.

## Settings

Beyond NVDA's standard rate, pitch, volume, inflection and language, the add-on exposes the engine's synthesis parameters directly in the Speech settings and the settings ring:

| Setting | What it does |
|---|---|
| **Sample rate** | 8000, 11025, or 22050 Hz. Higher is better quality. |
| **Glottal source** | Excitation waveform: standard, soft, rounded, open, relaxed, bright, buzzy, harsh — or the voice's own default. |
| **Intonation** | `Normal`, `Monotone`, or `Expressive` — or the voice's own default. |
| **Voicing** | `Normal`, `Soft`, or `Whispered` — or the voice's own default. |
| **Gender** | Vocal-tract model: `Male`, `Female`, or `Neutral` — or the voice's own default. |
| **Breathiness** | Level of breath (aspiration) noise. |
| **Roughness** | Random jitter in the fundamental frequency. Small amounts sound less mechanical. |
| **Vowel length** | Scales vowel duration and prominence. |

## How it works

The SoftVoice engine is a 32-bit DLL. NVDA 2026.1 is the first 64-bit release and cannot load it in-process; 2025.3 and earlier are 32-bit and could. To behave identically on every version — and because the engine creates a top-level window and is not thread-safe — the engine always runs in a dedicated 32-bit helper process, `svwebspeak-host.exe`. The host captures the engine's PCM and streams it to NVDA, which plays it through its own `WavePlayer`, so output-device selection, ducking and cancellation all behave normally.

### Self-contained registration

The 1997 `SVCTL32` engine clips every utterance to about 0.4 seconds unless it has been *registered* with a valid registration number. Historically that number lived in the Windows registry under `HKLM\SOFTWARE\SoftVoice\ProdWorks` (value `SV_KEY`), planted by an `sv_license.reg` file the user had to import.

This build supplies the registration number in-process instead, via two small, documented binary patches (see [`tools/patch_binaries.py`](tools/patch_binaries.py)):

- **`svwebspeak-host.exe`** no longer reads `SV_KEY` from the registry; it hands the number straight to the engine's `SVRegister`.
- **`SVctl32.DLL`**'s `SVRegister` skips its own registry lookups and jumps directly to the routine that validates the number and unlocks full-length audio.

The audible result is identical to importing `sv_license.reg`, but nothing on the system is read or written. `tools/patch_binaries.py` reproduces both patches byte-for-byte from pristine binaries, verifying the original bytes before it touches anything.

### Debug logging

For troubleshooting, set the environment variable `SVWEBSPEAK_DEBUG=1` before starting NVDA. The driver writes a detailed log to `%TEMP%\svwebspeak-debug.log` (host launch, engine init/registration status, and every parameter change). Errors are always logged to NVDA's own log regardless.

### Files

| File | Role |
|---|---|
| `synthDrivers/svWebspeak/__init__.py` | The NVDA synth driver. |
| `synthDrivers/svWebspeak/svwebspeak-host.exe` | 32-bit host process (patched: self-registering). |
| `synthDrivers/svWebspeak/SVctl32.DLL` | The SoftVoice engine (patched: self-registering). |
| `synthDrivers/svWebspeak/SVENG32.DLL` | English voice data. |
| `synthDrivers/svWebspeak/Svspan32.dll` | Spanish voice data. |
| `globalPlugins/svWebspeak.py`, `_svGithubUpdater.py` | Optional update checker in the Tools menu. |
| `tools/patch_binaries.py` | Reproduces the two registration patches from pristine binaries. |

### Building

The add-on is a zip archive with `manifest.ini` at the root, renamed to `.nvda-addon`:

```bash
zip -r svWebspeak.nvda-addon manifest.ini doc globalPlugins synthDrivers tools -x '*__pycache__*' '*.pyc'
```

## Credits

This add-on is derivative work, not original to this repository:

- The **SoftVoice** engine and its voice data are the work of **SoftVoice, Inc.** and are included here as (aside from the registration patch described above) unmodified binaries.
- The svWebspeak driver and host are the work of **seedy60** (`github.com/seedy60/svWebspeak`).
- This repository continues that work, making the add-on fully self-contained so no registry entry is required.

## Licence

No licence has been established for this work.

The bundled engine binaries are the property of SoftVoice, Inc. and are redistributed here in the same form the add-on has always been distributed in, with only the minimal patch needed to register the engine in-process rather than from the registry. Nothing here is offered as a grant of rights over that engine.

If you are one of the original authors or a rights holder and would like attribution corrected or the material removed, please open an issue.
