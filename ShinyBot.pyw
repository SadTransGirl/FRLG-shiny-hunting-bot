"""Double-click to open the shiny bot window (no console)."""
import os
import sys
import traceback

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

try:
    from shinybot.gui import main

    main()
except Exception as error:  # .pyw has no console, so show what went wrong
    import tkinter.messagebox

    hint = ''
    if isinstance(error, ModuleNotFoundError):
        hint = ('\n\nA package is missing. In a terminal in this folder run:\n'
                'python -m pip install -r requirements.txt')
    tkinter.messagebox.showerror('ShinyBot failed to start',
                                 f'{error}{hint}\n\n{traceback.format_exc()[-1500:]}')
