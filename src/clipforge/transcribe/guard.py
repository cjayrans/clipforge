"""No-speech guard (PRD F17). Pure functions.

v1 ranks from the transcript, so footage with almost no speech (silent gameplay, music) makes weak clips. A source
whose transcript has fewer than 20 words per minute is marked FAILED with reason `no_speech` and the operator is told,
instead of ranking it.
"""

from __future__ import annotations

from clipforge.schemas import Transcript

NO_SPEECH_WPM = 20.0
NO_SPEECH_REASON = "no_speech"


def words_per_minute(transcript: Transcript) -> float:
    if transcript.duration_s <= 0:
        return 0.0
    return len(transcript.words) / (transcript.duration_s / 60.0)


def is_no_speech(transcript: Transcript, threshold_wpm: float = NO_SPEECH_WPM) -> bool:
    return words_per_minute(transcript) < threshold_wpm
