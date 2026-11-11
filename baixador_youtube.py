#!/usr/bin/env python3
"""
Baixador de MP3 / MP4 do YouTube (interface gráfica)

Rodar direto:  pip install -U yt-dlp mutagen pillow customtkinter   ->   python baixador_youtube.py
Precisa do ffmpeg (no PATH ou ffmpeg.exe ao lado do programa).
O .exe e o instalador (Setup) são gerados na nuvem pelo GitHub Actions (build.yml).
"""
# Versão = MAIOR.MENOR.CORREÇÃO (ex.: 3.1.4)
#   CORREÇÃO (3.1.4 -> 3.1.5): conserta erro / ajuste pequeno
#   MENOR    (3.1.5 -> 3.2.0): função nova
#   MAIOR    (3.x.x -> 4.0.0): mudança grande (visual novo, reescrita...)
# Para publicar: aumente o número abaixo e envie este arquivo ao GitHub. O GitHub Actions gera o
# BaixadorYT-Setup.exe sozinho e o programa instalado se atualiza quando for aberto.
APP_VERSION = "3.1.0"

import importlib.machinery
import io
import json
import os
import queue
import re
import shutil
import sys
import threading
import subprocess
import tempfile
import urllib.parse
import urllib.request
import tkinter as tk
from tkinter import filedialog, messagebox

_HOME = os.path.join(os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config"), "BaixadorYT")
_YT_ZIP = os.path.join(_HOME, "yt-dlp.zip")
if os.path.exists(_YT_ZIP):
    sys.path.insert(0, _YT_ZIP)
    sys.meta_path.insert(0, importlib.machinery.PathFinder)

try:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled, sanitize_filename
    from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, ID3NoHeaderError
    from mutagen.mp4 import MP4, MP4Cover
    from mutagen.flac import FLAC, Picture
    from PIL import Image, ImageTk
    import customtkinter as ctk
except ImportError:
    sys.exit("Faltam dependências. Rode: pip install -U yt-dlp mutagen pillow customtkinter")

PW, PH = 320, 180  # tamanho da prévia
HOME = os.path.join(os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config"), "BaixadorYT")
CFG = os.path.join(HOME, "config.json")
ARCHIVE = os.path.join(HOME, "baixados.txt")
BROWSER = [None]  # navegador para cookies (None = nenhum)
NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
FROZEN = getattr(sys, "frozen", False)
GITHUB_REPO = "SEU_USUARIO/SEU_REPOSITORIO"  # o GitHub Actions troca isto pelo seu repositório na hora de gerar o .exe
UA = {"User-Agent": "BaixadorYT"}
os.environ["PATH"] = os.pathsep.join([HOME, os.environ.get("PATH", "")])  # deno.exe / ffmpeg.exe baixados ficam aqui


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


# ---------- Deno (o yt-dlp exige um executor de JavaScript para o YouTube) ----------
def deno_ok():
    exe = shutil.which("deno")
    if not exe:
        return False
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=20, creationflags=NOWIN).stdout
        m = re.search(r"deno (\d+)\.(\d+)", out)
        return bool(m) and (int(m.group(1)), int(m.group(2))) >= (2, 3)
    except Exception:
        return False


def ensure_deno(say):
    """Windows: baixa o Deno sozinho na primeira vez (fica na pasta BaixadorYT). Retorna True se estiver pronto."""
    if deno_ok():
        return True
    if os.name != "nt":
        say("Falta o Deno (versão 2.3 ou maior), que o yt-dlp exige para o YouTube. Instale em https://deno.com")
        return False
    try:
        import zipfile
        arch = "aarch64" if os.environ.get("PROCESSOR_ARCHITECTURE", "").lower() == "arm64" else "x86_64"
        say("Baixando o Deno (só na primeira vez, ~40 MB)…")
        req = urllib.request.Request(
            f"https://github.com/denoland/deno/releases/latest/download/deno-{arch}-pc-windows-msvc.zip",
            headers={"User-Agent": "BaixadorYT"})
        z = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(req, timeout=300).read()))
        os.makedirs(HOME, exist_ok=True)
        with open(os.path.join(HOME, "deno.exe"), "wb") as f:
            f.write(z.read("deno.exe"))
    except Exception as ex:
        say(f"Não consegui baixar o Deno ({ex}). Instale manualmente em https://deno.com")
        return False
    return deno_ok()


