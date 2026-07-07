"""
Video render engine — refactored from make_videos.py.
GUI gọi VideoEngine(config, log_cb, progress_cb).run().
"""

import csv
import random
import subprocess
import shutil
import tempfile
from pathlib import Path
from collections import defaultdict
from datetime import datetime


IMAGE_EXTENSIONS = [".png", ".jpg", ".jpeg", ".webp", ".bmp", ".heic", ".heif"]
VIDEO_EXTENSIONS = [".mp4", ".mov", ".m4v", ".mkv", ".webm"]
AUDIO_EXTENSIONS = [".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"]


DEFAULT_CONFIG = {
    # Counts / sizes
    "TOTAL_VIDEOS": 10,
    "IMAGES_PER_VIDEO": 3,
    "IMAGE_DURATION": 1.5,
    "FADE_DURATION": 0.15,
    "WIDTH": 1080,
    "HEIGHT": 1920,
    "FPS": 30,

    # Scale
    "IMAGE_SCALE_MODE": "fit_blur_bg",   # fill | fit | fit_blur_bg
    "IMAGE_SCALE_PERCENT": 120,
    "FIT_BACKGROUND_COLOR": "black",
    "BLUR_BACKGROUND_STRENGTH": 8,
    "BLUR_BACKGROUND_BRIGHTNESS": -0.08,
    "BLUR_BACKGROUND_SATURATION": 1.1,

    # Audio
    "MUSIC_START_SECONDS": 71,
    "VOICE_START_SECONDS": 0,
    "MUSIC_VOLUME": 0.4,
    "VOICE_VOLUME": 1.5,

    # Combo rule
    "MAX_SAME_IMAGE_PER_POSITION_PER_BATCH": 3,
    "RANDOM_SEED": None,

    # Cleanup
    "AUTO_CLEAN_TEMP": True,   # Xoá _temp sau khi render thành công 100%

    # Performance (Apple Silicon)
    "FFMPEG_THREADS": 0,        # 0 = auto; đặt 4-6 để giảm heat, chậm hơn chút
    "USE_HW_ENCODER": True,     # Dùng VideoToolbox trên Apple Silicon (giảm nhiệt nhiều)
}


def collect_files_from_folder(folder, extensions):
    folder = Path(folder)
    if not folder.exists():
        return []
    files = []
    for ext in extensions:
        files.extend(folder.glob(f"*{ext}"))
        files.extend(folder.glob(f"*{ext.upper()}"))
    return sorted(set(files))


def sort_images_smart(image_files):
    def key(p):
        if p.stem.isdigit():
            return (0, int(p.stem), p.name.lower())
        return (1, p.name.lower())
    return sorted(image_files, key=key)


