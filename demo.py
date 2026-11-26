"""Запуск локальной демонстрации: python demo.py [--godot PATH]."""
import argparse
import shutil
import webbrowser
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parent
def build_ui():
    dest=ROOT/"runtime"/"frontend"
    shutil.copytree(ROOT/"frontend"/"public", dest, dirs_exist_ok=True)
    return dest

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--godot",metavar="EXE")
    parser.add_argument("--build-ui",action="store_true")
    parser.add_argument("--video",action="store_true",help="Открыть сайт рядом со сценой")
    args=parser.parse_args()
    ui=build_ui()
    if args.godot=="godot" and os.name=="nt":
        packages=Path(os.getenv("LOCALAPPDATA","."))/"Microsoft/WinGet/Packages"
        exe=next(packages.glob("GodotEngine.GodotEngine_*/*win64.exe"),None)
        if exe: args.godot=str(exe)
    if args.build_ui:
        print(ui)
        return
    env=dict(os.environ,HOME_MODEL="egor",STORAGE="sqlite",GATEWAY_ADAPTER="godot",GODOT_HOST="127.0.0.1",
             GATEWAY_TOKEN=secrets.token_urlsafe(32),DEMO_UI="1",PYTHONUTF8="1",FRONTEND_DIR=str(ui),
             GODOT_PORT="8765",REGISTRY_PATH=str(ROOT/"iot_gateway/config/devices.json"),
             GATEWAY_URL="http://127.0.0.1:9000",BACKEND_EVENTS_URL="http://127.0.0.1:8000/api/gateway/events")
    jobs=[]
    def launch(cmd,folder):
        jobs.append(subprocess.Popen(cmd,cwd=ROOT/folder,env=env))
    try:
        launch([sys.executable,"-m","uvicorn","app.main:app","--host","127.0.0.1","--port","8000"],"backend")
        launch([sys.executable,"-m","uvicorn","gateway.app:app","--host","127.0.0.1","--port","9000"],"iot_gateway")
        if args.godot: launch([args.godot,"--position","740,40","--path",str(ROOT/"godot_sim")],"godot_sim")
        else: launch([sys.executable,"tools/fake_godot.py"],"iot_gateway")
        print("reallysmartHOME   http://127.0.0.1:8000",flush=True)
        if args.video:
            from urllib.request import urlopen
            for _ in range(100):
                try:
                    with urlopen("http://127.0.0.1:8000/api/health",timeout=1): break
                except OSError: time.sleep(.1)
            edge=Path(os.getenv("ProgramFiles(x86)","/nonexistent"))/"Microsoft/Edge/Application/msedge.exe"
            if edge.is_file():
                subprocess.Popen([str(edge),"--new-window","--window-position=0,40","--window-size=720,850","http://127.0.0.1:8000"])
            else: webbrowser.open("http://127.0.0.1:8000")
        while all(p.poll() is None for p in jobs): time.sleep(.5)
        raise SystemExit("Один из компонентов завершился")
    except KeyboardInterrupt: pass
    finally:
        for p in reversed(jobs):
            if p.poll() is None: p.terminate()
        for p in jobs: p.wait()
if __name__=="__main__": main()
