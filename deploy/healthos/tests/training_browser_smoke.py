"""Exercise training entry with the existing isolated HTTPS/Chrome harness."""
from pathlib import Path
from auth_browser_smoke import main


if __name__ == "__main__":
    main(Path(__file__).with_suffix(".js"),
         Path(__file__).resolve().parents[3] / "output/playwright/training-update")
