# Rhoticity's Dashcam Footage Frame Extractor

These scripts will take a directory of separate dashcam footage clips with an overlay of the GPS coordinates, extract the first frame of each as a still image, and use an EXIF editor to add the coordinates and date/time to the metadata. Image files are written to their own separate subdirectory, and after a duplicate and coordinate correctness check, any suspected duplicates or frames with bad/missing coordinates are also moved to a recheck directory.

To use, copy the three files from this repo to your footage directory. When you're ready to extract frames, run the shell script. This will create a Python virtual environment in your directory using requirements.txt and execute the Python script within it. You can safely delete the virtual environment directory when you're done with it.

Install Python 3.13 and the following system tools before running; `requirements.txt` installs Python libraries, not the Tesseract OCR engine or ffprobe:

- macOS (Homebrew): `brew install ffmpeg tesseract`
- Debian/Ubuntu: `sudo apt-get update && sudo apt-get install -y ffmpeg tesseract-ocr tesseract-ocr-eng`
- Windows: `winget install Gyan.FFmpeg` and `winget install UB-Mannheim.Tesseract-OCR`. Run `python extract-coords.py` after installing the requirements in a virtual environment.

Tesseract needs its English (`eng`) language data; check with `tesseract --list-langs`. Ensure `ffprobe` (included with FFmpeg) and `tesseract` are on your PATH, or set `FFPROBE` and `TESSERACT_CMD` to their executable paths. FFmpeg is bundled by the Python `imageio-ffmpeg` package; `FFMPEG` can override its executable path. The script checks for missing tools at startup and prints installation guidance.

Here's a video walkthrough of the script in action: https://youtu.be/ZPLTN5cTyTA
