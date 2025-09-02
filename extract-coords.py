import os
import subprocess
import piexif
import pytesseract
import cv2
from PIL import Image
import re
import datetime
import json
import imagehash

# ---- Settings ----
FFMPEG_TIMEOUT = 30
FFPROBE_TIMEOUT = 10
HASH_THRESHOLD = 5
COORD_TOLERANCE = 0.0001  # ~11 meters
INPUT_EXTENSIONS = [".mp4"]

seen_hashes = {}
seen_coordinates = []

def extract_first_frame(video_path, frame_path):
    try:
        subprocess.run([
            "ffmpeg", "-i", video_path,
            "-frames:v", "1", "-update", "1", "-q:v", "2",
            frame_path
        ], check=True, timeout=FFMPEG_TIMEOUT)
        return True
    except subprocess.TimeoutExpired:
        print(f"⏱️ ffmpeg timed out for {video_path}")
        return False
    except subprocess.CalledProcessError as e:
        print(f"❌ ffmpeg error for {video_path}: {e}")
        return False

def dms_rational(deg):
    d = int(deg)
    m = int((deg - d) * 60)
    s = round((deg - d - m / 60) * 3600, 6)
    return [(d, 1), (m, 1), (int(s * 10000), 10000)]

def decimal_to_dms_coords(lat, lon):
    lat_ref = 'N' if lat >= 0 else 'S'
    lon_ref = 'E' if lon >= 0 else 'W'
    return dms_rational(abs(lat)), lat_ref, dms_rational(abs(lon)), lon_ref

def extract_gps_from_image(image_path):
    img = cv2.imread(image_path)
    height, width = img.shape[:2]
    crop = img[int(height*0.88):height, 0:int(width*0.5)]

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 180, 255, cv2.THRESH_BINARY)
    config = r'--psm 6 -c tessedit_char_whitelist=0123456789:.NSEW '
    text = pytesseract.image_to_string(thresh, config=config)

    print("📄 OCR Output:\n", text)

    match = re.search(r"N[:\s]?(\d{1,3}\.\d+).+[EW][:\s]?(\d{1,3}\.\d+)", text, re.IGNORECASE)
    if match:
        try:
            lat = float(match.group(1))
            lon = float(match.group(2))
            dir_match = re.search(r"[EW]", text, re.IGNORECASE)
            if dir_match and dir_match.group(0).upper() == "W":
                lon = -lon
            print(f"📍 Parsed Coordinates: lat={lat}, lon={lon}")
            return lat, lon
        except Exception as e:
            print("❌ Error parsing coordinates:", e)

    print("⚠️ Could not extract GPS coordinates from OCR text.")
    return None, None

def get_video_creation_datetime(video_path):
    try:
        result = subprocess.run([
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_entries", "format_tags=creation_time",
            video_path
        ], capture_output=True, text=True, check=True, timeout=FFPROBE_TIMEOUT)

        data = json.loads(result.stdout)
        creation_iso = data["format"]["tags"]["creation_time"]
        dt = datetime.datetime.fromisoformat(creation_iso.replace("Z", "")).replace(microsecond=0)
        return dt.strftime("%Y:%m:%d %H:%M:%S")
    except Exception as e:
        print(f"⚠️ Failed to get media creation time for {video_path}: {e}")
        return None

def exif_already_contains_data(image_path):
    try:
        exif_dict = piexif.load(image_path)
        gps = exif_dict.get("GPS", {})
        datetime = exif_dict.get("Exif", {}).get(piexif.ExifIFD.DateTimeOriginal)
        has_gps = piexif.GPSIFD.GPSLatitude in gps and piexif.GPSIFD.GPSLongitude in gps
        has_date = datetime is not None
        return has_gps and has_date
    except Exception:
        return False

def is_duplicate_image(image_path):
    try:
        img = Image.open(image_path)
        hash_val = imagehash.phash(img)

        for existing_hash in seen_hashes.values():
            if abs(hash_val - existing_hash) <= HASH_THRESHOLD:
                return True

        seen_hashes[image_path] = hash_val
        return False
    except Exception as e:
        print(f"⚠️ Error during image hash check: {e}")
        return False

def is_duplicate_coordinates(lat, lon):
    for seen_lat, seen_lon in seen_coordinates:
        if abs(lat - seen_lat) <= COORD_TOLERANCE and abs(lon - seen_lon) <= COORD_TOLERANCE:
            return True
    seen_coordinates.append((lat, lon))
    return False

def add_gps_and_timestamp_to_exif(image_path, lat, lon, datetime_str):
    lat_dms, lat_ref, lon_dms, lon_ref = decimal_to_dms_coords(lat, lon)
    exif_dict = {
        "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: lat_ref.encode(),
            piexif.GPSIFD.GPSLatitude: lat_dms,
            piexif.GPSIFD.GPSLongitudeRef: lon_ref.encode(),
            piexif.GPSIFD.GPSLongitude: lon_dms,
        },
        "0th": {},
        "Exif": {}
    }

    if datetime_str:
        exif_dict["Exif"].update({
            piexif.ExifIFD.DateTimeOriginal: datetime_str.encode(),
            piexif.ExifIFD.DateTimeDigitized: datetime_str.encode()
        })
        exif_dict["0th"].update({
            piexif.ImageIFD.DateTime: datetime_str.encode()
        })

    exif_bytes = piexif.dump(exif_dict)
    piexif.insert(exif_bytes, image_path)

def process_video(video_path, output_dir, index, total):
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    frame_path = os.path.join(output_dir, base_name + "_frame.jpg")

    print(f"\n[{index}/{total}] 🚗 Processing {os.path.basename(video_path)}...")

    if os.path.exists(frame_path) and exif_already_contains_data(frame_path):
        print(f"⏭️ Skipping (already processed): {frame_path}")
        return

    if not extract_first_frame(video_path, frame_path):
        print(f"❌ Failed to extract frame from {video_path}")
        return

    if is_duplicate_image(frame_path):
        os.remove(frame_path)
        print(f"🗑️ Removed visual duplicate: {frame_path}")
        return

    lat, lon = extract_gps_from_image(frame_path)
    if lat is None or lon is None:
        print(f"⚠️ Skipping due to missing GPS: {frame_path}")
        return

    if lat == 0.0 and lon == 0.0:
        review_dir = os.path.join(output_dir, "review_frames")
        os.makedirs(review_dir, exist_ok=True)
        review_path = os.path.join(review_dir, os.path.basename(frame_path))
        os.rename(frame_path, review_path)
        print(f"📥 Moved (0,0) frame to review: {review_path}")
        return

    if is_duplicate_coordinates(lat, lon):
        os.remove(frame_path)
        print(f"🗑️ Removed GPS duplicate: {frame_path}")
        return

    datetime_str = get_video_creation_datetime(video_path)
    add_gps_and_timestamp_to_exif(frame_path, lat, lon, datetime_str)
    print(f"✅ Geotagged & timestamped: {frame_path}")

if __name__ == "__main__":
    input_dir = os.getcwd()
    output_dir = os.path.join(input_dir, "output_frames")
    os.makedirs(output_dir, exist_ok=True)

    video_files = [
        f for f in os.listdir(input_dir)
        if os.path.splitext(f)[1].lower() in INPUT_EXTENSIONS
    ]
    total = len(video_files)

    for i, filename in enumerate(video_files, start=1):
        video_path = os.path.join(input_dir, filename)
        process_video(video_path, output_dir, i, total)
