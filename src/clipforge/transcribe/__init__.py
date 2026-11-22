"""Audio extraction, chunking, Groq transcription and the local faster-whisper fallback.

Owner: Phase 1 worktree A (`feat/ingest-transcribe`). See docs/ARCHITECTURE.md §4.2.
Output: `schemas.Transcript`. Import `faster_whisper` lazily (it is installed on Linux only).
"""
