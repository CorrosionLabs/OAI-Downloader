"""Run the native folder picker in its own process and UI thread."""
import json
import sys
import tkinter as tk
from tkinter import filedialog


def main():
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(
            parent=root, initialdir=sys.argv[1], mustexist=True,
        )
        print(json.dumps(selected))
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
