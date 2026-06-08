"""Desktop GUI for the Reddit story video maker.

Left side: paste a Reddit URL, preview the story/script, pick voice + background.
Right side: embedded video player previews the finished render before you post it.

Run with:  python gui.py
"""
import multiprocessing as mp
import os
import queue as queue_module
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# When launched without a console (e.g. via pythonw.exe / launch_gui.vbs), stdout
# and stderr are None - libraries like moviepy/tqdm crash trying to write progress
# messages to them ("'NoneType' object has no attribute 'write'"). Give them a
# harmless place to write instead.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import vlc

from assemble import build_thumbnail_image
from fetch_story import COMMON_STORY_SUBREDDITS, build_script, fetch_story, fetch_trending_post, story_from_text
from main import pipeline_worker
from tts import VOICES
from tiktok_upload import TikTokNotConfigured, is_configured as tiktok_is_configured, upload_video as upload_to_tiktok
from youtube_upload import YouTubeNotConfigured, is_configured as youtube_is_configured, upload_video as upload_to_youtube

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), "output")


class VideoMakerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Reddit Story Video Maker")
        self.geometry("1180x680")
        self.minsize(980, 560)

        self.story = None
        self.background_path = tk.StringVar()
        self.output_name = tk.StringVar()
        self.voice = tk.StringVar(value="male_us")
        self.status = tk.StringVar(value="Paste a Reddit post URL and press Enter or click Preview.")
        self.timer_text = tk.StringVar(value="")
        self.auto_open = tk.BooleanVar(value=True)
        self.part_number = tk.IntVar(value=1)
        self.player_status = tk.StringVar(value="No video yet — generate one to preview it here.")

        self._timer_job = None
        self._gen_start = None
        self._gen_estimate = None
        self._last_video_path = None
        self._last_video_paths = []  # every file from the most recent render - >1 means it was auto-split into parts
        self._last_bg_segment = None  # {"path": <bg clip path>, "end": <seconds>} of the last rendered segment
        self._last_part = None  # part number (2+) baked into the most recently rendered video, if any

        # Generation runs in its own process so a hang anywhere in the pipeline
        # (TTS, transcription, ffmpeg encoding) can be killed outright via Stop,
        # rather than freezing the GUI with no way to recover.
        self._gen_process = None
        self._gen_queue = None
        self._gen_paths = None  # (audio_path, video_path, bg_path) of the in-flight render

        # VLC setup for the embedded preview player
        self._vlc_instance = vlc.Instance("--quiet")
        self._player = self._vlc_instance.media_player_new()

        self._build_layout()

    # ---------- UI layout ----------
    def _build_layout(self):
        pad = {"padx": 10, "pady": 6}

        root = ttk.Frame(self)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(0, weight=1)

        left = ttk.Frame(root)
        left.grid(row=0, column=0, sticky="nsew", padx=(10, 5), pady=10)
        right = ttk.Frame(root)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=10)

        self._build_left_pane(left)
        self._build_right_pane(right)

        # Status bar spans the full width
        ttk.Label(self, textvariable=self.status, anchor="w", foreground="#333").pack(fill="x", padx=10, pady=(0, 8))

    def _build_left_pane(self, parent):
        # URL row
        url_frame = ttk.Frame(parent)
        url_frame.pack(fill="x", pady=(0, 6))
        ttk.Label(url_frame, text="Reddit post URL:").pack(side="left")
        self.url_entry = ttk.Entry(url_frame)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=8)
        self.url_entry.bind("<Return>", lambda _evt: self.on_preview())
        self.preview_btn = ttk.Button(url_frame, text="Preview", command=self.on_preview)
        self.preview_btn.pack(side="left")
        ttk.Button(url_frame, text="Paste manually...", command=self.on_paste_manually).pack(side="left", padx=(6, 0))
        ttk.Button(url_frame, text="🔥 Find Trending Story", command=self.on_find_trending).pack(side="left", padx=(6, 0))

        # Preview area
        preview_frame = ttk.LabelFrame(parent, text="Preview (edit the script before generating if you like)")
        preview_frame.pack(fill="both", expand=True, pady=6)

        self.title_label = ttk.Label(preview_frame, text="", font=("Segoe UI", 11, "bold"), wraplength=480)
        self.title_label.pack(anchor="w", padx=8, pady=(8, 0))
        self.meta_label = ttk.Label(preview_frame, text="", foreground="#555")
        self.meta_label.pack(anchor="w", padx=8)

        self.script_box = tk.Text(preview_frame, wrap="word", height=14)
        self.script_box.pack(fill="both", expand=True, padx=8, pady=8)

        # Options
        opts_frame = ttk.Frame(parent)
        opts_frame.pack(fill="x", pady=6)

        ttk.Label(opts_frame, text="Voice:").grid(row=0, column=0, sticky="w")
        voice_menu = ttk.Combobox(opts_frame, textvariable=self.voice, values=list(VOICES.keys()), state="readonly", width=12)
        voice_menu.grid(row=0, column=1, sticky="w", padx=(6, 24))

        ttk.Label(opts_frame, text="Background clip:").grid(row=0, column=2, sticky="w")
        self.bg_entry = ttk.Entry(opts_frame, textvariable=self.background_path)
        self.bg_entry.grid(row=0, column=3, sticky="we", padx=6)
        ttk.Button(opts_frame, text="Browse...", command=self.on_browse_background).grid(row=0, column=4)
        opts_frame.columnconfigure(3, weight=1)

        ttk.Label(opts_frame, text="Output name:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.name_entry = ttk.Entry(opts_frame, textvariable=self.output_name)
        self.name_entry.grid(row=1, column=1, columnspan=4, sticky="we", padx=6, pady=(8, 0))
        ttk.Label(opts_frame, text="(saved as <name>.mp4 + <name>_text.srt)", foreground="#777").grid(
            row=2, column=1, columnspan=4, sticky="w", padx=6
        )

        # Generate row
        gen_frame = ttk.Frame(parent)
        gen_frame.pack(fill="x", pady=6)
        self.generate_btn = ttk.Button(gen_frame, text="Generate Video", command=self.on_generate, state="disabled")
        self.generate_btn.pack(side="left")
        self.stop_generate_btn = ttk.Button(gen_frame, text="■ Stop", command=self.on_stop_generate, state="disabled")
        self.stop_generate_btn.pack(side="left", padx=(6, 0))
        ttk.Checkbutton(gen_frame, text="Open output folder when done", variable=self.auto_open).pack(side="left", padx=12)

        part_frame = ttk.Frame(gen_frame)
        part_frame.pack(side="left", padx=12)
        ttk.Label(part_frame, text="Part #:").pack(side="left")
        self.part_spinbox = ttk.Spinbox(part_frame, from_=1, to=20, width=3, textvariable=self.part_number, state="readonly")
        self.part_spinbox.pack(side="left", padx=(4, 0))
        ttk.Label(
            part_frame,
            text="(2+ continues the last background and shows a red \"PART N\" badge on the card)",
            foreground="#777",
        ).pack(side="left", padx=(6, 0))

        progress_frame = ttk.Frame(parent)
        progress_frame.pack(fill="x")
        self.progress = ttk.Progressbar(progress_frame, mode="determinate", maximum=100, value=0)
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(progress_frame, textvariable=self.timer_text, foreground="#555", width=22, anchor="e").pack(side="left", padx=(8, 0))

    def _build_right_pane(self, parent):
        player_frame = ttk.LabelFrame(parent, text="Preview your video before posting")
        player_frame.pack(fill="both", expand=True)

        # This frame's window handle gets handed to VLC to render video into.
        self.video_canvas = tk.Frame(player_frame, bg="black")
        self.video_canvas.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        ttk.Label(player_frame, textvariable=self.player_status, foreground="#555").pack(anchor="w", padx=8)

        controls = ttk.Frame(player_frame)
        controls.pack(fill="x", padx=8, pady=8)
        self.play_btn = ttk.Button(controls, text="▶ Play", command=self.on_play_pause, state="disabled")
        self.play_btn.pack(side="left")
        self.stop_btn = ttk.Button(controls, text="■ Stop", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(controls, text="Open in default player", command=self.on_open_external).pack(side="left", padx=6)

        publish = ttk.Frame(player_frame)
        publish.pack(fill="x", padx=8, pady=(0, 8))
        self.upload_youtube_btn = ttk.Button(publish, text="Upload to YouTube", command=self.on_upload_youtube, state="disabled")
        self.upload_youtube_btn.pack(side="left")
        ttk.Label(publish, text="(uploads as a public Short using the story title)", foreground="#777").pack(side="left", padx=(8, 0))

        self.upload_tiktok_btn = ttk.Button(publish, text="Upload to TikTok", command=self.on_upload_tiktok, state="disabled")
        self.upload_tiktok_btn.pack(side="left", padx=(16, 0))
        ttk.Label(publish, text="(sends to your TikTok inbox to review + post until your app is audited)", foreground="#777").pack(side="left", padx=(8, 0))

    # ---------- preview actions ----------
    def on_browse_background(self):
        path = filedialog.askopenfilename(
            title="Choose a background video clip",
            filetypes=[("Video files", "*.mp4 *.mov *.mkv *.webm"), ("All files", "*.*")],
        )
        if path:
            self.background_path.set(path)

    def on_preview(self):
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showwarning("Missing URL", "Paste a Reddit post URL first.")
            return

        self._set_busy(True, "Fetching story...")

        def work():
            try:
                story = fetch_story(url)
                script = build_script(story)
            except Exception as exc:
                self.after(0, lambda: self._preview_failed(exc))
                return
            self.after(0, lambda: self._preview_loaded(story, script))

        threading.Thread(target=work, daemon=True).start()

    def on_find_trending(self):
        self._set_busy(True, "Scanning trending subreddits for a story...")

        def work():
            try:
                story = fetch_trending_post()
                script = build_script(story)
            except Exception as exc:
                self.after(0, lambda: self._preview_failed(exc))
                return
            self.after(0, lambda: self._preview_loaded(story, script))
            self.after(0, lambda: self.url_entry.delete(0, "end"))
            self.after(0, lambda: self.url_entry.insert(0, story.get("url", "")))

        threading.Thread(target=work, daemon=True).start()

    def on_paste_manually(self):
        dialog = tk.Toplevel(self)
        dialog.title("Paste a story manually")
        dialog.geometry("520x420")
        dialog.transient(self)
        dialog.grab_set()

        pad = {"padx": 10, "pady": 6}
        ttk.Label(dialog, text="Title:").pack(anchor="w", **pad)
        title_entry = ttk.Entry(dialog)
        title_entry.pack(fill="x", padx=10)

        ttk.Label(dialog, text="Body (optional):").pack(anchor="w", **pad)
        body_box = tk.Text(dialog, wrap="word", height=14)
        body_box.pack(fill="both", expand=True, padx=10)

        ttk.Label(dialog, text="Subreddit (optional - pick a common one or type your own):").pack(anchor="w", **pad)
        sub_entry = ttk.Combobox(dialog, values=COMMON_STORY_SUBREDDITS)
        sub_entry.pack(fill="x", padx=10)

        def use_story():
            title = title_entry.get().strip()
            if not title:
                messagebox.showwarning("Missing title", "Enter at least a title.", parent=dialog)
                return
            story = story_from_text(
                title=title,
                body=body_box.get("1.0", "end").strip(),
                subreddit=sub_entry.get().strip(),
            )
            script = build_script(story)
            dialog.destroy()
            self._preview_loaded(story, script)

        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(fill="x", padx=10, pady=10)
        ttk.Button(btn_frame, text="Use this story", command=use_story).pack(side="right")
        ttk.Button(btn_frame, text="Cancel", command=dialog.destroy).pack(side="right", padx=6)

    def _preview_loaded(self, story, script):
        self.story = story
        self.title_label.config(text=story["title"])
        self.meta_label.config(text=f"r/{story['subreddit']}  •  {len(script)} characters  (~{len(script)//15}s narrated)")
        self.script_box.delete("1.0", "end")
        self.script_box.insert("1.0", script)
        self.output_name.set(self._slugify(story["title"]))
        self._set_busy(False, "Preview loaded. Edit the script if needed, then choose a background and generate.")
        self.generate_btn.config(state="normal")

    def _preview_failed(self, exc):
        self._set_busy(False, "Failed to fetch story.")
        messagebox.showerror(
            "Couldn't fetch story",
            f"{exc}\n\n"
            "If this is a 401/403, you likely need Reddit API credentials in a .env file "
            "(see README.md for the 2-minute setup), or paste the story text manually.",
        )

    # ---------- generation actions ----------
    def on_generate(self):
        script = self.script_box.get("1.0", "end").strip()
        bg_path = self.background_path.get().strip()

        if not script:
            messagebox.showwarning("Missing script", "There's no script text to narrate.")
            return
        if not bg_path or not os.path.exists(bg_path):
            messagebox.showwarning("Missing background", "Choose a valid background video clip first.")
            return

        os.makedirs(OUTPUT_DIR, exist_ok=True)
        name = self._slugify(self.output_name.get().strip() or (self.story["title"] if self.story else "video"))
        self.output_name.set(name)
        audio_path = os.path.join(OUTPUT_DIR, f"{name}.mp3")
        video_path = os.path.join(OUTPUT_DIR, f"{name}.mp4")
        srt_path = os.path.join(OUTPUT_DIR, f"{name}_text.srt")

        part = self.part_number.get()
        continue_from = None
        if (part > 1 and self._last_bg_segment
                and self._last_bg_segment["path"] == bg_path):
            continue_from = self._last_bg_segment["end"]

        self._stop_player()
        self._set_busy(True, "Starting render...")
        self.generate_btn.config(state="disabled")
        self.stop_generate_btn.config(state="normal")
        self.play_btn.config(state="disabled")
        self.stop_btn.config(state="disabled")
        self.player_status.set("Generating your video... it'll load here automatically when ready.")
        self._start_generation_timer(self._estimate_total_seconds(script))

        # Run the whole pipeline in its own process: TTS, transcription, and ffmpeg
        # encoding are all blocking calls that can stall, and a thread can't be killed
        # from the outside - a separate process can, which is what makes Stop possible
        # and guarantees the GUI never gets stuck showing "generating" forever.
        self._gen_paths = (video_path, bg_path, part if part > 1 else None)
        self._gen_queue = mp.Queue()
        self._gen_process = mp.Process(
            target=pipeline_worker,
            args=(script, bg_path, audio_path, video_path, srt_path, self.voice.get(), continue_from, self._gen_queue),
            kwargs={"title": self.story["title"] if self.story else None, "part": part if part > 1 else None},
        )
        self._gen_process.start()
        self.after(150, self._poll_generation)

    def _poll_generation(self):
        if self._gen_process is None:
            return  # Stop already cleaned everything up

        try:
            while True:
                kind, payload = self._gen_queue.get_nowait()
                if kind == "status":
                    self.status.set(payload)
                elif kind == "done":
                    video_path, bg_path, part = self._gen_paths
                    self._generate_done(video_path, bg_path, part, payload)
                    return
                elif kind == "error":
                    self._generate_failed(payload)
                    return
        except queue_module.Empty:
            pass

        if self._gen_process.is_alive():
            self.after(200, self._poll_generation)
        else:
            # Process exited without reporting done/error - it crashed outright
            # (e.g. a native-level crash) rather than raising a catchable exception.
            self._generate_failed("The render process exited unexpectedly (crashed) without an error message.")

    def on_stop_generate(self):
        if self._gen_process and self._gen_process.is_alive():
            self._gen_process.terminate()
            self._gen_process.join()
        self._gen_process = None
        self._gen_queue = None
        self._gen_paths = None
        self._stop_generation_timer(completed=False)
        self._set_busy(False, "Generation stopped.")
        self.generate_btn.config(state="normal")
        self.stop_generate_btn.config(state="disabled")
        self.player_status.set("Generation stopped — nothing to preview.")

    def _generate_done(self, video_path, bg_path, part, result):
        video_paths = result["video_paths"]
        segment_end = result["segment_end"]

        self._last_bg_segment = {"path": bg_path, "end": segment_end}
        self._last_video_paths = video_paths
        self._gen_process = None
        self._gen_queue = None
        self._gen_paths = None
        self._stop_generation_timer(completed=True)
        self.generate_btn.config(state="normal")
        self.stop_generate_btn.config(state="disabled")
        self._load_video_into_player(video_paths[0])

        if len(video_paths) > 1:
            # Auto-split kicked in (story ran past the ~60s threshold): each part is
            # its own MP4, ending (except the last) with a "FOLLOW FOR PART N" banner.
            self._last_part = None
            names = "\n".join(os.path.basename(p) for p in video_paths)
            self._set_busy(False, f"Done! Story auto-split into {len(video_paths)} parts.")
            messagebox.showinfo(
                "Saved as multiple parts",
                f"This story ran long, so it was auto-split into {len(video_paths)} parts, "
                f"each ending with a \"FOLLOW FOR PART N\" call-to-action:\n\n{names}\n\n"
                f"All saved in {OUTPUT_DIR}. Part 1 is loaded in the player — hit "
                "\"Upload to YouTube\" and all parts will be posted automatically, "
                "back to back, each labeled with the story title + its part number.",
            )
        else:
            self._last_part = part
            self._set_busy(False, f"Done! Saved to {video_path}")

        if self.auto_open.get():
            os.startfile(OUTPUT_DIR)

    def _generate_failed(self, error_text):
        self._gen_process = None
        self._gen_queue = None
        self._gen_paths = None
        self._stop_generation_timer(completed=False)
        self._set_busy(False, "Render failed.")
        self.generate_btn.config(state="normal")
        self.stop_generate_btn.config(state="disabled")
        self.player_status.set("Render failed — nothing to preview.")

        log_path = os.path.join(os.path.dirname(__file__), "last_error.log")
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(error_text)
        except OSError:
            pass

        first_line = next((line for line in reversed(error_text.strip().splitlines()) if line.strip()), "Unknown error")
        messagebox.showerror("Render failed", f"{first_line}\n\nFull details written to last_error.log")

    # ---------- embedded player ----------
    def _load_video_into_player(self, video_path):
        self._last_video_path = video_path
        media = self._vlc_instance.media_new(video_path)
        self._player.set_media(media)

        # Hand VLC the native window handle of the canvas frame so it renders inside it.
        self._player.set_hwnd(self.video_canvas.winfo_id())

        self.play_btn.config(state="normal", text="▶ Play")
        self.stop_btn.config(state="normal")
        self.upload_youtube_btn.config(state="normal")
        self.upload_tiktok_btn.config(state="normal")
        self.player_status.set(f"Ready to preview: {os.path.basename(video_path)}")

    def on_play_pause(self):
        if self._player.is_playing():
            self._player.pause()
            self.play_btn.config(text="▶ Play")
        else:
            self._player.play()
            self.play_btn.config(text="⏸ Pause")

    def on_stop(self):
        self._stop_player()

    def _stop_player(self):
        try:
            self._player.stop()
        except Exception:
            pass
        self.play_btn.config(text="▶ Play")

    def on_open_external(self):
        if self._last_video_path and os.path.exists(self._last_video_path):
            os.startfile(self._last_video_path)
        else:
            messagebox.showinfo("No video yet", "Generate a video first, then you can open it in your default player.")

    # ---------- publishing ----------
    def on_upload_youtube(self):
        video_paths = [p for p in self._last_video_paths if p and os.path.exists(p)]
        if not video_paths and self._last_video_path and os.path.exists(self._last_video_path):
            video_paths = [self._last_video_path]
        if not video_paths:
            messagebox.showinfo("No video yet", "Generate a video first, then you can upload it.")
            return

        if not youtube_is_configured():
            messagebox.showerror(
                "YouTube not set up",
                "No YouTube OAuth client secret found.\n\n"
                "Set it up once (see the youtube_upload.py module docstring for the "
                "step-by-step Google Cloud Console instructions), saving the downloaded "
                "JSON as youtube_client_secret.json next to gui.py. Then try again — "
                "you'll get a one-time browser sign-in prompt.",
            )
            return

        base_title = self.story["title"] if self.story else self.output_name.get() or "Reddit Story"
        subreddit = self.story["subreddit"] if self.story else ""
        sub_line = f"r/{subreddit}\n\n" if subreddit else ""

        # A multi-part render (auto-split) posts every part back to back, each
        # labeled "<title> (Part N)" so they read as one continuing series. A
        # single video keeps the old behavior, driven by the manual Part # spinbox.
        if len(video_paths) > 1:
            jobs = []
            for index, path in enumerate(video_paths):
                part_number = index + 1
                is_last = index == len(video_paths) - 1
                part_line = (
                    f"Part {part_number} — the final part of this story.\n\n" if is_last
                    else f"Part {part_number} of this story — more coming soon!\n\n"
                )
                jobs.append((path, part_number, part_line))
        else:
            part_number = self._last_part
            part_line = f"Part {part_number} of this story — more coming soon!\n\n" if part_number else ""
            jobs = [(video_paths[0], part_number, part_line)]

        self.upload_youtube_btn.config(state="disabled")
        self.player_status.set("Signing in to YouTube...")

        def report(message):
            self.after(0, lambda: self.player_status.set(message))

        def work():
            urls = []
            try:
                for index, (path, part_number, part_line) in enumerate(jobs):
                    if len(jobs) > 1:
                        report(f"Uploading part {index + 1} of {len(jobs)}...")

                    part_suffix = f" (Part {part_number})" if part_number else ""
                    title = (base_title + part_suffix)[:100]
                    description = f"{sub_line}{part_line}#shorts #reddit #redditstories #storytime"

                    thumbnail_path = os.path.join(OUTPUT_DIR, f"_thumbnail_{index + 1}.png")
                    build_thumbnail_image(base_title, part=part_number).save(thumbnail_path)

                    urls.append(upload_to_youtube(path, title, description, thumbnail_path=thumbnail_path, on_status=report))
            except Exception as exc:
                self.after(0, lambda: self._upload_failed(exc))
                return
            self.after(0, lambda: self._upload_done(urls))

        threading.Thread(target=work, daemon=True).start()

    def _upload_done(self, urls):
        self.upload_youtube_btn.config(state="normal")
        if len(urls) > 1:
            joined = "\n".join(urls)
            self.player_status.set(f"Uploaded all {len(urls)} parts to YouTube.")
            messagebox.showinfo("Uploaded!", f"All {len(urls)} parts are live on YouTube:\n\n{joined}")
        else:
            self.player_status.set(f"Uploaded to YouTube: {urls[0]}")
            messagebox.showinfo("Uploaded!", f"Your video is live on YouTube:\n{urls[0]}")

    def _upload_failed(self, exc):
        self.upload_youtube_btn.config(state="normal")
        self.player_status.set("YouTube upload failed.")
        if isinstance(exc, YouTubeNotConfigured):
            messagebox.showerror("YouTube not set up", str(exc))
        else:
            messagebox.showerror("Upload failed", str(exc))

    def on_upload_tiktok(self):
        video_paths = [p for p in self._last_video_paths if p and os.path.exists(p)]
        if not video_paths and self._last_video_path and os.path.exists(self._last_video_path):
            video_paths = [self._last_video_path]
        if not video_paths:
            messagebox.showinfo("No video yet", "Generate a video first, then you can upload it.")
            return

        if not tiktok_is_configured():
            messagebox.showerror(
                "TikTok not set up",
                "No TikTok developer app credentials found.\n\n"
                "Set it up once (see the tiktok_upload.py module docstring for the "
                "step-by-step TikTok for Developers instructions), saving the client "
                "key and secret in a .env file next to gui.py. Then try again — "
                "you'll get a one-time browser sign-in prompt.",
            )
            return

        base_title = self.story["title"] if self.story else self.output_name.get() or "Reddit Story"
        subreddit = self.story["subreddit"] if self.story else ""
        sub_line = f"r/{subreddit}\n\n" if subreddit else ""

        # Same "post every part back to back" behavior as the YouTube flow - a
        # multi-part render sends every part to the TikTok inbox in order, each
        # captioned "<title> (Part N)" so they read as one continuing series.
        if len(video_paths) > 1:
            jobs = []
            for index, path in enumerate(video_paths):
                part_number = index + 1
                is_last = index == len(video_paths) - 1
                part_line = (
                    f"Part {part_number} — the final part of this story.\n\n" if is_last
                    else f"Part {part_number} of this story — more coming soon!\n\n"
                )
                jobs.append((path, part_number, part_line))
        else:
            part_number = self._last_part
            part_line = f"Part {part_number} of this story — more coming soon!\n\n" if part_number else ""
            jobs = [(video_paths[0], part_number, part_line)]

        self.upload_tiktok_btn.config(state="disabled")
        self.player_status.set("Signing in to TikTok...")

        def report(message):
            self.after(0, lambda: self.player_status.set(message))

        def work():
            results = []
            try:
                for index, (path, part_number, part_line) in enumerate(jobs):
                    if len(jobs) > 1:
                        report(f"Sending part {index + 1} of {len(jobs)} to TikTok...")

                    part_suffix = f" (Part {part_number})" if part_number else ""
                    title = (base_title + part_suffix)[:150]
                    description = f"{title}\n\n{sub_line}{part_line}#shorts #reddit #redditstories #storytime #fyp"

                    results.append(upload_to_tiktok(path, title, description, on_status=report))
            except Exception as exc:
                self.after(0, lambda: self._tiktok_upload_failed(exc))
                return
            self.after(0, lambda: self._tiktok_upload_done(results))

        threading.Thread(target=work, daemon=True).start()

    def _tiktok_upload_done(self, results):
        self.upload_tiktok_btn.config(state="normal")
        joined = "\n".join(results)
        if len(results) > 1:
            self.player_status.set(f"Sent all {len(results)} parts to TikTok.")
            messagebox.showinfo(
                "Sent to TikTok!",
                f"All {len(results)} parts were sent:\n\n{joined}\n\n"
                "Open the TikTok app to review and post each one (or, once your "
                "app passes TikTok's review, flip on direct posting and this "
                "happens automatically too).",
            )
        else:
            self.player_status.set("Sent to TikTok - check your inbox in the app.")
            messagebox.showinfo("Sent to TikTok!", f"{joined}\n\nOpen the TikTok app to review and post it.")

    def _tiktok_upload_failed(self, exc):
        self.upload_tiktok_btn.config(state="normal")
        self.player_status.set("TikTok upload failed.")
        if isinstance(exc, TikTokNotConfigured):
            messagebox.showerror("TikTok not set up", str(exc))
        else:
            messagebox.showerror("Upload failed", str(exc))

    # ---------- helpers ----------
    def _update_status(self, text):
        self.after(0, lambda: self.status.set(text))

    def _set_busy(self, busy, status_text):
        self.status.set(status_text)
        self.preview_btn.config(state="disabled" if busy else "normal")

    # ---------- generation timer / ETA ----------
    CHARS_PER_SECOND_SPOKEN = 17.5   # roughly how fast edge-tts narrates (measured from test runs)
    SECONDS_OF_PROCESSING_PER_SECOND_OF_AUDIO = 3.1  # captions (GPU) + render combined - measured ~3.1x
    FIXED_OVERHEAD_SECONDS = 10      # voiceover generation + encoding setup

    def _estimate_total_seconds(self, script: str) -> float:
        audio_seconds = max(len(script) / self.CHARS_PER_SECOND_SPOKEN, 5)
        return audio_seconds * self.SECONDS_OF_PROCESSING_PER_SECOND_OF_AUDIO + self.FIXED_OVERHEAD_SECONDS

    @staticmethod
    def _format_mmss(seconds: float) -> str:
        seconds = max(0, int(seconds))
        return f"{seconds // 60}:{seconds % 60:02d}"

    def _start_generation_timer(self, estimated_total_seconds: float):
        self._gen_start = time.monotonic()
        self._gen_estimate = estimated_total_seconds
        self.progress.config(value=0)
        self._tick()

    def _tick(self):
        if self._gen_start is None:
            return
        elapsed = time.monotonic() - self._gen_start
        estimate = self._gen_estimate or 1
        # Cap displayed progress at 95% until the job actually finishes, since the
        # estimate is approximate and we don't want to show 100% prematurely.
        pct = min(elapsed / estimate * 100, 95)
        self.progress.config(value=pct)
        self.timer_text.set(f"Elapsed {self._format_mmss(elapsed)} / ~{self._format_mmss(estimate)} ETA")
        self._timer_job = self.after(1000, self._tick)

    def _stop_generation_timer(self, completed: bool):
        if self._timer_job is not None:
            self.after_cancel(self._timer_job)
            self._timer_job = None
        if completed and self._gen_start is not None:
            elapsed = time.monotonic() - self._gen_start
            self.progress.config(value=100)
            self.timer_text.set(f"Finished in {self._format_mmss(elapsed)}")
        else:
            self.progress.config(value=0)
            self.timer_text.set("")
        self._gen_start = None
        self._gen_estimate = None

    @staticmethod
    def _slugify(text, max_len=50):
        safe = "".join(c if c.isalnum() or c in " -_" else "" for c in text)
        safe = "_".join(safe.split())
        return safe[:max_len] or "video"

    def destroy(self):
        self._stop_player()
        if self._gen_process and self._gen_process.is_alive():
            self._gen_process.terminate()
            self._gen_process.join()
        super().destroy()


if __name__ == "__main__":
    app = VideoMakerApp()
    app.mainloop()
