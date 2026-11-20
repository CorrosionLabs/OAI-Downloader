"""Launcher for OAI-Downloader (Python and packaged executable)."""

import json
import locale
from pathlib import Path
import subprocess
import sys
import tkinter as tk
import webbrowser

from PIL import Image, ImageTk
from tkinter import filedialog, messagebox, ttk

from core.paths import get_settings_file


APP_NAME = "OAI-Downloader"
ASSET_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets"
TOOLS_URL = "https://corrosionlabs.github.io/tools/"


def load_image(filename, max_width, max_height):
    path = ASSET_ROOT / filename
    if not path.is_file():
        return None
    with Image.open(path) as image:
        image = image.convert("RGBA")
        image.thumbnail((max_width, max_height), Image.Resampling.LANCZOS)
        return ImageTk.PhotoImage(image)

OS_NAME = {"win32": "Windows", "darwin": "macOS"}.get(sys.platform, "Linux" if sys.platform.startswith("linux") else sys.platform)
TEXT = {
    "es": {
        "language": "Idioma", "system": "Sistema detectado", "folder": "Carpeta de trabajo",
        "choose": "Seleccionar carpeta", "setup": "Configuración inicial",
        "setup_info": "Elige dónde se guardarán los datos y las copias de seguridad. Se creará un archivo settings.json para recordar esta ubicación.",
        "intro": "Descarga y conserva una copia local de tus conversaciones y recursos de ChatGPT.",
        "start": "Iniciar aplicación", "clear": "Borrar configuración local",
        "keep": "Las descargas y copias de seguridad no se borrarán.",
        "missing": "Selecciona una carpeta válida antes de continuar.",
        "invalid": "La carpeta configurada ya no existe. Selecciona otra.",
        "clear_title": "Borrar configuración local",
        "clear_confirm": "¿Eliminar la configuración de OAI-Downloader?\n\nLos backups y las descargas se conservarán.",
        "cleared": "Configuración eliminada. Los backups se han conservado.",
        "saved_error": "No se pudo guardar la configuración:",
        "clear_error": "No se pudo borrar la configuración:",
        "file_missing": "No se encuentra", "pip_missing": "pip no está disponible.",
        "install_error": "No se pudieron instalar las dependencias.",
        "launch_error": "Error al iniciar:",
    },
    "en": {
        "language": "Language", "system": "Detected system", "folder": "Data folder",
        "choose": "Choose folder", "setup": "Initial setup",
        "setup_info": "Choose where data and backups will be stored. A settings.json file will be created to remember this location.",
        "intro": "Download and keep a local copy of your ChatGPT conversations and resources.",
        "start": "Start application", "clear": "Clear local settings",
        "keep": "Downloads and backups will not be deleted.",
        "missing": "Choose a valid folder before continuing.",
        "invalid": "The configured folder no longer exists. Choose another.",
        "clear_title": "Clear local settings",
        "clear_confirm": "Delete OAI-Downloader settings?\n\nBackups and downloads will be kept.",
        "cleared": "Settings deleted. Backups have been kept.",
        "saved_error": "Could not save settings:",
        "clear_error": "Could not clear settings:",
        "file_missing": "Missing file", "pip_missing": "pip is not available.",
        "install_error": "Could not install dependencies.",
        "launch_error": "Could not start:",
    },
    "fr": {
        "language": "Langue", "system": "Système détecté", "folder": "Dossier de données",
        "choose": "Choisir un dossier", "setup": "Configuration initiale",
        "setup_info": "Choisissez où conserver les données et les sauvegardes. Un fichier settings.json sera créé pour mémoriser cet emplacement.",
        "intro": "Téléchargez et conservez une copie locale de vos conversations et ressources ChatGPT.",
        "start": "Démarrer l’application", "clear": "Effacer la configuration locale",
        "keep": "Les téléchargements et les sauvegardes seront conservés.",
        "missing": "Choisissez un dossier valide avant de continuer.",
        "invalid": "Le dossier configuré n’existe plus. Choisissez-en un autre.",
        "clear_title": "Effacer la configuration locale",
        "clear_confirm": "Supprimer la configuration d’OAI-Downloader ?\n\nLes sauvegardes et téléchargements seront conservés.",
        "cleared": "Configuration supprimée. Les sauvegardes sont conservées.",
        "saved_error": "Impossible d’enregistrer la configuration :",
        "clear_error": "Impossible d’effacer la configuration :",
        "file_missing": "Fichier introuvable", "pip_missing": "pip n’est pas disponible.",
        "install_error": "Impossible d’installer les dépendances.",
        "launch_error": "Erreur au démarrage :",
    },
}


