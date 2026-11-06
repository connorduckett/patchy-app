"""The small Patchy window: shows where Patchy is running. Closing it stops Patchy."""
import os
import tkinter as tk
import webbrowser
from tkinter import font as tkfont

from . import __version__, paths
from .web import lan_address

BG, FG, DIM, ACCENT = "#161a21", "#e6e9ef", "#8b93a3", "#7aa2ff"


def run(url, cfg, stop, port, minimized=False, open_folder=None):
    root = tk.Tk()
    root.title("Patchy")
    root.resizable(False, False)
    root.configure(bg=BG)
    try:
        icon = tk.PhotoImage(file=os.path.join(paths.static_dir(), "icon-192.png"))
        root.iconphoto(True, icon)
        logo = icon.subsample(3)
    except tk.TclError:
        logo = None
    family = tkfont.nametofont("TkDefaultFont").actual()["family"]

    def label(parent, text, color=DIM, size=10, bold=False, **kw):
        lb = tk.Label(parent, text=text, fg=color, bg=BG, font=(family, size, "bold" if bold else "normal"),
                      justify="left", **kw)
        lb.pack(anchor="w")
        return lb

    def link(parent, target):
        lb = tk.Label(parent, text=target, fg=ACCENT, bg=BG, cursor="hand2", font=(family, 11, "underline"))
        lb.bind("<Button-1>", lambda _e: webbrowser.open(target))
        lb.pack(anchor="w")

    pad = tk.Frame(root, bg=BG, padx=22, pady=18)
    pad.pack()
    head = tk.Frame(pad, bg=BG)
    head.pack(anchor="w")
    if logo:
        tk.Label(head, image=logo, bg=BG).pack(side="left", padx=(0, 12))
    title = tk.Frame(head, bg=BG)
    title.pack(side="left")
    label(title, "Patchy is running", FG, 15, True)
    label(title, f"version {__version__}", size=9)

    body = tk.Frame(pad, bg=BG)
    body.pack(anchor="w", pady=(14, 6), fill="x")
    label(body, "On this computer:")
    link(body, url)
    lan = tk.Frame(body, bg=BG)
    lan.pack(anchor="w", pady=(8, 0), fill="x")

    def refresh_lan():
        for w in lan.winfo_children():
            w.destroy()
        ip = lan_address() if cfg.get("allow_lan") else ""
        if ip:
            label(lan, "Phones, tablets and other computers:")
            link(lan, f"http://{ip}:{port}/")
        else:
            label(lan, "Only this computer can open Patchy. For phones and tablets,\n"
                       "turn on network access in Patchy's Settings.")

    refresh_lan()
    btns = tk.Frame(pad, bg=BG)
    btns.pack(anchor="w", pady=(12, 4))

    def button(text, cmd, primary=False):
        tk.Button(btns, text=text, command=cmd, padx=12, pady=4, relief="flat", cursor="hand2", bd=0,
                  bg=ACCENT if primary else "#2a303b", fg="#0d1117" if primary else FG,
                  activebackground="#9bb8ff" if primary else "#353c49",
                  activeforeground="#0d1117" if primary else FG).pack(side="left", padx=(0, 8))

    button("Open Patchy", lambda: webbrowser.open(url), primary=True)
    if open_folder:
        button("Data folder", lambda: open_folder(cfg["data_dir"]))
    button("Quit", stop.set)
    label(pad, "Closing this window stops Patchy.", size=9).pack_configure(pady=(8, 0))

    root.protocol("WM_DELETE_WINDOW", stop.set)
    last_lan = [cfg.get("allow_lan")]

    def tick():
        if stop.is_set():
            root.destroy()
            return
        if cfg.get("allow_lan") != last_lan[0]:
            last_lan[0] = cfg.get("allow_lan")
            refresh_lan()
        root.after(400, tick)

    if minimized:
        root.iconify()
    root.after(400, tick)
    root.mainloop()
