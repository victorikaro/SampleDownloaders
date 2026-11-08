#!/usr/bin/env python3
"""
Baixador de MP3 / MP4 do YouTube (interface gráfica)

Rodar direto:  pip install -U yt-dlp mutagen pillow   ->   python baixador_youtube.py
Precisa do ffmpeg (no PATH ou ffmpeg.exe ao lado do programa).
Para virar .exe com atualização automática, veja launcher.py e build_exe.bat.
"""
APP_VERSION = "1.1.0"

import io
import json
import os
import queue
import re
import shutil
import sys
import threading
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled, sanitize_filename
    from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, ID3NoHeaderError
    from mutagen.mp4 import MP4, MP4Cover
    from PIL import Image, ImageTk
except ImportError:
    sys.exit("Faltam dependências. Rode: pip install -U yt-dlp mutagen pillow")

PW, PH = 320, 180  # tamanho da prévia
HOME = os.path.join(os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config"), "BaixadorYT")
CFG = os.path.join(HOME, "config.json")


# ---------- configurações salvas ----------
def load_cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(d):
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(CFG, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ---------- capa ----------
def thumb_urls(thumbs):
    big = sorted(thumbs or [], key=lambda t: (t.get("width") or 0) * (t.get("height") or 0), reverse=True)
    return [t["url"] for t in big if t.get("url")]


def video_cover_urls(vid, thumbs=()):
    return [f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
            f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"] + thumb_urls(thumbs)


def fetch_cover(urls):
    """Tenta cada URL até achar uma imagem JPEG/PNG válida (ignora webp)."""
    for u in urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
            data = urllib.request.urlopen(req, timeout=15).read()
        except Exception:
            continue
        if data[:3] == b"\xff\xd8\xff":
            return data, "image/jpeg"
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return data, "image/png"
    return None


def crop_box(size, crop):
    w, h = size
    if crop["mode"] == "orig":
        return (0, 0, w, h)
    side = max(8, int(min(w, h) * crop["s"]))
    left = min(max(int(crop["cx"] * w - side / 2), 0), w - side)
    top = min(max(int(crop["cy"] * h - side / 2), 0), h - side)
    return (left, top, left + side, top + side)


def cover_jpeg(img, crop):
    img = img.crop(crop_box(img.size, crop))
    if max(img.size) > 1000:
        img.thumbnail((1000, 1000))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=92)
    return buf.getvalue(), "image/jpeg"


# ---------- YouTube / metadados ----------
def probe(url, single):
    """Lê o link sem baixar. Retorna (é_playlist, álbum, entradas, urls_da_capa, info)."""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "noplaylist": single}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=False)
    if info.get("_type") == "playlist":
        entries = [e for e in (info.get("entries") or []) if e]
        urls = thumb_urls(info.get("thumbnails"))
        if entries:
            urls += video_cover_urls(entries[0].get("id"))
        return True, info.get("title") or "Playlist", entries, urls, info
    urls = video_cover_urls(info.get("id"), info.get("thumbnails"))
    return False, None, [{"url": url, "id": info.get("id"), "title": info.get("title")}], urls, info


def build_meta(info, album, idx, total):
    title = info.get("track") or info.get("title") or ""
    artist = info.get("artist") or info.get("creator")
    if isinstance(artist, (list, tuple)):
        artist = ", ".join(artist)
    if not artist:
        if " - " in title:  # "Artista - Música"
            artist, title = [s.strip() for s in title.split(" - ", 1)]
        else:
            artist = info.get("channel") or info.get("uploader") or ""
    artist = re.sub(r"\s*-\s*Topic$", "", artist)
    return {"title": title, "artist": artist, "album": album or info.get("album") or title,
            "year": (info.get("upload_date") or "")[:4], "idx": idx, "total": total}


def tag_file(path, fmt, m, cover):
    data, mime = cover if cover else (None, None)
    if fmt == "MP3":
        try:
            t = ID3(path)
        except ID3NoHeaderError:
            t = ID3()
        t.add(TIT2(encoding=3, text=m["title"]))
        t.add(TPE1(encoding=3, text=m["artist"]))
        t.add(TPE2(encoding=3, text=m["artist"]))
        t.add(TALB(encoding=3, text=m["album"]))
        if m["idx"]:
            t.add(TRCK(encoding=3, text=f'{m["idx"]}/{m["total"]}'))
        if m["year"]:
            t.add(TDRC(encoding=3, text=m["year"]))
        if data:
            t.add(APIC(encoding=3, mime=mime, type=3, desc="Capa", data=data))
        t.save(path, v2_version=3)
    else:
        f = MP4(path)
        f["\xa9nam"], f["\xa9ART"], f["aART"] = [m["title"]], [m["artist"]], [m["artist"]]
        f["\xa9alb"] = [m["album"]]
        if m["year"]:
            f["\xa9day"] = [m["year"]]
        if m["idx"]:
            f["trkn"] = [(m["idx"], m["total"])]
        if data:
            fi = MP4Cover.FORMAT_JPEG if mime == "image/jpeg" else MP4Cover.FORMAT_PNG
            f["covr"] = [MP4Cover(data, imageformat=fi)]
        f.save()