LANGUAGE_NAMES = {"Español": "es", "English": "en", "Français": "fr"}


def tr(key):
    return TEXT[LANGUAGE_NAMES[language.get()]][key]


def configured_folder():
    settings_file = get_settings_file()
    if not settings_file.is_file():
        return None
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        folder = Path(data["data_root"])
        return folder if folder.is_absolute() and folder.is_dir() else None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_folder(folder):
    settings_file = get_settings_file()
    data = {}
    if settings_file.is_file():
        try:
            existing = json.loads(settings_file.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                data = existing
        except (OSError, ValueError):
            pass
    data["data_root"] = str(folder)
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    temporary = settings_file.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(settings_file)


def start_app():
    if getattr(sys, "frozen", False):
        from run_web import main as run_application
        run_application()
        return 0

    project_root = Path(__file__).resolve().parent
    for filename in ("requirements.txt", "run_web.py"):
        if not (project_root / filename).is_file():
            messagebox.showerror(APP_NAME, f"{tr('file_missing')}: {filename}")
            return 1
    try:
        pip_check = subprocess.run(
            [sys.executable, "-m", "pip", "--version"], cwd=project_root, check=False,
        )
        if pip_check.returncode != 0:
            messagebox.showerror(APP_NAME, tr("pip_missing"))
            return pip_check.returncode
        install = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
            cwd=project_root, check=False,
        )
        if install.returncode != 0:
            messagebox.showerror(APP_NAME, tr("install_error"))
            return install.returncode
        return subprocess.run(
            [sys.executable, "run_web.py"], cwd=project_root, check=False,
        ).returncode
    except OSError as exc:
        messagebox.showerror(APP_NAME, f"{tr('launch_error')} {exc}")
        return 1


def choose_folder():
    selected = filedialog.askdirectory(title=tr("folder"), parent=window)
    if selected:
        folder_value.set(selected)


def launch():
    folder = configured_folder()
    if folder is None:
        raw = folder_value.get().strip()
        selected = Path(raw).expanduser() if raw else None
        if selected is None or not selected.is_absolute() or not selected.is_dir():
            messagebox.showwarning(APP_NAME, tr("missing"))
            show_setup()
            return
        try:
            save_folder(selected.resolve())
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"{tr('saved_error')} {exc}")
            return
    window.destroy()
    sys.exit(start_app())


def clear_settings():
    if not messagebox.askyesno(tr("clear_title"), tr("clear_confirm"), parent=window):
        return
    try:
        get_settings_file().unlink(missing_ok=True)
    except OSError as exc:
        messagebox.showerror(APP_NAME, f"{tr('clear_error')} {exc}")
        return
    folder_value.set("")
    messagebox.showinfo(APP_NAME, tr("cleared"))
    show_setup()


def show_setup():
    setup_frame.pack(fill="x", pady=(12, 0))
    setup_title.configure(text=tr("setup"))
    setup_info.configure(text=tr("setup_info"))
    folder_label.configure(text=tr("folder"))
    choose_button.configure(text=tr("choose"))


def translate_ui(*_):
    language_label.configure(text=tr("language"))
    system_label.configure(text=f"{tr('system')}: {OS_NAME}")
    intro_label.configure(text=tr("intro"))
    start_button.configure(text=tr("start"))
    clear_button.configure(text=tr("clear"))
    keep_label.configure(text=tr("keep"))
    if setup_frame.winfo_manager():
        show_setup()


try:
    default_language = (locale.getlocale()[0] or "en").split("_")[0].lower()
except (ValueError, TypeError):
    default_language = "en"
if default_language not in TEXT:
    default_language = "en"

if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--folder-picker":
    from web.folder_picker import main as folder_picker_main
    sys.argv.pop(1)
    folder_picker_main()
    sys.exit(0)


window = tk.Tk()
window.title(APP_NAME)
window.geometry("510x520")
window.minsize(510, 520)
window.resizable(False, False)
window.configure(bg="#000000")

window.icon_image = load_image("icon.png", 256, 256)
if window.icon_image:
    window.iconphoto(True, window.icon_image)

