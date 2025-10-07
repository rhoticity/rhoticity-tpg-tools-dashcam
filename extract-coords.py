#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
extract-coords.py

Features
- Option A bootstrap (no PATH drama):
  * Uses imageio-ffmpeg to resolve an ffmpeg binary automatically.
  * Preflight checks for ffprobe and tesseract with friendly guidance.
  * Env overrides: FFPROBE, TESSERACT_CMD.

- Interactive mode selection:
  * "First frame only" per video (default).
  * "Interval mode" -> user provides seconds (float) + optional safety cap on frames/video.

- Safety cap:
  * Prompt for max frames per video (default 500).
  * Estimate frames from duration; if estimate > cap, limit with -frames:v CAP.

- Robust OCR:
  * Multi-ROI search (bottom-left, bottom-right, bottom strip).
  * Upscale + hist eq + adaptive threshold + morphology.
  * Regex accepts decimal and DMS formats; common OCR char fixes.

- EXIF writing:
  * Writes GPS tags and DateTimeOriginal (if available from ffprobe).
  * For interval frames, DateTimeOriginal ≈ creation_time + n*interval.

- Non-interactive ffmpeg (-y) to avoid overwrite prompts.

Tested on Windows with Python 3.13.x
"""

import os
import re
import json
import math
import datetime
import subprocess
import shutil
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PIL import Image
import piexif
import imagehash
import pytesseract
import imageio_ffmpeg

# ---------- Settings ----------
FFMPEG_TIMEOUT = 30            # single-frame extract
FFPROBE_TIMEOUT = 10
FFMPEG_TIMEOUT_BULK = 300      # multi-frame interval extracts

# Safety cap default for interval mode
DEFAULT_MAX_FRAMES_PER_VIDEO = 500

# Skip writing new image if it's too similar to existing (lower = stricter)
HASH_THRESHOLD = 5

# Consider coords "same" if within ~11 meters
COORD_TOLERANCE = 0.0001

# Recognized input video extensions
INPUT_EXTENSIONS = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp"}

# Save thresholded ROIs for debugging (False by default)
SAVE_DEBUG_ROIS = False


# ---------- Executables (Option A) ----------
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()  # resolved path from imageio-ffmpeg
FFPROBE = os.environ.get("FFPROBE") or shutil.which("ffprobe")
TESSERACT = os.environ.get("TESSERACT_CMD") or shutil.which("tesseract")

# If TESSERACT is explicitly provided, wire it into pytesseract
if TESSERACT:
    try:
        pytesseract.pytesseract.tesseract_cmd = TESSERACT
    except Exception:
        pass


def preflight_or_die():
    missing = []
    if not FFMPEG or not os.path.exists(FFMPEG):
        missing.append("ffmpeg")
    if not FFPROBE:
        missing.append("ffprobe")
    if not TESSERACT:
        missing.append("tesseract")
    if missing:
        print("❌ Missing required tools:", ", ".join(missing))
        print("\nWindows quick install (PowerShell):")
        print("  winget install Gyan.FFmpeg")
        print("  winget install UB-Mannheim.Tesseract-OCR")
        print("\nOr set explicit locations (no PATH needed):")
        print(r"  setx FFPROBE C:\ffmpeg\bin\ffprobe.exe")
        print(r"  setx TESSERACT_CMD C:\Program Files\Tesseract-OCR\tesseract.exe")
        print("\nAlternatively, inside Python:")
        print(r"  import pytesseract; pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'")
        raise SystemExit(1)


# ---------- Utilities ----------
def nearly_same_coords(lat1, lon1, lat2, lon2, tol=COORD_TOLERANCE):
    if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
        return False
    return (abs(lat1 - lat2) <= tol) and (abs(lon1 - lon2) <= tol)


def dms_to_decimal(deg, minutes, seconds, hemi):
    val = float(deg) + float(minutes) / 60.0 + float(seconds) / 3600.0
    if hemi.upper() in ("S", "W"):
        val *= -1.0
    return val


def ensure_dir(p: Path):
    p.mkdir(parents=True, exist_ok=True)


# ---------- ffmpeg / ffprobe helpers ----------
def extract_first_frame(video_path: Path, frame_path: Path) -> bool:
    """
    Extract first frame to JPEG using FFmpeg (non-interactive).
    """
    cmd = [
        FFMPEG, "-y", "-i", str(video_path),
        "-frames:v", "1", "-q:v", "2",
        str(frame_path)
    ]
    subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT)
    return frame_path.exists()


def ffprobe_creation_time(video_path: Path) -> Optional[datetime.datetime]:
    """
    Read format_tags=creation_time from ffprobe (UTC).
    """
    cmd = [
        FFPROBE, "-v", "quiet",
        "-print_format", "json",
        "-show_entries", "format_tags=creation_time",
        str(video_path)
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
    try:
        data = json.loads(proc.stdout)
        tags = data.get("format", {}).get("tags", {}) or {}
        ct = tags.get("creation_time")
        if not ct:
            return None
        # Normalize ISO timestamp (e.g., "2025-06-09T19:58:35.000000Z")
        dt = datetime.datetime.fromisoformat(ct.replace("Z", "+00:00"))
        return dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    except Exception:
        return None


def ffprobe_duration_seconds(video_path: Path) -> Optional[float]:
    """
    Return total duration (seconds) using ffprobe.
    """
    cmd = [
        FFPROBE, "-v", "quiet",
        "-print_format", "json",
        "-show_entries", "format=duration",
        str(video_path)
    ]
    try:
        proc = subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
        data = json.loads(proc.stdout)
        dur = data.get("format", {}).get("duration")
        if dur is None:
            return None
        return float(dur)
    except Exception:
        return None


def extract_frames_interval(video_path: Path, out_dir: Path, interval_sec: float, max_frames: Optional[int]) -> list[Path]:
    """
    Extract frames every `interval_sec` seconds into out_dir as frame_000001.jpg, etc.
    If max_frames is provided, limit total frames emitted by ffmpeg.
    Returns a sorted list of frame paths.
    """
    ensure_dir(out_dir)
    pattern = out_dir / "frame_%06d.jpg"
    fps_expr = f"fps=1/{interval_sec:.6f}"

    cmd = [
        FFMPEG, "-y", "-i", str(video_path),
        "-vf", fps_expr,
        "-q:v", "2",
    ]
    if max_frames is not None and max_frames > 0:
        cmd += ["-frames:v", str(max_frames)]
    cmd += [str(pattern)]

    subprocess.run(cmd, check=True, timeout=FFMPEG_TIMEOUT_BULK)
    frames = sorted(out_dir.glob("frame_*.jpg"))
    return frames


# ---------- OCR ----------
_DECIMAL_RE = re.compile(
    r'([NS])\s*[: ]?\s*([0-9]{1,3}\.[0-9]+)\D+([EW])\s*[: ]?\s*([0-9]{1,3}\.[0-9]+)',
    re.IGNORECASE
)
_DMS_RE = re.compile(
    r'([NS])\s*([0-9]{1,3})[°:\s]\s*([0-9]{1,2})[\'’:\s]\s*([0-9]{1,2}(?:\.[0-9]+)?)["”]?\D+'
    r'([EW])\s*([0-9]{1,3})[°:\s]\s*([0-9]{1,2})[\'’:\s]\s*([0-9]{1,2}(?:\.[0-9]+)?)["”]?',
    re.IGNORECASE
)

def _preprocess_for_ocr(crop: np.ndarray) -> np.ndarray:
    """
    Upscale + equalize + adaptive threshold + light morphology.
    """
    crop = cv2.resize(crop, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    thr = cv2.adaptiveThreshold(
        gray, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 31, 15
    )
    kernel = np.ones((2, 2), np.uint8)
    thr = cv2.morphologyEx(thr, cv2.MORPH_OPEN, kernel, iterations=1)
    thr = cv2.morphologyEx(thr, cv2.MORPH_CLOSE, kernel, iterations=1)
    return thr


def extract_gps_from_image(image_path: Path) -> tuple[Optional[float], Optional[float]]:
    """
    Try multiple bottom ROIs, run OCR, and parse decimal or DMS coordinates.
    """
    img = cv2.imread(str(image_path))
    if img is None:
        print(f"⚠️ Could not read image: {image_path}")
        return None, None

    H, W = img.shape[:2]
    rois = [
        (int(H * 0.84), H, 0,            int(W * 0.50)),
        (int(H * 0.84), H, int(W * 0.50), W),
        (int(H * 0.86), H, 0,            W),
    ]

    config = r'--psm 7 --oem 3 -c tessedit_char_whitelist=0123456789NSEW.:°\'" -l eng'
    dbg_dir = image_path.parent / "debug_rois" if SAVE_DEBUG_ROIS else None
    if dbg_dir:
        ensure_dir(dbg_dir)

    for (y1, y2, x1, x2) in rois:
        y1 = max(0, min(H - 1, y1)); y2 = max(0, min(H, y2))
        x1 = max(0, min(W - 1, x1)); x2 = max(0, min(W, x2))
        crop = img[y1:y2, x1:x2]
        if crop.size == 0:
            continue

        thr = _preprocess_for_ocr(crop)
        if dbg_dir:
            cv2.imwrite(str(dbg_dir / f"{image_path.stem}_{y1}-{y2}_{x1}-{x2}.png"), thr)

        text = pytesseract.image_to_string(thr, config=config)
        cleaned = (text.replace('O', '0').replace('o', '0')
                        .replace('I', '1').replace('|', '1')
                        .replace('S', '5'))

        print("📄 OCR Output (candidate):")
        print(cleaned.strip())

        m = _DECIMAL_RE.search(cleaned)
        if m:
            hemi_lat, lat_s, hemi_lon, lon_s = m.groups()
            lat = float(lat_s); lon = float(lon_s)
            if hemi_lat.upper() == 'S': lat = -lat
            if hemi_lon.upper() == 'W': lon = -lon
            print(f"📍 Parsed (decimal): lat={lat}, lon={lon}")
            return lat, lon

        m = _DMS_RE.search(cleaned)
        if m:
            ns, d1, m1, s1, ew, d2, m2, s2 = m.groups()
            lat = dms_to_decimal(d1, m1, s1, ns)
            lon = dms_to_decimal(d2, m2, s2, ew)
            print(f"📍 Parsed (DMS): lat={lat}, lon={lon}")
            return lat, lon

    print("⚠️ Could not extract GPS coordinates from OCR text.")
    return None, None


# ---------- EXIF ----------
def _deg_to_rational(deg):
    # Convert decimal degrees to EXIF rationals (DMS)
    d = int(abs(deg))
    m_float = (abs(deg) - d) * 60
    m = int(m_float)
    s = round((m_float - m) * 60 * 1000)
    return ((d, 1), (m, 1), (s, 1000))


def write_exif_gps_and_time(image_path: Path, lat: float, lon: float, dt: Optional[datetime.datetime]):
    """
    Writes GPSLatitude/Longitude (+Ref) and DateTimeOriginal to the JPEG.
    """
    img = Image.open(image_path)
    exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}

    # Carry forward existing EXIF if present
    try:
        exif_bytes = img.info.get("exif", None)
        if exif_bytes:
            exif_dict = piexif.load(exif_bytes)
    except Exception:
        pass

    gps_ifd = exif_dict.get("GPS", {})

    # GPS refs
    gps_ifd[piexif.GPSIFD.GPSLatitudeRef]  = b"N" if lat >= 0 else b"S"
    gps_ifd[piexif.GPSIFD.GPSLongitudeRef] = b"E" if lon >= 0 else b"W"
    gps_ifd[piexif.GPSIFD.GPSLatitude]     = _deg_to_rational(lat)
    gps_ifd[piexif.GPSIFD.GPSLongitude]    = _deg_to_rational(lon)

    exif_dict["GPS"] = gps_ifd

    # DateTimeOriginal
    if dt:
        ts = dt.strftime("%Y:%m:%d %H:%M:%S").encode("ascii")
        exif_dict["Exif"][piexif.ExifIFD.DateTimeOriginal] = ts

    exif_bytes = piexif.dump(exif_dict)
    img.save(image_path, "jpeg", exif=exif_bytes)


# ---------- Processing ----------
def process_video_first_frame(video_path: Path, output_dir: Path, idx: int, total: int):
    print(f"[{idx}/{total}] 🚗 Processing {video_path.name} (first frame)...")

    frame_path = output_dir / f"{video_path.stem}_frame.jpg"
    ensure_dir(output_dir)

    # Extract one frame
    try:
        extracted = extract_first_frame(video_path, frame_path)
        if not extracted:
            print(f"⚠️ Failed to extract frame for {video_path.name}")
            return
    except subprocess.CalledProcessError as e:
        print(f"❌ ffmpeg failed for {video_path.name}: {e}")
        return
    except subprocess.TimeoutExpired:
        print(f"⏱️ ffmpeg timed out for {video_path.name}")
        return

    # OCR GPS
    lat, lon = extract_gps_from_image(frame_path)
    if lat is None or lon is None:
        print(f"⚠️ Skipping due to missing GPS: {frame_path}")
        return

    # Read creation time
    dt = ffprobe_creation_time(video_path)

    # Write EXIF
    try:
        write_exif_gps_and_time(frame_path, lat, lon, dt)
        print(f"✅ Wrote EXIF: lat={lat}, lon={lon}" + (f", time={dt}" if dt else ""))
    except Exception as e:
        print(f"❌ Failed to write EXIF: {e}")


def process_video_interval(video_path: Path, output_dir: Path, interval_sec: float, max_frames: int, idx: int, total: int):
    print(f"[{idx}/{total}] 🚗 Processing {video_path.name} (every {interval_sec}s, cap {max_frames} frames)...")

    # Per-video subdir for frames
    video_out_dir = output_dir / video_path.stem
    ensure_dir(video_out_dir)

    # Estimate frames and inform about capping
    duration = ffprobe_duration_seconds(video_path)
    if duration is not None and interval_sec > 0:
        est = int(math.floor(duration / interval_sec))
        if est > max_frames:
            print(f"ℹ️ Estimated {est} frames; applying safety cap of {max_frames}.")

    # Extract multiple frames (capped)
    try:
        frames = extract_frames_interval(video_path, video_out_dir, interval_sec, max_frames)
        if not frames:
            print(f"⚠️ No frames extracted for {video_path.name}")
            return
        print(f"🖼️ Extracted {len(frames)} frames.")
    except subprocess.CalledProcessError as e:
        print(f"❌ ffmpeg failed for {video_path.name}: {e}")
        return
    except subprocess.TimeoutExpired:
        print(f"⏱️ ffmpeg timed out for {video_path.name}")
        return

    # Base time
    base_dt = ffprobe_creation_time(video_path)

    # Iterate frames and OCR + EXIF
    for i, frame_path in enumerate(frames, start=1):
        # Approximate timestamp: creation_time + (i-1)*interval
        dt = None
        if base_dt is not None:
            try:
                dt = base_dt + datetime.timedelta(seconds=(i - 1) * interval_sec)
            except Exception:
                dt = base_dt

        lat, lon = extract_gps_from_image(frame_path)
        if lat is None or lon is None:
            print(f"⚠️ [frame {i}] No GPS parsed: {frame_path.name}")
            continue

        try:
            write_exif_gps_and_time(frame_path, lat, lon, dt)
            stamp = dt if dt else "unknown time"
            print(f"✅ [frame {i}] EXIF set: lat={lat}, lon={lon}, time={stamp}")
        except Exception as e:
            print(f"❌ [frame {i}] Failed to write EXIF: {e}")


# ---------- Interactive prompt ----------
def ask_mode_and_params():
    print("\nHow would you like to extract images from your videos?")
    print("  [1] First frame only (default)")
    print("  [2] Every N seconds (interval mode)")
    choice = input("Enter 1 or 2 [default 1]: ").strip()

    if choice == "" or choice == "1":
        return ("first", None, None)

    if choice == "2":
        # Interval
        while True:
            n = input("Extract a frame every how many seconds? (e.g., 5, 2.5): ").strip()
            try:
                interval = float(n)
                if interval <= 0:
                    print("Please enter a positive number.")
                    continue
                break
            except ValueError:
                print("Please enter a valid number, e.g., 5 or 2.5.")

        # Safety cap
        cap_in = input(f"Maximum frames per video? [default {DEFAULT_MAX_FRAMES_PER_VIDEO}]: ").strip()
        if cap_in == "":
            cap = DEFAULT_MAX_FRAMES_PER_VIDEO
        else:
            try:
                cap = int(cap_in)
                if cap <= 0:
                    print(f"Non-positive cap given; using default {DEFAULT_MAX_FRAMES_PER_VIDEO}.")
                    cap = DEFAULT_MAX_FRAMES_PER_VIDEO
            except ValueError:
                print(f"Invalid number; using default {DEFAULT_MAX_FRAMES_PER_VIDEO}.")
                cap = DEFAULT_MAX_FRAMES_PER_VIDEO

        return ("interval", interval, cap)

    print("Unrecognized choice; defaulting to first frame mode.")
    return ("first", None, None)


# ---------- Main ----------
def main():
    preflight_or_die()

    mode, interval, cap = ask_mode_and_params()

    input_dir = Path.cwd()
    output_dir = input_dir / "output_frames"
    ensure_dir(output_dir)

    video_files = [
        p for p in input_dir.iterdir()
        if p.is_file() and p.suffix.lower() in INPUT_EXTENSIONS
    ]
    total = len(video_files)
    if total == 0:
        print("ℹ️ No video files found here.")
        return

    for i, vp in enumerate(sorted(video_files), start=1):
        try:
            if mode == "first":
                process_video_first_frame(vp, output_dir, i, total)
            else:
                process_video_interval(vp, output_dir, interval, cap, i, total)
        except KeyboardInterrupt:
            print("\n🛑 Interrupted by user.")
            break
        except Exception as e:
            print(f"⚠️ Unexpected error on {vp.name}: {e}")

    print("\n✨ Done.")


if __name__ == "__main__":
    main()