class QuietLogger:
    def __init__(self, q):
        self.q = q

    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass

    def error(self, msg):
        self.q.put(("log", str(msg)))


# ---------- aplicativo ----------
class App:
    def __init__(self, root):
        self.root = root
        root.title(f"Baixador YouTube – MP3 / MP4  (v{APP_VERSION})")
        root.geometry("800x660")
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.cx = self.cy = 0.5
        self.pv_url = self.pv_img = self.pv_tk = self._after = None

        s = load_cfg()
        self.url = tk.StringVar()
        self.fmt = tk.StringVar(value=s.get("fmt", "MP3"))
        self.quality = tk.StringVar(value=s.get("quality", "192"))
        self.mode = tk.StringVar(value=s.get("mode", "fast"))
        self.single = tk.BooleanVar(value=s.get("single", False))
        self.crop_mode = tk.StringVar(value=s.get("crop_mode", "square"))
        self.zoom = tk.DoubleVar(value=1.0)
        self.outdir = tk.StringVar(value=s.get("outdir") or os.path.join(os.path.expanduser("~"), "Downloads"))
        self.status = tk.StringVar(value=os.environ.get("BYT_NOTE") or "Pronto.")

        p = ttk.Frame(root, padding=14)
        p.pack(fill="both", expand=True)

        ttk.Label(p, text="Link do vídeo ou playlist:").pack(anchor="w")
        row = ttk.Frame(p); row.pack(fill="x", pady=(2, 10))
        ttk.Entry(row, textvariable=self.url).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Colar", command=self.paste).pack(side="left", padx=(6, 0))

        mid = ttk.Frame(p); mid.pack(fill="x")
        left = ttk.Frame(mid); left.pack(side="left", fill="both", expand=True)

        f1 = ttk.LabelFrame(left, text="Formato", padding=8); f1.pack(fill="x")
        for t in ("MP3", "MP4"):
            ttk.Radiobutton(f1, text=t, value=t, variable=self.fmt, command=self.on_fmt).pack(anchor="w")
        ttk.Label(f1, text="Qualidade (kbps / altura):").pack(anchor="w", pady=(6, 0))
        self.cb = ttk.Combobox(f1, textvariable=self.quality, state="readonly", width=10)
        self.cb.pack(anchor="w")
        self.set_quality_values(keep=True)

        f2 = ttk.LabelFrame(left, text="Modo", padding=8); f2.pack(fill="x", pady=(8, 0))
        ttk.Radiobutton(f2, text="Rápido (sem metadados)", value="fast", variable=self.mode).pack(anchor="w")
        ttk.Radiobutton(f2, text="Completo (metadados + capa)", value="full", variable=self.mode).pack(anchor="w")
        ttk.Checkbutton(f2, text="Só o vídeo (ignorar playlist do link)", variable=self.single).pack(anchor="w", pady=(6, 0))

        right = ttk.LabelFrame(mid, text="Capa (usada no modo Completo)", padding=8)
        right.pack(side="left", padx=(10, 0))
        self.canvas = tk.Canvas(right, width=PW, height=PH, bg="#222", highlightthickness=0)
        self.canvas.pack()
        self.canvas.bind("<Button-1>", self.on_drag)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.pv_title = ttk.Label(right, text="", wraplength=PW); self.pv_title.pack(anchor="w", pady=(4, 0))
        r = ttk.Frame(right); r.pack(fill="x")
        ttk.Radiobutton(r, text="Quadrado (1:1)", value="square", variable=self.crop_mode, command=self.draw).pack(side="left")
        ttk.Radiobutton(r, text="Original", value="orig", variable=self.crop_mode, command=self.draw).pack(side="left", padx=8)
        r = ttk.Frame(right); r.pack(fill="x")
        ttk.Label(r, text="Tamanho do corte:").pack(side="left")
        ttk.Scale(r, from_=0.4, to=1.0, variable=self.zoom, command=lambda _v: self.draw()).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(right, text="Arraste sobre a imagem para posicionar o corte.", foreground="#666").pack(anchor="w")

        ttk.Label(p, text="Salvar em:").pack(anchor="w", pady=(10, 0))
        row = ttk.Frame(p); row.pack(fill="x", pady=2)
        ttk.Entry(row, textvariable=self.outdir).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Escolher…", command=self.pick_dir).pack(side="left", padx=(6, 0))

        row = ttk.Frame(p); row.pack(fill="x", pady=10)
        self.btn_go = ttk.Button(row, text="Baixar", command=self.start); self.btn_go.pack(side="left")
        self.btn_stop = ttk.Button(row, text="Cancelar", command=self.cancel.set, state="disabled")
        self.btn_stop.pack(side="left", padx=6)

        self.bar = ttk.Progressbar(p, maximum=100); self.bar.pack(fill="x")
        ttk.Label(p, textvariable=self.status).pack(anchor="w", pady=(4, 6))
        self.log = tk.Text(p, height=8, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)

        self.url.trace_add("write", self.url_changed)
        self.single.trace_add("write", lambda *_: (setattr(self, "pv_url", None), self.url_changed()))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.draw()
        root.after(100, self.poll)

    # --- configurações ---
    def save_settings(self):
        save_cfg({"outdir": self.outdir.get(), "fmt": self.fmt.get(), "quality": self.quality.get(),
                  "mode": self.mode.get(), "single": self.single.get(), "crop_mode": self.crop_mode.get()})

    def on_close(self):
        self.save_settings()
        self.root.destroy()

    def set_quality_values(self, keep=False):
        mp3 = self.fmt.get() == "MP3"
        vals = ("128", "192", "256", "320") if mp3 else ("Melhor", "1080", "720", "480", "360")
        self.cb["values"] = vals
        if not (keep and self.quality.get() in vals):
            self.quality.set("192" if mp3 else "Melhor")

    def on_fmt(self):
        self.set_quality_values()

    def paste(self):
        try:
            self.url.set(self.root.clipboard_get().strip())
        except tk.TclError:
            pass

    def pick_dir(self):
        d = filedialog.askdirectory(initialdir=self.outdir.get())
        if d:
            self.outdir.set(d)
            self.save_settings()

    def log_add(self, text):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    # --- prévia e corte da capa ---
    def url_changed(self, *_):
        if self._after:
            self.root.after_cancel(self._after)
        self._after = self.root.after(800, self.load_preview)

    def load_preview(self):
        url = self.url.get().strip()
        if not url.startswith("http") or url == self.pv_url:
            return
        self.pv_url = url
        self.pv_title.config(text="Carregando prévia…")
        threading.Thread(target=self.preview_worker, args=(url, self.single.get()), daemon=True).start()

    def preview_worker(self, url, single):
        try:
            is_pl, album, entries, urls, info = probe(url, single)
            got = fetch_cover(urls)
            img = Image.open(io.BytesIO(got[0])).convert("RGB") if got else None
            title = f"Playlist: {album} ({len(entries)} faixas)" if is_pl else (info.get("title") or url)
            self.q.put(("preview", (url, title, img)))
        except Exception as ex:
            self.q.put(("preview", (url, f"Não foi possível carregar a prévia: {ex}", None)))

    def set_preview(self, url, title, img):
        if url != self.pv_url:
            return
        self.pv_img, self.cx, self.cy = img, 0.5, 0.5
        self.pv_title.config(text=title)
        if img:
            k, _, _ = self.geom()
            self.pv_tk = ImageTk.PhotoImage(img.resize((max(1, int(img.width * k)), max(1, int(img.height * k)))))
        self.draw()

    def geom(self):
        k = min(PW / self.pv_img.width, PH / self.pv_img.height)
        return k, (PW - self.pv_img.width * k) / 2, (PH - self.pv_img.height * k) / 2

    def crop(self):
        return {"mode": self.crop_mode.get(), "s": float(self.zoom.get()), "cx": self.cx, "cy": self.cy}

    def on_drag(self, e):
        if not self.pv_img or self.crop_mode.get() == "orig":
            return
        k, ox, oy = self.geom()
        self.cx = min(max((e.x - ox) / (self.pv_img.width * k), 0), 1)
        self.cy = min(max((e.y - oy) / (self.pv_img.height * k), 0), 1)
        self.draw()

    def draw(self):
        c = self.canvas
        c.delete("all")
        if not self.pv_img or not self.pv_tk:
            c.create_text(PW / 2, PH / 2, text="A prévia da capa aparece aqui", fill="#aaa")
            return
        k, ox, oy = self.geom()
        c.create_image(ox, oy, anchor="nw", image=self.pv_tk)
        if self.crop_mode.get() == "orig":
            return
        l, t, r, b = crop_box(self.pv_img.size, self.crop())
        x0, y0, x1, y1 = ox + l * k, oy + t * k, ox + r * k, oy + b * k
        ex, ey = ox + self.pv_img.width * k, oy + self.pv_img.height * k
        for box in ((ox, oy, ex, y0), (ox, y1, ex, ey), (ox, y0, x0, y1), (x1, y0, ex, y1)):
            c.create_rectangle(*box, fill="black", stipple="gray50", outline="")
        c.create_rectangle(x0, y0, x1, y1, outline="white", width=2)

    # --- execução ---
    def poll(self):
        try:
            while True:
                k, v = self.q.get_nowait()
                if k == "log":
                    self.log_add(v)
                elif k == "status":
                    self.status.set(v)
                elif k == "progress":
                    self.bar["value"] = v
                elif k == "preview":
                    self.set_preview(*v)
                elif k == "done":
                    self.btn_go.config(state="normal")
                    self.btn_stop.config(state="disabled")
                    self.status.set(v)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def start(self):
        url = self.url.get().strip()
        if not url:
            messagebox.showwarning("Link vazio", "Cole o link do YouTube primeiro.")
            return
        if not shutil.which("ffmpeg"):
            messagebox.showerror("ffmpeg não encontrado",
                                 "Instale o ffmpeg ou coloque o ffmpeg.exe na mesma pasta do programa.")
            return
        self.save_settings()
        cfg = {"url": url, "fmt": self.fmt.get(), "quality": self.quality.get(),
               "full": self.mode.get() == "full", "single": self.single.get(),
               "outdir": self.outdir.get(), "crop": self.crop(),
               "cover_img": self.pv_img if self.pv_url == url else None}
        self.cancel.clear()
        self.bar["value"] = 0
        self.btn_go.config(state="disabled")
        self.btn_stop.config(state="normal")
        threading.Thread(target=self.worker, args=(cfg,), daemon=True).start()

    def worker(self, c):
        put = self.q.put
        try:
            put(("status", "Lendo o link…"))
            is_pl, album, entries, urls, _ = probe(c["url"], c["single"])
            outdir = os.path.join(c["outdir"], sanitize_filename(album)) if is_pl else c["outdir"]
            os.makedirs(outdir, exist_ok=True)

            cover = None
            if c["full"]:
                if c["cover_img"]:
                    cover = cover_jpeg(c["cover_img"], c["crop"])  # capa já recortada na prévia
                elif is_pl:
                    cover = fetch_cover(urls)

            n, ok = len(entries), 0
            for i, e in enumerate(entries, 1):
                if self.cancel.is_set():
                    break
                url = e.get("url") or f"https://www.youtube.com/watch?v={e.get('id')}"
                put(("status", f"[{i}/{n}] {e.get('title') or url}"))
                try:
                    path, vinfo = self.download(url, c, outdir, f"{i:02d} - " if is_pl else "", i, n)
                    if c["full"]:
                        cv = cover or (None if is_pl else fetch_cover(
                            video_cover_urls(vinfo.get("id"), vinfo.get("thumbnails"))))
                        tag_file(path, c["fmt"], build_meta(vinfo, album, i if is_pl else None, n), cv)
                    ok += 1
                    put(("log", f"✔ {os.path.basename(path)}"))
                except DownloadCancelled:
                    break
                except Exception as ex:
                    put(("log", f"✘ Falhou: {ex}"))
                put(("progress", i / n * 100))

            if self.cancel.is_set():
                put(("done", f"Cancelado. {ok} de {n} concluído(s)."))
            else:
                put(("done", f"Concluído: {ok} de {n} em {outdir}"))
        except Exception as ex:
            put(("log", f"Erro: {ex}"))
            put(("done", "Erro ao processar o link."))

    def download(self, url, c, outdir, prefix, i, n):
        put, cur = self.q.put, [0.0]

        def hook(d):
            if self.cancel.is_set():
                raise DownloadCancelled()
            if d["status"] == "downloading":
                tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                frac = d.get("downloaded_bytes", 0) / tot if tot else 0
                cur[0] = max(cur[0], frac * 0.9)
                put(("progress", ((i - 1) + cur[0]) / n * 100))

        tmpl = os.path.join(outdir, prefix).replace("%", "%%") + "%(title)s.%(ext)s"
        o = {"outtmpl": tmpl, "quiet": True, "no_warnings": True, "noplaylist": True,
             "progress_hooks": [hook], "logger": QuietLogger(self.q)}
        if c["fmt"] == "MP3":
            o["format"] = "bestaudio/best"
            o["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                                    "preferredquality": c["quality"]}]
        else:
            h = "" if c["quality"] == "Melhor" else f"[height<={c['quality']}]"
            o["format"] = f"bestvideo{h}[ext=mp4]+bestaudio[ext=m4a]/bestvideo{h}+bestaudio/best{h}"
            o["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(o) as ydl:
            vinfo = ydl.extract_info(url, download=True)
            path = ((vinfo.get("requested_downloads") or [{}])[0]).get("filepath")
            if not path or not os.path.exists(path):
                base = os.path.splitext(ydl.prepare_filename(vinfo))[0]
                path = base + (".mp3" if c["fmt"] == "MP3" else ".mp4")
        return path, vinfo


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()