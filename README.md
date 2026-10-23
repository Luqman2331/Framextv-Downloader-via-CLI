# framextvdownloaderviacli

This tool downloads videos from framextv.cc.

## What it does and How to use

- It downloads videos and subtitles (`.srt` files).
- Download scripts framextcdownload.py and Run(before run this command check the Requirements):
```bash
  python3 framextvdownload.py
```

  Then the UI will appear.

- First, click your video want to watch and copy a video link from the web. Example: https://framextv.cc/movie/969681
- Then, paste the link in the UI and press Enter or click Download.
- Then, pick the quality.
- Then, pick the subtitle in your main language, if available. Press Space or click a track to pick it, then press Enter to start the download.
- Subtitles are saved as separate `.srt` files. The subtitles are NOT inside the video file. To watch with subtitles, use VLC or a media player that can load a separate `.srt` file.
- For TV shows, use `/watch/<id>/<season>/<episode>`. Example: https://framextv.cc/watch/335032/1/1
- If a download stops, you can run it again. It will continue.
- After a download finishes, the video pieces (`.ts` files) are deleted by themselves. Use `--no-clean` to keep them.
- If a video part fails, the tool will try again by itself.
- The video will be in a folder called `dl_framextv_<id>/`.

##  Requirements.

- Python 3
- ffmpeg (it must be in your PATH)
- Windows:

```cmd
    winget install -e --id Gyan.FFmpeg
```

    Note for Windows: after you install ffmpeg, close the terminal and open a new one.

- Linux:

```bash
    sudo xbps-install -S ffmpeg
```

- These Python packages for window and linux:

```bash
  pip install requests textual
```

Note for Windows: after you install ffmpeg, close the terminal and open a new one. Then run the tool.

