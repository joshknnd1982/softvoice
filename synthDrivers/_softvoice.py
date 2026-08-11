"""Client-side helper for SoftVoice speech synthesis.

Loads softvoice_wrapper.dll directly via ctypes.  On 64-bit NVDA 2026.1+
the entire synth driver (including this module) runs inside NVDA's built-in
32-bit bridge host, so this code always operates in a 32-bit process.
"""
from __future__ import annotations

import ctypes
import logging
import os
import queue
import threading
import time
from typing import Any, Callable, Dict, Optional, Tuple

LOGGER = logging.getLogger(__name__)

# Stream item types from softvoice_wrapper.h
SV_ITEM_NONE = 0
SV_ITEM_AUDIO = 1
SV_ITEM_DONE = 2
SV_ITEM_ERROR = 3

# DLL setter functions allowed for dll_call
_ALLOWED_DLL_CALLS = frozenset({
	"sv_setRate", "sv_setPitch", "sv_setF0Range", "sv_setF0Perturb",
	"sv_setVowelFactor", "sv_setAVBias", "sv_setAFBias", "sv_setAHBias",
	"sv_setPersonality", "sv_setF0Style", "sv_setVoicingMode", "sv_setGender",
	"sv_setGlottalSource", "sv_setSpeakingMode", "sv_setVoice",
	"sv_setPauseFactor", "sv_setTrimSilence", "sv_setMaxLeadMs",
})

AudioChunk = Tuple[bytes, Optional[int], bool, int]  # (data, index, is_final, seq)


# 8-bit unsigned -> 16-bit signed is a pure byte remap.  The sample
# (b - 128) << 8 has a zero low byte, and its high byte is (b - 128) & 0xFF,
# which is exactly b ^ 0x80.  So the whole conversion is "XOR 0x80, then
# interleave zero bytes": two C-level operations instead of a per-sample
# Python loop that ran on the audio thread holding the GIL.
_XOR_SIGN_TABLE = bytes(b ^ 0x80 for b in range(256))


def _convert_audio(data: bytes) -> bytes:
	"""Convert 8-bit unsigned PCM to 16-bit signed little-endian PCM.

	The SoftVoice engine outputs 8-bit unsigned PCM.
	NVDA's WASAPI backend can't handle 8-bit, so we convert to 16-bit.
	WASAPI handles the 11025 Hz resampling natively.
	"""
	out = bytearray(len(data) * 2)
	out[1::2] = data.translate(_XOR_SIGN_TABLE)
	return bytes(out)


# ---------------------------------------------------------------------------
# Audio handling (shared by both 32-bit and 64-bit modes)
# ---------------------------------------------------------------------------

class AudioWorker(threading.Thread):
	"""Pulls audio events from the queue and feeds them to nvwave.WavePlayer."""

	def __init__(self, player, audio_queue: "queue.Queue[Optional[AudioChunk]]",
				 get_sequence: Callable[[], int], convert_8to16: bool = False,
				 player_lock: Optional[threading.RLock] = None,
				 auto_idle: bool = True):
		super().__init__(daemon=True, name="SoftVoiceAudioWorker")
		self._player = player
		self._queue = audio_queue
		self._get_sequence = get_sequence
		self._convert_8to16 = convert_8to16
		self._running = True
		self._stopping = False
		self._player_lock = player_lock or threading.RLock()
		self._auto_idle = auto_idle

	def run(self) -> None:
		while self._running:
			try:
				chunk = self._queue.get(timeout=0.1)
			except queue.Empty:
				continue
			if chunk is None:
				break

			data, second, is_final, seq = chunk

			if seq < self._get_sequence():
				self._queue.task_done()
				continue

			# Marker callback: feed empty buffer with onDone to WavePlayer
			if callable(second):
				if not self._stopping:
					try:
						with self._player_lock:
							if not self._stopping and self._player:
								self._player.feed(b"", onDone=second)
					except Exception:
						LOGGER.exception("Marker feed failed")
				self._queue.task_done()
				continue

			# Idle signal
			if second == "IDLE":
				if not self._stopping:
					try:
						with self._player_lock:
							if not self._stopping and self._player:
								self._player.idle()
					except Exception:
						LOGGER.exception("Player idle failed")
				self._queue.task_done()
				continue

			# Done marker (from _read_loop)
			if not data and second is None:
				if is_final and self._auto_idle:
					with self._player_lock:
						if not self._stopping:
							self._player.idle()
					if not self._stopping:
						self._invoke_done_callback()
				self._queue.task_done()
				continue

			if self._stopping:
				self._queue.task_done()
				continue

			try:
				if self._convert_8to16 and data:
					data = _convert_audio(data)
				with self._player_lock:
					if not self._stopping and self._player:
						self._player.feed(data)
			except FileNotFoundError:
				LOGGER.warning("Sound device not found during feed")
			except Exception:
				LOGGER.exception("WavePlayer feed failed")
			self._queue.task_done()

	def stop(self) -> None:
		self._stopping = True
		self._running = False
		self._queue.put(None)

	def _invoke_done_callback(self) -> None:
		if _on_done:
			try:
				_on_done()
			except Exception:
				LOGGER.exception("Done callback failed")


