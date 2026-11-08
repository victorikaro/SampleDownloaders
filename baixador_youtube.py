#!/usr/bin/env python3
"""
Baixador de MP3 / MP4 do YouTube (interface gráfica)

Rodar direto:  pip install -U yt-dlp mutagen pillow   ->   python baixador_youtube.py
Precisa do ffmpeg (no PATH ou ffmpeg.exe ao lado do programa).
Para virar .exe com atualização automática, veja launcher.py e build_exe.bat.
"""
APP_VERSION = "2.0.0"

import io
import json
import os
import queue
import re
import shutil
import sys
import threading
import subprocess
import urllib.parse
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled, sanitize_filename
    from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, ID3NoHeaderError
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.flac import FLAC, Picture
    from PIL import Image, ImageTk
except ImportError:
    sys.exit("Faltam dependências. Rode: pip install -U yt-dlp mutagen pillow")

PW, PH = 320, 180  # tamanho da prévia
HOME = os.path.join(os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config"), "BaixadorYT")
CFG = os.path.join(HOME, "config.json")
ARCHIVE = os.path.join(HOME, "baixados.txt")
BROWSER = [None]  # navegador para cookies (None = nenhum)


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


def itunes_cover(artist, title):
    """Capa oficial do álbum (iTunes Search, sem chave). Só aceita se o nome da música bater."""
    try:
        q = urllib.parse.quote(f"{artist} {title}")
        r = json.loads(urllib.request.urlopen(f"https://itunes.apple.com/search?term={q}&entity=song&limit=1", timeout=10).read())["results"][0]
        if title.lower()[:6] not in r["trackName"].lower():
            return None
        return fetch_cover([r["artworkUrl100"].replace("100x100bb", "1000x1000bb")])
    except Exception:
        return None


def load_archive():
    try:
        with open(ARCHIVE, encoding="utf-8") as f:
            return set(f.read().split())
    except Exception:
        return set()


def append_archive(key):
    try:
        os.makedirs(HOME, exist_ok=True)
        with open(ARCHIVE, "a", encoding="utf-8") as f:
            f.write(key + "\n")
    except Exception:
        pass


# ---------- textos ignorados ----------
_KW = r"(?:official|oficial|music|lyrics?|letra|v[ií]deo|videoclipe|clipe|[aá]udio|visuali[sz]er|hd|hq|4k|with)"
AUTO_RE = re.compile(r"[\(\[]\s*(?:" + _KW + r"\b[\s,/&+-]*)+[\)\]]", re.IGNORECASE)


def parse_words(text):
    return [w.strip() for w in re.split(r"[,\n]", text) if w.strip()]


def clean(text, words):
    """Remove os textos ignorados (sem diferenciar maiúsculas/minúsculas) e arruma o que sobrar."""
    if not words or not text:
        return text
    for w in words:
        if isinstance(w, re.Pattern):
            text = w.sub("", text)
        else:
            text = re.sub(re.escape(w), "", text, flags=re.IGNORECASE)
    text = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", "", text)          # parênteses vazios
    text = re.sub(r"(\s[-–—|]\s*){2,}", " - ", text)               # separadores duplicados
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" -–—_|")


# ---------- YouTube / metadados ----------
def probe(url, single):
    """Lê o link sem baixar. Retorna (é_playlist, álbum, entradas, urls_da_capa, info)."""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "noplaylist": single}
    if BROWSER[0]:
        opts["cookiesfrombrowser"] = (BROWSER[0],)
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