class VideoEngine:
    def __init__(self, config, image_paths, fixed_video, music_file, voice_file,
                 overlay_video, output_dir, history_csv,
                 log_cb=None, progress_cb=None, ffmpeg_path="ffmpeg", ffprobe_path="ffprobe",
                 stop_flag=None, batch_name_prefix="", project_name=""):
        self.cfg = {**DEFAULT_CONFIG, **(config or {})}
        self.images = [Path(p) for p in image_paths]
        self.fixed_video = Path(fixed_video) if fixed_video else None
        self.music_file = Path(music_file)
        self.voice_file = Path(voice_file) if voice_file else None
        self.overlay_video = Path(overlay_video) if overlay_video else None
        self.output_root = Path(output_dir)
        ts = datetime.now().strftime('%y%m%d_%H%M%S')
        if batch_name_prefix:
            prefix = "".join(c if c.isalnum() or c in "-_" else "_"
                             for c in batch_name_prefix).strip("_")
            self.batch_id = prefix or ts
        else:
            self.batch_id = ts
        self.file_prefix = "".join(c if c.isalnum() or c in "-_" else "_"
                                   for c in (project_name or "video")).strip("_") or "video"
        self.batch_dir = self.output_root / self.batch_id
        self.output_dir = self.batch_dir  # alias for backward compat
        self.temp_dir = self.batch_dir / "_temp"
        self.history_csv = Path(history_csv)
        self.log_cb = log_cb or (lambda msg: None)
        self.progress_cb = progress_cb or (lambda done, total, label="": None)
        self.ffmpeg = ffmpeg_path
        self.ffprobe = ffprobe_path
        self.stop_flag = stop_flag or (lambda: False)
        self._has_videotoolbox = self._detect_videotoolbox() if self.cfg.get("USE_HW_ENCODER", True) else False
        self._dims_cache: dict[str, tuple[int, int]] = {}

        # Chỉ tạo output_root ở init. batch_dir/temp_dir sẽ được tạo
        # khi thực sự render (run()) để tránh đẻ folder rác khi
        # chỉ gọi capacity_summary().
        self.output_root.mkdir(parents=True, exist_ok=True)

        self._migrate_history_if_needed()

    def _migrate_history_if_needed(self):
        """Đổi history.csv format cũ (Image_1/2/3) → mới (Images)."""
        if not self.history_csv.exists():
            return
        try:
            with open(self.history_csv, newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                fields = list(reader.fieldnames or [])
                if "Images" in fields:
                    return  # đã là format mới
                if not any(f.startswith("Image_") for f in fields):
                    return  # file rỗng/không hợp lệ, kệ nó
                rows = list(reader)
        except Exception:
            return

        new_rows = []
        for row in rows:
            names = []
            for i in range(1, 100):
                v = (row.get(f"Image_{i}") or "").strip()
                if v:
                    names.append(v)
            if not names:
                continue
            new_rows.append({
                "Batch_ID": row.get("Batch_ID", ""),
                "Video_ID": row.get("Video_ID", ""),
                "Images": ";".join(names),
                "Rendered_At": row.get("Rendered_At", ""),
            })

        backup = self.history_csv.with_suffix(".csv.bak")
        try:
            shutil.copy2(self.history_csv, backup)
        except Exception:
            pass
        with open(self.history_csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["Batch_ID", "Video_ID", "Images", "Rendered_At"])
            w.writeheader()
            for r in new_rows:
                w.writerow(r)
        self.log_cb(f"History migrated: {len(new_rows)} rows → format mới (backup: {backup.name})")

    # ---------------- image dimension helpers ----------------

    def _get_image_dims(self, img: Path) -> tuple[int, int]:
        key = str(img)
        if key not in self._dims_cache:
            r = subprocess.run(
                [self.ffprobe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", str(img)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"
            )
            parts = r.stdout.strip().split(",")
            self._dims_cache[key] = (int(parts[0]), int(parts[1])) if len(parts) == 2 else (0, 0)
        return self._dims_cache[key]

    def _is_target_ratio(self, w: int, h: int) -> bool:
        if h == 0:
            return False
        target = self.cfg["WIDTH"] / self.cfg["HEIGHT"]
        actual = w / h
        return abs(actual - target) / target < 0.02  # ±2% tolerance

    # ---------------- hardware detection ----------------

    def _detect_videotoolbox(self):
        try:
            r = subprocess.run(
                [self.ffmpeg, "-hide_banner", "-encoders"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            return "h264_videotoolbox" in r.stdout
        except Exception:
            return False

    def _vcodec_temp(self):
        """Codec nhanh cho file trung gian — giảm CPU encoding time."""
        return ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p"]

    def _vcodec_output(self):
        """Codec chất lượng cho output cuối — dùng hardware nếu có."""
        if self._has_videotoolbox:
            return ["-c:v", "h264_videotoolbox", "-q:v", "45", "-pix_fmt", "nv12"]
        return ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p"]

    def _thread_opts(self):
        t = int(self.cfg.get("FFMPEG_THREADS", 0))
        return ["-threads", str(t)] if t > 0 else []

    # ---------------- low level ----------------

    def _log(self, msg):
        self.log_cb(msg)

    def _run(self, cmd):
        if self.stop_flag():
            raise RuntimeError("Stopped by user")
        thread_opts = self._thread_opts()
        if thread_opts:
            cmd = [cmd[0]] + thread_opts + list(cmd[1:])
        self._log("$ " + " ".join(f'"{x}"' if " " in str(x) else str(x) for x in cmd))
        # On Windows, hide subprocess console
        startupinfo = None
        try:
            import subprocess as sp
            si = sp.STARTUPINFO()
            si.dwFlags |= sp.STARTF_USESHOWWINDOW
            startupinfo = si
        except Exception:
            pass
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", startupinfo=startupinfo
        )
        if proc.returncode != 0:
            self._log(proc.stdout or "")
            raise RuntimeError(f"FFmpeg failed (code {proc.returncode})")

    def _ffprobe_duration(self, path):
        cmd = [
            self.ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path)
        ]
        startupinfo = None
        try:
            import subprocess as sp
            si = sp.STARTUPINFO()
            si.dwFlags |= sp.STARTF_USESHOWWINDOW
            startupinfo = si
        except Exception:
            pass
        result = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", check=True, startupinfo=startupinfo
        )
        return float(result.stdout.strip())

    # ---------------- combo / history ----------------

    def _load_history(self):
        """
        Đọc history. Hỗ trợ 2 format:
        - Mới: cột "Images" với nhiều tên ảnh nối bằng ';'
        - Cũ:  cột "Image_1", "Image_2", "Image_3" (hardcode 3 ảnh)
        """
        used = set()
        if not self.history_csv.exists():
            return used
        with open(self.history_csv, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if row.get("Images"):
                    names = tuple(n.strip() for n in row["Images"].split(";") if n.strip())
                    if names:
                        used.add(names)
                else:
                    names = []
                    for i in range(1, 100):
                        v = row.get(f"Image_{i}", "")
                        if v:
                            names.append(v.strip())
                        else:
                            break
                    if names:
                        used.add(tuple(names))
        return used

    def _filter_history(self, history_set):
        """Giữ lại combo có đúng IMAGES_PER_VIDEO ảnh và đều thuộc set hiện tại."""
        names = {img.name for img in self.images}
        ipv = self.cfg["IMAGES_PER_VIDEO"]
        out = set()
        for combo in history_set:
            if len(combo) == ipv and len(set(combo)) == ipv \
                    and all(n in names for n in combo):
                out.add(combo)
        return out

    def _append_history(self, batch_id, video_id, combo):
        """Ghi history dùng cột 'Images' động (ngăn cách bằng ;)."""
        exists = self.history_csv.exists()
        self.history_csv.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_csv, "a", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=[
                "Batch_ID", "Video_ID", "Images", "Rendered_At"
            ], extrasaction="ignore")
            if not exists:
                w.writeheader()
            w.writerow({
                "Batch_ID": batch_id,
                "Video_ID": video_id,
                "Images": ";".join(p.name for p in combo),
                "Rendered_At": datetime.now().strftime("%y%m%d_%H%M%S"),
            })

    def _generate_combos(self, total, historical):
        ipv = self.cfg["IMAGES_PER_VIDEO"]
        max_pos = self.cfg["MAX_SAME_IMAGE_PER_POSITION_PER_BATCH"]
        position_usage = {i: defaultdict(int) for i in range(ipv)}
        combos = []
        current_batch_used = set()
        for vi in range(total):
            done = False
            for _ in range(20000):
                selected = []
                for pos in range(ipv):
                    candidates = [
                        img for img in self.images
                        if position_usage[pos][img.name] < max_pos and img not in selected
                    ]
                    if not candidates:
                        break
                    selected.append(random.choice(candidates))
                if len(selected) != ipv:
                    continue
                key = tuple(s.name for s in selected)
                if key in historical or key in current_batch_used:
                    continue
                for pos, img in enumerate(selected):
                    position_usage[pos][img.name] += 1
                current_batch_used.add(key)
                combos.append(selected)
                done = True
                break
            if not done:
                raise RuntimeError(
                    f"Không tạo đủ combo cho video {vi+1}. "
                    f"Giảm TOTAL_VIDEOS hoặc tăng số ảnh / nới constraint."
                )
        return combos

    # ---------------- filters ----------------

    def _image_scale_filter(self):
        W, H, FPS = self.cfg["WIDTH"], self.cfg["HEIGHT"], self.cfg["FPS"]
        mode = self.cfg["IMAGE_SCALE_MODE"]
        sf = self.cfg["IMAGE_SCALE_PERCENT"] / 100.0
        sw, sh = int(W * sf), int(H * sf)

        if mode == "fill":
            return (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H},fps={FPS},format=yuv420p")
        if mode == "fit":
            return (f"scale={sw}:{sh}:force_original_aspect_ratio=decrease,"
                    f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={self.cfg['FIT_BACKGROUND_COLOR']},"
                    f"fps={FPS},format=yuv420p")
        if mode == "fit_blur_bg":
            return (
                f"split=2[bg][fg];"
                f"[bg]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},"
                f"boxblur={self.cfg['BLUR_BACKGROUND_STRENGTH']}:1,"
                f"eq=brightness={self.cfg['BLUR_BACKGROUND_BRIGHTNESS']}:"
                f"saturation={self.cfg['BLUR_BACKGROUND_SATURATION']}[bgout];"
                f"[fg]scale={sw}:{sh}:force_original_aspect_ratio=decrease[fgout];"
                f"[bgout][fgout]overlay=(W-w)/2:(H-h)/2,fps={FPS},format=yuv420p"
            )
        raise ValueError(f"IMAGE_SCALE_MODE không hợp lệ: {mode}")

    def _normalize_image(self, img, out_path):
        W, H, FPS = self.cfg["WIDTH"], self.cfg["HEIGHT"], self.cfg["FPS"]
        if self.cfg["IMAGE_SCALE_MODE"] == "fit_blur_bg":
            w, h = self._get_image_dims(img)
            if self._is_target_ratio(w, h):
                # Ảnh đã đúng tỉ lệ → fill đơn giản, bỏ qua blur pipeline
                vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                      f"crop={W}:{H},fps={FPS},format=yuv420p")
            else:
                vf = self._image_scale_filter()
        else:
            vf = self._image_scale_filter()
        self._run([
            self.ffmpeg, "-y",
            "-loop", "1", "-t", str(self.cfg["IMAGE_DURATION"]),
            "-i", str(img),
            "-vf", vf, "-an", "-r", str(self.cfg["FPS"]),
            *self._vcodec_temp(),
            str(out_path)
        ])

    def _normalize_fixed_video(self, src, out_path):
        W, H, FPS = self.cfg["WIDTH"], self.cfg["HEIGHT"], self.cfg["FPS"]
        vf = (f"scale={W}:{H}:force_original_aspect_ratio=increase,"
              f"crop={W}:{H},fps={FPS},format=yuv420p")
        self._run([
            self.ffmpeg, "-y", "-i", str(src), "-vf", vf, "-an", "-r", str(FPS),
            *self._vcodec_temp(),
            str(out_path)
        ])

    def _make_visual(self, clips, fixed_clip, out_path):
        """
        Build xfade chain dong cho N anh + 1 fixed video (optional).
        fixed_clip=None → chỉ xfade ảnh với nhau.
        """
        ID = self.cfg["IMAGE_DURATION"]
        FD = self.cfg["FADE_DURATION"]

        all_inputs = list(clips) + ([fixed_clip] if fixed_clip else [])
        N = len(clips) if fixed_clip else len(clips) - 1

        parts = []
        for k in range(N):
            offset = (k + 1) * ID - (k + 1) * FD
            in1 = "[0:v]" if k == 0 else f"[v{k}]"
            in2 = f"[{k+1}:v]"
            if k == N - 1:
                parts.append(
                    f"{in1}{in2}xfade=transition=fade:duration={FD}:offset={offset},"
                    f"format=yuv420p[vout]"
                )
            else:
                parts.append(
                    f"{in1}{in2}xfade=transition=fade:duration={FD}:offset={offset}[v{k+1}]"
                )
        fc = ";".join(parts)

        cmd = [self.ffmpeg, "-y"]
        for inp in all_inputs:
            cmd += ["-i", str(inp)]
        cmd += [
            "-filter_complex", fc,
            "-map", "[vout]", "-an",
            "-r", str(self.cfg["FPS"]),
            *self._vcodec_temp(),
            str(out_path),
        ]
        self._run(cmd)

    def _apply_overlay(self, base, overlay, out_path):
        W, H, FPS = self.cfg["WIDTH"], self.cfg["HEIGHT"], self.cfg["FPS"]
        if overlay is None:
            # Re-encode với output codec (step này là bước encode cuối trước audio)
            self._run([
                self.ffmpeg, "-y", "-i", str(base),
                *self._vcodec_output(),
                str(out_path)
            ])
            return
        dur = self._ffprobe_duration(base)
        fc = (
            f"[1:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
            f"crop={W}:{H},fps={FPS},"
            f"trim=duration={dur},setpts=PTS-STARTPTS,"
            f"colorkey=0x000000:0.16:0.06,format=rgba,"
            f"lutrgb=r=255:g=255:b=255[ov];"
            f"[0:v][ov]overlay=0:0:format=auto,format=yuv420p[vout]"
        )
        self._run([
            self.ffmpeg, "-y", "-i", str(base),
            "-stream_loop", "-1", "-i", str(overlay),
            "-filter_complex", fc, "-map", "[vout]", "-an",
            "-t", str(dur), "-r", str(FPS),
            *self._vcodec_output(),
            str(out_path)
        ])

    def _add_audio(self, video, out_path):
        dur = self._ffprobe_duration(video)
        has_voice = self.voice_file and self.voice_file.exists()
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".m4a", prefix="vidauto_audio_")
        import os; os.close(tmp_fd)
        tmp_audio = Path(tmp_path)
        try:
            if has_voice:
                delay_ms = int(self.cfg["VOICE_START_SECONDS"] * 1000)
                fc = (
                    f"[0:a]volume={self.cfg['MUSIC_VOLUME']},"
                    f"asetpts=PTS-STARTPTS,atrim=duration={dur},"
                    f"apad=whole_dur={dur}[music];"
                    f"[1:a]volume={self.cfg['VOICE_VOLUME']},"
                    f"adelay={delay_ms}|{delay_ms},"
                    f"asetpts=PTS-STARTPTS,atrim=duration={dur},"
                    f"apad=whole_dur={dur}[voice];"
                    f"[music][voice]amix=inputs=2:duration=longest"
                    f":dropout_transition=2:normalize=false[aout]"
                )
                self._run([
                    self.ffmpeg, "-y",
                    "-ss", str(self.cfg["MUSIC_START_SECONDS"]), "-i", str(self.music_file),
                    "-i", str(self.voice_file),
                    "-filter_complex", fc,
                    "-map", "[aout]",
                    "-c:a", "aac", "-t", str(dur),
                    str(tmp_audio)
                ])
            else:
                fc = (
                    f"[0:a]volume={self.cfg['MUSIC_VOLUME']},"
                    f"asetpts=PTS-STARTPTS,atrim=duration={dur},"
                    f"apad=whole_dur={dur}[aout]"
                )
                self._run([
                    self.ffmpeg, "-y",
                    "-ss", str(self.cfg["MUSIC_START_SECONDS"]), "-i", str(self.music_file),
                    "-filter_complex", fc,
                    "-map", "[aout]",
                    "-c:a", "aac", "-t", str(dur),
                    str(tmp_audio)
                ])
            # Bước 2: mux video + audio
            self._run([
                self.ffmpeg, "-y",
                "-i", str(video),
                "-i", str(tmp_audio),
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy", "-c:a", "copy",
                str(out_path)
            ])
        finally:
            try:
                tmp_audio.unlink()
            except Exception:
                pass

    # ---------------- public ----------------

    @staticmethod
    def _ordered_perm(n, k):
        """Số permutation ordered chọn k phần tử khác nhau từ n: n*(n-1)*...*(n-k+1)."""
        if n < k or k <= 0:
            return 0
        result = 1
        for i in range(k):
            result *= (n - i)
        return result

    def capacity_summary(self):
        n = len(self.images)
        ipv = self.cfg["IMAGES_PER_VIDEO"]
        total_ordered = self._ordered_perm(n, ipv)
        hist = self._filter_history(self._load_history())
        remaining = max(0, total_ordered - len(hist))
        max_by_pos = n * self.cfg["MAX_SAME_IMAGE_PER_POSITION_PER_BATCH"]
        return {
            "image_count": n,
            "total_ordered": total_ordered,
            "history_used": len(hist),
            "remaining": remaining,
            "max_by_position": max_by_pos,
            "effective_max": min(max_by_pos, remaining),
        }

    def run(self):
        if self.cfg["RANDOM_SEED"] is not None:
            random.seed(self.cfg["RANDOM_SEED"])

        if len(self.images) < self.cfg["IMAGES_PER_VIDEO"]:
            raise RuntimeError(
                f"Cần ít nhất {self.cfg['IMAGES_PER_VIDEO']} ảnh, đang có {len(self.images)}."
            )

        stats = self.capacity_summary()
        total = self.cfg["TOTAL_VIDEOS"]
        if total > stats["effective_max"]:
            raise RuntimeError(
                f"TOTAL_VIDEOS={total} quá cao. Tối đa render được {stats['effective_max']} "
                f"(combo còn lại sau history + giới hạn vị trí)."
            )

        # Tạo batch_dir và temp_dir ngay trước khi render (không tạo ở __init__).
        self.batch_dir.mkdir(parents=True, exist_ok=True)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        has_fixed = self.fixed_video and self.fixed_video.exists()

        self._log(f"=== BATCH START | id={self.batch_id} | "
                  f"images={stats['image_count']} | target={total} | "
                  f"mode={self.cfg['IMAGE_SCALE_MODE']} ===")
        self._log(f"Output → {self.batch_dir}")

        historical = self._filter_history(self._load_history())
        combos = self._generate_combos(total, historical)
        for i, c in enumerate(combos, 1):
            names = ", ".join(p.name for p in c)
            self._log(f"video_{i:03d}: {names}")

        batch_id = self.batch_id
        fixed_norm = None
        if has_fixed:
            fixed_norm = self.temp_dir / "fixed_normalized.mp4"
            self._normalize_fixed_video(self.fixed_video, fixed_norm)

        ipv = self.cfg["IMAGES_PER_VIDEO"]
        # mỗi video: N normalize ảnh + 1 visual + 1 overlay + 1 audio
        total_steps = len(combos) * (ipv + 3)
        step = 0

        rendered = 0
        for idx, combo in enumerate(combos, 1):
            if self.stop_flag():
                self._log("Đã dừng bởi người dùng.")
                break

            vid = f"{self.file_prefix}_{idx:03d}"
            self._log(f"\n--- Rendering {vid} ({idx}/{len(combos)}) ---")
            clips = []
            for i, img in enumerate(combo, 1):
                clip_path = self.temp_dir / f"{vid}_image_{i}.mp4"
                self._normalize_image(img, clip_path)
                clips.append(clip_path)
                step += 1
                self.progress_cb(step, total_steps, f"{vid} image {i}/{ipv}")

            visual = self.temp_dir / f"{vid}_visual.mp4"
            self._make_visual(clips, fixed_norm, visual)  # fixed_norm=None nếu không có fixed video
            step += 1
            self.progress_cb(step, total_steps, f"{vid} visual")

            with_overlay = self.temp_dir / f"{vid}_overlay.mp4"
            self._apply_overlay(visual, self.overlay_video, with_overlay)
            step += 1
            self.progress_cb(step, total_steps, f"{vid} overlay")

            final = self.output_dir / f"{vid}.mp4"
            self._add_audio(with_overlay, final)
            step += 1
            self.progress_cb(step, total_steps, f"{vid} audio → DONE")

            self._append_history(batch_id, vid, combo)
            rendered += 1
            self._log(f"✓ Exported: {final}")

        self._log(f"\n=== DONE. Rendered {rendered}/{len(combos)} videos in {self.output_dir} ===")

        # Dọn _temp: chỉ khi (a) bật setting, (b) không bị stop, (c) render đủ
        full_success = (rendered == len(combos)) and not self.stop_flag()
        if self.cfg.get("AUTO_CLEAN_TEMP", True) and full_success:
            import time
            for attempt in range(5):
                try:
                    if self.temp_dir.exists():
                        shutil.rmtree(self.temp_dir)
                    self._log(f"✓ Đã dọn _temp ({self.temp_dir})")
                    break
                except Exception as e:
                    if attempt < 4:
                        time.sleep(1)
                    else:
                        self._log(f"(không dọn được _temp: {e})")
        elif not full_success:
            self._log(f"(giữ _temp lại để debug — render chưa thành công 100%)")
        else:
            self._log(f"(giữ _temp do tắt AUTO_CLEAN_TEMP)")

        return rendered
