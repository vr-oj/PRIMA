"""Dispatch adapter helpers before PyInstaller's Qt runtime hooks load native Qt."""
import sys
if len(sys.argv) == 3 and sys.argv[1] == "--prima-mm-worker":
    from cameras.micro_manager_process import worker_main
    worker_main(int(sys.argv[2]))
    sys.exit(0)