def build_meta(info, album, idx, total, words=(), over=None):
    title = clean(info.get("track") or info.get("title") or "", words)
    artist = info.get("artist") or info.get("creator")
    if isinstance(artist, (list, tuple)):
        artist = ", ".join(artist)
    if not artist:
        if " - " in title:  # "Artista - Música"
            artist, title = [s.strip() for s in title.split(" - ", 1)]
        else:
            artist = info.get("channel") or info.get("uploader") or ""
    artist = clean(re.sub(r"\s*-\s*Topic$", "", artist), words)
    m = {"title": title, "artist": artist, "album": clean(album or info.get("album") or title, words),
            "year": (info.get("upload_date") or "")[:4], "idx": idx, "total": total}
    m.update({k: v for k, v in (over or {}).items() if v})
    return m


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
    elif fmt == "FLAC":
        f = FLAC(path)
        f["title"], f["artist"], f["album"] = m["title"], m["artist"], m["album"]
        if m["year"]:
            f["date"] = m["year"]
        if m["idx"]:
            f["tracknumber"] = str(m["idx"])
        if data:
            pic = Picture()
            pic.type, pic.mime, pic.data = 3, mime, data
            f.add_picture(pic)
        f.save()
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
        root.geometry("840x830")
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
        self.ignore = tk.StringVar(value=s.get("ignore", ""))
        self.auto = tk.BooleanVar(value=s.get("auto", True))
        self.m_title, self.m_artist, self.m_album = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.skip = tk.BooleanVar(value=s.get("skip", True))
        self.norm = tk.BooleanVar(value=s.get("norm", False))
        self.itunes = tk.BooleanVar(value=s.get("itunes", False))
        self.dark = tk.BooleanVar(value=s.get("dark", False))
        self.cookies = tk.StringVar(value=s.get("cookies", "Nenhum"))
        self.outdir = tk.StringVar(value=s.get("outdir") or os.path.join(os.path.expanduser("~"), "Downloads"))
        self.status = tk.StringVar(value=os.environ.get("BYT_NOTE") or "Pronto.")

        p = ttk.Frame(root, padding=14)
        p.pack(fill="both", expand=True)

        ttk.Label(p, text="Link do vídeo ou playlist:").pack(anchor="w")
        row = ttk.Frame(p); row.pack(fill="x", pady=(2, 10))
        ttk.Entry(row, textvariable=self.url).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Colar", command=self.paste).pack(side="left", padx=(6, 0))

        ttk.Label(p, text="Mais links (um por linha) – entram na fila depois do primeiro:").pack(anchor="w")
        self.extra = tk.Text(p, height=2)
        self.extra.pack(fill="x", pady=(2, 8))

        mid = ttk.Frame(p); mid.pack(fill="x")
        left = ttk.Frame(mid); left.pack(side="left", fill="both", expand=True)

        f1 = ttk.LabelFrame(left, text="Formato", padding=8); f1.pack(fill="x")
        rf = ttk.Frame(f1); rf.pack(anchor="w")
        for t in ("MP3", "M4A", "FLAC", "MP4"):
            ttk.Radiobutton(rf, text=t, value=t, variable=self.fmt, command=self.on_fmt).pack(side="left", padx=(0, 6))
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

        nb = ttk.Notebook(p); nb.pack(fill="x", pady=(10, 0))
        t1, t2, t3 = (ttk.Frame(nb, padding=8) for _ in range(3))
        nb.add(t1, text="Metadados e faixas"); nb.add(t2, text="Limpar nomes"); nb.add(t3, text="Avançado")

        e = ttk.Frame(t1); e.pack(side="left", fill="x", expand=True)
        for i, (lbl, var) in enumerate((("Título (vídeo único)", self.m_title), ("Artista", self.m_artist), ("Álbum", self.m_album))):
            ttk.Label(e, text=lbl).grid(row=i, column=0, sticky="w", pady=2)
            ttk.Entry(e, textvariable=var).grid(row=i, column=1, sticky="ew", padx=6)
        e.columnconfigure(1, weight=1)
        ttk.Label(e, text="Preenchido pela prévia; edite à vontade. Vale para o 1º link.", foreground="#777").grid(row=3, column=0, columnspan=2, sticky="w")
        tr = ttk.Frame(t1); tr.pack(side="left", padx=(10, 0))
        ttk.Label(tr, text="Faixas da playlist (nada marcado = todas):").pack(anchor="w")
        self.tracks = tk.Listbox(tr, selectmode="extended", height=5, width=46, exportselection=False)
        self.tracks.pack()

        ttk.Checkbutton(t2, text="Remover automaticamente (Official Video), [Official Audio], (Lyrics), (Áudio Oficial), (HD)…",
                        variable=self.auto).pack(anchor="w")
        ttk.Label(t2, text="Outros textos para ignorar (separe por vírgula):").pack(anchor="w", pady=(6, 0))
        ttk.Entry(t2, textvariable=self.ignore).pack(fill="x", pady=2)

        for txt, var in (("Pular o que já foi baixado antes (desmarque para baixar de novo)", self.skip),
                         ("Normalizar o volume (só áudio)", self.norm),
                         ("Usar capa oficial do álbum (iTunes) em vídeo único, se encontrar", self.itunes),
                         ("Tema escuro", self.dark)):
            ttk.Checkbutton(t3, text=txt, variable=var, command=self.apply_theme).pack(anchor="w")
        r = ttk.Frame(t3); r.pack(anchor="w", pady=(4, 0))
        ttk.Label(r, text="Usar cookies do navegador (vídeos restritos):").pack(side="left")
        ttk.Combobox(r, textvariable=self.cookies, values=("Nenhum", "chrome", "edge", "firefox", "brave", "opera"),
                     state="readonly", width=10).pack(side="left", padx=6)

        ttk.Label(p, text="Salvar em:").pack(anchor="w", pady=(10, 0))
        row = ttk.Frame(p); row.pack(fill="x", pady=2)
        ttk.Entry(row, textvariable=self.outdir).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="Escolher…", command=self.pick_dir).pack(side="left", padx=(6, 0))

        row = ttk.Frame(p); row.pack(fill="x", pady=10)
        self.btn_go = ttk.Button(row, text="Baixar", command=self.start); self.btn_go.pack(side="left")
        self.btn_stop = ttk.Button(row, text="Cancelar", command=self.cancel.set, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        ttk.Button(row, text="Abrir pasta", command=self.open_dir).pack(side="left")

        self.bar = ttk.Progressbar(p, maximum=100); self.bar.pack(fill="x")
        ttk.Label(p, textvariable=self.status).pack(anchor="w", pady=(4, 6))
        self.log = tk.Text(p, height=6, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True)

        self.url.trace_add("write", self.url_changed)
        self.single.trace_add("write", lambda *_: (setattr(self, "pv_url", None), self.url_changed()))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.draw()
        self.last_clip = ""
        self.apply_theme()
        root.after(100, self.poll)
        root.after(1000, self.clip_poll)

    # --- configurações ---
    def save_settings(self):
        save_cfg({"outdir": self.outdir.get(), "fmt": self.fmt.get(), "quality": self.quality.get(),
                  "mode": self.mode.get(), "single": self.single.get(), "crop_mode": self.crop_mode.get(),
                  "ignore": self.ignore.get(), "auto": self.auto.get(), "skip": self.skip.get(),
                  "norm": self.norm.get(), "itunes": self.itunes.get(), "dark": self.dark.get(),
                  "cookies": self.cookies.get()})

    def on_close(self):
        self.save_settings()
        self.root.destroy()

    def set_quality_values(self, keep=False):
        f = self.fmt.get()
        vals = {"MP4": ("Melhor", "1080", "720", "480", "360"), "FLAC": ("Sem perda",)}.get(f, ("128", "192", "256", "320"))
        self.cb["values"] = vals
        if not (keep and self.quality.get() in vals):
            self.quality.set({"MP4": "Melhor", "FLAC": "Sem perda"}.get(f, "192"))

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
        BROWSER[0] = None if self.cookies.get() == "Nenhum" else self.cookies.get()
        threading.Thread(target=self.preview_worker, args=(url, self.single.get(), self.words()), daemon=True).start()

    def preview_worker(self, url, single, words):
        try:
            is_pl, album, entries, urls, info = probe(url, single)
            got = fetch_cover(urls)
            img = Image.open(io.BytesIO(got[0])).convert("RGB") if got else None
            title = f"Playlist: {album} ({len(entries)} faixas)" if is_pl else (info.get("title") or url)
            guess = {"album": clean(album, words)} if is_pl else build_meta(info, None, None, None, words)
            self.q.put(("preview", (url, title, img, [e.get("title") or "?" for e in entries] if is_pl else [], guess)))
        except Exception as ex:
            self.q.put(("preview", (url, f"Não foi possível carregar a prévia: {ex}", None, [], {})))

    def set_preview(self, url, title, img, tracks=(), guess=None):
        if url != self.pv_url:
            return
        self.pv_img, self.cx, self.cy = img, 0.5, 0.5
        self.pv_title.config(text=title)
        self.tracks.delete(0, "end")
        for i, t in enumerate(tracks, 1):
            self.tracks.insert("end", f"{i:02d} - {t}")
        g = guess or {}
        self.m_title.set(g.get("title", "")); self.m_artist.set(g.get("artist", "")); self.m_album.set(g.get("album", ""))
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

    # --- extras ---
    def words(self):
        return ([AUTO_RE] if self.auto.get() else []) + parse_words(self.ignore.get())

    def links(self):
        return [self.url.get().strip()] + re.findall(r"https?://\S+", self.extra.get("1.0", "end"))

    def clip_poll(self):
        try:
            t = self.root.clipboard_get().strip()
        except tk.TclError:
            t = ""
        if t != self.last_clip:
            self.last_clip = t
            if re.match(r"https?://(www\.|m\.|music\.)?(youtube\.com|youtu\.be)/", t) and not self.url.get().strip():
                self.url.set(t)
        self.root.after(1000, self.clip_poll)

    def apply_theme(self):
        bg, fg, fld = ("#1e1e1e", "#e8e8e8", "#2b2b2b") if self.dark.get() else ("#f0f0f0", "#111111", "#ffffff")
        st = ttk.Style()
        st.theme_use("clam")
        st.configure(".", background=bg, foreground=fg, fieldbackground=fld)
        st.configure("TNotebook.Tab", background=fld, foreground=fg)
        st.map("TCombobox", fieldbackground=[("readonly", fld)], foreground=[("readonly", fg)])
        self.root.configure(bg=bg)
        for w in (self.log, self.tracks, self.extra):
            w.configure(bg=fld, fg=fg, insertbackground=fg)

    def open_dir(self):
        d = self.outdir.get()
        os.makedirs(d, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(d)
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", d])

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
                    self.root.bell()
                    messagebox.showinfo("Baixador YouTube", v)
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
        BROWSER[0] = None if self.cookies.get() == "Nenhum" else self.cookies.get()
        cfg = {"url": url, "fmt": self.fmt.get(), "quality": self.quality.get(),
               "full": self.mode.get() == "full", "single": self.single.get(),
               "outdir": self.outdir.get(), "crop": self.crop(), "words": self.words(), "links": self.links(),
               "picks": list(self.tracks.curselection()) if self.pv_url == url else [],
               "over": {"title": self.m_title.get().strip(), "artist": self.m_artist.get().strip(),
                        "album": self.m_album.get().strip()} if self.pv_url == url else {},
               "skip": self.skip.get(), "norm": self.norm.get(), "itunes": self.itunes.get(),
               "cover_img": self.pv_img if self.pv_url == url else None}
        self.cancel.clear()
        self.bar["value"] = 0
        self.btn_go.config(state="disabled")
        self.btn_stop.config(state="normal")
        threading.Thread(target=self.worker, args=(cfg,), daemon=True).start()

    def worker(self, c):
        put, ok, tot, outdir = self.q.put, 0, 0, c["outdir"]
        for k, link in enumerate(c["links"]):
            if self.cancel.is_set():
                break
            try:
                r = self.run_link(c, link, k == 0)
                ok, tot, outdir = ok + r[0], tot + r[1], r[2]
            except Exception as ex:
                put(("log", f"Erro em {link}: {ex}"))
        put(("done", ("Cancelado. " if self.cancel.is_set() else "Concluído: ") + f"{ok} de {tot} arquivo(s) em {outdir}"))

    def run_link(self, c, link, first):
        put, words = self.q.put, c["words"]
        put(("status", f"Lendo o link… {link}"))
        is_pl, album, entries, urls, _ = probe(link, c["single"])
        over = {k: v for k, v in (c["over"] if first else {}).items() if not (is_pl and k == "title")}
        album = clean(album, words) if album else album
        outdir = os.path.join(c["outdir"], sanitize_filename(album)) if is_pl else c["outdir"]
        os.makedirs(outdir, exist_ok=True)
        if first and is_pl and c["picks"]:
            entries = [entries[i] for i in c["picks"] if i < len(entries)]
        done, cover = load_archive(), None
        if c["full"]:
            if first and c["cover_img"]:
                cover = cover_jpeg(c["cover_img"], c["crop"])  # capa já recortada na prévia
            elif is_pl:
                cover = fetch_cover(urls)

        n, ok = len(entries), 0
        for i, e in enumerate(entries, 1):
            if self.cancel.is_set():
                break
            vid = e.get("id")
            key = f'{c["fmt"]}:{vid}' if vid else None
            url = e.get("url") or f"https://www.youtube.com/watch?v={vid}"
            if c["skip"] and key in done:
                put(("log", f"↷ Já baixado: {e.get('title') or url}"))
                continue
            put(("status", f"[{i}/{n}] {e.get('title') or url}"))
            try:
                path, vinfo = self.download(url, c, outdir, f"{i:02d} - " if is_pl else "", i, n,
                                            over.get("title") or e.get("title"))
                if c["full"]:
                    m = build_meta(vinfo, album, i if is_pl else None, n, words, over)
                    cv = cover or (None if is_pl else fetch_cover(video_cover_urls(vinfo.get("id"), vinfo.get("thumbnails"))))
                    if c["itunes"] and not is_pl:
                        cv = itunes_cover(m["artist"], m["title"]) or cv
                    tag_file(path, c["fmt"], m, cv)
                ok += 1
                if key:
                    append_archive(key)
                put(("log", f"✔ {os.path.basename(path)}"))
            except DownloadCancelled:
                break
            except Exception as ex:
                put(("log", f"✘ Falhou: {ex}"))
            put(("progress", i / n * 100))
        return ok, n, outdir

    def download(self, url, c, outdir, prefix, i, n, title=None):
        put, cur = self.q.put, [0.0]

        def hook(d):
            if self.cancel.is_set():
                raise DownloadCancelled()
            if d["status"] == "downloading":
                tot = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                frac = d.get("downloaded_bytes", 0) / tot if tot else 0
                cur[0] = max(cur[0], frac * 0.9)
                put(("progress", ((i - 1) + cur[0]) / n * 100))

        name = sanitize_filename(clean(title, c["words"])) if title else ""
        tmpl = os.path.join(outdir, prefix).replace("%", "%%") + (name.replace("%", "%%") or "%(title)s") + ".%(ext)s"
        o = {"outtmpl": tmpl, "quiet": True, "no_warnings": True, "noplaylist": True,
             "progress_hooks": [hook], "logger": QuietLogger(self.q)}
        if BROWSER[0]:
            o["cookiesfrombrowser"] = (BROWSER[0],)
        if c["fmt"] != "MP4":
            codec = c["fmt"].lower()  # mp3 / m4a / flac
            pp = {"key": "FFmpegExtractAudio", "preferredcodec": codec}
            if codec != "flac":
                pp["preferredquality"] = c["quality"]
            o["format"], o["postprocessors"] = "bestaudio/best", [pp]
            if c["norm"]:
                o["postprocessor_args"] = {"extractaudio": ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]}
        else:
            h = "" if c["quality"] == "Melhor" else f"[height<={c['quality']}]"
            o["format"] = f"bestvideo{h}[ext=mp4]+bestaudio[ext=m4a]/bestvideo{h}+bestaudio/best{h}"
            o["merge_output_format"] = "mp4"

        with yt_dlp.YoutubeDL(o) as ydl:
            vinfo = ydl.extract_info(url, download=True)
            path = ((vinfo.get("requested_downloads") or [{}])[0]).get("filepath")
            if not path or not os.path.exists(path):
                base = os.path.splitext(ydl.prepare_filename(vinfo))[0]
                path = base + "." + c["fmt"].lower()
        return path, vinfo


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()