"""Double-click to open the shiny bot window (no console)."""
import os
import sys

os.chdir(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.getcwd())

from shinybot.gui import main  # noqa: E402

main()