style = ttk.Style(window)
style.theme_use("clam")
style.configure("Launch.TButton", padding=(12, 8), background="#292929", foreground="#f2f2f2", font=("Arial", 12), borderwidth=0)
style.map("Launch.TButton", background=[("active", "#454545")])
style.configure("Start.TButton", padding=(18, 16), background="#a82626", foreground="#ffffff", font=("Arial", 16, "bold"), borderwidth=0)
style.map("Start.TButton", background=[("active", "#c93434")])
style.configure("Launch.TCombobox", fieldbackground="#292929", background="#292929", foreground="#ffffff", selectbackground="#292929", selectforeground="#ffffff", arrowcolor="#ffffff", padding=5, font=("Arial", 11))
style.map("Launch.TCombobox", fieldbackground=[("readonly", "#292929")], foreground=[("readonly", "#ffffff")], selectbackground=[("readonly", "#292929")], selectforeground=[("readonly", "#ffffff")])

language = tk.StringVar(value=next(name for name, code in LANGUAGE_NAMES.items() if code == default_language))
folder_value = tk.StringVar()

header = tk.Frame(window, bg="#000000", padx=22, pady=15)
header.pack(fill="x")
header_image = load_image("cabe.png", 450, 100)
if header_image:
    tk.Label(header, image=header_image, bg="#000000").pack(anchor="center")
else:
    tk.Label(header, text=APP_NAME, bg="#000000", fg="#f0f0f0", font=("Arial", 17, "bold")).pack(anchor="w")

body = tk.Frame(window, bg="#000000", padx=22, pady=14)
body.pack(fill="both", expand=True)

language_row = tk.Frame(body, bg="#000000")
language_row.pack(fill="x")
language_label = tk.Label(language_row, bg="#000000", fg="#c9c9c9")
language_label.pack(side="left")
lang_combo = ttk.Combobox(language_row, textvariable=language, values=tuple(LANGUAGE_NAMES), state="readonly", width=12, style="Launch.TCombobox")
lang_combo.pack(side="right")

system_label = tk.Label(body, bg="#000000", fg="#c9c9c9", anchor="w")
system_label.pack(fill="x", pady=(12, 0))

intro_label = tk.Label(body, bg="#000000", fg="#bdbdbd", anchor="w", justify="left", wraplength=455, font=("Arial", 11))
intro_label.pack(fill="x", pady=(22, 0))

setup_frame = tk.Frame(body, bg="#000000")
setup_title = tk.Label(setup_frame, bg="#000000", fg="#eeeeee", font=("Arial", 11, "bold"), anchor="w")
setup_title.pack(fill="x")
setup_info = tk.Label(setup_frame, bg="#000000", fg="#aaaaaa", anchor="w")
setup_info.pack(fill="x", pady=(3, 10))
folder_label = tk.Label(setup_frame, bg="#000000", fg="#c9c9c9", anchor="w")
folder_label.pack(fill="x")
folder_row = tk.Frame(setup_frame, bg="#000000")
folder_row.pack(fill="x", pady=(5, 0))
tk.Entry(folder_row, textvariable=folder_value, bg="#262626", fg="#f0f0f0", insertbackground="#f0f0f0", relief="flat", font=("Arial", 10)).pack(side="left", fill="x", expand=True, ipady=7)
choose_button = ttk.Button(folder_row, command=choose_folder, style="Launch.TButton")
choose_button.pack(side="right", padx=(8, 0))

footer = tk.Frame(window, bg="#000000", padx=22, pady=15)
footer.pack(fill="x", side="bottom")
start_button = ttk.Button(footer, command=launch, style="Start.TButton")
start_button.pack(fill="x", pady=(0, 8))
clear_button = ttk.Button(footer, command=clear_settings, style="Launch.TButton")
clear_button.pack(fill="x")
keep_label = tk.Label(footer, bg="#000000", fg="#999999", font=("Arial", 9))
keep_label.pack(pady=(9, 0))

brand_image = load_image("marca.jpg", 90, 27)
if brand_image:
    brand_label = tk.Label(footer, image=brand_image, bg="#000000", cursor="hand2")
    brand_label.pack(pady=(12, 0))
    brand_label.bind("<Button-1>", lambda _event: webbrowser.open(TOOLS_URL))

language.trace_add("write", translate_ui)
if configured_folder() is None:
    show_setup()
translate_ui()
window.eval("tk::PlaceWindow . center")

if __name__ == "__main__":
    window.mainloop()