# ---------------------------------------------------------------------------
# 32-bit direct client (loads DLL in-process)
# ---------------------------------------------------------------------------

class SoftVoiceDirectClient:
	"""Direct ctypes access to softvoice_wrapper.dll for 32-bit NVDA."""

	def __init__(self) -> None:
		self._dll = None
		self._handle = None
		self._audio_queue: "queue.Queue[Optional[AudioChunk]]" = queue.Queue()
		self._player = None
		self._player_lock = threading.RLock()
		self._audio_worker: Optional[AudioWorker] = None
		self._should_stop = False
		# _sequence is the "stop generation".  stop() bumps it; an utterance
		# only starts if the generation it was queued under is still current.
		# Guarded by _state_lock so a cancel can never land in the gap between
		# the check and sv_startSpeakW.
		self._state_lock = threading.Lock()
		self._sequence = 0
		self._current_seq = 0
		# Audio format
		self._sample_rate = 0
		self._channels = 0
		self._bits_per_sample = 0
		# Feature flags (cached after first init)
		self._has_pause_factor = False
		self._has_trim_silence = False
		# Read buffer
		self._buf_size = 65536
		self._audio_buf = None
		self._audio_view = None
		self._out_type = None
		self._out_value = None

	def do_initialize(self, dll_path: str, tibase_path: str, initial_voice: int) -> Dict[str, Any]:
		"""Load the wrapper DLL and initialize the engine.

		The SoftVoice engine (tibase32.dll) cannot survive a
		FreeLibrary/LoadLibrary cycle, so we keep the DLL and engine
		handle alive for the entire NVDA process and only re-create
		the Python-side audio plumbing on each init.
		"""
		if self._handle:
			# Engine still alive from a previous init — reuse it.
			return {
				"format": {
					"sampleRate": self._sample_rate,
					"channels": self._channels,
					"bitsPerSample": self._bits_per_sample,
				},
				"hasPauseFactor": self._has_pause_factor,
				"hasTrimSilence": self._has_trim_silence,
			}

		if self._dll is None:
			self._dll = ctypes.cdll.LoadLibrary(dll_path)
			self._setup_ctypes()

		self._audio_buf = ctypes.create_string_buffer(self._buf_size)
		self._audio_view = memoryview(self._audio_buf)
		self._out_type = ctypes.c_int(0)
		self._out_value = ctypes.c_int(0)

		self._handle = self._dll.sv_initW(tibase_path, initial_voice)
		if not self._handle:
			raise RuntimeError("sv_initW returned NULL")

		self._has_pause_factor = hasattr(self._dll, "sv_setPauseFactor")
		self._has_trim_silence = hasattr(self._dll, "sv_setTrimSilence")

		# Query audio format
		sr = ctypes.c_int(0)
		ch = ctypes.c_int(0)
		bps = ctypes.c_int(0)
		if self._dll.sv_getFormat(self._handle, ctypes.byref(sr), ctypes.byref(ch), ctypes.byref(bps)):
			self._sample_rate = sr.value
			self._channels = ch.value
			self._bits_per_sample = bps.value
		else:
			self._sample_rate = 22050
			self._channels = 1
			self._bits_per_sample = 16

		return {
			"format": {
				"sampleRate": self._sample_rate,
				"channels": self._channels,
				"bitsPerSample": self._bits_per_sample,
			},
			"hasPauseFactor": self._has_pause_factor,
			"hasTrimSilence": self._has_trim_silence,
		}

	def _setup_ctypes(self):
		dll = self._dll
		dll.sv_initW.argtypes = (ctypes.c_wchar_p, ctypes.c_int)
		dll.sv_initW.restype = ctypes.c_void_p
		dll.sv_free.argtypes = (ctypes.c_void_p,)
		dll.sv_free.restype = None
		dll.sv_stop.argtypes = (ctypes.c_void_p,)
		dll.sv_stop.restype = None
		dll.sv_startSpeakW.argtypes = (ctypes.c_void_p, ctypes.c_wchar_p)
		dll.sv_startSpeakW.restype = ctypes.c_int
		dll.sv_read.argtypes = (
			ctypes.c_void_p,
			ctypes.POINTER(ctypes.c_int),
			ctypes.POINTER(ctypes.c_int),
			ctypes.c_void_p,
			ctypes.c_int,
		)
		dll.sv_read.restype = ctypes.c_int
		dll.sv_getFormat.argtypes = (
			ctypes.c_void_p,
			ctypes.POINTER(ctypes.c_int),
			ctypes.POINTER(ctypes.c_int),
			ctypes.POINTER(ctypes.c_int),
		)
		dll.sv_getFormat.restype = ctypes.c_int
		for func_name in _ALLOWED_DLL_CALLS:
			if hasattr(dll, func_name):
				fn = getattr(dll, func_name)
				fn.argtypes = (ctypes.c_void_p, ctypes.c_int)
				fn.restype = None

	# ------------------------------------------------------------------
	# Audio
	def initialize_audio(self, channels: int, sample_rate: int, bits_per_sample: int) -> None:
		if self._player:
			return
		import nvwave
		import config
		try:
			from buildVersion import version_year
		except ImportError:
			version_year = 2025

		# SoftVoice engine outputs 8-bit unsigned PCM at 11025 Hz.
		# WASAPI can't handle 8-bit, so convert to 16-bit.
		# Let WASAPI handle the 11025 Hz resampling natively.
		convert_8to16 = (bits_per_sample == 8)
		player_bps = 16 if convert_8to16 else bits_per_sample

		if version_year >= 2025:
			device = config.conf["audio"]["outputDevice"]
			player = nvwave.WavePlayer(channels, sample_rate, player_bps,
									   outputDevice=device)
		else:
			device = config.conf["speech"]["outputDevice"]
			player = nvwave.WavePlayer(channels, sample_rate, player_bps,
									   outputDevice=device, buffered=True)
		self._player = player
		self._audio_worker = AudioWorker(player, self._audio_queue,
										 lambda: self._sequence,
										 convert_8to16=convert_8to16,
										 player_lock=self._player_lock,
										 auto_idle=False)
		self._audio_worker.start()

	# ------------------------------------------------------------------
	# Speech (blocking)
	def do_speak(self, text: str, generation: Optional[int] = None) -> bool:
		"""Start speech and pump read loop. Returns True on success.

		``generation`` is the stop generation the caller captured when it
		began the utterance.  If a cancel has happened since, the segment is
		dropped instead of being spoken after the user asked for silence.
		"""
		with self._state_lock:
			if generation is not None and generation != self._sequence:
				return False
			self._should_stop = False
			self._current_seq = self._sequence
			if self._audio_worker:
				self._audio_worker._stopping = False
		rc = self._dll.sv_startSpeakW(self._handle, text)
		if rc != 0:
			LOGGER.error("sv_startSpeakW returned %d", rc)
			self._audio_queue.put((b"", None, True, self._current_seq))
			return False
		return self._read_loop()

	def _read_loop(self) -> bool:
		"""Poll sv_read() and push audio to queue. Returns True if completed normally."""
		while not self._should_stop:
			try:
				n = self._dll.sv_read(
					self._handle,
					ctypes.byref(self._out_type),
					ctypes.byref(self._out_value),
					self._audio_buf,
					self._buf_size,
				)
			except Exception:
				LOGGER.exception("sv_read crashed")
				self._audio_queue.put((b"", None, True, self._current_seq))
				return False

			t = self._out_type.value

			if t == SV_ITEM_AUDIO and n > 0:
				# Slice through the memoryview: .raw would copy the whole
				# 64 KB buffer on every read, however few bytes arrived.
				self._audio_queue.put((self._audio_view[:n].tobytes(), None, False, self._current_seq))
			elif t == SV_ITEM_DONE:
				self._audio_queue.put((b"", None, True, self._current_seq))
				return True
			elif t == SV_ITEM_ERROR:
				LOGGER.error("Wrapper error %d", self._out_value.value)
				self._audio_queue.put((b"", None, True, self._current_seq))
				return False
			elif t == SV_ITEM_NONE:
				time.sleep(0.001)
		return False

	# ------------------------------------------------------------------
	# Control
	def dll_call(self, func_name: str, value: int) -> None:
		if func_name not in _ALLOWED_DLL_CALLS:
			raise ValueError(f"Disallowed: {func_name}")
		fn = getattr(self._dll, func_name, None)
		if fn:
			fn(self._handle, value)

	def stop(self) -> None:
		with self._state_lock:
			self._sequence += 1
			self._should_stop = True
			if self._audio_worker:
				self._audio_worker._stopping = True
		if self._handle:
			self._dll.sv_stop(self._handle)
		# Call player.stop() WITHOUT the lock.  WavePlayer.stop() is
		# thread-safe (NVDA calls it from MainThread while synths feed
		# from background threads).  Using the lock here would deadlock
		# if AudioWorker is blocked inside feed() on WASAPI buffer space.
		if self._player:
			try:
				self._player.stop()
			except Exception:
				LOGGER.exception("WavePlayer stop failed")

	def pause(self, switch: bool) -> None:
		with self._player_lock:
			if self._player:
				self._player.pause(switch)

	def feed_marker(self, on_done=None) -> None:
		self._audio_queue.put((b"", on_done, False, self._current_seq))

	def player_idle(self) -> None:
		self._audio_queue.put((b"", "IDLE", False, self._current_seq))

	def get_format(self) -> Dict[str, int]:
		return {
			"sampleRate": self._sample_rate,
			"channels": self._channels,
			"bitsPerSample": self._bits_per_sample,
		}

	# ------------------------------------------------------------------
	# Shutdown
	def shutdown(self) -> None:
		# Stop the player *before* taking _player_lock.  If AudioWorker is
		# parked inside WavePlayer.feed() waiting on WASAPI buffer space it
		# holds that lock, and an unconditional acquire here would hang
		# NVDA's main thread for good when switching synthesizers.
		player = self._player
		if player:
			try:
				player.stop()
			except Exception:
				LOGGER.exception("WavePlayer stop failed during shutdown")
		if self._audio_worker:
			self._audio_worker.stop()
			self._audio_worker.join(timeout=1)
			self._audio_worker = None
		if player:
			self._player = None
			if self._player_lock.acquire(timeout=2):
				try:
					player.close()
				except Exception:
					LOGGER.exception("WavePlayer close failed during shutdown")
				finally:
					self._player_lock.release()
			else:
				# A feed is still in flight.  Closing underneath it could
				# crash NVDA, so abandon this player instead; the OS
				# reclaims the device when the process exits.
				LOGGER.warning("Audio still busy at shutdown; abandoning the player")
		# Do NOT call sv_free — the SoftVoice engine cannot be
		# re-initialized after teardown.  Keep the DLL and handle
		# alive; the OS reclaims everything when NVDA exits.
		self._audio_queue = queue.Queue()
		self._sequence = 0
		self._current_seq = 0


