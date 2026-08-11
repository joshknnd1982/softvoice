# -*- coding: utf-8 -*-
# synthDrivers/sv.py
#
# SoftVoice (late 90s) NVDA synth driver.
#
# FINAL FIXES:
# 1. PAUSE FACTOR INVERTED:
#    - Slider 0 (User) -> DLL 100 (Engine) = Zero Pauses (Robot).
#    - Slider 100 (User) -> DLL 0 (Engine) = Full Natural Pauses.
#
# 2. SECRET WEAPON (sv_setTrimSilence):
#    - Automatically enables 'Trim Silence' when the slider is below 50 (low pauses).
#    - This forces the engine to cut startup latency for fast response.
#
# 3. EXPLICIT PARAMETER LOGIC:
#    - Custom personalities (Martian, etc.) now retain their native pitch/timbre
#      unless the user explicitly moves a slider while on that voice.
#    - Switching back to 'Male' forces all user sliders to re-apply.

import os
import threading
import queue
import re
from collections import OrderedDict

from logHandler import log
from synthDriverHandler import SynthDriver, VoiceInfo, synthDoneSpeaking, synthIndexReached
from speech.commands import IndexCommand, PitchCommand
from autoSettingsUtils.driverSetting import DriverSetting, NumericDriverSetting, BooleanDriverSetting

from . import _softvoice

MAX_STRING_LENGTH = 200

# Speech sequence op codes produced by _buildOps.
_OP_TEXT = 0
_OP_INDEX = 1
_OP_PITCH = 2


def _chunkText(s: str, limit: int = MAX_STRING_LENGTH):
    """Split text into engine-sized pieces, breaking at spaces.

    The engine takes a bounded string per call.  Cutting at a fixed offset
    sliced words in half and made SoftVoice mispronounce whatever straddled
    the boundary, so prefer the last space that fits and only hard-cut a
    token that exceeds the limit on its own.
    """
    if len(s) <= limit:
        return [s]
    out = []
    start = 0
    n = len(s)
    while start < n:
        if n - start <= limit:
            out.append(s[start:])
            break
        cut = s.rfind(" ", start, start + limit + 1)
        if cut <= start:
            out.append(s[start:start + limit])
            start += limit
        else:
            out.append(s[start:cut])
            start = cut + 1
    return out

# --- Background Thread ---
class _BgThread(threading.Thread):
    def __init__(self, q: "queue.Queue", stopEvent: "threading.Event"):
        super().__init__(name=f"{self.__class__.__module__}.{self.__class__.__qualname__}")
        self.daemon = True
        self._q = q
        self._stop = stopEvent

    def run(self):
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                if item is None: return
                func, args, kwargs = item
                func(*args, **kwargs)
            except Exception:
                log.error("SoftVoice: error running background synth function", exc_info=True)
            finally:
                try: self._q.task_done()
                except Exception: pass

# --- Text Cleaning ---
_PUNCT_TRANSLATE = str.maketrans({
    "’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "...", "\u00a0": " ",
})
_control_re = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]+")
_STRIP_CHARS = {"\ufeff", "\u00ad", "\u200b", "\u200c", "\u200d", "\u200e", "\u200f"}
_labelColonRe = re.compile(r"([A-Za-z]{2,})\s*:\s*([A-Za-z])")
_labelSemiRe = re.compile(r"([A-Za-z]{2,})\s*;\s*([A-Za-z])")
_spellWordRe = re.compile(r"[A-Za-z0-9]+")
_acronymWordRe = re.compile(r"\b[A-Z]{2,5}\b")

def _applyAcronymSpacing(s: str) -> str:
    """Insert spaces into short ALL-CAPS words: NVDA -> N V D A.
    This helps avoid SoftVoice expanding acronyms into unintended words.
    """
    def _repl(m: re.Match) -> str:
        w = m.group(0)
        return " ".join(list(w))
    return _acronymWordRe.sub(_repl, s)

# --- Number processing (optional) ---
# SoftVoice sometimes spells long digit runs. We can pre-expand numbers into words before handing text to the engine.
_numberTokenRe = re.compile(r"\b\d{1,3}(?:,\d{3})+\b|\b\d+\b")
_validCommaNumberRe = re.compile(r"^\d{1,3}(?:,\d{3})+$")

_EN_ONES = (
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen",
)
_EN_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_EN_SCALES = (
    (10**18, "quintillion"),
    (10**15, "quadrillion"),
    (10**12, "trillion"),
    (10**9, "billion"),
    (10**6, "million"),
    (10**3, "thousand"),
    (1, ""),
)