def http_get(url, timeout=10):
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read()


def ensure_ffmpeg(say):
    """Windows: baixa o ffmpeg sozinho na primeira vez (fica na pasta BaixadorYT do APPDATA)."""
    if shutil.which("ffmpeg") or os.name != "nt":
        return
    try:
        import zipfile
        say("Baixando o ffmpeg (só na primeira vez, ~80 MB)…")
        z = zipfile.ZipFile(io.BytesIO(http_get("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip", 600)))
        os.makedirs(HOME, exist_ok=True)
        for n in z.namelist():
            if n.endswith(("/bin/ffmpeg.exe", "/bin/ffprobe.exe")):
                with open(os.path.join(HOME, os.path.basename(n)), "wb") as f:
                    f.write(z.read(n))
    except Exception as ex:
        say(f"Não consegui baixar o ffmpeg ({ex}). Coloque o ffmpeg.exe em {HOME}.")


def update_ytdlp(say):
    """Baixa o yt-dlp mais novo (o YouTube muda toda hora). Passa a valer na próxima abertura."""
    try:
        tag = json.loads(http_get("https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"))["tag_name"]
        vf = os.path.join(HOME, "yt-dlp.version")
        cur = open(vf).read().strip() if os.path.exists(vf) else ""
        if tag != cur or not os.path.exists(_YT_ZIP):
            data = http_get("https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp", 120)
            os.makedirs(HOME, exist_ok=True)
            with open(_YT_ZIP + ".tmp", "wb") as f:
                f.write(data)
            os.replace(_YT_ZIP + ".tmp", _YT_ZIP)
            with open(vf, "w") as f:
                f.write(tag)
            say(f"yt-dlp atualizado ({tag}). Vale a partir da próxima abertura.")
    except Exception:
        pass


