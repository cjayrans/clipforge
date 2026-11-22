"""Generate tests/fixtures/synthetic_90s.mp4: 90 s, 1920x1080, 30 fps, H.264 + AAC 440 Hz sine.

Three shots with hard cuts at 30 s and 60 s (testsrc, testsrc2, smptehdbars) so shot detection has known boundaries.
No faces (exercises the fit_blur default). Needs ffmpeg; run it inside the image: `make fixtures`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_OUT = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "synthetic_90s.mp4"
SIZE, RATE, SHOT_S = "1920x1080", 30, 30


def command(out: Path) -> list[str]:
    shots = ["testsrc", "testsrc2", "smptehdbars"]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for src in shots:
        cmd += ["-f", "lavfi", "-i", f"{src}=size={SIZE}:rate={RATE}:duration={SHOT_S}"]
    cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={SHOT_S * len(shots)}"]
    cmd += [
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-map", "3:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "38", "-pix_fmt", "yuv420p", "-g", "60",
        "-c:a", "aac", "-b:a", "64k",
        "-movflags", "+faststart",
        str(out),
    ]  # fmt: skip
    return cmd


def main() -> int:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_OUT
    if shutil.which("ffmpeg") is None:
        print("ffmpeg not found; run `make fixtures` (uses the Docker image)", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(command(out), check=True)
    print(f"wrote {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