# ---------------------------------------------------------------------------
# Module-level singleton and public API
# ---------------------------------------------------------------------------

_client: SoftVoiceDirectClient = SoftVoiceDirectClient()
_on_done: Optional[Callable] = None
_format: Dict[str, int] = {}
_has_pause_factor: bool = False
_has_trim_silence: bool = False


def initialize(done_callback=None) -> Dict[str, Any]:
	"""Load the DLL and initialize the engine."""
	global _on_done, _format, _has_pause_factor, _has_trim_silence
	_on_done = done_callback

	addon_dir = os.path.abspath(os.path.dirname(__file__))
	tibase_path = _find_tibase32(addon_dir)
	dll_path = os.path.join(addon_dir, "softvoice_wrapper.dll")

	result = _client.do_initialize(dll_path, tibase_path, 1)

	_format = result.get("format", {})
	_has_pause_factor = result.get("hasPauseFactor", False)
	_has_trim_silence = result.get("hasTrimSilence", False)

	_client.initialize_audio(
		channels=_format.get("channels", 1),
		sample_rate=_format.get("sampleRate", 22050),
		bits_per_sample=_format.get("bitsPerSample", 16),
	)

	return result


def has_pause_factor() -> bool:
	return _has_pause_factor


def has_trim_silence() -> bool:
	return _has_trim_silence


