#!/usr/bin/env python3
"""
Launcher do Baixador YouTube (é este arquivo que vira o .exe).

A cada abertura ele:
  1) baixa a versão mais nova do baixador_youtube.py do SEU repositório no GitHub;
  2) baixa a versão mais nova do yt-dlp (o YouTube muda toda hora e o yt-dlp precisa acompanhar);
  3) abre o app.
Sem internet, usa a última cópia salva. Assim você só refaz o .exe se mudar as bibliotecas.
"""
import importlib.machinery
import json
import os
import runpy
import shutil
import sys
import urllib.request

# >>> EDITE AQUI (repositório PÚBLICO onde está o baixador_youtube.py) <<<
REPO = "victorikaro/SampleDownloaders"
BRANCH = "main"
APP_FILE = "baixador_youtube.py"

FROZEN = getattr(sys, "frozen", False)
BASE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
HOME = os.path.join(os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config"), "BaixadorYT")
APP_PATH = os.path.join(HOME, APP_FILE)
YT_ZIP = os.path.join(HOME, "yt-dlp.zip")
YT_VER = os.path.join(HOME, "yt-dlp.version")


def _deps():
    """Nunca é chamada: só avisa o PyInstaller de quais bibliotecas embutir no .exe."""
    import io, queue, threading, urllib.request  # noqa
    import tkinter, tkinter.ttk, tkinter.filedialog, tkinter.messagebox  # noqa
    import mutagen.id3, mutagen.mp4  # noqa
    import PIL.Image, PIL.ImageTk  # noqa


def get(url, timeout=8):
    req = urllib.request.Request(url, headers={"User-Agent": "BaixadorYT"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def update_app(notes):
    try:
        new = get(f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/{APP_FILE}")
        compile(new, APP_FILE, "exec")  # só aceita se for Python válido
        old = open(APP_PATH, "rb").read() if os.path.exists(APP_PATH) else b""
        if new != old:
            with open(APP_PATH, "wb") as f:
                f.write(new)
            notes.append("Programa atualizado.")
    except Exception:
        pass


def update_ytdlp(notes):
    try:
        tag = json.loads(get("https://api.github.com/repos/yt-dlp/yt-dlp/releases/latest"))["tag_name"]
        cur = open(YT_VER).read().strip() if os.path.exists(YT_VER) else ""
        if tag != cur or not os.path.exists(YT_ZIP):
            data = get("https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp", timeout=90)
            with open(YT_ZIP + ".tmp", "wb") as f:
                f.write(data)
            os.replace(YT_ZIP + ".tmp", YT_ZIP)
            with open(YT_VER, "w") as f:
                f.write(tag)
            notes.append(f"yt-dlp atualizado ({tag}).")
    except Exception:
        pass


def main():
    os.makedirs(HOME, exist_ok=True)
    os.environ["PATH"] = os.pathsep.join([BASE, HOME, os.environ.get("PATH", "")])  # ffmpeg.exe ao lado do programa
    notes = []

    import tkinter as tk
    splash = tk.Tk()
    splash.title("Baixador YouTube")
    tk.Label(splash, text="Verificando atualizações…", padx=40, pady=24).pack()
    splash.update()

    if not os.path.exists(APP_PATH):  # primeira vez / sem internet: usa a cópia embutida
        src = os.path.join(getattr(sys, "_MEIPASS", BASE), APP_FILE)
        if os.path.exists(src):
            shutil.copy(src, APP_PATH)
    update_app(notes)
    update_ytdlp(notes)
    splash.destroy()

    if os.path.exists(YT_ZIP):  # yt-dlp baixado tem prioridade sobre o embutido
        sys.path.insert(0, YT_ZIP)
        sys.meta_path.insert(0, importlib.machinery.PathFinder)
    os.environ["BYT_NOTE"] = " ".join(notes) or "Pronto. (tudo atualizado)"
    runpy.run_path(APP_PATH, run_name="__main__")


if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk(); r.withdraw()
        messagebox.showerror("Baixador YouTube", f"Erro ao iniciar:\n{ex}")