def check_release():
    """Procura um release mais novo no GitHub. Retorna (versão, url_do_setup) ou None."""
    if not FROZEN or "SEU_USUARIO" in GITHUB_REPO:
        return None
    try:
        r = json.loads(http_get(f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"))
        new = tuple(int(x) for x in r["tag_name"].lstrip("v").split("."))
        if new > tuple(int(x) for x in APP_VERSION.split(".")):
            a = next(x for x in r["assets"] if x["name"].lower().endswith("setup.exe"))
            return ".".join(map(str, new)), a["browser_download_url"]
    except Exception:
        pass
    return None


def hint(msg):
    m = str(msg)
    if "Requested format is not available" in m:
        return ("Dica: costuma ser o YouTube sem liberar os formatos (falta do Deno, ou bloqueio). "
                "Tente de novo e, se persistir, use cookies na aba Avançado.")
    if "Sign in to confirm" in m or "not a bot" in m:
        return ("Dica: aba Avançado → 'Usar cookies do navegador' (o navegador em que você está logado no YouTube; "
                "se der erro com Chrome/Edge, tente Firefox).")
    return ""


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
COOKIE_BAD = set()  # navegadores cujos cookies não deu para ler nesta rodada
COOKIE_FAIL = re.compile(r"Could not copy|cookie database|decrypt|DPAPI|locked", re.IGNORECASE)


def run_with_cookies(opts, say, fn):
    """Roda fn(opts) com os cookies do navegador; se não der para ler (ex.: Chrome aberto), avisa e segue sem eles."""
    b = BROWSER[0]
    if b and b not in COOKIE_BAD:
        try:
            return fn({**opts, "cookiesfrombrowser": (b,)})
        except Exception as ex:
            if not COOKIE_FAIL.search(str(ex)):
                raise
            COOKIE_BAD.add(b)
            say(f"⚠ Não consegui ler os cookies do {b} (feche o navegador por completo ou use o Firefox). "
                "Continuando sem cookies.")
    return fn(opts)


def probe(url, single, say=lambda m: None):
    """Lê o link sem baixar. Retorna (é_playlist, álbum, entradas, urls_da_capa, info)."""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": "in_playlist", "noplaylist": single}

    def go(o):
        with yt_dlp.YoutubeDL(o) as y:
            return y.extract_info(url, download=False)
    info = run_with_cookies(opts, say, go)
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
    def warning(self, msg):  # só repassa os avisos que explicam por que o YouTube falhou
        if re.search(r"JavaScript|runtime|challenge|SABR|sign in|signature|format", str(msg), re.IGNORECASE):
            self.q.put(("log", "⚠ " + str(msg)))

    def error(self, msg):
        self.q.put(("log", str(msg)))


# ---------- aparência (estilo web app: cartões arredondados, destaque índigo) ----------
BG = ("#f4f5f9", "#0f1117"); CARD = ("#ffffff", "#171a23"); FIELD = ("#f1f3f8", "#1f2330"); LINE = ("#e3e6ee", "#2a2f3d")
TEXT = ("#1b1f2a", "#e6e8ee"); MUTED = ("#6b7280", "#8b92a5")
ACC, ACC_H, DANGER = "#6366f1", "#4f46e5", "#ef4444"
ACC_SOFT, ACC_SOFT_H = ("#e0e7ff", "#312e81"), ("#c7d2fe", "#3730a3")


def F(size=13, weight="normal"):
    return ctk.CTkFont(size=size, weight=weight)


def card(parent, title=None):
    c = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=14, border_width=1, border_color=LINE)
    if title:
        ctk.CTkLabel(c, text=title, font=F(13, "bold"), text_color=TEXT).pack(anchor="w", padx=14, pady=(12, 2))
    return c


def label(parent, text, muted=False):
    return ctk.CTkLabel(parent, text=text, text_color=MUTED if muted else TEXT, font=F(12 if muted else 13),
                        anchor="w", justify="left")


def entry(parent, var):
    return ctk.CTkEntry(parent, textvariable=var, height=36, corner_radius=10, fg_color=FIELD,
                        border_color=LINE, text_color=TEXT, font=F(13))


def button(parent, text, cmd, kind="primary", **kw):
    h = kw.pop("height", 36)
    st = {"primary": dict(fg_color=ACC, hover_color=ACC_H, text_color="white"),
          "ghost": dict(fg_color=FIELD, hover_color=LINE, text_color=TEXT),
          "danger": dict(fg_color="transparent", hover_color=FIELD, text_color=DANGER, border_width=1, border_color=DANGER)}[kind]
    return ctk.CTkButton(parent, text=text, command=cmd, height=h, corner_radius=10, font=F(13, "bold"), **st, **kw)


def switch(parent, text, var, cmd=None):
    return ctk.CTkSwitch(parent, text=text, variable=var, command=cmd, progress_color=ACC, text_color=TEXT, font=F(13))


def option(parent, var, values, width=120):
    return ctk.CTkOptionMenu(parent, variable=var, values=list(values), width=width, height=32, corner_radius=10,
                             fg_color=FIELD, button_color=ACC, button_hover_color=ACC_H, text_color=TEXT, font=F(13))


# ---------- aplicativo ----------
class App:
    def __init__(self, root):
        self.root = root
        root.title(f"Baixador YouTube  (v{APP_VERSION})")
        root.geometry(f"1000x{min(800, max(560, root.winfo_screenheight() - 90))}")  # cabe em telas menores
        root.minsize(900, 520)
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.ready = threading.Event()  # fica "ligado" quando o Deno está pronto
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
        self.dark = tk.BooleanVar(value=s.get("dark", True))
        self.cookies = tk.StringVar(value=s.get("cookies", "Nenhum"))
        self.outdir = tk.StringVar(value=s.get("outdir") or os.path.join(os.path.expanduser("~"), "Downloads"))
        self.status = tk.StringVar(value=os.environ.get("BYT_NOTE") or "Pronto.")

        # Rodapé fixo (botões, progresso, status, log); o resto rola se a tela for pequena.
        bottom = ctk.CTkFrame(root, fg_color=CARD, corner_radius=0, border_width=1, border_color=LINE)
        bottom.pack(side="bottom", fill="x")
        body = ctk.CTkScrollableFrame(root, fg_color=BG, corner_radius=0)
        body.pack(side="top", fill="both", expand=True)

        hd = ctk.CTkFrame(body, fg_color="transparent"); hd.pack(fill="x", padx=8, pady=(6, 12))
        ctk.CTkLabel(hd, text="▼", width=40, height=40, corner_radius=12, fg_color=ACC, text_color="white",
                     font=F(18, "bold")).pack(side="left")
        tt = ctk.CTkFrame(hd, fg_color="transparent"); tt.pack(side="left", padx=12)
        ctk.CTkLabel(tt, text="Baixador YouTube", font=F(22, "bold"), text_color=TEXT).pack(anchor="w")
        label(tt, "MP3, M4A, FLAC e MP4 com metadados e capa", muted=True).pack(anchor="w")
        ctk.CTkLabel(hd, text=f"v{APP_VERSION}", width=64, height=26, corner_radius=13, fg_color=ACC_SOFT,
                     text_color=TEXT, font=F(12, "bold")).pack(side="right")

        top = ctk.CTkFrame(body, fg_color="transparent"); top.pack(fill="x", padx=8)
        top.columnconfigure(0, weight=1)
        left = ctk.CTkFrame(top, fg_color="transparent"); left.grid(row=0, column=0, sticky="nsew")
        right = card(top, "Capa  ·  usada no modo Completo"); right.grid(row=0, column=1, sticky="n", padx=(12, 0))

        c1 = card(left, "Link do vídeo ou playlist"); c1.pack(fill="x")
        r = ctk.CTkFrame(c1, fg_color="transparent"); r.pack(fill="x", padx=14, pady=(4, 8))
        entry(r, self.url).pack(side="left", fill="x", expand=True)
        button(r, "Colar", self.paste, "ghost", width=70).pack(side="left", padx=(8, 0))
        label(c1, "Mais links (um por linha) entram na fila depois do primeiro", muted=True).pack(anchor="w", padx=14)
        self.extra = ctk.CTkTextbox(c1, height=54, corner_radius=10, fg_color=FIELD, text_color=TEXT,
                                    border_width=1, border_color=LINE, font=F(13))
        self.extra.pack(fill="x", padx=14, pady=(4, 14))

        c2 = card(left, "Formato e modo"); c2.pack(fill="x", pady=(12, 0))
        r = ctk.CTkFrame(c2, fg_color="transparent"); r.pack(fill="x", padx=14, pady=(4, 10))
        self.seg(r, {t: t for t in ("MP3", "M4A", "FLAC", "MP4")}, self.fmt, self.on_fmt).pack(side="left")
        self.cb = option(r, self.quality, ["192"], 110)
        self.cb.pack(side="right")
        label(r, "Qualidade").pack(side="right", padx=8)
        self.set_quality_values(keep=True)
        self.seg(c2, {"Rápido (sem metadados)": "fast", "Completo (metadados + capa)": "full"}, self.mode).pack(
            fill="x", padx=14, pady=(0, 10))
        switch(c2, "Só o vídeo (ignorar a playlist do link)", self.single).pack(anchor="w", padx=14, pady=(0, 14))

        self.canvas = tk.Canvas(right, width=PW, height=PH, bg="#0b0d12", highlightthickness=0)
        self.canvas.pack(padx=14, pady=(6, 0))
        self.canvas.bind("<Button-1>", self.on_drag)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.pv_title = ctk.CTkLabel(right, text="", wraplength=PW, justify="left", anchor="w", text_color=TEXT, font=F(12))
        self.pv_title.pack(anchor="w", padx=14, pady=(6, 0))
        self.seg(right, {"Quadrado 1:1": "square", "Original": "orig"}, self.crop_mode, self.draw).pack(fill="x", padx=14, pady=(8, 4))
        r = ctk.CTkFrame(right, fg_color="transparent"); r.pack(fill="x", padx=14)
        label(r, "Tamanho do corte").pack(side="left")
        ctk.CTkSlider(r, from_=0.4, to=1.0, variable=self.zoom, command=lambda _v: self.draw(), progress_color=ACC,
                      button_color=ACC, button_hover_color=ACC_H, width=170).pack(side="right")
        label(right, "Arraste sobre a imagem para posicionar o corte.", muted=True).pack(anchor="w", padx=14, pady=(2, 14))

        tv = ctk.CTkTabview(body, height=230, corner_radius=14, border_width=1, border_color=LINE, fg_color=CARD,
                            segmented_button_fg_color=FIELD, segmented_button_selected_color=ACC_SOFT,
                            segmented_button_selected_hover_color=ACC_SOFT_H, segmented_button_unselected_color=FIELD,
                            segmented_button_unselected_hover_color=LINE, text_color=TEXT)
        tv.pack(fill="x", padx=8, pady=12)
        t1, t2, t3 = (tv.add(n) for n in ("Metadados e faixas", "Limpar nomes", "Avançado"))

        e = ctk.CTkFrame(t1, fg_color="transparent"); e.pack(side="left", fill="both", expand=True)
        for i, (lbl, var) in enumerate((("Título (vídeo único)", self.m_title), ("Artista", self.m_artist), ("Álbum", self.m_album))):
            label(e, lbl).grid(row=i, column=0, sticky="w", pady=4)
            entry(e, var).grid(row=i, column=1, sticky="ew", padx=(8, 0))
        e.columnconfigure(1, weight=1)
        label(e, "Preenchido pela prévia; edite à vontade. Vale para o 1º link.", muted=True).grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        tr = ctk.CTkFrame(t1, fg_color="transparent"); tr.pack(side="left", padx=(16, 0))
        label(tr, "Faixas da playlist (nada marcado = todas)", muted=True).pack(anchor="w")
        self.tracks = tk.Listbox(tr, selectmode="extended", height=6, width=42, exportselection=False, relief="flat",
                                 highlightthickness=0, bd=0, activestyle="none", font=("Segoe UI", 10))
        self.tracks.pack(pady=(4, 0))

        switch(t2, "Remover automaticamente (Official Video), [Official Audio], (Lyrics), (Áudio Oficial), (HD)…",
               self.auto).pack(anchor="w")
        label(t2, "Outros textos para ignorar (separe por vírgula)").pack(anchor="w", pady=(10, 2))
        entry(t2, self.ignore).pack(fill="x")

        for i, (txt, var) in enumerate((("Pular o que já foi baixado", self.skip), ("Normalizar o volume (só áudio)", self.norm),
                                        ("Capa oficial do álbum (iTunes), vídeo único", self.itunes), ("Tema escuro", self.dark))):
            switch(t3, txt, var, self.apply_theme).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 28), pady=5)
        r = ctk.CTkFrame(t3, fg_color="transparent"); r.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        label(r, "Cookies do navegador (vídeos restritos)").pack(side="left")
        option(r, self.cookies, ("Nenhum", "chrome", "edge", "firefox", "brave", "opera")).pack(side="left", padx=8)

        c3 = card(body, "Salvar em"); c3.pack(fill="x", padx=8)
        r = ctk.CTkFrame(c3, fg_color="transparent"); r.pack(fill="x", padx=14, pady=(4, 14))
        entry(r, self.outdir).pack(side="left", fill="x", expand=True)
        button(r, "Escolher…", self.pick_dir, "ghost", width=100).pack(side="left", padx=(8, 0))

        row = ctk.CTkFrame(bottom, fg_color="transparent"); row.pack(fill="x", padx=16, pady=(12, 8))
        self.btn_go = button(row, "Baixar", self.start, height=40, width=140); self.btn_go.pack(side="left")
        self.btn_stop = button(row, "Cancelar", self.cancel.set, "danger", height=40, width=110, state="disabled")
        self.btn_stop.pack(side="left", padx=8)
        button(row, "Abrir pasta", self.open_dir, "ghost", height=40, width=120).pack(side="left")
        self.bar = ctk.CTkProgressBar(bottom, height=8, progress_color=ACC, fg_color=FIELD)
        self.bar.set(0); self.bar.pack(fill="x", padx=16)
        ctk.CTkLabel(bottom, textvariable=self.status, anchor="w", justify="left", wraplength=940,
                     text_color=MUTED, font=F(12)).pack(fill="x", padx=16, pady=(4, 0))
        self.log = ctk.CTkTextbox(bottom, height=56, corner_radius=10, fg_color=FIELD, text_color=TEXT,
                                  font=("Consolas", 12), state="disabled")
        self.log.pack(fill="x", padx=16, pady=(6, 12))

        self.url.trace_add("write", self.url_changed)
        self.single.trace_add("write", lambda *_: (setattr(self, "pv_url", None), self.url_changed()))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.draw()
        self.last_clip = ""
        self.apply_theme()
        root.after(100, self.poll)
        root.after(1000, self.clip_poll)
        threading.Thread(target=self.prep, daemon=True).start()

    def seg(self, parent, opts, var, cmd=None):
        """Botão segmentado: opts = {texto mostrado: valor guardado na variável}."""
        inv = {v: k for k, v in opts.items()}

        def on(lbl):
            var.set(opts[lbl])
            if cmd:
                cmd()
        w = ctk.CTkSegmentedButton(parent, values=list(opts), command=on, fg_color=FIELD, selected_color=ACC_SOFT,
                                   selected_hover_color=ACC_SOFT_H, unselected_color=FIELD, unselected_hover_color=LINE,
                                   text_color=TEXT, font=F(13))
        w.set(inv.get(var.get(), next(iter(opts))))
        return w

    def prep(self):
        def say(m):
            self.q.put(("status", m))
            self.q.put(("log", m))
        try:
            ensure_deno(say)
            ensure_ffmpeg(say)
        finally:
            self.ready.set()
        update_ytdlp(say)
        r = check_release()
        if r:
            self.q.put(("update", r))

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
        self.cb.configure(values=list(vals))
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
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

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
        self.pv_title.configure(text="Carregando prévia…")
        BROWSER[0] = None if self.cookies.get() == "Nenhum" else self.cookies.get()
        threading.Thread(target=self.preview_worker, args=(url, self.single.get(), self.words()), daemon=True).start()

    def preview_worker(self, url, single, words):
        try:
            is_pl, album, entries, urls, info = probe(url, single, lambda m: self.q.put(("log", m)))
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
        self.pv_title.configure(text=title)
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
        d = self.dark.get()
        ctk.set_appearance_mode("dark" if d else "light")
        self.root.configure(fg_color=BG)
        self.canvas.configure(bg="#0b0d12" if d else "#e9ebf2")
        self.tracks.configure(bg=FIELD[1] if d else FIELD[0], fg=TEXT[1] if d else TEXT[0],
                              selectbackground=ACC, selectforeground="white")
        self.draw()

    # --- atualização do programa (baixa o Setup novo inteiro e instala por cima) ---
    def ask_update(self, ver, url):
        if messagebox.askyesno("Atualização disponível",
                               f"Existe a versão {ver} (você está na {APP_VERSION}).\nBaixar e instalar agora?"):
            self.btn_go.configure(state="disabled")
            threading.Thread(target=self.do_update, args=(url,), daemon=True).start()

    def do_update(self, url):
        put = self.q.put
        try:
            path = os.path.join(tempfile.gettempdir(), "BaixadorYT-Setup.exe")
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r, open(path, "wb") as f:
                total, got = int(r.headers.get("Content-Length") or 0), 0
                while True:
                    chunk = r.read(1 << 18)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        put(("progress", got * 100 / total))
                        put(("status", f"Baixando a atualização… {got * 100 // total}%"))
            put(("install", path))
        except Exception as ex:
            put(("done", f"Não consegui baixar a atualização: {ex}"))

    def run_setup(self, path):
        self.save_settings()
        subprocess.Popen([path, "/update"], creationflags=0x00000008)  # DETACHED_PROCESS: continua depois de fechar o app
        self.root.destroy()

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
                    self.bar.set(v / 100)
                elif k == "preview":
                    self.set_preview(*v)
                elif k == "update":
                    self.ask_update(*v)
                elif k == "install":
                    self.run_setup(v)
                elif k == "done":
                    self.btn_go.configure(state="normal")
                    self.btn_stop.configure(state="disabled")
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
        if self.ready.is_set() and not shutil.which("ffmpeg"):
            messagebox.showerror("ffmpeg não encontrado",
                                 "Instale o ffmpeg ou coloque o ffmpeg.exe na mesma pasta do programa.")
            return
        self.save_settings()
        BROWSER[0] = None if self.cookies.get() == "Nenhum" else self.cookies.get()
        COOKIE_BAD.clear()  # tenta os cookies de novo a cada clique (o Chrome pode ter sido fechado)
        cfg = {"url": url, "fmt": self.fmt.get(), "quality": self.quality.get(),
               "full": self.mode.get() == "full", "single": self.single.get(),
               "outdir": self.outdir.get(), "crop": self.crop(), "words": self.words(), "links": self.links(),
               "picks": list(self.tracks.curselection()) if self.pv_url == url else [],
               "over": {"title": self.m_title.get().strip(), "artist": self.m_artist.get().strip(),
                        "album": self.m_album.get().strip()} if self.pv_url == url else {},
               "skip": self.skip.get(), "norm": self.norm.get(), "itunes": self.itunes.get(),
               "cover_img": self.pv_img if self.pv_url == url else None}
        self.cancel.clear()
        self.bar.set(0)
        self.btn_go.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        threading.Thread(target=self.worker, args=(cfg,), daemon=True).start()

    def worker(self, c):
        put, ok, tot, outdir = self.q.put, 0, 0, c["outdir"]
        if not self.ready.is_set():
            put(("status", "Preparando o Deno (só na primeira vez)…"))
            self.ready.wait()
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
        is_pl, album, entries, urls, _ = probe(link, c["single"], lambda m: put(("log", m)))
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
                if hint(ex):
                    put(("log", hint(ex)))
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
        o = {"outtmpl": tmpl, "quiet": True, "no_warnings": False, "noplaylist": True,
             "progress_hooks": [hook], "logger": QuietLogger(self.q)}
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

        def go(opts):
            with yt_dlp.YoutubeDL(opts) as ydl:
                vinfo = ydl.extract_info(url, download=True)
                path = ((vinfo.get("requested_downloads") or [{}])[0]).get("filepath")
                if not path or not os.path.exists(path):
                    base = os.path.splitext(ydl.prepare_filename(vinfo))[0]
                    path = base + "." + c["fmt"].lower()
            return path, vinfo
        return run_with_cookies(o, lambda m: put(("log", m)), go)


if __name__ == "__main__":
    root = ctk.CTk()
    App(root)
    root.mainloop()