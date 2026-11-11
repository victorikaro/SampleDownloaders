#!/usr/bin/env python3
"""
Instalador do Baixador YouTube (vira o BaixadorYT-Setup.exe).
Traz o programa completo dentro dele: copia para %LOCALAPPDATA%\\Programs\\BaixadorYT, cria os atalhos
(Área de Trabalho e Menu Iniciar) e abre o programa. O próprio programa baixa este Setup nas atualizações
e o roda com /update (ele espera o programa fechar e instala por cima).
"""
import os
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
UPDATE = "/update" in sys.argv
DEST = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Programs", "BaixadorYT")
EXE = os.path.join(DEST, "BaixadorYT.exe")
PAYLOAD = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))), "BaixadorYT.exe")


def shortcuts():
    exe, d = EXE.replace("'", "''"), DEST.replace("'", "''")
    ps = ("$w=New-Object -ComObject WScript.Shell;"
          "foreach($f in @([Environment]::GetFolderPath('Desktop'),[Environment]::GetFolderPath('Programs'))){"
          "$s=$w.CreateShortcut(\"$f\\Baixador YouTube.lnk\");"
          f"$s.TargetPath='{exe}';$s.WorkingDirectory='{d}';$s.Save()}}")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], creationflags=NOWIN, timeout=60)


def install(say):
    say("Copiando o programa…")
    os.makedirs(DEST, exist_ok=True)
    tmp = EXE + ".novo"
    shutil.copyfile(PAYLOAD, tmp)
    for _ in range(180):  # se o programa antigo ainda estiver fechando, espera (até ~3 min)
        try:
            os.replace(tmp, EXE)
            break
        except PermissionError:
            say("Aguardando o Baixador YouTube fechar…")
            time.sleep(1)
    else:
        raise RuntimeError("Feche o Baixador YouTube e rode o instalador de novo.")
    say("Criando atalhos…")
    try:
        shortcuts()
    except Exception:
        pass
    say("Pronto! Abrindo o programa…")


def main():
    root = tk.Tk()
    root.title("Baixador YouTube – " + ("Atualização" if UPDATE else "Instalação"))
    root.geometry("420x130")
    root.resizable(False, False)
    msg = tk.StringVar(value="Atualizando…" if UPDATE else "Instalando…")
    tk.Label(root, textvariable=msg, pady=18).pack()
    bar = ttk.Progressbar(root, mode="indeterminate", length=340)
    bar.pack()
    bar.start(12)
    st = {"msg": msg.get(), "done": False, "err": None}

    def work():
        try:
            install(lambda m: st.update(msg=m))
        except Exception as ex:
            st["err"] = ex
        finally:
            st["done"] = True

    def tick():
        msg.set(st["msg"])
        if not st["done"]:
            root.after(200, tick)
        elif st["err"]:
            messagebox.showerror("Baixador YouTube", f"Não consegui instalar:\n{st['err']}")
            root.destroy()
        else:
            subprocess.Popen([EXE], cwd=DEST)
            root.after(800, root.destroy)

    threading.Thread(target=work, daemon=True).start()
    tick()
    root.mainloop()


if __name__ == "__main__":
    main()