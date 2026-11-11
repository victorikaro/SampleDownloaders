#!/usr/bin/env python3
"""
Launcher do Baixador YouTube (é este arquivo que vira o .exe).

VERSÕES (MAIOR.MENOR.CORREÇÃO, ex.: 2.1.4):
  CORREÇÃO (2.1.4 -> 2.1.5): conserta erro / ajuste pequeno.
  MENOR    (2.1.5 -> 2.2.0): função nova. Atualiza sozinho pelo GitHub.
  MAIOR    (2.x.x -> 3.0.0): mudança grande, em geral com biblioteca nova.
                             Exige refazer o .exe (build_exe.bat). Até lá o .exe
                             segue usando a última versão compatível.
EXE_MAJOR é o número MAIOR que este .exe suporta. Ao refazer o .exe para uma
versão MAIOR nova, suba o EXE_MAJOR junto.

A cada abertura ele:
  1) baixa o baixador_youtube.py mais novo do seu GitHub (só se a versão for igual ou maior);
  2) baixa o yt-dlp mais novo (o YouTube muda toda hora);
  3) abre o app, mostrando na barra de status o que foi atualizado ou o que deu errado.
"""
import importlib.machinery
import json
import os
import re
import runpy
import shutil
import sys
import time
import urllib.request

# Pode deixar assim: na primeira abertura o programa pergunta o repositório e guarda.
REPO = "SEU_USUARIO/SEU_REPOSITORIO"
BRANCH = "main"
APP_FILE = "baixador_youtube.py"
EXE_MAJOR = 3

FROZEN = getattr(sys, "frozen", False)
BASE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
HOME = os.path.join(os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config"), "BaixadorYT")
APP_PATH = os.path.join(HOME, APP_FILE)
YT_ZIP = os.path.join(HOME, "yt-dlp.zip")
YT_VER = os.path.join(HOME, "yt-dlp.version")
VER_RE = re.compile(rb'APP_VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"')


def _deps():
    """Nunca é chamada: só avisa o PyInstaller de quais bibliotecas embutir no .exe."""
    import io, queue, threading, urllib.request  # noqa
    import tkinter, tkinter.ttk, tkinter.filedialog, tkinter.messagebox  # noqa
    import mutagen.id3, mutagen.mp4, mutagen.flac  # noqa
    import PIL.Image, PIL.ImageTk  # noqa
    import customtkinter  # noqa


def ver(data):
    m = VER_RE.search(data or b"")
    return tuple(int(x) for x in m.groups()) if m else (0, 0, 0)


def fmt(v):
    return ".".join(map(str, v))


def read(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return b""


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "BaixadorYT", "Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def get_repo(parent):
    f = os.path.join(HOME, "repo.txt")
    if REPO != "SEU_USUARIO/SEU_REPOSITORIO":
        return REPO
    if os.path.exists(f):
        return read(f).decode().strip()
    from tkinter import simpledialog
    r = (simpledialog.askstring("Baixador YouTube", "Cole o repositório do GitHub (usuario/repositorio):",
                                parent=parent) or "").strip()
    if r:
        write(f, r.encode())
    return r


def seed_app():
    """Se a cópia embutida no .exe for mais nova que a salva, usa a embutida (refazer o .exe sempre atualiza)."""
    src = read(os.path.join(getattr(sys, "_MEIPASS", BASE), APP_FILE))
    if src and (not os.path.exists(APP_PATH) or ver(src) > ver(read(APP_PATH))):
        write(APP_PATH, src)


def update_app(repo, notes):
    if not repo:
        notes.append("Sem repositório do GitHub configurado.")
        return
    new, err = None, None
    for br in (BRANCH, "master" if BRANCH == "main" else "main"):
        try:  # ?t= evita o cache do GitHub (que demora uns 5 min para atualizar)
            new = get(f"https://raw.githubusercontent.com/{repo}/{br}/{APP_FILE}?t={int(time.time())}")
            break
        except Exception as ex:
            err = ex
    if new is None:
        notes.append(f"Não consegui verificar atualizações ({err}). Confira o repositório '{repo}' (precisa ser público).")
        return
    try:
        compile(new, APP_FILE, "exec")
    except SyntaxError:
        notes.append("A versão do GitHub tem erro de código; ignorei.")
        return
    old = read(APP_PATH)
    nv, ov = ver(new), ver(old)
    if nv[0] > EXE_MAJOR:
        notes.append(f"A versão {fmt(nv)} é grande e exige um .exe novo (rode o build_exe.bat). Usando a {fmt(ov)}.")
    elif nv >= ov and new != old:
        write(APP_PATH, new)
        notes.append(f"Programa atualizado: {fmt(ov)} -> {fmt(nv)}.")


def update_ytdlp(notes):
    try:
        tag = json.loads(get("https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"))["tag_name"]
        cur = read(YT_VER).decode().strip()
        if tag != cur or not os.path.exists(YT_ZIP):
            write(YT_ZIP + ".tmp", get("https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp", timeout=90))
            os.replace(YT_ZIP + ".tmp", YT_ZIP)
            write(YT_VER, tag.encode())
            notes.append(f"yt-dlp atualizado ({tag}).")
    except Exception:
        pass


def ensure_ffmpeg(label, splash):
    """Windows: baixa o ffmpeg sozinho na primeira vez (fica na pasta BaixadorYT do APPDATA)."""
    if shutil.which("ffmpeg") or os.name != "nt":
        return
    try:
        import io, zipfile
        label.config(text="Baixando o ffmpeg (só na primeira vez, ~80 MB)…")
        splash.update()
        z = zipfile.ZipFile(io.BytesIO(get("https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip", timeout=600)))
        for n in z.namelist():
            if n.endswith(("/bin/ffmpeg.exe", "/bin/ffprobe.exe")):
                write(os.path.join(HOME, os.path.basename(n)), z.read(n))
    except Exception:
        pass


def main():
    os.makedirs(HOME, exist_ok=True)
    os.environ["PATH"] = os.pathsep.join([BASE, HOME, os.environ.get("PATH", "")])  # ffmpeg.exe ao lado do programa
    notes = []

    import tkinter as tk
    splash = tk.Tk()
    splash.title("Baixador YouTube")
    lbl = tk.Label(splash, text="Verificando atualizações…", padx=40, pady=24)
    lbl.pack()
    splash.update()
    ensure_ffmpeg(lbl, splash)
    lbl.config(text="Verificando atualizações…")
    splash.update()

    seed_app()
    update_app(get_repo(splash), notes)
    update_ytdlp(notes)
    splash.destroy()

    if os.path.exists(YT_ZIP):  # yt-dlp baixado tem prioridade sobre o embutido
        sys.path.insert(0, YT_ZIP)
        sys.meta_path.insert(0, importlib.machinery.PathFinder)
    os.environ["BYT_NOTE"] = " ".join(notes) or f"Pronto. Tudo atualizado (v{fmt(ver(read(APP_PATH)))})."
    runpy.run_path(APP_PATH, run_name="__main__")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk(); r.withdraw()
        messagebox.showerror("Baixador YouTube",
                             f"Erro ao iniciar (programa v{fmt(ver(read(APP_PATH)))}, .exe v{EXE_MAJOR}):\n{ex}\n\n"
                             f"Se persistir, apague a pasta:\n{HOME}")