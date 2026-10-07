import os
from pathlib import Path
import subprocess
import sys


def main() -> int:
    project_root = Path(__file__).resolve().parent
    os.chdir(project_root)
    requirements_path = Path("requirements.txt")
    run_web_path = Path("run_web.py")

    print("OAI-Downloader")
    print("==============")

    if not requirements_path.is_file():
        print("Error: requirements.txt was not found.", file=sys.stderr)
        return 1

    if not run_web_path.is_file():
        print("Error: run_web.py was not found.", file=sys.stderr)
        return 1

    try:
        pip_check = subprocess.run(
            [sys.executable, "-m", "pip", "--version"],
            cwd=project_root,
            check=False,
        )
    except OSError as error:
        print(f"Error: pip could not be checked: {error}", file=sys.stderr)
        return 1

    if pip_check.returncode != 0:
        print("Error: pip is not available for this Python interpreter.", file=sys.stderr)
        return pip_check.returncode

    print("Checking dependencies...", flush=True)
    try:
        pip_install = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "-r",
                "requirements.txt",
            ],
            cwd=project_root,
            check=False,
        )
    except OSError as error:
        print(f"Error: dependencies could not be checked: {error}", file=sys.stderr)
        return 1

    if pip_install.returncode != 0:
        print("Error: pip could not install or verify the dependencies.", file=sys.stderr)
        return pip_install.returncode

    print("Dependencies are ready.")
    print("Starting application...", flush=True)

    try:
        application = subprocess.run(
            [sys.executable, "run_web.py"],
            cwd=project_root,
            check=False,
        )
    except OSError as error:
        print(f"Error: the application could not be started: {error}", file=sys.stderr)
        return 1

    return application.returncode


if __name__ == "__main__":
    sys.exit(main())