def generation() -> int:
	"""Return the current stop generation.

	Capture this before an utterance and hand it to :func:`speak` so segments
	queued before a cancel are discarded rather than spoken after it.
	"""
	return _client._sequence


def speak(text: str, generation: Optional[int] = None) -> bool:
	"""Single-chunk speech (blocks until done). Returns True on success."""
	return _client.do_speak(text, generation)


def dll_call(func_name: str, value: int) -> None:
	"""Call a sv_set* function by name (parameter setters)."""
	_client.dll_call(func_name, value)


def stop() -> None:
	_client.stop()


def pause(switch: bool) -> None:
	_client.pause(switch)


def feed_marker(on_done=None) -> None:
	"""Feed an empty marker to the player. on_done fires when it reaches playback."""
	_client.feed_marker(on_done)


def player_idle() -> None:
	"""Signal the player that no more audio is coming."""
	_client.player_idle()


def terminate() -> None:
	_client.shutdown()


def get_format() -> Dict[str, int]:
	return dict(_format)


def check() -> bool:
	"""Check if the required files are available."""
	base = os.path.abspath(os.path.dirname(__file__))
	wrapper_dll = os.path.join(base, "softvoice_wrapper.dll")
	tibase_dll = _find_tibase32(base)
	return os.path.isfile(wrapper_dll) and bool(tibase_dll)


def _find_tibase32(base_path: str) -> str:
	for p in (
		os.path.join(base_path, "tibase32.dll"),
		os.path.join(base_path, "TIBASE32.DLL"),
	):
		if os.path.isfile(p):
			return p
	return ""
