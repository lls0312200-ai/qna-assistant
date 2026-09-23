"""Single-process local launcher with a process lock."""
import argparse
import os
import webbrowser
import threading
from pathlib import Path

import uvicorn
from app import create_app, default_data_dir


def main():
    parser=argparse.ArgumentParser(description='Q&A Assistant 로컬 실행')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data-dir',type=Path,default=default_data_dir())
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    args.data_dir.mkdir(parents=True,exist_ok=True)
    lock=open(args.data_dir/'app.lock','a+b')
    lock.seek(0)
    if lock.read(1)==b'':
        lock.write(b'0');lock.flush()
    lock.seek(0)
    try:
        if os.name=='nt':
            import msvcrt
            msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except OSError:
        raise SystemExit('같은 데이터 폴더를 사용하는 앱이 이미 실행 중입니다.')
    app=create_app(args.data_dir)
    if not args.no_browser:
        threading.Timer(1.2,lambda:webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
    try:
        uvicorn.run(app,host='127.0.0.1',port=args.port,workers=1,access_log=False)
    finally:
        lock.close()


if __name__=='__main__':
    main()
