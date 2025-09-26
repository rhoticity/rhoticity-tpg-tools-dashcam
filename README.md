# Rhoticity's Dashcam Footage Frame Extractor

These scripts will take a directory of separate dashcam footage clips with an overlay of the GPS coordinates, extract the first frame of each as a still image, and use an EXIF editor to add the coordinates and date/time to the metadata. Image files are written to their own separate subdirectory, and after a duplicate and coordinate correctness check, any suspected duplicates or frames with bad/missing coordinates are also moved to a recheck directory.

To use, copy the three files from this repo to your footage directory. When you're ready to extract frames, run the shell script. This will create a Python virtual environment in your directory using requirements.txt and execute the Python script within it. You can safely delete the virtual environment directory when you're done with it.