def _enUnder1000(n: int) -> str:
    n = int(n)
    if n <= 0:
        return "zero"
    parts = []
    if n >= 100:
        parts.append(_EN_ONES[n // 100])
        parts.append("hundred")
        n = n % 100
    if n >= 20:
        parts.append(_EN_TENS[n // 10])
        n = n % 10
        if n:
            parts.append(_EN_ONES[n])
    elif n > 0:
        parts.append(_EN_ONES[n])
    return " ".join(parts)

def _intToWordsEn(n: int) -> str:
    n = int(n)
    if n == 0:
        return "zero"
    if n < 0:
        return "minus " + _intToWordsEn(-n)
    parts = []
    for scale, name in _EN_SCALES:
        if n < scale:
            continue
        chunk = n // scale
        n = n % scale
        if chunk:
            parts.append(_enUnder1000(chunk))
            if name:
                parts.append(name)
    return " ".join(parts).strip()

def _applyNumberProcessingEnglish(text: str, mode: int) -> str:
    if not text or mode <= 0:
        return text

    def repl(m: "re.Match[str]") -> str:
        tok = m.group(0)
        if "," in tok and not _validCommaNumberRe.match(tok):
            return tok
        digits = tok.replace(",", "")
        # Keep things that look like IDs (leading zeros).
        if len(digits) > 1 and digits.startswith("0"):
            return tok
        try:
            n = int(digits)
        except Exception:
            return tok
        if mode == 1 and n < 10000:
            return tok
        # Avoid massive output for extremely large numbers.
        if n >= 10**21:
            return tok
        return _intToWordsEn(n)

    return _numberTokenRe.sub(repl, text)

def _sanitizeText(s: str) -> str:
    if not s: return ""
    s = s.translate(_PUNCT_TRANSLATE)
    for ch in _STRIP_CHARS: s = s.replace(ch, "")
    s = _control_re.sub(" ", s)
    s = "".join((c if ord(c) <= 0xFFFF else " ") for c in s)
    s = " ".join(s.split())
    return s.strip()


# --- Enum Definitions ---
variants = OrderedDict()
def _v(_id, label): variants[str(_id)] = VoiceInfo(str(_id), label)
_v(0, "Male"); _v(1, "Female"); _v(2, "Large Male"); _v(3, "Child"); _v(4, "Giant Male")
_v(5, "Mellow Female"); _v(6, "Mellow Male"); _v(7, "Crisp Male"); _v(8, "The Fly")
_v(9, "Robotoid"); _v(10, "Martian"); _v(11, "Colossus"); _v(12, "Fast Fred")
_v(13, "Old Woman"); _v(14, "Munchkin"); _v(15, "Troll"); _v(16, "Nerd")
_v(17, "Milktoast"); _v(18, "Tipsy"); _v(19, "Choirboy")

intstyles = OrderedDict()
def _i(_id, label): intstyles[str(_id)] = VoiceInfo(str(_id), label)
_i(0, "normal1"); _i(1, "normal2"); _i(2, "monotone"); _i(3, "sung"); _i(4, "random")

vmodes = OrderedDict()
def _m(_id, label): vmodes[str(_id)] = VoiceInfo(str(_id), label)
_m(0, "normal"); _m(1, "breathy"); _m(2, "whispered")

genders = OrderedDict()
def _g(_id, label): genders[str(_id)] = VoiceInfo(str(_id), label)
_g(0, "male"); _g(1, "female"); _g(2, "child"); _g(3, "giant")

glots = OrderedDict()
def _t(_id, label): glots[str(_id)] = VoiceInfo(str(_id), label)
_t(0, "default"); _t(1, "male"); _t(2, "female"); _t(3, "child")
_t(4, "high"); _t(5, "mellow"); _t(6, "impulse"); _t(7, "odd"); _t(8, "colossus")

smodes = OrderedDict()
def _k(_id, label): smodes[str(_id)] = VoiceInfo(str(_id), label)
_k(0, "Natural"); _k(1, "Word-at-a-time"); _k(2, "Spelled")

numprocs = OrderedDict()
def _np(_id, label): numprocs[str(_id)] = VoiceInfo(str(_id), label)
_np(0, "Off")
_np(1, "Large numbers (>= 10000)")
_np(2, "All numbers")


class SynthDriver(SynthDriver):
    name = "sv"
    description = "SoftVoice (nvwave)"
    supportedSettings = (
        SynthDriver.RateSetting(), SynthDriver.VariantSetting(),
        SynthDriver.VoiceSetting(), SynthDriver.PitchSetting(),
        SynthDriver.InflectionSetting(),
        NumericDriverSetting("perturb", "Perturbation"),
        NumericDriverSetting("vfactor", "Vowel Factor"),
        NumericDriverSetting("avbias", "Voicing Gain"),
        NumericDriverSetting("afbias", "Frication Gain"),
        NumericDriverSetting("ahbias", "Aspiration Gain"),
        DriverSetting("intstyle", "Intonation Style"),
        DriverSetting("vmode", "Voicing Mode"),
        DriverSetting("gender", "Gender"),
        DriverSetting("glot", "Glottal Source"),
        DriverSetting("smode", "Speaking Mode"),
        BooleanDriverSetting("useAbbreviations", "Use abbreviations"),
        DriverSetting("numproc", "Number processing"),
        NumericDriverSetting("pauseFactor", "Pause factor"),
    )
    # PitchCommand is how NVDA asks for capital pitch change.  Declaring it
    # lets NVDA drive the feature itself, which works identically whether the
    # driver runs in-process or inside NVDA's 32-bit host.
    supportedCommands = {IndexCommand, PitchCommand}
    supportedNotifications = {synthIndexReached, synthDoneSpeaking}
    _availableVoices = OrderedDict(
        (str(index + 1), VoiceInfo(str(index + 1), name, language))
        for index, (name, language) in enumerate([("English", "en"), ("Spanish", "es")])
    )

    def _getAvailableVoices(self):
        return self._availableVoices

    @classmethod
    def check(cls):
        return _softvoice.check()

    def __init__(self):
        # Ensure config.pre_configSave exists (bridge host compat)
        import config
        if not hasattr(config, 'pre_configSave'):
            import extensionPoints
            config.pre_configSave = extensionPoints.Action()
        super().__init__()

        _softvoice.initialize()

        # Parameter writes are collected here and applied by the speech thread
        # just before the next utterance.  See _queueParam.
        self._pendingParams = {}
        self._paramsLock = threading.Lock()
        self._appliedPitch = None

        # _DllProxy routes sv_set*(handle, value) calls onto that queue so all
        # existing setter code works unchanged.
        driver = self

        class _DllProxy:
            def __getattr__(self, name):
                def _call(handle, value):
                    driver._queueParam(name, value)
                return _call
        self._dll = _DllProxy()
        self._handle = True  # truthy dummy; actual handle is in _softvoice

        self._hasPauseFactor = _softvoice.has_pause_factor()
        self._hasTrimSilence = _softvoice.has_trim_silence()

        self.speaking = False
        self._speakGeneration = 0
        self._terminating = False

        self._bgQueue = queue.Queue()
        self._bgStop = threading.Event()
        self._bgThread = _BgThread(self._bgQueue, self._bgStop)
        self._bgThread.start()

        self._ratePercent = 50
        self._pitchPercent = 50
        self._inflectionPercent = 50
        self._perturbPercent = 0
        self._vfactorPercent = 20
        self._avbiasPercent = 50
        self._afbiasPercent = 50
        self._ahbiasPercent = 50
        self._pauseFactorPercent = 50
        self._useAbbreviations = True

        self._variant = "0"
        self._intstyle = "0"
        self._vmode = "0"
        self._gender = "0"
        self._glot = "0"
        self._smode = "0"
        self._numproc = "0"
        self.curvoice = "1"

        self._paramExplicit = {
            "intstyle": False, "vmode": False, "gender": False, "glot": False,
            "smode": False, "inflection": False, "perturb": False,
            "vfactor": False, "avbias": False, "afbias": False, "ahbias": False,
            "pitch": False
        }

        self._initializing = True
        self.rate = self._ratePercent
        self.pitch = 4
        self.inflection = 25
        self.perturb = self._perturbPercent
        self.vfactor = self._vfactorPercent
        self.avbias = self._avbiasPercent
        self.afbias = self._afbiasPercent
        self.ahbias = self._ahbiasPercent
        self.voice = self.curvoice
        self.pauseFactor = self._pauseFactorPercent
        self._initializing = False


    def _queueParam(self, funcName, value):
        """Defer a DLL parameter write to the speech thread.

        Settings can be changed from NVDA's main thread -- or, on 64-bit, from
        an rpyc thread inside the host process -- while the speech thread is
        sitting inside sv_read().  The SoftVoice engine is not thread-safe, so
        writes are collected here and applied by the speech thread just before
        the next utterance, which is also exactly when the user expects to
        hear them.
        """
        try:
            value = int(value)
        except (TypeError, ValueError):
            log.error(f"SoftVoice: ignoring non-numeric value {value!r} for {funcName}")
            return
        with self._paramsLock:
            # Re-queueing moves the call to the end, so the last write wins and
            # ordering is preserved (a personality change resets the sliders it
            # precedes, so it must not jump ahead of them).
            self._pendingParams.pop(funcName, None)
            self._pendingParams[funcName] = value

    def _flushPendingParams(self):
        """Apply deferred parameter writes.  Speech thread only."""
        with self._paramsLock:
            if not self._pendingParams:
                return
            pending = list(self._pendingParams.items())
            self._pendingParams.clear()
        for funcName, value in pending:
            try:
                _softvoice.dll_call(funcName, value)
            except Exception:
                log.error(f"SoftVoice: failed to apply {funcName}", exc_info=True)
        # A personality or timbre change can move the engine's pitch, so the
        # cached "already applied" pitch is no longer trustworthy.
        self._appliedPitch = None

    def _applyPitchPercent(self, pct):
        """Push an absolute pitch to the engine.  Speech thread only."""
        pct = self._clampPercent(pct)
        if self._appliedPitch == pct or not self._handle:
            return
        try:
            _softvoice.dll_call("sv_setPitch", self._percentToParam(pct, 10, 2000))
            self._appliedPitch = pct
        except Exception:
            log.error("SoftVoice: failed to set pitch", exc_info=True)

    def _pitchCommandTarget(self, cmd) -> int:
        """Resolve a PitchCommand to an absolute pitch percentage.

        NVDA keeps the offset and multiplier in ``_offset``/``_multiplier``;
        those are also the exact fields NVDA's 32-bit bridge puts on the wire,
        so reading them keeps direct and bridged mode identical.  Resolving
        against our own pitch rather than the command's ``newValue`` property
        avoids depending on whichever synth ``getSynth()`` happens to return.
        """
        base = int(getattr(self, "_pitchPercent", 50))
        multiplier = getattr(cmd, "_multiplier", 1)
        if multiplier != 1:
            return self._clampPercent(round(base * multiplier))
        return self._clampPercent(base + int(getattr(cmd, "_offset", 0)))

    def _enqueue(self, func, *args, **kwargs):
        if not self._terminating: self._bgQueue.put((func, args, kwargs))

    def terminate(self):
        self._terminating = True
        self.cancel()
        try:
            self._bgStop.set()
            self._bgQueue.put(None)
            self._bgThread.join(timeout=2.0)
        except Exception:
            log.error("SoftVoice: error stopping the speech thread", exc_info=True)
        with self._paramsLock:
            self._pendingParams.clear()
        _softvoice.terminate()
        self._handle = None
        self._dll = None

    def cancel(self):
        self._speakGeneration += 1
        self.speaking = False
        _softvoice.stop()
        try:
            while True:
                self._bgQueue.get_nowait()
                self._bgQueue.task_done()
        except queue.Empty: pass

    def pause(self, switch):
        _softvoice.pause(switch)

    # --- Speaking ---
    def _buildOps(self, speechSequence):
        """Flatten a speech sequence into ordered (op, value) pairs.

        Text is coalesced between commands so the engine still gets long runs
        to chew on, while indexes and pitch changes keep their position in the
        stream instead of all landing at the end.
        """
        ops = []
        textBuf = []

        def flushText():
            if not textBuf:
                return
            safe = self._softVoiceSafeText(" ".join(textBuf))
            textBuf.clear()
            if safe:
                ops.append((_OP_TEXT, safe))

        for item in speechSequence:
            if isinstance(item, str):
                textBuf.append(item)
            elif isinstance(item, IndexCommand):
                flushText()
                ops.append((_OP_INDEX, item.index))
            elif isinstance(item, PitchCommand):
                flushText()
                ops.append((_OP_PITCH, self._pitchCommandTarget(item)))
        flushText()
        # A pitch change with nothing after it affects nothing.
        while ops and ops[-1][0] == _OP_PITCH:
            ops.pop()
        anyText = any(op == _OP_TEXT for (op, _) in ops)
        allIndexes = [value for (op, value) in ops if op == _OP_INDEX]
        return ops, anyText, allIndexes

    def _notifyIndexesAndDone(self, indexes):
        for i in indexes: synthIndexReached.notify(synth=self, index=i)
        synthDoneSpeaking.notify(synth=self)
        self.speaking = False

    def speak(self, speechSequence):
        if len(speechSequence) == 1 and isinstance(speechSequence[0], IndexCommand):
            self._enqueue(self._notifyIndexesAndDone, [speechSequence[0].index])
            return
        ops, anyText, allIndexes = self._buildOps(speechSequence)
        if not anyText:
            self._enqueue(self._notifyIndexesAndDone, allIndexes)
            return
        # The stop generation is captured here, on NVDA's thread, rather than
        # inside _speakBg: an utterance cancelled between now and the speech
        # thread picking it up is then dropped instead of spoken.
        self._enqueue(self._speakBg, ops, _softvoice.generation())

    def _speakBg(self, ops, stopGen):
        if stopGen != _softvoice.generation():
            # Cancelled after this job was queued but before it ran.
            return
        self._speakGeneration += 1
        gen = self._speakGeneration
        self.speaking = True

        # This is the only place the engine is written to, so apply whatever
        # settings changed since the last utterance first.
        self._flushPendingParams()

        isWordMode = (str(self._smode) == "1")
        basePitch = int(getattr(self, "_pitchPercent", 50))
        # Custom personalities keep their own pitch unless the user has
        # actually moved the slider while on that voice.
        applyUserPitch = (self._variant == "0" or self._paramExplicit.get("pitch", False))
        if applyUserPitch:
            self._applyPitchPercent(basePitch)

        for (op, value) in ops:
            if not self.speaking:
                break
            if op == _OP_PITCH:
                if applyUserPitch:
                    self._applyPitchPercent(value)
                continue
            if op == _OP_INDEX:
                def cb(index=value, g=gen):
                    if self._speakGeneration == g:
                        synthIndexReached.notify(synth=self, index=index)
                _softvoice.feed_marker(on_done=cb)
                continue
            for seg in (value.split(" ") if isWordMode else _chunkText(value)):
                if not self.speaking:
                    break
                seg = seg.strip()
                if not seg:
                    continue
                if not _softvoice.speak(seg, stopGen):
                    self.speaking = False
                    break

        if not self.speaking:
            synthDoneSpeaking.notify(synth=self)
            return

        def doneCb(g=gen):
            if self._speakGeneration == g:
                self.speaking = False
                synthDoneSpeaking.notify(synth=self)
        _softvoice.feed_marker(on_done=doneCb)
        _softvoice.player_idle()

    def _softVoiceSafeText(self, s: str) -> str:
        s = _sanitizeText(s)
        if not s: return ""
        if self._pauseFactorPercent < 50:
            s = _labelColonRe.sub(r"\1 \2", s); s = _labelSemiRe.sub(r"\1 \2", s)
        # Optional acronym handling: if disabled, spell short ALL-CAPS words (2-5 letters).
        if (not bool(getattr(self, "_useAbbreviations", True))) and str(getattr(self, "_smode", "0")) != "2":
            s = _applyAcronymSpacing(s)

        # Optional number expansion (helps when SoftVoice spells long digit runs).
        try: numMode = int(getattr(self, "_numproc", "0") or 0)
        except Exception: numMode = 0
        if numMode and str(getattr(self, "curvoice", "1")) == "1" and str(getattr(self, "_smode", "0")) != "2":
            s = _applyNumberProcessingEnglish(s, numMode)

        if str(getattr(self, "_smode", "0")) == "2":
            def _spellMatch(m): return " ".join(list(m.group(0)))
            s = _spellWordRe.sub(_spellMatch, s)
        return " ".join(s.split()).strip()

    # --- Settings ---
    def _percentToParam(self, val, minVal, maxVal):
        ratio = float(val) / 100.0
        return int(round(minVal + (maxVal - minVal) * ratio))
    def _clampPercent(self, v): return max(0, min(100, int(v)))

    # Timbre Settings (protected)
    def _timbre_setter(self, name, func, minV, maxV, val):
        self._clampPercent(val)
        current = getattr(self, f"_{name}Percent")
        new_val = int(val)
        if getattr(self, "_initializing", False):
            setattr(self, f"_{name}Percent", new_val)
            return
        if self._variant != "0" and not self._paramExplicit[name]:
             setattr(self, f"_{name}Percent", new_val)
             if new_val != current:
                 self._paramExplicit[name] = True
                 if self._handle: getattr(self._dll, func)(self._handle, self._percentToParam(new_val, minV, maxV))
             return
        setattr(self, f"_{name}Percent", new_val)
        self._paramExplicit[name] = True
        if self._handle: getattr(self._dll, func)(self._handle, self._percentToParam(new_val, minV, maxV))

    def _get_rate(self): return int(self._ratePercent)
    def _set_rate(self, v):
        self._ratePercent = self._clampPercent(v)
        if self._handle: self._dll.sv_setRate(self._handle, self._percentToParam(self._ratePercent, 20, 500))

    def _get_pitch(self): return int(self._pitchPercent)
    def _set_pitch(self, v):
        # Pitch is not queued like the other parameters: _speakBg pushes it at
        # the start of every utterance anyway (and moves it around for
        # PitchCommand), so recording the value here is enough.
        new_val = self._clampPercent(v)
        current = int(getattr(self, "_pitchPercent", 50))
        self._pitchPercent = new_val

        if getattr(self, "_initializing", False):
            return

        # For custom voices, don't override the personality's own pitch unless
        # the user actually changes the slider while on that voice.
        if self._variant != "0" and not self._paramExplicit.get("pitch", False):
            if new_val != current:
                self._paramExplicit["pitch"] = True
            return

        self._paramExplicit["pitch"] = True

    def _get_inflection(self): return int(self._inflectionPercent)
    def _set_inflection(self, v): self._timbre_setter("inflection", "sv_setF0Range", 0, 500, v)
    def _get_perturb(self): return int(self._perturbPercent)
    def _set_perturb(self, v): self._timbre_setter("perturb", "sv_setF0Perturb", 0, 500, v)
    def _get_vfactor(self): return int(self._vfactorPercent)
    def _set_vfactor(self, v): self._timbre_setter("vfactor", "sv_setVowelFactor", 0, 500, v)
    def _get_avbias(self): return int(self._avbiasPercent)
    def _set_avbias(self, v): self._timbre_setter("avbias", "sv_setAVBias", -50, 50, v)
    def _get_afbias(self): return int(self._afbiasPercent)
    def _set_afbias(self, v): self._timbre_setter("afbias", "sv_setAFBias", -50, 50, v)
    def _get_ahbias(self): return int(self._ahbiasPercent)
    def _set_ahbias(self, v): self._timbre_setter("ahbias", "sv_setAHBias", -50, 50, v)

    def _get_pauseFactor(self): return int(self._pauseFactorPercent)
    def _set_pauseFactor(self, v):
        self._pauseFactorPercent = self._clampPercent(v)
        if self._handle:
            if self._hasPauseFactor:
                inverted = 100 - int(self._pauseFactorPercent)
                try: self._dll.sv_setPauseFactor(self._handle, inverted)
                except: pass
            if self._hasTrimSilence:
                try: self._dll.sv_setTrimSilence(self._handle, 1 if self._pauseFactorPercent < 50 else 0)
                except: pass

    def _getAvailableVariants(self): return variants
    def _get_variant(self): return getattr(self, "_variant", "0")
    def _set_variant(self, _id):
        new_v = str(_id)
        prev_v = getattr(self, "_variant", "0")
        self._variant = new_v

        # Switching personality should clear "explicit override" flags so that custom
        # voices come up with their own defaults unless the user tweaks a knob.
        if prev_v != new_v:
            for k in self._paramExplicit:
                self._paramExplicit[k] = False

        if self._handle:
            try:
                self._dll.sv_setPersonality(self._handle, int(_id))

                # Baseline (Male): re-assert the user's numeric sliders after a
                # personality change.  Pitch is handled by _speakBg.
                if new_v == "0":
                    self._dll.sv_setRate(self._handle, self._percentToParam(self._ratePercent, 20, 500))
                    self._dll.sv_setF0Range(self._handle, self._percentToParam(self._inflectionPercent, 0, 500))
                    self._dll.sv_setF0Perturb(self._handle, self._percentToParam(self._perturbPercent, 0, 500))
                    self._dll.sv_setVowelFactor(self._handle, self._percentToParam(self._vfactorPercent, 0, 500))
                    self._dll.sv_setAVBias(self._handle, self._percentToParam(self._avbiasPercent, -50, 50))
                    self._dll.sv_setAFBias(self._handle, self._percentToParam(self._afbiasPercent, -50, 50))
                    self._dll.sv_setAHBias(self._handle, self._percentToParam(self._ahbiasPercent, -50, 50))
            except Exception:
                pass

    def _get_voice(self): return getattr(self, "curvoice", "1")
    def _set_voice(self, v):
        self.curvoice = str(v)
        if self._handle: self._dll.sv_setVoice(self._handle, int(v))

    def _set_enum_generic(self, attr_name, func_name, val_id):
        key = attr_name.strip("_")
        val = int(val_id)
        current = int(getattr(self, attr_name, 0))
        setattr(self, attr_name, str(val_id))
        if not self._paramExplicit.get(key, False):
            if self._variant == "0" and val == current:
                return
            if self._variant != "0":
                self._paramExplicit[key] = True
                if self._handle: getattr(self._dll, func_name)(self._handle, val)
                return
        self._paramExplicit[key] = True
        if self._handle: getattr(self._dll, func_name)(self._handle, val)

    def _get_availableNumprocs(self): return numprocs
    def _get_useAbbreviations(self):
        # When enabled, we let SoftVoice handle abbreviations/acronyms normally.
        # When disabled, we insert spaces into short ALL-CAPS words (e.g. NVDA -> N V D A)
        # to prevent unwanted expansions like "Nevada access".
        return bool(getattr(self, "_useAbbreviations", True))

    def _set_useAbbreviations(self, v):
        if isinstance(v, str):
            vv = v.strip().lower()
            self._useAbbreviations = vv in ("1", "true", "yes", "on")
            return
        try:
            self._useAbbreviations = bool(int(v))
        except Exception:
            self._useAbbreviations = bool(v)

    def _get_numproc(self): return getattr(self, "_numproc", "0")
    def _set_numproc(self, v): self._numproc = str(v)

    def _get_availableIntstyles(self): return intstyles
    def _get_intstyle(self): return getattr(self, "_intstyle", "0")
    def _set_intstyle(self, v): self._set_enum_generic("_intstyle", "sv_setF0Style", v)
    def _get_availableVmodes(self): return vmodes
    def _get_vmode(self): return getattr(self, "_vmode", "0")
    def _set_vmode(self, v): self._set_enum_generic("_vmode", "sv_setVoicingMode", v)
    def _get_availableGenders(self): return genders
    def _get_gender(self): return getattr(self, "_gender", "0")
    def _set_gender(self, v): self._set_enum_generic("_gender", "sv_setGender", v)
    def _get_availableGlots(self): return glots
    def _get_glot(self): return getattr(self, "_glot", "0")
    def _set_glot(self, v): self._set_enum_generic("_glot", "sv_setGlottalSource", v)
    def _get_availableSmodes(self): return smodes
    def _get_smode(self): return getattr(self, "_smode", "0")
    def _set_smode(self, v): self._set_enum_generic("_smode", "sv_setSpeakingMode", v)


# ---------------------------------------------------------------------------
# 64-bit NVDA 2026.1+: use the built-in bridge to run the full driver in a
# 32-bit host process.  Audio plays from the host directly via nvwave.
# On 32-bit (including the bridge host), this block is skipped and the
# SynthDriver class defined above is used as-is.
# ---------------------------------------------------------------------------
import ctypes as _ctypes
if _ctypes.sizeof(_ctypes.c_void_p) == 8:
    from _bridge.clients.synthDriverHost32.synthDriver import SynthDriverProxy32 as _Proxy32

    # Fallbacks used only if the host stops answering, so a settings dialog
    # never dies on a dropped connection.  These mirror the driver's own
    # startup values.
    _BRIDGE_DEFAULTS = {
        "inflection": 25, "perturb": 0, "vfactor": 20,
        "avbias": 50, "afbias": 50, "ahbias": 50, "pauseFactor": 50,
        "intstyle": "0", "vmode": "0", "gender": "0", "glot": "0", "smode": "0",
        "numproc": "0", "useAbbreviations": True,
    }

    class SynthDriver(_Proxy32):
        name = "sv"
        description = "SoftVoice (nvwave)"
        synthDriver32Path = os.path.abspath(os.path.dirname(__file__))
        synthDriver32Name = "sv"

        def __init__(self):
            # Populated before super(), which loads settings and will call the
            # accessors below.
            self._paramCache = {}
            super().__init__()

        # NVDA's SynthDriverProxy only implements _get_/_set_ for its six
        # standard settings, so everything SoftVoice adds used to be dropped
        # from the voice dialog.  The remote service will get or set any
        # supported setting by name, so the accessors are simply written out.
        def _getRemoteParam(self, settingId):
            """Read a setting from the 32-bit host, caching the result.

            Nothing but this proxy changes these values, so the cache stays
            authoritative and opening the voice dialog no longer costs a
            round trip per control.
            """
            try:
                return self._paramCache[settingId]
            except KeyError:
                pass
            try:
                value = self._remoteService.getParam(settingId)
            except Exception:
                log.error(f"SoftVoice: could not read {settingId} from the 32-bit host", exc_info=True)
                return _BRIDGE_DEFAULTS.get(settingId)
            self._paramCache[settingId] = value
            return value

        def _setRemoteParam(self, settingId, value):
            try:
                self._remoteService.setParam(settingId, value)
            except Exception:
                log.error(f"SoftVoice: could not set {settingId} on the 32-bit host", exc_info=True)
                return
            self._paramCache[settingId] = value

        def _get_inflection(self): return self._getRemoteParam("inflection")
        def _set_inflection(self, v): self._setRemoteParam("inflection", v)
        def _get_perturb(self): return self._getRemoteParam("perturb")
        def _set_perturb(self, v): self._setRemoteParam("perturb", v)
        def _get_vfactor(self): return self._getRemoteParam("vfactor")
        def _set_vfactor(self, v): self._setRemoteParam("vfactor", v)
        def _get_avbias(self): return self._getRemoteParam("avbias")
        def _set_avbias(self, v): self._setRemoteParam("avbias", v)
        def _get_afbias(self): return self._getRemoteParam("afbias")
        def _set_afbias(self, v): self._setRemoteParam("afbias", v)
        def _get_ahbias(self): return self._getRemoteParam("ahbias")
        def _set_ahbias(self, v): self._setRemoteParam("ahbias", v)
        def _get_pauseFactor(self): return self._getRemoteParam("pauseFactor")
        def _set_pauseFactor(self, v): self._setRemoteParam("pauseFactor", v)
        def _get_useAbbreviations(self): return self._getRemoteParam("useAbbreviations")
        def _set_useAbbreviations(self, v): self._setRemoteParam("useAbbreviations", v)
        def _get_intstyle(self): return self._getRemoteParam("intstyle")
        def _set_intstyle(self, v): self._setRemoteParam("intstyle", v)
        def _get_vmode(self): return self._getRemoteParam("vmode")
        def _set_vmode(self, v): self._setRemoteParam("vmode", v)
        def _get_gender(self): return self._getRemoteParam("gender")
        def _set_gender(self, v): self._setRemoteParam("gender", v)
        def _get_glot(self): return self._getRemoteParam("glot")
        def _set_glot(self, v): self._setRemoteParam("glot", v)
        def _get_smode(self): return self._getRemoteParam("smode")
        def _set_smode(self, v): self._setRemoteParam("smode", v)
        def _get_numproc(self): return self._getRemoteParam("numproc")
        def _set_numproc(self, v): self._setRemoteParam("numproc", v)

        # Combo box choices are static, so the dialog reads them locally.
        def _get_availableIntstyles(self): return intstyles
        def _get_availableVmodes(self): return vmodes
        def _get_availableGenders(self): return genders
        def _get_availableGlots(self): return glots
        def _get_availableSmodes(self): return smodes
        def _get_availableNumprocs(self): return numprocs

        def _get_supportedSettings(self):
            """Hide any setting this proxy can't actually reach.

            Everything the driver declares is covered above; the check is a
            backstop so adding a setting to the driver and forgetting the
            proxy degrades to a missing control instead of a broken dialog.
            """
            cached = getattr(self, "_bridgeSettingsCache", None)
            if cached is not None:
                return cached
            usable = []
            for setting in super()._get_supportedSettings():
                if hasattr(self.__class__, setting.id):
                    usable.append(setting)
                else:
                    log.debugWarning(f"SoftVoice: no bridge accessor for {setting.id!r}; hiding it")
            self._bridgeSettingsCache = usable
            return usable

        @classmethod
        def check(cls):
            if not super().check():
                return False
            base = os.path.abspath(os.path.dirname(__file__))
            return (
                os.path.isfile(os.path.join(base, "softvoice_wrapper.dll"))
                and _softvoice._find_tibase32(base) != ""
            )