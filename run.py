"""Single-process local launcher with a process lock."""
import argparse
import os
import webbrowser
import threading
from getpass import getpass
from pathlib import Path

import uvicorn
from app import create_app, default_data_dir


def main():
    parser=argparse.ArgumentParser(description='Q&A Assistant 로컬 실행')
    parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--data-dir',type=Path,default=default_data_dir())
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--init-admin',action='store_true',help='서버를 시작하지 않고 최초 관리자 계정을 한 번 등록합니다.')
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
    try:
        app=create_app(args.data_dir)
        runtime=app.state.runtime
        if args.init_admin:
            if not runtime.setup_required():
                raise SystemExit('이미 초기 관리자 등록이 완료되었습니다. 기존 계정으로 로그인하세요.')
            try:
                username=input('최초 관리자 아이디: ')
                password=getpass('비밀번호 (10~128자): ')
                if password != getpass('비밀번호 확인: '):
                    raise ValueError('비밀번호가 일치하지 않습니다.')
                runtime.initialize_admin(username,password)
            except (EOFError, KeyboardInterrupt):
                raise SystemExit('관리자 등록을 취소했습니다. 서버를 시작하지 않았습니다.') from None
            except ValueError as exc:
                raise SystemExit(str(exc)) from None
            print('최초 관리자 계정을 등록했습니다. 이제 start.bat 또는 python run.py로 실행하세요.')
            return
        if runtime.setup_required():
            raise SystemExit('관리자 등록이 필요합니다. 서버에서 python run.py --init-admin을 먼저 실행하세요. --data-dir를 사용했다면 같은 경로를 지정하세요.')
        if not args.no_browser:
            threading.Timer(1.2,lambda:webbrowser.open(f'http://127.0.0.1:{args.port}')).start()
        uvicorn.run(app,host='127.0.0.1',port=args.port,workers=1,access_log=False)
    finally:
        lock.close()


if __name__=='__main__':
    main()
